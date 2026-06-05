"""
모의투자 주식잔고조회 (국내주식-006, TR_ID VTTC8434R).

※ inquire-balance 는 계좌비밀번호(TRADE_PWD)·hashkey 가 필요 없는 조회 API 입니다.
  (주문/정정·취소 POST 등에만 hashkey 사용)
"""

from __future__ import annotations

from datetime import datetime
import logging

import requests

from config import (
    ACCOUNT_INITIAL_PRINCIPAL,
    ACCOUNT_NO,
    ACCOUNT_PROD_CODE,
    APP_KEY,
    APP_SECRET,
    BASE_URL,
)
from kis_headers import build_kis_headers
from kis_rate import (
    KISRateLimitError,
    _response_indicates_rate_limit,
    kis_request,
)
from trade_state import sync_positions_from_broker_holdings

logger = logging.getLogger(__name__)

BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
TR_ID_MOCK = "VTTC8434R"

# KIS 가 만료 토큰을 HTTP 500 + 아래 코드로 반환하는 경우가 많음
_TOKEN_EXPIRED_CODES = frozenset({"EGW00123", "EGW00002", "EGW00121"})


def _intish(value: object) -> int:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _floatish(value: object) -> float:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


# 대시보드·디스코드 — 수익률은 아래 fixed_seed 공식만 사용 (비중·슬롯 시드 없음)
FIXED_SEED_WON = 10_000_000
FIXED_ORIGINAL_CAPITAL_WON = FIXED_SEED_WON
ORIGINAL_SEED_WON = FIXED_SEED_WON
DASHBOARD_FIXED_PRINCIPAL_WON = FIXED_SEED_WON


def compute_total_assets(stock_eval: int, cash: int) -> int:
    """전체 자산 = 주식 평가금액 + 예수금."""
    return int(stock_eval or 0) + int(cash or 0)


def account_stock_eval_and_cash(account: dict[str, object]) -> tuple[int, int]:
    """예수금 + 주식 평가액 — KIS 잔고 필드 우선 (보유 합산은 보조)."""
    current_deposit = _intish(account.get("cash"))
    kis_stock = _intish(account.get("stock_eval") or account.get("total_eval"))
    if kis_stock > 0:
        return kis_stock, current_deposit

    current_stock_valuation = 0
    holdings = account.get("holdings")
    if isinstance(holdings, dict) and holdings:
        for row in holdings.values():
            if not isinstance(row, dict):
                continue
            ev = _intish(row.get("eval_amount"))
            if ev > 0:
                current_stock_valuation += ev
                continue
            qty = _intish(row.get("quantity"))
            px = _intish(row.get("current_price"))
            if qty > 0 and px > 0:
                current_stock_valuation += qty * px
    return current_stock_valuation, current_deposit


def compute_realized_return_metrics(
    current_deposit: int,
    current_stock_valuation: int,
) -> dict[str, int | float]:
    """
    유일한 수익률 공식 — 대시보드·디스코드·보고서 공통.

        total_assets = current_deposit + current_stock_valuation
        fixed_seed = 10_000_000
        return_rate = ((total_assets - fixed_seed) / fixed_seed) * 100
    """
    current_deposit = int(current_deposit or 0)
    current_stock_valuation = int(current_stock_valuation or 0)
    total_assets = current_deposit + current_stock_valuation
    fixed_seed = FIXED_SEED_WON
    profit_loss = total_assets - fixed_seed
    return_rate = ((total_assets - fixed_seed) / fixed_seed) * 100.0

    return {
        "current_deposit": current_deposit,
        "current_stock_valuation": current_stock_valuation,
        "total_assets": total_assets,
        "fixed_seed": fixed_seed,
        "profit_loss": profit_loss,
        "return_rate": round(return_rate, 2),
        # 레거시 별칭
        "cash": current_deposit,
        "stock_eval": current_stock_valuation,
        "realized_pnl": profit_loss,
        "realized_return_pct": round(return_rate, 2),
        "total_asset_pnl": profit_loss,
        "total_asset_return_pct": round(return_rate, 2),
    }


def dashboard_pnl_from_account(account: dict[str, object]) -> dict[str, int | float]:
    """계좌 스냅샷 → compute_realized_return_metrics (단일 공식)."""
    stock_val, deposit = account_stock_eval_and_cash(account)
    return compute_realized_return_metrics(deposit, stock_val)


