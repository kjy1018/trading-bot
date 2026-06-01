"""
매매 체결 영구 저장 — trade_history.db (sqlite3).

trade_state.json 삭제·손상과 무관하게 당일·주간 집계·영수증 UI를 복구한다.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_DIR / "trade_history.db"

_db_lock = threading.RLock()
_schema_ready = False


def trade_history_db_path() -> Path:
    raw = os.environ.get("TRADE_HISTORY_DB_PATH", "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_DB_PATH


def _connect() -> sqlite3.Connection:
    path = trade_history_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            trade_time TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            stock_code TEXT,
            side TEXT NOT NULL,
            price INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            pnl INTEGER,
            profit_pct REAL,
            exit_type TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trade_date);
        CREATE INDEX IF NOT EXISTS idx_trades_date_side ON trades(trade_date, side);
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS account_snapshots (
            snapshot_date TEXT PRIMARY KEY,
            account_total_eval INTEGER NOT NULL,
            stock_eval INTEGER NOT NULL DEFAULT 0,
            cash INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def init_trade_history_db() -> None:
    global _schema_ready
    with _db_lock:
        if _schema_ready:
            return
        with _connect() as conn:
            _ensure_schema(conn)
        _schema_ready = True


def save_trade_record(
    *,
    stock_name: str,
    side: str,
    price: int,
    quantity: int,
    trade_date: str | None = None,
    trade_time: str | None = None,
    stock_code: str = "",
    pnl: int | None = None,
    profit_pct: float | None = None,
    exit_type: str | None = None,
) -> int:
    """
    체결 1건 저장. side: 'buy' | 'sell' (또는 한글 매수/매도).
    반환: row id.
    """
    init_trade_history_db()
    now = datetime.now()
    day = trade_date or now.strftime("%Y-%m-%d")
    ts = trade_time or now.strftime("%Y-%m-%d %H:%M:%S")
    norm_side = _normalize_side(side)
    if norm_side not in ("buy", "sell"):
        raise ValueError(f"invalid side: {side}")
    qty = int(quantity or 0)
    px = int(price or 0)
    if qty <= 0 or px <= 0:
        raise ValueError("price and quantity must be positive")

    with _db_lock:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    trade_date, trade_time, stock_name, stock_code,
                    side, price, quantity, pnl, profit_pct, exit_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day,
                    ts,
                    str(stock_name or stock_code or "").strip() or str(stock_code),
                    str(stock_code or "").strip() or None,
                    norm_side,
                    px,
                    qty,
                    int(pnl) if pnl is not None and norm_side == "sell" else None,
                    float(profit_pct) if profit_pct is not None and norm_side == "sell" else None,
                    str(exit_type) if exit_type and norm_side == "sell" else None,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)


def _normalize_side(side: object) -> str:
    text = str(side or "").strip().lower()
    if text in ("buy", "b", "매수"):
        return "buy"
    if text in ("sell", "s", "매도"):
        return "sell"
    return text


def save_account_snapshot(
    account_total_eval: int,
    *,
    stock_eval: int = 0,
    cash: int = 0,
    snapshot_date: str | None = None,
) -> None:
    """당일 계좌 총자산 스냅샷 — 누적 수익 곡선용 (하루 1행, 최신 값으로 갱신)."""
    init_trade_history_db()
    day = snapshot_date or date.today().isoformat()
    total = int(account_total_eval or 0)
    if total <= 0:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO account_snapshots (
                    snapshot_date, account_total_eval, stock_eval, cash, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_date) DO UPDATE SET
                    account_total_eval = excluded.account_total_eval,
                    stock_eval = excluded.stock_eval,
                    cash = excluded.cash,
                    updated_at = excluded.updated_at
                """,
                (day, total, int(stock_eval or 0), int(cash or 0), ts),
            )
            conn.commit()


