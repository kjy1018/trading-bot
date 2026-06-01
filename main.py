"""
멀티모드 실시간 매매 봇 대시보드
레이아웃: [지휘관 현황판] → [보유 슬롯 5] → [계좌 · 청산 영수증]
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from html import escape
from typing import Any

import streamlit as st

import config
import selected_modes
import trade_state
from stock_names import (
    enrich_position,
    format_stock_label,
    is_code_only_display,
    is_valid_korean_name,
    lookup_master,
    normalize_code,
    resolve_stock_name,
)

# API·캐시 실패 시 슬롯 즉시 한글 표기 (지휘관 가독성)
_SLOT_NAME_FALLBACK: dict[str, str] = {
    "004800": "효성",
    "068270": "셀트리온",
    "006400": "삼성SDI",
    "066570": "LG전자",
}
from trading_logic import (
    assemble_commander_slots,
    build_commander_dashboard_metrics,
    format_slot_identity_line,
)
from trade_state import (
    ensure_trade_state_file,
    get_daily_signature,
    get_dashboard_refresh_nonce,
    get_positions_revision,
    get_weekly_signature,
)
from scheduler import (
    MAX_SLOTS,
    MONITOR_INTERVAL_SEC,
    SCAN_END_TIME,
    REALTIME_SCAN_INTERVAL_SEC,
    SCAN_START_TIME,
    _total_seed,
    TRAILING_MIN_PEAK_PROFIT_PCT,
    TRAILING_TIER1_DROP_PCT,
    TRAILING_TIER2_DROP_PCT,
    TRAILING_TIER3_DROP_PCT,
    emergency_liquidate_all,
    get_account_ui_snapshot,
    get_daily_stats,
    get_daily_trade_history,
    get_order_status_snapshot,
    get_positions_snapshot,
    build_status_banner_text,
    get_boot_scan_status,
    get_recent_watch_hms,
    get_scan_timing,
    get_scheduler_status,
    get_ui_universe_recommendations,
    get_watch_time_snapshot,
    manual_buy_recommended_pick,
    preview_manual_pick_entry,
    start_background_scheduler,
    update_position_trading_mode,
)

UI_TICK_MS = int(getattr(config, "UI_REFRESH_INTERVAL_SEC", 5) * 1000)
COMMANDER_PNL_REFRESH_SEC = int(
    getattr(config, "COMMANDER_PNL_REFRESH_SEC", getattr(config, "UI_REFRESH_INTERVAL_SEC", 5))
)
ACCOUNT_REFRESH_SEC = int(getattr(config, "ACCOUNT_REFRESH_SEC", 60))
MAX_SLOTS_DISPLAY = getattr(config, "MAX_SIMULTANEOUS_STOCKS", MAX_SLOTS)
CONTROL_SLOT_POOL_TTL_SEC = 45.0
UI_SLOT_CACHE_GRACE_SEC = 15.0

_MODE_LABELS = {
    "scanning": "실시간 유니버스 탐색 중",
    "realtime_watch": "장중 실시간 유니버스 감시",
    "scalp_watch": "단타 초단위 감시 + 스윙/장투 보유",
    "monitoring": "보유 슬롯 실시간 감시 (웹소켓 체결가 + REST 보조)",
    "swing_active": "스윙·장투 보유 감시",
    "off_hours": "장외 · 보유 유지",
}
_SLOT_MODE_OPTIONS = ["단타", "스윙", "장투"]
_SLOT_MODE_TO_VALUE = {
    "단타": "scalping",
    "스윙": "swing",
    "장투": "long_term",
}
_SLOT_MODE_FROM_VALUE = {value: label for label, value in _SLOT_MODE_TO_VALUE.items()}
_DEFAULT_SLOT_MODE_LABEL = "장투"

# DEBUG: Prior missed trades were caused by synchronous quote/order calls leaking
# into render paths. This UI now reads scheduler snapshots only and leaves live
# broker traffic to background workers.
st.set_page_config(page_title="실시간 멀티모드 매매 봇", page_icon="📈", layout="wide")

start_background_scheduler()
trade_state.ensure_trade_state_file()


def _clear_commander_metrics_cache() -> None:
    for key in (
        "commander_metrics",
        "commander_metrics_sig",
        "commander_week_sig",
    ):
        st.session_state.pop(key, None)


def _clear_positions_session_cache() -> None:
    for key in (
        "positions_display",
        "positions_last_ok_at",
        "positions_cache_stale",
    ):
        st.session_state.pop(key, None)


def _mirror_runtime_state_to_session(*, force: bool = False) -> bool:
    """스케줄러 메모리 스냅샷 → st.session_state (잔고·슬롯 즉시 UI 반영)."""
    revision = int(get_positions_revision())
    account = get_account_ui_snapshot()
    acct_ts = str(account.get("updated_at") or "")
    prev_rev = st.session_state.get("runtime_state_revision")
    prev_acct = st.session_state.get("runtime_account_updated_at")
    if (
        not force
        and prev_rev == revision
        and prev_acct == acct_ts
        and st.session_state.get("runtime_positions") is not None
    ):
        return False

    positions = get_positions_snapshot()
    st.session_state["runtime_state_revision"] = revision
    st.session_state["runtime_account"] = dict(account)
    st.session_state["runtime_account_updated_at"] = acct_ts
    st.session_state["runtime_positions"] = [dict(p) for p in positions if isinstance(p, dict)]
    return True


def _session_account_snapshot() -> dict[str, Any]:
    snap = st.session_state.get("runtime_account")
    return dict(snap) if isinstance(snap, dict) else get_account_ui_snapshot()


def _session_positions_snapshot() -> list[dict[str, Any]]:
    rows = st.session_state.get("runtime_positions")
    if isinstance(rows, list):
        return [dict(p) for p in rows if isinstance(p, dict)]
    return [dict(p) for p in get_positions_snapshot()]


def _ui_scheduler_signature() -> tuple[Any, ...]:
    """스케줄러·계좌 스냅샷 변경 감지 — 세션 캐시 무효화용."""
    _mirror_runtime_state_to_session()
    status = get_scheduler_status()
    positions = _session_positions_snapshot()
    account = _session_account_snapshot()
    pos_codes = tuple(
        sorted(
            code
            for code in (
                normalize_code(p.get("code")) for p in positions if isinstance(p, dict)
            )
            if len(code) == 6
        )
    )
    holdings = account.get("holdings") or {}
    acct_codes: tuple[str, ...] = ()
    if isinstance(holdings, dict):
        acct_codes = tuple(sorted(str(k) for k in holdings.keys() if str(k).isdigit()))
    return (
        int(status.get("slot_count", 0)),
        pos_codes,
        str(account.get("updated_at") or ""),
        acct_codes,
        int(account.get("stock_eval") or account.get("total_eval") or 0),
        int(account.get("cash") or 0),
        int(get_positions_revision()),
        int(get_dashboard_refresh_nonce()),
    )


def _sync_ui_snapshots_from_scheduler(*, trigger_rerun: bool = False) -> bool:
    """account/positions 스냅샷 시그니처 변경 시 UI 세션 캐시를 버리고 필요하면 rerun."""
    mirrored = _mirror_runtime_state_to_session()
    if mirrored:
        _clear_positions_session_cache()
        _clear_commander_metrics_cache()
    sig = _ui_scheduler_signature()
    prev = st.session_state.get("ui_scheduler_sig")
    if prev == sig:
        return False
    st.session_state["ui_scheduler_sig"] = sig
    _clear_positions_session_cache()
    _clear_commander_metrics_cache()
    st.session_state.pop("account_cache", None)
    st.session_state.pop("account_cache_stale", None)
    if trigger_rerun and prev is not None:
        st.rerun()
    return True


def _apply_boot_scan_result(result: dict[str, Any]) -> None:
    _clear_commander_metrics_cache()
    ts = (
        result.get("watch_hms")
        or result.get("completed_at")
        or datetime.now().strftime("%H:%M:%S")
    )
    full = result.get("watch_full") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _push_watch_to_ui_session(ts, full, epoch=float(result.get("watch_epoch", 0.0)))
    summary = result.get("summary") or result.get("message") or ""
    if summary:
        st.session_state["last_force_scan_summary"] = summary
    st.session_state["immediate_boot_scan_done"] = True
    if result.get("success"):
        st.session_state["boot_scan_toast"] = f"✅ 기동 스캔 완료 ({ts})"
    else:
        st.session_state["boot_scan_toast"] = (
            f"⚠️ 기동 스캔 일부 실패 ({ts}): {result.get('error', summary)}"
        )


def _sync_boot_scan_to_session() -> None:
    """백그라운드 기동·AI 상태만 폴링(UI 블로킹 없음)."""
    st.session_state.setdefault("ui_bootstrapped", True)
    if st.session_state.get("immediate_boot_scan_done"):
        return

    sched = get_scheduler_status()
    narr = str(sched.get("ai_daily_narrative") or "")
    if narr and "대기" not in narr:
        _clear_commander_metrics_cache()

    boot = get_boot_scan_status()
    status = boot.get("status", "idle")
    if status == "running":
        st.session_state.setdefault(
            "boot_scan_toast",
            "🔄 백그라운드: 100억 유니버스 · AI 분석 진행 중…",
        )
        return
    if status in ("done", "error") and boot.get("result"):
        _apply_boot_scan_result(boot["result"])
        return
    if narr and "대기" not in narr:
        st.session_state["immediate_boot_scan_done"] = True
def _read_account_panel_values() -> tuple[int, int, int, bool, str | None]:
    """st.session_state.runtime_account 우선 — 스케줄러 잔고 즉시 반영."""
    _mirror_runtime_state_to_session()
    snap = _session_account_snapshot()
    stock_eval = int(snap.get("stock_eval") or snap.get("total_eval") or 0)
    cash = int(snap.get("cash") or 0)
    account_total = int(snap.get("account_total_eval") or (stock_eval + cash))
    stale = bool(snap.get("stale", False))
    updated_at = snap.get("updated_at")
    return stock_eval, cash, account_total, stale, (
        str(updated_at) if updated_at else None
    )


def _enrich_slot_position(pos: dict[str, Any], token: str | None) -> dict[str, Any]:
    """슬롯 렌더용 — 한글명·display_name 강제 보강."""
    out = enrich_position(pos, access_token=token, allow_api=False)
    code = normalize_code(out.get("code"))
    if len(code) != 6:
        return out
    fb = _SLOT_NAME_FALLBACK.get(code) or lookup_master(code)
    if fb and is_code_only_display(out.get("name"), code):
        out["name"] = fb
    if fb and is_code_only_display(out.get("display_name"), code):
        out["display_name"] = f"{fb} ({code})"
    elif is_code_only_display(out.get("display_name"), code):
        out["display_name"] = format_stock_label(code, out.get("name"))
    return out


def _positions_cache_snapshot() -> list[dict[str, Any]]:
    cached = st.session_state.get("positions_display") or []
    if not isinstance(cached, list):
        return []
    return [dict(p) for p in cached if isinstance(p, dict)]


def _store_stable_positions(
    positions: list[dict[str, Any]],
    *,
    slot_count: int | None = None,
) -> None:
    if not positions:
        if slot_count is not None and slot_count <= 0:
            st.session_state["positions_display"] = []
            st.session_state["positions_last_ok_at"] = time.time()
        return
    st.session_state["positions_display"] = [dict(p) for p in positions]
    st.session_state["positions_last_ok_at"] = time.time()


def _positions_for_display() -> list[dict[str, Any]]:
    _sync_ui_snapshots_from_scheduler(trigger_rerun=False)
    _mirror_runtime_state_to_session()
    snap = _session_positions_snapshot()
    token = st.session_state.get("access_token")
    status = get_scheduler_status()
    slot_count = int(status.get("slot_count", 0))
    if snap:
        enriched = [_enrich_slot_position(p, token) for p in snap]
        _store_stable_positions(enriched, slot_count=slot_count)
        st.session_state["positions_cache_stale"] = False
        return enriched
    if slot_count <= 0:
        _store_stable_positions([], slot_count=0)
        st.session_state["positions_cache_stale"] = False
        return []
    cached = _positions_cache_snapshot()
    last_ok = float(st.session_state.get("positions_last_ok_at", 0.0) or 0.0)
    grace_sec = min(3.0, UI_SLOT_CACHE_GRACE_SEC)
    within_grace = cached and (time.time() - last_ok <= grace_sec)
    if within_grace and len(cached) == slot_count:
        st.session_state["positions_cache_stale"] = True
        return [_enrich_slot_position(p, token) for p in cached]
    if cached and len(cached) != slot_count:
        _clear_positions_session_cache()
    st.session_state["positions_cache_stale"] = False
    return []


def _slot_controls_state() -> dict[int, dict[str, Any]]:
    raw = st.session_state.setdefault("slot_controls", {})
    return raw if isinstance(raw, dict) else {}


def _hydrate_trading_modes_from_disk() -> dict[str, str]:
    """F5·재기동 — selected_modes.json 을 session_state 보다 먼저 복원."""
    disk = selected_modes.load_modes()
    st.session_state["trading_modes"] = dict(disk)
    st.session_state["_trading_modes_disk_loaded"] = True
    return st.session_state["trading_modes"]


def _trading_modes_book() -> dict[str, str]:
    """종목코드·빈슬롯 키 → 한글 모드 라벨 (메모리 + selected_modes.json)."""
    if not st.session_state.get("_trading_modes_disk_loaded"):
        return _hydrate_trading_modes_from_disk()
    raw = st.session_state.get("trading_modes")
    if not isinstance(raw, dict):
        return _hydrate_trading_modes_from_disk()
    return raw


def _mode_label_from_value(raw_mode: object) -> str:
    mode = str(raw_mode or "").strip().lower()
    return _SLOT_MODE_FROM_VALUE.get(mode, _DEFAULT_SLOT_MODE_LABEL)


def _mode_widget_key_for_slot(idx: int) -> str:
    """슬롯 번호 고정 위젯 키 — 종목이 바뀌어도 위젯·설정이 슬롯에 묶인다."""
    return f"slot_mode_widget_{selected_modes.slot_storage_key(idx)}"


def _resolve_mode_label_for_slot(
    idx: int,
    *,
    code: str | None = None,
    current_mode_value: str | None = None,
) -> str:
    """슬롯(_slot_N) > 종목코드 > 포지션 백엔드 > 기본(장투)."""
    label = selected_modes.resolve_mode_label_for_slot(
        idx, code=code, fallback_value=current_mode_value
    )
    if label not in _SLOT_MODE_OPTIONS:
        label = _DEFAULT_SLOT_MODE_LABEL
    book = _trading_modes_book()
    book[selected_modes.slot_storage_key(idx)] = label
    norm_code = normalize_code(code) if code else ""
    if len(norm_code) == 6:
        book[norm_code] = label
    return label


def _persist_slot_mode(idx: int, label: str, code: str | None = None) -> None:
    if label not in _SLOT_MODE_OPTIONS:
        label = _DEFAULT_SLOT_MODE_LABEL
    book = _trading_modes_book()
    book[selected_modes.slot_storage_key(idx)] = label
    norm_code = normalize_code(code) if code else ""
    selected_modes.sync_slot_mode(idx, label, norm_code or None)
    if len(norm_code) == 6:
        book[norm_code] = label


def _commit_trading_mode_change(idx: int, code: str) -> None:
    """selectbox on_change — 슬롯·종목코드·보유 포지션 백엔드 동기화."""
    widget_key = _mode_widget_key_for_slot(idx)
    label = str(st.session_state.get(widget_key) or _DEFAULT_SLOT_MODE_LABEL)
    if label not in _SLOT_MODE_OPTIONS:
        label = _DEFAULT_SLOT_MODE_LABEL
        st.session_state[widget_key] = label
    norm_code = normalize_code(code)
    _persist_slot_mode(idx, label, norm_code or None)
    if len(norm_code) != 6:
        return
    held_codes = {
        normalize_code(p.get("code"))
        for p in get_positions_snapshot()
        if normalize_code(p.get("code"))
    }
    if norm_code not in held_codes:
        return
    result = update_position_trading_mode(norm_code, label)
    if result.get("success"):
        st.session_state["slot_action_notice"] = (
            f"✅ 슬롯 {idx} {result.get('message', '')}"
        )
        _clear_commander_metrics_cache()
    else:
        st.session_state["slot_action_notice"] = (
            f"⚠️ 슬롯 {idx} 모드 변경 실패: {result.get('message', '처리 실패')}"
        )


def _prepare_mode_selectbox_state(
    idx: int,
    *,
    code: str | None = None,
    current_mode_value: str | None = None,
) -> str:
    """F5 직후 — 슬롯 우선 디스크 장부로 selectbox 값 고정."""
    label = _resolve_mode_label_for_slot(
        idx, code=code, current_mode_value=current_mode_value
    )
    widget_key = _mode_widget_key_for_slot(idx)
    st.session_state[widget_key] = label
    return widget_key


def _selected_slot_mode_label(idx: int, ticker: str | None = None) -> str:
    code = normalize_code(ticker) if ticker else ""
    return _resolve_mode_label_for_slot(idx, code=code or None)


def _selected_slot_mode_value(idx: int, ticker: str | None = None) -> str:
    code = normalize_code(ticker) if ticker else ""
    return selected_modes.resolve_mode_value_for_slot(idx, code=code or None)


def _render_slot_mode_selector(
    idx: int,
    *,
    ticker: str | None = None,
    current_mode: str | None = None,
) -> str:
    code = normalize_code(ticker) if ticker else ""
    widget_key = _prepare_mode_selectbox_state(
        idx, code=code or None, current_mode_value=current_mode
    )
    st.caption("매매 모드")
    selected = st.selectbox(
        f"슬롯 {idx} 매매 모드",
        options=_SLOT_MODE_OPTIONS,
        key=widget_key,
        label_visibility="collapsed",
        on_change=_commit_trading_mode_change,
        args=(idx, code),
    )
    label = str(selected)
    _persist_slot_mode(idx, label, code or None)
    return label


def _candidate_with_selected_mode(idx: int, candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not candidate:
        return None
    code = normalize_code(candidate.get("code"))
    out = dict(candidate)
    mode_value = _selected_slot_mode_value(idx, code or None)
    out["selected_trading_mode"] = mode_value
    out["trading_mode"] = mode_value
    out["mode_label"] = _selected_slot_mode_label(idx, code or None)
    out["ui_slot_index"] = idx
    out["ui_mode_locked"] = True
    return out


def _get_slot_recommendation_pool() -> list[dict[str, Any]]:
    token = st.session_state.get("access_token")
    cache = st.session_state.setdefault(
        "slot_recommend_pool_cache",
        {"ts": 0.0, "items": []},
    )
    now = time.time()
    if (
        cache.get("items")
        and now - float(cache.get("ts", 0.0)) < CONTROL_SLOT_POOL_TTL_SEC
    ):
        return [_enrich_slot_position(item, token) for item in cache["items"]]

    try:
        items = get_ui_universe_recommendations(limit=60)
    except Exception:
        items = list(cache.get("items") or [])
    enriched = [_enrich_slot_position(item, token) for item in items]
    cache["ts"] = now
    cache["items"] = enriched
    return enriched


def _assign_next_slot_recommendation(
    slot_idx: int,
    positions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    controls = _slot_controls_state()
    pool = _get_slot_recommendation_pool()
    if not pool:
        controls[slot_idx] = {}
        return None

    held_codes = {
        normalize_code(p.get("code")) for p in positions[:MAX_SLOTS_DISPLAY]
    }
    used_codes = held_codes | {
        normalize_code(v.get("code"))
        for key, v in controls.items()
        if key != slot_idx and isinstance(v, dict)
    }

    cursor = int(st.session_state.get("slot_recommend_cursor", 0))
    total = len(pool)
    for offset in range(total):
        pick_idx = (cursor + offset) % total
        candidate = _enrich_slot_position(pool[pick_idx], st.session_state.get("access_token"))
        code = normalize_code(candidate.get("code"))
        if len(code) != 6 or code in used_codes:
            continue
        controls[slot_idx] = candidate
        st.session_state["slot_recommend_cursor"] = (pick_idx + 1) % total
        new_code = normalize_code(candidate.get("code"))
        slot_key = f"_slot_{slot_idx}"
        book = _trading_modes_book()
        if len(new_code) == 6 and new_code not in book and slot_key in book:
            book[new_code] = book[slot_key]
            selected_modes.save_mode(new_code, book[new_code])
        return candidate

    controls[slot_idx] = {}
    return None


def _sync_slot_controls(positions: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    controls = _slot_controls_state()
    held_count = min(len(positions), MAX_SLOTS_DISPLAY)
    held_codes = {
        normalize_code(p.get("code")) for p in positions[:MAX_SLOTS_DISPLAY]
    }

    for idx in list(controls):
        if idx <= held_count or idx > MAX_SLOTS_DISPLAY:
            controls.pop(idx, None)

    for idx in range(held_count + 1, MAX_SLOTS_DISPLAY + 1):
        current = controls.get(idx) if isinstance(controls.get(idx), dict) else {}
        code = normalize_code(current.get("code"))
        duplicated = any(
            normalize_code(other.get("code")) == code
            for key, other in controls.items()
            if key != idx and isinstance(other, dict)
        )
        if len(code) != 6 or code in held_codes or duplicated:
            _assign_next_slot_recommendation(idx, positions)
    return controls


def _is_valid_slot_candidate(candidate: dict[str, Any] | None) -> bool:
    if not isinstance(candidate, dict):
        return False
    code = normalize_code(candidate.get("code"))
    return len(code) == 6 and code.isdigit()


def _safe_slot_candidate(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not _is_valid_slot_candidate(candidate):
        return None
    return dict(candidate)


def _build_live_balance_snapshot(positions: list[dict[str, Any]]) -> dict[str, Any]:
    total_cost = 0
    total_eval = 0
    summary_parts: list[str] = []
    for pos in positions[:MAX_SLOTS_DISPLAY]:
        qty = int(pos.get("quantity") or 0)
        entry = int(pos.get("entry_price") or 0)
        current = int(pos.get("current_price") or entry or 0)
        total_cost += entry * qty
        total_eval += current * qty
        if qty > 0:
            summary_parts.append(f"{_slot_title_label(pos)} {qty}주")

    pnl = total_eval - total_cost
    pct = pnl / total_cost * 100.0 if total_cost > 0 else 0.0
    if pnl > 0:
        theme = "plus"
    elif pnl < 0:
        theme = "minus"
    else:
        theme = "flat"
    return {
        "total_cost": total_cost,
        "total_eval": total_eval,
        "total_pnl": pnl,
        "total_pct": pct,
        "theme": theme,
        "summary": " · ".join(summary_parts) if summary_parts else "보유 종목 없음",
    }


def _warm_ui_session_caches() -> None:
    """F5 후에도 슬롯·계좌 캐시가 비지 않도록 디스크/스케줄러에서 복구."""
    _hydrate_trading_modes_from_disk()
    snap = get_positions_snapshot()
    token = st.session_state.get("access_token")
    book = _trading_modes_book()
    for pos in snap:
        code = normalize_code(pos.get("code"))
        if len(code) != 6:
            continue
        if code not in book:
            book[code] = selected_modes.get_mode_label(
                code, fallback_value=str(pos.get("trading_mode") or "")
            )
    selected_modes.save_modes(book)
    if snap:
        status = get_scheduler_status()
        _store_stable_positions(
            [_enrich_slot_position(p, token) for p in snap],
            slot_count=int(status.get("slot_count", len(snap))),
        )
    trade_state.ensure_trade_state_file()
    _sync_ui_snapshots_from_scheduler(trigger_rerun=False)


def _sync_force_scan_to_session() -> None:
    """긴급 우회 — F5 강제 탐색 폴링 비활성."""
    return


def _handle_browser_refresh_force_scan() -> None:
    """긴급 우회 — UI 블로킹 없이 스킵(실시간 엔진이 탐색 담당)."""
    return


def _format_countdown(seconds: int) -> str:
    mm, ss = divmod(max(0, seconds), 60)
    return f"{mm:02d}분 {ss:02d}초"


def _parse_to_hms(raw: object) -> str | None:
    """세션·스케줄러 값 → HH:MM:SS."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text == "대기 중":
        return None
    if len(text) >= 19 and text[10] == " ":
        try:
            return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").strftime(
                "%H:%M:%S"
            )
        except ValueError:
            return None
    if len(text) == 8 and text.count(":") == 2:
        try:
            datetime.strptime(text, "%H:%M:%S")
            return text
        except ValueError:
            return None
    return None


