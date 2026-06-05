"""
당일 손절 종목 기록 — 매수 후보 '과거 손실 페널티'용 sell_history.json
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from config import PROJECT_DIR

logger = logging.getLogger(__name__)

SELL_HISTORY_FILE = PROJECT_DIR / "sell_history.json"
_lock = threading.Lock()


def _penalty_points() -> int:
    try:
        import config as cfg

        return int(getattr(cfg, "LOSS_REENTRY_PENALTY_POINTS", 50))
    except ImportError:
        return 50


def _empty_payload(day: str | None = None) -> dict[str, Any]:
    return {
        "date": day or date.today().isoformat(),
        "stops": [],
    }


def _load_raw() -> dict[str, Any]:
    if not SELL_HISTORY_FILE.is_file():
        return _empty_payload()
    try:
        data = json.loads(SELL_HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_payload()
    if not isinstance(data, dict):
        return _empty_payload()
    stops = data.get("stops")
    if not isinstance(stops, list):
        data["stops"] = []
    return data


def _save_raw(data: dict[str, Any]) -> None:
    try:
        SELL_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        SELL_HISTORY_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("sell_history.json 저장 실패: %s", exc)


def _normalize_code(code: object) -> str:
    text = "".join(ch for ch in str(code or "") if ch.isdigit())[-6:]
    return text if len(text) == 6 else ""


def _is_stop_loss_exit(exit_type: object, pnl: int | None = None) -> bool:
    label = str(exit_type or "")
    if "손절" in label:
        return True
    if pnl is not None and int(pnl) < 0 and label not in ("익절",):
        return False
    return "손절" in label


def _merge_stop_row(data: dict[str, Any], row: dict[str, Any]) -> None:
    code = _normalize_code(row.get("code") or row.get("stock_code"))
    if len(code) != 6:
        return
    stops: list[dict[str, Any]] = data.setdefault("stops", [])
    for existing in stops:
        if _normalize_code(existing.get("code")) == code:
            existing.update(row)
            return
    stops.append(row)


def bootstrap_today_from_db() -> set[str]:
    """trade_history.db 당일 손절 → sell_history.json 동기화."""
    today = date.today().isoformat()
    try:
        import trade_history_db as thdb

        thdb.init_trade_history_db()
        sells = thdb.list_trades_for_date(today, side="sell")
    except Exception as exc:
        logger.debug("sell_history DB bootstrap 스킵: %s", exc)
        return set()

    codes: set[str] = set()
    with _lock:
        data = _load_raw()
        if str(data.get("date") or "") != today:
            data = _empty_payload(today)
        for row in sells:
            exit_type = row.get("exit_type") or ""
            pnl = row.get("pnl")
            if not _is_stop_loss_exit(exit_type, int(pnl) if pnl is not None else None):
                continue
            code = _normalize_code(row.get("stock_code"))
            if len(code) != 6:
                continue
            codes.add(code)
            _merge_stop_row(
                data,
                {
                    "code": code,
                    "name": row.get("stock_name") or code,
                    "exit_type": exit_type or "손절",
                    "sell_time": row.get("trade_time") or today,
                    "pnl": int(pnl) if pnl is not None else None,
                    "profit_pct": row.get("profit_pct"),
                },
            )
        data["date"] = today
        _save_raw(data)
    return codes


def record_stop_loss_sell(
    *,
    code: str,
    name: str = "",
    exit_type: str = "손절",
    sell_time: str | None = None,
    pnl: int | None = None,
    profit_pct: float | None = None,
) -> None:
    """당일 손절 체결 시 sell_history.json 에 기록."""
    norm = _normalize_code(code)
    if len(norm) != 6:
        return
    ts = sell_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    day = ts[:10] if len(ts) >= 10 else date.today().isoformat()
    row = {
        "code": norm,
        "name": str(name or norm),
        "exit_type": str(exit_type or "손절"),
        "sell_time": ts,
        "pnl": int(pnl) if pnl is not None else None,
        "profit_pct": float(profit_pct) if profit_pct is not None else None,
    }
    with _lock:
        data = _load_raw()
        if str(data.get("date") or "") != day:
            data = _empty_payload(day)
        _merge_stop_row(data, row)
        data["date"] = day
        _save_raw(data)
    logger.info(
        "sell_history 기록 — 당일 손절 %s(%s) · %s",
        row["name"],
        norm,
        row["sell_time"],
    )


def get_today_stop_loss_codes(*, bootstrap_db: bool = True) -> set[str]:
    """오늘 손절한 종목 코드 집합."""
    today = date.today().isoformat()
    if bootstrap_db:
        bootstrap_today_from_db()
    with _lock:
        data = _load_raw()
    if str(data.get("date") or "") != today:
        return set()
    codes: set[str] = set()
    for row in data.get("stops") or []:
        if not isinstance(row, dict):
            continue
        if "손절" not in str(row.get("exit_type") or "손절"):
            continue
        code = _normalize_code(row.get("code"))
        if len(code) == 6:
            codes.add(code)
    return codes


def _pick_base_score(pick: dict[str, Any]) -> float:
    for key in ("entry_score", "swing_score", "scalp_score", "brain_score", "score"):
        val = pick.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return 0.0


def apply_loss_penalty_to_candidates(
    candidates: list[dict[str, Any]],
    *,
    penalty_points: int | None = None,
) -> list[dict[str, Any]]:
    """
    당일 손절 종목 — entry_score 에서 penalty 차감 → entry_score_final.
    """
    pts = _penalty_points() if penalty_points is None else int(penalty_points)
    stop_codes = get_today_stop_loss_codes()
    out: list[dict[str, Any]] = []
    for pick in candidates:
        row = dict(pick)
        code = _normalize_code(row.get("code"))
        base = _pick_base_score(row)
        penalty = pts if code in stop_codes else 0
        final = base - penalty
        row["entry_score_base"] = round(base, 2)
        row["entry_score"] = round(base, 2)
        row["loss_penalty"] = penalty
        row["entry_score_final"] = round(final, 2)
        if penalty > 0:
            row["loss_penalty_reason"] = f"당일 손절 재진입 -{penalty}점"
            logger.info(
                "과거 손실 페널티 -%d %s(%s) base=%.1f → final=%.1f",
                penalty,
                row.get("name") or code,
                code,
                base,
                final,
            )
        out.append(row)
    return out


def rank_candidates_by_final_score(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """페널티 반영 최종 점수 내림차순."""
    return sorted(
        candidates,
        key=lambda p: float(p.get("entry_score_final") or p.get("entry_score") or 0),
        reverse=True,
    )