def list_account_snapshots() -> list[dict[str, Any]]:
    init_trade_history_db()
    with _db_lock:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT snapshot_date, account_total_eval, stock_eval, cash, updated_at
                FROM account_snapshots
                ORDER BY snapshot_date ASC
                """
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def aggregate_daily_realized_by_date() -> list[dict[str, Any]]:
    """날짜별 실현손익 합계 (매도 체결 기준)."""
    init_trade_history_db()
    with _db_lock:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    trade_date,
                    COALESCE(SUM(pnl), 0) AS daily_pnl,
                    COUNT(*) AS sell_count
                FROM trades
                WHERE side = 'sell'
                GROUP BY trade_date
                ORDER BY trade_date ASC
                """
            ).fetchall()
    return [
        {
            "trade_date": str(r["trade_date"]),
            "daily_pnl": int(r["daily_pnl"] or 0),
            "sell_count": int(r["sell_count"] or 0),
        }
        for r in rows
    ]


def build_performance_chart_series(
    *,
    initial_principal: int,
    live_account_total: int | None = None,
) -> dict[str, Any]:
    """
    대시보드 차트용 시계열.
    - equity: 날짜별 전체 자산 (스냅샷 우선, 없으면 원금+누적 실현손익)
    - daily_pnl: 날짜별 당일 실현손익
    """
    principal = max(0, int(initial_principal or 0))
    daily_rows = aggregate_daily_realized_by_date()
    snapshots = {str(s["snapshot_date"]): int(s["account_total_eval"] or 0) for s in list_account_snapshots()}

    dates: list[str] = sorted(
        {str(r["trade_date"]) for r in daily_rows} | set(snapshots.keys())
    )
    today = date.today().isoformat()
    if live_account_total and live_account_total > 0 and today not in dates:
        dates.append(today)
    if not dates and live_account_total and live_account_total > 0:
        dates = [today]
    dates = sorted(set(dates))

    pnl_by_date = {str(r["trade_date"]): int(r["daily_pnl"]) for r in daily_rows}
    cum_realized = 0
    equity_series: list[dict[str, Any]] = []
    bar_series: list[dict[str, Any]] = []

    for d in dates:
        cum_realized += pnl_by_date.get(d, 0)
        snap_total = snapshots.get(d)
        if snap_total and snap_total > 0:
            total_assets = snap_total
        else:
            total_assets = principal + cum_realized if principal > 0 else cum_realized

        if d == today and live_account_total and live_account_total > 0:
            total_assets = live_account_total

        equity_series.append(
            {"날짜": d, "전체 자산 (원)": int(total_assets)}
        )
        bar_series.append(
            {"날짜": d, "실현 손익 (원)": int(pnl_by_date.get(d, 0))}
        )

    return {
        "equity": equity_series,
        "daily_pnl": bar_series,
        "principal": principal,
    }


def aggregate_daily(trade_date: str | None = None) -> dict[str, Any]:
    """당일 매도 청산 건수·실현손익 합계."""
    init_trade_history_db()
    day = trade_date or date.today().isoformat()
    with _db_lock:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS sell_count,
                    COALESCE(SUM(pnl), 0) AS total_pnl
                FROM trades
                WHERE trade_date = ? AND side = 'sell'
                """,
                (day,),
            ).fetchone()
    sell_count = int(row["sell_count"] or 0) if row else 0
    total_pnl = int(row["total_pnl"] or 0) if row else 0
    return {
        "trade_date": day,
        "sell_count": sell_count,
        "total_pnl": total_pnl,
        "trade_count": sell_count,
    }


def aggregate_week(week_start: str | None = None) -> dict[str, Any]:
    """주간(월요일~일요일) 매도 실현손익."""
    init_trade_history_db()
    if week_start:
        start = datetime.strptime(week_start, "%Y-%m-%d").date()
    else:
        today = date.today()
        start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=7)
    start_s = start.isoformat()
    end_s = end.isoformat()
    with _db_lock:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS sell_count,
                    COALESCE(SUM(pnl), 0) AS total_pnl
                FROM trades
                WHERE side = 'sell'
                  AND trade_date >= ? AND trade_date < ?
                """,
                (start_s, end_s),
            ).fetchone()
    sell_count = int(row["sell_count"] or 0) if row else 0
    total_pnl = int(row["total_pnl"] or 0) if row else 0
    return {
        "week_start": start_s,
        "sell_count": sell_count,
        "total_pnl": total_pnl,
        "week_trade_count": sell_count,
        "week_realized_pnl": total_pnl,
    }


