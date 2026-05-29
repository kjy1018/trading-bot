import json

import requests

from config import ACCOUNT_NO, ACCOUNT_PROD_CODE, BASE_URL
from kis_headers import build_kis_headers
from kis_rate import (
    KISRateLimitError,
    Priority,
    _response_indicates_rate_limit,
    call_with_retry,
)
from quote import fetch_bid_ladder

ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
# 한국투자증권 공식 open-trading-api order_cash 기준 (모의투자)
TR_ID_BUY_MOCK = "VTTC0012U"
TR_ID_SELL_MOCK = "VTTC0011U"

# DEBUG: Previous trade-miss behavior came from treating order submission as an
# immediate fill. All helpers in this module return broker submission receipts
# only; actual position mutation must happen after asynchronous fill
# confirmation elsewhere.
def _str_body(fields: dict) -> dict:
    """요청 본문의 모든 값을 문자열로 변환 (KIS API 필수)."""
    return {key: str(value) for key, value in fields.items()}


def normalize_order_receipt(
    *,
    side: str,
    data: dict,
    output: dict,
    stock_code: str,
    quantity: int,
    order_price: int | None = None,
) -> dict:
    """브로커 주문 접수 응답 -> 제출 영수증."""
    receipt = {
        "success": True,
        "submitted": True,
        "filled": False,
        "side": side,
        "message": data.get("msg1", "주문이 접수되었습니다."),
        "order_no": output.get("ODNO", ""),
        "order_time": output.get("ORD_TMD", ""),
        "stock_code": stock_code,
        "quantity": quantity,
    }
    if order_price is not None:
        receipt["order_price"] = int(order_price)
    return receipt


def _validate_order_inputs(stock_code: str, quantity: int, app_key: str, app_secret: str) -> str:
    code = stock_code.strip()
    if len(code) != 6 or not code.isdigit():
        raise ValueError("종목코드는 6자리 숫자여야 합니다.")
    if quantity < 1:
        raise ValueError("주문 수량은 1주 이상이어야 합니다.")
    if not app_key or not app_secret:
        raise ValueError("app_key와 app_secret은 필수입니다.")
    return code


def _place_market_order(
    access_token: str,
    stock_code: str,
    quantity: int,
    app_key: str,
    app_secret: str,
    tr_id: str,
    sll_type: str | None,
    side_label: str,
) -> dict:
    code = _validate_order_inputs(stock_code, quantity, app_key, app_secret)

    url = f"{BASE_URL}{ORDER_PATH}"
    headers = build_kis_headers(
        access_token=access_token,
        app_key=app_key,
        app_secret=app_secret,
        tr_id=tr_id,
    )

    # 국내주식 현금주문 — EXCG_ID_DVSN_CD(해외주식용) 미포함
    raw_body = {
        "CANO": ACCOUNT_NO,
        "ACNT_PRDT_CD": ACCOUNT_PROD_CODE,
        "PDNO": code,
        "ORD_DVSN": "01",
        "ORD_QTY": quantity,
        "ORD_UNPR": 0,
    }
    if sll_type is not None:
        raw_body["SLL_TYPE"] = sll_type

    body = _str_body(raw_body)

    def _post() -> dict:
        response = requests.post(
            url,
            headers=headers,
            data=json.dumps(body),
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"주문 HTTP 오류 [{response.status_code}]: {response.text}"
            )
        data = response.json()
        if _response_indicates_rate_limit(data, response.text):
            raise KISRateLimitError(
                f"주문 속도 제한 [{data.get('msg_cd')}]: {data.get('msg1')}"
            )
        if data.get("rt_cd") != "0":
            raise RuntimeError(
                f"주문 실패 [{data.get('msg_cd')}]: {data.get('msg1')}"
            )
        output = data.get("output") or {}
        return normalize_order_receipt(
            side=side_label,
            data=data,
            output=output,
            stock_code=code,
            quantity=quantity,
        )

    return call_with_retry(
        _post,
        priority=Priority.ORDER,
        user_message="초당 제한으로 재시도 중...",
    )


