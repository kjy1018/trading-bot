"""
장중 실시간 통합 탐색 — 단타(분봉) + 스윙(1H) 병렬 후보 선정.
당일 손절 종목은 sell_history.json 기준 entry_score_final 에 -50점 페널티.
"""

from __future__ import annotations

import logging
from typing import Any

from config import (
    LOSS_REENTRY_PENALTY_POINTS,
    MARKET_SCAN_ENABLE_FALLBACK,
    MARKET_SCAN_MIN_PICKS,
    POLLING_ENABLE_SCALP_FALLBACK,
    PULLBACK_MAX_PCT,
    PULLBACK_MIN_PCT,
    REALTIME_SCAN_BATCH_SIZE,
)
from brain import enrich_stock_with_brain
from stock_ranking import is_common_stock_for_trade
from sell_history import (
    apply_loss_penalty_to_candidates,
    get_today_stop_loss_codes,
    rank_candidates_by_final_score,
)
from trading_logic import is_polling_strategy_mode
from market_ai import get_active_allocation
from brain import rank_universe_for_scan
from intraday_attack import get_intraday_attack_phase
from ma_convergence import (
    select_convergence_candidates,
    select_morning_disparity_breakouts,
)
from stock_intraday import select_scalping_stocks
from stock_swing import select_swing_stocks

logger = logging.getLogger(__name__)


def _convergence_rank_boost(row: dict) -> float:
    """이격도 100 근접 종목 — 매수 큐 우선순위 가산."""
    try:
        import config as cfg

        if str(row.get("entry_track") or "") != "convergence":
            return 0.0
        dist = float(row.get("convergence_dist") or 99.0)
        band = float(getattr(cfg, "MA_CONVERGENCE_BAND_PCT", 3.0))
        base = float(getattr(cfg, "MA_CONVERGENCE_RANK_BOOST", 50.0))
        if dist <= band:
            return base + max(0.0, band - dist) * 5.0
        return max(0.0, base * 0.5 - dist * 2.0)
    except Exception:
        return 0.0


def _min_pick_target(max_count: int) -> int:
    return max(max_count, int(MARKET_SCAN_MIN_PICKS))


def _select_trend_follow_candidates(
    universe: list[dict],
    *,
    exclude_codes: set[str],
    max_count: int,
) -> list[dict]:
    """
    기본 추세 추종 폴백 — Brain 점수 + 당일 눌림(-13%~-1%) 종목.
    1H 차트 API 없이 유니버스 메타만 사용.
    """
    if max_count <= 0:
        return []
    lo = float(PULLBACK_MAX_PCT)
    hi = float(PULLBACK_MIN_PCT)
    candidates: list[tuple[float, dict]] = []

    for raw in universe:
        stock = enrich_stock_with_brain(dict(raw), auto_mode=True)
        code = str(stock.get("code") or "").strip()
        if len(code) != 6 or code in exclude_codes:
            continue
        if not is_common_stock_for_trade(stock):
            continue
        chg = float(stock.get("change_rate") or 0.0)
        if chg > hi or chg < lo - 3.0:
            continue
        brain = float(stock.get("brain_score") or 0.0)
        flow = float(stock.get("flow_score") or 0.0)
        dip_bonus = max(0.0, min(abs(chg), abs(lo))) * 1.5
        score = brain + flow * 0.3 + dip_bonus + float(stock.get("leader_boost") or 0)
        row = {
            **stock,
            "price": int(stock.get("price") or stock.get("current_price") or 0),
            "entry_score": round(score, 2),
            "entry_track": "trend_follow",
            "entry_basis": "trend_pullback_fallback",
            "setup": "추세추종·눌림 폴백",
            "trading_mode": stock.get("trading_mode") or "swing",
            "mode_label": stock.get("mode_label") or "스윙",
        }
        candidates.append((score, row))

    candidates.sort(key=lambda x: x[0], reverse=True)
    picks: list[dict] = []
    seen: set[str] = set()
    for _, row in candidates:
        code = row.get("code")
        if not code or code in seen:
            continue
        seen.add(code)
        picks.append(row)
        if len(picks) >= max_count:
            break
    if picks:
        logger.info(
            "추세추종 폴백 후보 %d건 (눌림 %.1f%%~%.1f%%)",
            len(picks),
            lo,
            hi,
        )
    return picks


