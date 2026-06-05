"""
매수 시점 차트 패턴 → 고정 길이 특징 벡터.
스윙(60분봉) 기준; 일봉 보조 특징 포함.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

FEATURE_NAMES: list[str] = [
    "hour_of_day",
    "golden_alignment",
    "ma5_ma20_spread_pct",
    "ma20_ma60_spread_pct",
    "ma20_slope_pct",
    "pullback_from_5bar_high_pct",
    "volume_dry_ratio",
    "last_bar_body_pct",
    "rsi_14",
    "atr_pct",
    "change_rate",
    "brain_rank",
    "brain_score",
    "entry_price_log",
]

_MODE_ENCODING = {"day_trading": 0, "swing": 1, "long_term": 2}


def _ma(values: list[float | int], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(closes: list[int], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses <= 0:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def _atr_pct(bars: list[dict], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(-period, 0):
        h = float(bars[i].get("high") or bars[i]["close"])
        l = float(bars[i].get("low") or bars[i]["close"])
        prev_c = float(bars[i - 1]["close"])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    atr = sum(trs) / len(trs) if trs else 0.0
    last = float(bars[-1]["close"] or 1)
    return (atr / last * 100.0) if last > 0 else 0.0


def _golden_alignment(closes: list[int]) -> bool:
    if len(closes) < 60:
        return False
    ma5 = _ma(closes, 5)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    if ma5 is None or ma20 is None or ma60 is None:
        return False
    return ma5 > ma20 > ma60


def _volume_dry_ratio(bars: list[dict]) -> float:
    if len(bars) < 5:
        return 1.0
    vols = [int(b.get("volume") or 0) for b in bars]
    peak = max(vols[-8:-2]) if len(vols) >= 8 else max(vols[:-2])
    if peak <= 0:
        return 1.0
    return float(vols[-2]) / float(peak)


def extract_chart_features(
    hourly_bars: list[dict] | None,
    *,
    pick: dict[str, Any] | None = None,
    entry_dt: datetime | None = None,
    entry_price: int | None = None,
) -> dict[str, float]:
    """매수 직전 차트 패턴 특징."""
    pick = pick or {}
    entry_dt = entry_dt or datetime.now()
    bars = list(hourly_bars or [])
    closes = [int(b["close"]) for b in bars if int(b.get("close") or 0) > 0]

    ma5 = _ma(closes, 5)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    last_close = float(closes[-1]) if closes else float(entry_price or pick.get("price") or 0)

    ma5_ma20 = 0.0
    if ma5 and ma20 and ma20 > 0:
        ma5_ma20 = (ma5 - ma20) / ma20 * 100.0
    ma20_ma60 = 0.0
    if ma20 and ma60 and ma60 > 0:
        ma20_ma60 = (ma20 - ma60) / ma60 * 100.0

    ma20_slope = 0.0
    if len(closes) >= 25 and ma20:
        ma20_prev = _ma(closes[:-5], 20)
        if ma20_prev and ma20_prev > 0:
            ma20_slope = (ma20 - ma20_prev) / ma20_prev * 100.0

    pullback = 0.0
    if len(closes) >= 5:
        hi = max(closes[-5:])
        if hi > 0:
            pullback = (last_close - hi) / hi * 100.0

    body_pct = 0.0
    if bars:
        b = bars[-1]
        o = float(b.get("open") or b["close"])
        c = float(b["close"])
        if o > 0:
            body_pct = (c - o) / o * 100.0

    price = int(entry_price or pick.get("price") or last_close or 0)
    chg = float(pick.get("change_rate") or 0.0)

    return {
        "hour_of_day": float(entry_dt.hour + entry_dt.minute / 60.0),
        "golden_alignment": 1.0 if _golden_alignment(closes) else 0.0,
        "ma5_ma20_spread_pct": round(ma5_ma20, 4),
        "ma20_ma60_spread_pct": round(ma20_ma60, 4),
        "ma20_slope_pct": round(ma20_slope, 4),
        "pullback_from_5bar_high_pct": round(pullback, 4),
        "volume_dry_ratio": round(_volume_dry_ratio(bars), 4),
        "last_bar_body_pct": round(body_pct, 4),
        "rsi_14": round(_rsi(closes), 4),
        "atr_pct": round(_atr_pct(bars), 4),
        "change_rate": round(chg, 4),
        "brain_rank": float(pick.get("brain_rank") or pick.get("rank") or 0),
        "brain_score": float(pick.get("brain_score") or pick.get("score") or 0),
        "entry_price_log": round(math.log10(max(price, 1)), 4),
    }


def features_to_vector(features: dict[str, float]) -> list[float]:
    return [float(features.get(name, 0.0) or 0.0) for name in FEATURE_NAMES]


def _trading_dates_around(anchor: datetime, count: int) -> list[str]:
    dates: list[str] = []
    day = anchor
    while len(dates) < count:
        if day.weekday() < 5:
            dates.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
    dates.reverse()
    return dates


def _bars_before_entry(hourly: list[dict], entry_dt: datetime) -> list[dict]:
    entry_date = entry_dt.strftime("%Y%m%d")
    entry_hour = f"{entry_dt.hour:02d}"
    out: list[dict] = []
    for bar in hourly:
        d = str(bar.get("date") or "")
        h = str(bar.get("hour") or "00")
        if d < entry_date:
            out.append(bar)
        elif d == entry_date and h < entry_hour:
            out.append(bar)
    return out


def fetch_hourly_bars_at_entry(
    access_token: str,
    app_key: str,
    app_secret: str,
    code: str,
    entry_dt: datetime,
    *,
    trading_days: int = 12,
) -> list[dict]:
    """매수 시점 이전까지의 60분봉 (분봉→집계)."""
    from stock_swing import _fetch_day_minute_bars, _resample_minutes_to_hourly

    all_minutes: list[dict] = []
    dates = _trading_dates_around(entry_dt, max(5, trading_days))
    for idx, date_str in enumerate(dates):
        if idx > 0:
            from kis_rate import kis_loop_pause

            kis_loop_pause()
        all_minutes.extend(
            _fetch_day_minute_bars(access_token, app_key, app_secret, code, date_str)
        )
    hourly = _resample_minutes_to_hourly(all_minutes)
    return _bars_before_entry(hourly, entry_dt)


def fetch_hourly_bars_for_pick(
    access_token: str,
    app_key: str,
    app_secret: str,
    pick: dict[str, Any],
) -> list[dict]:
    """실시간 매수 직전 — 현재 시점 60분봉."""
    from stock_swing import _fetch_hourly_bars

    code = str(pick.get("code") or "").strip()[-6:]
    if len(code) != 6:
        return []
    return _fetch_hourly_bars(access_token, app_key, app_secret, code)
