"""
trade_history.db 체결 이력 → 매수 시점 차트 패턴 CSV.
라벨: label_stop_loss 1=손절/손실, 0=익절·수익
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from config import APP_KEY, APP_SECRET, PROJECT_DIR
from ml_stop_loss.features import (
    FEATURE_NAMES,
    extract_chart_features,
    fetch_hourly_bars_at_entry,
    features_to_vector,
)

logger = logging.getLogger(__name__)

DEFAULT_DATASET_PATH = PROJECT_DIR / "data" / "trade_pattern_dataset.csv"
DEFAULT_SEED_PATH = PROJECT_DIR / "data" / "ml_trade_seed.json"
DB_PATH = PROJECT_DIR / "trade_history.db"

CSV_COLUMNS = [
    "sample_id",
    "stock_code",
    "stock_name",
    "buy_time",
    "buy_price",
    "sell_time",
    "sell_price",
    "pnl",
    "profit_pct",
    "exit_type",
    "label_stop_loss",
    "trading_mode",
    "augment_offset_bars",
    *FEATURE_NAMES,
]


def _parse_dt(text: str) -> datetime | None:
    text = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _label_from_sell(sell: dict[str, Any]) -> int:
    exit_type = str(sell.get("exit_type") or "")
    if "손절" in exit_type:
        return 1
    pnl = sell.get("pnl")
    if pnl is not None and int(pnl) < 0:
        return 1
    pct = sell.get("profit_pct")
    if pct is not None and float(pct) < 0:
        return 1
    return 0


def _load_trades() -> list[dict[str, Any]]:
    if not DB_PATH.is_file():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY trade_time ASC, id ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_seed_trades() -> list[dict[str, Any]]:
    if not DEFAULT_SEED_PATH.is_file():
        return []
    try:
        raw = json.loads(DEFAULT_SEED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    return []


def _group_buy_sell_pairs(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """종목코드별 매수→매도 쌍 (FIFO)."""
    buys: dict[str, list[dict[str, Any]]] = {}
    pairs: list[dict[str, Any]] = []

    for row in trades:
        side = str(row.get("side") or "").lower()
        code = str(row.get("stock_code") or "").strip()
        if side == "buy":
            if len(code) == 6:
                buys.setdefault(code, []).append(row)
            continue
        if side != "sell":
            continue

        label = _label_from_sell(row)
        if len(code) == 6 and buys.get(code):
            buy = buys[code].pop(0)
            pairs.append({"buy": buy, "sell": row, "label": label})
        else:
            pairs.append({"buy": None, "sell": row, "label": label})

    return pairs


def _augment_bars(bars: list[dict], offset: int) -> list[dict]:
    if offset <= 0 or len(bars) <= offset:
        return bars
    return bars[:-offset]


def _row_from_pair(
    pair: dict[str, Any],
    *,
    token: str | None,
    app_key: str,
    app_secret: str,
    augment_offset: int = 0,
    sample_suffix: str = "",
) -> dict[str, Any] | None:
    buy = pair.get("buy")
    sell = pair["sell"]
    code = ""
    buy_time = ""
    buy_price = 0
    if buy:
        code = str(buy.get("stock_code") or "").strip()
        buy_time = str(buy.get("trade_time") or "")
        buy_price = int(buy.get("price") or 0)
    else:
        code = str(sell.get("stock_code") or "").strip()

    if len(code) != 6:
        return None

    entry_dt = _parse_dt(buy_time) or _parse_dt(str(sell.get("trade_time") or ""))
    if entry_dt is None:
        return None

    hourly: list[dict] = []
    if token and app_key and app_secret:
        try:
            hourly = fetch_hourly_bars_at_entry(
                token, app_key, app_secret, code, entry_dt
            )
        except Exception as exc:
            logger.warning("차트 조회 실패 %s: %s", code, exc)

    hourly = _augment_bars(hourly, augment_offset)
    pick = {
        "code": code,
        "name": (buy or sell).get("stock_name"),
        "price": buy_price or int(sell.get("price") or 0),
        "change_rate": 0.0,
    }
    feats = extract_chart_features(
        hourly,
        pick=pick,
        entry_dt=entry_dt,
        entry_price=buy_price,
    )

    sample_id = f"{code}_{buy_time or sell.get('trade_time')}{sample_suffix}"
    row: dict[str, Any] = {
        "sample_id": sample_id.replace(" ", "_").replace(":", ""),
        "stock_code": code,
        "stock_name": str((buy or sell).get("stock_name") or code),
        "buy_time": buy_time,
        "buy_price": buy_price,
        "sell_time": str(sell.get("trade_time") or ""),
        "sell_price": int(sell.get("price") or 0),
        "pnl": sell.get("pnl"),
        "profit_pct": sell.get("profit_pct"),
        "exit_type": sell.get("exit_type") or "",
        "label_stop_loss": int(pair["label"]),
        "trading_mode": "swing",
        "augment_offset_bars": augment_offset,
    }
    row.update(feats)
    return row


def _row_from_seed(
    seed: dict[str, Any],
    *,
    token: str | None,
    app_key: str,
    app_secret: str,
) -> dict[str, Any] | None:
    code = str(seed.get("stock_code") or "").strip()
    buy_time = str(seed.get("buy_time") or "")
    entry_dt = _parse_dt(buy_time)
    if len(code) != 6 or entry_dt is None:
        return None

    hourly: list[dict] = []
    if token:
        try:
            hourly = fetch_hourly_bars_at_entry(
                token, app_key, app_secret, code, entry_dt
            )
        except Exception as exc:
            logger.warning("시드 차트 조회 실패 %s: %s", code, exc)

    buy_price = int(seed.get("buy_price") or 0)
    pick = {
        "code": code,
        "name": seed.get("stock_name"),
        "price": buy_price,
        "change_rate": float(seed.get("change_rate") or 0),
        "brain_rank": seed.get("brain_rank"),
        "brain_score": seed.get("brain_score"),
    }
    feats = extract_chart_features(
        hourly, pick=pick, entry_dt=entry_dt, entry_price=buy_price
    )
    return {
        "sample_id": str(seed.get("trade_id") or f"seed_{code}"),
        "stock_code": code,
        "stock_name": str(seed.get("stock_name") or code),
        "buy_time": buy_time,
        "buy_price": buy_price,
        "sell_time": str(seed.get("sell_time") or ""),
        "sell_price": int(seed.get("sell_price") or 0),
        "pnl": seed.get("pnl"),
        "profit_pct": seed.get("profit_pct"),
        "exit_type": seed.get("exit_type") or "",
        "label_stop_loss": int(seed.get("label_stop_loss") or 0),
        "trading_mode": str(seed.get("trading_mode") or "swing"),
        "augment_offset_bars": 0,
        **feats,
    }


def build_trade_pattern_dataset(
    *,
    output_path: Path | None = None,
    token: str | None = None,
    augment_offsets: tuple[int, ...] = (0, 1, 2),
) -> tuple[Path, list[dict[str, Any]]]:
    """
    DB 체결 + 시드 JSON → CSV.
    augment_offsets: 매수 시점 N봉 이전 패턴 추가(소량 데이터 증강).
    """
    output_path = output_path or DEFAULT_DATASET_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)

    trades = _load_trades()
    pairs = _group_buy_sell_pairs(trades)
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    app_key = APP_KEY
    app_secret = APP_SECRET

    for pair in pairs:
        for offset in augment_offsets:
            row = _row_from_pair(
                pair,
                token=token,
                app_key=app_key,
                app_secret=app_secret,
                augment_offset=offset,
                sample_suffix=f"_aug{offset}" if offset else "",
            )
            if row and row["sample_id"] not in seen_ids:
                seen_ids.add(row["sample_id"])
                rows.append(row)

    existing_buy_keys = {
        (str(r.get("stock_code") or ""), str(r.get("buy_time") or ""))
        for r in rows
    }
    for seed in _load_seed_trades():
        code = str(seed.get("stock_code") or "").strip()
        buy_time = str(seed.get("buy_time") or "")
        if (code, buy_time) in existing_buy_keys:
            continue
        row = _row_from_seed(seed, token=token, app_key=app_key, app_secret=app_secret)
        if row and row["sample_id"] not in seen_ids:
            seen_ids.add(row["sample_id"])
            existing_buy_keys.add((code, buy_time))
            rows.append(row)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    loss_n = sum(1 for r in rows if int(r.get("label_stop_loss") or 0) == 1)
    win_n = len(rows) - loss_n
    logger.info(
        "데이터셋 %s — %d행 (손절=%d, 수익=%d)",
        output_path,
        len(rows),
        loss_n,
        win_n,
    )
    return output_path, rows


def load_dataset_rows(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or DEFAULT_DATASET_PATH
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))
