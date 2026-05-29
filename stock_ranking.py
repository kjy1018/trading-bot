"""국내주식 거래대금·순위 API."""

import time
from typing import Any

import requests

from config import BASE_URL, TOP_RANK_COUNT
from kis_headers import build_kis_headers
from kis_rate import KISRateLimitError, _response_indicates_rate_limit, kis_request

_rank_cache: list[dict[str, Any]] = []
_rank_cache_at: float = 0.0
_rank_cache_limit: int = 0


def _rank_cache_ttl_sec() -> float:
    try:
        import config as cfg

        return max(300.0, float(getattr(cfg, "TRADE_RANK_CACHE_SEC", 300)))
    except ImportError:
        return 300.0

# 거래량/거래대금 순위 (fid_blng_cls_code=3 → 거래금액순)
VOLUME_RANK_PATH = "/uapi/domestic-stock/v1/quotations/volume-rank"
TR_ID_VOLUME_RANK = "FHPST01710000"

# ETF·ETN·ELW 등 모의투자 매매불가/제외 대상 (종목명 키워드)
_ETF_ETN_NAME_KEYWORDS = (
    "ETF",
    "ETN",
    "ELW",
    "KODEX",
    "TIGER",
    "ARIRANG",
    "KBSTAR",
    "KINDEX",
    "KOSEF",
    "HANARO",
    "SOL",
    "PLUS",
    "RISE",
    "ACE",
    "TIMEFOLIO",
    "마이티",
    "TRUST",
    "레버리지",
    "인버스",
    "INVERSE",
    "LEVERAGE",
    "스팩",
    "SPAC",
)


def _name_is_etf_or_etn(name: str) -> bool:
    upper = name.upper().replace(" ", "")
    return any(kw in upper for kw in _ETF_ETN_NAME_KEYWORDS)


def _raw_code_from_row(row: dict) -> str:
    for key in ("stck_shrn_iscd", "mksc_shrn_iscd", "std_pdno", "pdno"):
        val = str(row.get(key) or "").strip().upper()
        if val:
            return val
    return ""


def _parse_common_stock_codes(raw: str, name: str) -> tuple[str, str] | None:
    """
    일반주만 통과: HTS 기준 A접두(예: A005930) 또는 6자리+명칭으로 일반주 판별.
    Q 등 다른 접두·ETF/ETN 명칭은 제외.
    """
    if not raw:
        return None

    if _name_is_etf_or_etn(name):
        return None

    # A005930 형태 (정상 일반주)
    if raw.startswith("A") and len(raw) >= 7 and raw[1:7].isdigit():
        code = raw[1:7]
        return f"A{code}", code

    # API가 6자리만 반환하는 경우: A접두가 없으면 ETF/ETN 명칭만으로 1차 걸러진 뒤 일반주로 간주
    if len(raw) == 6 and raw.isdigit():
        return f"A{raw}", raw

    # Q(ETN)·기타 접두 상품 → 제외
    return None


def is_common_stock_for_trade(stock: dict) -> bool:
    """모의투자 매수 가능한 A접두 일반주인지 확인."""
    raw = str(stock.get("raw_code") or "").strip().upper()
    code = str(stock.get("code") or "").strip()
    name = stock.get("name") or ""

    if not raw.startswith("A") or len(code) != 6 or not code.isdigit():
        return False
    if raw != f"A{code}":
        return False
    if _name_is_etf_or_etn(name):
        return False
    return True


def _parse_rank_row(row: dict) -> dict | None:
    raw = _raw_code_from_row(row)
    name = (row.get("hts_kor_isnm") or row.get("kor_isnm") or raw or "").strip()
    parsed_codes = _parse_common_stock_codes(raw, name)
    if not parsed_codes:
        return None

    raw_code, code = parsed_codes
    price = int(row.get("stck_prpr") or row.get("stck_hgpr") or 0)
    change_rate = float(row.get("prdy_ctrt") or row.get("prdy_ctrt_rate") or 0)
    trade_amount = int(row.get("acml_tr_pbmn") or row.get("tr_pbmn") or 0)

    return {
        "code": code,
        "raw_code": raw_code,
        "name": name,
        "price": price,
        "change_rate": change_rate,
        "trade_amount": trade_amount,
    }


