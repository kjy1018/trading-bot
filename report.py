"""
리포트·정산 수익률 정규화.

※ 보유 슬롯 파일 = positions_state.json (schema: slot_portfolio_v2).
  코드·문서에서 portfolio.json 으로 부르는 경우 동일 파일을 가리킵니다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

PORTFOLIO_STATE_FILE = Path(config.PROJECT_DIR) / "positions_state.json"


def load_portfolio_state_raw() -> dict[str, Any]:
    """positions_state.json 원본."""
    if not PORTFOLIO_STATE_FILE.is_file():
        return {}
    try:
        return json.loads(PORTFOLIO_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("portfolio 로드 실패: %s", exc)
        return {}


def list_bot_held_positions(*, from_disk: bool = True) -> list[dict[str, Any]]:
    """봇이 '보유 중'으로 인식하는 종목 (슬롯 filled + quantity>0)."""
    rows: list[dict[str, Any]] = []
    if from_disk:
        try:
            import trade_state

            persisted = trade_state.load_persisted_positions()
            for code, pos in sorted(persisted.items()):
                if int(pos.get("quantity") or 0) > 0:
                    rows.append({**dict(pos), "code": code})
            return rows
        except Exception as exc:
            logger.debug("trade_state 로드 실패, 파일 직접 파싱: %s", exc)

    data = load_portfolio_state_raw()
    categories = data.get("categories") or {}
    for _cat, slots in categories.items():
        if not isinstance(slots, dict):
            continue
        for _uid, entry in slots.items():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status") or "") != "filled":
                continue
            pos = entry.get("position")
            if not isinstance(pos, dict):
                continue
            qty = int(pos.get("quantity") or 0)
            if qty <= 0:
                continue
            code = str(pos.get("code") or entry.get("code") or "").strip()
            rows.append({**pos, "code": code, "slot_uid": entry.get("slot_uid")})
    return rows


def count_broker_holdings(account: dict[str, Any]) -> int:
    """KIS 잔고 스냅샷 기준 실보유 종목 수."""
    holdings = account.get("holdings")
    if isinstance(holdings, dict) and holdings:
        return sum(
            1
            for h in holdings.values()
            if isinstance(h, dict) and int(h.get("quantity") or 0) > 0
        )
    stock_eval = int(account.get("stock_eval") or account.get("total_eval") or 0)
    return 1 if stock_eval > 0 else 0


def count_bot_holdings(positions: dict[str, Any] | list[dict[str, Any]] | None = None) -> int:
    if positions is None:
        return len(list_bot_held_positions())
    if isinstance(positions, dict):
        return sum(1 for p in positions.values() if int(p.get("quantity") or 0) > 0)
    return sum(1 for p in positions if int(p.get("quantity") or 0) > 0)


def has_no_current_holdings(
    account: dict[str, Any],
    *,
    positions: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> bool:
    """증권사·봇 모두 보유 0 — 리포트 손실률 0% 예외 대상."""
    return count_broker_holdings(account) == 0 and count_bot_holdings(positions) == 0


def normalize_settlement_for_report(
    account: dict[str, Any],
    *,
    positions: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    리포트용 정산 지표.
    보유 종목이 없으면 손실률·당일 평가손익률을 0%로 고정 (유령/전량매도 직후 KIS 왜곡 방지).
    """
    from account import dashboard_pnl_from_account

    acct = dict(account or {})
    base = dashboard_pnl_from_account(acct)
    cash = int(acct.get("cash") or base.get("current_deposit") or 0)
    stock_eval = int(acct.get("stock_eval") or acct.get("total_eval") or 0)
    daily_pnl = int(acct.get("daily_eval_pnl") or 0)
    daily_pct = float(acct.get("daily_eval_pnl_pct") or 0.0)

    out: dict[str, Any] = {
        "return_rate": float(base["return_rate"]),
        "profit_loss": int(base["profit_loss"]),
        "total_assets": int(base["total_assets"]),
        "current_deposit": int(base["current_deposit"]),
        "current_stock_valuation": int(base["current_stock_valuation"]),
        "daily_eval_pnl": daily_pnl,
        "daily_eval_pnl_pct": daily_pct,
        "cash": cash,
        "stock_eval": stock_eval,
        "holdings_zero_override": False,
        "broker_holding_count": count_broker_holdings(acct),
        "bot_holding_count": count_bot_holdings(positions),
    }

    if has_no_current_holdings(acct, positions=positions):
        out["holdings_zero_override"] = True
        out["return_rate"] = 0.0
        out["profit_loss"] = 0
        out["daily_eval_pnl"] = 0
        out["daily_eval_pnl_pct"] = 0.0
        logger.info(
            "리포트 보유 0건 — 손실률 0%% 고정 (실제 예수금 %s, 주식평가 %s)",
            f"{cash:,}",
            f"{stock_eval:,}",
        )
    return out


def apply_report_settlement_to_account(account: dict[str, Any], settlement: dict[str, Any]) -> dict[str, Any]:
    """스냅샷 dict에 정규화된 정산 필드 병합."""
    acct = dict(account)
    acct["return_rate"] = settlement["return_rate"]
    acct["profit_loss"] = settlement["profit_loss"]
    acct["total_assets"] = settlement["total_assets"]
    acct["daily_eval_pnl"] = settlement["daily_eval_pnl"]
    acct["daily_eval_pnl_pct"] = settlement["daily_eval_pnl_pct"]
    acct["total_return_pct"] = settlement["return_rate"]
    acct["settlement_return_pct"] = settlement["return_rate"]
    acct["settlement_profit_loss"] = settlement["profit_loss"]
    acct["holdings_zero_override"] = settlement.get("holdings_zero_override", False)
    return acct


def purge_phantom_positions_if_broker_empty(
    account: dict[str, Any],
) -> list[str]:
    """
    증권사 보유 0인데 positions_state 에만 남은 종목 제거.
    반환: 삭제된 종목코드 목록.
    """
    if count_broker_holdings(account) > 0:
        return []
    bot_rows = list_bot_held_positions()
    if not bot_rows:
        return []

    removed = [str(r.get("code") or "").strip() for r in bot_rows if r.get("code")]
    try:
        import trade_state
        from slot_registry import apply_config_to_portfolio_state, reconcile_holdings_to_slots

        slots = trade_state.get_slots_book()
        positions, slots = apply_config_to_portfolio_state({}, slots, recalc_targets=False)
        reconcile_holdings_to_slots(slots, positions)
        trade_state.save_portfolio_state(positions, slots)
        logger.warning(
            "증권사 보유 0 — 유령 포지션 제거: %s",
            ", ".join(removed),
        )
    except Exception as exc:
        logger.exception("유령 포지션 제거 실패: %s", exc)
    return removed
