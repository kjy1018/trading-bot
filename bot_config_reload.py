"""
대시보드 설정 변경 → 디스크 저장 → 봇 엔진 즉시 재로딩.

감시 대상:
  - selected_modes.json  (슬롯/종목 매매 모드)
  - positions_state.json (슬롯·포지션 스냅샷)
  - slots_config.json    (UI 변경 시 집계 스냅샷 — watchdog용)
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
SLOTS_CONFIG_FILE = PROJECT_DIR / "slots_config.json"

_mtime_lock = threading.RLock()
_last_mtimes: dict[str, float] = {}
_watchdog_started = False
_suppress_watch_until: float = 0.0


def suppress_file_watch(seconds: float = 2.0) -> None:
    """자체 저장 직후 watchdog/mtime 폴링 재진입 방지."""
    global _suppress_watch_until
    import time

    _suppress_watch_until = time.time() + max(0.5, float(seconds))


def watched_config_paths() -> tuple[Path, ...]:
    import selected_modes
    import trade_state

    return (
        selected_modes.SELECTED_MODES_FILE,
        trade_state.POSITIONS_STATE_FILE,
        SLOTS_CONFIG_FILE,
    )


def snapshot_mtimes() -> dict[str, float]:
    out: dict[str, float] = {}
    for path in watched_config_paths():
        key = str(path)
        if path.is_file():
            out[key] = path.stat().st_mtime
        else:
            out[key] = 0.0
    return out


def init_config_mtime_baseline() -> None:
    """엔진 기동 시 mtime 기준선."""
    global _last_mtimes
    with _mtime_lock:
        if not isinstance(_last_mtimes, dict):
            _last_mtimes = {}
        _last_mtimes = snapshot_mtimes()


def detect_config_file_changes() -> bool:
    """감시 파일 mtime 변경 여부."""
    global _last_mtimes
    import time

    if time.time() < _suppress_watch_until:
        return False
    current = snapshot_mtimes()
    with _mtime_lock:
        if not isinstance(_last_mtimes, dict):
            _last_mtimes = {}
        if not _last_mtimes:
            _last_mtimes = dict(current)
            return False
        keys = set(current) | set(_last_mtimes)
        changed = any(current.get(k, 0.0) != _last_mtimes.get(k, 0.0) for k in keys)
        if changed:
            _last_mtimes = dict(current)
        return changed


def reload_config(reason: str) -> int:
    """
    설정 변경 신호 — trade_state nonce 증가 + mtime 기준 갱신.
    엔진 루프가 nonce/mtime을 보고 reload_engine_from_disk() 호출.
    """
    import trade_state

    nonce = trade_state.bump_engine_config_reload(reason)
    global _last_mtimes
    with _mtime_lock:
        _last_mtimes = snapshot_mtimes()
    logger.info("reload_config: %s (nonce=%d)", reason, nonce)
    return nonce


def finalize_config_write() -> None:
    """봇 자체 저장 직후 mtime 기준 재동기화 + watchdog 억제."""
    suppress_file_watch(2.5)
    init_config_mtime_baseline()


def load_slots_config_file() -> dict[str, Any]:
    """slots_config.json — UI 집계 스냅샷."""
    if not SLOTS_CONFIG_FILE.is_file():
        return {}
    try:
        raw = json.loads(SLOTS_CONFIG_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError, TypeError):
        return {}


def resolve_mode_from_slots_config(
    display_idx: int,
    *,
    slot_uid: str | None = None,
    code: str | None = None,
) -> str | None:
    """slots_config.json 에서 슬롯/종목 모드 조회 (selected_modes 보조)."""
    data = load_slots_config_file()
    if not data:
        return None

    modes = data.get("modes")
    if isinstance(modes, dict) and int(display_idx or 0) > 0:
        import selected_modes

        label = modes.get(selected_modes.slot_storage_key(int(display_idx)))
        if label and label in selected_modes.LABEL_TO_VALUE:
            return selected_modes.LABEL_TO_VALUE[label]

    norm_code = str(code or "").strip()
    if isinstance(modes, dict) and len(norm_code) == 6 and norm_code.isdigit():
        label = modes.get(norm_code)
        if label:
            import selected_modes

            if label in selected_modes.LABEL_TO_VALUE:
                return selected_modes.LABEL_TO_VALUE[label]

    for row in data.get("slots") or []:
        if not isinstance(row, dict):
            continue
        if slot_uid and str(row.get("slot_uid") or "") == str(slot_uid):
            pers = row.get("slot_personality")
            if pers:
                return str(pers)
        if int(row.get("display_idx") or 0) == int(display_idx or 0) and display_idx:
            pers = row.get("slot_personality")
            if pers:
                return str(pers)
    return None


def export_slots_config_snapshot(
    slots: dict[str, dict[str, Any]],
    *,
    reason: str = "",
) -> None:
    """대시보드 슬롯 설정 집계 — slots_config.json."""
    import selected_modes

    modes = selected_modes.load_modes()
    simplified: list[dict[str, Any]] = []
    for entry in sorted(
        slots.values(),
        key=lambda e: int(e.get("display_idx") or 0),
    ):
        if not isinstance(entry, dict):
            continue
        simplified.append(
            {
                "slot_uid": entry.get("slot_uid"),
                "display_idx": entry.get("display_idx"),
                "slot_type": entry.get("slot_type"),
                "slot_personality": entry.get("slot_personality"),
                "personality_updated_at": entry.get("personality_updated_at"),
                "status": entry.get("status"),
                "code": entry.get("code"),
            }
        )
    payload = {
        "version": 1,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
        "modes": modes,
        "slots": simplified,
    }
    tmp = SLOTS_CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SLOTS_CONFIG_FILE)
    global _last_mtimes
    with _mtime_lock:
        if not isinstance(_last_mtimes, dict):
            _last_mtimes = {}
        _last_mtimes[str(SLOTS_CONFIG_FILE)] = SLOTS_CONFIG_FILE.stat().st_mtime


def start_config_watchdog() -> None:
    """watchdog 설치 시 파일 변경 → reload_config + 엔진 wake (미설치면 mtime 폴링만)."""
    global _watchdog_started
    if _watchdog_started:
        return
    _watchdog_started = True
    init_config_mtime_baseline()

    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        logger.info(
            "watchdog 패키지 없음 — 엔진 루프 mtime 폴링으로 설정 변경 감지"
        )
        return

    watch_names = {p.name for p in watched_config_paths()}

    class _Handler(FileSystemEventHandler):
        def on_modified(self, event: Any) -> None:
            if getattr(event, "is_directory", False):
                return
            name = Path(str(getattr(event, "src_path", ""))).name
            if name not in watch_names:
                return
            reload_config(f"watchdog:{name}")
            try:
                from scheduler import _wake_engine

                _wake_engine()
            except Exception as exc:
                logger.debug("watchdog wake 실패: %s", exc)

        def on_created(self, event: Any) -> None:
            self.on_modified(event)

    observer = Observer()
    observer.schedule(_Handler(), str(PROJECT_DIR), recursive=False)
    observer.daemon = True
    observer.start()
    logger.info("watchdog 설정 감시 시작: %s", ", ".join(sorted(watch_names)))
