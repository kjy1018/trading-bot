"""
한국투자증권 국내주식 실시간 체결가 WebSocket (H0STCNT0).
REST 폴링 없이 체결 틱마다 가격 전달 → 초당 호출 제한 회피.
Heartbeat: N초간 수신 없으면 close() 후 재연결.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import requests

from kis_rate import kis_request

logger = logging.getLogger(__name__)

try:
    import config as _cfg
except ImportError:
    _cfg = None  # type: ignore

try:
    import websocket

    _HAS_WS = True
except ImportError:
    websocket = None  # type: ignore
    _HAS_WS = False


def _heartbeat_timeout_sec() -> float:
    if _cfg is None:
        return 5.0
    return float(getattr(_cfg, "WS_HEARTBEAT_TIMEOUT_SEC", 5.0))


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
    """단일 WS 세션으로 보유 종목 H0STCNT0 구독 + heartbeat 감시."""

    def __init__(
        self,
        *,
        ws_url: str,
        rest_base: str,
        app_key: str,
        app_secret: str,
        on_trade: Callable[[str, int], None],
        on_status: Callable[[str, str | None], None] | None = None,
        heartbeat_timeout_sec: float | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._rest_base = rest_base
        self._app_key = app_key
        self._app_secret = app_secret
        self._on_trade = on_trade
        self._on_status = on_status
        self._heartbeat_timeout = float(
            heartbeat_timeout_sec
            if heartbeat_timeout_sec is not None
            else _heartbeat_timeout_sec()
        )
        self._lock = threading.Lock()
        self._desired_codes: set[str] = set()
        self._subscribed: set[str] = set()
        self._ws_app: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._running = False
        self._approval_key: str | None = None
        self._approval_ts: float = 0.0
        self._last_error: str | None = None
        self._status_label: str = "WS 초기화 전"
        self._connected = False
        self._connected_at: float = 0.0
        self._last_rx_at: float = 0.0
        self._last_trade_at: float = 0.0
        self._reconnecting = False

    def _touch_rx(self) -> None:
        self._last_rx_at = time.time()

    def _seconds_since_rx(self) -> float | None:
        ref = self._last_rx_at if self._last_rx_at > 0 else self._connected_at
        if ref <= 0:
            return None
        return max(0.0, time.time() - ref)

    def is_connected(self) -> bool:
        return bool(_HAS_WS and self._running and self._connected)

    def is_ready(self) -> bool:
        """소켓 open + heartbeat 구간 내 실제 수신이 있어야 True."""
        if not self.is_connected():
            return False
        gap = self._seconds_since_rx()
        if gap is None:
            return False
        return gap <= self._heartbeat_timeout

    def last_error(self) -> str | None:
        return self._last_error

    def health_snapshot(self) -> dict[str, Any]:
        gap = self._seconds_since_rx()
        alive = self.is_ready()
        connected = self.is_connected()
        if not _HAS_WS:
            label = "websocket-client 미설치"
        elif not self._running:
            label = "WS 중지됨"
        elif self._reconnecting or (not connected and self._running):
            label = "WS 연결 끊김 (재연결 중...)"
        elif connected and not alive:
            label = "WS 연결됨 · 데이터 수신 없음 (재연결 예정)"
        elif alive:
            label = "WS 정상 (실시간 수신 중)"
        else:
            label = self._status_label or "WS 상태 확인 중"
        trade_gap = (
            max(0.0, time.time() - self._last_trade_at)
            if self._last_trade_at > 0
            else None
        )
        return {
            "connected": connected,
            "alive": alive,
            "reconnecting": bool(self._reconnecting or (self._running and not connected)),
            "status_label": label,
            "seconds_since_rx": gap,
            "seconds_since_trade": trade_gap,
            "heartbeat_timeout_sec": self._heartbeat_timeout,
            "last_error": self._last_error,
        }

    def set_target_codes(self, codes: set[str]) -> None:
        with self._lock:
            self._desired_codes = {c for c in codes if len(c) == 6 and c.isdigit()}

    def _emit(self, msg: str, err: str | None = None) -> None:
        self._status_label = msg
        if err:
            self._last_error = err
        if self._on_status:
            try:
                self._on_status(msg, err)
            except Exception:
                logger.exception("ws on_status")

    def _force_reconnect(self, reason: str) -> None:
        self._connected = False
        self._reconnecting = True
        logger.warning("ws heartbeat timeout — reconnect: %s", reason)
        self._emit("WS 연결 끊김 (재연결 중...)", reason)
        app = self._ws_app
        if app is not None:
            try:
                app.close()
            except Exception as exc:
                logger.debug("ws close on heartbeat: %s", exc)

    def _heartbeat_loop(self) -> None:
        while self._running:
            time.sleep(1.0)
            if not self._connected:
                continue
            gap = self._seconds_since_rx()
            if gap is None:
                continue
            if gap > self._heartbeat_timeout:
                self._force_reconnect(
                    f"Heartbeat: {gap:.1f}s 동안 데이터 수신 없음 "
                    f"(한도 {self._heartbeat_timeout:.0f}s)"
                )

    def start(self) -> bool:
        if not _HAS_WS:
            logger.error("pip install websocket-client 필요")
            self._emit("websocket-client 미설치", "ImportError")
            return False
        with self._lock:
            if self._running:
                return True
            self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="kis-ws", daemon=True)
        self._thread.start()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="kis-ws-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        self._connected = False
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
            hub._reconnecting = True
            hub._subscribed.clear()
            try:
                hub._emit("WS 연결 시도", None)

                def on_open(ws: websocket.WebSocketApp) -> None:
                    hub._connected = True
                    hub._reconnecting = False
                    hub._connected_at = time.time()
                    hub._last_rx_at = 0.0
                    hub._emit("WS 연결됨 · 수신 대기", None)
                    hub._sync(ws)

                def on_message(ws: websocket.WebSocketApp, message: str) -> None:
                    if not message:
                        return
                    hub._touch_rx()
                    if message[0] in "{[":
                        try:
                            j = json.loads(message)
                            hdr = j.get("header") or {}
                            if hdr.get("tr_id") == "PINGPONG":
                                ws.send(message)
                                return
                            if hub._connected and hub._status_label.startswith("WS 연결"):
                                hub._emit("WS 정상 (실시간 수신 중)", None)
                        except json.JSONDecodeError:
                            pass
                        return
                    parsed = _parse_h0stcnt0_price(message)
                    if parsed:
                        c, p = parsed
                        hub._last_trade_at = time.time()
                        hub._emit("WS 정상 (실시간 수신 중)", None)
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
                    hub._reconnecting = True
                    err = str(error)
                    logger.warning("ws error: %s", err)
                    hub._emit("WS 연결 끊김 (재연결 중...)", err)

                def on_close(ws, code, msg) -> None:
                    hub._connected = False
                    hub._reconnecting = True
                    detail = str(msg) if msg else None
                    hub._emit("WS 연결 끊김 (재연결 중...)", detail)

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
                hub._reconnecting = True
                logger.exception("ws run_forever")
                hub._emit("WS 연결 끊김 (재연결 중...)", str(exc))
                time.sleep(min(backoff, 30.0))
                backoff = min(backoff * 1.5, 30.0)
            else:
                backoff = 1.0
            if not hub._running:
                break
            hub._reconnecting = True
            hub._emit("WS 연결 끊김 (재연결 중...)", hub._last_error)
            time.sleep(1.0)
