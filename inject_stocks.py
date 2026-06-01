#!/usr/bin/env python3
"""
실계좌 보유 종목을 4세대 스윙 봇 메모리/디스크 포맷으로 주입합니다.

※ 보유 슬롯은 trade_state 메모리 스토어(선택: POSITIONS_PERSIST_PATH)에 저장됩니다.
  trade_state.json 은 당일 청산 영수증(completed_trades) 전용이며,
  슬롯 1·2 순서는 positions 딕셔너리 삽입 순서 → 대시보드 1·2번 슬롯에 반영됩니다.

실행:
  python inject_stocks.py
  python inject_stocks.py --entry-date 2026-05-15
  python inject_stocks.py --fetch-price   # 토큰 가능 시 현재가·수익률 반영
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import trade_state

TRADE_STATE_FILE = trade_state.TRADE_STATE_FILE

# 슬롯 순서 = 아래 리스트 순서 (1번 → 2번)
INJECTIONS: list[dict] = [
    {
        "slot": 1,
        "name": "주성엔지니어링",
        "code": "036930",
        "entry_price": 193_450,
        "quantity": 10,
    },
    {
        "slot": 2,
        "name": "알테오젠",
        "code": "196170",
        "entry_price": 376_375,
        "quantity": 2,
    },
]


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _build_position(
    *,
    slot: int,
    name: str,
    code: str,
    entry_price: int,
    quantity: int,
    entry_date: str,
    current_price: int | None = None,
) -> dict:
    price = current_price if current_price and current_price > 0 else entry_price
    profit_pct = (
        (price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
    )
    peak = max(entry_price, price)

    return {
        "slot": slot,
        "code": code,
        "name": name,
        "entry_price": entry_price,
        "quantity": quantity,
        "current_price": price,
        "peak_price": peak,
        "trailing_active": False,
        "trailing_stop_price": entry_price,
        "profit_pct": round(profit_pct, 2),
        "peak_profit_pct": round(
            (peak - entry_price) / entry_price * 100 if entry_price else 0.0, 2
        ),
        "entry_change_rate": 0.0,
        "swing_score": None,
        "entry_date": entry_date,
        "updated_at": _now_str(),
        "injected": True,
        "injected_at": _now_str(),
    }


def _fetch_current_prices(codes: list[str]) -> dict[str, int]:
    try:
        import config
        from auth import get_access_token
        from kis_rate import kis_loop_pause
        from stock import get_current_price

        token = get_access_token()
        out: dict[str, int] = {}
        for idx, code in enumerate(codes):
            if idx > 0:
                kis_loop_pause()
            q = get_current_price(
                token, code, config.APP_KEY, config.APP_SECRET
            )
            out[code] = int(q["price"])
        return out
    except Exception as exc:
        print(f"[경고] 현재가 조회 실패 — 평단가로 채웁니다: {exc}", file=sys.stderr)
        return {}


def _save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


def _ensure_trade_state_daily() -> None:
    """trade_state.json 이 없거나 날짜만 맞추어 두기 (영수증 파일 손상 방지)."""
    today = date.today().isoformat()
    if TRADE_STATE_FILE.is_file():
        try:
            raw = json.loads(TRADE_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("date") == today:
                return
        except json.JSONDecodeError:
            pass
    _save_json(
        TRADE_STATE_FILE,
        {
            "date": today,
            "total_trade_count": 0,
            "total_realized_profit": 0,
            "completed_trades": [],
        },v
    )


def inject(
    *,
    entry_date: str | None = None,
    fetch_price: bool = False,
    merge: bool = False,
) -> dict[str, dict]:
    entry_date = entry_date or date.today().isoformat()
    codes = [s["code"] for s in INJECTIONS]
    live_prices = _fetch_current_prices(codes) if fetch_price else {}

    existing: dict[str, dict] = {}
    if merge:
        existing = trade_state.load_persisted_positions()

    positions: dict[str, dict] = {} if not merge else {}
    if merge:
        for code, pos in existing.items():
            if code not in codes:
                positions[code] = pos

    for spec in INJECTIONS:
        code = spec["code"]
        cur = live_prices.get(code)
        positions[code] = _build_position(
            slot=spec["slot"],
            name=spec["name"],
            code=code,
            entry_price=spec["entry_price"],
            quantity=spec["quantity"],
            entry_date=entry_date,
            current_price=cur,
        )

    trade_state.save_persisted_positions(positions)
    _ensure_trade_state_daily()
    return positions


def main() -> int:
    parser = argparse.ArgumentParser(
        description="스윙 봇 보유 슬롯(1·2)에 종목을 메모리 포지션 스토어에 주입"
    )
    parser.add_argument(
        "--entry-date",
        default=date.today().isoformat(),
        help="매수일자 (YYYY-MM-DD), 기본값: 오늘",
    )
    parser.add_argument(
        "--fetch-price",
        action="store_true",
        help="KIS API 로 현재가·수익률 반영 (토큰 필요)",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="기존 3번 이후 슬롯 등 다른 종목은 유지하고 1·2번만 덮어쓰기",
    )
    args = parser.parse_args()

    positions = inject(
        entry_date=args.entry_date,
        fetch_price=args.fetch_price,
        merge=args.merge,
    )

    print("[OK] 주입 완료")
    print(f"   revision: {trade_state.get_positions_revision()}")
    print(f"   trade_state.json: 당일 영수증만 유지 ({TRADE_STATE_FILE})")
    print()
    for spec in INJECTIONS:
        p = positions[spec["code"]]
        print(
            f"   슬롯 {spec['slot']}: {p['name']} ({p['code']}) "
            f"{p['quantity']}주 @ {p['entry_price']:,}원 "
            f"| 현재 {p['current_price']:,}원 ({p['profit_pct']:+.2f}%) "
            f"| 매수일 {p['entry_date']}"
        )
    print()
    print("   Streamlit/스케줄러를 재시작하면 대시보드 슬롯에 반영됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
