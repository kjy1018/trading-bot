"""
단타 트랙 — 1분/3분봉 실시간 수급·체결 강도 기반 진입.
스윙 1H 정배열·눌림목(_passes_quality_and_setup)과 분리된 병렬 파이프라인.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from config import (
    SCALP_MAX_CHASE_CHANGE_PCT,
    SCALP_MIN_CHANGE_PCT,
    SCALP_MIN_VOL_SURGE_RATIO,
    SCALP_SCAN_TOP_N,
    SCALP_TOP_WICK_REJECT_PCT,
)
from brain import enrich_stock_with_brain
from brain_classifier import ClassificationContext, TradingMode, get_brain_classifier
from stock_ranking import is_common_stock_for_trade
from stock_swing import fetch_intraday_minute_bars
from trading_logic import apply_expected_exit_to_position

logger = logging.getLogger(__name__)


def _minute_slot_key(row: dict) -> tuple[str, int]:
    """분 단위 슬롯 (hhmm 필드 우선)."""
    date_s = str(row.get("date") or "")
    hhmm = str(row.get("hhmm") or "").zfill(4)
    if len(hhmm) >= 4:
        hh, mm = int(hhmm[:2]), int(hhmm[2:4])
    else:
        hh = int(str(row.get("hour") or "09")[:2])
        mm = 0
    return date_s, hh * 60 + mm


def resample_minutes(minute_rows: list[dict], n: int) -> list[dict]:
    """1분봉 → N분봉 OHLCV."""
    if n <= 1:
        return list(minute_rows)
    buckets: dict[tuple[str, int], dict] = {}
    for row in minute_rows:
        date_s, slot_min = _minute_slot_key(row)
        bucket_min = (slot_min // n) * n
        key = (date_s, bucket_min)
        b = buckets.get(key)
        if b is None:
            bh, bm = divmod(bucket_min, 60)
            buckets[key] = {
                "date": date_s,
                "hour": f"{bh:02d}",
                "hhmm": f"{bh:02d}{bm:02d}",
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": int(row.get("volume") or 0),
            }
        else:
            b["high"] = max(b["high"], row["high"])
            b["low"] = min(b["low"], row["low"])
            b["close"] = row["close"]
            b["volume"] = int(b.get("volume", 0)) + int(row.get("volume") or 0)
    ordered = sorted(buckets.items(), key=lambda x: (x[0][0], x[0][1]))
    return [v for _, v in ordered]


def _intraday_session_minutes(minute_rows: list[dict]) -> list[dict]:
    """장중 09:00~15:30 구간만."""
    out: list[dict] = []
    for row in minute_rows:
        _, slot = _minute_slot_key(row)
        bh, bm = divmod(slot, 60)
        if bh < 9 or (bh == 15 and bm > 30) or bh > 15:
            continue
        out.append(row)
    out.sort(key=lambda r: _minute_slot_key(r))
    return out


def _rising_closes(bars: list[dict], count: int = 3) -> bool:
    if len(bars) < count:
        return False
    closes = [int(b["close"]) for b in bars[-count:]]
    return all(closes[i] > closes[i - 1] for i in range(1, len(closes)))


def _volume_surge(bars: list[dict], ratio_min: float) -> bool:
    if len(bars) < 6:
        return False
    vols = [int(b.get("volume") or 0) for b in bars[-6:]]
    if max(vols) <= 0:
        return False
    last_v = vols[-1]
    prev_avg = sum(vols[:-1]) / max(len(vols) - 1, 1)
    return prev_avg > 0 and last_v / prev_avg >= ratio_min


def _bid_pressure_proxy(bars: list[dict]) -> float:
    """양봉 체결 비중 근사 (호가창 강도 대용)."""
    if len(bars) < 5:
        return 0.0
    window = bars[-8:]
    bull = sum(1 for b in window if int(b["close"]) >= int(b["open"]))
    return bull / len(window)


def analyze_scalping_setup(
    stock: dict[str, Any],
    minute_bars: list[dict],
) -> tuple[float, dict[str, Any]] | None:
    """
    1m/3m 수급·체결 강도 — 단타 진입 후보.
    Returns (score, enriched) or None.
    """
    session = _intraday_session_minutes(minute_bars)
    if len(session) < 12:
        return None

    bars_3m = resample_minutes(session, 3)
    if len(bars_3m) < 4:
        return None

    price = int(bars_3m[-1]["close"])
    if price <= 0:
        return None
    day_high = max(int(b.get("high") or b.get("close") or 0) for b in session)
    if day_high > 0:
        drop_from_high = (price - day_high) / day_high * 100.0
        if drop_from_high <= -abs(float(SCALP_TOP_WICK_REJECT_PCT)):
            return None

    change = float(stock.get("change_rate") or 0)
    if change < SCALP_MIN_CHANGE_PCT or change > SCALP_MAX_CHASE_CHANGE_PCT:
        return None

    if not _rising_closes(bars_3m, 3):
        if not _rising_closes(session, 3):
            return None

    if not _volume_surge(session, SCALP_MIN_VOL_SURGE_RATIO):
        return None

    pressure = _bid_pressure_proxy(session)
    if pressure < 0.45:
        return None

    score = (
        change * 4.0
        + pressure * 30.0
        + min(40.0, float(stock.get("prdy_vrss_vol_rate") or 0) / 25.0)
    )

    enriched = {
        **stock,
        "price": price,
        "scalp_score": round(score, 2),
        "setup": "1m/3m 수급·체결강도",
        "swing_setup_passed": False,
        "intraday_pressure": round(pressure, 2),
        "entry_basis": "intraday_momentum",
    }
    return score, enriched


def select_scalping_stocks(
    access_token: str,
    app_key: str,
    app_secret: str,
    universe: list[dict],
    exclude_codes: set[str] | None = None,
    max_count: int = 1,
) -> list[dict]:
    """등락·거래량 상위 종목만 분봉 심층 스캔."""
    exclude = exclude_codes or set()
    ranked = sorted(
        universe,
        key=lambda s: (
            float(s.get("brain_score") or 0),
            float(s.get("flow_score") or 0),
            float(s.get("leader_boost") or 0),
            float(s.get("change_rate") or 0),
        ),
        reverse=True,
    )
    candidates: list[tuple[float, dict]] = []
    brain = get_brain_classifier()
    checked = 0

    for stock in ranked:
        if stock["code"] in exclude:
            continue
        if not is_common_stock_for_trade(stock):
            continue
        if checked >= SCALP_SCAN_TOP_N:
            break
        checked += 1
        try:
            minutes = fetch_intraday_minute_bars(
                access_token, app_key, app_secret, stock["code"], trading_days=1
            )
            result = analyze_scalping_setup(stock, minutes)
            if not result:
                continue
            score, enriched = result
            bars_3m = resample_minutes(_intraday_session_minutes(minutes), 3)
            ctx = ClassificationContext(
                stock=enriched,
                hourly_bars=bars_3m,
                swing_setup_passed=False,
                extra={"minute_bars": minutes, "bars_3m": bars_3m},
            )
            decision = brain.classify(ctx)
            scalp_score = float(decision.scores.get("scalping", 0))
            if decision.mode != TradingMode.SCALPING and scalp_score < 45.0:
                continue
            tagged = enrich_stock_with_brain(
                brain.tag_stock(enriched, decision),
                auto_mode=True,
            )
            apply_expected_exit_to_position(
                tagged, hourly_bars=bars_3m, force_recalc=True
            )
            candidates.append((score, tagged))
        except Exception as exc:
            logger.warning("단타 분봉 분석 실패 %s: %s", stock["code"], exc)

    candidates.sort(key=lambda x: x[0], reverse=True)
    picks: list[dict] = []
    seen: set[str] = set()
    for _, s in candidates:
        if s["code"] in seen:
            continue
        seen.add(s["code"])
        picks.append(s)
        if len(picks) >= max_count:
            break
    return picks
