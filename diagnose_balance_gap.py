"""총자산-예수금 차액 · 미체결 주문 · positions_state 동기화 진단."""
from __future__ import annotations

import json
from datetime import date

import requests

from account import extract_balance_holdings, get_account_snapshot, inquire_balance_with_retry
from auth import get_access_token
from config import ACCOUNT_NO, ACCOUNT_PROD_CODE, APP_KEY, APP_SECRET, BASE_URL
from kis_headers import build_kis_headers
from kis_rate import kis_request
from report import list_bot_held_positions


def _intish(v: object) -> int:
    text = str(v or "").strip().replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def fetch_unfilled_orders(access_token: str) -> tuple[list[dict], dict]:
    today = date.today().strftime("%Y%m%d")
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    params = {
        "CANO": ACCOUNT_NO,
        "ACNT_PRDT_CD": ACCOUNT_PROD_CODE,
        "INQR_STRT_DT": today,
        "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "00",
        "INQR_DVSN": "00",
        "PDNO": "",
        "CCLD_DVSN": "02",
        "ORD_GNO_BRNO": "",
        "ODNO": "",
        "INQR_DVSN_3": "00",
        "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    headers = build_kis_headers(
        access_token=access_token,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        tr_id="VTTC8001R",
    )
    with kis_request():
        resp = requests.get(url, headers=headers, params=params, timeout=30)
    data = resp.json()
    rows = data.get("output1") or []
    if isinstance(rows, dict):
        rows = [rows]
    return [r for r in rows if isinstance(r, dict)], data


def estimate_order_margin(row: dict) -> int:
    qty = _intish(row.get("rmnd_qty") or row.get("nccs_qty") or row.get("ord_qty"))
    price = _intish(row.get("ord_unpr") or row.get("avg_prvs") or row.get("ord_unpr"))
    if qty <= 0 or price <= 0:
        return 0
    return qty * price


