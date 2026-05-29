"""
KIS Open API 전역 호출 속도 제한.

EGW00201(초당 거래건수 초과) 방지:
- 모든 REST 호출은 단일 락 + 최소 1.5초 간격(기본 초당 1회 미만)
- 주문 처리 중에는 백그라운드 조회 일시 정지(order priority lane)
- 제한 응답 시 2초 후 자동 재시도
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from enum import Enum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_lock = threading.Lock()
_call_sem = threading.Semaphore(1)
_tls = threading.local()
_last_call_at: float = 0.0
_bg_pause_until: float = 0.0
_order_lane_depth: int = 0

RATE_LIMIT_CODES = frozenset({"EGW00201", "EGW00003"})
_RATE_LIMIT_PATTERN = re.compile(
    r"초당\s*거래건수|EGW00201|rate\s*limit",
    re.IGNORECASE,
)

DEFAULT_MIN_INTERVAL_SEC = 1.5


class KISRateLimitError(RuntimeError):
    """KIS 초당 거래건수 초과."""

    def __init__(self, message: str, *, msg_cd: str | None = None) -> None:
        super().__init__(message)
        self.msg_cd = msg_cd


class Priority(Enum):
    NORMAL = "normal"
    ORDER = "order"


def _min_interval() -> float:
    try:
        import config as cfg

        return max(1.5, float(getattr(cfg, "KIS_API_MIN_INTERVAL_SEC", 1.5)))
    except ImportError:
        return DEFAULT_MIN_INTERVAL_SEC


def kis_post_call_sleep() -> None:
    """KIS REST 1건 종료 후 최소 간격(기본 1.5초) 강제 대기."""
    time.sleep(_min_interval())


def kis_loop_pause() -> None:
    """다종목 for/while 루프 — 종목 간 추가 대기(동일 간격)."""
    kis_post_call_sleep()


def is_rate_limit_message(text: str) -> bool:
    if not text:
        return False
    if any(code in text for code in RATE_LIMIT_CODES):
        return True
    return bool(_RATE_LIMIT_PATTERN.search(text))


def is_rate_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, KISRateLimitError):
        return True
    return is_rate_limit_message(str(exc))


def _response_indicates_rate_limit(data: dict[str, Any] | None, raw_text: str = "") -> bool:
    if data:
        msg_cd = str(data.get("msg_cd") or "")
        msg1 = str(data.get("msg1") or "")
        if msg_cd in RATE_LIMIT_CODES:
            return True
        if is_rate_limit_message(msg1):
            return True
    return is_rate_limit_message(raw_text)


def throttle(priority: Priority | str = Priority.NORMAL) -> None:
    """
    다음 KIS REST 호출 전 전역 간격 확보.
    ORDER 우선 시 백그라운드(NORMAL) 호출은 order lane 종료까지 대기.
    """
    if isinstance(priority, str):
        priority = Priority.ORDER if priority == "order" else Priority.NORMAL

    interval = _min_interval()
    while True:
        with _lock:
            now = time.monotonic()
            pause_wait = 0.0
            if priority == Priority.NORMAL and _order_lane_depth > 0:
                pause_wait = max(0.0, _bg_pause_until - now)
            interval_wait = max(0.0, interval - (now - _last_call_at))
            wait = max(pause_wait, interval_wait)
        if wait > 0:
            time.sleep(min(wait, 0.25))
            continue
        with _lock:
            now = time.monotonic()
            if priority == Priority.NORMAL and _order_lane_depth > 0 and now < _bg_pause_until:
                continue
            interval_wait = max(0.0, interval - (now - _last_call_at))
            if interval_wait <= 0:
                return
        time.sleep(interval_wait)


@contextlib.contextmanager
def kis_request(priority: Priority | str = Priority.NORMAL) -> Iterator[None]:
    """KIS REST 1건 — 전역 직렬화 + 호출 종료 시점부터 최소 간격 (재진입 허용)."""
    depth = int(getattr(_tls, "kis_depth", 0) or 0)
    if depth > 0:
        yield
        return
    with _call_sem:
        throttle(priority)
        _tls.kis_depth = 1
        try:
            yield
        finally:
            _tls.kis_depth = 0
            with _lock:
                _last_call_at = time.monotonic()
            kis_post_call_sleep()


def mark_call_complete() -> None:
    """레거시 — kis_request() 사용 권장."""
    with _lock:
        _last_call_at = time.monotonic()


@contextlib.contextmanager
def order_priority_lane(max_duration_sec: float = 45.0) -> Iterator[None]:
    """주문·체결 확인 구간 — 백그라운드 inquire/순위 조회 일시 정지."""
    global _order_lane_depth, _bg_pause_until
    with _lock:
        _order_lane_depth += 1
        _bg_pause_until = time.monotonic() + max_duration_sec
    try:
        yield
    finally:
        with _lock:
            _order_lane_depth = max(0, _order_lane_depth - 1)
            if _order_lane_depth == 0:
                _bg_pause_until = 0.0


def call_with_retry(
    fn: Callable[..., T],
    /,
    *args: Any,
    priority: Priority | str = Priority.ORDER,
    max_retries: int | None = None,
    retry_wait_sec: float | None = None,
    user_message: str = "초당 제한으로 재시도 중...",
    **kwargs: Any,
) -> T:
    """주문·중요 조회용 — EGW00201 시 2초 후 재시도."""
    try:
        import config as cfg

        retries = int(max_retries or getattr(cfg, "KIS_RATE_LIMIT_MAX_RETRIES", 4))
        wait = float(retry_wait_sec or getattr(cfg, "KIS_RATE_LIMIT_RETRY_SEC", 2.0))
    except ImportError:
        retries = 4
        wait = 2.0

    last_exc: BaseException | None = None
    for attempt in range(retries):
        if attempt > 0:
            logger.info("%s (%d/%d)", user_message, attempt + 1, retries)
            time.sleep(wait)
        try:
            if priority == Priority.ORDER or priority == "order":
                with order_priority_lane():
                    with kis_request(priority):
                        return fn(*args, **kwargs)
            with kis_request(priority):
                return fn(*args, **kwargs)
        except KISRateLimitError as exc:
            last_exc = exc
            throttle(priority)
        except RuntimeError as exc:
            if is_rate_limit_error(exc):
                last_exc = KISRateLimitError(str(exc))
                throttle(priority)
                continue
            raise
        except Exception as exc:
            if is_rate_limit_error(exc):
                last_exc = KISRateLimitError(str(exc))
                throttle(priority)
                continue
            raise
    if last_exc:
        raise last_exc
    raise KISRateLimitError("KIS API 호출 재시도 한도 초과")