def _push_watch_to_ui_session(
    hms: str,
    full: str | None = None,
    *,
    epoch: float = 0.0,
) -> None:
    """탐색 완료 직후 배너·세션에 감시 시각 즉시 반영."""
    parsed = _parse_to_hms(hms)
    if not parsed:
        return
    st.session_state["banner_recent_watch"] = parsed
    st.session_state["last_scan_time"] = parsed
    st.session_state["last_force_scan_at"] = parsed
    if full:
        st.session_state["last_scan_time_full"] = full
        st.session_state["last_force_scan_full"] = full
    if epoch > 0:
        st.session_state["watch_epoch"] = epoch


def _sync_watch_time_from_scheduler() -> None:
    """백그라운드 탐색 완료 시 스케줄러 → Streamlit 세션 동기화."""
    snap = get_watch_time_snapshot()
    hms = _parse_to_hms(snap.get("hms")) or _parse_to_hms(snap.get("full"))
    if not hms:
        return
    epoch = float(snap.get("epoch") or 0.0)
    prev_epoch = float(st.session_state.get("watch_epoch", 0.0))
    if epoch >= prev_epoch:
        _push_watch_to_ui_session(
            hms, str(snap.get("full") or ""), epoch=epoch
        )


def _resolve_recent_watch_hms() -> str:
    """
    최근 감시 시각 — 세션 저장값(강제·완료 탐색) 1순위, 스케줄러 2순위.
    """
    _sync_watch_time_from_scheduler()

    for key in (
        "banner_recent_watch",
        "last_scan_time",
        "last_force_scan_at",
    ):
        hms = _parse_to_hms(st.session_state.get(key))
        if hms:
            return hms

    sched = get_recent_watch_hms()
    if sched and sched != "대기 중":
        _push_watch_to_ui_session(
            sched,
            get_watch_time_snapshot().get("full"),
            epoch=float(st.session_state.get("watch_epoch", 0)),
        )
        return sched

    return "대기 중"


