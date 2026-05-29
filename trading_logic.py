"""
모드별 예상 매도가(목표가) 산출 — Brain Classifier trading_mode 연동.

단타: 피봇 저항 근사 +3~5%
스윙: 60분봉 직전 고점 매물대 +10~15%
장투: Trailing 표기 + 일봉·시간봉 지지 마지노선
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any

logger = logging.getLogger(__name__)

try:
    import config as _cfg
except ImportError:
    _cfg = None  # type: ignore


def _cfg_float(name: str, default: float) -> float:
    if _cfg is None:
        return default
    return float(getattr(_cfg, name, default))


def _cfg_int(name: str, default: int) -> int:
    if _cfg is None:
        return default
    return int(getattr(_cfg, name, default))


def _cfg_bool(name: str, default: bool) -> bool:
    if _cfg is None:
        return default
    return bool(getattr(_cfg, name, default))


def _normalize_mode_value(raw_mode: Any, fallback: str = "swing") -> str:
    """UI/selected_modes 입력 라벨을 내부 모드값으로 정규화."""
    text = str(raw_mode or "").strip()
    lowered = text.lower()
    aliases = {
        "scalping": "scalping",
        "scalp": "scalping",
        "short": "scalping",
        "danta": "scalping",
        "단타": "scalping",
        "swing": "swing",
        "스윙": "swing",
        "long_term": "long_term",
        "longterm": "long_term",
        "long": "long_term",
        "jangtu": "long_term",
        "장투": "long_term",
    }
    if text in aliases:
        return aliases[text]
    if lowered in aliases:
        return aliases[lowered]
    fb = aliases.get(str(fallback).lower())
    return fb or "swing"


def _api_throttle_sleep() -> None:
    """KIS 호출 전 공통 스로틀 안전벨트."""
    time.sleep(max(_cfg_float("KIS_API_MIN_INTERVAL_SEC", 0.25), 0.25))


def _profit_pct(entry: int, target: int) -> float:
    if entry <= 0:
        return 0.0
    return (target - entry) / entry * 100.0


def _clamp_price(value: float, entry: int, *, min_pct: float, max_pct: float) -> int:
    lo = entry * (1 + min_pct / 100.0)
    hi = entry * (1 + max_pct / 100.0)
    return max(1, int(round(max(lo, min(hi, value)))))


def _recent_pivot_resistance(hourly_bars: list[dict], lookback: int = 8) -> int | None:
    """당일·근시간 고점 클러스터를 피봇 저항으로 근사."""
    if not hourly_bars:
        return None
    window = hourly_bars[-lookback:] if len(hourly_bars) >= lookback else hourly_bars
    highs = [int(b.get("high", b.get("close", 0))) for b in window]
    highs = [h for h in highs if h > 0]
    if not highs:
        return None
    return max(highs)


def _swing_prior_high(hourly_bars: list[dict], exclude_last: int = 3) -> int | None:
    """눌림 직전 60분봉 구간의 매물대 상단(고점)."""
    if len(hourly_bars) < exclude_last + 5:
        return None
    zone = hourly_bars[: -exclude_last] if exclude_last else hourly_bars
    zone = zone[-40:] if len(zone) > 40 else zone
    highs = [int(b.get("high", b.get("close", 0))) for b in zone]
    highs = [h for h in highs if h > 0]
    return max(highs) if highs else None


def _support_floor(hourly_bars: list[dict], entry: int, lookback: int = 20) -> int:
    """지지 마지노선 — 최근 저점 또는 MA20 근사."""
    if hourly_bars:
        window = hourly_bars[-lookback:] if len(hourly_bars) >= lookback else hourly_bars
        lows = [int(b.get("low", b.get("close", 0))) for b in window]
        lows = [l for l in lows if l > 0]
        if lows:
            floor = int(min(lows))
            closes = [int(b.get("close", 0)) for b in window if int(b.get("close", 0)) > 0]
            if len(closes) >= 10:
                ma20 = sum(closes[-20:]) / min(20, len(closes))
                floor = int(min(floor, ma20 * 0.98))
            return max(1, floor)
    return max(1, int(round(entry * 0.92)))


def compute_expected_exit(
    *,
    entry_price: int,
    trading_mode: str,
    hourly_bars: list[dict] | None = None,
    peak_price: int | None = None,
) -> dict[str, Any]:
    """
    모드별 예상 매도가 산출.

    Returns:
        target_price, target_profit_pct, target_kind, target_note, target_display
    """
    entry = int(entry_price)
    if entry <= 0:
        return {
            "target_price": 0,
            "target_profit_pct": 0.0,
            "target_kind": "unknown",
            "target_note": "",
            "target_display": "—",
        }

    mode = (trading_mode or "swing").lower()
    bars = hourly_bars or []

    if mode == "scalping":
        tp = _cfg_float("SCALP_TAKE_PROFIT_PCT", 3.0)
        target = int(round(entry * (1 + tp / 100.0)))
        return {
            "target_price": target,
            "target_profit_pct": round(tp, 2),
            "target_kind": "limit",
            "target_note": f"분봉 +{tp:.1f}% 익절 / -{_cfg_float('SCALP_STOP_LOSS_PCT', 1.5):.1f}% 손절",
            "target_display": "limit",
        }

    if mode == "long_term":
        floor_px = _support_floor(bars, entry)
        ceiling_pct = _cfg_float("TARGET_LONG_CEILING_PCT", 20.0)
        ceiling = int(round(entry * (1 + ceiling_pct / 100.0)))
        return {
            "target_price": floor_px,
            "target_ceiling_price": ceiling,
            "target_profit_pct": round(_profit_pct(entry, ceiling), 2),
            "target_kind": "trailing",
            "target_note": "추세 추종(Trailing 익절선)",
            "target_display": "trailing",
        }

    # swing (default)
    min_p = _cfg_float("TARGET_SWING_MIN_PCT", 10.0)
    max_p = _cfg_float("TARGET_SWING_MAX_PCT", 15.0)
    prior_high = _swing_prior_high(bars)
    pct_mid = (min_p + max_p) / 2.0
    pct_target = int(round(entry * (1 + pct_mid / 100.0)))
    if prior_high and prior_high > entry:
        raw = max(prior_high, int(round(entry * (1 + min_p / 100.0))))
        target = min(int(round(entry * (1 + max_p / 100.0))), raw)
    else:
        target = _clamp_price(pct_target, entry, min_pct=min_p, max_pct=max_p)
    target = max(target, int(round(entry * (1 + min_p / 100.0))))
    target = min(target, int(round(entry * (1 + max_p / 100.0))))
    pct = _profit_pct(entry, target)
    return {
        "target_price": target,
        "target_profit_pct": round(pct, 2),
        "target_kind": "limit",
        "target_note": f"60분 고점 매물대·+{min_p:.0f}~{max_p:.0f}%",
        "target_display": "limit",
    }


def refresh_target_live_fields(position: dict[str, Any]) -> None:
    """현재가 갱신 시 목표까지 남은 비율 등 실시간 표시 필드."""
    entry = int(position.get("entry_price") or 0)
    current = int(position.get("current_price") or entry)
    target = int(position.get("target_price") or 0)
    kind = str(position.get("target_kind") or "limit")

    if entry <= 0:
        return

    position["target_profit_pct"] = round(
        float(position.get("target_profit_pct") or _profit_pct(entry, target)),
        2,
    )

    if kind == "trailing":
        ceiling = int(position.get("target_ceiling_price") or 0)
        if ceiling > entry:
            position["target_profit_pct"] = round(_profit_pct(entry, ceiling), 2)
        progress = (current - entry) / entry * 100.0
        position["target_progress_pct"] = round(progress, 2)
        position["target_remaining_pct"] = None
        return

    if target <= entry:
        position["target_progress_pct"] = round((current - entry) / entry * 100.0, 2)
        position["target_remaining_pct"] = None
        return

    span = target - entry
    progress = (current - entry) / span * 100.0 if span > 0 else 0.0
    position["target_progress_pct"] = round(max(0.0, min(100.0, progress)), 2)
    position["target_remaining_pct"] = round(
        max(0.0, (target - current) / current * 100.0), 2
    )


def apply_expected_exit_to_position(
    position: dict[str, Any],
    *,
    hourly_bars: list[dict] | None = None,
    force_recalc: bool = False,
) -> dict[str, Any]:
    """포지션 dict에 target_* 필드 병합 (JSON 스냅샷 저장용)."""
    if not force_recalc and int(position.get("target_price") or 0) > 0:
        refresh_target_live_fields(position)
        return position

    entry = int(position.get("entry_price") or position.get("price") or 0)
    mode = position_trading_mode(position)
    fields = compute_expected_exit(
        entry_price=entry,
        trading_mode=mode,
        hourly_bars=hourly_bars,
        peak_price=int(position.get("peak_price") or entry),
    )
    position.update(fields)
    position["target_set_at"] = position.get("updated_at") or position.get("entry_date")
    refresh_target_live_fields(position)
    return position


def exit_unit_price_for_position(position: dict[str, Any]) -> int:
    """목표 도달 시 가정 체결 단가 (장투는 ceiling, 그 외 target_price)."""
    entry = int(position.get("entry_price") or 0)
    kind = str(position.get("target_kind") or "limit")
    if kind == "trailing":
        ceiling = int(position.get("target_ceiling_price") or 0)
        if ceiling > entry:
            return ceiling
        pct = float(position.get("target_profit_pct") or 0.0)
        return max(1, int(round(entry * (1 + pct / 100.0))))
    target = int(position.get("target_price") or 0)
    return target if target > 0 else entry


def _reference_capital_won(open_cost: int = 0) -> int:
    """헤더 수익률 % 분모 — 총 시드 기준."""
    from betting_engine import account_total_seed

    seed = account_total_seed()
    return max(int(open_cost), seed)


def aggregate_open_holdings(
    positions: list[dict[str, Any]],
    *,
    refresh_live: bool = True,
) -> dict[str, Any]:
    """
    보유 슬롯만 집계 (슬롯 카드와 분리).
    - unrealized_pnl: 평가손익
    - expected_additional_pnl: 현재가→목표가(🎯) 추가 예상금 합
    """
    empty = {
        "position_count": 0,
        "open_cost": 0,
        "current_value": 0,
        "unrealized_pnl": 0,
        "expected_additional_pnl": 0,
    }
    if not positions:
        return empty

    open_cost = 0
    current_value = 0
    expected_additional = 0

    for pos in positions:
        if refresh_live:
            refresh_target_live_fields(pos)
        entry = int(pos.get("entry_price") or 0)
        current = int(pos.get("current_price") or entry)
        qty = int(pos.get("quantity") or 0)
        if entry <= 0 or qty <= 0:
            continue
        open_cost += entry * qty
        current_value += current * qty
        exit_px = exit_unit_price_for_position(pos)
        expected_additional += (exit_px - current) * qty

    return {
        "position_count": len(positions),
        "open_cost": open_cost,
        "current_value": current_value,
        "unrealized_pnl": current_value - open_cost,
        "expected_additional_pnl": expected_additional,
    }


def aggregate_portfolio_metrics(
    positions: list[dict[str, Any]],
    *,
    refresh_live: bool = True,
) -> dict[str, Any]:
    """레거시 — 보유 기준 가중 지표."""
    hold = aggregate_open_holdings(positions, refresh_live=refresh_live)
    open_cost = int(hold.get("open_cost", 0))
    if open_cost <= 0:
        return {
            **hold,
            "total_cost": 0,
            "target_value": 0,
            "current_return_pct": 0.0,
            "expected_return_pct": 0.0,
            "expected_pnl_at_target": 0,
        }
    unrealized = int(hold["unrealized_pnl"])
    expected_add = int(hold["expected_additional_pnl"])
    current_value = int(hold["current_value"])
    return {
        **hold,
        "total_cost": open_cost,
        "target_value": current_value + expected_add,
        "current_return_pct": round(unrealized / open_cost * 100.0, 2),
        "expected_return_pct": round(
            expected_add / current_value * 100.0 if current_value > 0 else 0.0, 2
        ),
        "expected_pnl_at_target": expected_add,
    }


def get_capital_allocation() -> dict[str, float]:
    """AI 판세 기반 단타/스윙/장투 자금 비중 (실시간 롤링 엔진)."""
    from market_ai import get_active_allocation

    return get_active_allocation()


def _default_mode_target_pct(mode: str) -> float:
    mode = (mode or "swing").lower()
    if mode == "scalping":
        return _cfg_float("SCALP_TAKE_PROFIT_PCT", 3.0)
    if mode == "long_term":
        return _cfg_float("TARGET_LONG_CEILING_PCT", 20.0)
    lo = _cfg_float("TARGET_SWING_MIN_PCT", 10.0)
    hi = _cfg_float("TARGET_SWING_MAX_PCT", 15.0)
    return (lo + hi) / 2.0


def assemble_commander_slots(
    positions: list[dict[str, Any]],
    *,
    slot_controls: dict[int, dict[str, Any]] | None = None,
    max_slots: int = 5,
    include_control_candidates: bool = True,
) -> list[dict[str, Any]]:
    """
    전황판 5슬롯 — 보유 포지션 + (선택) 슬롯에 고정된 후보만 묶음.

    include_control_candidates=False 이거나 슬롯이 전부 보유면
    자동 추천 풀·유니버스 종목은 절대 포함하지 않음.
    """
    from selected_modes import resolve_mode_label_for_slot, resolve_mode_value_for_slot
    from stock_names import normalize_code

    held_count = min(len(positions), max_slots)
    if held_count >= max_slots:
        include_control_candidates = False
    controls: dict[int, dict[str, Any]] = {}
    if include_control_candidates and isinstance(slot_controls, dict):
        controls = slot_controls
    slots: list[dict[str, Any]] = []

    for idx in range(1, max_slots + 1):
        if idx <= held_count:
            pos = positions[idx - 1]
            code = normalize_code(pos.get("code"))
            if len(code) != 6:
                continue
            mode = position_trading_mode(pos)
            entry = int(pos.get("entry_price") or 0)
            current = int(pos.get("current_price") or entry)
            qty = int(pos.get("quantity") or 0)
            deployed = int(pos.get("deployed_won") or 0)
            if deployed <= 0 and entry > 0 and qty > 0:
                deployed = entry * qty
            profit = float(pos.get("profit_pct") or 0.0)
            if profit == 0.0 and entry > 0 and current > 0:
                profit = (current - entry) / entry * 100.0
            tgt = float(pos.get("target_profit_pct") or 0.0)
            if tgt <= 0:
                tgt = _default_mode_target_pct(mode)
            slots.append(
                {
                    "slot_idx": idx,
                    "code": code,
                    "name": str(pos.get("name") or code),
                    "is_held": True,
                    "trading_mode": mode,
                    "mode_label": str(pos.get("mode_label") or resolve_mode_label_for_slot(idx, code=code)),
                    "entry_price": entry,
                    "current_price": current,
                    "price": current,
                    "quantity": qty,
                    "deployed_won": max(1, deployed),
                    "profit_pct": round(profit, 2),
                    "change_rate": float(pos.get("change_rate") or profit),
                    "target_profit_pct": round(tgt, 2),
                }
            )
            continue

        cand = controls.get(idx)
        if not isinstance(cand, dict):
            continue
        code = normalize_code(cand.get("code"))
        if len(code) != 6:
            continue
        mode = resolve_mode_value_for_slot(
            idx,
            code=code,
            fallback_value=str(cand.get("trading_mode") or cand.get("selected_trading_mode") or ""),
        )
        price = int(cand.get("price") or cand.get("current_price") or 0)
        slots.append(
            {
                "slot_idx": idx,
                "code": code,
                "name": str(cand.get("name") or code),
                "is_held": False,
                "trading_mode": mode,
                "mode_label": resolve_mode_label_for_slot(idx, code=code),
                "entry_price": 0,
                "current_price": price,
                "price": price,
                "quantity": 0,
                "deployed_won": max(1, price) if price > 0 else 1,
                "profit_pct": 0.0,
                "change_rate": float(cand.get("change_rate") or 0.0),
                "target_profit_pct": round(_default_mode_target_pct(mode), 2),
            }
        )
    return slots


def _slot_intraday_expectation(slot: dict[str, Any]) -> tuple[float, float, float]:
    """슬롯 1개당 당일 기대 수익률 % (low, high, mid) — 모드·목표가·현재가 연동."""
    mode = str(slot.get("trading_mode") or "swing").lower()
    bp = float(slot.get("bid_pressure") or 0.0)
    bias = (bp - 0.5) * 0.6

    if slot.get("is_held"):
        cur = float(slot.get("profit_pct") or 0.0)
        tgt = float(slot.get("target_profit_pct") or _default_mode_target_pct(mode))
        if mode == "scalping":
            stop = _cfg_float("SCALP_STOP_LOSS_PCT", 1.5)
            low = max(cur - stop, -stop)
            high = min(max(tgt, cur + 0.25), _cfg_float("SCALP_TAKE_PROFIT_PCT", 3.0) + 0.5)
        elif mode == "long_term":
            low = cur - 2.0 + bias
            high = max(tgt, cur + 1.0) + bias
        else:
            low = cur - 1.2 + bias
            high = max(tgt, cur + 0.5) + bias
    else:
        chg = float(slot.get("change_rate") or 0.0)
        tgt = float(slot.get("target_profit_pct") or _default_mode_target_pct(mode))
        if mode == "scalping":
            stop = _cfg_float("SCALP_STOP_LOSS_PCT", 1.5)
            low = chg - stop + bias
            high = chg + _cfg_float("SCALP_TAKE_PROFIT_PCT", 3.0) + bias
        elif mode == "long_term":
            low = chg - 1.5 + bias
            high = chg + min(tgt, _cfg_float("TARGET_LONG_CEILING_PCT", 20.0) * 0.4) + bias
        else:
            low = chg - 0.8 + bias
            high = chg + tgt * 0.85 + bias

    mid = (low + high) / 2.0
    return round(low, 2), round(high, 2), round(mid, 2)


def compute_commander_ai_daily_forecast(
    slots: list[dict[str, Any]],
    *,
    positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    지휘관 슬롯 등록 종목만 가중 평균 — 시장 주도주 스캔 미사용.
    전원 보유 시 오늘 통합 수익률·목표가 경로와 수치를 맞춘다.
    """
    if not slots:
        return {
            "pct_low": 0.0,
            "pct_high": 0.0,
            "pct_mid": 0.0,
            "narrative": "슬롯에 종목을 등록하면 모드별 AI 가이드가 표시됩니다.",
            "slot_count": 0,
            "source": "commander_slots",
        }

    weighted: list[tuple[float, float, float, int]] = []
    for slot in slots:
        low, high, mid = _slot_intraday_expectation(slot)
        w = max(1, int(slot.get("deployed_won") or 1))
        weighted.append((low, high, mid, w))

    total_w = sum(w for *_, w in weighted)
    pct_low = sum(r[0] * r[3] for r in weighted) / total_w
    pct_high = sum(r[1] * r[3] for r in weighted) / total_w
    pct_mid = sum(r[2] * r[3] for r in weighted) / total_w

    held_slots = [s for s in slots if s.get("is_held")]
    if positions and held_slots and len(held_slots) == len(slots):
        hold = aggregate_open_holdings(positions)
        open_cost = int(hold.get("open_cost", 0))
        ref = _reference_capital_won(open_cost)
        if ref > 0:
            unreal = int(hold.get("unrealized_pnl", 0))
            expected_add = int(hold.get("expected_additional_pnl", 0))
            today_pct = unreal / ref * 100.0
            path_pct = (unreal + expected_add) / ref * 100.0
            pct_mid = round(today_pct * 0.4 + path_pct * 0.6, 2)
            half = max(0.35, min(1.5, (pct_high - pct_low) / 2.0 * 0.55))
            pct_low = round(pct_mid - half, 2)
            pct_high = round(pct_mid + half, 2)

    labels = [
        f"{s.get('mode_label', '?')}{str(s.get('code', ''))[-4:]}"
        for s in slots[:5]
    ]
    narrative = (
        f"🧠 슬롯 AI ({len(slots)}종 · {', '.join(labels)}) → "
        f"당일 기대 {pct_low:+.1f}~{pct_high:+.1f}% "
        f"(등록 종목·모드·목표가 연동)"
    )
    return {
        "date": date.today().isoformat(),
        "pct_low": round(pct_low, 2),
        "pct_high": round(pct_high, 2),
        "pct_mid": round(pct_mid, 2),
        "narrative": narrative,
        "slot_count": len(slots),
        "source": "commander_slots",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_commander_dashboard_metrics(
    positions: list[dict[str, Any]],
    *,
    commander_slots: list[dict[str, Any]] | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    """
    지휘관 자금 총괄 전황판.
    - 오늘 통합 / 이번 주 통합: trade_state 실현·평가
    - 오늘 총 예상: 전황판 슬롯 등록 종목만 (시장 주도주 스캔 미사용)
    """
    from market_ai import enrich_commander_slots_with_quotes, get_ai_forecast_cached
    from trade_state import get_daily_realized_pnl, get_week_realized_summary

    # 체결/수량 변동 직후에는 캐시를 무시하고 파일 기준으로 즉시 동기화한다.
    daily = get_daily_realized_pnl(force_refresh=True)
    weekly = get_week_realized_summary()
    ai = get_ai_forecast_cached()
    ai_weekly = ai.get("weekly") or {}
    alloc = get_capital_allocation()

    slots = commander_slots or assemble_commander_slots(
        positions, include_control_candidates=False
    )
    from stock_names import normalize_code

    roster_codes = {
        normalize_code(s.get("code"))
        for s in slots
        if len(normalize_code(s.get("code"))) == 6
    }
    pos_for_hold = positions
    if roster_codes:
        pos_for_hold = [
            p
            for p in positions
            if normalize_code(p.get("code")) in roster_codes
        ]
    hold = aggregate_open_holdings(pos_for_hold)

    if access_token and slots:
        need_quote = any(
            (not s.get("is_held"))
            or int(s.get("current_price") or s.get("price") or 0) <= 0
            for s in slots
        )
        if need_quote:
            for attempt in range(3):
                try:
                    _api_throttle_sleep()
                    slots = enrich_commander_slots_with_quotes(access_token, slots)
                    break
                except Exception as exc:
                    logger.warning("슬롯 시세 보강 재시도(%d): %s", attempt + 1, exc)
                    if attempt < 2:
                        time.sleep(1.0)


    ai_daily = compute_commander_ai_daily_forecast(slots, positions=pos_for_hold)
    narrative = str(ai_daily.get("narrative", ""))
    ai_day_low = float(ai_daily.get("pct_low", 0))
    ai_day_high = float(ai_daily.get("pct_high", 0))

    today_realized = int(daily.get("today_realized_pnl", 0))
    unrealized = int(hold.get("unrealized_pnl", 0))
    today_combined = today_realized + unrealized
    open_cost = int(hold.get("open_cost", 0))
    week_realized = int(weekly.get("week_realized_pnl", 0))

    ref_today = _reference_capital_won(open_cost)
    ref_week = _reference_capital_won(0)

    today_pct = today_combined / ref_today * 100.0 if ref_today > 0 else 0.0
    week_pct = week_realized / ref_week * 100.0 if ref_week > 0 else 0.0

    ai_day_mid = float(ai_daily.get("pct_mid", 0))
    if ai_day_mid == 0.0 and (ai_day_low or ai_day_high):
        ai_day_mid = round((ai_day_low + ai_day_high) / 2.0, 2)
    ai_week_low = float(ai_weekly.get("pct_low", 0))
    ai_week_high = float(ai_weekly.get("pct_high", 0))
    ai_week_mid = float(ai_weekly.get("pct_mid", 0))

    return {
        "stats_date": daily.get("stats_date", ""),
        "position_count": int(hold.get("position_count", 0)),
        "today_realized_pnl": today_realized,
        "today_unrealized_pnl": unrealized,
        "today_combined_pnl": today_combined,
        "today_return_pct": round(today_pct, 2),
        "today_trade_count": int(daily.get("today_trade_count", 0)),
        "week_start": weekly.get("week_start", ""),
        "week_realized_pnl": week_realized,
        "week_trade_count": int(weekly.get("week_trade_count", 0)),
        "week_return_pct": round(week_pct, 2),
        "ai_daily_pct_low": ai_day_low,
        "ai_daily_pct_high": ai_day_high,
        "ai_daily_pct_mid": ai_day_mid,
        "ai_daily_narrative": narrative,
        "ai_weekly_pct_low": ai_week_low,
        "ai_weekly_pct_high": ai_week_high,
        "ai_weekly_pct_mid": ai_week_mid,
        "ai_weekly_narrative": str(ai_weekly.get("narrative", "")),
        "ai_scalp_weight": float(alloc.get("scalp_weight", 0.35)),
        "ai_swing_weight": float(alloc.get("swing_weight", 0.45)),
        "ai_long_weight": float(alloc.get("long_weight", 0.20)),
        # 레거시
        "unrealized_pnl": unrealized,
        "current_return_pct": round(today_pct, 2),
        "today_expected_return_pct": ai_day_mid,
        "expected_return_pct": ai_day_mid,
        "week_combined_pnl": week_realized,
    }


def format_slot_identity_line(position: dict[str, Any]) -> str:
    """슬롯 한 줄: [단타] | 🎯 목표가: 15,400원"""
    label = str(position.get("mode_label") or "스윙")
    css = str(position.get("mode_badge_class") or "mode-swing")
    kind = str(position.get("target_kind") or "limit")
    target = int(position.get("target_price") or 0)

    if kind == "trailing":
        ceiling = int(position.get("target_ceiling_price") or 0)
        if ceiling > 0:
            return (
                f'<span class="mode-badge {css}">{label}</span>'
                f' <span class="slot-identity-sep">|</span> '
                f"🎯 목표가: <b>{ceiling:,}원</b> <span class='slot-trail-tag'>(Trailing)</span>"
            )
        return (
            f'<span class="mode-badge {css}">{label}</span>'
            f' <span class="slot-identity-sep">|</span> '
            f"🎯 마지노선: <b>{target:,}원</b> <span class='slot-trail-tag'>(Trailing)</span>"
        )

    if target <= 0:
        return (
            f'<span class="mode-badge {css}">{label}</span>'
            f' <span class="slot-identity-sep">|</span> 🎯 목표가: —'
        )

    return (
        f'<span class="mode-badge {css}">{label}</span>'
        f' <span class="slot-identity-sep">|</span> '
        f"🎯 목표가: <b>{target:,}원</b>"
    )


def format_target_line_for_ui(position: dict[str, Any]) -> str:
    """슬롯 카드용 한 줄 HTML 텍스트 (이스케이프 최소)."""
    kind = str(position.get("target_kind") or "limit")
    entry = int(position.get("entry_price") or 0)
    target = int(position.get("target_price") or 0)
    pct = float(position.get("target_profit_pct") or 0.0)

    if kind == "trailing":
        floor_px = target
        pct_txt = f" · 상단 목표 +{pct:.1f}%" if pct > 0 else ""
        return (
            f"🎯 예상 매도: <b>추세 추종(Trailing)</b> · "
            f"마지노선 {floor_px:,}원{pct_txt}"
        )

    if target <= 0:
        return "🎯 예상 매도가: 계산 중…"

    sign = "+" if pct >= 0 else ""
    return f"🎯 예상 매도가: {target:,}원 (예상 {sign}{pct:.1f}%)"


def position_trading_mode(position: dict[str, Any]) -> str:
    """포지션 청산·감시 — selected_modes.json 사용자 설정 우선."""
    try:
        from selected_modes import get_mode_value_for_position

        return _normalize_mode_value(get_mode_value_for_position(position), "swing")
    except ImportError:
        return _normalize_mode_value(position.get("trading_mode"), "swing")


def monitor_interval_for_positions(positions: list[dict[str, Any]]) -> float:
    """REST 폴백 감시 주기(초). WS 연결 시 0 → 체결 틱이 즉시 판정."""
    if _cfg_bool("USE_REALTIME_WEBSOCKET", True):
        try:
            from scheduler import _realtime_ws_ready

            if _realtime_ws_ready():
                return 0.0
        except Exception:
            pass
    modes = {position_trading_mode(p) for p in positions}
    if "scalping" in modes:
        return _cfg_float("SCALP_MONITOR_INTERVAL_SEC", 1.0)
    if "long_term" in modes:
        return _cfg_float("LONG_MONITOR_INTERVAL_SEC", 8.0)
    return _cfg_float("SWING_MONITOR_INTERVAL_SEC", 5.0)


def requires_fast_tick_exit(position: dict[str, Any]) -> bool:
    return position_trading_mode(position) == "scalping"


def requires_minute_bars(position: dict[str, Any]) -> bool:
    return position_trading_mode(position) == "scalping"


def requires_hourly_bars(position: dict[str, Any]) -> bool:
    return position_trading_mode(position) in ("swing", "long_term")


def get_entry_stop_loss(entry_price: int, mode: str) -> int:
    """모드별 진입 손절가 — 장투·스윙은 패닉 손절 없음(0)."""
    entry = int(entry_price)
    if entry <= 0:
        return 0
    mode = _normalize_mode_value(mode, "swing")
    if mode == "scalping":
        sl = _cfg_float("SCALP_STOP_LOSS_PCT", 1.5)
        return int(round(entry * (1 - sl / 100.0)))
    return 0


def apply_tactical_fields_on_position(position: dict[str, Any]) -> dict[str, Any]:
    """신규·모드변경 포지션 — selected_modes.json 인격에 맞는 전술 필드."""
    mode = _normalize_mode_value(position_trading_mode(position), "swing")
    entry = int(position.get("entry_price") or position.get("price") or 0)
    position["trading_mode"] = mode
    position.setdefault("swing_add_count", 0)
    position.setdefault("swing_exit_stage", 0)
    position.setdefault("dca_count", 0)
    position["stop_loss_price"] = get_entry_stop_loss(entry, mode)
    if mode == "scalping":
        position["scalp_entry_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        position["pyramid_done"] = True
    elif mode == "swing":
        position["pyramid_done"] = int(position.get("swing_add_count") or 0) >= _cfg_int(
            "SWING_MAX_SPLIT_BUYS", 3
        )
    elif mode == "long_term":
        position["last_dca_at"] = position.get("last_dca_at") or datetime.now().isoformat()
        position["trailing_active"] = False
    return position


def _parse_position_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if " " in fmt else text[:10], fmt)
        except ValueError:
            continue
    return None


def _must_flat_scalp_today() -> bool:
    """단타 당일 청산 — 장마감 전 강제 플랫."""
    flat_at = "15:20"
    if _cfg is not None:
        flat_at = str(getattr(_cfg, "SCALP_FORCE_FLAT_TIME", flat_at))
    try:
        hh, mm = flat_at.split(":")
        cutoff = dt_time(int(hh), int(mm))
    except ValueError:
        cutoff = dt_time(15, 20)
    return datetime.now().time() >= cutoff


def _must_force_long_term_liquidation(now: datetime | None = None) -> bool:
    """
    6월 마스터 테스트용 장투 강제 청산 타이머.
    마감일/시각 도달 시 손익 무관 전량 청산 신호를 발생시킨다.
    """
    now_dt = now or datetime.now()
    enabled = _cfg_bool("LONG_FORCE_EXIT_ENABLED", True)
    if not enabled:
        return False
    cutoff_date = str(
        getattr(_cfg, "LONG_FORCE_EXIT_DATE", "2026-06-26")
        if _cfg is not None
        else "2026-06-26"
    )
    cutoff_time = str(
        getattr(_cfg, "LONG_FORCE_EXIT_TIME", "15:00")
        if _cfg is not None
        else "15:00"
    )
    try:
        d = date.fromisoformat(cutoff_date)
    except ValueError:
        d = date(2026, 6, 26)
    try:
        hh, mm = cutoff_time.split(":")
        t = dt_time(int(hh), int(mm))
    except ValueError:
        t = dt_time(15, 0)
    return now_dt.date() >= d and now_dt.time() >= t


def _long_force_liquidation_stage(now: datetime | None = None) -> int | None:
    """장투 6월 작전 분할청산 단계(1~N). 시작 전이면 None."""
    now_dt = now or datetime.now()
    if not _cfg_bool("LONG_FORCE_EXIT_ENABLED", True):
        return None
    try:
        start_h, start_m = str(getattr(_cfg, "LONG_FORCE_SPLIT_START_TIME", "14:30")).split(":")
        end_h, end_m = str(getattr(_cfg, "LONG_FORCE_SPLIT_END_TIME", "15:20")).split(":")
        start_t = dt_time(int(start_h), int(start_m))
        end_t = dt_time(int(end_h), int(end_m))
    except Exception:
        start_t = dt_time(14, 30)
        end_t = dt_time(15, 20)
    tranches = max(1, _cfg_int("LONG_FORCE_SPLIT_TRANCHES", 5))
    interval_min = max(1, _cfg_int("LONG_FORCE_SPLIT_INTERVAL_MIN", 10))
    if now_dt.time() < start_t:
        return None
    start_min = start_t.hour * 60 + start_t.minute
    now_min = now_dt.hour * 60 + now_dt.minute
    stage = (now_min - start_min) // interval_min + 1
    if now_dt.time() >= end_t:
        return tranches
    return max(1, min(tranches, int(stage)))


def _scalp_top_wick_rejected(
    current: int,
    minute_bars: list[dict] | None = None,
) -> bool:
    """당일 고점 대비 -3.5% 이상 이탈한 윗꼬리 종목 차단."""
    if current <= 0 or not minute_bars:
        return False
    highs = [int(b.get("high") or b.get("close") or 0) for b in minute_bars if int(b.get("high") or b.get("close") or 0) > 0]
    if not highs:
        return False
    session_high = max(highs)
    if session_high <= 0:
        return False
    drop_pct = (current - session_high) / session_high * 100.0
    limit = -abs(_cfg_float("SCALP_TOP_WICK_REJECT_PCT", 3.5))
    return drop_pct <= limit


def compute_swing_accumulation_zone(
    hourly_bars: list[dict] | None,
    entry: int,
) -> dict[str, int] | None:
    """일·60분봉 근사 매집 구간(지지~중심)."""
    if not hourly_bars or entry <= 0:
        return None
    lookback = _cfg_int("SWING_ACCUMULATION_LOOKBACK", 20)
    window = hourly_bars[-lookback:] if len(hourly_bars) >= lookback else hourly_bars
    lows = [int(b.get("low", b.get("close", 0))) for b in window]
    highs = [int(b.get("high", b.get("close", 0))) for b in window]
    lows = [x for x in lows if x > 0]
    highs = [x for x in highs if x > 0]
    if not lows or not highs:
        return None
    support = min(lows)
    resistance = max(highs)
    mid = int((support + resistance) / 2)
    return {
        "support": max(1, support),
        "mid": max(1, mid),
        "resistance": max(1, resistance),
        "entry": entry,
    }


def _swing_ma_support_ok(hourly_bars: list[dict] | None, current: int) -> bool:
    """구출 매수 전 MA 지지 유효성 확인(무지성 물타기 금지)."""
    if not hourly_bars or current <= 0:
        return False
    closes = [int(b.get("close") or 0) for b in hourly_bars if int(b.get("close") or 0) > 0]
    if len(closes) < 20:
        return False
    ma20 = sum(closes[-20:]) / 20.0
    ma60 = sum(closes[-60:]) / min(60, len(closes))
    support = max(ma20, ma60)
    tol = _cfg_float("SWING_RESCUE_MA_TOLERANCE_PCT", 1.0)
    return current >= support * (1 - tol / 100.0)


def _biz_days_since(raw_dt: str | None) -> int:
    dt = _parse_position_dt(raw_dt)
    if not dt:
        return 0
    d0 = dt.date()
    d1 = datetime.now().date()
    if d1 < d0:
        return 0
    days = 0
    cur = d0
    while cur <= d1:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return max(0, days - 1)


def _scalp_stagnation_exit(
    pos: dict[str, Any],
    profit_pct: float,
    minute_bars: list[dict] | None,
) -> str | None:
    """30분 횡보·거래대금/수급 미유입 시 본절 청산."""
    need = _cfg_int("SCALP_STAGNATION_MINUTES", 30)
    if not minute_bars or len(minute_bars) < max(need, 8):
        return None
    window = minute_bars[-need:]
    highs = [int(b.get("high", b.get("close", 0))) for b in window]
    lows = [int(b.get("low", b.get("close", 0))) for b in window]
    vols = [int(b.get("volume") or 0) for b in window]
    entry = int(pos.get("entry_price") or 0)
    if entry <= 0 or not highs or not lows:
        return None
    range_pct = (max(highs) - min(lows)) / entry * 100.0
    max_range = _cfg_float("SCALP_STAGNATION_RANGE_PCT", 0.45)
    half = len(vols) // 2
    vol_recent = sum(vols[half:]) if half else sum(vols)
    vol_prior = sum(vols[:half]) if half else vol_recent
    vol_fade = vol_prior > 0 and vol_recent < vol_prior * 0.65
    if range_pct <= max_range and vol_fade and profit_pct <= 0.35:
        return "단타 30분 횡보·수급 이탈 본절"
    return None


def _swing_trailing_exit(
    pos: dict[str, Any], current: int, entry: int
) -> str | None:
    stop_loss = int(pos.get("stop_loss_price") or 0)
    if stop_loss > 0 and current <= stop_loss:
        return "ATR 가변 손절"
    if stop_loss <= 0 and entry > 0:
        floor_px = int(
            round(entry * (1 - _cfg_float("ATR_MAX_LOSS_PCT", 15.0) / 100))
        )
        if current <= floor_px:
            return "ATR 가변 손절(보수 하한)"

    if not pos.get("trailing_active"):
        return None

    peak = int(pos.get("peak_price", entry))
    stop_line = int(pos.get("trailing_stop_price", 0))
    if stop_line <= 0:
        peak_profit = (peak - entry) / entry * 100 if entry else 0.0
        drop = None
        if peak_profit >= 20.0:
            drop = _cfg_float("TRAILING_TIER3_DROP_PCT", 5.0)
        elif peak_profit >= 10.0:
            drop = _cfg_float("TRAILING_TIER2_DROP_PCT", 3.0)
        elif peak_profit >= _cfg_float("TRAILING_MIN_PEAK_PROFIT_PCT", 5.0):
            drop = _cfg_float("TRAILING_TIER1_DROP_PCT", 2.0)
        if drop is not None:
            stop_line = int(peak * (1 - drop / 100))
    if stop_line > 0 and current <= stop_line:
        tier = pos.get("trailing_tier", "")
        return f"계단 트레일링 익절{(' ' + tier) if tier else ''}"
    return None


def _swing_target_exit(pos: dict[str, Any], current: int, entry: int) -> str | None:
    kind = str(pos.get("target_kind") or "limit")
    if kind == "trailing":
        return _swing_trailing_exit(pos, current, entry)
    target = int(pos.get("target_price") or 0)
    if target > entry and current >= target:
        return "목표가 도달 익절"
    return _swing_trailing_exit(pos, current, entry)


def _scalp_volume_fade(minute_bars: list[dict] | None) -> bool:
    if not minute_bars or len(minute_bars) < 6:
        return False
    vols = [int(b.get("volume") or 0) for b in minute_bars[-6:]]
    if max(vols) <= 0:
        return False
    ratio = _cfg_float("SCALP_VOLUME_FADE_RATIO", 0.45)
    return vols[-1] <= vols[-3] * ratio and vols[-2] <= vols[-4] * ratio


def decide_scalping_exit(
    pos: dict[str, Any],
    current: int,
    profit_pct: float,
    *,
    minute_bars: list[dict] | None = None,
) -> str | None:
    """단타 — 당일청산 · +3% / -1.5% · 30분 횡보 본절."""
    entry = int(pos.get("entry_price") or 0)
    if entry <= 0:
        return None

    if _must_flat_scalp_today():
        return "단타 당일 마감 청산"

    if _scalp_top_wick_rejected(current, minute_bars):
        return "단타 윗꼬리 이탈 방어 청산"

    stop_pct = _cfg_float("SCALP_STOP_LOSS_PCT", 1.5)
    take_pct = _cfg_float("SCALP_TAKE_PROFIT_PCT", 3.0)
    stop_px = int(pos.get("stop_loss_price") or 0)
    if stop_px <= 0:
        stop_px = int(round(entry * (1 - stop_pct / 100.0)))
    if current <= stop_px or profit_pct <= -stop_pct:
        return f"단타 손절 (-{stop_pct:.1f}%)"

    target = int(pos.get("target_price") or round(entry * (1 + take_pct / 100.0)))
    if current >= target or profit_pct >= take_pct:
        return f"단타 익절 (+{take_pct:.1f}%)"

    stale = _scalp_stagnation_exit(pos, profit_pct, minute_bars)
    if stale:
        return stale

    if _scalp_volume_fade(minute_bars) and profit_pct > 0.2:
        return "단타 수급 이탈(거래량 급감)"

    return None


def decide_swing_exit(
    pos: dict[str, Any],
    current: int,
    profit_pct: float,
    *,
    hourly_bars: list[dict] | None = None,
) -> str | None:
    """스윙·테마 — 매집 하단 이탈 + 고정 손절선 기준으로만 청산."""
    entry = int(pos.get("entry_price") or 0)
    if entry <= 0:
        return None

    if pos.get("theme_prepare_exit"):
        if profit_pct >= _cfg_float("SWING_PARTIAL_EXIT_PCT_1", 10.0):
            return "테마 디데이 목표 익절"
        return None

    target_pct = _cfg_float("SWING_TARGET_PROFIT_PCT", 15.0)
    partial_1 = _cfg_float("SWING_PARTIAL_EXIT_PCT_1", 10.0)
    hard_stop = abs(_cfg_float("SWING_HARD_STOP_LOSS_PCT", 12.0))
    rescue_timeout = max(1, _cfg_int("SWING_RESCUE_TIMEOUT_BIZ_DAYS", 3))
    rebound_min = _cfg_float("SWING_RESCUE_TIMEOUT_MIN_REBOUND_PCT", 1.0)
    stage = int(pos.get("swing_exit_stage") or 0)

    if profit_pct >= target_pct:
        return f"스윙 목표 익절 (+{target_pct:.0f}%)"
    if stage < 1 and profit_pct >= partial_1:
        pos["swing_exit_stage"] = 1
        pos["trailing_active"] = True
        pos["trailing_drop_pct"] = 2.5
        return None

    zone = compute_swing_accumulation_zone(hourly_bars, entry)
    if zone and current >= zone["support"]:
        # 조정·눌림 — 패닉 손절 없음 (분할매수는 plan_position_add)
        pass
    elif zone and current < zone["support"] * 0.97:
        if profit_pct <= -hard_stop:
            return f"스윙 매집 하단 이탈 손절 (-{hard_stop:.1f}%)"

    kind = str(pos.get("target_kind") or "limit")
    target = int(pos.get("target_price") or 0)
    if kind != "trailing" and target > entry and current >= target:
        return "스윙 매물대 목표 익절"
    # 구출(분할매수) 이후에도 반등 없이 기한 초과면 즉시 손절.
    if int(pos.get("swing_add_count") or 0) > 0:
        held_biz = _biz_days_since(str(pos.get("entry_date") or pos.get("updated_at") or ""))
        if held_biz >= rescue_timeout and profit_pct < rebound_min and current <= entry:
            return f"스윙 구출 실패 Time/Trend 청산 ({held_biz}영업일)"
    if profit_pct <= -hard_stop:
        return f"스윙 고정 손절 (-{hard_stop:.1f}%)"
    return None


def decide_long_term_exit(
    pos: dict[str, Any],
    current: int,
    profit_pct: float,
) -> str | None:
    """장투 — 일일 소음 무시, 손절 없음, 트레일링 익절만."""
    stage = _long_force_liquidation_stage()
    if stage is not None:
        tranches = max(1, _cfg_int("LONG_FORCE_SPLIT_TRANCHES", 5))
        done = int(pos.get("long_force_exit_stage") or 0)
        if stage > done:
            return f"장투 6월 작전 분할청산 {stage}/{tranches}"
    entry = int(pos.get("entry_price") or 0)
    if entry <= 0:
        return None

    noise = _cfg_float("LONG_NOISE_FILTER_PCT", 2.0)
    peak = int(pos.get("peak_price") or entry)
    peak_profit = (peak - entry) / entry * 100 if entry else 0.0
    min_peak = _cfg_float("LONG_TRAIL_MIN_PEAK_PCT", 5.0)

    if peak_profit < min_peak:
        return None

    if not pos.get("trailing_active"):
        pos["trailing_active"] = True
        pos["trailing_drop_pct"] = _cfg_float("LONG_TRAILING_TIER1_DROP_PCT", 3.0)

    drop = float(pos.get("trailing_drop_pct") or _cfg_float("LONG_TRAILING_TIER1_DROP_PCT", 3.0))
    if peak_profit >= 15.0:
        drop = _cfg_float("LONG_TRAILING_TIER2_DROP_PCT", 5.0)
        pos["trailing_drop_pct"] = drop

    stop_line = int(pos.get("trailing_stop_price") or 0)
    if stop_line <= 0:
        stop_line = int(peak * (1 - drop / 100.0))
        pos["trailing_stop_price"] = stop_line

    if current <= stop_line and peak_profit >= min_peak:
        return "장투 추적 익절(Trailing)"

    if abs(profit_pct) < noise:
        return None
    return None


def plan_position_add(
    position: dict[str, Any],
    *,
    minute_bars: list[dict] | None = None,
    hourly_bars: list[dict] | None = None,
    capital: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """
    모드별 추가 매수(피라미딩/분할매수/적립식).
    selected_modes.json 인격 기준.
    """
    from betting_engine import quantity_for_budget

    cap = capital or {}
    available = int(cap.get("available", 0))
    mode = _normalize_mode_value(position_trading_mode(position), "swing")
    entry = int(position.get("entry_price") or 0)
    current = int(position.get("current_price") or entry)
    if entry <= 0 or current <= 0 or available <= 0:
        return None

    if mode == "scalping":
        return None

    if mode == "swing":
        adds = int(position.get("swing_add_count") or 0)
        max_adds = _cfg_int("SWING_MAX_SPLIT_BUYS", 3)
        if adds >= max_adds or position.get("pyramid_done"):
            return None
        dip_min = _cfg_float("SWING_DIP_BUY_MIN_PCT", 2.5)
        dip_max = _cfg_float("SWING_DIP_BUY_MAX_PCT", 8.0)
        drop_pct = (entry - current) / entry * 100.0
        if drop_pct < dip_min or drop_pct > dip_max:
            return None
        zone = compute_swing_accumulation_zone(hourly_bars, entry)
        if zone and current < zone["support"]:
            return None
        if _cfg_bool("SWING_RESCUE_REQUIRE_MA_SUPPORT", True):
            if not _swing_ma_support_ok(hourly_bars, current):
                return None
        deployed = int(position.get("deployed_won") or entry * int(position.get("quantity") or 0))
        target = int(position.get("target_deploy_won") or deployed * 2)
        gap = max(0, target - deployed)
        slice_won = min(available, gap, max(deployed // (adds + 2), 500_000))
        if slice_won < 300_000:
            return None
        qty = quantity_for_budget(current, slice_won)
        if qty < 1:
            return None
        slot_cap = _cfg_int("SLOT_MAX_ALLOCATION_LIMIT", 1_800_000)
        if slot_cap > 0 and deployed + slice_won > slot_cap:
            return None
        return {
            "add_budget_won": slice_won,
            "add_qty": qty,
            "reason": f"스윙 계획 분할매수 {adds + 1}/{max_adds} (눌림 {drop_pct:.1f}%)",
            "mode": "swing",
        }

    if mode == "long_term":
        if _must_force_long_term_liquidation():
            return None
        adds = int(position.get("dca_count") or 0)
        if adds >= _cfg_int("LONG_MAX_DCA_ADDS", 12):
            position["pyramid_done"] = True
            return None
        last_raw = position.get("last_dca_at")
        last_dt = _parse_position_dt(str(last_raw) if last_raw else None)
        interval = _cfg_int("LONG_DCA_INTERVAL_SEC", 86_400)
        if last_dt and (datetime.now() - last_dt).total_seconds() < interval:
            return None
        deployed = int(position.get("deployed_won") or 0)
        target = int(position.get("target_deploy_won") or deployed)
        gap = max(0, target - deployed)
        pct = _cfg_float("LONG_DCA_SLICE_PCT", 0.08)
        seed_slice = int(cap.get("total_seed", deployed) * pct)
        slice_won = min(available, gap, seed_slice, 800_000)
        if slice_won < 200_000:
            return None
        qty = quantity_for_budget(current, slice_won)
        if qty < 1:
            return None
        slot_cap = _cfg_int("SLOT_MAX_ALLOCATION_LIMIT", 1_800_000)
        if slot_cap > 0 and deployed + slice_won > slot_cap:
            return None
        return {
            "add_budget_won": slice_won,
            "add_qty": qty,
            "reason": f"장투 적립식 매수 {adds + 1}회",
            "mode": "long_term",
        }

    return None


def decide_position_exit(
    pos: dict[str, Any],
    current: int,
    profit_pct: float,
    *,
    minute_bars: list[dict] | None = None,
    hourly_bars: list[dict] | None = None,
) -> str | None:
    """모드별 실시간 청산 — selected_modes.json 인격 분기."""
    mode = _normalize_mode_value(position_trading_mode(pos), "swing")
    entry = int(pos.get("entry_price") or 0)

    if mode == "scalping":
        return decide_scalping_exit(
            pos, current, profit_pct, minute_bars=minute_bars
        )
    if mode == "long_term":
        return decide_long_term_exit(pos, current, profit_pct)
    return decide_swing_exit(
        pos, current, profit_pct, hourly_bars=hourly_bars
    )