def get_top_trading_amount_stocks(
    access_token: str,
    app_key: str,
    app_secret: str,
    limit: int = TOP_RANK_COUNT,
    *,
    bypass_cache: bool = False,
) -> list[dict]:
    """
    거래대금 상위 종목 조회 (순위분석 API, 거래금액순).
    공식: /quotations/volume-rank, TR FHPST01710000, FID_BLNG_CLS_CODE=3

    TRADE_RANK_CACHE_SEC(기본 5분) 동안 동일 limit 결과 재사용.
    """
    global _rank_cache, _rank_cache_at, _rank_cache_limit

    now = time.time()
    if (
        not bypass_cache
        and _rank_cache
        and _rank_cache_limit >= limit
        and (now - _rank_cache_at) < _rank_cache_ttl_sec()
    ):
        return [dict(x) for x in _rank_cache[:limit]]

    url = f"{BASE_URL}{VOLUME_RANK_PATH}"
    headers = build_kis_headers(
        access_token=access_token,
        app_key=app_key,
        app_secret=app_secret,
        tr_id=TR_ID_VOLUME_RANK,
    )
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "20171",
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "0",
        "FID_BLNG_CLS_CODE": "3",
        "FID_TRGT_CLS_CODE": "111111111",
        "FID_TRGT_EXLS_CLS_CODE": "0000000000",
        "FID_INPUT_PRICE_1": "",
        "FID_INPUT_PRICE_2": "",
        "FID_VOL_CNT": "",
        "FID_INPUT_DATE_1": "",
    }

    with kis_request():
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(
                f"거래대금 순위 HTTP 오류 [{response.status_code}]: {response.text}"
            )

        data = response.json()
        if _response_indicates_rate_limit(data, response.text):
            raise KISRateLimitError(
                f"거래대금 순위 속도 제한 [{data.get('msg_cd')}]: {data.get('msg1')}"
            )
        if data.get("rt_cd") != "0":
            raise RuntimeError(
                f"거래대금 순위 실패 [{data.get('msg_cd')}]: {data.get('msg1')}"
            )

    output = data.get("output") or []
    if isinstance(output, dict):
        output = [output]

    parsed: list[dict] = []
    for row in output:
        item = _parse_rank_row(row)
        if item and item["price"] > 0:
            parsed.append(item)

    parsed.sort(key=lambda x: x["trade_amount"], reverse=True)
    result = parsed[: max(limit, TOP_RANK_COUNT)]
    _rank_cache = [dict(x) for x in result]
    _rank_cache_at = time.time()
    _rank_cache_limit = len(_rank_cache)
    return [dict(x) for x in _rank_cache[:limit]]


def select_leader_stock(
    ranked: list[dict],
    min_change: float,
    max_change: float,
) -> dict | None:
    """등락률 구간 내 거래대금 1위 종목 (단일)."""
    picks = select_leader_stocks(ranked, min_change, max_change, max_count=1)
    return picks[0] if picks else None


def select_leader_stocks(
    ranked: list[dict],
    min_change: float,
    max_change: float,
    exclude_codes: set[str] | None = None,
    max_count: int = 1,
) -> list[dict]:
    """
    등락률 min_change ~ max_change 구간에서 거래대금 순으로 최대 max_count개 선정.
    exclude_codes에 있는 종목은 제외 (중복 매수 방지).
    """
    exclude = exclude_codes or set()
    candidates = [
        s
        for s in ranked
        if min_change <= s["change_rate"] <= max_change
        and s["trade_amount"] > 0
        and s["code"] not in exclude
    ]
    candidates.sort(key=lambda x: x["trade_amount"], reverse=True)
    seen: set[str] = set()
    result: list[dict] = []
    for stock in candidates:
        if stock["code"] in seen:
            continue
        if not is_common_stock_for_trade(stock):
            continue
        seen.add(stock["code"])
        result.append(stock)
        if len(result) >= max_count:
            break
    return result
