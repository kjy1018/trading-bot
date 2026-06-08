"""
엔진 Cool-down — 감시 시각 지연 등 과부하 시 Hard Refresh 대신 휴지기.

감시 시각이 WATCH_STALE_HARD_REFRESH_SEC(기본 120초) 초과 시 1회 진입,
ENGINE_COOLDOWN_SEC(기본 600초) 동안 API·스캔·매수 중단.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cooldown_until: float = 0.0
_cooldown_reason: str = ""
_entered_at: float = 0.0
_on_enter: Callable[[str], None] | None = None
_on_exit: Callable[[], None] | None = None
_quiet_loggers: list[tuple[logging.Logger, int]] = []


def _cfg_float(name: str, default: float) -> float:
    try:
        import config as cfg

        return float(getattr(cfg, name, default))
    except ImportError:
        return default


def _cfg_bool(name: str, default: bool) -> bool:
    try:
        import config as cfg

        return bool(getattr(cfg, name, default))
    except ImportError:
        return default


def cooldown_duration_sec() -> float:
    return max(60.0, _cfg_float("ENGINE_COOLDOWN_SEC", 600.0))


def cooldown_enabled() -> bool:
    return _cfg_bool("ENGINE_COOLDOWN_ENABLED", True)


def register_hooks(
    *,
    on_enter: Callable[[str], None] | None = None,
    on_exit: Callable[[], None] | None = None,
) -> None:
    global _on_enter, _on_exit
    _on_enter = on_enter
    _on_exit = on_exit


def _set_quiet_logging(quiet: bool) -> None:
    global _quiet_loggers
    names = (
        "scheduler",
        "account",
        "market_scan",
        "brain",
        "stock_swing",
        "stock_intraday",
        "kis_rate",
    )
    if quiet:
        _quiet_loggers = []
        for name in names:
            lg = logging.getLogger(name)
            _quiet_loggers.append((lg, lg.level))
            lg.setLevel(logging.WARNING)
        return
    for lg, prev in _quiet_loggers:
        lg.setLevel(prev)
    _quiet_loggers = []


def is_cooldown_active() -> bool:
    if not cooldown_enabled():
        return False
    with _lock:
        if _cooldown_until <= 0:
            return False
        return time.time() < _cooldown_until


def is_cooldown_expired() -> bool:
    with _lock:
        if _cooldown_until <= 0:
            return False
        return time.time() >= _cooldown_until


def remaining_sec() -> float:
    with _lock:
        if _cooldown_until <= 0:
            return 0.0
        return max(0.0, _cooldown_until - time.time())


def cooldown_snapshot() -> dict[str, Any]:
    with _lock:
        active = (
            cooldown_enabled()
            and _cooldown_until > 0
            and time.time() < _cooldown_until
        )
        return {
            "active": active,
            "enabled": cooldown_enabled(),
            "reason": _cooldown_reason,
            "entered_at": _entered_at,
            "until_epoch": _cooldown_until,
            "remaining_sec": max(0.0, _cooldown_until - time.time()) if active else 0.0,
        }


def enter_cooldown(reason: str) -> bool:
    """Cool-down 진입 — 이미 활성이면 False."""
    if not cooldown_enabled():
        return False
    global _cooldown_until, _cooldown_reason, _entered_at
    now = time.time()
    with _lock:
        if _cooldown_until > now:
            return False
        duration = cooldown_duration_sec()
        _cooldown_until = now + duration
        _cooldown_reason = str(reason or "과부하").strip()
        _entered_at = now
    _set_quiet_logging(True)
    logger.warning(
        "Cool-down 진입 — %s · %d초 휴지 (매수·API·스캔 중단)",
        _cooldown_reason,
        int(duration),
    )
    if _on_enter:
        try:
            _on_enter(_cooldown_reason)
        except Exception as exc:
            logger.warning("Cool-down on_enter hook 실패: %s", exc)
    return True


def exit_cooldown() -> bool:
    """휴지기 종료 — 정상 운영 복귀."""
    global _cooldown_until, _cooldown_reason, _entered_at
    with _lock:
        if _cooldown_until <= 0:
            return False
        _cooldown_until = 0.0
        _cooldown_reason = ""
        _entered_at = 0.0
    _set_quiet_logging(False)
    logger.warning("시스템 휴지기 종료, 감시 재시작")
    if _on_exit:
        try:
            _on_exit()
        except Exception as exc:
            logger.warning("Cool-down on_exit hook 실패: %s", exc)
    return True


def try_recover_if_due() -> bool:
    """만료 시 자동 복구. True = 방금 복구됨."""
    if not is_cooldown_expired():
        return False
    return exit_cooldown()


def sleep_chunk_sec() -> float:
    """엔진 루프 대기 단위(초)."""
    return min(30.0, max(1.0, _cfg_float("ENGINE_COOLDOWN_SLEEP_CHUNK_SEC", 15.0)))