def _collect_cap(max_count: int, universe_size: int) -> int:
    """후보 풀 수집 상한 — 페널티 반영 후 재정렬용."""
    return min(max(max_count * 4, max_count + 3), max(universe_size, 1), 40)


def select_market_entries(
    access_token: str,
    app_key: str,
    app_secret: str,
    universe: list[dict],
    *,
    exclude_codes: set[str] | None = None,
    max_count: int = 1,
    batch_offset: int = 0,
    batch_size: int | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """
    단타 + 스윙 후보 수집 → 당일 손절 페널티 반영 → entry_score_final 순 상위 N.
    """
    exclude = set(exclude_codes or ())
    batch_size = batch_size or REALTIME_SCAN_BATCH_SIZE
    universe = rank_universe_for_scan(universe)
    n = len(universe)
    stop_codes = get_today_stop_loss_codes()
    meta: dict[str, Any] = {
        "universe_size": n,
        "scalp_candidates": 0,
        "swing_candidates": 0,
        "batch_offset": batch_offset,
        "today_stop_loss_codes": sorted(stop_codes),
        "loss_penalty_points": int(LOSS_REENTRY_PENALTY_POINTS),
    }
    if max_count <= 0 or n == 0:
        return [], meta

    alloc = get_active_allocation()
    scalp_w = float(alloc.get("scalp_weight", 0.35))
    swing_w = float(alloc.get("swing_weight", 0.45))
    meta["allocation"] = alloc

    attack_phase = get_intraday_attack_phase()
    meta["attack_phase"] = attack_phase

    if is_polling_strategy_mode():
        meta["polling_mode"] = True
        if attack_phase == "morning_scalp":
            scalp_max = max_count
            swing_max = 0
        elif attack_phase == "afternoon_swing":
            scalp_max = 0
            swing_max = max_count
        else:
            scalp_max = 0
            swing_max = 0
    else:
        scalp_max = max(0, min(max_count, round(max_count * scalp_w)))
        if scalp_max <= 0 and max_count > 0 and scalp_w >= swing_w:
            scalp_max = 1
        swing_max = max(0, max_count - scalp_max)
        if swing_max <= 0 and max_count > scalp_max:
            swing_max = max(1, max_count - scalp_max)

    collect_cap = _collect_cap(max_count, n)
    pool: list[dict] = []
    seen: set[str] = set()

    if is_polling_strategy_mode() and attack_phase == "morning_scalp":
        morning_picks = select_morning_disparity_breakouts(
            access_token,
            app_key,
            app_secret,
            universe,
            exclude_codes=exclude,
            max_count=collect_cap,
        )
        meta["morning_scalp_candidates"] = len(morning_picks)
        for p in morning_picks:
            code = p.get("code")
            if code and code not in seen:
                seen.add(code)
                row = dict(p)
                row["entry_track"] = "morning_scalp"
                pool.append(row)
    elif is_polling_strategy_mode() and attack_phase == "convergence":
        conv_picks = select_convergence_candidates(
            access_token,
            app_key,
            app_secret,
            universe,
            exclude_codes=exclude,
            max_count=collect_cap,
        )
        meta["convergence_candidates"] = len(conv_picks)
        for p in conv_picks:
            code = p.get("code")
            if code and code not in seen:
                seen.add(code)
                row = dict(p)
                row["entry_track"] = "convergence"
                row["trading_mode"] = row.get("trading_mode") or "swing"
                row["mode_label"] = row.get("mode_label") or "스윙"
                pool.append(row)

    if scalp_max > 0 and not (
        is_polling_strategy_mode() and attack_phase == "morning_scalp"
    ):
        scalp_picks = select_scalping_stocks(
            access_token,
            app_key,
            app_secret,
            universe,
            exclude_codes=exclude,
            max_count=collect_cap,
        )
        meta["scalp_candidates"] = len(scalp_picks)
        for p in scalp_picks:
            code = p.get("code")
            if code and code not in seen:
                seen.add(code)
                row = dict(p)
                row["entry_track"] = "scalp"
                pool.append(row)
    else:
        meta["scalp_candidates"] = 0

    meta["scalp_slot_budget"] = scalp_max
    meta["swing_slot_budget"] = swing_max

    afternoon_swing = (
        is_polling_strategy_mode() and attack_phase == "afternoon_swing"
    )
    need_swing = swing_max > 0 or afternoon_swing or (
        is_polling_strategy_mode()
        and attack_phase == "convergence"
        and len(pool) < _min_pick_target(max_count)
    )
    if need_swing:
        if n > batch_size:
            slice_stocks = []
            for i in range(batch_size):
                slice_stocks.append(universe[(batch_offset + i) % n])
        else:
            slice_stocks = universe

        swing_picks = select_swing_stocks(
            access_token,
            app_key,
            app_secret,
            slice_stocks,
            exclude_codes=exclude | seen,
            max_count=collect_cap,
            afternoon_mode=afternoon_swing,
        )
        meta["swing_candidates"] = len(swing_picks)
        for p in swing_picks:
            code = p.get("code")
            if code and code not in seen:
                seen.add(code)
                row = dict(p)
                row["entry_track"] = "swing"
                pool.append(row)
    elif swing_max <= 0:
        meta["swing_candidates"] = 0

    target = _min_pick_target(max_count)
    meta["min_pick_target"] = target

    if MARKET_SCAN_ENABLE_FALLBACK and len(pool) < target:
        need = target - len(pool)
        meta["fallback_triggered"] = True

        if POLLING_ENABLE_SCALP_FALLBACK and need > 0:
            scalp_fb = select_scalping_stocks(
                access_token,
                app_key,
                app_secret,
                universe,
                exclude_codes=exclude | seen,
                max_count=max(need * 2, 3),
            )
            meta["scalp_fallback"] = len(scalp_fb)
            for p in scalp_fb:
                code = p.get("code")
                if code and code not in seen:
                    seen.add(code)
                    row = dict(p)
                    row["entry_track"] = "scalp_fallback"
                    pool.append(row)
            need = target - len(pool)

        if need > 0:
            trend_fb = _select_trend_follow_candidates(
                universe,
                exclude_codes=exclude | seen,
                max_count=need,
            )
            meta["trend_fallback"] = len(trend_fb)
            for p in trend_fb:
                code = p.get("code")
                if code and code not in seen:
                    seen.add(code)
                    pool.append(p)

    if not pool:
        meta["pool_size"] = 0
        return [], meta

    penalized = apply_loss_penalty_to_candidates(pool)
    for row in penalized:
        boost = _convergence_rank_boost(row)
        if boost > 0:
            row["convergence_boost"] = round(boost, 2)
            row["entry_score_final"] = round(
                float(row.get("entry_score_final") or row.get("entry_score") or 0)
                + boost,
                2,
            )
    ranked = rank_candidates_by_final_score(penalized)
    picks = ranked[:target]

    if stop_codes and picks:
        penalized_in_top = [
            p.get("code") for p in picks if int(p.get("loss_penalty") or 0) > 0
        ]
        if penalized_in_top:
            logger.info(
                "손절 페널티 종목이 높은 점수로 재진입 후보 — %s",
                penalized_in_top,
            )

    meta["pool_size"] = len(pool)
    meta["penalized_count"] = sum(1 for p in penalized if int(p.get("loss_penalty") or 0) > 0)
    return picks, meta
