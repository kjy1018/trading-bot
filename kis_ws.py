"""
한국투자증권 국내주식 실시간 체결가 WebSocket (H0STCNT0).
REST 폴링 없이 체결 틱마다 가격 전달 → 초당 호출 제한 회피.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import requests

from kis_rate import kis_request

logger = logging.getLogger(__name__)

try:
    import websocket

    _HAS_WS = True
except ImportError:
    websocket = None  # type: ignore
    _HAS_WS = False


def fetch_websocket_approval(rest_base: str, app_key: str, app_secret: str) -> str:
    """POST /oauth2/Approval → approval_key."""
    url = f"{rest_base.rstrip('/')}/oauth2/Approval"
    headers = {"content-type": "application/json; charset=utf-8"}
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "secretkey": app_secret,
    }
    with kis_request():
        response = requests.post(url, headers=headers, data=json.dumps(body), timeout=15)
        response.raise_for_status()
        data = response.json()
    key = data.get("approval_key")
    if not key:
        raise RuntimeError(f"approval_key 없음: {data}")
    return str(key)


def _parse_h0stcnt0_price(message: str) -> tuple[str, int] | None:
    """0|H0STCNT0|001|CODE^TIME^PRICE^..."""
    if "H0STCNT0" not in message or "^" not in message:
        return None
    parts = message.split("|")
    if len(parts) < 4 or parts[1] != "H0STCNT0":
        return None
    fields = parts[3].split("^")
    if len(fields) < 3:
        return None
    code = fields[0].strip()[-6:]
    if len(code) != 6 or not code.isdigit():
        return None
    try:
        price = int(fields[2].replace(",", "").strip())
    except ValueError:
        return None
    if price <= 0:
        return None
    return code, price


class KisRealtimeHub:
    """단일 WS 세션으로 보유 종목 H0STCNT0 구독."""

    def __init__(
        self,
        *,
        ws_url: str,
        rest_base: str,
        app_key: str,
        app_secret: str,
        on_trade: Callable[[str, int], None],
        on_status: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._rest_base = rest_base
        self._app_key = app_key
        self._app_secret = app_secret
        self._on_trade = on_trade
        self._on_status = on_status
        self._lock = threading.Lock()
        self._desired_codes: set[str] = set()
        self._subscribed: set[str] = set()
        self._ws_app: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._approval_key: str | None = None
        self._approval_ts: float = 0.0
        self._last_error: str | None = None
        self._connected = False

    def is_ready(self) -> bool:
        return bool(_HAS_WS and self._running and self._connected)

    def last_error(self) -> str | None:
        return self._last_error

    def set_target_codes(self, codes: set[str]) -> None:
        with self._lock:
            self._desired_codes = {c for c in codes if len(c) == 6 and c.isdigit()}

    def _emit(self, msg: str, err: str | None = None) -> None:
        self._last_error = err
        if self._on_status:
            try:
                self._on_status(msg, err)
            except Exception:
                logger.exception("ws on_status")

    def start(self) -> bool:
        if not _HAS_WS:
            logger.error("pip install websocket-client 필요")
            self._emit("websocket-client 미설치", "ImportError")
            return False
        with self._lock:
            if self._running:
                return True
            self._running = True
        threading.Thread(target=self._run_loop, name="kis-ws", daemon=True).start()
        return True

    def stop(self) -> None:
        self._running = False
        try:
            if self._ws_app:
                self._ws_app.close()
        except Exception:
            pass

    def _approval(self) -> str:
        if self._approval_key and time.time() - self._approval_ts < 3500:
            return self._approval_key
        self._approval_key = fetch_websocket_approval(
            self._rest_base, self._app_key, self._app_secret
        )
        self._approval_ts = time.time()
        return self._approval_key

    def _send(self, ws: websocket.WebSocketApp, code: str, reg: str) -> None:
        payload = {
            "header": {
                "approval_key": self._approval(),
                "custtype": "P",
                "tr_type": reg,
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}},
        }
        ws.send(json.dumps(payload))

    def _sync(self, ws: websocket.WebSocketApp) -> None:
        with self._lock:
            want = set(self._desired_codes)
        for code in list(self._subscribed - want):
            try:
                self._send(ws, code, "2")
            except Exception as exc:
                logger.warning("ws unsub %s: %s", code, exc)
            self._subscribed.discard(code)
        for code in want - self._subscribed:
            try:
                self._send(ws, code, "1")
            except Exception as exc:
                logger.warning("ws sub %s: %s", code, exc)
            else:
                self._subscribed.add(code)

    def _run_loop(self) -> None:
        backoff = 1.0
        hub = self

        while hub._running:
            try:
                hub._emit("WS 연결 시도", None)

                def on_open(ws: websocket.WebSocketApp) -> None:
                    hub._connected = True
                    hub._emit("WS 연결됨", None)
                    hub._sync(ws)

                def on_message(ws: websocket.WebSocketApp, message: str) -> None:
                    if not message:
                        return
                    if message[0] in "{[":
                        try:
                            j = json.loads(message)
                            hdr = j.get("header") or {}
                            if hdr.get("tr_id") == "PINGPONG":
                                ws.send(message)
                                return
                        except json.JSONDecodeError:
                            pass
                        return
                    parsed = _parse_h0stcnt0_price(message)
                    if parsed:
                        c, p = parsed
                        try:
                            hub._on_trade(c, p)
                        except Exception:
                            logger.exception("on_trade %s", c)
                    with hub._lock:
                        want = set(hub._desired_codes)
                    if want != hub._subscribed:
                        hub._sync(ws)

                def on_error(ws: websocket.WebSocketApp, error: object) -> None:
                    hub._connected = False
                    err = str(error)
                    logger.warning("ws error: %s", err)
                    hub._emit("WS 오류", err)

                def on_close(ws, code, msg) -> None:
                    hub._connected = False
                    hub._emit("WS 종료", str(msg) if msg else None)

                hub._ws_app = websocket.WebSocketApp(
                    hub._ws_url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                hub._ws_app.run_forever(ping_interval=60, ping_timeout=30)
            except Exception as exc:
                hub._connected = False
                logger.exception("ws run_forever")
                hub._emit("WS 재연결 대기", str(exc))
                time.sleep(min(backoff, 30.0))
                backoff = min(backoff * 1.5, 30.0)
            else:
                backoff = 1.0
            if not hub._running:
                break
            time.sleep(1.0)
