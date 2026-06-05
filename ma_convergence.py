"""
이격도(현재가/MA×100) 수렴 스캔 — 정배열 무관, 평균선(100) 근접 우선 매수.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _cfg(name: str, default: Any) -> Any:
    try:
        import config as cfg

        return getattr(cfg, name, default)
    except ImportError:
        return default


def disparity_index(price: float, ma: float) -> float:
    """이격도 — 100이면 MA와 동일."""
    if ma <= 0 or price <= 0:
        return 999.0
    return price / ma * 100.0


def convergence_distance(disparity: float, *, target: float | None = None) -> float:
    tgt = float(target if target is not None else _cfg("MA_DISPARITY_TARGET", 100.0))
    return abs(float(disparity) - tgt)


def _ma(values: list[int | float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def score_convergence_from_bars(
    stock: dict[str, Any],
    bars: list[dict],
    *,
    ma_period: int | None = None,
) -> tuple[float, dict[str, Any]] | None:
    """60분/일봉 — MA 근접 점수 (정배열 불필요)."""
    if len(bars) < 10:
        return None
    period = int(ma_period or _cfg("MA_CONVERGENCE_MA_PERIOD", 20))
    closes = [int(b["close"]) for b in bars if int(b.get("close") or 0) > 0]
    if len(closes) < period:
        return None
    ma = _ma(closes, period)
    if ma is None or ma <= 0:
        return None
    price = int(closes[-1])
    disp = disparity_index(float(price), float(ma))
    dist = convergence_distance(disp)
    band = float(_cfg("MA_CONVERGENCE_BAND_PCT", 3.0))
    max_dist = float(_cfg("MA_CONVERGENCE_MAX_DIST_PCT", 8.0))
    if dist > max_dist:
        return None
    # 100에 가까울수록 고점 — band 이내면 대폭 가산
    score = max(0.0, 100.0 - dist * 12.0)
    if dist <= band:
        score += 40.0
    prev_disp = None
    if len(closes) >= period + 3:
        prev_ma = _ma(closes[:-3], period)
        if prev_ma and prev_ma > 0:
            prev_disp = disparity_index(float(closes[-4]), float(prev_ma))
    converging = prev_disp is not None and abs(prev_disp - 100.0) > dist
    if converging:
        score += 15.0

    enriched = {
        **stock,
        "price": price,
        "disparity_index": round(disp, 2),
        "convergence_dist": round(dist, 2),
        "ma20": int(ma),
        "entry_score": round(score, 2),
        "setup": f"이격도수렴({disp:.1f}→100)",
        "entry_basis": "ma_convergence",
        "entry_track": "convergence",
        "swing_setup_passed": False,
        "convergence_converging": converging,
    }
    return score, enriched


def is_disparity_breakout(
    stock: dict[str, Any],
    bars: list[dict],
) -> tuple[bool, float]:
    """오전 단타 — 이격도 100 상향 돌파 + 변동성."""
    if len(bars) < 12:
        return False, 0.0
    period = int(_cfg("MA_CONVERGENCE_MA_PERIOD", 20))
    closes = [int(b["close"]) for b in bars]
    ma = _ma(closes, period)
    if ma is None:
        return False, 0.0
    prev_price = float(closes[-4])
    cur_price = float(closes[-1])
    prev_disp = disparity_index(prev_price, float(ma))
    cur_disp = disparity_index(cur_price, float(ma))
    chg = float(stock.get("change_rate") or 0)
    vol_min = float(_cfg("MORNING_SCALP_MIN_CHANGE_PCT", 2.0))
    crossing = prev_disp < 100.0 <= cur_disp or (prev_disp < 99.5 and cur_disp >= 99.5)
    volatile = chg >= vol_min
    score = (cur_disp - prev_disp) * 5.0 + chg * 2.0
    return bool(crossing and volatile), score


def select_convergence_candidates(
    access_token: str,
    app_key: str,
    app_secret: str,
    universe: list[dict],
    *,
    exclude_codes: set[str] | None = None,
    max_count: int = 5,
    use_daily: bool = False,
) -> list[dict]:
    """이격도 100 근접 종목 — 정배열 무관."""
    from stock_ranking import is_common_stock_for_trade

    exclude = exclude_codes or set()
    candidates: list[tuple[float, dict]] = []

    for stock in universe:
        code = str(stock.get("code") or "").strip()
        if len(code) != 6 or code in exclude:
            continue
        if not is_common_stock_for_trade(stock):
            continue
        try:
            if use_daily:
                from stock_daily import fetch_daily_ohlc_bars

                bars = fetch_daily_ohlc_bars(
                    access_token, app_key, app_secret, code, lookback_days=60
                )
            else:
                from stock_swing import _fetch_hourly_bars

                bars = _fetch_hourly_bars(access_token, app_key, app_secret, code)
            result = score_convergence_from_bars(stock, bars or [])
            if result:
                score, enriched = result
                candidates.append((score, enriched))
        except Exception as exc:
            logger.debug("이격도 스캔 실패 %s: %s", code, exc)

    candidates.sort(key=lambda x: x[0], reverse=True)
    picks: list[dict] = []
    seen: set[str] = set()
    for _, row in candidates:
        c = row.get("code")
        if c in seen:
            continue
        seen.add(c)
        picks.append(row)
        if len(picks) >= max_count:
            break
    if picks:
        logger.info(
            "이격도 수렴 후보 %d건 (1위 %s 이격도=%s)",
            len(picks),
            picks[0].get("name"),
            picks[0].get("disparity_index"),
        )
    return picks


def select_morning_disparity_breakouts(
    access_token: str,
    app_key: str,
    app_secret: str,
    universe: list[dict],
    *,
    exclude_codes: set[str] | None = None,
    max_count: int = 3,
) -> list[dict]:
    """09~10시 — 이격도 100 돌파 + 변동성 단타 후보."""
    from stock_intraday import fetch_intraday_minute_bars
    from stock_ranking import is_common_stock_for_trade

    exclude = exclude_codes or set()
    ranked = sorted(
        universe,
        key=lambda s: abs(float(s.get("change_rate") or 0)),
        reverse=True,
    )
    candidates: list[tuple[float, dict]] = []
    checked = 0
    for stock in ranked:
        code = str(stock.get("code") or "").strip()
        if len(code) != 6 or code in exclude:
            continue
        if not is_common_stock_for_trade(stock):
            continue
        if checked >= int(_cfg("MORNING_SCALP_SCAN_TOP_N", 25)):
            break
        checked += 1
        try:
            minutes = fetch_intraday_minute_bars(
                access_token, app_key, app_secret, code, trading_days=1
            )
            from stock_intraday import resample_minutes, _intraday_session_minutes

            session = _intraday_session_minutes(minutes)
            bars_3m = resample_minutes(session, 3)
            ok, br_score = is_disparity_breakout(stock, bars_3m if len(bars_3m) >= 8 else session)
            if not ok:
                continue
            price = int(session[-1]["close"]) if session else int(stock.get("price") or 0)
            row = {
                **stock,
                "price": price,
                "entry_score": round(60.0 + br_score, 2),
                "scalp_score": round(60.0 + br_score, 2),
                "setup": "오전 이격도 돌파",
                "entry_basis": "morning_disparity_breakout",
                "entry_track": "morning_scalp",
                "trading_mode": "day_trading",
                "mode_label": "단타",
            }
            candidates.append((row["entry_score"], row))
        except Exception as exc:
            logger.debug("오전 돌파 스캔 %s: %s", code, exc)

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in candidates[:max_count]]
