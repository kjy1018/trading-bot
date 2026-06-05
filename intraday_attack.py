"""장중 시간대별 공략 모드."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any


def _parse_hhmm(text: str) -> time:
    h, m = str(text).strip().split(":")[:2]
    return time(int(h), int(m))


def get_intraday_attack_phase(now: datetime | None = None) -> str:
    """
    morning_scalp  — 09:00~10:00 이격도 돌파 단타
    afternoon_swing — 14:00~15:00 정배열 눌림목
    convergence — 그 외 이격도 수렴 중심
    """
    try:
        import config as cfg

        morning_start = _parse_hhmm(
            getattr(cfg, "MORNING_ATTACK_START", "09:00")
        )
        morning_end = _parse_hhmm(getattr(cfg, "MORNING_ATTACK_END", "10:00"))
        afternoon_start = _parse_hhmm(
            getattr(cfg, "AFTERNOON_ATTACK_START", "14:00")
        )
        afternoon_end = _parse_hhmm(
            getattr(cfg, "AFTERNOON_ATTACK_END", "15:00")
        )
    except ImportError:
        morning_start, morning_end = time(9, 0), time(10, 0)
        afternoon_start, afternoon_end = time(14, 0), time(15, 0)

    now = now or datetime.now()
    t = now.time()
    if morning_start <= t < morning_end:
        return "morning_scalp"
    if afternoon_start <= t < afternoon_end:
        return "afternoon_swing"
    return "convergence"
