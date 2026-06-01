"""
한국투자증권 국내주식 실시간 체결가 WebSocket (H0STCNT0).
REST 폴링 없이 체결 틱마다 가격 전달 → 초당 호출 제한 회피.
Heartbeat: N초간 수신 없으면 close() 후 무한 재연결(1s→3s→5s 백오프).
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
    """수신 공백 허용 시간 — 보안 SW 지연 고려, 최소 15초."""
    if _cfg is None:
        return 20.0
    raw = float(getattr(_cfg, "WS_HEARTBEAT_TIMEOUT_SEC", 20.0))
    return max(15.0, raw)


def _reconnect_backoff_steps() -> tuple[float, ...]:
    """재연결 대기 — 1초 → 3초 → 5초, 이후 5초 유지."""
    if _cfg is None:
        return (1.0, 3.0, 5.0)
    raw = getattr(_cfg, "WS_RECONNECT_BACKOFF_SEC", (1.0, 3.0, 5.0))
    if isinstance(raw, (list, tuple)) and raw:
        steps = tuple(float(x) for x in raw if float(x) > 0)
        if steps:
            return steps
    return (1.0, 3.0, 5.0)


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
        on_reconnected: Callable[[], None] | None = None,
        heartbeat_timeout_sec: float | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._rest_base = rest_base
        self._app_key = app_key
        self._app_secret = app_secret
        self._on_trade = on_trade
        self._on_status = on_status
        self._on_reconnected = on_reconnected
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
        self._opened_once = False
        self._reconnect_attempts = 0
        self._was_connected = False
        self._pending_resync = False
        self._resync_callback_lock = threading.Lock()
        self._resync_callback_running = False

    def _touch_rx(self) -> None:
        self._last_rx_at = time.time()

    def _seconds_since_rx(self) -> float | None:
        ref = self._last_rx_at if self._last_rx_at > 0 else self._connected_at
        if ref <= 0:
            return None
        return max(0.0, time.time() - ref)

    def is_connected(self) -> bool:
        app = self._ws_app
        sock = getattr(app, "sock", None) if app is not None else None
        return bool(
            _HAS_WS
            and self._running
            and self._connected
            and self._opened_once
            and app is not None
            and sock is not None
        )

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
        else:
            self._last_error = None
        if self._on_status:
            try:
                self._on_status(msg, err)
            except Exception:
                logger.exception("ws on_status")

    def _mark_disconnected(self) -> None:
        """끊김 표시 — 재연결 시 REST 잔고 동기화 플래그."""
        if self._was_connected:
            self._pending_resync = True
        self._was_connected = False
        self._connected = False
        self._reconnecting = True
        self._approval_key = None

    def _fire_reconnected_callback(self) -> None:
        """재연결 직후 콜백(별도 스레드) — inquire_balance 동기화 등."""
        if not self._on_reconnected:
            return
        with self._resync_callback_lock:
            if self._resync_callback_running:
                return
            self._resync_callback_running = True

        def _run() -> None:
            try:
                self._on_reconnected()
            except Exception:
                logger.exception("ws on_reconnected callback")
            finally:
                with self._resync_callback_lock:
                    self._resync_callback_running = False

        threading.Thread(
            target=_run,
            name="kis-ws-post-reconnect",
            daemon=True,
        ).start()

    def _force_reconnect(self, reason: str) -> None:
        logger.debug("ws heartbeat timeout — reconnect: %s", reason)
        self._emit("WS 연결 끊김 (재연결 중...)", None)
        self._mark_disconnected()
        self._safe_ws_close(self._ws_app, context="heartbeat")

    def _heartbeat_loop(self) -> None:
        while self._running:
            time.sleep(1.0)
            if not self._connected:
                continue
            if self._ws_app is None:
                continue
            gap = self._seconds_since_rx()
            if gap is None:
                continue
            if gap > self._heartbeat_timeout:
                self._force_reconnect(
                    f"Heartbeat: {gap:.1f}s 동안 데이터 수신 없음 "
                    f"(한도 {self._heartbeat_timeout:.0f}s)"
                )

    def _next_reconnect_delay(self) -> float:
        """
        지수 백오프 — 1초 → 3초 → 5초, 이후 5초 유지.
        무한 재시도(상한 없음).
        """
        steps = _reconnect_backoff_steps()
        idx = min(self._reconnect_attempts, len(steps) - 1)
        delay = steps[idx]
        self._reconnect_attempts += 1
        if self._reconnect_attempts == 1 or self._reconnect_attempts % 20 == 0:
            logger.info(
                "ws 재연결 대기 %.1fs (시도 %d회)",
                delay,
                self._reconnect_attempts,
            )
        else:
            logger.debug(
                "ws 재연결 대기 %.1fs (시도 %d회)",
                delay,
                self._reconnect_attempts,
            )
        return delay

    def _reset_reconnect_backoff(self) -> None:
        self._reconnect_attempts = 0

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
        self._mark_disconnected()
        self._pending_resync = False
        self._safe_ws_close(self._ws_app, context="stop")

    def _approval(self) -> str:
        if self._approval_key and time.time() - self._approval_ts < 3500:
            return self._approval_key
        self._approval_key = fetch_websocket_approval(
            self._rest_base, self._app_key, self._app_secret
        )
        self._approval_ts = time.time()
        return self._approval_key

    def _send(self, ws: websocket.WebSocketApp, code: str, reg: str) -> None:
        if ws is None:
            return
        payload = {
            "header": {
                "approval_key": self._approval(),
                "custtype": "P",
                "tr_type": reg,
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}},
        }
        self._safe_ws_send(ws, json.dumps(payload), context=f"reg:{code}:{reg}")

    def _safe_ws_send(
        self,
        ws: websocket.WebSocketApp | None,
        payload: str,
        *,
        context: str,
    ) -> None:
        if ws is None:
            return
        sock = getattr(ws, "sock", None)
        if sock is None:
            return
        try:
            ws.send(payload)
        except Exception as exc:
            logger.debug("ws send skipped (%s): %s", context, exc)

    def _safe_ws_close(
        self,
        ws: websocket.WebSocketApp | None,
        *,
        context: str,
    ) -> None:
        if ws is None:
            return
        sock = getattr(ws, "sock", None)
        if sock is None:
            return
        try:
            ws.close()
        except Exception as exc:
            logger.debug("ws close skipped (%s): %s", context, exc)

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
        hub = self

        while hub._running:
            hub._reconnecting = True
            hub._subscribed.clear()
            try:
                hub._emit("WS 연결 시도", None)

                def on_open(ws: websocket.WebSocketApp) -> None:
                    if ws is None:
                        return
                    need_resync = hub._pending_resync
                    hub._pending_resync = False
                    hub._connected = True
                    hub._was_connected = True
                    hub._opened_once = True
                    hub._reconnecting = False
                    hub._reset_reconnect_backoff()
                    hub._connected_at = time.time()
                    hub._last_rx_at = 0.0
                    if need_resync:
                        hub._emit("WS 재연결됨 · 잔고 동기화 중", None)
                        hub._fire_reconnected_callback()
                    else:
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
                                hub._safe_ws_send(ws, message, context="pingpong")
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
                    err = str(error)
                    logger.debug("ws on_error (재연결 예정): %s", err)
                    hub._emit("WS 연결 끊김 (재연결 중...)", None)
                    hub._mark_disconnected()

                def on_close(ws, code, msg) -> None:
                    logger.debug(
                        "ws on_close code=%s msg=%s (재연결 예정)",
                        code,
                        msg,
                    )
                    hub._emit("WS 연결 끊김 (재연결 중...)", None)
                    hub._mark_disconnected()

                hub._ws_app = websocket.WebSocketApp(
                    hub._ws_url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                hub._ws_app.run_forever(ping_interval=60, ping_timeout=30)
            except Exception as exc:
                logger.debug("ws run_forever 종료 — 재연결 예정: %s", exc)
                hub._emit("WS 연결 끊김 (재연결 중...)", None)
                hub._mark_disconnected()
            if not hub._running:
                break
            delay = hub._next_reconnect_delay()
            hub._reconnecting = True
            hub._emit("WS 재연결 대기 중...", None)
            time.sleep(delay)
