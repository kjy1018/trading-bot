"""
장중 실시간 통합 탐색 — 단타(분봉) + 스윙(1H) 병렬 후보 선정.
"""

from __future__ import annotations

import logging
from typing import Any

from config import REALTIME_SCAN_BATCH_SIZE
from market_ai import get_active_allocation
from brain import rank_universe_for_scan
from stock_intraday import select_scalping_stocks
from stock_swing import select_swing_stocks

logger = logging.getLogger(__name__)


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
    단타 우선 → 스윙 1H 후보 병합 (중복 제거).
    batch_offset: 유니버스 회전 스캔 커서 (스윙 API 부하 분산).
    """
    exclude = set(exclude_codes or ())
    batch_size = batch_size or REALTIME_SCAN_BATCH_SIZE
    universe = rank_universe_for_scan(universe)
    n = len(universe)
    meta: dict[str, Any] = {
        "universe_size": n,
        "scalp_candidates": 0,
        "swing_candidates": 0,
        "batch_offset": batch_offset,
    }
    if max_count <= 0 or n == 0:
        return [], meta

    alloc = get_active_allocation()
    scalp_w = float(alloc.get("scalp_weight", 0.35))
    swing_w = float(alloc.get("swing_weight", 0.45))
    meta["allocation"] = alloc

    scalp_max = max(0, min(max_count, round(max_count * scalp_w)))
    if scalp_max <= 0 and max_count > 0 and scalp_w >= swing_w:
        scalp_max = 1
    swing_max = max(0, max_count - scalp_max)
    if swing_max <= 0 and max_count > scalp_max:
        swing_max = max(1, max_count - scalp_max)

    scalp_picks: list[dict] = []
    if scalp_max > 0:
        scalp_picks = select_scalping_stocks(
            access_token,
            app_key,
            app_secret,
            universe,
            exclude_codes=exclude,
            max_count=scalp_max,
        )
    meta["scalp_candidates"] = len(scalp_picks)
    meta["scalp_slot_budget"] = scalp_max
    meta["swing_slot_budget"] = swing_max

    merged: list[dict] = []
    seen: set[str] = set()
    for p in scalp_picks:
        if p["code"] not in seen:
            seen.add(p["code"])
            merged.append(p)

    remaining = min(swing_max, max_count - len(merged))
    if remaining > 0:
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
            max_count=remaining,
        )
        meta["swing_candidates"] = len(swing_picks)
        for p in swing_picks:
            if p["code"] not in seen:
                seen.add(p["code"])
                merged.append(p)
    else:
        meta["swing_candidates"] = 0

    return merged[:max_count], meta