def resolve_principal_won(principal: int | None = None) -> int:
    base = int(principal if principal is not None else ACCOUNT_INITIAL_PRINCIPAL)
    return max(0, base)


def compute_principal_pnl(
    stock_eval: int,
    cash: int,
    *,
    principal: int | None = None,
) -> int:
    """손익(원) — 항상 profit_loss 공식 (인자 principal 무시)."""
    _ = principal
    return int(compute_realized_return_metrics(cash, stock_eval)["profit_loss"])


def compute_principal_return_pct(
    stock_eval: int,
    cash: int,
    *,
    principal: int | None = None,
) -> float:
    """수익률(%) — 항상 return_rate 공식 (인자 principal 무시)."""
    _ = principal
    return float(compute_realized_return_metrics(cash, stock_eval)["return_rate"])


def build_fixed_hero_dashboard_metrics(
    stock_eval: int,
    cash: int,
    *,
    seed: int | None = None,
) -> dict[str, int | float]:
    _ = seed
    return compute_realized_return_metrics(cash, stock_eval)


def _kis_rate_to_pct(value: object) -> float:
    """
    KIS 수익률 필드(소수) → 표시용 %.
    예: -0.01006107 → -1.01%
    """
    raw = _floatish(value)
    if raw == 0.0:
        return 0.0
    if abs(raw) < 1.0:
        return round(raw * 100.0, 2)
    return round(raw, 2)


def _parse_account_summary_row(summary: dict) -> dict[str, object]:
    """잔고조회 output2[0] — 당일 자산증감·총 평가수익률."""
    total_eval = _intish(summary.get("tot_evlu_amt"))
    prev_eval = _intish(summary.get("bfdy_tot_asst_evlu_amt"))
    daily_change_amt = _intish(summary.get("asst_icdc_amt"))
    if daily_change_amt == 0 and total_eval and prev_eval:
        daily_change_amt = total_eval - prev_eval

    daily_change_pct = _kis_rate_to_pct(summary.get("asst_icdc_erng_rt"))
    if daily_change_pct == 0.0 and prev_eval > 0 and daily_change_amt:
        daily_change_pct = round(daily_change_amt / prev_eval * 100.0, 2)

    purchase_amt = _intish(summary.get("pchs_amt_smtl_amt"))
    eval_pnl = _intish(summary.get("evlu_pfls_smtl_amt"))

    return {
        "daily_eval_pnl": daily_change_amt,
        "daily_eval_pnl_pct": daily_change_pct,
        "total_eval_pnl": eval_pnl,
        "total_purchase_amt": purchase_amt,
        "prev_day_total_eval": prev_eval,
    }


class KISApiError(RuntimeError):
    """한국투자증권 API 비즈니스/HTTP 오류."""

    def __init__(
        self,
        message: str,
        *,
        msg_cd: str | None = None,
        http_status: int | None = None,
        token_expired: bool = False,
    ) -> None:
        super().__init__(message)
        self.msg_cd = msg_cd
        self.http_status = http_status
        self.token_expired = token_expired


def _normalize_cano(cano: str) -> str:
    digits = "".join(ch for ch in str(cano).strip() if ch.isdigit())
    if len(digits) != 8:
        raise ValueError(
            f"CANO(종합계좌번호)는 8자리여야 합니다. 현재: {cano!r}"
        )
    return digits


def _normalize_prod_code(code: str) -> str:
    prod = str(code).strip()
    if len(prod) != 2 or not prod.isdigit():
        raise ValueError(
            f"ACNT_PRDT_CD(계좌상품코드)는 2자리여야 합니다. 현재: {code!r}"
        )
    return prod


def _balance_query_params() -> dict[str, str]:
    """공식 open-trading-api inquire_balance 와 동일한 쿼리 파라미터."""
    return {
        "CANO": _normalize_cano(ACCOUNT_NO),
        "ACNT_PRDT_CD": _normalize_prod_code(ACCOUNT_PROD_CODE),
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "01",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }


