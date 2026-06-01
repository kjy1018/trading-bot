import logging
import queue
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any

from account import get_account_snapshot
import config
import trade_state
from notification_manager import NotificationManager
from discord_control import start_discord_control_bot
from risk_atr import atr_stop_price, wilder_atr
from auth import get_access_token
from kis_rate import (
    KISRateLimitError,
    Priority,
    call_with_retry,
    is_rate_limit_error,
    kis_loop_pause,
    order_priority_lane,
)
from order import buy_limit_order, buy_market_order, sell_market_order, sell_smart_sor
from stock import get_current_price
from stock_ranking import is_common_stock_for_trade
from stock_swing import get_swing_universe, _fetch_hourly_bars
from betting_engine import (
    BetTier,
    get_capital_snapshot,
    plan_entry,
    quantity_for_budget,
    should_pyramid_add,
)
from market_ai import refresh_ai_forecasts
from market_scan import select_market_entries
from stock_names import (
    enrich_position,
    is_valid_korean_name,
    normalize_code,
    register_universe_names,
    resolve_stock_name,
)
from brain import (
    enrich_stock_with_brain,
    pick_brain_recommendations,
    plan_theme_actions,
    theme_timeline_hints,
)
from brain_classifier import (
    MODE_BADGE_CSS,
    MODE_LABEL_KO,
    MODE_POLICIES,
    TradingMode,
    get_brain_classifier,
)
from trading_logic import (
    apply_expected_exit_to_position,
    apply_tactical_fields_on_position,
    decide_position_exit,
    get_entry_stop_loss,
    is_polling_strategy_mode,
    monitor_interval_for_positions,
    passes_polling_entry_ma_filter,
    plan_position_add,
    position_trading_mode,
    refresh_target_live_fields,
    requires_daily_bars,
    requires_fast_tick_exit,
    requires_hourly_bars,
    requires_minute_bars,
)

logger = logging.getLogger(__name__)

# DEBUG: Prior missed trades came from synchronous quote/order traffic sharing one
# execution path. Orders are now enqueued and positions mutate only after
# asynchronous fill confirmation from broker account snapshots.

def _total_seed() -> int:
    from betting_engine import account_total_seed

    return account_total_seed()
MAX_SLOTS = getattr(config, "MAX_SIMULTANEOUS_STOCKS", 5)
TARGET_PROFIT_PCT = getattr(config, "TARGET_PROFIT_PCT", 12.0)
TARGET_PROFIT_MAX_PCT = getattr(config, "TARGET_PROFIT_MAX_PCT", 15.0)
TRAILING_MIN_PEAK_PROFIT_PCT = getattr(config, "TRAILING_MIN_PEAK_PROFIT_PCT", 5.0)
TRAILING_TIER1_DROP_PCT = getattr(config, "TRAILING_TIER1_DROP_PCT", 2.0)
TRAILING_TIER2_DROP_PCT = getattr(config, "TRAILING_TIER2_DROP_PCT", 3.0)
TRAILING_TIER3_DROP_PCT = getattr(config, "TRAILING_TIER3_DROP_PCT", 5.0)
MONITOR_INTERVAL_SEC = getattr(config, "MONITOR_INTERVAL_SEC", 5)
WS_FAILBACK_POLL_SEC = float(getattr(config, "WS_FAILBACK_POLL_SEC", 5.0))
REALTIME_SCAN_INTERVAL_SEC = float(
    getattr(config, "REALTIME_SCAN_INTERVAL_SEC", getattr(config, "SCAN_INTERVAL_SEC", 20))
)
REALTIME_UNIVERSE_REFRESH_SEC = float(getattr(config, "REALTIME_UNIVERSE_REFRESH_SEC", 300))
REALTIME_SCAN_BATCH_SIZE = int(getattr(config, "REALTIME_SCAN_BATCH_SIZE", 30))
REALTIME_ENGINE_TICK_SEC = float(getattr(config, "REALTIME_ENGINE_TICK_SEC", 0.35))
KIS_API_MIN_INTERVAL_SEC = getattr(config, "KIS_API_MIN_INTERVAL_SEC", 0.35)
SCAN_START_TIME = getattr(config, "AUTO_TRADE_SCAN_START_TIME", "09:00")
SCAN_END_TIME = getattr(config, "AUTO_TRADE_SCAN_END_TIME", "15:30")

def _extract_hms_from_timestamp(raw: str | None) -> str | None:
    """'YYYY-MM-DD HH:MM:SS' 또는 'HH:MM:SS' → HH:MM:SS."""
    if not raw:
        return None
    text = str(raw).strip()
    if len(text) >= 19 and text[10] == " ":
        return text[11:19]
    if len(text) == 8 and text.count(":") == 2:
        return text
    return None


def build_status_banner_text(recent_watch_hms: str) -> str:
    """상단 초록 배너 문구 (최근 감시 시각 포함)."""
    label = recent_watch_hms if recent_watch_hms else "대기 중"
    return (
        "「실시간 웹소켓 감시 · ATR 가변 손절 · SOR 스마트 매도」 | "
        f"[최근 감시: {label}] — 실시간 롤링 엔진 · 슬롯 {MAX_SLOTS} · "
        f"총시드 {_total_seed():,}원 · 가변베팅"
    )


BANNER_TEXT = build_status_banner_text("대기 중")

_state_lock = threading.Lock()
_positions_lock = threading.Lock()

_state: dict[str, Any] = {
    "running": False,
    "banner": BANNER_TEXT,
    "engine_mode": "off_hours",
    "last_job_at": None,
    "last_job_result": None,
    "last_error": None,
    "positions": [],
    "slot_count": 0,
    "max_slots": MAX_SLOTS,
    "monitoring": False,
    "scanning": False,
    "last_scan_completed_at": None,
    "last_scan_completed_hms": None,
    "last_scan_summary": None,
    "daily_trade_count": 0,
    "daily_total_pnl": 0,
    "daily_trade_history": [],
    "daily_stats_date": None,
    "ws_status": "WS 초기화 전",
    "ws_last_error": None,
    "account_snapshot": {
        "total_eval": 0,
        "cash": 0,
        "holdings": {},
        "daily_eval_pnl": 0,
        "daily_eval_pnl_pct": 0.0,
        "total_return_pct": 0.0,
        "stock_eval": 0,
        "account_total_eval": 0,
        "updated_at": None,
        "stale": True,
    },
    "order_status": [],
    "order_events": [],
    "ui_slot_recommendations": [],
    "buy_paused": False,
    "daily_close_report": None,
    "daily_close_report_date": None,
}

_positions: dict[str, dict[str, Any]] = {}
_engine_thread: threading.Thread | None = None
_monitor_thread: threading.Thread | None = None
_last_scan_at: float = 0.0
_last_monitor_at: float = 0.0
_last_balance_poll_at: float = 0.0
_daily_bars_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_universe_cache: list[dict[str, Any]] = []
_universe_cache_at: float = 0.0
_scan_batch_cursor: int = 0
_intraday_bars_cache: dict[str, tuple[float, list[dict]]] = {}
_watch_epoch: float = 0.0
_stats_date: date | None = None
_last_position_persist: float = 0.0
_POSITION_PERSIST_INTERVAL = 2.0
_last_synced_slot_count: int = -1

_emergency_lock = threading.Lock()
_start_lock = threading.Lock()
_started = False
_ws_hub: Any = None
_exit_eval_lock = threading.Lock()
_scan_job_lock = threading.Lock()  # 레거시 — 신규는 _scan_active 사용
_universe_lock = threading.Lock()
_scan_active_lock = threading.Lock()
_scan_active = False
_engine_wake = threading.Event()
WS_WATCHLIST_TOP_N = int(getattr(config, "WS_WATCHLIST_TOP_N", 20))
WS_SCAN_DEBOUNCE_SEC = float(getattr(config, "WS_SCAN_DEBOUNCE_SEC", 0.8))
WS_ENGINE_WAIT_SEC = float(getattr(config, "WS_ENGINE_WAIT_SEC", 0.05))

_boot_scan_lock = threading.Lock()
_boot_scan_status = "idle"  # idle | running | done | error
_boot_scan_result: dict[str, Any] | None = None
_boot_scan_thread: threading.Thread | None = None

_force_scan_lock = threading.Lock()
_force_scan_running = False
_force_scan_result: dict[str, Any] | None = None
_force_scan_result_epoch: float = 0.0
_buy_paused = False

ORDER_STATUS_POLL_SEC = float(getattr(config, "ORDER_STATUS_POLL_SEC", 10))
ORDER_FILL_POLL_ACTIVE_SEC = float(
    getattr(config, "ORDER_FILL_POLL_ACTIVE_SEC", 1)
)
ACCOUNT_SNAPSHOT_REFRESH_SEC = float(getattr(config, "ACCOUNT_SNAPSHOT_REFRESH_SEC", 45))
ORDER_EVENT_HISTORY_MAX = int(getattr(config, "ORDER_EVENT_HISTORY_MAX", 40))

_order_queue: queue.Queue[dict[str, Any]] = queue.Queue()
_order_worker_thread: threading.Thread | None = None
_order_fill_thread: threading.Thread | None = None
_order_fill_wake = threading.Event()
_orders_lock = threading.Lock()
_orders: dict[str, dict[str, Any]] = {}
_order_events: deque[dict[str, Any]] = deque(maxlen=ORDER_EVENT_HISTORY_MAX)
_last_account_refresh_at: float = 0.0
_last_ws_resync_at: float = 0.0
WS_RECONNECT_RESYNC_DEBOUNCE_SEC = float(
    getattr(config, "WS_RECONNECT_RESYNC_DEBOUNCE_SEC", 3.0)
)
_notifier = NotificationManager()
_last_open_summary_sent_date: str | None = None
_last_close_summary_sent_date: str | None = None
_last_close_report_sent_date: str | None = None
_last_preopen_boot_date: str | None = None


def _wake_order_fill_poller() -> None:
    """매도·매수 주문 접수 직후 체결 폴러 즉시 깨우기."""
    _order_fill_wake.set()


def _has_pending_orders() -> bool:
    with _orders_lock:
        return any(
            _is_open_order_status(str(v.get("status")))
            for v in _orders.values()
        )


def _parse_hhmm(value: str) -> dt_time:
    hour, minute = value.split(":")
    return dt_time(int(hour), int(minute))


SCAN_START = _parse_hhmm(SCAN_START_TIME)
SCAN_END = _parse_hhmm(SCAN_END_TIME)


def _is_weekday() -> bool:
    return datetime.now().weekday() < 5


def _in_scan_window(now: dt_time) -> bool:
    return SCAN_START <= now < SCAN_END


def _quote_price(token: str, code: str) -> dict:
    """보유 감시용 초경량 현재가 (전역 kis_request 적용)."""
    return get_current_price(token, code, config.APP_KEY, config.APP_SECRET)


def _mark_scan_completed(summary: str = "") -> None:
    global _watch_epoch
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    hms = now.strftime("%H:%M:%S")
    _watch_epoch = time.time()
    with _state_lock:
        _state["last_scan_completed_at"] = ts
        _state["last_scan_completed_hms"] = hms
        _state["last_scan_summary"] = summary or "실시간 유니버스 탐색 완료"
        _state["banner"] = build_status_banner_text(hms)


def get_watch_time_snapshot() -> dict[str, Any]:
    """UI(main) 스레드에서 세션 동기화용 최신 감시 시각."""
    with _state_lock:
        return {
            "hms": _state.get("last_scan_completed_hms"),
            "full": _state.get("last_scan_completed_at"),
            "epoch": _watch_epoch,
            "summary": _state.get("last_scan_summary"),
        }


def _attach_watch_fields(result: dict[str, Any]) -> dict[str, Any]:
    """탐색 완료 직후 반환 dict에 watch_hms·watch_full 주입."""
    snap = get_watch_time_snapshot()
    hms = snap.get("hms") or _extract_hms_from_timestamp(snap.get("full"))
    if hms:
        result["watch_hms"] = hms
        result["watch_full"] = snap.get("full")
        result["watch_epoch"] = snap.get("epoch", 0.0)
        result["completed_at"] = hms
    return result


def get_recent_watch_hms() -> str:
    """백그라운드·강제 탐색 완료 시각 (HH:MM:SS). 기록 없으면 '대기 중'."""
    with _state_lock:
        hms = _state.get("last_scan_completed_hms")
        full = _state.get("last_scan_completed_at")
    parsed = hms or _extract_hms_from_timestamp(full)
    return parsed if parsed else "대기 중"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_ts() -> float:
    return time.time()


_MODE_BY_VALUE = {mode.value: mode for mode in TradingMode}
_MODE_BY_LABEL = {str(label): mode for mode, label in MODE_LABEL_KO.items()}


def _normalize_trading_mode(raw: Any, fallback: str = "swing") -> str:
    from trading_categories import normalize_trading_category

    return normalize_trading_category(raw, fallback=fallback)


def _mode_tags_for_value(mode_value: str) -> dict[str, Any]:
    mode = _MODE_BY_VALUE.get(mode_value, TradingMode.SWING)
    policy = MODE_POLICIES[mode]
    return {
        "trading_mode": mode.value,
        "mode_label": MODE_LABEL_KO[mode],
        "mode_badge_class": MODE_BADGE_CSS[mode],
        "mode_hold_hint": (
            "당일청산"
            if policy.exit_same_day
            else f"{policy.hold_days_min}~{policy.hold_days_max}일"
        ),
        "mode_chart": policy.chart_timeframe,
        "mode_policy": policy.description,
    }


def _apply_trading_mode_override(
    payload: dict[str, Any],
    raw_mode: Any,
    *,
    fallback: str = "swing",
) -> dict[str, Any]:
    out = dict(payload)
    mode_value = _normalize_trading_mode(raw_mode, fallback=fallback)
    out.update(_mode_tags_for_value(mode_value))
    out["selected_trading_mode"] = mode_value
    return out


def _lock_ui_trading_mode(pick: dict[str, Any]) -> dict[str, Any]:
    """
    전황판 슬롯·UI 수동 모드 — 판단뇌 자동 분류보다 우선 고정.

    ui_slot_index 가 있으면 selected_modes.json 의 _slot_N 을 최우선으로 읽는다.
    """
    out = dict(pick)
    slot_idx = out.get("ui_slot_index")
    code = normalize_code(out.get("code"))
    mode_raw = out.get("selected_trading_mode") or out.get("trading_mode")
    if slot_idx is not None and int(slot_idx) > 0:
        try:
            from slot_registry import get_slot_personality_for_display_idx

            mode_raw = get_slot_personality_for_display_idx(int(slot_idx))
            out["ui_slot_index"] = int(slot_idx)
            out["ui_mode_locked"] = True
        except ImportError:
            pass
        if not mode_raw:
            try:
                from selected_modes import resolve_mode_value_for_slot

                mode_raw = resolve_mode_value_for_slot(
                    int(slot_idx),
                    code=code or None,
                    fallback_value=mode_raw,
                )
                out["ui_slot_index"] = int(slot_idx)
                out["ui_mode_locked"] = True
            except ImportError:
                pass
    elif out.get("ui_mode_locked") and mode_raw:
        out["ui_mode_locked"] = True
    if mode_raw:
        out = _apply_trading_mode_override(
            out,
            mode_raw,
            fallback=str(mode_raw or TradingMode.SWING.value),
        )
    return out


def _public_order_state(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticket_id": order.get("ticket_id"),
        "action": order.get("action"),
        "code": order.get("code"),
        "name": order.get("name"),
        "trading_mode": order.get("trading_mode"),
        "mode_label": order.get("mode_label"),
        "status": order.get("status"),
        "message": order.get("message"),
        "quantity": int(order.get("quantity") or 0),
        "budget_won": int(order.get("budget_won") or 0),
        "created_at": order.get("created_at"),
        "updated_at": order.get("updated_at"),
        "broker_order_no": order.get("broker_order_no"),
        "reason": order.get("reason"),
    }


def _publish_order_snapshot() -> None:
    with _orders_lock:
        states = sorted(
            (_public_order_state(v) for v in _orders.values()),
            key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""),
            reverse=True,
        )
        events = [dict(e) for e in _order_events]
    with _state_lock:
        _state["order_status"] = states[:20]
        _state["order_events"] = events[-10:]


def _append_order_event(order: dict[str, Any]) -> None:
    event = {
        "ticket_id": order.get("ticket_id"),
        "status": order.get("status"),
        "message": order.get("message"),
        "code": order.get("code"),
        "name": order.get("name"),
        "updated_at": order.get("updated_at"),
    }
    with _orders_lock:
        _order_events.append(event)
    _publish_order_snapshot()


def _is_open_order_status(status: str) -> bool:
    return status in {"queued", "submitting", "submitted", "pending_fill", "partial_fill"}


def _reserved_buy_budget_won() -> int:
    with _orders_lock:
        return sum(
            int(o.get("budget_won") or 0)
            for o in _orders.values()
            if str(o.get("action")) in {"buy", "pyramid_buy"} and _is_open_order_status(str(o.get("status")))
        )