def list_trades_for_date(
    trade_date: str | None = None,
    *,
    side: str | None = None,
) -> list[dict[str, Any]]:
    init_trade_history_db()
    day = trade_date or date.today().isoformat()
    params: list[Any] = [day]
    sql = """
        SELECT * FROM trades
        WHERE trade_date = ?
    """
    if side:
        sql += " AND side = ?"
        params.append(_normalize_side(side))
    sql += " ORDER BY trade_time ASC, id ASC"
    with _db_lock:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_sell_receipts_for_date(trade_date: str | None = None) -> list[dict[str, Any]]:
    """UI·알림용 한글 영수증 형식."""
    out: list[dict[str, Any]] = []
    for row in list_trades_for_date(trade_date, side="sell"):
        pnl = int(row.get("pnl") or 0)
        pct = float(row.get("profit_pct") or 0.0)
        out.append(
            {
                "종목명": row.get("stock_name") or "-",
                "종목코드": row.get("stock_code") or "",
                "매도시간": row.get("trade_time") or row.get("trade_date") or "-",
                "수익률": f"{pct:+.2f}%",
                "수익금액": pnl,
                "청산구분": row.get("exit_type") or "청산",
            }
        )
    return out


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def migration_done(key: str) -> bool:
    init_trade_history_db()
    with _db_lock:
        with _connect() as conn:
            r = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
    return bool(r and str(r["value"]) == "1")


def _set_migration_done(key: str) -> None:
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, '1')
                ON CONFLICT(key) DO UPDATE SET value = '1'
                """,
                (key,),
            )
            conn.commit()


def migrate_completed_trades_from_json(receipts: list[dict[str, Any]]) -> int:
    """trade_state.json completed_trades → DB 1회 이전."""
    if migration_done("json_completed_trades_v1"):
        return 0
    imported = 0
    for r in receipts or []:
        if not isinstance(r, dict):
            continue
        dt_text = str(r.get("매도시간") or r.get("trade_time") or "")
        trade_date = dt_text[:10] if len(dt_text) >= 10 else date.today().isoformat()
        pnl = int(r.get("수익금액") or r.get("pnl") or 0)
        try:
            save_trade_record(
                stock_name=str(r.get("종목명") or r.get("name") or "unknown"),
                side="sell",
                price=max(1, abs(pnl)) if pnl else 1,
                quantity=1,
                trade_date=trade_date,
                trade_time=dt_text or None,
                stock_code=str(r.get("종목코드") or r.get("code") or ""),
                pnl=pnl,
                profit_pct=_parse_pct(r.get("수익률")),
                exit_type=str(r.get("청산구분") or "청산"),
            )
            imported += 1
        except (ValueError, sqlite3.Error):
            continue
    _set_migration_done("json_completed_trades_v1")
    return imported


def save_meta_json(key: str, payload: dict[str, Any]) -> None:
    """meta 테이블에 JSON 저장."""
    import json as _json

    init_trade_history_db()
    blob = _json.dumps(payload, ensure_ascii=False)
    with _db_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(key), blob),
            )
            conn.commit()


def load_meta_json(key: str) -> dict[str, Any] | None:
    init_trade_history_db()
    with _db_lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (str(key),)
            ).fetchone()
    if not row:
        return None
    try:
        import json as _json

        data = _json.loads(str(row["value"] or "{}"))
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


def _parse_pct(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value or "").strip().replace("%", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None
