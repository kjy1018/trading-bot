"""
일봉 조회 (inquire-daily-itemchartprice) — 스윙/장투 MA·지표용.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import requests

from config import BASE_URL
from kis_headers import build_kis_headers
from kis_rate import kis_request

logger = logging.getLogger(__name__)

DAILY_CHART_PATH = (
    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
)
TR_ID_DAILY_CHART = "FHKST03010100"


def _safe_int(val: object) -> int:
    try:
        return int(float(str(val).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def fetch_daily_ohlc_bars(
    access_token: str,
    app_key: str,
    app_secret: str,
    code: str,
    *,
    lookback_days: int = 90,
) -> list[dict[str, Any]]:
    """최근 일봉 OHLCV (과거→현재)."""
    cal_days = max(int(lookback_days) * 2 + 10, 30)
    start = (datetime.now() - timedelta(days=cal_days)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    url = f"{BASE_URL}{DAILY_CHART_PATH}"
    headers = build_kis_headers(
        access_token=access_token,
        app_key=app_key,
        app_secret=app_secret,
        tr_id=TR_ID_DAILY_CHART,
    )
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code.strip()[-6:],
        "FID_INPUT_DATE_1": start,
        "FID_INPUT_DATE_2": end,
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    try:
        with kis_request():
            response = requests.get(url, headers=headers, params=params, timeout=25)
        if response.status_code != 200:
            return []
        data = response.json()
        if data.get("rt_cd") != "0":
            return []
        rows = data.get("output2") or data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            close = _safe_int(row.get("stck_clpr") or row.get("stck_prpr"))
            if close <= 0:
                continue
            d = str(row.get("stck_bsop_date") or row.get("date") or "")[:8]
            out.append(
                {
                    "date": d,
                    "open": _safe_int(row.get("stck_oprc") or close),
                    "high": _safe_int(row.get("stck_hgpr") or close),
                    "low": _safe_int(row.get("stck_lwpr") or close),
                    "close": close,
                    "volume": _safe_int(row.get("acml_vol") or row.get("cntg_vol")),
                }
            )
        out.sort(key=lambda b: str(b.get("date") or ""))
        if lookback_days > 0 and len(out) > lookback_days:
            out = out[-lookback_days:]
        return out
    except requests.RequestException as exc:
        logger.debug("일봉 조회 실패 %s: %s", code, exc)
        return []


def compute_ma_bundle(closes: list[int]) -> dict[str, float | None]:
    """MA5/20/60 및 정배열 여부."""
    clean = [int(c) for c in closes if int(c) > 0]
    n = len(clean)

    def _ma(period: int) -> float | None:
        if n < period:
            return None
        return sum(clean[-period:]) / float(period)

    ma5 = _ma(5)
    ma20 = _ma(20)
    ma60 = _ma(60)
    aligned = (
        ma5 is not None
        and ma20 is not None
        and ma60 is not None
        and ma5 > ma20 > ma60
    )
    return {
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "ma_aligned": aligned,
        "last_close": float(clean[-1]) if clean else None,
    }


def daily_bars_as_hourly_proxy(daily_bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """스윙 매집·MA 로직 호환용 — 일봉을 봉 단위로 전달."""
    return [
        {
            "open": int(b.get("open") or b.get("close") or 0),
            "high": int(b.get("high") or b.get("close") or 0),
            "low": int(b.get("low") or b.get("close") or 0),
            "close": int(b.get("close") or 0),
            "volume": int(b.get("volume") or 0),
        }
        for b in daily_bars
        if int(b.get("close") or 0) > 0
    ]
