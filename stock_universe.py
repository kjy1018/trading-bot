"""
국내 활성주(코스피·코스닥) 유니버스 — 수급·리스크 1차 필터.
60분봉 정배열·눌림목 진입 조건(stock_swing._passes_quality_and_setup)은 변경하지 않음.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import requests

from config import (
    BASE_URL,
    KIS_API_MIN_INTERVAL_SEC,
    SWING_LIQUIDITY_MAX_CHECKS,
    SWING_LIQUIDITY_LOOKBACK_DAYS,
    SWING_MIN_AVG_TRADE_VALUE_5D,
    SWING_MIN_STOCK_PRICE,
    SWING_RANK_EXCLUDE_CLS,
    SWING_UNIVERSE_MAX_RANK_PAGES,
)
from kis_headers import build_kis_headers
from kis_rate import kis_loop_pause, kis_request
from stock_ranking import is_common_stock_for_trade

logger = logging.getLogger(__name__)

VOLUME_RANK_PATH = "/uapi/domestic-stock/v1/quotations/volume-rank"
TR_ID_VOLUME_RANK = "FHPST01710000"

DAILY_CHART_PATH = (
    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
)
TR_ID_DAILY_CHART = "FHKST03010100"

PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
TR_ID_PRICE = "FHKST01010100"

# iscd_stat_cls_code — 관리·환기·정지 등 (정상 00/51 제외)
_RISK_ISCD_STAT = frozenset({"02", "03", "04", "05", "06", "07", "08", "09"})
# mrkt_warn_cls_code — 01 투자주의, 02 투자경고, 03 투자위험
_RISK_MRKT_WARN = frozenset({"01", "02", "03"})


def is_excluded_risk_stock(detail: dict) -> bool:
    """관리·환기·투자경고/위험·동전주·거래정지 등 리스크 종목."""
    price = int(detail.get("price") or 0)
    if price < SWING_MIN_STOCK_PRICE:
        return True

    name = str(detail.get("name") or "")
    if "관리" in name or "환기" in name:
        return True

    if str(detail.get("temp_stop_yn") or "").upper() == "Y":
        return True
    if str(detail.get("sltr_yn") or "").upper() == "Y":
        return True
    if str(detail.get("invt_caful_yn") or "").upper() == "Y":
        return True

    mang = str(detail.get("mang_issu_cls_code") or "").strip().upper()
    if mang in ("Y", "1", "01", "02"):
        return True

    warn = str(detail.get("mrkt_warn_cls_code") or "").strip()
    if warn in _RISK_MRKT_WARN:
        return True

    stat = str(detail.get("iscd_stat_cls_code") or "").strip()
    if stat in _RISK_ISCD_STAT:
        return True

    return False


def _parse_rank_row(row: dict) -> dict | None:
    raw = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "")
    code = raw[-6:] if len(raw) >= 6 else raw
    if len(code) != 6 or not code.isdigit():
        return None
    name = (row.get("hts_kor_isnm") or row.get("kor_isnm") or "").strip()
    price = int(row.get("stck_prpr") or 0)
    if price < SWING_MIN_STOCK_PRICE:
        return None
    trade_amount = int(row.get("acml_tr_pbmn") or row.get("tr_pbmn") or 0)
    change = float(row.get("prdy_ctrt") or 0)
    return {
        "code": code,
        "name": name,
        "price": price,
        "change_rate": change,
        "trade_amount": trade_amount,
        "raw_code": f"A{code}",
    }


def fetch_trading_amount_rank_pool(
    access_token: str,
    app_key: str,
    app_secret: str,
    *,
    max_pages: int = SWING_UNIVERSE_MAX_RANK_PAGES,
) -> list[dict]:
    """
    거래대금순위 API 연속조회 — 코스피·코스닥 전체 활성주(보통주) 풀.
    API 단계에서 관리·환기·투자경고/위험·ETF 등 제외 + 최소가 1,000원.
    """
    url = f"{BASE_URL}{VOLUME_RANK_PATH}"
    tr_cont_req = ""
    pool: list[dict] = []
    seen: set[str] = set()

    for page in range(max_pages):
        if page > 0:
            kis_loop_pause()
        headers = build_kis_headers(
            access_token=access_token,
            app_key=app_key,
            app_secret=app_secret,
            tr_id=TR_ID_VOLUME_RANK,
            tr_cont=tr_cont_req,
        )
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "1",
            "FID_BLNG_CLS_CODE": "3",
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": SWING_RANK_EXCLUDE_CLS,
            "FID_INPUT_PRICE_1": str(SWING_MIN_STOCK_PRICE),
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        }
        try:
            with kis_request():
                response = requests.get(
                    url, headers=headers, params=params, timeout=30
                )
        except requests.RequestException as exc:
            logger.warning("거래대금순위 조회 실패(page %d): %s", page, exc)
            break

        if response.status_code != 200:
            logger.warning(
                "거래대금순위 HTTP %s: %s",
                response.status_code,
                response.text[:200],
            )
            break

        data = response.json()
        if data.get("rt_cd") != "0":
            logger.warning(
                "거래대금순위 API 오류: %s", data.get("msg1", data)
            )
            break

        rows = data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            break

        for row in rows:
            item = _parse_rank_row(row)
            if not item or item["code"] in seen:
                continue
            if not is_common_stock_for_trade(item):
                continue
            seen.add(item["code"])
            pool.append(item)

        tr_cont_resp = (response.headers.get("tr_cont") or "D").upper()
        if tr_cont_resp not in ("M", "F"):
            break
        tr_cont_req = "N"

    logger.info("거래대금순위 풀 %d종목 (최대 %d페이지)", len(pool), max_pages)
    return pool


def fetch_avg_trade_value(
    access_token: str,
    app_key: str,
    app_secret: str,
    code: str,
    *,
    days: int = SWING_LIQUIDITY_LOOKBACK_DAYS,
) -> float | None:
    """최근 N거래일 일별 거래대금 평균(원)."""
    cal_days = max(days * 2 + 5, 12)
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
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start,
        "FID_INPUT_DATE_2": end,
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    try:
        with kis_request():
            response = requests.get(url, headers=headers, params=params, timeout=20)
        if response.status_code != 200:
            return None
        data = response.json()
        if data.get("rt_cd") != "0":
            return None
        rows = data.get("output2") or data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        amounts: list[int] = []
        for row in rows:
            amt = int(row.get("acml_tr_pbmn") or 0)
            if amt > 0:
                amounts.append(amt)
        if len(amounts) < days:
            return None
        recent = amounts[-days:]
        return sum(recent) / len(recent)
    except requests.RequestException:
        return None


def fetch_quote_with_risk(
    access_token: str,
    app_key: str,
    app_secret: str,
    code: str,
) -> dict | None:
    """현재가 + 리스크·시총 필드."""
    url = f"{BASE_URL}{PRICE_PATH}"
    headers = build_kis_headers(
        access_token=access_token,
        app_key=app_key,
        app_secret=app_secret,
        tr_id=TR_ID_PRICE,
    )
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    try:
        with kis_request():
            response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code != 200:
            return None
        data = response.json()
        if data.get("rt_cd") != "0":
            return None
        out = data.get("output") or {}
        price = int(out.get("stck_prpr") or 0)
        if price <= 0:
            return None
        shares = int(float(out.get("lstn_stcn") or 0))
        cap_won = price * shares if shares > 0 else 0
        hts_avls = out.get("hts_avls")
        if hts_avls:
            try:
                cap_won = max(cap_won, int(float(hts_avls)) * 100_000_000)
            except (TypeError, ValueError):
                pass
        return {
            "code": code,
            "name": (out.get("hts_kor_isnm") or code).strip(),
            "price": price,
            "change_rate": float(out.get("prdy_ctrt") or 0),
            "volume": int(out.get("acml_vol") or 0),
            "market_cap": cap_won,
            "raw_code": f"A{code}",
            "iscd_stat_cls_code": out.get("iscd_stat_cls_code"),
            "mrkt_warn_cls_code": out.get("mrkt_warn_cls_code"),
            "mang_issu_cls_code": out.get("mang_issu_cls_code"),
            "invt_caful_yn": out.get("invt_caful_yn"),
            "temp_stop_yn": out.get("temp_stop_yn"),
            "sltr_yn": out.get("sltr_yn"),
            "prdy_vrss_vol_rate": float(out.get("prdy_vrss_vol_rate") or 0),
            "frgn_ntby_qty": int(out.get("frgn_ntby_qty") or 0),
            "pgtr_ntby_qty": int(out.get("pgtr_ntby_qty") or 0),
            "inst_buy_streak": (
                1
                if int(out.get("frgn_ntby_qty") or 0) > 0
                and int(out.get("pgtr_ntby_qty") or 0) > 0
                else (1 if int(out.get("frgn_ntby_qty") or 0) > 0 else 0)
            ),
        }
    except requests.RequestException:
        return None


def build_active_universe(
    access_token: str,
    app_key: str,
    app_secret: str,
) -> list[dict]:
    """
    코스피·코스닥 활성주 전체 스캔 풀.
    - 수급: 최근 5일 평균 거래대금 ≥ 100억
    - 리스크: 관리·환기·투자경고/위험·1,000원 미만 제외
    """
    pool = fetch_trading_amount_rank_pool(
        access_token, app_key, app_secret
    )
    universe: list[dict] = []
    min_avg = SWING_MIN_AVG_TRADE_VALUE_5D
    fail_streak = 0

    for idx, item in enumerate(pool):
        if idx > 0:
            kis_loop_pause()
        if idx >= SWING_LIQUIDITY_MAX_CHECKS:
            logger.info(
                "5일 거래대금 검증 상한 %d종목 도달 — 순위 상위 위주 스캔",
                SWING_LIQUIDITY_MAX_CHECKS,
            )
            break

        code = item["code"]
        avg_tv = fetch_avg_trade_value(
            access_token, app_key, app_secret, code
        )
        if avg_tv is None or avg_tv < min_avg:
            fail_streak += 1
            if fail_streak >= 35 and len(universe) >= 5:
                logger.info(
                    "수급 필터 연속 미달 — 거래대금 하위 종목 스킵 (검증 %d건)",
                    idx + 1,
                )
                break
            continue
        fail_streak = 0

        detail = fetch_quote_with_risk(
            access_token, app_key, app_secret, code
        )
        if not detail or not is_common_stock_for_trade(detail):
            continue
        if is_excluded_risk_stock(detail):
            continue

        detail["avg_trade_value_5d"] = int(avg_tv)
        detail["trade_amount"] = item.get("trade_amount", 0)
        universe.append(detail)

    logger.info(
        "활성주 유니버스 %d종목 (5일 평균 거래대금 ≥ %s억 · "
        "리스크·동전주 제외 · 순위풀 %d)",
        len(universe),
        min_avg // 100_000_000,
        len(pool),
    )
    return universe