def _render_top_status_banner() -> None:
    """상단 초록 배너 — 세션·스케줄러에서 최신 감시 시각을 매번 재조회."""
    _sync_watch_time_from_scheduler()
    watch = _resolve_recent_watch_hms()

    if watch == "대기 중":
        fallback = _parse_to_hms(st.session_state.get("banner_recent_watch"))
        if fallback:
            watch = fallback

    if watch != "대기 중":
        _push_watch_to_ui_session(watch)

    banner = build_status_banner_text(watch)
    if watch != "대기 중":
        banner = banner.replace(
            f"[최근 감시: {watch}]",
            f'[최근 감시: <span class="watch-ts">{watch}</span>]',
        )
    st.markdown(
        f'<div class="auto-trade-banner">{banner}</div>',
        unsafe_allow_html=True,
    )


def _render_order_status_panel() -> None:
    rows = get_order_status_snapshot()
    if not rows:
        return
    latest = rows[:4]
    parts: list[str] = []
    for row in latest:
        prefix = f"[{row.get('mode_label')}] " if row.get("mode_label") else ""
        parts.append(
            f"{prefix}{str(row.get('name') or row.get('code') or '')} · "
            f"{str(row.get('status') or '')} · "
            f"{str(row.get('message') or '')}"
        )
    line = " | ".join(parts)
    if line:
        st.caption(f"주문 큐: {line}")