def _place_limit_order(
    access_token: str,
    stock_code: str,
    quantity: int,
    price: int,
    app_key: str,
    app_secret: str,
    tr_id: str,
    sll_type: str | None,
    side_label: str,
) -> dict:
    code = _validate_order_inputs(stock_code, quantity, app_key, app_secret)
    if price < 1:
        raise ValueError("지정가는 1원 이상이어야 합니다.")

    url = f"{BASE_URL}{ORDER_PATH}"
    headers = build_kis_headers(
        access_token=access_token,
        app_key=app_key,
        app_secret=app_secret,
        tr_id=tr_id,
    )
    raw_body = {
        "CANO": ACCOUNT_NO,
        "ACNT_PRDT_CD": ACCOUNT_PROD_CODE,
        "PDNO": code,
        "ORD_DVSN": "00",
        "ORD_QTY": quantity,
        "ORD_UNPR": price,
    }
    if sll_type is not None:
        raw_body["SLL_TYPE"] = sll_type
    body = _str_body(raw_body)

    def _post() -> dict:
        response = requests.post(
            url, headers=headers, data=json.dumps(body), timeout=30
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"주문 HTTP 오류 [{response.status_code}]: {response.text}"
            )
        data = response.json()
        if _response_indicates_rate_limit(data, response.text):
            raise KISRateLimitError(
                f"주문 속도 제한 [{data.get('msg_cd')}]: {data.get('msg1')}"
            )
        if data.get("rt_cd") != "0":
            raise RuntimeError(
                f"주문 실패 [{data.get('msg_cd')}]: {data.get('msg1')}"
            )
        output = data.get("output") or {}
        return normalize_order_receipt(
            side=side_label,
            data=data,
            output=output,
            stock_code=code,
            quantity=quantity,
            order_price=price,
        )

    return call_with_retry(
        _post,
        priority=Priority.ORDER,
        user_message="초당 제한으로 재시도 중...",
    )


def buy_market_order(
    access_token: str,
    stock_code: str,
    quantity: int,
    app_key: str,
    app_secret: str,
) -> dict:
    """국내주식 현금 시장가 매수 주문 접수 영수증."""
    return _place_market_order(
        access_token,
        stock_code,
        quantity,
        app_key,
        app_secret,
        TR_ID_BUY_MOCK,
        sll_type=None,
        side_label="buy",
    )


def buy_limit_order(
    access_token: str,
    stock_code: str,
    quantity: int,
    price: int,
    app_key: str,
    app_secret: str,
) -> dict:
    """국내주식 현금 지정가 매수 접수 영수증."""
    return _place_limit_order(
        access_token,
        stock_code,
        quantity,
        price,
        app_key,
        app_secret,
        TR_ID_BUY_MOCK,
        sll_type=None,
        side_label="buy_limit",
    )


def sell_market_order(
    access_token: str,
    stock_code: str,
    quantity: int,
    app_key: str,
    app_secret: str,
) -> dict:
    """국내주식 현금 시장가 매도 접수 영수증."""
    return _place_market_order(
        access_token,
        stock_code,
        quantity,
        app_key,
        app_secret,
        TR_ID_SELL_MOCK,
        sll_type="01",
        side_label="sell",
    )


def sell_smart_sor(
    access_token: str,
    stock_code: str,
    total_quantity: int,
    app_key: str,
    app_secret: str,
    *,
    max_splits: int = 3,
) -> dict:
    """
    스마트 지정가 분할 매도(SOR) 접수.
    호가 조회 실패·잔량 부족 시 시장가 매도 접수로 폴백.
    """
    if total_quantity < 1:
        raise ValueError("매도 수량은 1주 이상이어야 합니다.")

    ladder = fetch_bid_ladder(
        access_token,
        stock_code,
        app_key,
        app_secret,
        levels=max(1, min(3, max_splits)),
    )
    if not ladder:
        m = sell_market_order(
            access_token, stock_code, total_quantity, app_key, app_secret
        )
        m["avg_price"] = None
        m["splits"] = 0
        return m

    prices: list[int] = []
    for i in range(min(3, len(ladder))):
        prices.append(ladder[i][0])
    while len(prices) < 3 and prices:
        prices.append(prices[-1])
    if not prices:
        m = sell_market_order(
            access_token, stock_code, total_quantity, app_key, app_secret
        )
        m["avg_price"] = None
        m["splits"] = 0
        return m

    splits = min(max_splits, total_quantity, len(prices))
    base = total_quantity // splits
    rem = total_quantity % splits
    chunks: list[tuple[int, int]] = []
    for i in range(splits):
        q = base + (1 if i < rem else 0)
        if q < 1:
            continue
        px = prices[min(i, len(prices) - 1)]
        chunks.append((px, q))

    if not chunks:
        m = sell_market_order(
            access_token, stock_code, total_quantity, app_key, app_secret
        )
        m["avg_price"] = None
        m["splits"] = 0
        return m

    order_nos: list[str] = []
    for px, qty in chunks:
        r = _place_limit_order(
            access_token,
            stock_code,
            qty,
            px,
            app_key,
            app_secret,
            TR_ID_SELL_MOCK,
            sll_type="01",
            side_label="sell_sor",
        )
        if r.get("order_no"):
            order_nos.append(str(r["order_no"]))

    return {
        "success": True,
        "submitted": True,
        "filled": False,
        "side": "sell_sor",
        "message": f"SOR 분할 {len(chunks)}건 접수",
        "order_no": ",".join(order_nos),
        "stock_code": _validate_order_inputs(
            stock_code, 1, app_key, app_secret
        ),
        "quantity": total_quantity,
        "splits": len(chunks),
        "avg_price": int(
            round(sum(px * q for px, q in chunks) / max(total_quantity, 1))
        ),
    }
