"""
영구 저장 — trade_state.json (당일 청산 영수증).
보유 슬롯(positions)은 프로세스 메모리가 기준 — Render ephemeral FS 회피.
선택: POSITIONS_PERSIST_PATH 환경변수(Render Disk 등)에만 비동기 백업.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent
TRADE_STATE_FILE = PROJECT_DIR / "trade_state.json"
POSITIONS_STATE_FILE = PROJECT_DIR / "positions_state.json"  # 레거시 부트스트랩용

_lock = threading.RLock()
_runtime_positions: dict[str, dict[str, Any]] = {}
_runtime_revision: int = 0
_positions_bootstrapped: bool = False
_dashboard_refresh_nonce: int = 0
_dashboard_refresh_reason: str = ""
_dashboard_refresh_at: str = ""


def _positions_persist_path() -> Path | None:
    raw = os.environ.get("POSITIONS_PERSIST_PATH", "").strip()
    if raw:
        return Path(raw)
    return None


def _bump_positions_revision() -> None:
    global _runtime_revision
    _runtime_revision += 1


def get_positions_revision() -> int:
    """메모리 포지션 변경 카운터 — Streamlit st.session_state 동기화용."""
    with _lock:
        return int(_runtime_revision)


def get_positions_file_mtime() -> float:
    """레거시 호환 — revision을 float로 노출 (파일 mtime 미사용)."""
    return float(get_positions_revision())


def request_dashboard_refresh(reason: str = "fill") -> int:
    """체결 직후 Streamlit watchdog가 st.rerun() 하도록 nonce 증가."""
    global _dashboard_refresh_nonce, _dashboard_refresh_reason, _dashboard_refresh_at
    with _lock:
        _dashboard_refresh_nonce += 1
        _dashboard_refresh_reason = str(reason or "fill")
        _dashboard_refresh_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return int(_dashboard_refresh_nonce)


def get_dashboard_refresh_nonce() -> int:
    with _lock:
        return int(_dashboard_refresh_nonce)


def get_dashboard_refresh_meta() -> dict[str, Any]:
    with _lock:
        return {
            "nonce": int(_dashboard_refresh_nonce),
            "reason": _dashboard_refresh_reason,
            "at": _dashboard_refresh_at,
        }


def _monday_of(day: date | None = None) -> date:
    """해당 주 월요일 (주간 성적 기준일)."""
    d = day or date.today()
    return d - timedelta(days=d.weekday())


def _default_weekly(monday: date | None = None) -> dict[str, Any]:
    m = monday or _monday_of()
    return {
        "week_start": m.isoformat(),
        "realized_pnl": 0,
        "trade_count": 0,
        "trades": [],
    }


def _default_daily() -> dict[str, Any]:
    return {
        "date": date.today().isoformat(),
        "total_trade_count": 0,
        "total_realized_profit": 0,
        "completed_trades": [],
        "weekly": _default_weekly(),
    }


def _default_positions_file() -> dict[str, Any]:
    return {"positions": {}}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError, TypeError):
        return {}


def _read_positions_from_path(path: Path) -> dict[str, dict[str, Any]]:
    data = _load_json(path)
    raw = data.get("positions") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}


def _maybe_persist_positions_to_disk(positions: dict[str, dict[str, Any]]) -> None:
    """선택적 디스크 백업 — POSITIONS_PERSIST_PATH 설정 시에만."""
    path = _positions_persist_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _save_json(path, {"positions": positions})
    except OSError:
        pass


def _bootstrap_runtime_positions_once() -> None:
    global _positions_bootstrapped, _runtime_positions
    if _positions_bootstrapped:
        return
    _positions_bootstrapped = True
    loaded: dict[str, dict[str, Any]] = {}
    persist_path = _positions_persist_path()
    if persist_path is not None and persist_path.is_file():
        loaded = _read_positions_from_path(persist_path)
    elif POSITIONS_STATE_FILE.is_file():
        loaded = _read_positions_from_path(POSITIONS_STATE_FILE)
    if loaded:
        _runtime_positions = {k: dict(v) for k, v in loaded.items()}
        _bump_positions_revision()


def _normalize_daily_data(data: dict[str, Any]) -> dict[str, Any]:
    """재부팅·구버전 JSON — KeyError 방지용 필드 보강."""
    defaults = _default_daily()
    data.setdefault("date", defaults["date"])
    data.setdefault("total_trade_count", 0)
    data.setdefault("total_realized_profit", 0)
    data.setdefault("completed_trades", [])
    if not isinstance(data.get("completed_trades"), list):
        data["completed_trades"] = []
    data["total_trade_count"] = int(data.get("total_trade_count", 0) or 0)
    data["total_realized_profit"] = int(data.get("total_realized_profit", 0) or 0)
    return data


def _weekly_block(data: dict[str, Any]) -> dict[str, Any]:
    week = data.get("weekly")
    if not isinstance(week, dict):
        data["weekly"] = _default_weekly()
        return data["weekly"]
    return week


def _ensure_daily_unlocked() -> dict[str, Any]:
    """당일 영수증·합산만 관리 (날짜 변경 시 영수증 리셋, 보유는 별도 파일)."""
    today = date.today().isoformat()
    if not TRADE_STATE_FILE.is_file():
        data = _default_daily()
        _save_json(TRADE_STATE_FILE, data)
        return _ensure_ai_forecast_block(_ensure_weekly_block(data))

    data = _normalize_daily_data(_load_json(TRADE_STATE_FILE))
    if data.get("date") != today:
        preserved_weekly = data.get("weekly") if isinstance(data.get("weekly"), dict) else None
        preserved_ai_weekly = None
        ai_old = data.get("ai_forecast")
        monday_iso = _monday_of(date.today()).isoformat()
        if isinstance(ai_old, dict):
            w = ai_old.get("weekly")
            if isinstance(w, dict) and w.get("week_start") == monday_iso:
                preserved_ai_weekly = w
        data = _default_daily()
        if preserved_weekly and preserved_weekly.get("week_start") == monday_iso:
            data["weekly"] = preserved_weekly
        if preserved_ai_weekly:
            try:
                from market_ai import _default_ai_block

                data["ai_forecast"] = _default_ai_block()
                data["ai_forecast"]["weekly"] = preserved_ai_weekly
            except ImportError:
                pass
        _save_json(TRADE_STATE_FILE, data)
        data = _ensure_weekly_block(data)
        return _ensure_ai_forecast_block(data)

    data = _normalize_daily_data(data)
    data.setdefault("weekly", _default_weekly())
    data = _ensure_weekly_block(data)
    return _ensure_ai_forecast_block(data)


def _ensure_ai_forecast_block(data: dict[str, Any]) -> dict[str, Any]:
    try:
        from market_ai import _ensure_ai_block

        return _ensure_ai_block(data)
    except ImportError:
        return data


def _ensure_weekly_block(data: dict[str, Any]) -> dict[str, Any]:
    """주간 블록 유지 — 월요일이 바뀌면 주간 실현손익 리셋."""
    monday_iso = _monday_of(date.today()).isoformat()
    week = _weekly_block(data)
    if week.get("week_start") != monday_iso:
        data["weekly"] = _default_weekly()
        week = data["weekly"]
    week["realized_pnl"] = int(week.get("realized_pnl", 0))
    week["trade_count"] = int(week.get("trade_count", 0))
    if not isinstance(week.get("trades"), list):
        week["trades"] = []
    return data


def _parse_trade_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if " " in fmt else text[:10], fmt)
        except ValueError:
            continue
    return None


def ensure_trade_state_file() -> dict[str, Any]:
    with _lock:
        data = _ensure_daily_unlocked()
        return dict(data)


def ensure_positions_file() -> dict[str, Any]:
    """레거시 API — 메모리 포지션 스토어 초기화."""
    with _lock:
        _bootstrap_runtime_positions_once()
        return {"positions": {k: dict(v) for k, v in _runtime_positions.items()}}


def load_persisted_positions(*, force_reload: bool = False) -> dict[str, dict[str, Any]]:
    with _lock:
        if force_reload:
            global _positions_bootstrapped
            _positions_bootstrapped = False
            _bootstrap_runtime_positions_once()
        else:
            _bootstrap_runtime_positions_once()
        return {k: dict(v) for k, v in _runtime_positions.items()}


def save_persisted_positions(positions: dict[str, dict[str, Any]]) -> None:
    with _lock:
        _bootstrap_runtime_positions_once()
        _runtime_positions.clear()
        for code, pos in positions.items():
            if isinstance(pos, dict):
                _runtime_positions[str(code)] = dict(pos)
        _bump_positions_revision()
        snapshot = {k: dict(v) for k, v in _runtime_positions.items()}
    _maybe_persist_positions_to_disk(snapshot)


def sync_positions_from_broker_holdings(
    holdings: dict[str, dict[str, Any]],
) -> int:
    """
    KIS 잔고 holdings 기준으로 in-memory 포지션 전체 동기화.
    증권사에 없는 종목은 메모리에서 제거, 보유 종목은 수량·시세 반영.
    """
    with _lock:
        _bootstrap_runtime_positions_once()
        existing = {k: dict(v) for k, v in _runtime_positions.items()}
        synced: dict[str, dict[str, Any]] = {}
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for raw_code, holding in (holdings or {}).items():
            if not isinstance(holding, dict):
                continue
            code = str(raw_code or holding.get("code") or "").strip()
            code = "".join(ch for ch in code if ch.isdigit())[-6:]
            if len(code) != 6:
                continue
            qty = int(holding.get("quantity") or 0)
            if qty <= 0:
                continue

            prev = dict(existing.get(code) or {})
            current_price = int(
                holding.get("current_price") or prev.get("current_price") or 0
            )
            avg_price = int(holding.get("avg_price") or 0)
            entry_price = avg_price or int(prev.get("entry_price") or current_price or 0)
            name = str(holding.get("name") or prev.get("name") or code).strip()

            pos = dict(prev)
            pos.update(
                {
                    "code": code,
                    "name": name,
                    "quantity": qty,
                    "current_price": current_price,
                    "entry_price": entry_price,
                    "updated_at": now_text,
                    "display_name": prev.get("display_name")
                    or (f"{name} ({code})" if name != code else code),
                }
            )
            if current_price > 0 and entry_price > 0:
                pos["profit_pct"] = (current_price - entry_price) / entry_price * 100.0
            synced[code] = pos

        _runtime_positions.clear()
        _runtime_positions.update(synced)
        _bump_positions_revision()
        snapshot = {k: dict(v) for k, v in synced.items()}
    _maybe_persist_positions_to_disk(snapshot)
    return len(synced)


# 레거시 import 호환
sync_positions_file_from_broker_holdings = sync_positions_from_broker_holdings


def record_completed_trade(
    name: str,
    pnl: int,
    profit_pct: float,
    exit_type: str,
    sell_time: str | None = None,
    code: str = "",
) -> dict[str, Any]:
    with _lock:
        data = _ensure_daily_unlocked()
        sell_time = sell_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        receipt: dict[str, Any] = {
            "종목명": name,
            "종목코드": code,
            "매도시간": sell_time,
            "수익률": f"{profit_pct:+.2f}%",
            "수익금액": int(pnl),
            "청산구분": exit_type,
        }
        data.setdefault("completed_trades", []).append(receipt)
        data["total_trade_count"] = int(data.get("total_trade_count", 0)) + 1
        data["total_realized_profit"] = int(data.get("total_realized_profit", 0)) + int(pnl)

        week = _ensure_weekly_block(data)["weekly"]
        week["realized_pnl"] = int(week.get("realized_pnl", 0)) + int(pnl)
        week["trade_count"] = int(week.get("trade_count", 0)) + 1
        week.setdefault("trades", []).append(dict(receipt))

        _save_json(TRADE_STATE_FILE, data)
        return receipt


def get_totals() -> tuple[int, int, str]:
    with _lock:
        data = _ensure_daily_unlocked()
        return (
            int(data.get("total_trade_count", 0)),
            int(data.get("total_realized_profit", 0)),
            str(data.get("date", date.today().isoformat())),
        )


def get_completed_trades() -> list[dict[str, Any]]:
    with _lock:
        return [dict(r) for r in _ensure_daily_unlocked().get("completed_trades", [])]


def _parse_profit_pct(value: str | float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("%", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def get_daily_signature() -> tuple[str, int, int]:
    """5초 fragment 캐시 무효화 — 당일 청산(실현) 반영."""
    with _lock:
        data = _ensure_daily_unlocked()
        return (
            str(data.get("date", "")),
            int(data.get("total_realized_profit", 0)),
            int(data.get("total_trade_count", 0)),
        )


def get_daily_realized_pnl(*, force_refresh: bool = False) -> dict[str, Any]:
    """오늘 확정 실현손익 (trade_state.json — 새로고침·재부팅 후에도 유지)."""
    with _lock:
        if force_refresh:
            data = _normalize_daily_data(_load_json(TRADE_STATE_FILE))
            if data.get("date") != date.today().isoformat():
                data = _ensure_daily_unlocked()
            else:
                data.setdefault("weekly", _default_weekly())
                data = _ensure_weekly_block(data)
        else:
            data = _ensure_daily_unlocked()
        return {
            "stats_date": str(data.get("date", "")),
            "today_realized_pnl": int(data.get("total_realized_profit", 0)),
            "today_trade_count": int(data.get("total_trade_count", 0)),
        }


def force_refresh_daily_state() -> dict[str, Any]:
    """체결 직후 정산 스냅샷 강제 갱신."""
    return get_daily_realized_pnl(force_refresh=True)


def get_weekly_signature() -> tuple[str, int, int]:
    """5초 fragment 캐시 무효화용 — 청산 시 주간 실현 반영."""
    with _lock:
        data = _ensure_daily_unlocked()
        week = _weekly_block(data)
        return (
            str(week.get("week_start", "")),
            int(week.get("realized_pnl", 0)),
            int(week.get("trade_count", 0)),
        )


def _sync_weekly_from_today_receipts(data: dict[str, Any]) -> None:
    """주간 블록이 비었는데 당일 영수증에 이번 주 청산이 있으면 한 번 이관."""
    week = _weekly_block(data)
    if int(week.get("realized_pnl", 0)) != 0 or week.get("trades"):
        return
    monday_dt = datetime.strptime(week["week_start"], "%Y-%m-%d").date()
    for r in data.get("completed_trades") or []:
        dt = _parse_trade_datetime(r.get("매도시간", ""))
        if not dt or dt.date() < monday_dt:
            continue
        pnl = int(r.get("수익금액", 0))
        week["realized_pnl"] = int(week.get("realized_pnl", 0)) + pnl
        week["trade_count"] = int(week.get("trade_count", 0)) + 1
        week.setdefault("trades", []).append(dict(r))


def get_week_realized_summary() -> dict[str, Any]:
    """
    이번 주(월~금) 누적 실현손익만 — weekly 블록 JSON 영속.
    당일 영수증만 있고 weekly가 비었을 때 1회 동기화.
    """
    with _lock:
        data = _ensure_daily_unlocked()
        week = _weekly_block(data)
        before_realized = int(week.get("realized_pnl", 0))
        _sync_weekly_from_today_receipts(data)
        week = _weekly_block(data)
        if int(week.get("realized_pnl", 0)) != before_realized:
            _save_json(TRADE_STATE_FILE, data)
        realized = int(week.get("realized_pnl", 0))
        monday = str(week.get("week_start", _monday_of().isoformat()))
        return {
            "week_start": monday,
            "week_realized_pnl": realized,
            "week_trade_count": int(week.get("trade_count", 0)),
        }


def get_weekly_performance(
    unrealized_pnl: int,
    open_positions_cost: int,
) -> dict[str, Any]:
    """레거시 호환 — 주간 실현 요약."""
    summary = get_week_realized_summary()
    summary["week_unrealized_pnl"] = int(unrealized_pnl)
    summary["week_combined_pnl"] = summary["week_realized_pnl"] + int(unrealized_pnl)
    cost = int(open_positions_cost)
    if cost <= 0:
        try:
            import config

            cost = int(getattr(config, "AUTO_TRADE_TOTAL_BUDGET", 0) or 0)
        except ImportError:
            cost = 0
    summary["week_return_pct"] = round(
        summary["week_realized_pnl"] / cost * 100.0 if cost > 0 else 0.0, 2
    )
    return summary


def rebuild_weekly_from_trades(data: dict[str, Any] | None = None) -> None:
    """weekly.trades 기준으로 realized_pnl 재집계 (마이그레이션·검증용)."""
    with _lock:
        if data is None:
            data = _ensure_daily_unlocked()
        week = _ensure_weekly_block(data)["weekly"]
        monday_dt = datetime.strptime(week["week_start"], "%Y-%m-%d").date()
        total = 0
        trades: list[dict] = []
        for r in week.get("trades") or []:
            dt = _parse_trade_datetime(r.get("매도시간", ""))
            if dt and dt.date() >= monday_dt:
                pnl = int(r.get("수익금액", 0))
                total += pnl
                trades.append(r)
        week["realized_pnl"] = total
        week["trade_count"] = len(trades)
        week["trades"] = trades
        _save_json(TRADE_STATE_FILE, data)


_UI_MODE_LABELS = frozenset({"단타", "스윙", "장투"})


def _norm_ticker_key(key: str) -> str:
    text = str(key or "").strip()
    if len(text) == 6 and text.isdigit():
        return text
    if text.startswith("_slot_") and text[6:].isdigit():
        return text
    return ""


def load_ui_trading_modes() -> dict[str, str]:
    """F5·재기동 후에도 복구되는 종목별 매매 모드(한글 라벨) 장부."""
    with _lock:
        data = ensure_trade_state_file()
        raw = data.get("ui_trading_modes")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for key, label in raw.items():
            norm_key = _norm_ticker_key(str(key))
            norm_label = str(label or "").strip()
            if norm_key and norm_label in _UI_MODE_LABELS:
                out[norm_key] = norm_label
        return out


def save_ui_trading_mode(ticker_key: str, label: str) -> None:
    """종목코드(6자리) 또는 빈 슬롯 키(_slot_N)별 매매 모드 영속."""
    norm_key = _norm_ticker_key(ticker_key)
    norm_label = str(label or "").strip()
    if not norm_key or norm_label not in _UI_MODE_LABELS:
        return
    with _lock:
        data = _ensure_daily_unlocked()
        modes = data.setdefault("ui_trading_modes", {})
        if not isinstance(modes, dict):
            modes = {}
            data["ui_trading_modes"] = modes
        modes[norm_key] = norm_label
        _save_json(TRADE_STATE_FILE, data)


def receipts_for_ui() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in get_completed_trades():
        pnl = int(r.get("수익금액", 0))
        profit_pct = _parse_profit_pct(r.get("수익률", 0))
        rows.append({
            "name": r.get("종목명", "-"),
            "code": r.get("종목코드", ""),
            "time": r.get("매도시간", "-"),
            "pnl": pnl,
            "profit_pct": profit_pct,
            "exit_type": r.get("청산구분", "청산"),
            "수익률": r.get("수익률", f"{profit_pct:+.2f}%"),
            "수익금액": pnl,
            "종목명": r.get("종목명", "-"),
            "매도시간": r.get("매도시간", "-"),
        })
    return rows
