"""
증권사 잔고 스냅샷 기준 보유 수량 변화 감지 — 주문 체결 알림 누락 시 백업.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_snapshot: dict[str, dict[str, Any]] = {}
_sell_notified_until: dict[str, float] = {}
_SELL_NOTIFY_SUPPRESS_SEC = 180.0


def _norm_code(raw: object) -> str:
    text = "".join(ch for ch in str(raw or "").strip() if ch.isdigit())
    return text[-6:] if len(text) >= 6 else ""


def holdings_qty_map(holdings: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw_code, row in (holdings or {}).items():
        if not isinstance(row, dict):
            continue
        code = _norm_code(raw_code or row.get("code"))
        if len(code) != 6:
            continue
        qty = int(row.get("quantity") or 0)
        if qty <= 0:
            continue
        out[code] = {
            "qty": qty,
            "name": str(row.get("name") or code),
            "current_price": int(row.get("current_price") or 0),
            "avg_price": int(row.get("avg_price") or row.get("entry_price") or 0),
        }
    return out


def snapshot_copy() -> dict[str, dict[str, Any]]:
    with _lock:
        return {k: dict(v) for k, v in _snapshot.items()}


def update_snapshot_from_holdings(holdings: dict[str, Any] | None) -> None:
    with _lock:
        _snapshot.clear()
        _snapshot.update(holdings_qty_map(holdings))


def mark_sell_notified(code: str, *, ttl_sec: float = _SELL_NOTIFY_SUPPRESS_SEC) -> None:
    """주문 체결 알림 직후 — 동기화 백업 알림 중복 방지."""
    norm = _norm_code(code)
    if len(norm) != 6:
        return
    with _lock:
        _sell_notified_until[norm] = time.time() + max(30.0, float(ttl_sec))


def _sell_notify_suppressed(code: str) -> bool:
    norm = _norm_code(code)
    with _lock:
        until = float(_sell_notified_until.get(norm) or 0.0)
    return time.time() < until


def detect_holdings_changes(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """이전·현재 잔고 맵 비교 → 변화 이벤트 목록."""
    if not previous:
        return []

    events: list[dict[str, Any]] = []
    all_codes = set(previous) | set(current)

    for code in sorted(all_codes):
        old_qty = int((previous.get(code) or {}).get("qty") or 0)
        cur = current.get(code) or {}
        new_qty = int(cur.get("qty") or 0)
        name = str(cur.get("name") or (previous.get(code) or {}).get("name") or code)

        if old_qty > 0 and new_qty <= 0:
            events.append(
                {
                    "kind": "sold_out",
                    "code": code,
                    "name": name,
                    "old_qty": old_qty,
                    "new_qty": 0,
                    "delta_qty": -old_qty,
                }
            )
        elif new_qty < old_qty:
            events.append(
                {
                    "kind": "partial_sell",
                    "code": code,
                    "name": name,
                    "old_qty": old_qty,
                    "new_qty": new_qty,
                    "delta_qty": new_qty - old_qty,
                }
            )
        elif new_qty > old_qty and old_qty >= 0:
            events.append(
                {
                    "kind": "qty_increase",
                    "code": code,
                    "name": name,
                    "old_qty": old_qty,
                    "new_qty": new_qty,
                    "delta_qty": new_qty - old_qty,
                }
            )
        elif old_qty <= 0 and new_qty > 0:
            events.append(
                {
                    "kind": "new_holding",
                    "code": code,
                    "name": name,
                    "old_qty": 0,
                    "new_qty": new_qty,
                    "delta_qty": new_qty,
                }
            )
    return events


def process_holdings_snapshot(
    holdings: dict[str, Any] | None,
    *,
    notify: Callable[[dict[str, Any]], None] | None = None,
    has_pending_sell_order: Callable[[str], bool] | None = None,
    seed_if_empty: bool = True,
) -> list[dict[str, Any]]:
    """
    잔고 holdings dict 처리 — 변화 감지·알림·스냅샷 갱신.
    첫 호출(seed) 시 알림 없이 기준선만 저장.
    """
    current = holdings_qty_map(holdings)
    with _lock:
        previous = {k: dict(v) for k, v in _snapshot.items()}
        first_seed = seed_if_empty and not previous

    if first_seed:
        update_snapshot_from_holdings(holdings)
        return []

    events = detect_holdings_changes(previous, current)
    notified: list[dict[str, Any]] = []

    for ev in events:
        code = str(ev.get("code") or "")
        kind = str(ev.get("kind") or "")

        if kind in ("partial_sell", "sold_out"):
            if _sell_notify_suppressed(code):
                continue
            if has_pending_sell_order and has_pending_sell_order(code):
                continue

        if notify and kind in ("partial_sell", "sold_out", "qty_increase", "new_holding"):
            try:
                notify(ev)
                notified.append(ev)
            except Exception as exc:
                logger.debug("보유 변동 알림 실패 %s: %s", code, exc)

    update_snapshot_from_holdings(holdings)
    return notified
