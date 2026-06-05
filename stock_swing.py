"""
스윙 종목 선정 — 활성주 유니버스 + 1시간봉 정배열 + 눌림목.
스윙 진입(_passes_quality_and_setup)은 60분봉 기준 유지. 단타는 stock_intraday.py.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import requests

from config import (
    ATR_MAX_LOSS_PCT,
    ATR_MIN_LOSS_PCT,
    ATR_PERIOD,
    ATR_STOP_MULT,
    BASE_URL,
    KIS_API_MIN_INTERVAL_SEC,
    SWING_HOURLY_TARGET_BARS,
    SWING_HOURLY_TRADING_DAYS_FETCH,
    SWING_MA_LONG,
    SWING_MA_MID,
    SWING_MA_SHORT,
    SWING_MAX_CHASE_CHANGE_PCT,
    SWING_NEAR_MA20_PCT,
    SWING_NEAR_MA5_PCT,
    SWING_PULLBACK_BARS,
    SWING_VOLUME_DRY_RATIO,
)
from kis_headers import build_kis_headers
from kis_rate import kis_loop_pause, kis_request
from risk_atr import atr_stop_price, wilder_atr
from stock_ranking import is_common_stock_for_trade
from brain_classifier import get_brain_classifier
from trading_logic import apply_expected_exit_to_position
from brain import build_brain_universe, enrich_stock_with_brain

logger = logging.getLogger(__name__)

HOURLY_MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
TR_ID_HOURLY_MINUTE = "FHKST03010230"


def _trading_dates_back(count: int) -> list[str]:
    """최근 count 거래일 (YYYYMMDD, 과거→현재)."""
    dates: list[str] = []
    day = datetime.now()
    while len(dates) < count:
        if day.weekday() < 5:
            dates.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
    dates.reverse()
    return dates


def _fetch_day_minute_bars(
    access_token: str,
    app_key: str,
    app_secret: str,
    code: str,
    date_str: str,
) -> list[dict]:
    url = f"{BASE_URL}{HOURLY_MINUTE_PATH}"
    headers = build_kis_headers(
        access_token=access_token,
        app_key=app_key,
        app_secret=app_secret,
        tr_id=TR_ID_HOURLY_MINUTE,
    )
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_HOUR_1": "160000",
        "FID_INPUT_DATE_1": date_str,
        "FID_PW_DATA_INCU_YN": "Y",
        "FID_FAKE_TICK_INCU_YN": "",
    }
    try:
        with kis_request():
            response = requests.get(url, headers=headers, params=params, timeout=25)
        if response.status_code != 200:
            return []
        data = response.json()
        if data.get("rt_cd") != "0":
            return []
        output = data.get("output2") or data.get("output") or []
        if isinstance(output, dict):
            output = [output]
        rows: list[dict] = []
        for row in output:
            hhmm = str(row.get("stck_cntg_hour") or "000000").zfill(6)
            close = int(row.get("stck_prpr") or row.get("stck_clpr") or 0)
            if close <= 0:
                continue
            high = int(row.get("stck_hgpr") or close)
            low = int(row.get("stck_lwpr") or close)
            open_ = int(row.get("stck_oprc") or close)
            high = max(high, close, open_)
            low = min(low, close, open_) if low > 0 else min(close, open_)
            rows.append({
                "date": str(row.get("stck_bsop_date") or date_str),
                "hour": hhmm[:2],
                "hhmm": hhmm[:4],
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": int(row.get("cntg_vol") or row.get("acml_vol") or 0),
            })
        return rows
    except requests.RequestException:
        return []


def _resample_minutes_to_hourly(minute_rows: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str], dict] = {}
    for row in minute_rows:
        hour = row["hour"]
        if hour < "09" or hour > "15":
            continue
        key = (row["date"], hour)
        o = int(row.get("open", row["close"]))
        h = int(row.get("high", row["close"]))
        l = int(row.get("low", row["close"]))
        c = int(row["close"])
        vol = row["volume"]
        if key not in buckets:
            buckets[key] = {
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": vol,
                "date": row["date"],
                "hour": hour,
            }
        else:
            b = buckets[key]
            b["high"] = max(b["high"], h, c)
            b["low"] = min(b["low"], l, c)
            b["close"] = c
            b["volume"] += vol
    ordered = sorted(buckets.keys())
    return [buckets[k] for k in ordered]


def _drop_incomplete_current_hour(bars: list[dict]) -> list[dict]:
    """장중 미완성 1시간봉 제외."""
    if not bars:
        return bars
    now = datetime.now()
    if now.weekday() >= 5:
        return bars
    cur_key = (now.strftime("%Y%m%d"), f"{now.hour:02d}")
    last = bars[-1]
    if (last.get("date"), last.get("hour")) == cur_key:
        return bars[:-1]
    return bars


def fetch_intraday_minute_bars(
    access_token: str,
    app_key: str,
    app_secret: str,
    code: str,
    *,
    trading_days: int = 1,
) -> list[dict]:
    """당일·최근 N거래일 1분봉 (단타 수급 분석용)."""
    all_minutes: list[dict] = []
    dates = _trading_dates_back(max(1, trading_days))[-max(1, trading_days) :]
    for idx, date_str in enumerate(dates):
        if idx > 0:
            kis_loop_pause()
        all_minutes.extend(
            _fetch_day_minute_bars(access_token, app_key, app_secret, code, date_str)
        )
    return all_minutes


def _fetch_hourly_bars(
    access_token: str,
    app_key: str,
    app_secret: str,
    code: str,
) -> list[dict]:
    """최근 2주 내 60~70개 1시간봉 (분봉→60분 집계)."""
    all_minutes: list[dict] = []
    dates = _trading_dates_back(SWING_HOURLY_TRADING_DAYS_FETCH)
    for idx, date_str in enumerate(dates):
        if idx > 0:
            kis_loop_pause()
        all_minutes.extend(
            _fetch_day_minute_bars(access_token, app_key, app_secret, code, date_str)
        )

    hourly = _resample_minutes_to_hourly(all_minutes)
    hourly = _drop_incomplete_current_hour(hourly)
    if len(hourly) > SWING_HOURLY_TARGET_BARS:
        hourly = hourly[-SWING_HOURLY_TARGET_BARS:]
    return hourly


def _ma(values: list[int | float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _swing_require_golden() -> bool:
    try:
        import config as cfg

        return bool(getattr(cfg, "SWING_REQUIRE_GOLDEN_ALIGNMENT", False))
    except ImportError:
        return False


def _swing_ma_proximity_pct() -> float:
    try:
        import config as cfg

        return float(getattr(cfg, "SWING_MA_PROXIMITY_PCT", 5.0))
    except ImportError:
        return 5.0


def _is_above_or_near_ma(closes: list[int], price: int) -> bool:
    """정배열 대신 — 현재가가 MA20/MA5 위 또는 근접(±SWING_MA_PROXIMITY_PCT%)."""
    if len(closes) < SWING_MA_MID + 2:
        return False
    ma5 = _ma(closes, SWING_MA_SHORT)
    ma20 = _ma(closes, SWING_MA_MID)
    if ma20 is None or ma20 <= 0:
        return False
    tol = _swing_ma_proximity_pct() / 100.0
    floor20 = ma20 * (1.0 - tol)
    if price >= floor20:
        return True
    if ma5 and ma5 > 0 and price >= ma5 * (1.0 - tol):
        return True
    return False


def _is_golden_alignment(closes: list[int]) -> bool:
    """1H MA5 > MA20 > MA60 정배열 + MA20 우상향."""
    if len(closes) < SWING_MA_LONG + 5:
        return False
    ma5 = _ma(closes, SWING_MA_SHORT)
    ma20 = _ma(closes, SWING_MA_MID)
    ma60 = _ma(closes, SWING_MA_LONG)
    if ma5 is None or ma20 is None or ma60 is None:
        return False
    if not (ma5 > ma20 > ma60):
        return False
    ma20_prev = _ma(closes[:-5], SWING_MA_MID)
    if ma20_prev is None:
        return True
    return ma20 >= ma20_prev * 0.998


def _hourly_bar_change_pct(closes: list[int], idx: int) -> float:
    if idx <= 0 or closes[idx - 1] <= 0:
        return 0.0
    return (closes[idx] - closes[idx - 1]) / closes[idx - 1] * 100


def _is_pullback_breath(bars: list[dict], live_change: float) -> bool:
    """
    1시간봉 숨고르기:
    - 당일 급등 추격 금지
    - 최근 SWING_PULLBACK_BARS 봉 횡보/소폭 하락
    - 직전 완성봉 거래량 ≤ 전봉 50%
    """
    if live_change > SWING_MAX_CHASE_CHANGE_PCT:
        return False
    if len(bars) < SWING_PULLBACK_BARS + 3:
        return False

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]
    n = SWING_PULLBACK_BARS

    for i in range(-8, -1):
        chg = _hourly_bar_change_pct(closes, i)
        if chg >= 8.0:
            return False

    recent_closes = closes[-(n + 1) : -1]
    if len(recent_closes) < n:
        return False
    start_c, end_c = recent_closes[0], recent_closes[-1]
    if start_c <= 0:
        return False
    drift_pct = (end_c - start_c) / start_c * 100
    drift_min = -4.0
    vol_ratio = SWING_VOLUME_DRY_RATIO
    try:
        import config as cfg

        drift_min = float(getattr(cfg, "SWING_PULLBACK_DRIFT_MIN_PCT", -7.0))
        if getattr(cfg, "SWING_RELAX_PULLBACK_BREATH", True):
            vol_ratio = float(
                getattr(cfg, "SWING_VOLUME_DRY_RATIO_RELAXED", 0.7)
            )
    except ImportError:
        pass
    if drift_pct > 2.0 or drift_pct < drift_min:
        return False

    if len(volumes) < 3:
        return False
    vol_last = volumes[-2]
    vol_prev = volumes[-3]
    if vol_prev <= 0:
        return False
    if vol_last > vol_prev * vol_ratio:
        return False
    avg_vol = sum(volumes[-(n + 3) : -2]) / max(len(volumes[-(n + 3) : -2]), 1)
    if avg_vol > 0 and vol_last > avg_vol * vol_ratio:
        return False
    return True


def _near_ma_support(price: int, ma5: float, ma20: float) -> bool:
    near5 = abs(price - ma5) / ma5 * 100 <= SWING_NEAR_MA5_PCT
    near20 = price >= ma20 * (1 - SWING_NEAR_MA20_PCT / 100) and price <= ma20 * (
        1 + SWING_NEAR_MA20_PCT / 100
    )
    return near5 or near20


def _passes_quality_and_setup(
    stock: dict,
    bars: list[dict],
    *,
    require_golden: bool | None = None,
) -> tuple[float, dict] | None:
    if len(bars) < SWING_MA_LONG + 5:
        return None

    closes = [b["close"] for b in bars]
    hourly_close = closes[-1]
    ma5 = _ma(closes, SWING_MA_SHORT)
    ma20 = _ma(closes, SWING_MA_MID)
    if ma5 is None or ma20 is None:
        return None

    use_golden = _swing_require_golden() if require_golden is None else bool(require_golden)
    if use_golden:
        if not _is_golden_alignment(closes):
            return None
    else:
        try:
            import config as cfg

            if getattr(cfg, "SWING_REQUIRE_MA_PROXIMITY", True):
                if not _is_above_or_near_ma(closes, hourly_close):
                    return None
        except ImportError:
            if not _is_above_or_near_ma(closes, hourly_close):
                return None
    live_chg = float(stock.get("change_rate") or 0.0)
    breath_ok = _is_pullback_breath(bars, live_chg)
    if not breath_ok:
        try:
            import config as cfg

            lo = float(getattr(cfg, "PULLBACK_MAX_PCT", -13.0))
            hi = float(getattr(cfg, "PULLBACK_MIN_PCT", -1.0))
            if getattr(cfg, "SWING_RELAX_PULLBACK_BREATH", True) and lo <= live_chg <= hi:
                breath_ok = True
        except ImportError:
            pass
        if not breath_ok:
            return None
    if not _near_ma_support(hourly_close, ma5, ma20):
        return None

    dist_ma = min(abs(hourly_close - ma5) / ma5, abs(hourly_close - ma20) / ma20) * 100
    align_bonus = (ma5 - ma20) / ma20 * 10 if use_golden else 5.0
    score = 100 - dist_ma + align_bonus

    ohlc = [
        {
            "open": float(b["open"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            "close": float(b["close"]),
        }
        for b in bars
    ]
    atr_val = wilder_atr(ohlc, ATR_PERIOD)
    if atr_val is None:
        return None
    stop_px = atr_stop_price(
        hourly_close,
        atr_val,
        mult=ATR_STOP_MULT,
        min_loss_pct=ATR_MIN_LOSS_PCT,
        max_loss_pct=ATR_MAX_LOSS_PCT,
    )

    enriched = {
        **stock,
        "price": hourly_close,
        "hourly_close": hourly_close,
        "swing_score": round(score, 2),
        "ma5": int(ma5),
        "ma20": int(ma20),
        "setup": (
            "오후 정배열·눌림목"
            if require_golden is True
            else (
                "1H 정배열·눌림목"
                if use_golden
                else "1H 이평근절·눌림목"
            )
        ),
        "swing_setup_passed": True,
        "atr_14": round(atr_val, 4),
        "atr_stop_mult": ATR_STOP_MULT,
        "stop_loss_price": stop_px,
    }
    return score, enriched


def get_swing_universe(
    access_token: str,
    app_key: str,
    app_secret: str,
) -> list[dict]:
    """거래대금·수급 주도주 유니버스 (brain.py FlowTracker)."""
    return build_brain_universe(access_token, app_key, app_secret)


def select_swing_stocks(
    access_token: str,
    app_key: str,
    app_secret: str,
    universe: list[dict],
    exclude_codes: set[str] | None = None,
    max_count: int = 1,
    *,
    afternoon_mode: bool = False,
) -> list[dict]:
    """1시간봉 눌림목 (afternoon_mode=True 시 정배열 필수)."""
    require_golden = None
    if afternoon_mode:
        try:
            import config as cfg

            require_golden = bool(getattr(cfg, "SWING_AFTERNOON_REQUIRE_GOLDEN", True))
        except ImportError:
            require_golden = True
    exclude = exclude_codes or set()
    candidates: list[tuple[float, dict]] = []

    for stock in universe:
        if stock["code"] in exclude:
            continue
        if not is_common_stock_for_trade(stock):
            continue
        try:
            bars = _fetch_hourly_bars(
                access_token, app_key, app_secret, stock["code"]
            )
            result = _passes_quality_and_setup(
                stock, bars, require_golden=require_golden
            )
            if result:
                score, enriched = result
                tagged = enrich_stock_with_brain(
                    get_brain_classifier().tag_pick(enriched, hourly_bars=bars),
                    auto_mode=True,
                )
                apply_expected_exit_to_position(tagged, hourly_bars=bars)
                tagged["entry_score"] = round(float(score), 2)
                tagged["swing_score"] = round(float(score), 2)
                candidates.append((score, tagged))
        except Exception as exc:
            logger.warning("1H 차트 분석 실패 %s: %s", stock["code"], exc)

    candidates.sort(key=lambda x: x[0], reverse=True)
    seen: set[str] = set()
    picks: list[dict] = []
    for _, s in candidates:
        if s["code"] in seen:
            continue
        seen.add(s["code"])
        picks.append(s)
        if len(picks) >= max_count:
            break
    return picks
