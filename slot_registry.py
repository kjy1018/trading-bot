"""
고정 slot 포트폴리오 — 슬롯별 실시간 성격(long_term / swing / day_trading).
매도 후 empty 유지; empty 슬롯도 사용자가 지정한 성격 유지.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from trading_categories import (
    DAY_TRADING,
    LABEL_TO_CATEGORY,
    LONG_TERM,
    SWING,
    TRADING_CATEGORIES,
    category_matches,
    label_for_category,
    migrate_legacy_slot_uid,
    normalize_trading_category,
)

try:
    import config as _cfg
except ImportError:
    _cfg = None  # type: ignore

DEFAULT_SLOT_DEFINITIONS: list[dict[str, Any]] = [
    {"slot_uid": "1", "display_idx": 1},
    {"slot_uid": "2", "display_idx": 2},
    {"slot_uid": "3", "display_idx": 3},
    {"slot_uid": "4", "display_idx": 4},
    {"slot_uid": "5", "display_idx": 5},
]

SLOT_PORTFOLIO_SCHEMA = "slot_portfolio_v2"
SLOT_PORTFOLIO_SCHEMA_V1 = "slot_portfolio_v1"


def slot_definitions() -> list[dict[str, Any]]:
    raw = getattr(_cfg, "PORTFOLIO_SLOT_DEFINITIONS", None) if _cfg else None
    if isinstance(raw, list) and raw:
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            spec = dict(item)
            uid = str(spec.get("slot_uid") or spec.get("display_idx") or "").strip()
            if not uid:
                continue
            spec["slot_uid"] = uid
            spec["display_idx"] = int(spec.get("display_idx") or uid)
            out.append(spec)
        if out:
            return out
    return deepcopy(DEFAULT_SLOT_DEFINITIONS)


def normalize_slot_type(raw_mode: object, *, fallback: str = SWING) -> str:
    return normalize_trading_category(raw_mode, fallback=fallback)


def slot_type_matches(slot_type: str, trading_mode: object) -> bool:
    return category_matches(slot_type, trading_mode)


def slot_entry_mode(entry: dict[str, Any]) -> str:
    """슬롯의 현재 성격 — slot_personality 우선."""
    return normalize_trading_category(
        entry.get("slot_personality") or entry.get("slot_type") or entry.get("slot_id")
    )


def is_position_assigned_in_slots(slots: dict[str, dict[str, Any]], code: str) -> bool:
    norm = str(code or "").strip()
    for entry in slots.values():
        if str(entry.get("status") or "empty") != "filled":
            continue
        if str(entry.get("code") or "") == norm:
            return True
    return False


def find_any_empty_slot(slots: dict[str, dict[str, Any]]) -> str | None:
    ordered = sorted(slots.values(), key=lambda e: int(e.get("display_idx") or 0))
    for entry in ordered:
        if str(entry.get("status") or "empty") == "empty":
            return str(entry.get("slot_uid"))
    return None


def _apply_position_slot_meta(
    pos: dict[str, Any],
    slot_uid: str,
    mode: str,
) -> None:
    spec = _spec_by_uid(slot_uid)
    pos["slot_uid"] = slot_uid
    pos["slot_id"] = normalize_trading_category(mode)
    pos["slot_type"] = pos["slot_id"]
    pos["slot_personality"] = pos["slot_id"]
    pos["display_idx"] = int(spec.get("display_idx") or slot_uid) if spec else int(slot_uid)
    pos["trading_mode"] = normalize_trading_category(mode)


def _resolve_position_trading_mode(pos: dict[str, Any]) -> str:
    try:
        from selected_modes import get_mode_value_for_position

        return normalize_trading_category(get_mode_value_for_position(pos))
    except ImportError:
        pass
    return normalize_trading_category(
        pos.get("trading_mode") or pos.get("mode_label") or pos.get("slot_type")
    )


def _spec_by_uid(slot_uid: str) -> dict[str, Any] | None:
    for spec in slot_definitions():
        if str(spec.get("slot_uid")) == str(slot_uid):
            return dict(spec)
    return None


def _initial_personality_for_display_idx(display_idx: int) -> str:
    """최초 부트스트랩 — selected_modes.json 슬롯 설정 우선."""
    try:
        import selected_modes

        book = selected_modes.load_modes()
        label = book.get(f"_slot_{int(display_idx)}")
        if label and label in LABEL_TO_CATEGORY:
            return normalize_trading_category(LABEL_TO_CATEGORY[label])
    except Exception:
        pass
    defaults = {
        1: LONG_TERM,
        2: LONG_TERM,
        3: SWING,
        4: SWING,
        5: SWING,
    }
    return defaults.get(int(display_idx), SWING)


def _personality_fields(category: str) -> dict[str, str]:
    cat = normalize_trading_category(category)
    return {
        "slot_id": cat,
        "slot_type": cat,
        "slot_personality": cat,
    }


def default_slots_book() -> dict[str, dict[str, Any]]:
    book: dict[str, dict[str, Any]] = {}
    for spec in slot_definitions():
        uid = str(spec["slot_uid"])
        display_idx = int(spec.get("display_idx") or uid)
        pers = _initial_personality_for_display_idx(display_idx)
        book[uid] = {
            "slot_uid": uid,
            "display_idx": display_idx,
            "status": "empty",
            "code": None,
            **_personality_fields(pers),
        }
    return book


def normalize_slots_book(raw: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    book = default_slots_book()
    if not isinstance(raw, dict):
        return book
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        uid = migrate_legacy_slot_uid(entry.get("slot_uid") or entry.get("display_idx") or key)
        if not uid or uid not in book:
            uid = migrate_legacy_slot_uid(key)
        if not uid or uid not in book:
            continue
        merged = dict(book[uid])
        merged.update(entry)
        merged["slot_uid"] = uid
        pers = normalize_trading_category(
            merged.get("slot_personality")
            or merged.get("slot_id")
            or merged.get("slot_type")
            or book[uid].get("slot_type")
        )
        merged.update(_personality_fields(pers))
        merged["display_idx"] = int(merged.get("display_idx") or book[uid]["display_idx"])
        status = str(merged.get("status") or "empty").lower()
        merged["status"] = "filled" if status == "filled" else "empty"
        code = merged.get("code")
        merged["code"] = str(code).strip() if code else None
        if merged["status"] == "filled" and not merged["code"]:
            merged["status"] = "empty"
        if merged["status"] == "empty":
            merged["code"] = None
        book[uid] = merged
    return book


def slot_uid_for_display_idx(display_idx: int) -> str | None:
    for spec in slot_definitions():
        if int(spec.get("display_idx") or 0) == int(display_idx):
            return str(spec["slot_uid"])
    return None


def slot_id_for_display_idx(display_idx: int) -> str | None:
    """UI 슬롯 번호 → 카테고리 slot_id (long_term | swing | day_trading)."""
    for spec in slot_definitions():
        if int(spec.get("display_idx") or 0) == int(display_idx):
            return normalize_trading_category(spec.get("slot_id"))
    return None


def display_idx_for_slot_uid(slot_uid: str) -> int | None:
    spec = _spec_by_uid(str(slot_uid))
    if spec:
        return int(spec.get("display_idx") or 0)
    return None


def display_idx_for_slot_id(slot_id: str) -> int | None:
    """레거시 — 카테고리만으로는 유일하지 않음. 첫 empty 슬롯 display_idx."""
    cat = normalize_trading_category(slot_id)
    for spec in slot_definitions():
        if normalize_trading_category(spec.get("slot_type")) == cat:
            return int(spec.get("display_idx") or 0)
    return None


def slot_spec_for_display_idx(
    display_idx: int,
    slots: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """런타임 slots book 기준 — UI·매매 로직 실시간 성격."""
    uid = slot_uid_for_display_idx(display_idx)
    if not uid:
        return None
    if slots is None:
        import trade_state

        slots = trade_state.get_slots_book()
    entry = slots.get(uid)
    if isinstance(entry, dict):
        return dict(entry)
    return _spec_by_uid(uid)


def set_slot_personality_in_book(
    slots: dict[str, dict[str, Any]],
    slot_uid: str,
    category: object,
) -> bool:
    """슬롯 성격 변경 — empty/filled 무관, 재시작 없이 메모리 즉시 반영."""
    entry = slots.get(str(slot_uid))
    if not entry:
        uid = migrate_legacy_slot_uid(slot_uid)
        if uid:
            entry = slots.get(uid)
    if not entry:
        return False
    cat = normalize_trading_category(category)
    entry.update(_personality_fields(cat))
    entry["personality_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return True


def get_slot_personality_for_display_idx(
    display_idx: int,
    slots: dict[str, dict[str, Any]] | None = None,
) -> str:
    spec = slot_spec_for_display_idx(display_idx, slots)
    if spec:
        return normalize_trading_category(
            spec.get("slot_personality") or spec.get("slot_type")
        )
    return _initial_personality_for_display_idx(display_idx)


def resolve_live_trading_mode_for_position(
    position: dict[str, Any],
    slots: dict[str, dict[str, Any]] | None = None,
) -> str:
    """
    매수/매도 직전 — slots book 의 현재 슬롯 성격을 최우선.
    (UI 드롭다운 변경 → 즉시 이 값이 바뀜)
    """
    if slots is None:
        import trade_state

        slots = trade_state.get_slots_book()
    code = str(position.get("code") or "").strip()
    uid = migrate_legacy_slot_uid(position.get("slot_uid") or position.get("display_idx"))
    if not uid and code:
        for entry in slots.values():
            if str(entry.get("code") or "") == code:
                uid = str(entry.get("slot_uid") or "")
                break
    if uid and uid in slots:
        return normalize_trading_category(
            slots[uid].get("slot_personality") or slots[uid].get("slot_type")
        )
    try:
        from selected_modes import get_mode_value_for_position

        return normalize_trading_category(get_mode_value_for_position(position))
    except ImportError:
        return normalize_trading_category(position.get("trading_mode"))


def slot_spec_for_display_idx_legacy(display_idx: int) -> dict[str, Any] | None:
    uid = slot_uid_for_display_idx(display_idx)
    return _spec_by_uid(uid) if uid else None


def slot_spec(slot_uid: str, slots: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    if slots is None:
        import trade_state

        slots = trade_state.get_slots_book()
    entry = slots.get(str(slot_uid))
    if isinstance(entry, dict):
        return dict(entry)
    return _spec_by_uid(str(slot_uid))


def known_slot_uids() -> set[str]:
    return {str(spec["slot_uid"]) for spec in slot_definitions()}


def known_slot_ids() -> set[str]:
    return set(TRADING_CATEGORIES)


def count_empty_slots(slots: dict[str, dict[str, Any]], *, slot_type: str | None = None) -> int:
    n = 0
    for entry in slots.values():
        if str(entry.get("status") or "empty") != "empty":
            continue
        if slot_type and not slot_type_matches(slot_entry_mode(entry), slot_type):
            continue
        n += 1
    return n


def find_empty_slot_for_mode(
    slots: dict[str, dict[str, Any]],
    trading_mode: object,
    *,
    preferred_slot_uid: str | None = None,
    preferred_slot_id: str | None = None,
    preferred_display_idx: int | None = None,
) -> str | None:
    """동일 카테고리 empty slot_uid — long_term / swing / day_trading."""
    mode_type = normalize_trading_category(trading_mode)
    preferred = preferred_slot_uid
    if not preferred and preferred_display_idx:
        preferred = slot_uid_for_display_idx(int(preferred_display_idx))
    if not preferred and preferred_slot_id:
        preferred = migrate_legacy_slot_uid(preferred_slot_id)

    if preferred and preferred in slots:
        entry = slots[str(preferred)]
        if (
            str(entry.get("status") or "empty") == "empty"
            and slot_type_matches(slot_entry_mode(entry), mode_type)
        ):
            return str(preferred)

    ordered = sorted(slots.values(), key=lambda e: int(e.get("display_idx") or 0))
    for entry in ordered:
        if str(entry.get("status") or "empty") != "empty":
            continue
        if not slot_type_matches(slot_entry_mode(entry), mode_type):
            continue
        return str(entry.get("slot_uid"))
    return None


def assign_slot(
    slots: dict[str, dict[str, Any]],
    slot_uid: str,
    code: str,
    *,
    trading_mode: object | None = None,
) -> bool:
    entry = slots.get(str(slot_uid))
    if not entry:
        return False
    if str(entry.get("status") or "empty") != "empty":
        return False
    if trading_mode is not None and not slot_type_matches(slot_entry_mode(entry), trading_mode):
        return False
    entry["status"] = "filled"
    entry["code"] = str(code)
    return True


def release_slot_by_code(slots: dict[str, dict[str, Any]], code: str) -> str | None:
    norm = str(code or "").strip()
    released: str | None = None
    for uid, entry in slots.items():
        if str(entry.get("code") or "") == norm:
            entry["status"] = "empty"
            entry["code"] = None
            released = str(uid)
    return released


def release_slot(slots: dict[str, dict[str, Any]], slot_uid: str) -> None:
    entry = slots.get(str(slot_uid))
    if not entry:
        uid = migrate_legacy_slot_uid(slot_uid)
        if uid:
            entry = slots.get(uid)
    if not entry:
        return
    entry["status"] = "empty"
    entry["code"] = None


def is_slot_empty(slots: dict[str, dict[str, Any]], slot_uid: str) -> bool:
    uid = migrate_legacy_slot_uid(slot_uid) or str(slot_uid)
    entry = slots.get(uid)
    if not entry:
        return False
    return str(entry.get("status") or "empty") == "empty"


def sync_slots_with_positions(
    slots: dict[str, dict[str, Any]],
    positions: dict[str, dict[str, Any]],
) -> None:
    code_to_uid: dict[str, str] = {}
    for code, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        uid = migrate_legacy_slot_uid(pos.get("slot_uid") or pos.get("display_idx"))
        if not uid:
            uid = migrate_legacy_slot_uid(pos.get("slot_id"))
        if uid and uid in slots:
            code_to_uid[str(code)] = uid

    for _uid, entry in slots.items():
        code = entry.get("code")
        if str(entry.get("status") or "empty") == "filled" and code:
            if str(code) not in positions:
                entry["status"] = "empty"
                entry["code"] = None

    for code, uid in code_to_uid.items():
        pos = positions.get(code)
        if not pos:
            continue
        entry = slots[uid]
        if str(entry.get("status") or "empty") == "empty":
            if entry.get("code") and str(entry.get("code")) != str(code):
                continue
            if not slot_type_matches(
                slot_entry_mode(entry),
                pos.get("trading_mode") or pos.get("mode_label"),
            ):
                continue
            entry["status"] = "filled"
            entry["code"] = str(code)


def reconcile_holdings_to_slots(
    slots: dict[str, dict[str, Any]],
    positions: dict[str, dict[str, Any]],
) -> list[str]:
    """
    증권사 잔고·메모리 포지션을 슬롯북에 강제 반영.
    슬롯이 없으면 uncategorized 코드 목록 반환.
    """
    sync_slots_with_positions(slots, positions)
    uncategorized: list[str] = []

    for code, pos in sorted(positions.items(), key=lambda kv: str(kv[0])):
        if not isinstance(pos, dict):
            continue
        norm_code = str(code).strip()
        if is_position_assigned_in_slots(slots, norm_code):
            uid = migrate_legacy_slot_uid(pos.get("slot_uid") or pos.get("display_idx"))
            if not uid:
                for entry in slots.values():
                    if str(entry.get("code") or "") == norm_code:
                        uid = str(entry.get("slot_uid") or "")
                        break
            if uid and uid in slots:
                _apply_position_slot_meta(pos, uid, slot_entry_mode(slots[uid]))
            continue

        uid = migrate_legacy_slot_uid(pos.get("slot_uid") or pos.get("display_idx"))
        if uid and uid in slots and str(slots[uid].get("status") or "empty") == "empty":
            slot_mode = slot_entry_mode(slots[uid])
            if assign_slot(slots, uid, norm_code, trading_mode=slot_mode):
                _apply_position_slot_meta(pos, uid, slot_mode)
                continue

        mode = _resolve_position_trading_mode(pos)
        pos["trading_mode"] = mode
        uid = find_empty_slot_for_mode(slots, mode)
        if uid and assign_slot(slots, uid, norm_code, trading_mode=mode):
            _apply_position_slot_meta(pos, uid, mode)
            continue

        uid = find_any_empty_slot(slots)
        if uid:
            slot_mode = slot_entry_mode(slots[uid])
            if assign_slot(slots, uid, norm_code, trading_mode=slot_mode):
                _apply_position_slot_meta(pos, uid, slot_mode)
                pos["trading_mode"] = slot_mode
                continue

        uncategorized.append(norm_code)

    return uncategorized


def assign_legacy_positions_to_slots(
    slots: dict[str, dict[str, Any]],
    positions: dict[str, dict[str, Any]],
) -> None:
    reconcile_holdings_to_slots(slots, positions)


def build_slot_layout(
    positions: dict[str, dict[str, Any]],
    slots: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    layout: list[dict[str, Any]] = []
    assigned_codes: set[str] = set()
    ordered = sorted(slots.values(), key=lambda e: int(e.get("display_idx") or 0))
    for entry in ordered:
        uid = str(entry.get("slot_uid") or "")
        cat = slot_entry_mode(entry)
        status = str(entry.get("status") or "empty")
        code = str(entry.get("code") or "").strip()
        pos: dict[str, Any] | None = None
        if status == "filled" and code and code in positions:
            pos = dict(positions[code])
            pos["slot_uid"] = uid
            pos["slot_id"] = cat
            pos["slot_type"] = cat
            pos["slot_personality"] = cat
            pos["display_idx"] = int(entry.get("display_idx") or 0)
            pos["trading_mode"] = normalize_trading_category(
                pos.get("trading_mode") or cat
            )
            assigned_codes.add(code)
        layout.append(
            {
                "slot_uid": uid,
                "slot_id": cat,
                "slot_type": cat,
                "display_idx": int(entry.get("display_idx") or 0),
                "status": status,
                "is_empty": status != "filled" or pos is None,
                "is_uncategorized": False,
                "code": code or None,
                "position": pos,
            }
        )

    for code, raw_pos in sorted(positions.items(), key=lambda kv: str(kv[0])):
        norm = str(code).strip()
        if not norm or norm in assigned_codes or not isinstance(raw_pos, dict):
            continue
        pos = dict(raw_pos)
        pos["slot_uid"] = None
        pos["slot_id"] = "uncategorized"
        pos["slot_type"] = "uncategorized"
        pos["display_idx"] = 0
        pos["is_uncategorized"] = True
        pos["trading_mode"] = normalize_trading_category(pos.get("trading_mode"))
        layout.append(
            {
                "slot_uid": f"uncat_{norm}",
                "slot_id": "uncategorized",
                "slot_type": "uncategorized",
                "display_idx": 0,
                "status": "filled",
                "is_empty": False,
                "is_uncategorized": True,
                "code": norm,
                "position": pos,
            }
        )
    return layout


def is_category_grouped_portfolio_file(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    if str(data.get("schema") or "") == SLOT_PORTFOLIO_SCHEMA:
        return True
    cats = data.get("categories")
    return isinstance(cats, dict) and bool(set(cats.keys()) & set(TRADING_CATEGORIES))


def is_slot_keyed_portfolio_file(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    if str(data.get("schema") or "") == SLOT_PORTFOLIO_SCHEMA_V1:
        return True
    keys = {str(k) for k in data.keys() if k not in ("schema", "categories", "positions", "slots")}
    legacy = keys & {
        "long_term_1", "swing_1", "swing_2", "scalping_1", "scalping_2",
        "long_term", "swing", "day_trading",
    }
    return bool(legacy) and "positions" not in data and "categories" not in data


def serialize_slot_portfolio_file(
    positions: dict[str, dict[str, Any]],
    slots: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """
    positions_state.json — 3카테고리(long_term/swing/day_trading)별 분류 저장.
    """
    norm_slots = normalize_slots_book(slots)
    sync_slots_with_positions(norm_slots, positions)
    for code, pos in positions.items():
        if isinstance(pos, dict):
            pos["trading_mode"] = normalize_trading_category(pos.get("trading_mode"))

    payload: dict[str, Any] = {
        "schema": SLOT_PORTFOLIO_SCHEMA,
        "categories": {cat: {} for cat in TRADING_CATEGORIES},
    }
    ordered = sorted(norm_slots.values(), key=lambda e: int(e.get("display_idx") or 0))
    for entry in ordered:
        uid = str(entry.get("slot_uid") or "")
        cat = normalize_trading_category(entry.get("slot_type"))
        code = str(entry.get("code") or "").strip()
        status = str(entry.get("status") or "empty")
        pos: dict[str, Any] | None = None
        if status == "filled" and code and code in positions:
            pos = dict(positions[code])
            pos["slot_uid"] = uid
            pos["slot_id"] = cat
            pos["slot_type"] = cat
            pos["display_idx"] = int(entry.get("display_idx") or 0)
            pos["trading_mode"] = normalize_trading_category(pos.get("trading_mode"))
        cell = {
            "slot_uid": uid,
            "slot_id": cat,
            "slot_type": cat,
            "slot_personality": cat,
            "personality_updated_at": entry.get("personality_updated_at"),
            "display_idx": int(entry.get("display_idx") or 0),
            "status": "empty" if status != "filled" or not code else "filled",
            "code": code or None,
            "position": pos,
        }
        payload["categories"][cat][uid] = cell
    return payload


def deserialize_category_portfolio_file(
    data: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    book = default_slots_book()
    positions: dict[str, dict[str, Any]] = {}
    cats = data.get("categories") if isinstance(data.get("categories"), dict) else {}
    for cat_key, bucket in cats.items():
        if not isinstance(bucket, dict):
            continue
        cat = normalize_trading_category(cat_key)
        for uid_key, raw in bucket.items():
            if not isinstance(raw, dict):
                continue
            uid = migrate_legacy_slot_uid(raw.get("slot_uid") or uid_key) or str(uid_key)
            if uid not in book:
                continue
            merged = dict(book[uid])
            pers = normalize_trading_category(
                raw.get("slot_personality")
                or raw.get("slot_type")
                or cat
            )
            merged.update(_personality_fields(pers))
            merged["personality_updated_at"] = raw.get("personality_updated_at")
            merged["display_idx"] = int(raw.get("display_idx") or merged["display_idx"])
            status = str(raw.get("status") or "empty").lower()
            code = str(raw.get("code") or "").strip()
            pos_raw = raw.get("position")
            if isinstance(pos_raw, dict):
                pc = str(pos_raw.get("code") or code or "").strip()
                pc = "".join(ch for ch in pc if ch.isdigit())[-6:]
                if len(pc) == 6:
                    code = pc
            if status == "filled" and code:
                merged["status"] = "filled"
                merged["code"] = code
                if isinstance(pos_raw, dict):
                    pos = dict(pos_raw)
                    pos["code"] = code
                    pos["slot_uid"] = uid
                    pos["slot_id"] = cat
                    pos["slot_type"] = cat
                    pos["display_idx"] = merged["display_idx"]
                    pos["trading_mode"] = normalize_trading_category(pos.get("trading_mode"))
                    positions[code] = pos
            else:
                merged["status"] = "empty"
                merged["code"] = None
            book[uid] = merged
    return positions, normalize_slots_book(book)


def deserialize_slot_portfolio_file(
    data: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """v1 flat slot 키 JSON → (positions, slots)."""
    book = default_slots_book()
    positions: dict[str, dict[str, Any]] = {}
    for key, raw in data.items():
        if key in ("schema", "categories", "positions", "slots") or not isinstance(raw, dict):
            continue
        uid = migrate_legacy_slot_uid(raw.get("slot_uid") or raw.get("display_idx") or key)
        if not uid or uid not in book:
            continue
        merged = dict(book[uid])
        merged["slot_type"] = normalize_trading_category(
            raw.get("slot_type") or raw.get("slot_id") or merged["slot_type"]
        )
        merged["slot_id"] = merged["slot_type"]
        merged["display_idx"] = int(raw.get("display_idx") or merged["display_idx"])
        status = str(raw.get("status") or "empty").lower()
        code = str(raw.get("code") or "").strip()
        pos_raw = raw.get("position")
        if isinstance(pos_raw, dict):
            pc = str(pos_raw.get("code") or code or "").strip()
            pc = "".join(ch for ch in pc if ch.isdigit())[-6:]
            if len(pc) == 6:
                code = pc
        if status == "filled" and code:
            merged["status"] = "filled"
            merged["code"] = code
            if isinstance(pos_raw, dict):
                pos = dict(pos_raw)
                pos["code"] = code
                pos["slot_uid"] = uid
                pos["slot_id"] = merged["slot_type"]
                pos["slot_type"] = merged["slot_type"]
                pos["display_idx"] = merged["display_idx"]
                pos["trading_mode"] = normalize_trading_category(pos.get("trading_mode"))
                positions[code] = pos
        else:
            merged["status"] = "empty"
            merged["code"] = None
        book[uid] = merged
    return positions, normalize_slots_book(book)


def deserialize_legacy_portfolio_file(
    data: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    raw_pos = data.get("positions") if isinstance(data, dict) else None
    positions: dict[str, dict[str, Any]] = {}
    if isinstance(raw_pos, dict):
        positions = {str(k): dict(v) for k, v in raw_pos.items() if isinstance(v, dict)}
    raw_slots = data.get("slots") if isinstance(data, dict) else None
    slots = normalize_slots_book(raw_slots if isinstance(raw_slots, dict) else default_slots_book())
    for pos in positions.values():
        if isinstance(pos, dict):
            pos["trading_mode"] = normalize_trading_category(pos.get("trading_mode"))
    return positions, slots


def load_portfolio_file(
    data: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(data, dict) or not data:
        return {}, default_slots_book()
    if is_category_grouped_portfolio_file(data):
        return deserialize_category_portfolio_file(data)
    if is_slot_keyed_portfolio_file(data):
        return deserialize_slot_portfolio_file(data)
    if "positions" in data or "slots" in data:
        positions, slots = deserialize_legacy_portfolio_file(data)
        assign_legacy_positions_to_slots(slots, positions)
        return positions, normalize_slots_book(slots)
    positions: dict[str, dict[str, Any]] = {}
    for key, val in data.items():
        if key in ("schema", "categories") or not isinstance(val, dict):
            continue
        code = str(key).strip()
        code = "".join(ch for ch in code if ch.isdigit())[-6:]
        if len(code) == 6:
            positions[code] = dict(val)
    slots = default_slots_book()
    if positions:
        assign_legacy_positions_to_slots(slots, positions)
    return positions, normalize_slots_book(slots)