def _pending_entry_order_count() -> int:
    with _orders_lock:
        return sum(
            1
            for o in _orders.values()
            if str(o.get("action")) == "buy"
            and _is_open_order_status(str(o.get("status")))
            and int(o.get("baseline_qty") or 0) <= 0
        )


def _is_code_order_pending(code: str, actions: set[str] | None = None) -> bool:
    norm = normalize_code(code)
    with _orders_lock:
        for order in _orders.values():
            if normalize_code(order.get("code")) != norm:
                continue
            if actions and str(order.get("action")) not in actions:
                continue
            if _is_open_order_status(str(order.get("status"))):
                return True
    return False


def _update_order_state(ticket_id: str, **updates: Any) -> dict[str, Any] | None:
    with _orders_lock:
        order = _orders.get(ticket_id)
        if not order:
            return None
        order.update(updates)
        order["updated_at"] = _now_text()
        snapshot = dict(order)
    _append_order_event(snapshot)
    return snapshot


def _create_order_state(intent: dict[str, Any]) -> dict[str, Any]:
    ticket_id = uuid.uuid4().hex[:12]
    created = _now_text()
    mode_value = _normalize_trading_mode(
        intent.get("trading_mode"),
        fallback=_normalize_trading_mode(
            (intent.get("pick") or {}).get("trading_mode"),
            fallback=TradingMode.SWING.value,
        ),
    )
    mode_tags = _mode_tags_for_value(mode_value)
    order = {
        "ticket_id": ticket_id,
        "action": intent.get("action"),
        "code": normalize_code(intent.get("code")),
        "name": intent.get("name") or "",
        "trading_mode": mode_tags["trading_mode"],
        "mode_label": mode_tags["mode_label"],
        "status": "queued",
        "message": str(intent.get("message") or "주문 대기열 등록"),
        "created_at": created,
        "updated_at": created,
        "quantity": int(intent.get("quantity") or 0),
        "budget_won": int(intent.get("budget_won") or 0),
        "reason": intent.get("reason"),
        "baseline_qty": int(intent.get("baseline_qty") or 0),
        "slot_uid": str(intent.get("slot_uid") or intent.get("slot_id") or ""),
        "slot_id": str(intent.get("slot_id") or ""),
        "payload": dict(intent),
        "broker_order_no": "",
    }
    with _orders_lock:
        _orders[ticket_id] = order
    _append_order_event(order)
    return dict(order)


def _mark_position_order_pending(code: str, ticket_id: str, action: str) -> None:
    with _positions_lock:
        pos = _positions.get(code)
        if not pos:
            return
        pos["pending_order_ticket"] = ticket_id
        pos["pending_order_action"] = action
        pos["pending_order_at"] = _now_text()
    _maybe_persist_positions(False)
    _sync_positions_state()


def _clear_position_order_pending(code: str, ticket_id: str | None = None) -> None:
    with _positions_lock:
        pos = _positions.get(code)
        if not pos:
            return
        if ticket_id and str(pos.get("pending_order_ticket")) != ticket_id:
            return
        pos.pop("pending_order_ticket", None)
        pos.pop("pending_order_action", None)
        pos.pop("pending_order_at", None)
    _maybe_persist_positions(False)
    _sync_positions_state()


def _refresh_account_snapshot(
    force: bool = False,
    *,
    sync_runtime_positions: bool = True,
    bump_positions_revision: bool = True,
    publish_to_state: bool = True,
) -> dict[str, Any]:
    global _last_account_refresh_at
    now = _now_ts()
    with _state_lock:
        cached = dict(_state.get("account_snapshot") or {})
    if (
        not force
        and publish_to_state
        and cached.get("updated_at")
        and now - _last_account_refresh_at < ACCOUNT_SNAPSHOT_REFRESH_SEC
    ):
        return cached

    token = get_access_token()
    try:
        snap = call_with_retry(
            lambda: get_account_snapshot(
                token,
                sync_runtime_positions=sync_runtime_positions,
                bump_positions_revision=bump_positions_revision,
            ),
            priority=Priority.NORMAL,
            user_message="잔고 조회 재시도 중...",
        )
    except KISRateLimitError as exc:
        logger.debug("계좌 스냅샷 속도 제한 — 캐시 유지: %s", exc)
        if cached:
            stale = dict(cached)
            stale["stale"] = True
            stale["message"] = str(exc)
            return stale
        raise
    snap["stale"] = False
    if publish_to_state:
        with _state_lock:
            _state["account_snapshot"] = snap
        _last_account_refresh_at = now
        try:
            total = int(snap.get("account_total_eval") or 0)
            if total > 0:
                trade_state.save_account_snapshot_for_charts(
                    total,
                    stock_eval=int(snap.get("stock_eval") or snap.get("total_eval") or 0),
                    cash=int(snap.get("cash") or 0),
                )
        except Exception as exc:
            logger.debug("계좌 스냅샷 DB 저장 스킵: %s", exc)
        if sync_runtime_positions:
            _apply_runtime_positions_from_store()
    return snap


def _set_account_snapshot_stale(message: str | None = None) -> None:
    with _state_lock:
        snap = dict(_state.get("account_snapshot") or {})
        snap["stale"] = True
        if message:
            snap["message"] = message
        _state["account_snapshot"] = snap

def _wake_engine() -> None:
    _engine_wake.set()


def _begin_scan() -> bool:
    global _scan_active
    with _scan_active_lock:
        if _scan_active:
            return False
        _scan_active = True
        return True


def _end_scan() -> None:
    global _scan_active
    with _scan_active_lock:
        _scan_active = False


def _scan_debounce_sec() -> float:
    if _realtime_ws_ready():
        return WS_SCAN_DEBOUNCE_SEC
    return float(REALTIME_SCAN_INTERVAL_SEC)


def _should_run_realtime_scan() -> bool:
    """장중 빈 슬롯 — WS 틱·엔진 이벤트 기반 재탐색 (1H 정각 없음)."""
    if _buy_paused:
        return False
    if _empty_slots() <= 0:
        return False
    if not _is_weekday() or not _in_scan_window(datetime.now().time()):
        return False
    if _last_scan_at <= 0:
        return True
    return time.time() - _last_scan_at >= _scan_debounce_sec()


def _sync_balance_after_ws_reconnect() -> None:
    """
    WS 자동 재연결 직후 — 끊김 동안 놓친 체결·잔고를 inquire_balance로 보정.
    """
    global _last_ws_resync_at
    now = _now_ts()
    if now - _last_ws_resync_at < WS_RECONNECT_RESYNC_DEBOUNCE_SEC:
        logger.debug("WS 재연결 동기화 디바운스 — 스킵")
        return
    _last_ws_resync_at = now
    logger.info("WS 재연결 — inquire_balance 잔고·포지션 강제 동기화")
    with _state_lock:
        _state["ws_status"] = "WS 재연결됨 · 잔고 동기화 중"
    try:
        snap = _refresh_account_snapshot(
            force=True,
            sync_runtime_positions=True,
            bump_positions_revision=True,
        )
        _apply_runtime_positions_from_store()
        _sync_positions_state()
        try:
            trade_state.reload_positions_state_from_disk()
        except Exception as exc:
            logger.debug("WS 재연결 positions_state 재로드: %s", exc)
        _sync_ws_watchlist()
        _force_refresh_trade_state_sync("ws_reconnect")
        trade_state.request_dashboard_refresh("ws_reconnect")
        holdings = snap.get("holdings") or {}
        n_hold = len(holdings) if isinstance(holdings, dict) else 0
        n_slots = len(get_positions_snapshot())
        logger.info(
            "WS 재연결 동기화 완료 · broker %d종목 · UI 슬롯 %d",
            n_hold,
            n_slots,
        )
        with _state_lock:
            _state["ws_status"] = "WS 정상 (재연결·동기화 완료)"
            _state["ws_last_error"] = None
        try:
            from trading_logic import finalize_order_fill_dashboard_sync

            finalize_order_fill_dashboard_sync(side="ws_reconnect", code="")
        except Exception:
            pass
    except Exception as exc:
        logger.warning("WS 재연결 잔고 동기화 실패: %s", exc)
        with _state_lock:
            _state["ws_status"] = "WS 재연결됨 · 잔고 동기화 실패"
            _state["ws_last_error"] = str(exc)


def _sync_ws_watchlist() -> None:
    """보유 + 유니버스 상위 종목 WS 구독 — 체결 틱이 엔진을 깨움."""
    global _ws_hub
    if _ws_hub is None:
        return
    codes = set(_get_held_codes())
    if _universe_cache:
        for item in _universe_cache[:WS_WATCHLIST_TOP_N]:
            c = str(item.get("code", "")).strip()[-6:]
            if len(c) == 6 and c.isdigit():
                codes.add(c)
    try:
        _ws_hub.set_target_codes(codes)
    except Exception as exc:
        logger.warning("WS watchlist 동기화 실패: %s", exc)


def _sync_ui_recommendations_from_universe() -> None:
    rows = pick_brain_recommendations(_universe_cache, limit=40)
    with _state_lock:
        _state["ui_slot_recommendations"] = rows
        _state["brain_theme_timeline"] = theme_timeline_hints()


def _commander_slots_from_positions() -> list[dict[str, Any]]:
    """스케줄러 — 보유 슬롯만 AI 당일 가이드 대상 (시장 TOP 스캔 없음)."""
    from trading_logic import assemble_commander_slots

    with _positions_lock:
        positions = [dict(p) for p in _positions.values()]
    return assemble_commander_slots(
        positions,
        slot_controls=None,
        include_control_candidates=False,
    )


def _reload_universe_cache(token: str, *, refresh_ai: bool = True) -> list[dict[str, Any]]:
    """유니버스 캐시 강제 갱신 + (선택) AI 예측."""
    global _universe_cache, _universe_cache_at
    with _universe_lock:
        ranked = get_swing_universe(token, config.APP_KEY, config.APP_SECRET)
        _universe_cache = ranked
        _universe_cache_at = time.time()
        register_universe_names(ranked)
        _sync_ui_recommendations_from_universe()
        if refresh_ai:
            try:
                ai = refresh_ai_forecasts(
                    access_token=token,
                    universe=ranked,
                    commander_slots=_commander_slots_from_positions(),
                    force=True,
                )
                _sync_ai_forecast_to_state(ai)
            except Exception as exc:
                logger.warning("AI 예측 갱신 실패: %s", exc)
    _sync_ws_watchlist()
    return ranked


def _get_universe_cached(token: str) -> list[dict[str, Any]]:
    global _universe_cache, _universe_cache_at
    if (
        _universe_cache
        and time.time() - _universe_cache_at < REALTIME_UNIVERSE_REFRESH_SEC
    ):
        _sync_ui_recommendations_from_universe()
        _sync_ws_watchlist()
        return _universe_cache
    return _reload_universe_cache(token, refresh_ai=False)


def _sync_ai_forecast_to_state(ai: dict[str, Any]) -> None:
    """UI·로그용 AI 예측 스냅샷."""
    daily = ai.get("daily") or {}
    weekly = ai.get("weekly") or {}
    alloc = ai.get("allocation") or {}
    with _state_lock:
        _state["ai_daily_range"] = (
            f"{daily.get('pct_low', 0):+.1f}~{daily.get('pct_high', 0):+.1f}%"
        )
        _state["ai_weekly_range"] = (
            f"{weekly.get('pct_low', 0):+.1f}~{weekly.get('pct_high', 0):+.1f}%"
        )
        _state["ai_allocation"] = (
            f"단타 {float(alloc.get('scalp_weight', 0)) * 100:.0f}% · "
            f"스윙 {float(alloc.get('swing_weight', 0)) * 100:.0f}% · "
            f"장투 {float(alloc.get('long_weight', 0)) * 100:.0f}%"
        )
        _state["ai_daily_narrative"] = daily.get("narrative", "")
        _state["ai_weekly_narrative"] = weekly.get("narrative", "")


def _get_scalp_minute_bars_cached(token: str, code: str) -> list[dict]:
    now = time.time()
    cached = _intraday_bars_cache.get(code)
    if cached and now - cached[0] < 25.0:
        return cached[1]
    from stock_swing import fetch_intraday_minute_bars

    bars = fetch_intraday_minute_bars(
        token, config.APP_KEY, config.APP_SECRET, code, trading_days=1
    )
    _intraday_bars_cache[code] = (now, bars)
    return bars


