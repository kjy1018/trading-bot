import requests

from config import BASE_URL
from kis_headers import build_kis_headers
from kis_rate import (
    KISRateLimitError,
    _response_indicates_rate_limit,
    kis_loop_pause,
    kis_request,
)

PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
TR_ID_MOCK = "FHKST01010100"


def get_current_price(
    access_token: str,
    stock_code: str,
    app_key: str,
    app_secret: str,
) -> dict:
    """국내주식 현재가 조회 — 현재가, 전일 대비 등락률, 거래량 반환."""
    code = stock_code.strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError("종목코드는 6자리 숫자여야 합니다.")

    if not app_key or not app_secret:
        raise ValueError("app_key와 app_secret은 필수입니다.")

    url = f"{BASE_URL}{PRICE_PATH}"
    headers = build_kis_headers(
        access_token=access_token,
        app_key=app_key,
        app_secret=app_secret,
        tr_id=TR_ID_MOCK,
    )
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
    }

    with kis_request():
        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code != 200:
            raise RuntimeError(
                f"현재가 조회 HTTP 오류 [{response.status_code}]: {response.text}"
            )

        data = response.json()
        if _response_indicates_rate_limit(data, response.text):
            raise KISRateLimitError(
                f"현재가 조회 속도 제한 [{data.get('msg_cd')}]: {data.get('msg1')}"
            )
        if data.get("rt_cd") != "0":
            raise RuntimeError(
                f"현재가 조회 실패 [{data.get('msg_cd')}]: {data.get('msg1')}"
            )

    output = data.get("output") or {}
    return {
        "code": code,
        "name": output.get("hts_kor_isnm", ""),
        "price": int(output.get("stck_prpr") or 0),
        "change_rate": float(output.get("prdy_ctrt") or 0),
        "volume": int(output.get("acml_vol") or 0),
    }


def get_watchlist_quotes(
    access_token: str,
    watchlist: list[tuple[str, str]],
    app_key: str,
    app_secret: str,
) -> list[dict]:
    """관심 종목 리스트의 현재가·등락률을 일괄 조회합니다."""
    quotes = []
    for idx, (code, fallback_name) in enumerate(watchlist):
        if idx > 0:
            kis_loop_pause()
        quote = get_current_price(access_token, code, app_key, app_secret)
        if not quote.get("name"):
            quote["name"] = fallback_name
        quotes.append(quote)
    return quotes