def _parse_balance_response(response: requests.Response) -> dict:
    """HTTP 상태와 무관하게 KIS JSON 본문을 검사합니다."""
    try:
        data = response.json()
    except ValueError as exc:
        raise KISApiError(
            f"잔고 조회 응답 JSON 파싱 실패 (HTTP {response.status_code}): "
            f"{response.text[:300]}",
            http_status=response.status_code,
        ) from exc

    rt_cd = str(data.get("rt_cd", ""))
    msg_cd = str(data.get("msg_cd") or "")
    msg1 = str(data.get("msg1") or "알 수 없는 오류")

    if response.status_code == 403:
        raise KISApiError(
            f"잔고 조회 403 Forbidden [{msg_cd}]: {msg1}",
            msg_cd=msg_cd,
            http_status=403,
            token_expired=True,
        )

    if rt_cd == "0":
        return data

    if _response_indicates_rate_limit(data, response.text):
        raise KISRateLimitError(
            f"잔고 조회 속도 제한 [{msg_cd}]: {msg1}",
            msg_cd=msg_cd,
        )

    token_expired = msg_cd in _TOKEN_EXPIRED_CODES or "token" in msg1.lower()
    raise KISApiError(
        f"잔고 조회 실패 [{msg_cd}]: {msg1}",
        msg_cd=msg_cd,
        http_status=response.status_code,
        token_expired=token_expired,
    )


def inquire_balance(access_token: str, tr_cont: str = "") -> dict:
    """
    주식잔고조회 — output1(종목별), output2(계좌 요약).

    필수: Bearer 토큰, appkey/appsecret, tr_id(VTTC8434R), 쿼리 파라미터.
    TRADE_PWD / hashkey: 이 API 에서는 사용하지 않음.
    """
    if not access_token or not access_token.strip():
        raise ValueError("access_token 이 비어 있습니다.")

    url = f"{BASE_URL}{BALANCE_PATH}"
    headers = build_kis_headers(
        access_token=access_token.strip(),
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        tr_id=TR_ID_MOCK,
        tr_cont=tr_cont,
    )
    params = _balance_query_params()

    with kis_request():
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30,
        )
        return _parse_balance_response(response)


def inquire_balance_with_retry(access_token: str | None = None) -> dict:
    """만료·403 시 캐시 삭제 후 신규 토큰으로 1회 재시도."""
    from auth import get_access_token, refresh_access_token_after_forbidden

    token = (access_token or "").strip() or get_access_token()
    try:
        return inquire_balance(token)
    except KISApiError as exc:
        if exc.http_status == 403:
            logger.warning("잔고 조회 403 Forbidden — 토큰 캐시 삭제 후 재발급")
            fresh = refresh_access_token_after_forbidden()
            return inquire_balance(fresh)
        if not exc.token_expired:
            raise
        logger.warning("잔고 조회 토큰 만료 — 재발급 후 재시도 (%s)", exc.msg_cd)
        fresh = refresh_access_token_after_forbidden()
        return inquire_balance(fresh)


def _resolve_stock_eval_amount(summary: dict, holdings: dict[str, dict]) -> int:
    """유가(주식) 평가금액 — tot_evlu_amt(계좌 총자산)와 구분."""
    stock_eval = _intish(summary.get("scts_evlu_amt"))
    if stock_eval <= 0:
        stock_eval = _intish(summary.get("evlu_amt_smtl_amt"))
    if stock_eval <= 0:
        stock_eval = sum(_intish(h.get("eval_amount")) for h in holdings.values())
    return stock_eval


def get_account_summary(access_token: str | None = None) -> tuple[int, int]:
    """주식 평가금액, 예수금을 반환합니다."""
    data = inquire_balance_with_retry(access_token)
    output2 = data.get("output2") or []
    summary = output2[0] if isinstance(output2, list) and output2 else output2 or {}
    holdings = extract_balance_holdings(data)
    stock_eval = _resolve_stock_eval_amount(summary, holdings)
    cash = _intish(summary.get("dnca_tot_amt"))
    return stock_eval, cash