def _record_job_start() -> None:
    with _state_lock:
        _state["last_job_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _record_job_success(msg: str) -> None:
    with _state_lock:
        _state["last_job_result"] = msg
        _state["last_error"] = None


def _record_job_failure(exc: Exception) -> None:
    with _state_lock:
        _state["last_error"] = str(exc)
        _state["last_job_result"] = None


def _apply_trade_state_to_memory() -> None:
    trade_state.ensure_trade_state_file()
    count, total, stats_date = trade_state.get_totals()
    history = trade_state.receipts_for_ui()
    with _state_lock:
        _state["daily_trade_count"] = count
        _state["daily_total_pnl"] = total
        _state["daily_trade_history"] = history
        _state["daily_stats_date"] = stats_date


def _refresh_balance_after_fill(side: str, code: str) -> None:
    """전량 체결 확정 직후 account.py 잔고 강제 조회 + 메모리·대시보드 동기화."""
    norm = normalize_code(code)
    try:
        snap = _refresh_account_snapshot(
            force=True,
            sync_runtime_positions=True,
            bump_positions_revision=True,
        )
        holdings = snap.get("holdings") or {}
        logger.info(
            "체결 직후 잔고 갱신: %s %s · broker %d종목 · slots %d",
            side,
            norm,
            len(holdings) if isinstance(holdings, dict) else 0,
            len(get_positions_snapshot()),
        )
    except Exception as exc:
        logger.warning("체결 직후 잔고 갱신 실패 (%s %s): %s", side, norm, exc)
    try:
        from trading_logic import finalize_order_fill_dashboard_sync

        finalize_order_fill_dashboard_sync(side=side, code=norm or code)
    except Exception as exc:
        logger.debug("체결 직후 dashboard sync 실패: %s", exc)


def _sync_after_sell_fill_confirmed(code: str) -> None:
    """
    매도 전량 체결 확정 직후 — positions_state 재로드, 실현손익·UI 즉시 동기화.
    목표: 1초 이내 대시보드 반영.
    """
    norm = normalize_code(code)
    try:
        reloaded = trade_state.reload_positions_state_from_disk()
        logger.info(
            "매도 체결 직후 positions_state 재로드: %s · %d종목",
            norm,
            reloaded,
        )
    except Exception as exc:
        logger.warning("매도 체결 positions_state 재로드 실패 (%s): %s", norm, exc)
    _apply_runtime_positions_from_store()
    _force_refresh_trade_state_sync("sell_fill_confirmed")
    _sync_positions_state()
    trade_state.request_dashboard_refresh(f"sell:{norm or code}")
    _refresh_balance_after_fill("sell", norm or code)


def _force_refresh_trade_state_sync(reason: str = "") -> None:
    """체결/수량 변동 직후 정산 지표를 메모리·trade_state 기준으로 강제 동기화."""
    try:
        trade_state.force_refresh_daily_state()
    except Exception as exc:
        logger.debug("일일 정산 강제 갱신 실패(%s): %s", reason, exc)
    _apply_trade_state_to_memory()


def _safe_notify_fill(
    side: str,
    code: str,
    name: str,
    qty: int,
    detail: str,
    *,
    price: int = 0,
    profit_pct: float | None = None,
) -> None:
    # 정식 운용: 장중(09:00~15:30) 체결 알림만 발송
    if not (_is_weekday() and _in_scan_window(datetime.now().time())):
        return
    try:
        _notifier.send_fill(
            side=side,
            code=code,
            name=name,
            qty=int(qty or 0),
            detail=detail,
            price=int(price or 0),
            profit_pct=profit_pct,
        )
    except Exception as exc:
        logger.debug("체결 알림 실패: %s", exc)


def _parse_hhmm_safe(value: str, default_h: int, default_m: int) -> dt_time:
    try:
        h, m = str(value).split(":")
        return dt_time(int(h), int(m))
    except Exception:
        return dt_time(default_h, default_m)


def _bot_allocation_metrics() -> dict[str, int]:
    """봇 슬롯 실투입·시장가치 — config 시드와 대조용."""
    seed = int(getattr(config, "ACCOUNT_TOTAL_SEED", 7_500_000))
    deployed = 0
    market_value = 0
    for pos in get_positions_snapshot():
        deployed += int(pos.get("deployed_won") or 0)
        qty = int(pos.get("quantity") or pos.get("qty") or 0)
        price = int(pos.get("current_price") or pos.get("price") or 0)
        if qty > 0 and price > 0:
            market_value += qty * price
        else:
            entry = int(pos.get("avg_price") or pos.get("entry_price") or 0)
            if qty > 0 and entry > 0:
                market_value += qty * entry
    return {
        "bot_operating_seed": seed,
        "bot_deployed_won": deployed,
        "bot_market_value_won": market_value,
    }


def _daily_summary_from_snapshot(
    snap: dict[str, Any],
    *,
    stats: dict[str, Any],
    slot_count: int,
) -> dict[str, Any]:
    """KIS 잔고 스냅샷 기반 장시작/장마감 영수증 지표."""
    stock_eval = int(snap.get("stock_eval") or snap.get("total_eval") or 0)
    cash = int(snap.get("cash") or 0)
    bot = _bot_allocation_metrics()
    return {
        "daily_eval_pnl": int(snap.get("daily_eval_pnl") or 0),
        "daily_eval_pnl_pct": float(snap.get("daily_eval_pnl_pct") or 0.0),
        "total_return_pct": float(snap.get("total_return_pct") or 0.0),
        "slot_count": slot_count,
        "stock_eval": stock_eval,
        "total_eval": stock_eval,
        "cash": cash,
        "account_total_eval": int(snap.get("account_total_eval") or 0),
        "trade_count": int(stats.get("trade_count") or 0),
        "realized_pnl": int(stats.get("total_pnl") or 0),
        **bot,
    }


def fetch_instant_daily_summary_payload(*, tag: str = "장마감 직후") -> dict[str, Any]:
    """
    /요약 등 즉시 조회용 — KIS 잔고·당일 정산을 강제 갱신 후 영수증 필드 반환.
    스케줄 발송 시간과 무관하게 현재 시점 스냅샷을 사용한다.
    """
    from ai_briefing import build_ai_briefing_payload

    try:
        trade_state.ensure_trade_state_file()
    except Exception:
        pass
    daily = trade_state.get_daily_realized_pnl(force_refresh=True)
    stats = {
        "trade_count": int(daily.get("today_trade_count") or 0),
        "total_pnl": int(daily.get("today_realized_pnl") or 0),
    }
    snap = _refresh_account_snapshot(force=True)
    with _state_lock:
        slot_count = int(_state.get("slot_count") or 0)
    payload = _daily_summary_from_snapshot(snap, stats=stats, slot_count=slot_count)
    payload["tag"] = tag
    payload["briefing"] = ""
    payload["news_links"] = []
    if bool(getattr(config, "ENABLE_AI_BRIEFING", False)):
        try:
            ai = build_ai_briefing_payload("국내 증시 장마감 브리핑", side=tag)
            payload["briefing"] = str(ai.get("briefing") or "")
            news = ai.get("news")
            payload["news_links"] = news if isinstance(news, list) else []
        except Exception as exc:
            logger.debug("즉시 요약 AI 브리핑 실패: %s", exc)
    return payload


def _summary_send_window_ok(
    now_dt: datetime,
    *,
    start: dt_time,
    window_min: int,
) -> bool:
    """지정 시각부터 window_min 분 사이에만 True (재시작·18시 이후 중복 발송 방지)."""
    now_t = now_dt.time()
    if now_t < start:
        return False
    close_dt = datetime.combine(now_dt.date(), start)
    end_t = (close_dt + timedelta(minutes=max(1, window_min))).time()
    return now_t < end_t


def _maybe_send_market_open_summary(now_dt: datetime) -> None:
    """장시작 요약(09:05 등) — 결산 영수증과 별도, 당일 1회."""
    global _last_open_summary_sent_date
    if not bool(getattr(config, "ENABLE_NOTIFICATIONS", False)):
        return
    if not _is_weekday():
        return
    today_key = now_dt.date().isoformat()
    if _last_open_summary_sent_date == today_key:
        return
    open_t = _parse_hhmm_safe(
        str(getattr(config, "NOTIFY_MARKET_OPEN_SUMMARY_TIME", "09:05")), 9, 5
    )
    window_min = int(getattr(config, "NOTIFY_MARKET_OPEN_SUMMARY_WINDOW_MIN", 10))
    if not _summary_send_window_ok(now_dt, start=open_t, window_min=window_min):
        return
    _last_open_summary_sent_date = today_key
    stats = get_daily_stats(force_refresh=True)
    with _state_lock:
        slot_count = int(_state.get("slot_count") or 0)
    snap = _refresh_account_snapshot(force=True)
    _notifier.send_daily_summary(
        tag="장시작 직후",
        **_daily_summary_from_snapshot(snap, stats=stats, slot_count=slot_count),
    )


def _maybe_send_daily_close_summary(now_dt: datetime) -> None:
    """
    일일 결산 영수증 — NOTIFY_MARKET_CLOSE_SUMMARY_TIME(기본 15:35)에 당일 1회만.
    18:00 리마인드·확인 버튼 재발송 없음.
    """
    global _last_close_summary_sent_date
    if not bool(getattr(config, "ENABLE_NOTIFICATIONS", False)):
        return
    if not _is_weekday():
        return
    today_key = now_dt.date().isoformat()
    if _last_close_summary_sent_date == today_key:
        return
    close_t = _parse_hhmm_safe(
        str(getattr(config, "NOTIFY_MARKET_CLOSE_SUMMARY_TIME", "15:35")), 15, 35
    )
    window_min = int(getattr(config, "NOTIFY_MARKET_CLOSE_SUMMARY_WINDOW_MIN", 10))
    if not _summary_send_window_ok(now_dt, start=close_t, window_min=window_min):
        return
    _last_close_summary_sent_date = today_key
    set_buy_pause(True, source="safe_exit")
    stats = get_daily_stats(force_refresh=True)
    settlement = None
    try:
        from account import get_daily_trade_settlement

        settlement = get_daily_trade_settlement()
    except Exception as exc:
        logger.debug("DB 정산 스냅샷 실패: %s", exc)
    if settlement:
        stats = {
            "trade_count": int(settlement.get("trade_count") or 0),
            "total_pnl": int(settlement.get("realized_pnl") or 0),
        }
    with _state_lock:
        slot_count = int(_state.get("slot_count") or 0)
    snap = _refresh_account_snapshot(force=True)
    _notifier.send_daily_summary(
        tag="장마감 직후",
        **_daily_summary_from_snapshot(snap, stats=stats, slot_count=slot_count),
    )
    _maybe_send_daily_close_report(now_dt)


def _maybe_send_daily_close_report(now_dt: datetime) -> None:
    """15:30 마감 직후 — DB 복기·지수·보유 MA·내일 전략 리포트."""
    global _last_close_report_sent_date
    if not bool(getattr(config, "ENABLE_DAILY_CLOSE_REPORT", True)):
        return
    if not _is_weekday():
        return
    today_key = now_dt.date().isoformat()
    if _last_close_report_sent_date == today_key:
        return
    report_t = _parse_hhmm_safe(
        str(getattr(config, "DAILY_CLOSE_REPORT_TIME", "15:31")), 15, 31
    )
    window_min = int(getattr(config, "DAILY_CLOSE_REPORT_WINDOW_MIN", 20))
    if not _summary_send_window_ok(now_dt, start=report_t, window_min=window_min):
        return
    _last_close_report_sent_date = today_key
    try:
        from daily_close_report import build_daily_close_report

        snap = _refresh_account_snapshot(force=True)
        holdings = get_positions_snapshot()
        report = build_daily_close_report(
            trade_date=today_key,
            holdings=holdings,
            account=snap,
        )
        with _state_lock:
            _state["daily_close_report"] = report
            _state["daily_close_report_date"] = today_key
        if bool(getattr(config, "ENABLE_NOTIFICATIONS", False)):
            _notifier.send_daily_close_report(report)
        trade_state.request_dashboard_refresh("daily_close_report")
        logger.info("장 마감 AI 리포트 생성 완료 (%s)", today_key)
    except Exception as exc:
        logger.exception("장 마감 리포트 생성 실패: %s", exc)


def get_daily_close_report() -> dict[str, Any] | None:
    """UI용 최신 장마감 리포트."""
    with _state_lock:
        rep = _state.get("daily_close_report")
        rep_date = _state.get("daily_close_report_date")
    if isinstance(rep, dict) and rep.get("markdown"):
        return dict(rep)
    today = date.today().isoformat()
    if rep_date == today and isinstance(rep, dict):
        return dict(rep)
    try:
        from daily_close_report import load_saved_report

        saved = load_saved_report(today)
        if saved:
            with _state_lock:
                _state["daily_close_report"] = saved
                _state["daily_close_report_date"] = today
            return saved
    except Exception:
        pass
    return None


def _maybe_preopen_session_boot(now_dt: datetime) -> None:
    """
    정식 무인 운용: 08:30 이후 최초 1회 세션/잔고 스냅샷 갱신.
    장중 시작 전 계좌/토큰 상태를 워밍업한다.
    """
    global _last_preopen_boot_date
    if not _is_weekday():
        return
    today = now_dt.date().isoformat()
    if _last_preopen_boot_date == today:
        return
    preopen_t = dt_time(8, 30)
    if now_dt.time() < preopen_t:
        return
    try:
        get_access_token()
        _refresh_account_snapshot(force=True)
        _sync_positions_state()
        _last_preopen_boot_date = today
        logger.info("08:30 사전 세션 갱신 완료")
    except Exception as exc:
        from auth import format_token_error

        logger.warning(
            "08:30 사전 세션 갱신 실패: %s", format_token_error(exc)
        )


def _reset_daily_stats_if_needed() -> None:
    """당일 영수증만 리셋 — 보유 포지션(positions_state.json)은 유지."""
    global _stats_date
    today = date.today()
    trade_state.ensure_trade_state_file()
    _apply_trade_state_to_memory()
    with _state_lock:
        file_date = _state.get("daily_stats_date")
    if _stats_date == today and file_date == today.isoformat():
        return
    _stats_date = today
    _apply_trade_state_to_memory()


def _bootstrap_positions_from_store() -> None:
    """메모리 포지션 스토어 → 스케줄러 _positions (기동·잔고 동기화 후)."""
    global _positions
    from slot_registry import reconcile_holdings_to_slots

    persisted = trade_state.load_persisted_positions()
    token: str | None = None
    try:
        token = get_access_token()
    except Exception:
        pass
    with _positions_lock:
        _positions = {}
        for code, pos in persisted.items():
            enriched = enrich_position(pos, access_token=token)
            _positions[code] = enriched
        for code, pos in list(_positions.items()):
            if not pos.get("stop_loss_price"):
                _backfill_missing_atr_stop(code, pos)
            if not pos.get("target_price"):
                _ensure_position_targets(code, pos)
            else:
                refresh_target_live_fields(pos)
    slots = trade_state.get_slots_book()
    with _positions_lock:
        snap = {c: dict(p) for c, p in _positions.items()}
    reconcile_holdings_to_slots(slots, snap)
    with _positions_lock:
        for code, pos in snap.items():
            if not isinstance(pos, dict):
                continue
            live = _positions.get(code)
            if not isinstance(live, dict):
                continue
            for key in (
                "slot_uid",
                "slot_id",
                "slot_type",
                "slot_personality",
                "display_idx",
                "trading_mode",
            ):
                if pos.get(key) is not None:
                    live[key] = pos.get(key)
    trade_state.save_portfolio_state(
        {c: dict(p) for c, p in _positions.items()},
        slots,
    )
    _sync_positions_state()


def _apply_runtime_positions_from_store() -> None:
    """account 잔고 동기화 직후 메모리 스토어 변경분을 _positions에 반영."""
    persisted = trade_state.load_persisted_positions()
    token: str | None = None
    try:
        token = get_access_token()
    except Exception:
        pass
    with _positions_lock:
        for code in list(_positions.keys()):
            if code not in persisted:
                del _positions[code]
        for code, pos in persisted.items():
            if code in _positions:
                _positions[code].update(dict(pos))
            else:
                enriched = enrich_position(dict(pos), access_token=token)
                _positions[code] = enriched
                if not enriched.get("stop_loss_price"):
                    _backfill_missing_atr_stop(code, enriched)
                if not enriched.get("target_price"):
                    _ensure_position_targets(code, enriched)
    _sync_positions_state()


def _load_positions_from_disk() -> None:
    """레거시 별칭 — 디스크 대신 메모리 스토어에서 부트스트랩."""
    _bootstrap_positions_from_store()


def _backfill_missing_atr_stop(code: str, pos: dict[str, Any]) -> None:
    """레거시/주입 포지션에 ATR 손절가가 없을 때 1H 봉으로 1회 보강."""
    entry = int(pos.get("entry_price") or 0)
    if entry <= 0:
        return
    period = int(getattr(config, "ATR_PERIOD", 14))
    mult = float(pos.get("atr_stop_mult") or getattr(config, "ATR_STOP_MULT", 2.0))
    min_pct = float(getattr(config, "ATR_MIN_LOSS_PCT", 3.0))
    max_pct = float(getattr(config, "ATR_MAX_LOSS_PCT", 15.0))
    try:
        token = get_access_token()
        bars = _fetch_hourly_bars(token, config.APP_KEY, config.APP_SECRET, code)
        ohlc = [
            {
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
            }
            for b in bars
        ]
        atr_val = wilder_atr(ohlc, period)
        if atr_val is None or atr_val <= 0:
            atr_val = max(1.0, entry * max_pct / 200.0)
        pos["atr_14"] = round(float(atr_val), 4)
        pos["stop_loss_price"] = atr_stop_price(
            entry,
            float(atr_val),
            mult=mult,
            min_loss_pct=min_pct,
            max_loss_pct=max_pct,
        )
        logger.info(
            "ATR 손절 백필 %s: ATR=%.4f → 손절가 %s",
            code,
            float(atr_val),
            pos["stop_loss_price"],
        )
    except Exception as exc:
        logger.warning("ATR 백필 실패 %s: %s — 보수적 손절 적용", code, exc)
        pos["stop_loss_price"] = atr_stop_price(
            entry,
            max(1.0, entry * max_pct / 200.0),
            mult=mult,
            min_loss_pct=min_pct,
            max_loss_pct=max_pct,
        )


def _persist_positions_to_store() -> None:
    token: str | None = None
    try:
        token = get_access_token()
    except Exception:
        pass
    with _positions_lock:
        snapshot = {
            code: enrich_position(pos, access_token=token)
            for code, pos in _positions.items()
        }
        _positions.update(snapshot)
    trade_state.save_persisted_positions(snapshot)


def _persist_positions_to_disk() -> None:
    """레거시 별칭."""
    _persist_positions_to_store()


def _maybe_persist_positions(force: bool = False) -> None:
    """고빈도 틱 시 메모리 반영 완화."""
    global _last_position_persist
    now = time.time()
    if force or now - _last_position_persist >= _POSITION_PERSIST_INTERVAL:
        _last_position_persist = now
        _persist_positions_to_store()


def _exit_type_label(reason: str, pnl: int) -> str:
    if "익절" in reason or "트레일링" in reason:
        return "익절"
    if "손절" in reason:
        return "손절"
    if pnl > 0:
        return "익절"
    if pnl < 0:
        return "손절"
    return "청산"


def _record_trade_exit(
    name: str,
    code: str,
    entry: int,
    qty: int,
    exit_price: int,
    profit_pct: float,
    reason: str,
) -> int:
    pnl = (exit_price - entry) * qty
    exit_type = _exit_type_label(reason, pnl)
    trade_state.record_completed_trade(
        name=name,
        pnl=pnl,
        profit_pct=profit_pct,
        exit_type=exit_type,
        code=code,
        sell_price=exit_price,
        quantity=qty,
    )
    _force_refresh_trade_state_sync("record_completed_trade")
    return pnl


def _refresh_ws_subscriptions() -> None:
    """보유 + 유니버스 상위 종목 WS 구독 동기화."""
    _sync_ws_watchlist()


def _realtime_ws_ready() -> bool:
    hub = _ws_hub
    return bool(
        hub is not None
        and getattr(config, "USE_REALTIME_WEBSOCKET", True)
        and hub.is_ready()
    )


def _ws_health_snapshot() -> dict[str, Any]:
    hub = _ws_hub
    if hub is None:
        with _state_lock:
            status = str(_state.get("ws_status") or "WS 미기동")
            err = _state.get("ws_last_error")
        return {
            "connected": False,
            "alive": False,
            "reconnecting": "재연결" in status or "시도" in status,
            "status_label": status,
            "seconds_since_rx": None,
            "seconds_since_trade": None,
            "heartbeat_timeout_sec": float(
                getattr(config, "WS_HEARTBEAT_TIMEOUT_SEC", 20.0)
            ),
            "last_error": err,
        }
    if hasattr(hub, "health_snapshot"):
        snap = hub.health_snapshot()
        with _state_lock:
            _state["ws_status"] = str(snap.get("status_label") or _state.get("ws_status"))
            if snap.get("last_error"):
                _state["ws_last_error"] = snap.get("last_error")
            elif snap.get("alive"):
                _state["ws_last_error"] = None
        return snap
    return {
        "connected": bool(getattr(hub, "is_connected", lambda: False)()),
        "alive": _realtime_ws_ready(),
        "reconnecting": False,
        "status_label": "WS 상태 확인 중",
        "seconds_since_rx": None,
        "seconds_since_trade": None,
        "heartbeat_timeout_sec": float(
            getattr(config, "WS_HEARTBEAT_TIMEOUT_SEC", 20.0)
        ),
        "last_error": hub.last_error() if hasattr(hub, "last_error") else None,
    }


def _sync_positions_state() -> None:
    global _last_synced_slot_count
    from slot_registry import build_slot_layout

    token: str | None = None
    try:
        token = get_access_token()
    except Exception:
        pass
    with _positions_lock:
        pos_by_code = {code: dict(p) for code, p in _positions.items()}
        count = len(_positions)
    slots = trade_state.get_slots_book()
    layout = build_slot_layout(pos_by_code, slots)
    snapshot: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cell in layout:
        pos = cell.get("position")
        if not isinstance(pos, dict):
            continue
        code = normalize_code(pos.get("code"))
        if len(code) != 6 or code in seen:
            continue
        seen.add(code)
        snapshot.append(enrich_position(pos, access_token=token))
    for code, pos in pos_by_code.items():
        norm = normalize_code(code)
        if len(norm) != 6 or norm in seen:
            continue
        seen.add(norm)
        snapshot.append(enrich_position(dict(pos), access_token=token))
    with _state_lock:
        _state["positions"] = snapshot
        _state["slot_layout"] = layout
        _state["slot_count"] = count
        _state["monitoring"] = count > 0
    if _last_synced_slot_count != count:
        _last_synced_slot_count = count
        _force_refresh_trade_state_sync("slot_count_changed")
    _refresh_ws_subscriptions()


def _set_engine_mode(mode: str) -> None:
    with _state_lock:
        _state["engine_mode"] = mode
        _state["scanning"] = mode in ("scanning", "swing_active")


def _merge_pyramid_into_position(
    code: str,
    add_qty: int,
    add_price: int,
    add_budget_won: int,
) -> None:
    """정찰 포지션에 피라미딩 체결 반영 (가중 평단)."""
    with _positions_lock:
        pos = _positions.get(code)
        if not pos:
            return
        old_qty = int(pos.get("quantity") or 0)
        old_entry = int(pos.get("entry_price") or 0)
        new_qty = old_qty + add_qty
        if new_qty <= 0:
            return
        new_entry = int(
            round((old_entry * old_qty + add_price * add_qty) / new_qty)
        )
        pos["quantity"] = new_qty
        pos["entry_price"] = new_entry
        pos["current_price"] = add_price
        pos["peak_price"] = max(int(pos.get("peak_price", new_entry)), add_price)
        pos["deployed_won"] = int(pos.get("deployed_won", 0)) + add_budget_won
        mode = position_trading_mode(pos)
        if mode == "swing":
            pos["swing_add_count"] = int(pos.get("swing_add_count") or 0) + 1
            max_adds = int(getattr(config, "SWING_MAX_SPLIT_BUYS", 3))
            pos["pyramid_done"] = int(pos["swing_add_count"]) >= max_adds
            pos["bet_label"] = f"스윙 분할매수 {pos['swing_add_count']}/{max_adds}"
        elif mode == "long_term":
            pos["dca_count"] = int(pos.get("dca_count") or 0) + 1
            pos["last_dca_at"] = datetime.now().isoformat()
            pos["bet_label"] = f"장투 적립 {pos['dca_count']}회"
        else:
            pos["pyramid_done"] = True
            pos["bet_label"] = "정찰+피라미딩"
        pos["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _maybe_persist_positions(force=True)
    _sync_positions_state()


def _process_pyramid_for_code(code: str) -> None:
    """모드별 추가 매수 — 스윙 분할·장투 적립 (단타 제외)."""
    pos = _get_all_positions().get(code)
    if not pos:
        return
    mode = position_trading_mode(pos)
    if mode == "day_trading":
        return
    if pos.get("pyramid_done") and mode != "long_term":
        return
    if pos.get("pending_order_ticket"):
        return
    token = get_access_token()
    cap = _capital_snapshot_with_pending()
    try:
        minute_bars = None
        hourly_bars = None
        if requires_minute_bars(pos):
            minute_bars = _get_scalp_minute_bars_cached(token, code)
        if requires_hourly_bars(pos):
            hourly_bars = _fetch_hourly_bars(
                token, config.APP_KEY, config.APP_SECRET, code
            )
        plan = plan_position_add(
            pos,
            minute_bars=minute_bars,
            hourly_bars=hourly_bars,
            capital=cap,
        )
        if not plan and mode == "swing" and str(pos.get("bet_tier")) == BetTier.SCOUT.value:
            minute_bars = minute_bars or _get_scalp_minute_bars_cached(token, code)
            plan = should_pyramid_add(pos, minute_bars, cap)
        if not plan:
            return
        add_qty = int(plan["add_qty"])
        price = int(pos.get("current_price") or pos.get("entry_price") or 0)
        if add_qty < 1 or price <= 0:
            return
        cap = get_capital_snapshot(_get_all_positions())
        if int(cap.get("available", 0)) < int(plan["add_budget_won"]):
            return
        order_info = _enqueue_pyramid_order(code, plan)
        if order_info:
            msg = (
                f"[피라미딩 접수] {pos.get('name')}({code}) +{add_qty}주 "
                f"· 티켓 {order_info['ticket_id']}"
            )
            _record_job_success(msg)
            logger.info(msg)
    except Exception as exc:
        logger.warning("피라미딩 실패 %s: %s", code, exc)


def _process_pyramid_additions() -> None:
    """정찰대 전 종목 피라미딩 스윕."""
    for code in list(_get_all_positions().keys()):
        _process_pyramid_for_code(code)


def _get_position_count() -> int:
    with _positions_lock:
        return len(_positions)


def _get_held_codes() -> set[str]:
    with _positions_lock:
        return set(_positions.keys())


def _get_all_positions() -> dict[str, dict[str, Any]]:
    with _positions_lock:
        return {code: dict(pos) for code, pos in _positions.items()}


def _ensure_position_targets(code: str, position: dict[str, Any]) -> None:
    """뇌 모드별 예상 매도가 — positions_state.json 저장 필드."""
    if int(position.get("target_price") or 0) > 0:
        refresh_target_live_fields(position)
        return
    hourly: list[dict] | None = None
    try:
        token = get_access_token()
        hourly = _fetch_hourly_bars(token, config.APP_KEY, config.APP_SECRET, code)
    except Exception as exc:
        logger.warning("예상 매도가용 1H 봉 조회 실패 %s: %s", code, exc)
    apply_expected_exit_to_position(position, hourly_bars=hourly)


def update_position_trading_mode(code: str, trading_mode: str) -> dict[str, Any]:
    return _apply_live_mode_to_position_code(code, trading_mode)


def _apply_live_mode_to_position_code(code: str, trading_mode: str) -> dict[str, Any]:
    """보유 종목 — 실시간 슬롯 성격을 포지션 전술(손절/익절/보유)에 즉시 반영."""
    norm = normalize_code(code)
    mode_value = _normalize_trading_mode(trading_mode, fallback=TradingMode.SWING.value)
    with _positions_lock:
        pos = _positions.get(norm)
        if not pos:
            return {"success": False, "message": "보유 포지션을 찾을 수 없습니다."}
        pos.update(_mode_tags_for_value(mode_value))
        pos["trading_mode"] = mode_value
        pos["slot_type"] = mode_value
        pos["slot_id"] = mode_value
        try:
            from selected_modes import save_mode

            save_mode(norm, str(trading_mode))
        except ImportError:
            pass
        pos["updated_at"] = _now_text()
        pos.pop("target_price", None)
        pos.pop("target_profit_pct", None)
        pos.pop("target_kind", None)
        pos.pop("target_note", None)
        pos.pop("target_display", None)
        pos.pop("target_ceiling_price", None)
        apply_tactical_fields_on_position(pos)
        apply_expected_exit_to_position(pos, hourly_bars=None, force_recalc=True)
        snapshot = dict(pos)
    _maybe_persist_positions(force=True)
    _sync_positions_state()
    _update_engine_mode_from_state()
    _wake_engine()
    return {
        "success": True,
        "message": f"{snapshot.get('name', norm)} 모드를 {snapshot.get('mode_label', '스윙')}로 변경했습니다.",
        "position": snapshot,
    }


def set_slot_personality(slot_idx: int, mode_label: str) -> dict[str, Any]:
    """
    슬롯 실시간 모드 변경 — empty/filled 공통.
    slots book → positions_state.json → (보유 시) 포지션 전술 즉시 갱신.
    """
    from slot_registry import (
        set_slot_personality_in_book,
        slot_uid_for_display_idx,
    )
    from trading_categories import normalize_trading_category

    idx = int(slot_idx)
    uid = slot_uid_for_display_idx(idx)
    if not uid:
        return {"success": False, "message": "슬롯 번호가 올바르지 않습니다."}

    label = str(mode_label or "").strip()
    from selected_modes import MODE_LABELS

    if label not in MODE_LABELS:
        label = MODE_LABEL_KO.get(TradingMode.SWING, "스윙")

    mode_value = normalize_trading_category(_normalize_trading_mode(label))

    slots = trade_state.get_slots_book()
    if not set_slot_personality_in_book(slots, uid, mode_value):
        return {"success": False, "message": f"슬롯 {idx} 성격 저장 실패"}

    try:
        from selected_modes import sync_slot_mode

        sync_slot_mode(idx, label, None)
    except ImportError:
        pass

    held_code: str | None = None
    entry = slots.get(uid) or {}
    if str(entry.get("status") or "empty") == "filled":
        held_code = str(entry.get("code") or "").strip() or None

    with _positions_lock:
        if held_code and held_code in _positions:
            pos = _positions[held_code]
            pos.update(_mode_tags_for_value(mode_value))
            pos["trading_mode"] = mode_value
            pos["slot_type"] = mode_value
            pos["slot_id"] = mode_value
            pos["slot_uid"] = uid
            pos["updated_at"] = _now_text()
            pos.pop("target_price", None)
            pos.pop("target_profit_pct", None)
            pos.pop("target_kind", None)
            pos.pop("target_note", None)
            pos.pop("target_display", None)
            pos.pop("target_ceiling_price", None)
            apply_tactical_fields_on_position(pos)
            apply_expected_exit_to_position(pos, hourly_bars=None, force_recalc=True)
        snap = {c: dict(p) for c, p in _positions.items()}

    trade_state.save_portfolio_state(snap, slots)
    _sync_positions_state()
    _update_engine_mode_from_state()
    _wake_engine()

    ko = MODE_LABEL_KO.get(
        next((m for m in TradingMode if m.value == mode_value), TradingMode.SWING),
        label,
    )
    return {
        "success": True,
        "message": f"슬롯 {idx} 실시간 모드 → {ko} ({mode_value})",
        "slot_uid": uid,
        "trading_mode": mode_value,
        "held_code": held_code,
    }


def _sync_position_live_mode_from_slot(code: str) -> None:
    """매도 판정 직전 — slots book 최신 성격으로 포지션 전술 동기화."""
    from slot_registry import resolve_live_trading_mode_for_position
    from trading_categories import normalize_trading_category

    norm = normalize_code(code)
    with _positions_lock:
        pos = _positions.get(norm)
        if not pos:
            return
        live_mode = normalize_trading_category(
            resolve_live_trading_mode_for_position(dict(pos))
        )
        cur = normalize_trading_category(pos.get("trading_mode"))
        if cur == live_mode and normalize_trading_category(pos.get("slot_type")) == live_mode:
            return
        pos.update(_mode_tags_for_value(live_mode))
        pos["trading_mode"] = live_mode
        pos["slot_type"] = live_mode
        pos["slot_id"] = live_mode
        apply_tactical_fields_on_position(pos)
        apply_expected_exit_to_position(pos, hourly_bars=None, force_recalc=True)
        pos["updated_at"] = _now_text()


def _add_position(position: dict[str, Any], *, slot_id: str | None = None) -> None:
    from slot_registry import assign_slot, slot_spec
    from trading_categories import normalize_trading_category

    code = position["code"]
    token: str | None = None
    try:
        token = get_access_token()
    except Exception:
        pass
    position = enrich_position(position, access_token=token)
    if not is_valid_korean_name(position.get("name"), code):
        position["name"] = resolve_stock_name(
            code, position.get("name"), access_token=token
        )
    position.setdefault("peak_price", position["entry_price"])
    position.setdefault("trailing_active", False)
    position.setdefault("trailing_stop_price", position["entry_price"])
    position.setdefault("entry_date", date.today().isoformat())
    position = _apply_trading_mode_override(
        position,
        position.get("trading_mode"),
        fallback=TradingMode.SWING.value,
    )
    position = apply_tactical_fields_on_position(position)
    mode = normalize_trading_category(position_trading_mode(position))
    position["trading_mode"] = mode
    if mode == "swing" and not position.get("stop_loss_price"):
        _backfill_missing_atr_stop(code, position)
    _ensure_position_targets(code, position)

    slot_uid = str(position.get("slot_uid") or slot_id or "").strip()
    slots = trade_state.get_slots_book()
    cat = mode
    if slot_uid:
        spec = slot_spec(slot_uid, slots) or {}
        if spec:
            cat = normalize_trading_category(spec.get("slot_personality") or spec.get("slot_type"))
            position["slot_uid"] = slot_uid
            position["slot_id"] = cat
            position["slot_type"] = cat
            position["slot_personality"] = cat
            position["display_idx"] = spec.get("display_idx")
            position["trading_mode"] = cat
            position.update(_mode_tags_for_value(cat))
        assign_slot(slots, slot_uid, code, trading_mode=cat)

    with _positions_lock:
        _positions[code] = position
    with _positions_lock:
        snapshot = {c: dict(p) for c, p in _positions.items()}
    trade_state.save_portfolio_state(snapshot, slots)
    _sync_positions_state()


def _build_position_payload(
    pick: dict[str, Any],
    bet_plan: dict[str, Any],
    quantity: int,
    fill_price: int,
    *,
    entry_basis: str,
) -> dict[str, Any]:
    pick = _apply_trading_mode_override(
        pick,
        pick.get("trading_mode"),
        fallback=TradingMode.SWING.value,
    )
    mode = str(pick.get("trading_mode") or "swing")
    return {
        "code": pick["code"],
        "name": pick["name"],
        "entry_price": fill_price,
        "quantity": quantity,
        "bet_tier": bet_plan.get("tier", "standard"),
        "bet_label": bet_plan.get("label", ""),
        "bet_conviction": bet_plan.get("conviction", 0),
        "deployed_won": int(fill_price * quantity),
        "target_deploy_won": int(bet_plan.get("target_deploy_won", fill_price * quantity)),
        "pyramid_done": False,
        "current_price": fill_price,
        "peak_price": fill_price,
        "trailing_active": False,
        "trailing_stop_price": fill_price,
        "profit_pct": 0.0,
        "entry_change_rate": pick.get("change_rate", 0.0),
        "swing_score": pick.get("swing_score"),
        "entry_date": date.today().isoformat(),
        "entry_basis": entry_basis,
        "updated_at": _now_text(),
        "atr_14": pick.get("atr_14"),
        "atr_stop_mult": pick.get("atr_stop_mult"),
        "stop_loss_price": get_entry_stop_loss(
            fill_price, position_trading_mode({**pick, "trading_mode": mode})
        ),
        "trading_mode": pick.get("trading_mode", "swing"),
        "mode_label": pick.get("mode_label", "스윙"),
        "mode_badge_class": pick.get("mode_badge_class", "mode-swing"),
        "mode_confidence": pick.get("mode_confidence"),
        "mode_hold_hint": pick.get("mode_hold_hint"),
        "mode_chart": pick.get("mode_chart"),
        "mode_policy": pick.get("mode_policy"),
        "mode_rationale": pick.get("mode_rationale"),
        "target_price": pick.get("target_price"),
        "target_profit_pct": pick.get("target_profit_pct"),
        "target_kind": pick.get("target_kind"),
        "target_note": pick.get("target_note"),
        "target_display": pick.get("target_display"),
        "target_ceiling_price": pick.get("target_ceiling_price"),
    }


def _note_partial_fill_progress(
    order: dict[str, Any],
    ticket_id: str,
    action: str,
    current_qty: int,
) -> None:
    """부분 체결 — 알림·잔고·UI 갱신 없이 주문 상태만 갱신."""
    from trading_logic import (
        cumulative_buy_fill_qty,
        cumulative_sell_fill_qty,
        has_partial_order_fill,
        partial_fill_progress_message,
    )

    if not has_partial_order_fill(action, order, current_qty):
        return
    act = str(action or "").strip().lower()
    if act in ("buy", "pyramid_buy"):
        filled = cumulative_buy_fill_qty(order, current_qty)
    else:
        filled = cumulative_sell_fill_qty(order, current_qty)
    prev_filled = int(order.get("filled_qty") or 0)
    if str(order.get("status") or "") == "partial_fill" and filled == prev_filled:
        return
    msg = partial_fill_progress_message(action, order, current_qty)
    _update_order_state(
        ticket_id,
        status="partial_fill",
        filled_qty=filled,
        message=msg,
    )
    logger.debug("부분 체결 진행 %s · %s (전량 체결 시 알림)", ticket_id, msg)


def _apply_confirmed_buy_fill(order: dict[str, Any], holding: dict[str, Any]) -> str:
    from trading_logic import cumulative_buy_fill_qty, order_requested_quantity

    code = normalize_code(order.get("code"))
    hold_qty = int(holding.get("quantity") or 0)
    requested = order_requested_quantity(order)
    fill_qty = cumulative_buy_fill_qty(order, hold_qty)
    if requested > 0:
        fill_qty = min(fill_qty, requested)
    fill_price = int(holding.get("avg_price") or order.get("reference_price") or 0)
    if fill_price <= 0:
        fill_price = int(order.get("reference_price") or 0)
    payload = _build_position_payload(
        dict(order.get("pick") or {}),
        dict(order.get("bet_plan") or {}),
        fill_qty,
        fill_price,
        entry_basis=str(order.get("entry_basis") or "async_fill_confirmed"),
    )
    slot_uid = str(
        order.get("slot_uid") or payload.get("slot_uid") or order.get("slot_id") or ""
    ).strip()
    _add_position(payload, slot_id=slot_uid or None)
    msg = (
        f"[체결완료] {payload['name']}({code}) {fill_qty}주 · "
        f"평단 {fill_price:,}원"
    )
    _record_job_success(msg)
    try:
        trade_state.save_trade_record(
            stock_name=str(payload.get("name") or code),
            side="buy",
            price=fill_price,
            quantity=fill_qty,
            stock_code=code,
        )
    except Exception as exc:
        logger.debug("매수 체결 DB 저장 스킵: %s", exc)
    _safe_notify_fill(
        code,
        str(payload.get("name") or code),
        fill_qty,
        f"평단 {fill_price:,}원 · 모드 {payload.get('mode_label', '-')}",
        price=fill_price,
        profit_pct=0.0,
    )
    _refresh_balance_after_fill("buy", code)
    return msg


def _apply_confirmed_pyramid_fill(order: dict[str, Any], holding: dict[str, Any]) -> str:
    from trading_logic import cumulative_buy_fill_qty, order_requested_quantity

    code = normalize_code(order.get("code"))
    hold_qty = int(holding.get("quantity") or 0)
    requested = order_requested_quantity(order)
    fill_qty = cumulative_buy_fill_qty(order, hold_qty)
    if requested > 0:
        fill_qty = min(fill_qty, requested)
    fill_price = int(holding.get("avg_price") or order.get("reference_price") or 0)
    add_budget = fill_qty * max(fill_price, 0)
    _merge_pyramid_into_position(code, fill_qty, fill_price, add_budget)
    _clear_position_order_pending(code, str(order.get("ticket_id")))
    msg = f"[피라미딩 체결] {holding.get('name', code)}({code}) +{fill_qty}주"
    _record_job_success(msg)
    try:
        trade_state.save_trade_record(
            stock_name=str(holding.get("name") or code),
            side="buy",
            price=fill_price,
            quantity=fill_qty,
            stock_code=code,
        )
    except Exception as exc:
        logger.debug("피라미딩 체결 DB 저장 스킵: %s", exc)
    _safe_notify_fill(
        "추가매수",
        code,
        str(holding.get("name") or code),
        fill_qty,
        f"피라미딩 체결 · 누적수량 {hold_qty}주",
        price=fill_price,
        profit_pct=None,
    )
    _refresh_balance_after_fill("pyramid_buy", code)
    return msg


def _apply_confirmed_sell_fill(order: dict[str, Any], remaining_qty: int) -> str:
    from trading_logic import cumulative_sell_fill_qty, order_requested_quantity

    code = normalize_code(order.get("code"))
    with _positions_lock:
        pos = dict(_positions.get(code) or {})
    if not pos:
        return f"[매도체결] {code}"

    baseline = int(order.get("baseline_qty") or pos.get("quantity") or 0)
    requested = order_requested_quantity(order) or baseline
    sold_qty = cumulative_sell_fill_qty(order, remaining_qty)
    if sold_qty <= 0:
        sold_qty = requested
    sold_qty = min(sold_qty, requested) if requested > 0 else sold_qty
    exit_price = int(order.get("estimated_fill_price") or pos.get("current_price") or pos.get("entry_price") or 0)
    entry = int(pos.get("entry_price") or 0)
    profit_pct = (exit_price - entry) / entry * 100 if entry else 0.0
    pnl = _record_trade_exit(
        str(pos.get("name") or code),
        code,
        entry,
        sold_qty,
        exit_price,
        profit_pct,
        str(order.get("reason") or "비동기 매도 체결"),
    )

    with _positions_lock:
        live = _positions.get(code)
        released_slot: str | None = None
        if live:
            live_qty = int(live.get("quantity") or 0)
            new_qty = max(0, live_qty - sold_qty)
            reason = str(order.get("reason") or "")
            if "장투 6월 작전 분할청산" in reason:
                done = int(live.get("long_force_exit_stage") or 0)
                live["long_force_exit_stage"] = done + 1
                live["long_force_exit_at"] = _now_text()
            if new_qty <= 0 or remaining_qty <= 0:
                released_slot = str(
                    live.get("slot_uid")
                    or order.get("slot_uid")
                    or order.get("slot_id")
                    or live.get("slot_id")
                    or ""
                ).strip() or None
                _positions.pop(code, None)
            else:
                live["quantity"] = remaining_qty
                live["updated_at"] = _now_text()
                live.pop("pending_order_ticket", None)
                live.pop("pending_order_action", None)
                live.pop("pending_order_at", None)
    if released_slot:
        from slot_registry import release_slot

        slots = trade_state.get_slots_book()
        release_slot(slots, released_slot)
        with _positions_lock:
            snap = {c: dict(p) for c, p in _positions.items()}
        trade_state.save_portfolio_state(snap, slots)
    else:
        _maybe_persist_positions(force=True)
    msg = (
        f"[매도체결] {pos.get('name', code)}({code}) {sold_qty}주 · "
        f"수익률 {profit_pct:+.2f}% · 실현손익 {pnl:+,}원"
    )
    _safe_notify_fill(
        "매도",
        code,
        str(pos.get("name") or code),
        sold_qty,
        f"실현손익 {pnl:+,}원 · 사유 {order.get('reason') or '-'}",
        price=exit_price,
        profit_pct=profit_pct,
    )
    _sync_after_sell_fill_confirmed(code)
    return msg


def _peak_profit_pct(entry: int, peak: int) -> float:
    if entry <= 0:
        return 0.0
    return (peak - entry) / entry * 100


def _tiered_drop_from_peak(peak_profit_pct: float) -> float | None:
    """
    고점 수익률 구간별 고점 대비 하락 허용폭(%).
    +5~10%: -2% | +10~20%: -3% | +20%↑: -5%
    """
    if peak_profit_pct < TRAILING_MIN_PEAK_PROFIT_PCT:
        return None
    if peak_profit_pct >= 20.0:
        return TRAILING_TIER3_DROP_PCT
    if peak_profit_pct >= 10.0:
        return TRAILING_TIER2_DROP_PCT
    return TRAILING_TIER1_DROP_PCT


def _trailing_tier_label(peak_profit_pct: float, drop_pct: float) -> str:
    if peak_profit_pct >= 20.0:
        return f"T3(+20%↑·고점-{drop_pct:.0f}%)"
    if peak_profit_pct >= 10.0:
        return f"T2(+10~20%·고점-{drop_pct:.0f}%)"
    return f"T1(+5~10%·고점-{drop_pct:.0f}%)"


def _calc_trailing_stop_price(entry: int, peak: int) -> tuple[int, float, float, str]:
    """(보존선 가격, 고점수익률%, 하락허용%, 구간라벨)"""
    peak_profit = _peak_profit_pct(entry, peak)
    drop_pct = _tiered_drop_from_peak(peak_profit)
    if drop_pct is None:
        return 0, peak_profit, 0.0, ""
    stop = int(peak * (1 - drop_pct / 100))
    label = _trailing_tier_label(peak_profit, drop_pct)
    return stop, peak_profit, drop_pct, label


def _update_position_market(code: str, price: int, profit_pct: float) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _positions_lock:
        if code not in _positions:
            return
        pos = _positions[code]
        entry = int(pos["entry_price"])
        pos["current_price"] = price
        pos["profit_pct"] = profit_pct
        pos["updated_at"] = now

        peak = max(int(pos.get("peak_price", entry)), price)
        pos["peak_price"] = peak

        stop_price, peak_profit, drop_pct, tier_label = _calc_trailing_stop_price(
            entry, peak
        )
        if drop_pct is not None:
            pos["trailing_active"] = True
            pos["peak_profit_pct"] = round(peak_profit, 2)
            pos["trailing_drop_pct"] = drop_pct
            pos["trailing_tier"] = tier_label
            pos["trailing_stop_price"] = stop_price
        refresh_target_live_fields(pos)
    _maybe_persist_positions(False)
    _sync_positions_state()


def _sell_position_by_code(
    code: str, reason: str, *, prefer_market: bool = False
) -> str | None:
    order = _enqueue_sell_order(code, reason, prefer_market=prefer_market)
    if not order:
        return None
    return f"[{reason}] {code} 매도 주문 접수 · 티켓 {order['ticket_id']}"


def _get_daily_bars_cached(token: str, code: str) -> list[dict[str, Any]]:
    """폴링 모드 일봉 — 종목별 TTL 캐시."""
    norm = normalize_code(code)
    ttl = float(getattr(config, "POLLING_DAILY_BARS_CACHE_SEC", 300))
    lookback = int(getattr(config, "POLLING_DAILY_LOOKBACK_DAYS", 90))
    now = time.time()
    cached = _daily_bars_cache.get(norm)
    if cached and now - cached[0] < ttl:
        return list(cached[1])
    from stock_daily import fetch_daily_ohlc_bars

    bars = fetch_daily_ohlc_bars(
        token,
        config.APP_KEY,
        config.APP_SECRET,
        norm,
        lookback_days=lookback,
    )
    _daily_bars_cache[norm] = (now, bars)
    return bars


def _polling_refresh_balance_if_due() -> None:
    """폴맹 모드 — 주기적 inquire_balance·포지션 동기화."""
    global _last_balance_poll_at
    if not is_polling_strategy_mode():
        return
    interval = float(getattr(config, "POLLING_BALANCE_INTERVAL_SEC", 30))
    if time.time() - _last_balance_poll_at < interval:
        return
    _last_balance_poll_at = time.time()
    try:
        _refresh_account_snapshot(
            force=False,
            sync_runtime_positions=True,
            bump_positions_revision=False,
        )
        _apply_runtime_positions_from_store()
    except Exception as exc:
        logger.debug("폴링 잔고 동기화 실패: %s", exc)


def _evaluate_positions() -> None:
    positions = _get_all_positions()
    if not positions:
        return

    token = get_access_token()
    to_sell: list[tuple[str, str]] = []

    for idx, (code, pos) in enumerate(positions.items()):
        if idx > 0:
            kis_loop_pause()
        try:
            quote = _quote_price(token, code)
            current = quote["price"]
            entry = pos["entry_price"]
            profit_pct = (current - entry) / entry * 100 if entry else 0.0
            _update_position_market(code, current, profit_pct)
            fresh = _get_all_positions().get(code)
            if not fresh:
                continue
            _sync_position_live_mode_from_slot(code)
            fresh = _get_all_positions().get(code) or fresh
            minute_bars = None
            hourly_bars = None
            daily_bars = None
            if requires_fast_tick_exit(fresh):
                minute_bars = _get_scalp_minute_bars_cached(token, code)
            if requires_hourly_bars(fresh) and not requires_daily_bars(fresh):
                try:
                    hourly_bars = _fetch_hourly_bars(
                        token, config.APP_KEY, config.APP_SECRET, code
                    )
                except Exception:
                    hourly_bars = None
            if requires_daily_bars(fresh):
                daily_bars = _get_daily_bars_cached(token, code)
            elif requires_hourly_bars(fresh) and is_polling_strategy_mode():
                daily_bars = _get_daily_bars_cached(token, code)
            reason = decide_position_exit(
                fresh,
                current,
                profit_pct,
                minute_bars=minute_bars,
                hourly_bars=hourly_bars,
                daily_bars=daily_bars,
            )
            if reason:
                to_sell.append((code, reason))
        except Exception as exc:
            logger.warning("감시 조회 실패 %s: %s", code, exc)

    for code, reason in to_sell:
        with _exit_eval_lock:
            if code not in _get_all_positions():
                continue
            msg = _sell_position_by_code(code, reason)
            if msg:
                _record_job_success(msg)
                logger.info(msg)


def _pending_reserved_slot_ids() -> set[str]:
    from trading_categories import migrate_legacy_slot_uid

    reserved: set[str] = set()
    with _orders_lock:
        for order in _orders.values():
            if not _is_open_order_status(str(order.get("status"))):
                continue
            if str(order.get("action")) not in {"buy", "pyramid_buy"}:
                continue
            raw = str(order.get("slot_uid") or order.get("slot_id") or "").strip()
            uid = migrate_legacy_slot_uid(raw) or raw
            if uid:
                reserved.add(uid)
    return reserved


def _resolve_buy_slot_id(pick: dict[str, Any]) -> str | None:
    """타입 일치 empty slot_uid — long_term / swing / day_trading."""
    from slot_registry import find_empty_slot_for_mode, is_slot_empty
    from trading_categories import migrate_legacy_slot_uid

    slots = trade_state.get_slots_book()
    reserved = _pending_reserved_slot_ids()
    mode = pick.get("trading_mode") or pick.get("selected_trading_mode")
    preferred = pick.get("slot_uid") or migrate_legacy_slot_uid(pick.get("slot_id"))
    ui_idx = pick.get("ui_slot_index")
    sid = find_empty_slot_for_mode(
        slots,
        mode,
        preferred_slot_uid=str(preferred) if preferred else None,
        preferred_display_idx=int(ui_idx) if ui_idx else None,
    )
    if sid and sid in reserved:
        sid = find_empty_slot_for_mode(slots, mode)
    if not sid or sid in reserved:
        return None
    if not is_slot_empty(slots, sid):
        return None
    return sid


def _empty_slots(*, trading_mode: str | None = None) -> int:
    from slot_registry import count_empty_slots, normalize_slot_type, slot_type_matches

    slots = trade_state.get_slots_book()
    reserved = _pending_reserved_slot_ids()
    mode_filter = normalize_slot_type(trading_mode) if trading_mode else None
    empty = count_empty_slots(slots, slot_type=mode_filter)
    if not reserved:
        return empty
    blocked = 0
    for sid in reserved:
        entry = slots.get(sid)
        if not entry or str(entry.get("status") or "empty") != "empty":
            continue
        if mode_filter and not slot_type_matches(str(entry.get("slot_type")), mode_filter):
            continue
        blocked += 1
    return max(0, empty - blocked)


def _capital_snapshot_with_pending() -> dict[str, int]:
    cap = get_capital_snapshot(_get_all_positions())
    reserved = _reserved_buy_budget_won()
    cap["reserved_orders"] = reserved
    cap["available"] = max(0, int(cap.get("available", 0)) - reserved)
    return cap


def _estimate_buy_intent(candidate: dict[str, Any]) -> dict[str, Any]:
    sample = _lock_ui_trading_mode(dict(candidate))
    sample["price"] = int(sample.get("price") or sample.get("current_price") or 0)
    if not sample.get("trading_mode"):
        sample = _apply_trading_mode_override(
            sample,
            sample.get("selected_trading_mode") or sample.get("trading_mode"),
            fallback=TradingMode.LONG_TERM.value,
        )
    cap = _capital_snapshot_with_pending()
    bet_plan = plan_entry(sample, cap)
    budget_won = int(bet_plan.get("budget_won") or 0)
    factor = float(sample.get("bet_budget_factor") or 1.0)
    if 0 < factor < 1.0:
        budget_won = int(budget_won * factor)
        bet_plan = dict(bet_plan)
        bet_plan["budget_won"] = budget_won
        bet_plan["label"] = f"{bet_plan.get('label', '')} · 테마 분할".strip(" ·")
    qty = quantity_for_budget(int(sample.get("price") or 0), budget_won)
    return {"bet_plan": bet_plan, "budget_won": budget_won, "quantity": qty, "pick": sample}


def _entry_stop_loss(pick: dict[str, Any], mode: str) -> int:
    stop_px = int(pick.get("stop_loss_price") or 0)
    if stop_px > 0:
        return stop_px
    price = int(pick.get("price") or 0)
    return get_entry_stop_loss(price, mode)


def _prepare_pick_for_entry(token: str, pick: dict[str, Any]) -> dict[str, Any]:
    """주문 직전 후보 정규화 — 이름/현재가/판단뇌 태그 보강."""
    pick = _lock_ui_trading_mode(dict(pick))
    code = normalize_code(pick.get("code"))
    if len(code) != 6:
        raise ValueError("유효한 6자리 종목코드가 아닙니다.")

    quote = _quote_price(token, code)
    prepared = dict(pick)
    prepared["code"] = code
    prepared["name"] = resolve_stock_name(
        code,
        pick.get("name") or quote.get("name"),
        access_token=token,
    )
    prepared["price"] = int(quote.get("price") or pick.get("price") or 0)
    prepared["change_rate"] = float(
        quote.get("change_rate") if quote.get("change_rate") is not None else pick.get("change_rate") or 0.0
    )
    ui_locked = bool(
        pick.get("ui_mode_locked")
        or pick.get("ui_slot_index")
        or pick.get("selected_trading_mode")
    )
    locked_mode = pick.get("selected_trading_mode") or pick.get("trading_mode")
    if ui_locked and locked_mode:
        prepared = enrich_stock_with_brain(prepared, auto_mode=False)
        prepared = _apply_trading_mode_override(
            prepared,
            locked_mode,
            fallback=str(locked_mode),
        )
        prepared["ui_mode_locked"] = True
        if pick.get("ui_slot_index"):
            prepared["ui_slot_index"] = int(pick["ui_slot_index"])
    else:
        brain = get_brain_classifier()
        prepared = brain.tag_stock(prepared)
        prepared = enrich_stock_with_brain(prepared, auto_mode=True)
    return prepared


def _execute_pick_entry(token: str, pick: dict[str, Any]) -> dict[str, Any] | None:
    """후보 1건 주문 제출. 포지션 반영은 체결 확인 후 처리."""
    pick = _prepare_pick_for_entry(token, pick)
    cap = _capital_snapshot_with_pending()
    bet_plan = plan_entry(pick, cap)
    budget_won = int(bet_plan.get("budget_won", 0))
    price = int(pick["price"])
    qty = quantity_for_budget(price, budget_won)
    if qty < 1 or budget_won <= 0:
        return None

    mode = str(pick.get("trading_mode") or "swing")
    use_market = mode == "day_trading" and getattr(
        config, "SCALP_USE_MARKET_ORDER", True
    )
    if use_market:
        result = buy_market_order(
            token, pick["code"], qty, config.APP_KEY, config.APP_SECRET
        )
    elif getattr(config, "SWING_USE_LIMIT_AT_CURRENT", True):
        result = buy_limit_order(
            token,
            pick["code"],
            qty,
            pick["price"],
            config.APP_KEY,
            config.APP_SECRET,
        )
    else:
        result = buy_market_order(
            token, pick["code"], qty, config.APP_KEY, config.APP_SECRET
        )

    mode_tag = pick.get("mode_label", "스윙")
    bet_tag = bet_plan.get("label", "")
    msg = (
        f"[{mode_tag}·{bet_plan.get('tier', '')}] {pick['name']}({pick['code']}) "
        f"{qty}주 · {budget_won:,}원 — {bet_tag} (주문 접수)"
    )
    if result.get("order_no"):
        msg += f" #{result['order_no']}"
    logger.info(msg)
    return {
        "message": msg,
        "mode_tag": mode_tag,
        "bet_plan": bet_plan,
        "order": result,
        "pick": pick,
        "quantity": qty,
        "budget_won": budget_won,
        "reference_price": price,
        "entry_basis": pick.get("entry_basis", "ui_manual_pick"),
    }


def _submit_exit_order(
    token: str,
    pos: dict[str, Any],
    *,
    reason: str,
    prefer_market: bool = False,
) -> dict[str, Any]:
    code = normalize_code(pos.get("code"))
    qty = int(pos.get("quantity") or 0)
    if qty <= 0:
        raise ValueError("매도 수량이 없습니다.")
    estimate_exit_price = int(pos.get("current_price") or pos.get("entry_price") or 0)
    if prefer_market:
        result = sell_market_order(token, code, qty, config.APP_KEY, config.APP_SECRET)
    else:
        result = sell_smart_sor(
            token,
            code,
            qty,
            config.APP_KEY,
            config.APP_SECRET,
            max_splits=int(getattr(config, "SOR_MAX_SPLITS", 3)),
        )
        estimate_exit_price = int(result.get("avg_price") or estimate_exit_price)
    return {
        "result": result,
        "estimate_exit_price": estimate_exit_price,
        "reason": reason,
        "quantity": qty,
    }


def _submit_pyramid_order(
    token: str,
    pos: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    code = normalize_code(pos.get("code"))
    add_qty = int(plan.get("add_qty") or 0)
    if add_qty <= 0:
        raise ValueError("피라미딩 수량이 없습니다.")
    result = buy_market_order(token, code, add_qty, config.APP_KEY, config.APP_SECRET)
    reference_price = int(pos.get("current_price") or pos.get("entry_price") or 0)
    return {
        "result": result,
        "quantity": add_qty,
        "budget_won": int(plan.get("add_budget_won") or 0),
        "reference_price": reference_price,
    }


def _enqueue_intent(intent: dict[str, Any]) -> dict[str, Any]:
    order = _create_order_state(intent)
    if str(order.get("action")) in {"sell", "pyramid_buy"}:
        _mark_position_order_pending(
            str(order.get("code")),
            str(order.get("ticket_id")),
            str(order.get("action")),
        )
    _order_queue.put({"ticket_id": order["ticket_id"]})
    _wake_engine()
    _wake_order_fill_poller()
    return order


def _enqueue_pick_entry(
    pick: dict[str, Any],
    *,
    source: str,
    entry_basis: str,
) -> dict[str, Any] | None:
    code = normalize_code(pick.get("code"))
    if len(code) != 6 or _is_code_order_pending(code, {"buy"}):
        return None
    if code in _get_held_codes():
        return None
    estimate = _estimate_buy_intent(pick)
    prepared_pick = dict(estimate.get("pick") or pick)
    slot_uid = _resolve_buy_slot_id(prepared_pick)
    if not slot_uid:
        return None
    from slot_registry import display_idx_for_slot_uid, slot_spec
    from trading_categories import normalize_trading_category

    slots = trade_state.get_slots_book()
    spec = slot_spec(slot_uid, slots) or {}
    cat = normalize_trading_category(
        spec.get("slot_personality") or spec.get("slot_type") or prepared_pick.get("trading_mode")
    )
    prepared_pick = _apply_trading_mode_override(prepared_pick, cat, fallback=cat)
    prepared_pick["slot_uid"] = slot_uid
    prepared_pick["slot_id"] = cat
    ui_idx = display_idx_for_slot_uid(slot_uid)
    if ui_idx:
        prepared_pick["ui_slot_index"] = ui_idx

    if is_polling_strategy_mode():
        if cat == "day_trading" and getattr(config, "POLLING_DISABLE_DAY_TRADING", True):
            logger.info("폴링 모드 — 단타 슬롯 진입 스킵 %s", code)
            return None
        try:
            token = get_access_token()
            daily = _get_daily_bars_cached(token, code)
            if not passes_polling_entry_ma_filter(cat, daily):
                logger.info(
                    "폴링 MA 미충족 — 진입 스킵 %s (%s)",
                    code,
                    prepared_pick.get("name") or code,
                )
                return None
        except Exception as exc:
            logger.warning("폴링 MA 검증 실패 %s: %s", code, exc)
            return None

    intent = {
        "action": "buy",
        "code": code,
        "name": prepared_pick.get("name") or code,
        "pick": prepared_pick,
        "source": source,
        "entry_basis": entry_basis,
        "budget_won": int(estimate["budget_won"]),
        "quantity": int(estimate["quantity"]),
        "bet_plan": dict(estimate["bet_plan"]),
        "trading_mode": prepared_pick.get("trading_mode"),
        "mode_label": prepared_pick.get("mode_label"),
        "baseline_qty": 0,
        "slot_uid": slot_uid,
        "slot_id": cat,
        "message": "주문 접수 대기",
    }
    return _enqueue_intent(intent)


def _enqueue_sell_order(
    code: str,
    reason: str,
    *,
    prefer_market: bool = False,
) -> dict[str, Any] | None:
    norm = normalize_code(code)
    if _is_code_order_pending(norm, {"sell"}):
        return None
    with _positions_lock:
        pos = dict(_positions.get(norm) or {})
    if not pos:
        return None
    qty_all = int(pos.get("quantity") or 0)
    sell_qty = qty_all
    if "장투 6월 작전 분할청산" in str(reason):
        total_tranches = max(1, int(getattr(config, "LONG_FORCE_SPLIT_TRANCHES", 5)))
        done = int(pos.get("long_force_exit_stage") or 0)
        remaining_tranches = max(1, total_tranches - done)
        if remaining_tranches == 1:
            sell_qty = qty_all
        else:
            sell_qty = max(1, qty_all // remaining_tranches)
    intent = {
        "action": "sell",
        "code": norm,
        "name": pos.get("name") or norm,
        "trading_mode": pos.get("trading_mode"),
        "mode_label": pos.get("mode_label"),
        "baseline_qty": qty_all,
        "quantity": int(sell_qty),
        "reason": reason,
        "prefer_market": prefer_market,
        "position": pos,
        "estimated_fill_price": int(pos.get("current_price") or pos.get("entry_price") or 0),
        "message": f"{reason} 주문 접수 대기",
    }
    return _enqueue_intent(intent)


def _enqueue_pyramid_order(code: str, plan: dict[str, Any]) -> dict[str, Any] | None:
    norm = normalize_code(code)
    if _is_code_order_pending(norm, {"pyramid_buy"}):
        return None
    with _positions_lock:
        pos = dict(_positions.get(norm) or {})
    if not pos:
        return None
    intent = {
        "action": "pyramid_buy",
        "code": norm,
        "name": pos.get("name") or norm,
        "trading_mode": pos.get("trading_mode"),
        "mode_label": pos.get("mode_label"),
        "baseline_qty": int(pos.get("quantity") or 0),
        "quantity": int(plan.get("add_qty") or 0),
        "budget_won": int(plan.get("add_budget_won") or 0),
        "position": pos,
        "pyramid_plan": dict(plan),
        "message": "피라미딩 주문 접수 대기",
    }
    return _enqueue_intent(intent)


def _order_worker_loop() -> None:
    while True:
        item = _order_queue.get()
        ticket_id = str(item.get("ticket_id") or "")
        with _orders_lock:
            order = dict(_orders.get(ticket_id) or {})
        if not order:
            _order_queue.task_done()
            continue

        try:
            _update_order_state(ticket_id, status="submitting", message="주문 제출 중")
            with order_priority_lane():
                token = get_access_token()
                action = str(order.get("action"))
                if action == "buy":
                    submitted = _execute_pick_entry(
                        token,
                        dict(
                            order.get("payload", {}).get("pick")
                            or order.get("pick")
                            or {}
                        ),
                    )
                    if not submitted:
                        raise RuntimeError("가용 시드가 부족하거나 주문 수량이 없습니다.")
                    _update_order_state(
                        ticket_id,
                        status="pending_fill",
                        message=str(submitted["message"]),
                        broker_order_no=str(submitted["order"].get("order_no") or ""),
                        quantity=int(submitted["quantity"]),
                        budget_won=int(submitted["budget_won"]),
                        pick=dict(submitted["pick"]),
                        bet_plan=dict(submitted["bet_plan"]),
                        reference_price=int(submitted.get("reference_price") or 0),
                        name=submitted["pick"].get("name") or order.get("name"),
                    )
                elif action == "sell":
                    submitted = _submit_exit_order(
                        token,
                        dict(
                            order.get("payload", {}).get("position")
                            or order.get("position")
                            or {}
                        ),
                        reason=str(order.get("reason") or "비동기 매도"),
                        prefer_market=bool(
                            order.get("payload", {}).get("prefer_market")
                            or order.get("prefer_market")
                        ),
                    )
                    _update_order_state(
                        ticket_id,
                        status="pending_fill",
                        message=f"{order.get('reason') or '매도'} 주문 접수",
                        broker_order_no=str(submitted["result"].get("order_no") or ""),
                        estimated_fill_price=int(submitted["estimate_exit_price"]),
                        quantity=int(submitted["quantity"]),
                    )
                elif action == "pyramid_buy":
                    submitted = _submit_pyramid_order(
                        token,
                        dict(
                            order.get("payload", {}).get("position")
                            or order.get("position")
                            or {}
                        ),
                        dict(
                            order.get("payload", {}).get("pyramid_plan")
                            or order.get("pyramid_plan")
                            or {}
                        ),
                    )
                    _update_order_state(
                        ticket_id,
                        status="pending_fill",
                        message="피라미딩 주문 접수",
                        broker_order_no=str(submitted["result"].get("order_no") or ""),
                        quantity=int(submitted["quantity"]),
                        budget_won=int(submitted["budget_won"]),
                        reference_price=int(submitted["reference_price"]),
                    )
                else:
                    raise RuntimeError(f"지원하지 않는 주문 액션: {action}")
        except Exception as exc:
            if is_rate_limit_error(exc):
                _update_order_state(
                    ticket_id,
                    status="rejected",
                    message="초당 제한으로 재시도 중... (실패)",
                )
            else:
                logger.exception("주문 제출 실패 %s", ticket_id)
                _update_order_state(ticket_id, status="rejected", message=str(exc))
            _clear_position_order_pending(str(order.get("code")), ticket_id)
        finally:
            _publish_order_snapshot()
            _order_queue.task_done()


def _poll_pending_orders_once() -> None:
    with _orders_lock:
        pending = [
            dict(v)
            for v in _orders.values()
            if _is_open_order_status(str(v.get("status")))
        ]
    if not pending:
        return

    try:
        account = _refresh_account_snapshot(
            force=True,
            sync_runtime_positions=False,
            bump_positions_revision=False,
            publish_to_state=False,
        )
    except Exception as exc:
        if is_rate_limit_error(exc):
            logger.debug("체결 폴러 속도 제한: %s", exc)
            _set_account_snapshot_stale("속도 제한 — 잠시 후 재확인")
            return
        logger.warning("계좌 스냅샷 갱신 실패: %s", exc)
        _set_account_snapshot_stale(str(exc))
        return

    holdings = dict(account.get("holdings") or {})

    from trading_logic import is_total_order_fill

    for order in pending:
        ticket_id = str(order.get("ticket_id"))
        code = normalize_code(order.get("code"))
        holding = dict(holdings.get(code) or {})
        action = str(order.get("action"))
        baseline = int(order.get("baseline_qty") or 0)
        current_qty = int(holding.get("quantity") or 0)

        if action == "buy":
            if current_qty <= baseline:
                continue
            if not is_total_order_fill(action, order, current_qty):
                _note_partial_fill_progress(order, ticket_id, action, current_qty)
                continue
            msg = _apply_confirmed_buy_fill(order, holding)
            _update_order_state(ticket_id, status="filled", message=msg)
            _force_refresh_trade_state_sync("buy_fill_detected")
        elif action == "pyramid_buy":
            if current_qty <= baseline:
                continue
            if not is_total_order_fill(action, order, current_qty):
                _note_partial_fill_progress(order, ticket_id, action, current_qty)
                continue
            msg = _apply_confirmed_pyramid_fill(order, holding)
            _update_order_state(ticket_id, status="filled", message=msg)
            _force_refresh_trade_state_sync("pyramid_fill_detected")
        elif action == "sell":
            if current_qty >= baseline:
                continue
            if not is_total_order_fill(action, order, current_qty):
                _note_partial_fill_progress(order, ticket_id, action, current_qty)
                continue
            msg = _apply_confirmed_sell_fill(order, current_qty)
            _update_order_state(ticket_id, status="filled", message=msg)
            _clear_position_order_pending(code, ticket_id)
            _publish_order_snapshot()


def _order_fill_loop() -> None:
    while True:
        _poll_pending_orders_once()
        if _has_pending_orders():
            if _order_fill_wake.wait(timeout=ORDER_FILL_POLL_ACTIVE_SEC):
                _order_fill_wake.clear()
        else:
            if _order_fill_wake.wait(timeout=ORDER_STATUS_POLL_SEC):
                _order_fill_wake.clear()


def _start_order_workers_if_needed() -> None:
    global _order_worker_thread, _order_fill_thread
    if _order_worker_thread is None or not _order_worker_thread.is_alive():
        _order_worker_thread = threading.Thread(
            target=_order_worker_loop,
            name="order-queue-worker",
            daemon=True,
        )
        _order_worker_thread.start()
    if _order_fill_thread is None or not _order_fill_thread.is_alive():
        _order_fill_thread = threading.Thread(
            target=_order_fill_loop,
            name="order-fill-poller",
            daemon=True,
        )
        _order_fill_thread.start()


def _run_market_scan_and_buy(*, force: bool = False) -> dict[str, Any]:
    """
    활성주 유니버스 실시간 스캔 — 단타(1m/3m) + 스윙(1H 눌림목) 병렬.
    force=True: UI 새로고침 등 — 슬롯·장중 여부와 무관하게 유니버스 탐색.
    """
    completed_at = datetime.now().strftime("%H:%M:%S")
    slots = _empty_slots()
    held = _get_held_codes()
    in_window = _is_weekday() and _in_scan_window(datetime.now().time())

    if not force and _buy_paused:
        return {
            "success": False,
            "skipped": True,
            "message": "신규 매수 일시정지 모드입니다.",
            "completed_at": completed_at,
        }

    if not force:
        if slots <= 0:
            return {
                "success": False,
                "skipped": True,
                "message": "빈 슬롯 없음",
                "completed_at": completed_at,
            }
        if not in_window:
            return {
                "success": False,
                "skipped": True,
                "message": "장외 시간",
                "completed_at": completed_at,
            }

    _record_job_start()
    _set_engine_mode("scanning" if force else "realtime_watch")

    try:
        global _scan_batch_cursor
        token = get_access_token()
        ranked = _get_universe_cached(token)
        universe_count = len(ranked)

        pick_limit = slots if slots > 0 else (MAX_SLOTS if force else 0)
        picks, scan_meta = select_market_entries(
            token,
            config.APP_KEY,
            config.APP_SECRET,
            ranked,
            exclude_codes=held,
            max_count=max(pick_limit, 1),
            batch_offset=_scan_batch_cursor,
            batch_size=REALTIME_SCAN_BATCH_SIZE,
        )
        if ranked:
            _scan_batch_cursor = (
                _scan_batch_cursor + REALTIME_SCAN_BATCH_SIZE
            ) % len(ranked)
        picks_count = len(picks)

        if not picks:
            summary = (
                f"[강제탐색] 유니버스 {universe_count}종목 · "
                f"후보 0 · 보유 {len(held)}/{MAX_SLOTS}"
                if force
                else (
                    f"후보 없음 (보유 {len(held)}/{MAX_SLOTS} · "
                    f"{'스윙/장투 MA' if is_polling_strategy_mode() else '실시간 단타+1H스윙'})"
                )
            )
            _mark_scan_completed(summary)
            if not force:
                _record_job_failure(
                    RuntimeError(
                        f"탐색 후보 없음 (보유 {len(held)}/{MAX_SLOTS} · "
                        f"단타·1H스윙 · 5일 평균 거래대금≥"
                        f"{getattr(config, 'SWING_MIN_AVG_TRADE_VALUE_5D', 10_000_000_000) // 100_000_000:,}억)"
                    )
                )
            else:
                _record_job_success(summary)
            return _attach_watch_fields({
                "success": True,
                "universe_count": universe_count,
                "picks_count": 0,
                "bought_count": 0,
                "summary": summary,
                "message": summary,
                "completed_at": completed_at,
                "error": None,
            })

        queued_msgs: list[str] = []
        can_buy = slots > 0 and (in_window or not force)
        if force and slots > 0 and not in_window:
            can_buy = False

        for pick in picks:
            if not can_buy:
                break
            if _empty_slots() <= 0:
                break
            if pick["code"] in _get_held_codes():
                continue
            if not is_common_stock_for_trade(pick):
                continue

            order_info = _enqueue_pick_entry(
                pick,
                source="force_scan" if force else "realtime_scan",
                entry_basis=str(pick.get("entry_basis") or "scan_queue"),
            )
            if not order_info:
                continue
            msg = (
                f"[접수] {pick.get('name', pick['code'])}({pick['code']}) "
                f"주문 티켓 {order_info['ticket_id']}"
            )
            queued_msgs.append(msg)
            if len(queued_msgs) >= slots:
                break

        if queued_msgs:
            alloc_txt = ""
            if scan_meta.get("allocation"):
                a = scan_meta["allocation"]
                alloc_txt = (
                    f" · AI비중 단타{a.get('scalp_weight', 0) * 100:.0f}%"
                )
            summary = (
                f"{len(queued_msgs)}종목 주문접수(단타·스윙) · "
                f"보유 {_get_position_count()}/{MAX_SLOTS} · "
                f"단타후보 {scan_meta.get('scalp_candidates', 0)} · "
                f"스윙후보 {scan_meta.get('swing_candidates', 0)}"
                f"{alloc_txt} · "
                + " | ".join(queued_msgs)
            )
            _record_job_success(summary)
            _mark_scan_completed(summary)
        elif force:
            names = ", ".join(p["name"] for p in picks[:5])
            if slots <= 0:
                buy_note = "슬롯 없음"
            elif not in_window:
                buy_note = "장외"
            else:
                buy_note = "조건 미충족"
            summary = (
                f"[강제탐색] 유니버스 {universe_count}종목 · "
                f"후보 {picks_count} ({names}) · 매수 0 ({buy_note})"
            )
            _record_job_success(summary)
            _mark_scan_completed(summary)
        else:
            _record_job_failure(RuntimeError("매수 가능한 종목 없음"))
            _mark_scan_completed("탐색 완료 · 매수 조건 미충족")
            summary = "탐색 완료 · 매수 조건 미충족"

        return _attach_watch_fields({
            "success": True,
            "universe_count": universe_count,
            "picks_count": picks_count,
            "bought_count": len(queued_msgs),
            "summary": summary,
            "message": summary,
            "completed_at": completed_at,
            "error": None,
        })
    except Exception as exc:
        _record_job_failure(exc)
        _mark_scan_completed(f"탐색 오류: {exc}")
        logger.exception("스윙 스캔/매수 실패")
        return _attach_watch_fields({
            "success": False,
            "universe_count": 0,
            "picks_count": 0,
            "bought_count": 0,
            "summary": str(exc),
            "message": str(exc),
            "completed_at": completed_at,
            "error": str(exc),
        })
    finally:
        global _last_scan_at
        _last_scan_at = time.time()
        _update_engine_mode_from_state()
        _sync_positions_state()


def _try_scan_and_buy() -> None:
    if not _begin_scan():
        return
    try:
        _run_market_scan_and_buy(force=False)
    finally:
        _end_scan()


def get_boot_scan_status() -> dict[str, Any]:
    """UI 스레드 — 기동 1회 스캔 진행 상태(블로킹 없음)."""
    with _boot_scan_lock:
        return {
            "status": _boot_scan_status,
            "result": dict(_boot_scan_result) if _boot_scan_result else None,
        }


def get_force_scan_status() -> dict[str, Any]:
    """UI 스레드 — F5 강제 탐색 진행 상태."""
    with _force_scan_lock:
        return {
            "running": _force_scan_running,
            "result": dict(_force_scan_result) if _force_scan_result else None,
            "epoch": _force_scan_result_epoch,
        }


def _immediate_boot_scan_worker() -> None:
    global _boot_scan_status, _boot_scan_result
    try:
        result = run_immediate_universe_scan()
        with _boot_scan_lock:
            _boot_scan_result = result
            _boot_scan_status = "done" if result.get("success") else "error"
        logger.info(
            "기동 백그라운드 스캔 완료 (success=%s)",
            result.get("success"),
        )
    except Exception as exc:
        logger.exception("기동 백그라운드 스캔 실패: %s", exc)
        err_result = {
            "success": False,
            "error": str(exc),
            "message": str(exc),
            "summary": f"기동 스캔 오류: {exc}",
        }
        with _boot_scan_lock:
            _boot_scan_result = _attach_watch_fields(err_result)
            _boot_scan_status = "error"


def start_immediate_boot_scan_background() -> None:
    """Streamlit UI와 분리 — 유니버스+AI+탐색 1회를 백그라운드에서 실행."""
    global _boot_scan_thread, _boot_scan_status
    with _boot_scan_lock:
        if _boot_scan_status in ("running", "done"):
            return
        if _boot_scan_thread is not None and _boot_scan_thread.is_alive():
            return
        _boot_scan_status = "running"
        _boot_scan_thread = threading.Thread(
            target=_immediate_boot_scan_worker,
            name="boot-universe-scan",
            daemon=True,
        )
        _boot_scan_thread.start()
        logger.info("기동 백그라운드 스캔 스레드 시작")


def _force_scan_worker() -> None:
    global _force_scan_running, _force_scan_result, _force_scan_result_epoch
    try:
        result = force_market_scan_from_ui()
        with _force_scan_lock:
            _force_scan_result = result
            _force_scan_result_epoch = time.time()
    except Exception as exc:
        logger.exception("F5 백그라운드 강제 탐색 실패: %s", exc)
        with _force_scan_lock:
            _force_scan_result = {
                "success": False,
                "error": str(exc),
                "message": str(exc),
            }
            _force_scan_result_epoch = time.time()
    finally:
        with _force_scan_lock:
            _force_scan_running = False


def request_force_market_scan_background() -> dict[str, Any]:
    """UI 트리거 — 강제 탐색을 백그라운드에서 실행."""
    global _force_scan_running
    with _force_scan_lock:
        if _force_scan_running:
            return {
                "accepted": False,
                "message": "이전 강제 탐색이 아직 진행 중입니다.",
            }
        _force_scan_running = True
    threading.Thread(
        target=_force_scan_worker,
        name="ui-force-universe-scan",
        daemon=True,
    ).start()
    return {"accepted": True, "message": "백그라운드 강제 탐색 시작"}


def _quick_ai_bootstrap_worker() -> None:
    """기동 직후 AI만 선반영 — 전황판 '시장 분석 대기 중' 즉시 해제."""
    stagger = float(getattr(config, "BOOT_API_STAGGER_SEC", 8))
    if stagger > 0:
        time.sleep(stagger)
    try:
        token = get_access_token()
        ai = refresh_ai_forecasts(
            access_token=token,
            commander_slots=_commander_slots_from_positions(),
            force=False,
        )
        _sync_ai_forecast_to_state(ai)
        logger.info("AI 선분석 완료 — %s", ai.get("daily", {}).get("narrative", "")[:60])
    except Exception as exc:
        logger.warning("AI 선분석 실패: %s", exc)


def run_immediate_universe_scan() -> dict[str, Any]:
    """
    Streamlit 기동 즉시 1회 — 장중·타이머·슬롯 조건 무시.
    1) 100억 유니버스 로드  2) AI 예측  3) 실시간 탐색(강제)
    """
    global _last_scan_at

    _last_scan_at = 0.0
    _set_engine_mode("scanning")
    token = get_access_token()
    try:
        ranked = _reload_universe_cache(token, refresh_ai=False)
        logger.info("기동 즉시 스캔 — 유니버스 %d종목 (거래대금·AI 캐시 주기 준수)", len(ranked))
    except Exception as exc:
        logger.exception("기동 즉시 유니버스/AI 실패: %s", exc)
        try:
            ai = refresh_ai_forecasts(
                access_token=token,
                commander_slots=_commander_slots_from_positions(),
                force=False,
            )
            _sync_ai_forecast_to_state(ai)
        except Exception as exc2:
            logger.warning("AI 단독 갱신 실패: %s", exc2)

    if not _begin_scan():
        summary = "유니버스·AI 완료 · 다른 탐색 진행 중"
        _mark_scan_completed(summary)
        with _state_lock:
            _state["running"] = True
        return _attach_watch_fields(
            {
                "success": True,
                "summary": summary,
                "message": summary,
                "skipped": True,
            }
        )

    try:
        result = _run_market_scan_and_buy(force=True)
    finally:
        _end_scan()

    with _state_lock:
        _state["running"] = True
    _update_engine_mode_from_state()
    _wake_engine()
    return _attach_watch_fields(result)


def force_market_scan_from_ui() -> dict[str, Any]:
    """브라우저 새로고침(F5) 등 UI 트리거 — 즉시 실시간 유니버스 탐색."""
    global _last_scan_at
    _last_scan_at = 0.0
    token = get_access_token()
    try:
        _reload_universe_cache(token, refresh_ai=False)
    except Exception as exc:
        logger.warning("F5 강제탐색 전 유니버스 갱신 실패: %s", exc)
    if not _begin_scan():
        return _attach_watch_fields(
            {
                "success": False,
                "message": "다른 탐색이 진행 중입니다",
                "skipped": True,
            }
        )
    try:
        result = _run_market_scan_and_buy(force=True)
    finally:
        _end_scan()
    _wake_engine()
    return _attach_watch_fields(result)


def _update_engine_mode_from_state() -> None:
    now_t = datetime.now().time()
    count = _get_position_count()
    if count >= MAX_SLOTS:
        _set_engine_mode("monitoring")
    elif count > 0:
        modes = {str(p.get("trading_mode") or "swing") for p in _get_all_positions().values()}
        if "day_trading" in modes:
            _set_engine_mode("scalp_watch")
        else:
            _set_engine_mode("swing_active")
    elif _in_scan_window(now_t) and _is_weekday():
        _set_engine_mode("realtime_watch")
    elif now_t >= SCAN_END or not _is_weekday():
        _set_engine_mode("off_hours")
    else:
        _set_engine_mode("off_hours")


def _handle_ws_tick(code: str, price: int) -> None:
    """H0STCNT0 체결 틱 → 엔진 깨우기 · 청산 · 정찰 피라미딩."""
    if not code or price <= 0:
        return
    _wake_engine()

    with _exit_eval_lock:
        pos = _get_all_positions().get(code)
        if pos:
            entry = int(pos.get("entry_price") or 0)
            if entry > 0:
                profit_pct = (price - entry) / entry * 100.0
                _update_position_market(code, price, profit_pct)
                fresh = _get_all_positions().get(code)
                if fresh:
                    _sync_position_live_mode_from_slot(code)
                    fresh = _get_all_positions().get(code) or fresh
                    minute_bars = None
                    hourly_bars = None
                    if requires_fast_tick_exit(fresh):
                        try:
                            token = get_access_token()
                            minute_bars = _get_scalp_minute_bars_cached(
                                token, code
                            )
                        except Exception:
                            pass
                    if requires_hourly_bars(fresh):
                        try:
                            token = get_access_token()
                            hourly_bars = _fetch_hourly_bars(
                                token,
                                config.APP_KEY,
                                config.APP_SECRET,
                                code,
                            )
                        except Exception:
                            hourly_bars = None
                    reason = decide_position_exit(
                        fresh,
                        price,
                        profit_pct,
                        minute_bars=minute_bars,
                        hourly_bars=hourly_bars,
                    )
                    if reason:
                        msg = _sell_position_by_code(code, reason)
                        if msg:
                            _record_job_success(msg)
                            logger.info(msg)
                        return

    held = _get_all_positions().get(code)
    if held and position_trading_mode(held) in ("swing", "long_term"):
        try:
            _process_pyramid_for_code(code)
        except Exception as exc:
            logger.warning("WS 분할매수 %s: %s", code, exc)
    elif (
        held
        and str(held.get("bet_tier")) == BetTier.SCOUT.value
        and not held.get("pyramid_done")
    ):
        try:
            _process_pyramid_for_code(code)
        except Exception as exc:
            logger.warning("WS 피라미딩 %s: %s", code, exc)


def _process_theme_timeline() -> None:
    """테마 디데이 — 분할 매수·청산 준비 (brain.py ThemeEngine)."""
    if not (_is_weekday() and _in_scan_window(datetime.now().time())):
        return
    positions = _get_all_positions()
    actions = plan_theme_actions(
        positions,
        empty_slots=_empty_slots(),
    )
    for act in actions:
        code = normalize_code(act.code)
        if len(code) != 6:
            continue
        if act.action == "prepare_exit":
            if code in positions and not _is_code_order_pending(code, {"sell"}):
                msg = _enqueue_sell_order(
                    code,
                    f"테마 디데이: {act.reason}",
                    prefer_market=False,
                )
                if msg:
                    logger.info(msg)
        elif act.action == "staged_buy" and _empty_slots() > 0:
            pick = next(
                (dict(u) for u in _universe_cache if normalize_code(u.get("code")) == code),
                None,
            )
            if not pick or _is_code_order_pending(code, {"buy"}):
                continue
            pick = dict(pick)
            pick["bet_budget_factor"] = act.budget_factor
            pick["entry_basis"] = "theme_staged_accumulate"
            order = _enqueue_pick_entry(
                pick,
                source="theme_timeline",
                entry_basis="theme_staged_accumulate",
            )
            if order:
                logger.info(
                    "테마 분할 매수 접수 %s · 티켓 %s · %s",
                    code,
                    order.get("ticket_id"),
                    act.reason,
                )
        elif act.action == "tighten_trail":
            with _positions_lock:
                pos = _positions.get(code)
                if pos:
                    pos["trailing_drop_pct"] = min(
                        float(pos.get("trailing_drop_pct") or 3.0),
                        1.5,
                    )
                    pos["theme_prepare_exit"] = True


def _run_realtime_cycle() -> None:
    """장중 1틱 — WS 체결 즉시 판정 · 빈 슬롯 롤링 탐색."""
    global _last_monitor_at, _last_scan_at

    positions = list(_get_all_positions().values())
    ws_ok = _realtime_ws_ready()
    polling = is_polling_strategy_mode()

    try:
        _process_theme_timeline()
    except Exception as exc:
        logger.warning("테마 타임라인 처리 실패: %s", exc)

    if polling:
        _polling_refresh_balance_if_due()

    need_poll_eval = positions and (polling or not ws_ok)
    if need_poll_eval:
        poll_interval = max(
            monitor_interval_for_positions(positions),
            float(MONITOR_INTERVAL_SEC),
        )
        if poll_interval <= 0:
            poll_interval = float(MONITOR_INTERVAL_SEC)
        if time.time() - _last_monitor_at >= poll_interval:
            _last_monitor_at = time.time()
            try:
                _evaluate_positions()
            except Exception as exc:
                logger.exception("포지션 감시 오류: %s", exc)
    elif positions and ws_ok:
        _last_monitor_at = time.time()

    if polling or not ws_ok:
        try:
            _process_pyramid_additions()
        except Exception as exc:
            logger.exception("피라미딩 처리 오류: %s", exc)

    if _should_run_realtime_scan():
        _set_engine_mode("scanning")
        try:
            _try_scan_and_buy()
        except Exception as exc:
            logger.exception("실시간 탐색 오류: %s", exc)
    else:
        _update_engine_mode_from_state()

    _sync_positions_state()
    if not polling:
        _sync_ws_watchlist()


def _realtime_engine_loop() -> None:
    """장중 실시간 무한 루프 — 1H 정각 대기 없음 · 자금 롤링 엔진."""
    global _last_scan_at

    banner_watch = get_recent_watch_hms()
    with _state_lock:
        _state["running"] = True
        _state["banner"] = build_status_banner_text(banner_watch)
        _state["max_slots"] = MAX_SLOTS

    _last_scan_at = 0.0

    logger.info(
        "실시간 롤링 엔진 기동 (%s~%s · 탐색 %ds · 틱 %.2fs)",
        SCAN_START_TIME,
        SCAN_END_TIME,
        int(REALTIME_SCAN_INTERVAL_SEC),
        REALTIME_ENGINE_TICK_SEC,
    )

    while True:
        _reset_daily_stats_if_needed()
        now_dt = datetime.now()
        _maybe_preopen_session_boot(now_dt)
        _maybe_send_market_open_summary(now_dt)
        _maybe_send_daily_close_summary(now_dt)
        _maybe_send_daily_close_report(now_dt)
        now_t = now_dt.time()
        ws_ok = _realtime_ws_ready()

        if not _is_weekday():
            _set_engine_mode("off_hours")
            _engine_wake.wait(timeout=5.0)
            _engine_wake.clear()
            continue

        if now_t < SCAN_START:
            _set_engine_mode("off_hours")
            _engine_wake.wait(timeout=2.0)
            _engine_wake.clear()
            continue

        if now_t >= SCAN_END:
            _set_engine_mode("off_hours")
            if _get_position_count() > 0 and not ws_ok:
                try:
                    _evaluate_positions()
                except Exception as exc:
                    logger.exception("장마감 후 감시 오류: %s", exc)
            _sync_positions_state()
            _engine_wake.wait(timeout=5.0)
            _engine_wake.clear()
            continue

        try:
            _run_realtime_cycle()
        except Exception as exc:
            logger.exception("실시간 엔진 틱 오류: %s", exc)

        if ws_ok and not is_polling_strategy_mode():
            _engine_wake.wait(timeout=WS_ENGINE_WAIT_SEC)
            _engine_wake.clear()
        else:
            tick = float(
                getattr(config, "REALTIME_ENGINE_TICK_SEC", 8.0)
                if is_polling_strategy_mode()
                else REALTIME_ENGINE_TICK_SEC
            )
            time.sleep(tick)


def start_background_scheduler() -> None:
    global _started, _engine_thread, _monitor_thread
    with _start_lock:
        if _started:
            return
        trade_state.ensure_trade_state_file()
        trade_state.ensure_positions_file()
        _load_positions_from_disk()
        _apply_trade_state_to_memory()
        _engine_thread = threading.Thread(
            target=_realtime_engine_loop,
            name="realtime-rolling-engine",
            daemon=True,
        )
        _engine_thread.start()
        _monitor_thread = _engine_thread
        _started = True
        _start_order_workers_if_needed()
        threading.Thread(
            target=_quick_ai_bootstrap_worker,
            name="ai-bootstrap",
            daemon=True,
        ).start()
        start_immediate_boot_scan_background()
        _start_realtime_websocket_if_enabled()
        start_discord_control_bot(
            str(getattr(config, "DISCORD_BOT_TOKEN", "")),
            str(getattr(config, "DISCORD_CHANNEL_ID", "")),
        )


def _start_realtime_websocket_if_enabled() -> None:
    global _ws_hub
    if not getattr(config, "USE_REALTIME_WEBSOCKET", True):
        label = (
            "폴링 모드 (스윙/장투 · "
            f"{getattr(config, 'POLLING_POSITION_INTERVAL_SEC', 8):.0f}초 감시)"
            if getattr(config, "POLLING_STRATEGY_MODE", False)
            else "WS 비활성 (REST 감시)"
        )
        with _state_lock:
            _state["ws_status"] = label
            _state["ws_last_error"] = None
        return
    try:
        from kis_ws import KisRealtimeHub

        def on_status(msg: str, err: str | None) -> None:
            with _state_lock:
                _state["ws_status"] = msg
                _state["ws_last_error"] = err

        hub = KisRealtimeHub(
            ws_url=getattr(
                config, "WS_BASE_URL", "ws://ops.koreainvestment.com:31000"
            ),
            rest_base=config.BASE_URL,
            app_key=config.APP_KEY,
            app_secret=config.APP_SECRET,
            on_trade=_handle_ws_tick,
            on_status=on_status,
            on_reconnected=_sync_balance_after_ws_reconnect,
            heartbeat_timeout_sec=float(
                getattr(config, "WS_HEARTBEAT_TIMEOUT_SEC", 20.0)
            ),
        )
        if hub.start():
            _ws_hub = hub
            hub.set_target_codes(_get_held_codes())
        else:
            _ws_hub = None
    except Exception as exc:
        logger.exception("실시간 웹소켓 기동 실패")
        _ws_hub = None
        with _state_lock:
            _state["ws_status"] = "WS 기동 실패"
            _state["ws_last_error"] = str(exc)


def set_buy_pause(paused: bool, *, source: str = "system") -> dict[str, Any]:
    global _buy_paused
    _buy_paused = bool(paused)
    with _state_lock:
        _state["buy_paused"] = _buy_paused
    _record_job_success(
        f"신규 매수 {'일시정지' if _buy_paused else '재개'} ({source})"
    )
    return {"paused": _buy_paused, "source": source}


def get_scheduler_status() -> dict[str, Any]:
    with _boot_scan_lock:
        boot_status = _boot_scan_status
    with _state_lock:
        out = dict(_state)
    out["boot_scan_status"] = boot_status
    out["scan_active"] = _scan_active
    out["ws_ready"] = _realtime_ws_ready()
    out["ws_health"] = _ws_health_snapshot()
    return out


def get_account_ui_snapshot() -> dict[str, Any]:
    with _state_lock:
        return dict(_state.get("account_snapshot") or {})


def get_order_status_snapshot() -> list[dict[str, Any]]:
    with _state_lock:
        rows = _state.get("order_status") or []
    return [dict(r) for r in rows if isinstance(r, dict)]


def get_order_events_snapshot() -> list[dict[str, Any]]:
    with _state_lock:
        rows = _state.get("order_events") or []
    return [dict(r) for r in rows if isinstance(r, dict)]


def get_positions_snapshot() -> list[dict[str, Any]]:
    with _state_lock:
        return [dict(p) for p in _state.get("positions", [])]


def get_slot_layout_snapshot() -> list[dict[str, Any]]:
    """display_idx 순 고정 5칸 — empty/filled + slot_id."""
    with _state_lock:
        rows = _state.get("slot_layout") or []
    if rows:
        return [dict(r) for r in rows if isinstance(r, dict)]
    from slot_registry import build_slot_layout

    with _positions_lock:
        pos_by_code = {code: dict(p) for code, p in _positions.items()}
    slots = trade_state.get_slots_book()
    return build_slot_layout(pos_by_code, slots)


def get_position_snapshot() -> dict[str, Any] | None:
    positions = get_positions_snapshot()
    return positions[0] if positions else None


def get_ui_universe_recommendations(limit: int = 40) -> list[dict[str, Any]]:
    """UI 수동 슬롯용 — brain 주도주·테마 우선 추천 풀."""
    with _state_lock:
        rows = list(_state.get("ui_slot_recommendations") or [])
    if rows:
        return [dict(r) for r in rows[: max(0, int(limit))] if isinstance(r, dict)]
    return pick_brain_recommendations(_universe_cache, limit=max(0, int(limit)))


def manual_buy_recommended_pick(
    candidate: dict[str, Any],
    *,
    slot_idx: int | None = None,
) -> dict[str, Any]:
    """UI 추천 슬롯 반자동 진입."""
    completed_at = datetime.now().strftime("%H:%M:%S")
    code = normalize_code(candidate.get("code"))
    if len(code) != 6:
        return {"success": False, "message": "추천 종목 코드가 올바르지 않습니다."}
    if code in _get_held_codes():
        return {"success": False, "message": "이미 보유 중인 종목입니다."}
    if not (_is_weekday() and _in_scan_window(datetime.now().time())):
        return {"success": False, "message": "장중에만 수동 매수가 가능합니다."}
    if not _begin_scan():
        return {"success": False, "message": "다른 스캔/주문이 진행 중입니다."}

    try:
        pick = dict(candidate)
        if slot_idx is not None and int(slot_idx) > 0:
            pick["ui_slot_index"] = int(slot_idx)
            pick["ui_mode_locked"] = True
        pick = _lock_ui_trading_mode(pick)
        selected_mode = str(
            pick.get("selected_trading_mode")
            or pick.get("trading_mode")
            or TradingMode.SWING.value
        )
        if slot_idx is not None and int(slot_idx) > 0:
            from slot_registry import slot_spec_for_display_idx, slot_type_matches

            spec = slot_spec_for_display_idx(int(slot_idx), trade_state.get_slots_book())
            sid = str(spec.get("slot_uid") or "") if spec else ""
            if spec and not slot_type_matches(spec.get("slot_type"), selected_mode):
                return {
                    "success": False,
                    "message": (
                        f"슬롯 {slot_idx}({spec.get('slot_id')})는 "
                        f"{spec.get('slot_type')} 전용입니다. 다른 타입 종목은 배치할 수 없습니다."
                    ),
                }
        if _empty_slots(trading_mode=selected_mode) <= 0:
            return {
                "success": False,
                "message": f"{selected_mode} 타입 빈 슬롯이 없습니다.",
            }
        selected_label = _mode_tags_for_value(selected_mode)["mode_label"]
        order_info = _enqueue_pick_entry(
            pick,
            source="manual_slot_buy",
            entry_basis="ui_manual_pick",
        )
        if not order_info:
            return {"success": False, "message": "가용 시드가 부족하거나 중복 주문 대기 중입니다."}
        msg = f"{selected_label} · 주문 티켓 {order_info['ticket_id']} 접수"
        _record_job_success(f"수동 진입 접수 · {msg}")
        _mark_scan_completed(f"수동 진입 접수 · {msg}")
        _wake_engine()
        return {
            "success": True,
            "message": msg,
            "completed_at": completed_at,
            "watch_hms": get_recent_watch_hms(),
            "ticket_id": order_info["ticket_id"],
            "mode_label": selected_label,
        }
    except Exception as exc:
        _record_job_failure(exc)
        logger.exception("UI 수동 매수 실패")
        return {
            "success": False,
            "message": str(exc),
            "completed_at": completed_at,
        }
    finally:
        _end_scan()


def preview_manual_pick_entry(
    candidate: dict[str, Any],
    *,
    slot_idx: int | None = None,
) -> dict[str, Any]:
    """UI 추천 슬롯용 예상 투입금·수량 미리보기."""
    code = normalize_code(candidate.get("code"))
    if len(code) != 6:
        return {"success": False, "message": "추천 종목 코드가 올바르지 않습니다."}
    try:
        pick = dict(candidate)
        if slot_idx is not None and int(slot_idx) > 0:
            pick["ui_slot_index"] = int(slot_idx)
            pick["ui_mode_locked"] = True
        pick = _lock_ui_trading_mode(pick)
        pick["code"] = code
        pick["price"] = int(pick.get("price") or pick.get("current_price") or 0)
        pick["name"] = resolve_stock_name(code, pick.get("name"))
        estimate = _estimate_buy_intent(pick)
        bet_plan = dict(estimate["bet_plan"])
        budget_won = int(estimate["budget_won"])
        qty = int(estimate["quantity"])
        mode_value = str(
            (estimate.get("pick") or {}).get("trading_mode")
            or pick.get("selected_trading_mode")
            or pick.get("trading_mode")
            or TradingMode.SWING.value
        )
        return {
            "success": True,
            "code": code,
            "name": pick.get("name"),
            "price": int(pick.get("price") or 0),
            "budget_won": budget_won,
            "quantity": qty,
            "tier": str(bet_plan.get("tier") or "standard"),
            "label": str(bet_plan.get("label") or ""),
            "conviction": float(bet_plan.get("conviction") or 0.0),
            "available": int(_capital_snapshot_with_pending().get("available") or 0),
            "trading_mode": mode_value,
            "mode_label": _mode_tags_for_value(mode_value)["mode_label"],
        }
    except Exception as exc:
        logger.warning("UI 수동 매수 미리보기 실패 %s: %s", code, exc)
        return {"success": False, "message": str(exc), "code": code}


def get_scan_timing() -> dict[str, Any]:
    """실시간 롤링 상태 (다음 1H 대기 없음)."""
    since = max(0, int(time.time() - _last_scan_at)) if _last_scan_at > 0 else 0
    with _state_lock:
        return {
            "realtime_scan_interval_sec": int(REALTIME_SCAN_INTERVAL_SEC),
            "seconds_since_scan": since,
            "last_scan_completed_at": _state.get("last_scan_completed_at"),
            "last_scan_completed_hms": _state.get("last_scan_completed_hms"),
            "last_scan_summary": _state.get("last_scan_summary"),
            "engine_mode": _state.get("engine_mode"),
        }


def get_daily_stats(*, force_refresh: bool = False) -> dict[str, Any]:
    trade_state.ensure_trade_state_file()
    if force_refresh:
        trade_state.force_refresh_daily_state()
    count, total, stats_date = trade_state.get_totals()
    _apply_trade_state_to_memory()
    return {
        "trade_count": count,
        "total_pnl": total,
        "stats_date": stats_date,
    }


def get_daily_trade_history() -> list[dict[str, Any]]:
    trade_state.ensure_trade_state_file()
    return trade_state.receipts_for_ui()


trade_state.ensure_trade_state_file()
trade_state.ensure_positions_file()


def emergency_liquidate_all() -> dict[str, Any]:
    with _emergency_lock:
        count_before = _get_position_count()
        if count_before == 0:
            _sync_positions_state()
            return {"success": True, "sold_count": 0, "messages": []}

        messages: list[str] = []
        _record_job_start()
        with _exit_eval_lock:
            for code in list(_get_all_positions().keys()):
                msg = _sell_position_by_code(code, "긴급 일괄 청산", prefer_market=True)
                if msg:
                    messages.append(msg)
                    logger.info(msg)
        if messages:
            _record_job_success(" | ".join(messages))
        return {
            "success": True,
            "sold_count": len(messages),
            "messages": messages,
            "errors": [],
        }


if __name__ == "__main__":
    import os
    import sys

    # Render 등 클라우드에서 stdout 버퍼링으로 로그가 지연되는 것 방지
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    try:
        print("🚀 scheduler.py 단독 실행 시작", flush=True)
        if not str(getattr(config, "APP_KEY", "") or "").strip():
            print(
                "🚨 APP_KEY 가 비어 있습니다. Render Dashboard → Environment 에 "
                "APP_KEY / APP_SECRET 를 등록하세요.",
                flush=True,
            )
        elif not str(getattr(config, "APP_SECRET", "") or "").strip():
            print(
                "🚨 APP_SECRET 가 비어 있습니다. Render Dashboard → Environment 에 "
                "APP_SECRET 를 등록하세요.",
                flush=True,
            )
        # KIS 연결 사전 점검: 인증/계좌/응답 메시지를 터미널에 강제 노출.
        try:
            token = get_access_token()
            print(f"✅ KIS 토큰 발급 성공 (len={len(str(token))})")
            snap = _refresh_account_snapshot(force=True)
            print(
                "✅ KIS 계좌 스냅샷 성공: "
                f"총평가={int(snap.get('total_eval') or 0):,}원 "
                f"현금={int(snap.get('cash') or 0):,}원 "
                f"보유={len((snap.get('holdings') or {}))}종목"
            )
            if snap.get("message"):
                print(f"ℹ️ KIS 응답 메시지: {snap.get('message')}")
        except Exception as e:
            from auth import format_token_error

            print(f"🚨 [치명적 에러 발생]: {format_token_error(e)}", flush=True)
            print("🚨 [KIS 연결 실패 상세]")
            traceback.print_exc()
            raise

        start_background_scheduler()
        print("✅ 백그라운드 스케줄러 기동 완료. Ctrl+C로 종료.")

        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("🛑 사용자 종료 요청으로 scheduler.py를 종료합니다.")
    except Exception as e:
        print(f"🚨 [치명적 에러 발생]: {e}")
        traceback.print_exc()