st.markdown(
    """
    <style>
    .auto-trade-banner {
        padding: 0.55rem 1rem; border-radius: 0.5rem;
        background: linear-gradient(90deg, #1b5e20, #2e7d32);
        color: #fff; font-weight: 600; text-align: center;
        font-size: 0.98rem; line-height: 1.45;
    }
    .auto-trade-banner .watch-ts {
        color: #fff9c4; font-weight: 800; font-size: 1.05rem;
        letter-spacing: 0.04em; text-shadow: 0 0 6px rgba(0,0,0,0.25);
    }
    .countdown-box {
        font-size: 1.05rem; font-weight: 700; padding: 0.45rem 0.75rem;
        border-radius: 8px; background: #e8f5e9; border: 1px solid #81c784;
        margin: 0.35rem 0;
    }
    .slot-card-filled {
        border: 1px solid #e0e0e0; border-radius: 10px; padding: 0.65rem 0.5rem;
        background: #fff; min-height: 9.5rem; min-width: 0; overflow: hidden;
    }
    .slot-card-empty {
        border: 2px dashed #bdbdbd; border-radius: 10px; padding: 0.85rem 0.5rem;
        background: #fafafa; min-height: 9.5rem; text-align: center; color: #757575;
        min-width: 0; overflow: hidden;
    }
    .slot-card-empty.slot-card-recommend {
        border-style: solid;
        border-color: #90caf9;
        background: linear-gradient(180deg, #f8fbff 0%, #eef6ff 100%);
        color: #0d47a1;
    }
    .slot-card-empty .slot-recommend-tag {
        display: inline-block;
        margin-top: 0.35rem;
        padding: 0.15rem 0.55rem;
        border-radius: 999px;
        background: #e3f2fd;
        color: #1565c0;
        font-size: 0.75rem;
        font-weight: 700;
    }
    .profit-rate-banner { font-size: 1.3rem; font-weight: 700; padding: 0.35rem;
        border-radius: 8px; text-align: center; }
    .profit-rate-banner.plus { color: #c62828; background: #ffebee; border: 1px solid #ef5350; }
    .profit-rate-banner.minus { color: #1565c0; background: #e3f2fd; border: 1px solid #42a5f5; }
    .profit-rate-banner.flat { color: #616161; background: #f5f5f5; border: 1px solid #bdbdbd; }
    div[data-testid="stButton"] button[data-testid="baseButton-emergency_liquidate"] {
        background-color: #b71c1c !important; color: #fff !important; font-weight: 700;
    }
    .mode-badge {
        display: inline-block; padding: 0.15rem 0.55rem; border-radius: 999px;
        font-size: 0.78rem; font-weight: 700; color: #fff; margin-right: 0.35rem;
        vertical-align: middle; letter-spacing: 0.02em;
    }
    .mode-badge.mode-scalping {
        background: linear-gradient(135deg, #e65100, #ff9800);
        box-shadow: 0 1px 4px rgba(230,81,0,0.35);
    }
    .mode-badge.mode-swing {
        background: linear-gradient(135deg, #1b5e20, #43a047);
        box-shadow: 0 1px 4px rgba(27,94,32,0.35);
    }
    .mode-badge.mode-longterm {
        background: linear-gradient(135deg, #0d47a1, #42a5f5);
        box-shadow: 0 1px 4px rgba(13,71,161,0.35);
    }
    .mode-hint {
        font-size: 0.72rem; color: #616161; margin-left: 0.15rem;
    }
    .target-exit-line {
        color: #f9a825; font-weight: 700; font-size: 0.88rem;
        line-height: 1.35; margin: 0.2rem 0 0.35rem 0;
        padding: 0.25rem 0.4rem; border-radius: 6px;
        background: linear-gradient(90deg, #fffde7, #fff8e1);
        border: 1px solid #ffe082;
    }
    .target-exit-line .target-remain {
        color: #f57f17; font-weight: 600; font-size: 0.8rem;
    }
    .commander-pnl-board {
        padding: 0.85rem 1.1rem; margin: 0.5rem 0 0.65rem 0;
        border-radius: 12px;
        background: linear-gradient(135deg, #0d1b2a 0%, #1b263b 50%, #415a77 100%);
        border: 1px solid #778da9;
        box-shadow: 0 4px 14px rgba(13,27,42,0.25);
        color: #e0e1dd; text-align: center; line-height: 1.55;
    }
    .commander-pnl-board .pnl-label { font-size: 0.92rem; opacity: 0.92; }
    .commander-pnl-board .pnl-value {
        font-size: 1.35rem; font-weight: 800; letter-spacing: 0.02em;
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .commander-pnl-board .pnl-plus { color: #ff8a80; }
    .commander-pnl-board .pnl-minus { color: #82b1ff; }
    .commander-pnl-board .pnl-flat { color: #cfd8dc; }
    .commander-pnl-board .pnl-expected { color: #ffd54f; }
    .commander-pnl-board .pnl-week { color: #ce93d8; }
    .commander-pnl-board .pnl-ai { color: #80cbc4; }
    .commander-pnl-board .pnl-ai-week { color: #b39ddb; }
    .commander-pnl-board .pnl-divider {
        margin: 0 0.65rem; opacity: 0.55; font-weight: 300;
    }
    .slot-identity-line {
        margin: 0.15rem 0 0.45rem 0; font-size: 0.9rem; font-weight: 600;
        line-height: 1.4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .slot-identity-line .slot-identity-sep {
        color: #9e9e9e; font-weight: 400; margin: 0 0.2rem;
    }
    .slot-identity-line b { color: #f57f17; }
    .slot-trail-tag { font-size: 0.78rem; color: #5c6bc0; font-weight: 600; }
    div[data-testid="stSelectbox"] {
        min-width: 0;
    }
    div[data-testid="stSelectbox"] > div {
        min-width: 0;
    }
    div[data-testid="stSelectbox"] [data-baseweb="select"] {
        min-height: 2.1rem;
    }
    .hero-balance-board {
        margin: 0.15rem 0 0.8rem 0;
        padding: 0.9rem 1rem 1rem 1rem;
        border-radius: 16px;
        border: 1px solid rgba(255,255,255,0.12);
        box-shadow: 0 10px 26px rgba(15, 23, 42, 0.18);
        color: #fff;
    }
    .hero-balance-board.plus {
        background: linear-gradient(135deg, #7f1d1d 0%, #b91c1c 52%, #ef4444 100%);
    }
    .hero-balance-board.minus {
        background: linear-gradient(135deg, #0f172a 0%, #1d4ed8 52%, #60a5fa 100%);
    }
    .hero-balance-board.flat {
        background: linear-gradient(135deg, #374151 0%, #4b5563 52%, #6b7280 100%);
    }
    .hero-balance-head {
        font-size: 0.92rem;
        font-weight: 700;
        opacity: 0.92;
        margin-bottom: 0.7rem;
    }
    .hero-balance-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 0.85rem;
    }
    .hero-balance-card {
        background: rgba(255,255,255,0.12);
        border: 1px solid rgba(255,255,255,0.14);
        border-radius: 14px;
        padding: 0.85rem 0.95rem;
        min-width: 0;
        overflow: hidden;
    }
    .hero-balance-card-label {
        font-size: 0.82rem;
        opacity: 0.88;
        margin-bottom: 0.35rem;
    }
    .hero-balance-card-value {
        font-size: 1.75rem;
        font-weight: 800;
        line-height: 1.15;
        letter-spacing: 0.01em;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .hero-balance-foot {
        margin-top: 0.7rem;
        font-size: 0.82rem;
        opacity: 0.92;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    div[data-testid="stMetricValue"] > div {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _slot_rows_layout(n: int) -> list[int]:
    if n <= 4:
        return [n]
    if n == 5:
        return [3, 2]
    rows, rem = [], n
    while rem > 0:
        if rem <= 4:
            rows.append(rem)
            break
        rows.append(3)
        rem -= 3
    return rows


def _profit_pct_display(pct: float) -> str:
    if pct > 0:
        return f"▲{pct:.2f}%"
    if pct < 0:
        return f"▼{abs(pct):.2f}%"
    return f"—{pct:.2f}%"


def _pnl_css_class(pct: float) -> str:
    if pct > 0:
        return "pnl-plus"
    if pct < 0:
        return "pnl-minus"
    return "pnl-flat"


def _format_signed_pct(pct: float) -> str:
    return f"{pct:+.2f}%"


def _count_held_slots(positions: list[dict[str, Any]]) -> int:
    n = 0
    for p in positions[:MAX_SLOTS_DISPLAY]:
        if len(normalize_code(p.get("code"))) == 6:
            n += 1
    return n


def _assemble_commander_slots_for_metrics(
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    상단 PnL·AI 가이드용 슬롯 로스터.

    보유 5슬롯이면 추천 풀 동기화(_sync_slot_controls)를 호출하지 않으며,
    자동 추천 후보가 0.001%도 섞이지 않음.
    """
    held_n = _count_held_slots(positions)
    if held_n >= MAX_SLOTS_DISPLAY:
        return assemble_commander_slots(
            positions,
            slot_controls=None,
            max_slots=MAX_SLOTS_DISPLAY,
            include_control_candidates=False,
        )
    return assemble_commander_slots(
        positions,
        slot_controls=_slot_controls_state(),
        max_slots=MAX_SLOTS_DISPLAY,
        include_control_candidates=True,
    )


def _get_commander_metrics_cached(
    positions: list[dict[str, Any]],
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """fragment 주기마다 스냅샷·슬롯 시세 반영 (가벼운 캐시, 장중 멈춤 방지)."""
    ensure_trade_state_file()
    commander_slots = _assemble_commander_slots_for_metrics(positions)
    pos_sig = tuple(
        (
            p.get("code"),
            int(p.get("current_price") or 0),
            int(p.get("target_price") or 0),
            int(p.get("target_ceiling_price") or 0),
            int(p.get("quantity") or 0),
        )
        for p in positions
    )
    slot_sig = tuple(
        (
            s.get("slot_idx"),
            s.get("code"),
            s.get("trading_mode"),
            int(s.get("current_price") or s.get("price") or 0),
            round(float(s.get("change_rate") or 0), 2),
        )
        for s in commander_slots
    )
    daily_sig = get_daily_signature()
    week_sig = get_weekly_signature()
    acct_updated = str((get_account_ui_snapshot() or {}).get("updated_at") or "")
    cache_key = (pos_sig, slot_sig, daily_sig, week_sig, acct_updated)
    if (
        not force_refresh
        and st.session_state.get("commander_metrics_sig") == cache_key
        and st.session_state.get("commander_metrics")
    ):
        return dict(st.session_state.commander_metrics)

    token = st.session_state.get("access_token")
    metrics = build_commander_dashboard_metrics(
        positions,
        commander_slots=commander_slots,
        access_token=token,
    )
    metrics["commander_slot_count"] = len(commander_slots)
    metrics["commander_updated_hms"] = datetime.now().strftime("%H:%M:%S")
    st.session_state.commander_metrics_sig = cache_key
    st.session_state.commander_metrics = metrics
    return metrics


def _format_week_start_label(iso_date: str) -> str:
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
        return f"{d.month}/{d.day}(월)~"
    except ValueError:
        return iso_date


def _format_pct_range(low: float, high: float) -> str:
    return f"{low:+.1f}~{high:+.1f}%"


def _render_commander_pnl_board(positions: list[dict[str, Any]]) -> None:
    """지휘관 자금 총괄 — 오늘 통합 · 오늘 AI 예측 · 이번 주 통합."""
    m = _get_commander_metrics_cached(positions)

    today_pct = float(m.get("today_return_pct", 0))
    today_realized = int(m.get("today_realized_pnl", 0))
    today_unrealized = int(m.get("today_unrealized_pnl", 0))
    today_combined = int(m.get("today_combined_pnl", 0))

    ai_day_low = float(m.get("ai_daily_pct_low", 0))
    ai_day_high = float(m.get("ai_daily_pct_high", 0))

    week_pct = float(m.get("week_return_pct", 0))
    week_realized = int(m.get("week_realized_pnl", 0))
    week_start = str(m.get("week_start", ""))

    scalp_w = float(m.get("ai_scalp_weight", 0.35)) * 100
    swing_w = float(m.get("ai_swing_weight", 0.45)) * 100
    long_w = float(m.get("ai_long_weight", 0.20)) * 100

    today_cls = _pnl_css_class(today_pct)
    ai_day_cls = "pnl-ai" if (ai_day_low + ai_day_high) >= 0 else "pnl-minus"
    week_cls = "pnl-week" if week_pct >= 0 else _pnl_css_class(week_pct)

    narrative = str(m.get("ai_daily_narrative", ""))[:140]
    sub = (
        f"실시간 롤링 · AI 자금배분 단타{scalp_w:.0f}% / 스윙{swing_w:.0f}% / 장투{long_w:.0f}%"
    )
    if narrative:
        sub += f" · {narrative}"

    st.markdown(
        f'<div class="commander-pnl-board">'
        f'<span class="pnl-label">📈 오늘 통합 수익률:</span> '
        f'<span class="pnl-value {today_cls}">{_format_signed_pct(today_pct)}</span> '
        f'<span class="pnl-label">'
        f'(실현 {today_realized:+,} + 평가 {today_unrealized:+,} = <b>{today_combined:+,}원</b>)'
        f"</span>"
        f'<span class="pnl-divider">|</span>'
        f'<span class="pnl-label">🧠 오늘 총 예상 수익률:</span> '
        f'<span class="pnl-value {ai_day_cls}">{_format_pct_range(ai_day_low, ai_day_high)}</span> '
        f'<span class="pnl-label">(슬롯 등록 종목 · 모드·목표가 연동)</span>'
        f'<span class="pnl-divider">|</span>'
        f'<span class="pnl-label">📅 이번 주 통합 수익률:</span> '
        f'<span class="pnl-value {week_cls}">{_format_signed_pct(week_pct)}</span> '
        f'<span class="pnl-label">'
        f'(월~금 누적 실현 <b>{week_realized:+,}원</b> · {_format_week_start_label(week_start)})'
        f"</span>"
        f'<br><span class="pnl-label" style="font-size:0.78rem;">{sub}</span>'
        f"</div>",
        unsafe_allow_html=True,
    )


def _slot_progress_hint(pos: dict[str, Any]) -> str:
    kind = str(pos.get("target_kind") or "limit")
    if kind == "trailing":
        prog = pos.get("target_progress_pct")
        if isinstance(prog, (int, float)):
            return f" · 현재 {prog:+.1f}%"
    rem = pos.get("target_remaining_pct")
    if isinstance(rem, (int, float)) and rem > 0:
        return f" · 목표까지 +{rem:.1f}%"
    if isinstance(rem, (int, float)) and rem <= 0:
        return " · 목표 구간"
    return ""


def _render_profit_banner(profit: float) -> None:
    if profit > 0:
        cls, txt = "plus", f"▲ {profit:+.2f}%"
    elif profit < 0:
        cls, txt = "minus", f"▼ {profit:+.2f}%"
    else:
        cls, txt = "flat", f"— {profit:+.2f}%"
    st.markdown(
        f'<div class="profit-rate-banner {cls}">{txt}</div>',
        unsafe_allow_html=True,
    )


def _slot_title_label(pos: dict[str, Any]) -> str:
    """슬롯 제목 — 반드시 '한글종목명 (종목코드)'."""
    code = normalize_code(pos.get("code"))
    token = st.session_state.get("access_token")
    display = str(pos.get("display_name") or "").strip()
    if display and not is_code_only_display(display, code):
        return display

    name = str(pos.get("name") or "").strip()
    fb = _SLOT_NAME_FALLBACK.get(code) or lookup_master(code)
    if fb:
        return f"{fb} ({code})"
    if is_valid_korean_name(name, code):
        return f"{name} ({code})"
    resolved = resolve_stock_name(code, name, allow_api=False)
    if is_valid_korean_name(resolved, code):
        return f"{resolved} ({code})"
    return format_stock_label(code, name)


def _render_empty_control_slot(
    idx: int,
    candidate: dict[str, Any] | None,
    positions: list[dict[str, Any]],
) -> None:
    candidate = _safe_slot_candidate(candidate)
    cand_code = normalize_code(candidate.get("code")) if candidate else ""
    selected_mode_label = _render_slot_mode_selector(
        idx, ticker=cand_code or None
    )
    selected_candidate = _candidate_with_selected_mode(idx, candidate)
    st.markdown('<div class="slot-card-empty slot-card-recommend">', unsafe_allow_html=True)
    if candidate:
        label = _slot_title_label(candidate)
        price = int(candidate.get("price") or candidate.get("current_price") or 0)
        change = float(candidate.get("change_rate") or 0.0)
        trade_amount = int(candidate.get("trade_amount") or 0)
        st.markdown(f"**{label}**")
        st.markdown('<span class="slot-recommend-tag">주도주 우량주 후보</span>', unsafe_allow_html=True)
        st.caption(
            f"현재가 {price:,}원 · 등락 {change:+.2f}% · "
            f"5일 평균 거래대금 기준 100억 유니버스"
        )
        if trade_amount > 0:
            st.caption(f"거래대금 {trade_amount:,}원")
        preview_cache = st.session_state.setdefault("slot_buy_preview_cache", {})
        code = normalize_code(candidate.get("code"))
        preview_key = f"{code}:{_selected_slot_mode_value(idx, code)}"
        preview = preview_cache.get(preview_key) if isinstance(preview_cache, dict) else None
        if not preview or preview.get("code") != code:
            try:
                preview = preview_manual_pick_entry(
                    selected_candidate or candidate,
                    slot_idx=idx,
                )
            except Exception:
                preview = {"success": False, "code": code}
            if isinstance(preview_cache, dict):
                preview_cache[preview_key] = preview
        if preview and preview.get("success"):
            st.caption(
                f"{selected_mode_label} 모드 · 예상 투입금 {int(preview.get('budget_won', 0)):,}원 · "
                f"예상 수량 {int(preview.get('quantity', 0))}주 · "
                f"{str(preview.get('label') or preview.get('tier') or '').strip()}"
            )
        else:
            st.caption("예상 투입금/수량은 장중 시세 수신 후 표시됩니다.")
    else:
        st.markdown(
            f'<div style="font-size:1.6rem;">🧭</div><strong>슬롯 {idx}</strong><br>'
            f"<span style='font-size:0.9rem;'>[대기 중] 버튼을 눌러 종목을 추천받으세요</span>",
            unsafe_allow_html=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)
    b1, b2 = st.columns(2)
    with b1:
        if st.button(
            "🔄 다른 종목 추천",
            key=f"slot_rotate_{idx}",
            use_container_width=True,
        ):
            if candidate:
                cache = st.session_state.get("slot_buy_preview_cache")
                if isinstance(cache, dict):
                    code = normalize_code(candidate.get("code"))
                    for cache_key in list(cache):
                        if str(cache_key).startswith(f"{code}:"):
                            cache.pop(cache_key, None)
            try:
                _assign_next_slot_recommendation(idx, positions)
            except Exception:
                st.session_state["slot_action_notice"] = (
                    f"ℹ️ 슬롯 {idx} 추천 후보를 불러오는 중입니다. 잠시 후 다시 눌러주세요."
                )
            st.rerun()
    with b2:
        can_buy = candidate is not None
        if st.button(
            "🛒 이 종목 매수",
            key=f"slot_buy_{idx}",
            disabled=not can_buy,
            use_container_width=True,
            type="primary",
        ):
            with st.spinner("추천 종목 매수 주문 접수 중..."):
                try:
                    result = manual_buy_recommended_pick(
                        selected_candidate or candidate or {},
                        slot_idx=idx,
                    )
                except Exception:
                    result = {"success": False, "message": "주문 처리 대기 중입니다. 잠시 후 다시 시도해 주세요."}
            if result.get("success"):
                st.session_state["slot_action_notice"] = (
                    f"✅ 슬롯 {idx} {selected_mode_label} 모드 주문 접수: {result.get('message', '')}"
                )
                _clear_commander_metrics_cache()
                st.rerun()
            st.session_state["slot_action_notice"] = (
                f"⚠️ 슬롯 {idx} {selected_mode_label} 모드 진입 실패: {result.get('message', '주문 실패')}"
            )
            st.rerun()


def _render_slot(pos: dict[str, Any] | None, idx: int, positions: list[dict[str, Any]]) -> None:
    if pos:
        code = normalize_code(pos.get("code"))
        selected_mode_label = _render_slot_mode_selector(
            idx,
            ticker=code,
            current_mode=str(pos.get("trading_mode") or ""),
        )
        title = _slot_title_label(pos)
        st.markdown('<div class="slot-card-filled">', unsafe_allow_html=True)
        st.markdown(f"**{title}**")
        st.markdown(
            f'<div class="slot-identity-line">{format_slot_identity_line(pos)}</div>',
            unsafe_allow_html=True,
        )
        c1, c2 = st.columns(2)
        with c1:
            st.metric("평단가", f"{pos.get('entry_price', 0):,}원")
        with c2:
            st.metric("현재가", f"{pos.get('current_price', 0):,}원")
        _render_profit_banner(float(pos.get("profit_pct", 0)))
        trail = ""
        if pos.get("trailing_active"):
            trail = (
                f" · {pos.get('trailing_tier', '')} "
                f"보존 {int(pos.get('trailing_stop_price', 0)):,}원"
            )
        sl_px = int(pos.get("stop_loss_price") or 0)
        sl_txt = f" · ATR 손절 {sl_px:,}원" if sl_px > 0 else ""
        deployed = int(pos.get("deployed_won") or 0)
        bet_line = pos.get("bet_label") or pos.get("bet_tier") or ""
        cap_txt = f" · 투입 {deployed:,}원" if deployed > 0 else ""
        st.caption(
            f"슬롯 {idx} · {pos.get('quantity', 0)}주{cap_txt}"
            f"{(' · ' + str(bet_line)) if bet_line else ''} · "
            f"{pos.get('updated_at', '-')}{_slot_progress_hint(pos)}{trail}{sl_txt}"
        )
        st.markdown("</div>", unsafe_allow_html=True)
        st.button(
            "🔒 매수 락",
            key=f"slot_lock_{idx}",
            disabled=True,
            use_container_width=True,
        )
    else:
        controls = _sync_slot_controls(positions)
        _render_empty_control_slot(idx, controls.get(idx), positions)


def _render_slots_grid(positions: list[dict[str, Any]], max_slots: int) -> None:
    controls = _sync_slot_controls(positions)
    idx = 0
    for row_cols in _slot_rows_layout(max_slots):
        cols = st.columns(row_cols)
        for col in cols:
            idx += 1
            if idx > max_slots:
                return
            pos = positions[idx - 1] if idx - 1 < len(positions) else None
            with col:
                if pos is not None:
                    _render_slot(pos, idx, positions)
                else:
                    _render_empty_control_slot(idx, controls.get(idx), positions)


@st.fragment(run_every=timedelta(seconds=1))
def _ui_snapshot_watchdog() -> None:
    """체결·잔고 변경 nonce → st.session_state 미러 → st.rerun()."""
    prev_nonce = st.session_state.get("ui_dashboard_refresh_nonce")
    fill_nonce = int(get_dashboard_refresh_nonce())
    if prev_nonce is not None and fill_nonce != prev_nonce:
        st.session_state["ui_dashboard_refresh_nonce"] = fill_nonce
        _mirror_runtime_state_to_session(force=True)
        _clear_positions_session_cache()
        _clear_commander_metrics_cache()
        st.rerun()
        return
    if prev_nonce is None:
        st.session_state["ui_dashboard_refresh_nonce"] = fill_nonce

    prev_rev = st.session_state.get("runtime_state_revision")
    prev_acct = st.session_state.get("runtime_account_updated_at")

    if _mirror_runtime_state_to_session():
        _clear_positions_session_cache()
        _clear_commander_metrics_cache()

    rev = st.session_state.get("runtime_state_revision")
    acct = st.session_state.get("runtime_account_updated_at")
    if prev_rev is not None and rev != prev_rev:
        st.rerun()
        return
    if prev_acct and acct and acct != prev_acct:
        st.rerun()
        return

    _sync_ui_snapshots_from_scheduler(trigger_rerun=True)


@st.fragment(run_every=timedelta(seconds=3))
def _hero_balance_panel() -> None:
    positions = _positions_for_display()
    snap = _build_live_balance_snapshot(positions)
    st.markdown(
        f'<div class="hero-balance-board {snap["theme"]}">'
        f'<div class="hero-balance-head">실시간 잔고 전광판 · 보유 종목 자동 합산</div>'
        f'<div class="hero-balance-grid">'
        f'<div class="hero-balance-card"><div class="hero-balance-card-label">총 평가금액</div>'
        f'<div class="hero-balance-card-value">{int(snap["total_eval"]):,}원</div></div>'
        f'<div class="hero-balance-card"><div class="hero-balance-card-label">실시간 총 평가손익</div>'
        f'<div class="hero-balance-card-value">{int(snap["total_pnl"]):+,}원</div></div>'
        f'<div class="hero-balance-card"><div class="hero-balance-card-label">총 수익률</div>'
        f'<div class="hero-balance-card-value">{float(snap["total_pct"]):+.2f}%</div></div>'
        f"</div>"
        f'<div class="hero-balance-foot">{escape(str(snap["summary"]))}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )
    if st.session_state.get("positions_cache_stale"):
        st.caption("통신 지연 중입니다. 직전 정상 슬롯 데이터를 유지한 상태로 표시합니다.")


@st.fragment(run_every=timedelta(seconds=COMMANDER_PNL_REFRESH_SEC))
def _commander_pnl_live_panel() -> None:
    """F5 없이 상단 통합·예상 수익률·슬롯 로스터 실시간 동기화."""
    positions = _positions_for_display()
    _render_commander_pnl_board(positions)
    updated = str((st.session_state.get("commander_metrics") or {}).get("commander_updated_hms") or "")
    if updated:
        st.caption(
            f"🔄 슬롯 전용 실시간 갱신 · {updated} · "
            f"{COMMANDER_PNL_REFRESH_SEC}초 주기 (등록 종목만)"
        )


@st.fragment(run_every=timedelta(seconds=2))
def _ws_live_status_panel() -> None:
    """Heartbeat 기반 WS 상태 — 연결 open ≠ 정상 수신."""
    status = get_scheduler_status()
    health = status.get("ws_health") or {}
    alive = bool(health.get("alive"))
    reconnecting = bool(health.get("reconnecting"))
    connected = bool(health.get("connected"))
    label = str(health.get("status_label") or status.get("ws_status") or "WS 상태 확인 중")
    err = health.get("last_error") or status.get("ws_last_error")
    gap = health.get("seconds_since_rx")
    timeout = float(health.get("heartbeat_timeout_sec") or getattr(config, "WS_HEARTBEAT_TIMEOUT_SEC", 5))

    banner = st.empty()
    with banner.container():
        if not getattr(config, "USE_REALTIME_WEBSOCKET", True):
            st.info("실시간 WS 비활성 — REST 폴백 감시 모드")
            return

        gap_text = f" · 마지막 수신 {float(gap):.0f}초 전" if gap is not None else ""

        if alive:
            with st.status(f"🟢 {label}{gap_text}", state="complete"):
                st.caption(
                    f"Heartbeat 정상 (한도 {timeout:.0f}초) · "
                    f"체결 틱 즉시 판정 활성"
                )
        elif reconnecting or not connected:
            with st.status("🔴 WS 연결 끊김 (재연결 중...)", state="error"):
                st.write(label + gap_text)
                if err:
                    st.caption(str(err))
        else:
            with st.status("🟡 WS 연결됨 · 데이터 수신 없음", state="running"):
                st.write(label + gap_text)
                st.caption(
                    f"{timeout:.0f}초 동안 수신 없으면 자동 ws.close() 후 재접속"
                )
                if err:
                    st.caption(str(err))


@st.fragment(run_every=timedelta(seconds=5))
def _header_panel() -> None:
    status = get_scheduler_status()
    timing = get_scan_timing()
    since_scan = int(timing.get("seconds_since_scan", 0))
    scan_hms = timing.get("last_scan_completed_hms") or "—"
    stats = get_daily_stats(force_refresh=True)

    st.title("주도주·우량주 제어 대시보드")
    boot_toast = st.session_state.pop("boot_scan_toast", None)
    if boot_toast:
        if boot_toast.startswith("✅"):
            st.success(boot_toast)
        else:
            st.warning(boot_toast)

    if status.get("running"):
        _render_top_status_banner()
        mode = status.get("engine_mode", "off_hours")
        slots = int(status.get("slot_count", 0))
        max_s = int(status.get("max_slots") or MAX_SLOTS_DISPLAY)
        st.caption(
            f"{_MODE_LABELS.get(mode, mode)} · 보유 {slots}/{max_s} · "
            f"**WS 체결가 즉시판정 · 수동 추천 슬롯 제어** · "
            f"장투/주도주 우량주 5슬롯 체제",
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="countdown-box">'
            f"🛰️ 장중 실시간 무한 롤링 ({SCAN_START_TIME}~{SCAN_END_TIME}) · "
            f"마지막 탐색 {scan_hms} ({since_scan}초 전) · "
            f"보유 우량주 실시간 감시 · 빈 슬롯 수동 추천 교체"
            f"</div>",
            unsafe_allow_html=True,
        )
        summary = (
            st.session_state.get("last_force_scan_summary")
            or timing.get("last_scan_summary")
        )
        if summary:
            st.caption(f"📋 최근 탐색 요약: {summary}")
        _render_order_status_panel()
        theme_line = status.get("brain_theme_timeline") or []
        if theme_line:
            parts = [
                f"{t.get('title')} D-{t.get('days_until')}"
                for t in theme_line[:2]
                if isinstance(t, dict)
            ]
            if parts:
                st.caption("🧠 Brain 테마: " + " · ".join(parts))
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("오늘 매매", f"{stats.get('trade_count', 0)}회")
        with c2:
            pnl = int(stats.get("total_pnl", 0))
            st.metric("오늘 실현손익", f"{pnl:+,}원")
        with c3:
            st.metric("슬롯 체제", "장투/주도주 우량주 제어")
        st.caption(
            f"평일 {SCAN_START_TIME}~{SCAN_END_TIME} · "
            f"유니버스: 5일 평균 거래대금 ≥ "
            f"{getattr(config, 'SWING_MIN_AVG_TRADE_VALUE_5D', 10_000_000_000) // 100_000_000:,}억 · "
            f"ATR 가변 손절({getattr(config, 'ATR_MIN_LOSS_PCT', 3):.0f}~"
            f"{getattr(config, 'ATR_MAX_LOSS_PCT', 15):.0f}%) · "
            f"추천 슬롯은 버튼으로 즉시 교체 · 보유 슬롯은 매수 락 유지"
        )
        if st.button("긴급 일괄 청산", key="emergency_liquidate", type="primary"):
            with st.spinner("전량 매도 중..."):
                result = emergency_liquidate_all()
            if result.get("success") and result.get("sold_count", 0) > 0:
                st.success("전 포지션 청산 완료")
                st.rerun()
            elif result.get("sold_count", 0) == 0:
                st.info("청산할 포지션이 없습니다.")
            else:
                st.error("일부 청산 실패 — 로그 확인")
        if status.get("last_error"):
            st.warning(str(status["last_error"]))
    else:
        st.warning("엔진 기동 중...")


@st.fragment(run_every=timedelta(seconds=COMMANDER_PNL_REFRESH_SEC))
def _slots_panel() -> None:
    positions = _positions_for_display()
    st.markdown(
        f"### 장투/주도주 우량주 제어 슬롯 ({len(positions)}/{MAX_SLOTS_DISPLAY})"
    )
    slot_notice = st.session_state.pop("slot_action_notice", None)
    if slot_notice:
        if str(slot_notice).startswith("✅"):
            st.success(str(slot_notice))
        else:
            st.warning(str(slot_notice))
    if st.session_state.get("positions_cache_stale"):
        st.info("데이터 수신 대기 중입니다. 슬롯 레이아웃은 마지막 정상 상태를 유지합니다.")
    _render_slots_grid(positions[:MAX_SLOTS_DISPLAY], MAX_SLOTS_DISPLAY)
    if positions:
        st.caption(
            f"보유 슬롯은 실시간 감시 상태를 유지하고, 빈 슬롯은 [🔄 다른 종목 추천] 버튼으로 "
            f"100억 유니버스 대장주를 수동 교체합니다. 총시드 {_total_seed():,}원"
        )
    else:
        st.info(
            "보유 종목이 없습니다. 각 슬롯의 [🔄 다른 종목 추천] 버튼으로 우량주 후보를 불러오세요."
        )


@st.fragment(run_every=timedelta(seconds=min(ACCOUNT_REFRESH_SEC, COMMANDER_PNL_REFRESH_SEC)))
def _account_panel(token: str | None) -> None:
    _sync_ui_snapshots_from_scheduler(trigger_rerun=False)
    stock_eval, cash, account_total, stale, updated_at = _read_account_panel_values()
    st.markdown("### 모의투자 계좌 현황")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("주식 평가금액", f"{stock_eval:,}원")
    with c2:
        st.metric("예수금", f"{cash:,}원")
    with c3:
        st.metric("계좌 총자산", f"{account_total:,}원")
    if not token:
        st.caption("접속 토큰 없음 — 스케줄러 스냅샷만 표시합니다.")
    elif stale:
        st.caption("증권사 응답 대기 중입니다. 스케줄러가 보관 중인 최신 스냅샷을 표시합니다.")
    if updated_at:
        st.caption(
            f"잔고 스냅샷 · {updated_at} · "
            f"{min(ACCOUNT_REFRESH_SEC, COMMANDER_PNL_REFRESH_SEC)}초 주기 갱신"
        )


def _render_section_safely(
    label: str,
    renderer: Any,
    *args: Any,
    divider_after: bool = False,
) -> None:
    try:
        renderer(*args)
    except Exception:
        st.info(f"{label} 업데이트 지연 중입니다. 직전 정상 화면을 유지합니다.")
    if divider_after:
        st.divider()


@st.fragment(run_every=timedelta(seconds=30))
def _receipts_panel() -> None:
    history = get_daily_trade_history()
    stats = get_daily_stats(force_refresh=True)
    st.markdown("### 오늘 청산 영수증")
    st.caption(
        f"{stats.get('stats_date', '')} · 청산 {stats.get('trade_count', 0)}건"
    )
    if not history:
        st.info("오늘 완전 청산된 종목이 없습니다.")
        return
    st.dataframe(
        [
            {
                "종목명": e.get("종목명", e.get("name", "-")),
                "매도시간": e.get("매도시간", e.get("time", "-")),
                "청산": e.get("exit_type", "청산"),
                "수익률": _profit_pct_display(float(e.get("profit_pct", 0))),
                "수익금액": f"{int(e.get('pnl', e.get('수익금액', 0))):+,}원",
            }
            for e in history
        ],
        use_container_width=True,
        hide_index=True,
    )
    total = int(stats.get("total_pnl", 0))
    st.markdown(f"**오늘 합산 실현손익: {total:+,}원**")


_warm_ui_session_caches()
_mirror_runtime_state_to_session(force=True)
_sync_boot_scan_to_session()
_sync_watch_time_from_scheduler()
_handle_browser_refresh_force_scan()

cached_token = st.session_state.get("access_token")
token = str(cached_token) if cached_token else None

_ui_snapshot_watchdog()
_render_section_safely("실시간 WS", _ws_live_status_panel, divider_after=True)
_render_section_safely("실시간 잔고 전광판", _hero_balance_panel)
_render_section_safely("지휘관 실시간 수익률", _commander_pnl_live_panel)
_render_section_safely("상단 현황판", _header_panel, divider_after=True)
_render_section_safely("슬롯 제어판", _slots_panel, divider_after=True)
_render_section_safely("계좌 현황", _account_panel, token, divider_after=True)
_render_section_safely("청산 영수증", _receipts_panel)
