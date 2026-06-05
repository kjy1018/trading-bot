"""
수동 매수 — 슬롯 점유(Lock) · positions_state.json 원자 기록 · UI 동기화.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import trade_state
from slot_registry import (
    SLOT_STATUS_LOCKED,
    display_idx_for_slot_uid,
    is_slot_lockable,
    is_slot_locked_for_code,
    lock_slot,
    release_slot_lock,
    slot_spec_for_display_idx,
    slot_type_matches,
    slot_uid_for_display_idx,
)
from stock_names import normalize_code

logger = logging.getLogger(__name__)


class PortfolioSlotError(RuntimeError):
    """슬롯 Lock/해제 실패."""


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def resolve_forced_slot_uid(
    display_idx: int,
    *,
    trading_mode: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """UI 슬롯 번호 → slot_uid (기본 슬롯 자동 선택 없음)."""
    idx = int(display_idx)
    if idx <= 0:
        raise PortfolioSlotError("유효한 슬롯 번호가 필요합니다.")
    slot_uid = slot_uid_for_display_idx(idx)
    if not slot_uid:
        raise PortfolioSlotError(f"슬롯 {idx} 정의를 찾을 수 없습니다.")
    spec = slot_spec_for_display_idx(idx, trade_state.get_slots_book()) or {}
    if not spec:
        raise PortfolioSlotError(f"슬롯 {idx} 설정이 없습니다.")
    if trading_mode and not slot_type_matches(
        spec.get("slot_type") or spec.get("slot_personality"),
        trading_mode,
    ):
        raise PortfolioSlotError(
            f"슬롯 {idx}는 {spec.get('slot_type')} 전용입니다 — "
            f"선택 모드({trading_mode})와 맞지 않습니다."
        )
    return slot_uid, dict(spec)


def atomic_lock_slot_for_manual_buy(
    *,
    display_idx: int,
    code: str,
    name: str,
    trading_mode: str,
    pick: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    주문 전 슬롯 Lock — positions_state.json 즉시 기록.
    성공 시 slot_uid·갱신된 pick 반환.
    """
    norm = normalize_code(code)
    if len(norm) != 6:
        raise PortfolioSlotError("종목 코드가 올바르지 않습니다.")

    slot_uid, spec = resolve_forced_slot_uid(
        display_idx,
        trading_mode=trading_mode,
    )
    slots = {k: dict(v) for k, v in trade_state.get_slots_book().items()}
    positions = trade_state.load_persisted_positions()

    if not is_slot_lockable(slots, slot_uid):
        entry = slots.get(slot_uid) or {}
        status = str(entry.get("status") or "empty")
        if status == SLOT_STATUS_LOCKED:
            if is_slot_locked_for_code(slots, slot_uid, norm):
                logger.info("슬롯 %s 이미 동일 종목 Lock — 재사용", slot_uid)
            else:
                raise PortfolioSlotError(
                    f"슬롯 {display_idx}는 다른 종목({entry.get('code')}) 매수 대기 중입니다."
                )
        else:
            raise PortfolioSlotError(f"슬롯 {display_idx}는 비어 있지 않습니다.")

    for uid, entry in slots.items():
        if uid == slot_uid:
            continue
        if str(entry.get("status") or "") == SLOT_STATUS_LOCKED:
            if str(entry.get("code") or "") == norm:
                raise PortfolioSlotError(
                    f"{name}({norm}) — 다른 슬롯에서 이미 매수 Lock 중입니다."
                )

    if norm in positions and int(positions[norm].get("quantity") or 0) > 0:
        raise PortfolioSlotError("이미 보유 중인 종목입니다.")

    lock_meta = {
        "name": str(name or norm),
        "trading_mode": trading_mode,
        "locked_at": _now_text(),
        "source": "manual_buy",
        "ui_slot_index": int(display_idx),
    }
    if pick:
        lock_meta["entry_basis"] = str(pick.get("entry_basis") or "ui_manual_pick")

    if not lock_slot(
        slots,
        slot_uid,
        norm,
        name=str(name or norm),
        lock_meta=lock_meta,
    ):
        raise PortfolioSlotError(f"슬롯 {display_idx} Lock 실패")

    stub = {
        "code": norm,
        "name": str(name or norm),
        "quantity": 0,
        "entry_price": int((pick or {}).get("price") or (pick or {}).get("current_price") or 0),
        "current_price": int((pick or {}).get("price") or (pick or {}).get("current_price") or 0),
        "slot_uid": slot_uid,
        "slot_id": spec.get("slot_type") or trading_mode,
        "slot_type": spec.get("slot_type") or trading_mode,
        "slot_personality": spec.get("slot_personality") or spec.get("slot_type"),
        "display_idx": int(spec.get("display_idx") or display_idx),
        "trading_mode": trading_mode,
        "slot_lock": True,
        "slot_lock_at": lock_meta["locked_at"],
        "updated_at": lock_meta["locked_at"],
    }
    positions[norm] = stub
    trade_state.save_portfolio_state(positions, slots)
    logger.info(
        "슬롯 Lock 기록 — slot=%s(%s) code=%s → positions_state.json",
        display_idx,
        slot_uid,
        norm,
    )

    enriched = dict(pick or {})
    enriched.update(
        {
            "code": norm,
            "name": stub["name"],
            "force_slot_uid": slot_uid,
            "slot_uid": slot_uid,
            "slot_lock_reserved": True,
            "ui_slot_index": int(display_idx),
            "ui_mode_locked": True,
        }
    )
    ui_idx = display_idx_for_slot_uid(slot_uid)
    if ui_idx:
        enriched["ui_slot_index"] = ui_idx

    return {
        "ok": True,
        "slot_uid": slot_uid,
        "display_idx": int(display_idx),
        "code": norm,
        "pick": enriched,
        "positions": positions,
        "slots": slots,
    }


def release_slot_lock_by_uid(slot_uid: str, *, code: str | None = None) -> bool:
    """주문 실패·취소 시 Lock 해제."""
    uid = str(slot_uid or "").strip()
    if not uid:
        return False
    slots = {k: dict(v) for k, v in trade_state.get_slots_book().items()}
    positions = trade_state.load_persisted_positions()
    norm = normalize_code(code) if code else ""
    released = release_slot_lock(slots, uid)
    if norm and norm in positions and positions[norm].get("slot_lock"):
        if int(positions[norm].get("quantity") or 0) <= 0:
            positions.pop(norm, None)
    if released:
        trade_state.save_portfolio_state(positions, slots)
        logger.info("슬롯 Lock 해제 — slot_uid=%s code=%s", uid, norm or "-")
    return released


def flush_portfolio_state_for_ui(*, reason: str = "manual_buy_ui") -> None:
    """st.rerun() 전 — 디스크·메모리·엔진 포지션 스냅샷 일치."""
    trade_state.reload_positions_state_from_disk(bump_revision=True)
    try:
        from scheduler import _apply_runtime_positions_from_store, _sync_positions_state

        _apply_runtime_positions_from_store()
        _sync_positions_state()
    except Exception as exc:
        logger.debug("엔진 포지션 동기화 스킵 (%s): %s", reason, exc)
    logger.info("UI용 portfolio 동기화 완료 — %s", reason)