def main() -> None:
    token = get_access_token()
    bal = inquire_balance_with_retry(token)
    o2 = bal.get("output2") or []
    summary = o2[0] if isinstance(o2, list) and o2 else (o2 if isinstance(o2, dict) else {})
    holdings = extract_balance_holdings(bal)

    print("=== KIS 잔고 output2 (0이 아닌 필드) ===")
    if isinstance(summary, dict):
        for k in sorted(summary.keys()):
            v = summary.get(k)
            if v not in (None, "", "0", 0):
                print(f"  {k}: {v}")

    cash = _intish(summary.get("dnca_tot_amt"))
    tot = _intish(summary.get("tot_evlu_amt"))
    scts = _intish(summary.get("scts_evlu_amt"))
    evlu_smtl = _intish(summary.get("evlu_amt_smtl_amt"))
    ord_psbl = _intish(summary.get("ord_psbl_cash") or summary.get("nrcvb_buy_amt"))
    buy_pwr = _intish(summary.get("buy_pwr") or summary.get("nrcvb_buy_amt"))

    h_eval = sum(_intish(h.get("eval_amount")) for h in holdings.values())

    print("\n=== 잔고 요약 ===")
    print(f"보유 종목: {len(holdings)}건")
    for code, h in holdings.items():
        print(
            f"  {code} {h.get('name')} qty={h.get('quantity')} "
            f"eval={_intish(h.get('eval_amount')):,}원"
        )
    print(f"예수금(dnca_tot_amt): {cash:,}원")
    print(f"유가평가(scts_evlu_amt): {scts:,}원")
    print(f"평가합(evlu_amt_smtl_amt): {evlu_smtl:,}원")
    print(f"보유합산(eval): {h_eval:,}원")
    print(f"총자산(tot_evlu_amt): {tot:,}원")
    print(f"차액 tot - cash: {tot - cash:,}원")
    print(f"차액 tot - (cash + 보유평가): {tot - cash - h_eval:,}원")
    if ord_psbl:
        print(f"주문가능현금(ord_psbl/nrcvb): {ord_psbl:,}원")
    if buy_pwr:
        print(f"매수가능(buy_pwr): {buy_pwr:,}원")

    unfilled, meta = fetch_unfilled_orders(token)
    print("\n=== KIS 미체결 주문 (inquire-daily-ccld CCLD_DVSN=02) ===")
    print(f"API: rt_cd={meta.get('rt_cd')} msg={meta.get('msg1')}")
    print(f"미체결 건수: {len(unfilled)}")
    total_margin = 0
    for row in unfilled:
        margin = estimate_order_margin(row)
        total_margin += margin
        code = row.get("pdno") or row.get("mksc_shrn_iscd")
        print(
            f"  ODNO={row.get('odno')} {row.get('prdt_name') or row.get('item_name')}({code}) "
            f"{row.get('sll_buy_dvsn_cd_name') or row.get('sll_buy_dvsn_cd')} "
            f"미체결={row.get('rmnd_qty') or row.get('nccs_qty')}주 "
            f"@ {row.get('ord_unpr')}원 · 추정금액~{margin:,}원"
        )
    print(f"미체결 추정 합계: {total_margin:,}원")

    print("\n=== positions_state.json (봇 보유 인식) ===")
    bot_rows = list_bot_held_positions()
    if not bot_rows:
        print("  (보유 0건 — 모든 슬롯 empty)")
    for p in bot_rows:
        print(
            f"  {p.get('code')} {p.get('name')} qty={p.get('quantity')} "
            f"slot={p.get('slot_uid')} lock={p.get('slot_lock')}"
        )

    try:
        from scheduler import get_order_status_snapshot

        orders = get_order_status_snapshot()
        print("\n=== 봇 내부 주문 큐 (scheduler 메모리) ===")
        open_orders = [
            o
            for o in orders
            if str(o.get("status")) in {"queued", "submitting", "submitted", "pending_fill", "partial_fill"}
        ]
        print(f"미완료 주문: {len(open_orders)}건 / 전체 {len(orders)}건")
        for o in open_orders:
            qty = o.get("quantity")
            price = o.get("estimated_fill_price") or o.get("reference_price")
            est = _intish(qty) * _intish(price)
            print(
                f"  {o.get('action')} {o.get('code')} status={o.get('status')} "
                f"qty={qty} ticket={o.get('ticket_id')} est~{est:,}원"
            )
    except Exception as exc:
        print(f"\n봇 주문 큐 조회 스킵 (Streamlit 미기동?): {exc}")

    snap = get_account_snapshot(
        token, sync_runtime_positions=False, bump_positions_revision=False
    )
    print("\n=== get_account_snapshot (봇 계산) ===")
    print(
        f"cash={snap.get('cash'):,} stock_eval={snap.get('stock_eval'):,} "
        f"total_assets={snap.get('total_assets'):,} "
        f"account_total_eval={snap.get('account_total_eval'):,}"
    )

    print("\n=== 동기화 판정 ===")
    broker_empty = len(holdings) == 0
    bot_empty = len(bot_rows) == 0
    bfdy_sll = _intish(summary.get("bfdy_sll_amt"))
    bfdy_tlex = _intish(summary.get("bfdy_tlex_amt"))
    gap = tot - cash
    if bfdy_sll > 0 and abs(gap - (bfdy_sll - bfdy_tlex)) <= 2000:
        print(
            f"-> 미체결 없음 · 차액 {gap:,}원 = 전일매도(bfdy_sll_amt {bfdy_sll:,}) "
            f"- 수수료({bfdy_tlex:,}) = {bfdy_sll - bfdy_tlex:,}원"
        )
        print("   총자산(tot_evlu_amt)에는 반영됐지만 예수금(dnca_tot_amt)에는 아직 미입금(정산 대기)")
    elif unfilled:
        print(f"→ 미체결 {len(unfilled)}건 · 추정 {total_margin:,}원이 예수금 외에 묶였을 수 있음")
    elif broker_empty and bot_empty and tot > cash:
        print(
            f"→ 미체결 없음 · 보유 0인데 tot-cash={tot-cash:,}원 → "
            "KIS output2의 scts_evlu_amt/유가평가 잔존 또는 총자산 산식 차이 가능"
        )
        if scts > 0 or h_eval > 0:
            print(f"   scts_evlu_amt={scts:,} — UI '보유 0'이어도 API 유가평가 필드에 잔액 있음")
    elif broker_empty and not bot_empty:
        print("→ 증권사 0보유 vs positions_state 불일치 — 동기화 지연/유령 포지션")
    else:
        print("→ broker/bot 일치 또는 보유 존재")


if __name__ == "__main__":
    main()