def extract_balance_holdings(data: dict) -> dict[str, dict]:
    """잔고조회 output1 -> 코드별 보유 스냅샷."""
    output1 = data.get("output1") or []
    if isinstance(output1, dict):
        output1 = [output1]

    holdings: dict[str, dict] = {}
    for row in output1:
        if not isinstance(row, dict):
            continue
        code = str(
            row.get("pdno")
            or row.get("mksc_shrn_iscd")
            or row.get("stck_shrn_iscd")
            or ""
        ).strip()
        code = "".join(ch for ch in code if ch.isdigit())[-6:]
        if len(code) != 6:
            continue

        qty = _intish(row.get("hldg_qty") or row.get("hold_qty") or row.get("qty"))
        if qty <= 0:
            continue

        holdings[code] = {
            "code": code,
            "name": str(
                row.get("prdt_name")
                or row.get("hts_kor_isnm")
                or row.get("item_name")
                or code
            ).strip(),
            "quantity": qty,
            "avg_price": _intish(
                row.get("pchs_avg_pric")
                or row.get("pchs_avg_pric")
                or row.get("avg_buy_price")
                or row.get("pchs_unpr")
            ),
            "current_price": _intish(
                row.get("prpr")
                or row.get("stck_prpr")
                or row.get("now_pric")
                or row.get("current_price")
            ),
            "eval_pnl": _intish(
                row.get("evlu_pfls_amt")
                or row.get("evlu_pfls_smtl_amt")
                or row.get("profit_loss")
            ),
            "eval_amount": _intish(
                row.get("evlu_amt")
                or row.get("evlu_amt_smtl")
                or row.get("evaluation_amount")
            ),
        }
    return holdings


def get_holdings_snapshot(access_token: str | None = None) -> dict[str, dict]:
    """코드별 실보유 수량/평단/평가 스냅샷."""
    data = inquire_balance_with_retry(access_token)
    return extract_balance_holdings(data)


def get_account_snapshot(
    access_token: str | None = None,
    *,
    sync_runtime_positions: bool = True,
    bump_positions_revision: bool = True,
) -> dict[str, object]:
    """계좌 요약 + 보유 종목 스냅샷 + 당일/총 수익 지표(KIS output2)."""
    data = inquire_balance_with_retry(access_token)
    output2 = data.get("output2") or []
    summary = output2[0] if isinstance(output2, list) and output2 else output2 or {}
    holdings = extract_balance_holdings(data)
    if sync_runtime_positions:
        synced_count = sync_positions_from_broker_holdings(
            holdings,
            bump_revision=bump_positions_revision,
        )
        logger.info(
            "메모리 포지션 동기화: broker %d종목 → runtime %d종목 %s",
            len(holdings),
            synced_count,
            sorted(holdings.keys()),
        )
    pnl = _parse_account_summary_row(summary if isinstance(summary, dict) else {})
    stock_eval = _resolve_stock_eval_amount(summary, holdings)
    cash = _intish(summary.get("dnca_tot_amt"))
    account_total_eval = _intish(summary.get("tot_evlu_amt"))
    realized = compute_realized_return_metrics(cash, stock_eval)
    return_rate = float(realized["return_rate"])
    profit_loss = int(realized["profit_loss"])
    return {
        # total_eval: 레거시 키 — 주식 평가금액만 (계좌 총자산 아님)
        "total_eval": stock_eval,
        "stock_eval": stock_eval,
        "cash": cash,
        "total_assets": int(realized["total_assets"]),
        "profit_loss": profit_loss,
        "return_rate": return_rate,
        "account_total_eval": account_total_eval,
        "holdings": holdings,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_return_pct": return_rate,
        "account_return_pct": return_rate,
        "realized_return_pct": return_rate,
        "account_pnl": profit_loss,
        "realized_pnl": profit_loss,
        "principal_return_pct": return_rate,
        "principal_pnl": profit_loss,
        **pnl,
    }


def get_daily_trade_settlement(
    *,
    trade_date: str | None = None,
) -> dict[str, object]:
    """
    장마감·정산용 당일 매매 집계.
    trade_state.json 없이 trade_history.db 만으로 조회 가능.
    """
    from datetime import date

    import trade_history_db as thdb

    thdb.init_trade_history_db()
    day = trade_date or date.today().isoformat()
    agg = thdb.aggregate_daily(day)
    receipts = thdb.list_sell_receipts_for_date(day)
    buys = thdb.list_trades_for_date(day, side="buy")
    return {
        "trade_date": day,
        "trade_count": int(agg.get("trade_count") or 0),
        "realized_pnl": int(agg.get("total_pnl") or 0),
        "sell_count": int(agg.get("sell_count") or 0),
        "buy_count": len(buys),
        "completed_trades": receipts,
        "all_trades": buys + thdb.list_trades_for_date(day, side="sell"),
    }
