"""
종목·슬롯별 매매 모드 영구 저장 — selected_modes.json

F5·재기동 시 st.session_state 가 비어도 사용자 설정(단타/스윙/장투) 유지.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_lock = threading.RLock()

PROJECT_DIR = Path(__file__).resolve().parent
SELECTED_MODES_FILE = PROJECT_DIR / "selected_modes.json"

MODE_LABELS: tuple[str, ...] = ("단타", "스윙", "장투")
LABEL_TO_VALUE: dict[str, str] = {
    "단타": "scalping",
    "스윙": "swing",
    "장투": "long_term",
}
VALUE_TO_LABEL: dict[str, str] = {v: k for k, v in LABEL_TO_VALUE.items()}
DEFAULT_LABEL = "장투"


def _norm_key(key: str) -> str:
    text = str(key or "").strip()
    if len(text) == 6 and text.isdigit():
        return text
    if text.startswith("_slot_") and text[6:].isdigit():
        return text
    return ""


def _norm_label(label: str) -> str:
    text = str(label or "").strip()
    return text if text in MODE_LABELS else ""


def _load_file_unlocked() -> dict[str, str]:
    if not SELECTED_MODES_FILE.is_file():
        return {}
    try:
        raw = json.loads(SELECTED_MODES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return {}
    modes_raw = raw.get("modes") if isinstance(raw, dict) else raw
    if not isinstance(modes_raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, label in modes_raw.items():
        norm_key = _norm_key(str(key))
        norm_label = _norm_label(str(label))
        if norm_key and norm_label:
            out[norm_key] = norm_label
    return out


def _write_file_unlocked(modes: dict[str, str]) -> None:
    cleaned: dict[str, str] = {}
    for key, label in modes.items():
        norm_key = _norm_key(str(key))
        norm_label = _norm_label(str(label))
        if norm_key and norm_label:
            cleaned[norm_key] = norm_label
    payload: dict[str, Any] = {
        "version": 1,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "modes": cleaned,
    }
    tmp = SELECTED_MODES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SELECTED_MODES_FILE)


def _migrate_legacy_trade_state() -> dict[str, str]:
    """구버전 trade_state.json ui_trading_modes → selected_modes.json 1회 이전."""
    try:
        import trade_state

        legacy = trade_state.load_ui_trading_modes()
        if legacy:
            return dict(legacy)
    except Exception:
        pass
    return {}


def load_modes() -> dict[str, str]:
    """selected_modes.json 전체 로드 (F5·기동 시 최우선 호출)."""
    with _lock:
        modes = _load_file_unlocked()
        if modes:
            return dict(modes)
        legacy = _migrate_legacy_trade_state()
        if legacy:
            _write_file_unlocked(legacy)
            return dict(legacy)
        return {}


def save_mode(ticker_key: str, label: str) -> None:
    """단일 키 변경 — 파일 전체를 즉시 갱신."""
    norm_key = _norm_key(ticker_key)
    norm_label = _norm_label(label) or DEFAULT_LABEL
    if not norm_key:
        return
    with _lock:
        modes = _load_file_unlocked()
        modes[norm_key] = norm_label
        _write_file_unlocked(modes)


def save_modes(modes: dict[str, str]) -> None:
    """전체 장부 일괄 저장."""
    with _lock:
        merged = _load_file_unlocked()
        for key, label in modes.items():
            norm_key = _norm_key(str(key))
            norm_label = _norm_label(str(label))
            if norm_key and norm_label:
                merged[norm_key] = norm_label
        _write_file_unlocked(merged)


def get_mode_label(ticker_key: str, *, fallback_value: str | None = None) -> str:
    """한글 라벨 — 디스크 저장값이 최우선."""
    norm_key = _norm_key(ticker_key)
    if norm_key:
        saved = load_modes().get(norm_key)
        if saved:
            return saved
    if fallback_value:
        lowered = str(fallback_value).strip().lower()
        if lowered in VALUE_TO_LABEL:
            return VALUE_TO_LABEL[lowered]
        if fallback_value in LABEL_TO_VALUE:
            return str(fallback_value)
    return DEFAULT_LABEL


def get_mode_value(ticker_key: str, *, fallback_value: str | None = None) -> str:
    label = get_mode_label(ticker_key, fallback_value=fallback_value)
    return LABEL_TO_VALUE.get(label, "long_term")


def slot_storage_key(slot_idx: int) -> str:
    """전황판 슬롯 번호(1~N) — UI·주문의 최우선 모드 키."""
    return f"_slot_{int(slot_idx)}"


def resolve_mode_label_for_slot(
    slot_idx: int | None,
    *,
    code: str | None = None,
    fallback_value: str | None = None,
) -> str:
    """
    슬롯 UI 설정 → 종목코드 → fallback 순으로 모드 라벨 결정.

    종목코드만 조회하면 다른 슬롯에서 남은 설정이 섞여 단타↔스윙이 뒤바뀔 수 있어
    슬롯 키(_slot_N)를 항상 최우선한다.
    """
    modes = load_modes()
    if slot_idx is not None and int(slot_idx) > 0:
        sk = slot_storage_key(int(slot_idx))
        saved = modes.get(sk)
        if saved:
            return saved
    norm_code = _norm_key(str(code or ""))
    if norm_code and len(norm_code) == 6:
        saved = modes.get(norm_code)
        if saved:
            return saved
    if norm_code:
        return get_mode_label(norm_code, fallback_value=fallback_value)
    return get_mode_label("", fallback_value=fallback_value)


def resolve_mode_value_for_slot(
    slot_idx: int | None,
    *,
    code: str | None = None,
    fallback_value: str | None = None,
) -> str:
    label = resolve_mode_label_for_slot(
        slot_idx, code=code, fallback_value=fallback_value
    )
    return LABEL_TO_VALUE.get(label, "long_term")


def sync_slot_mode(slot_idx: int, label: str, code: str | None = None) -> None:
    """슬롯·(있으면) 종목코드에 동일 라벨을 동시에 기록."""
    norm_label = _norm_label(label) or DEFAULT_LABEL
    save_mode(slot_storage_key(slot_idx), norm_label)
    norm_code = _norm_key(str(code or ""))
    if norm_code and len(norm_code) == 6:
        save_mode(norm_code, norm_label)


def get_mode_value_for_position(position: dict[str, Any]) -> str:
    """trading_logic·스케줄러 — 포지션 dict + 영구 설정 병합."""
    code = str(position.get("code") or "").strip()
    norm = _norm_key(code) if code else ""
    fallback = str(position.get("trading_mode") or "swing")
    if norm:
        return get_mode_value(norm, fallback_value=fallback)
    return fallback if fallback in LABEL_TO_VALUE.values() else "swing"


def selectbox_index_for_label(label: str) -> int:
    """Streamlit selectbox index 고정용."""
    if label in MODE_LABELS:
        return MODE_LABELS.index(label)
    return MODE_LABELS.index(DEFAULT_LABEL)
