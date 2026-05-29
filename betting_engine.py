"""
AI 가변 베팅 엔진 — 고정 종목당 150만 원 폐지.

확신 구간: 총시드 30~40% 원샷
애매 구간: 정찰대(~100만) → 추세 확인 시 피라미딩 추가
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

try:
    import config as _cfg
except ImportError:
    _cfg = None  # type: ignore


class BetTier(str, Enum):
    CONVICTION = "conviction"
    SCOUT = "scout"
    STANDARD = "standard"


def _cfg_int(name: str, default: int) -> int:
    if _cfg is None:
        return default
    return int(getattr(_cfg, name, default))


def _cfg_float(name: str, default: float) -> float:
    if _cfg is None:
        return default
    return float(getattr(_cfg, name, default))


def account_total_seed() -> int:
    """총 운용 시드 (고정 슬롯금액 × N이 아님)."""
    explicit = _cfg_int("ACCOUNT_TOTAL_SEED", 0)
    if explicit > 0:
        return explicit
    per = _cfg_int("AUTO_TRADE_TOTAL_BUDGET", 1_500_000)
    slots = _cfg_int("MAX_SIMULTANEOUS_STOCKS", 5)
    return per * slots


def deployed_capital(positions: dict[str, dict[str, Any]]) -> int:
    total = 0
    for pos in positions.values():
        entry = int(pos.get("entry_price") or 0)
        qty = int(pos.get("quantity") or 0)
        if entry > 0 and qty > 0:
            total += entry * qty
    return total


def get_capital_snapshot(
    positions: dict[str, dict[str, Any]],
) -> dict[str, int]:
    seed = account_total_seed()
    deployed = deployed_capital(positions)
    reserve_pct = _cfg_float("BET_RESERVE_CASH_PCT", 0.05)
    reserve = int(seed * reserve_pct)
    available = max(0, seed - deployed - reserve)
    return {
        "total_seed": seed,
        "deployed": deployed,
        "reserve": reserve,
        "available": available,
    }


def score_conviction(pick: dict[str, Any]) -> float:
    """0~100 — 확신도 (수급·등락·모드·테마)."""
    score = 0.0
    chg = float(pick.get("change_rate") or 0)
    vol_pct = float(pick.get("prdy_vrss_vol_rate") or 0)
    mode_conf = float(pick.get("mode_confidence") or 50)
    scalp_score = float(pick.get("scalp_score") or 0)
    pressure = float(pick.get("intraday_pressure") or 0)

    score += min(35, chg * 4.0)
    score += min(25, vol_pct / 40.0)
    score += min(20, mode_conf * 0.2)
    score += min(15, scalp_score * 0.3)
    score += pressure * 10.0

    mode = str(pick.get("trading_mode") or "swing")
    if mode == "scalping" and chg >= 5.0 and vol_pct >= 300:
        score += 15.0
    if pick.get("setup") == "1m/3m 수급·체결강도" and pressure >= 0.55:
        score += 10.0

    return min(100.0, max(0.0, score))


def classify_tier(pick: dict[str, Any], conviction: float) -> BetTier:
    high = _cfg_float("BET_CONVICTION_SCORE_MIN", 72.0)
    scout_hi = _cfg_float("BET_SCOUT_SCORE_MAX", 58.0)
    if conviction >= high:
        return BetTier.CONVICTION
    if conviction <= scout_hi:
        return BetTier.SCOUT
    return BetTier.STANDARD


def plan_entry(
    pick: dict[str, Any],
    capital: dict[str, int],
) -> dict[str, Any]:
    """
    진입 베팅 계획.
    Returns: tier, budget_won, target_deploy_won, conviction, label
    """
    available = int(capital.get("available", 0))
    seed = int(capital.get("total_seed", account_total_seed()))
    if available <= 0:
        return {
            "tier": BetTier.STANDARD.value,
            "budget_won": 0,
            "target_deploy_won": 0,
            "conviction": 0.0,
            "label": "가용 시드 없음",
        }

    conviction = score_conviction(pick)
    tier = classify_tier(pick, conviction)
    price = int(pick.get("price") or 0)

    pct_min = _cfg_float("BET_CONVICTION_PCT_MIN", 0.30)
    pct_max = _cfg_float("BET_CONVICTION_PCT_MAX", 0.40)
    scout_won = _cfg_int("BET_SCOUT_WON", 1_000_000)
    scout_target = _cfg_int("BET_SCOUT_TARGET_DEPLOY_WON", 2_800_000)
    std_pct = _cfg_float("BET_STANDARD_PCT_OF_SEED", 0.12)

    if tier == BetTier.CONVICTION:
        target = int(seed * (pct_min + pct_max) / 2.0)
        budget = min(available, int(seed * pct_max), target)
        label = f"확신 원샷 · 시드 {pct_min*100:.0f}~{pct_max*100:.0f}%"
        return {
            "tier": tier.value,
            "budget_won": budget,
            "target_deploy_won": budget,
            "conviction": round(conviction, 1),
            "label": label,
            "pyramid_allowed": False,
        }

    if tier == BetTier.SCOUT:
        budget = min(available, scout_won, int(seed * 0.15))
        label = f"정찰대 {budget:,}원 · 확인 시 피라미딩"
        return {
            "tier": tier.value,
            "budget_won": budget,
            "target_deploy_won": min(scout_target, available + budget),
            "conviction": round(conviction, 1),
            "label": label,
            "pyramid_allowed": True,
        }

    budget = min(available, int(seed * std_pct), _cfg_int("BET_STANDARD_MAX_WON", 1_800_000))
    return {
        "tier": tier.value,
        "budget_won": budget,
        "target_deploy_won": budget,
        "conviction": round(conviction, 1),
        "label": f"표준 베팅 {budget:,}원",
        "pyramid_allowed": False,
    }


def quantity_for_budget(price: int, budget_won: int) -> int:
    if price <= 0 or budget_won <= 0:
        return 0
    return max(0, budget_won // price)


def _rising_closes(bars: list[dict], count: int = 3) -> bool:
    if len(bars) < count:
        return False
    closes = [int(b["close"]) for b in bars[-count:]]
    return all(closes[i] > closes[i - 1] for i in range(1, len(closes)))


def should_pyramid_add(
    position: dict[str, Any],
    minute_bars: list[dict] | None,
    capital: dict[str, int],
) -> dict[str, Any] | None:
    """
    정찰대 포지션 피라미딩 — 추세 유지 시 추가 매수 금액 반환.
    None이면 추가 없음.
    """
    if position.get("pyramid_done"):
        return None
    if str(position.get("bet_tier")) != BetTier.SCOUT.value:
        return None

    entry = int(position.get("entry_price") or 0)
    current = int(position.get("current_price") or entry)
    if entry <= 0 or current <= entry:
        return None

    profit_pct = (current - entry) / entry * 100.0
    min_profit = _cfg_float("BET_PYRAMID_MIN_PROFIT_PCT", 0.4)
    if profit_pct < min_profit:
        return None

    bars = minute_bars or []
    if len(bars) < 8:
        return None
    if not _rising_closes(bars, 3):
        return None

    deployed = int(position.get("deployed_won") or entry * int(position.get("quantity") or 0))
    target = int(position.get("target_deploy_won") or _cfg_int("BET_SCOUT_TARGET_DEPLOY_WON", 2_800_000))
    gap = target - deployed
    if gap <= 0:
        return None

    add_min = _cfg_int("BET_PYRAMID_ADD_MIN_WON", 1_500_000)
    add_max = _cfg_int("BET_PYRAMID_ADD_MAX_WON", 2_000_000)
    available = int(capital.get("available", 0))
    add_won = min(available, gap, add_max)
    if add_won < add_min and available >= add_min:
        add_won = min(available, add_min)
    if add_won <= 0 or quantity_for_budget(current, add_won) < 1:
        return None

    return {
        "add_budget_won": add_won,
        "add_qty": quantity_for_budget(current, add_won),
        "reason": f"정찰 확인·피라미딩 (+{profit_pct:.1f}%)",
    }
