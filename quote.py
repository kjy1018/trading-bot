"""
국내주식 호가 조회 — SOR 분할 매도용 매수 1~3호가(내가 매도 시 체결될 매수측 호가).
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from config import BASE_URL
from kis_headers import build_kis_headers
from kis_rate import kis_request

logger = logging.getLogger(__name__)

ASK_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
TR_ID_ASK = "FHKST01010200"


def _safe_int(val: Any) -> int:
    try:
        return int(float(str(val).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def fetch_bid_ladder(
    access_token: str,
    stock_code: str,
    app_key: str,
    app_secret: str,
    levels: int = 3,
) -> list[tuple[int, int]]:
    """
    매수호가 1~levels 단가·잔량 (가격 오름차순 = 1호가가 가장 높음).
    반환: [(가격, 잔량), ...] 유효 호가만.
    """
    code = stock_code.strip()
    url = f"{BASE_URL}{ASK_PATH}"
    headers = build_kis_headers(
        access_token=access_token,
        app_key=app_key,
        app_secret=app_secret,
        tr_id=TR_ID_ASK,
    )
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    try:
        with kis_request():
            response = requests.get(url, headers=headers, params=params, timeout=15)
            if response.status_code != 200:
                logger.warning("호가 조회 HTTP %s %s", response.status_code, code)
                return []
            data = response.json()
            if data.get("rt_cd") != "0":
                logger.warning("호가 조회 실패 %s: %s", code, data.get("msg1"))
                return []
            out = data.get("output1") or {}
            if isinstance(out, list) and out:
                out = out[0]
            if not isinstance(out, dict):
                return []
            ladder: list[tuple[int, int]] = []
            for i in range(1, levels + 1):
                p = _safe_int(out.get(f"bidp{i}") or out.get(f"BIDP{i}"))
                q = _safe_int(
                    out.get(f"bidp_rsqn{i}") or out.get(f"bidp_rsqn_{i}") or 0
                )
                if p > 0:
                    ladder.append((p, q))
            ladder.sort(key=lambda x: -x[0])
            return ladder[:levels]
    except requests.RequestException as exc:
        logger.warning("호가 조회 예외 %s: %s", code, exc)
        return []
