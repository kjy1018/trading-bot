"""
AI 종합 예측 엔진 — 당일·주간 기대 수익률 범위, 모드 비중(단타/스윙/장투).

KIS 시장 스냅샷(거래대금·테마·지수 프록시) + 규칙 기반 전략 추론.
OPENAI_API_KEY 설정 시 LLM으로 서술·범위 정교화(선택).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any

import requests

import config
from auth import get_access_token
from kis_rate import kis_request
from stock_ranking import get_top_trading_amount_stocks

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_last_refresh_at: float = 0.0
_slot_quote_enrich_at: dict[str, float] = {}

# 메모리 캐시 (trade_state.json 동기화)
_cached_forecast: dict[str, Any] | None = None

THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "우주항공": ("우주", "항공", "로켓", "위성", "스페이스", "SPACE", "항공우주", "갤럭시"),
    "AI/반도체": ("AI", "인공지능", "반도체", "HBM", "GPU", "엔비디아", "데이터", "클라우드"),
    "2차전지": ("2차전지", "배터리", "양극", "음극", "리튬", "전고체"),
    "바이오": ("바이오", "제약", "신약", "의료", "헬스"),
    "로봇": ("로봇", "자동화", "모션"),
}


def _cfg(name: str, default: Any) -> Any:
    return getattr(config, name, default)


_SLOT_API_DELAY_SEC = max(float(getattr(config, "KIS_API_MIN_INTERVAL_SEC", 0.25)), 0.25)
_SLOT_API_RETRY_WAIT_SEC = 1.0
_SLOT_API_RETRY_MAX = 2


def _sleep_slot_throttle(delay_sec: float = _SLOT_API_DELAY_SEC) -> None:
    """슬롯 API 루프 안전벨트: 초당 호출 과다 방지."""
    time.sleep(max(0.2, float(delay_sec)))


def _monday_of(day: date | None = None) -> date:
    d = day or date.today()
    return d - timedelta(days=d.weekday())


def _trade_state_path():
    from trade_state import TRADE_STATE_FILE

    return TRADE_STATE_FILE


def _load_trade_state() -> dict[str, Any]:
    from trade_state import _ensure_daily_unlocked

    with _lock:
        return _ensure_daily_unlocked()


def _save_ai_block(ai: dict[str, Any]) -> None:
    from trade_state import TRADE_STATE_FILE, _save_json

    with _lock:
        data = _load_trade_state()
        data["ai_forecast"] = ai
        _save_json(TRADE_STATE_FILE, data)


def _default_ai_block() -> dict[str, Any]:
    monday = _monday_of().isoformat()
    today = date.today().isoformat()
    return {
        "daily": {
            "date": today,
            "pct_low": 0.0,
            "pct_high": 0.0,
            "pct_mid": 0.0,
            "narrative": "시장 분석 대기 중",
            "themes": [],
            "liquidity_score": 0,
            "updated_at": None,
        },
        "weekly": {
            "week_start": monday,
            "pct_low": 0.0,
            "pct_high": 0.0,
            "pct_mid": 0.0,
            "narrative": "주간 가이드 대기 중",
            "macro_events": [],
            "kosdaq_trend": "neutral",
            "updated_at": None,
        },
        "allocation": {
            "scalp_weight": 0.35,
            "swing_weight": 0.45,
            "long_weight": 0.20,
            "updated_at": None,
        },
    }


def _ensure_ai_block(data: dict[str, Any]) -> dict[str, Any]:
    today = date.today().isoformat()
    monday = _monday_of().isoformat()
    defaults = _default_ai_block()
    ai = data.get("ai_forecast")
    if not isinstance(ai, dict):
        ai = defaults
    else:
        if (ai.get("daily") or {}).get("date") != today:
            ai["daily"] = dict(defaults["daily"])
        if (ai.get("weekly") or {}).get("week_start") != monday:
            ai["weekly"] = dict(defaults["weekly"])
        ai.setdefault("allocation", dict(defaults["allocation"]))
    data["ai_forecast"] = ai
    return ai


def get_ai_forecast_cached() -> dict[str, Any]:
    """UI·스케줄러용 최신 예측 (JSON 영속)."""
    global _cached_forecast
    with _lock:
        data = _load_trade_state()
        ai = _ensure_ai_block(data)
        _cached_forecast = dict(ai)
        return dict(ai)


def get_ai_signature() -> tuple[Any, ...]:
    ai = get_ai_forecast_cached()
    d = ai.get("daily") or {}
    w = ai.get("weekly") or {}
    a = ai.get("allocation") or {}
    return (
        d.get("date"),
        round(float(d.get("pct_mid", 0)), 2),
        d.get("updated_at"),
        w.get("week_start"),
        round(float(w.get("pct_mid", 0)), 2),
        w.get("updated_at"),
        round(float(a.get("scalp_weight", 0)), 2),
    )


def get_active_allocation() -> dict[str, float]:
    ai = get_ai_forecast_cached()
    alloc = ai.get("allocation") or {}
    scalp = float(alloc.get("scalp_weight", 0.35))
    swing = float(alloc.get("swing_weight", 0.45))
    long_w = float(alloc.get("long_weight", 0.20))
    total = scalp + swing + long_w or 1.0
    return {
        "scalp_weight": scalp / total,
        "swing_weight": swing / total,
        "long_weight": long_w / total,
    }


def _detect_themes(stocks: list[dict]) -> list[dict[str, Any]]:
    hits: dict[str, dict[str, Any]] = {}
    for s in stocks:
        name = str(s.get("name") or "")
        upper = name.upper()
        chg = float(s.get("change_rate") or 0)
        for theme, kws in THEME_KEYWORDS.items():
            if any(kw in name or kw.upper() in upper for kw in kws):
                bucket = hits.setdefault(
                    theme,
                    {"theme": theme, "count": 0, "avg_change": 0.0, "names": []},
                )
                bucket["count"] += 1
                bucket["avg_change"] += chg
                if len(bucket["names"]) < 3:
                    bucket["names"].append(name)
    out: list[dict[str, Any]] = []
    for theme, b in hits.items():
        if b["count"] > 0:
            b["avg_change"] = round(b["avg_change"] / b["count"], 2)
            b["strong"] = b["avg_change"] >= 3.0 and b["count"] >= 2
            out.append(b)
    out.sort(key=lambda x: (x["strong"], x["count"], x["avg_change"]), reverse=True)
    return out


def _macro_events_for_week(week_start: date) -> list[dict[str, str]]:
    """설정 기반 거시 일정 + 주차 키워드 매칭."""
    events: list[dict[str, str]] = []
    calendar = _cfg("AI_MACRO_CALENDAR", [])
    if not isinstance(calendar, list):
        return events
    week_num = week_start.isocalendar()[1]
    for item in calendar:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", ""))
        weeks = item.get("active_weeks")
        if weeks and week_num not in weeks:
            continue
        events.append({
            "title": title,
            "impact": str(item.get("impact", "neutral")),
            "note": str(item.get("note", "")),
        })
    return events


def _fetch_kosdaq_proxy(token: str) -> dict[str, Any]:
    """코스닥 추세 프록시 — 거래대금 상위 코스닥 성격 종목 평균 등락."""
    try:
        top = get_top_trading_amount_stocks(
            token, config.APP_KEY, config.APP_SECRET, limit=40
        )
        if not top:
            return {"avg_change": 0.0, "breadth_up_pct": 50.0, "trend": "neutral"}
        changes = [float(s.get("change_rate") or 0) for s in top]
        up = sum(1 for c in changes if c > 0)
        avg = sum(changes) / len(changes)
        breadth = up / len(changes) * 100.0
        if avg >= 1.2 and breadth >= 58:
            trend = "bullish"
        elif avg <= -0.8 or breadth <= 42:
            trend = "bearish"
        else:
            trend = "neutral"
        return {
            "avg_change": round(avg, 2),
            "breadth_up_pct": round(breadth, 1),
            "trend": trend,
        }
    except Exception as exc:
        logger.warning("코스닥 프록시 조회 실패: %s", exc)
        return {"avg_change": 0.0, "breadth_up_pct": 50.0, "trend": "neutral"}


def enrich_commander_slots_with_quotes(
    access_token: str,
    slots: list[dict[str, Any]],
    *,
    min_interval_sec: float | None = None,
) -> list[dict[str, Any]]:
    """
    슬롯 등록 종목만 현재가·등락·호가 강도(근사) 조회 — 시장 전체 스캔 없음.

    보유 종목은 스케줄러/WS 스냅샷 시세를 우선하고, KIS REST는 쓰로틀하여 UI 멈춤 방지.
    """
    from stock import get_current_price
    from stock_names import normalize_code

    if min_interval_sec is None:
        min_interval_sec = float(
            _cfg("COMMANDER_PNL_REFRESH_SEC", _cfg("UI_REFRESH_INTERVAL_SEC", 5))
        )

    try:
        from quote import fetch_bid_ladder
    except ImportError:
        fetch_bid_ladder = None  # type: ignore

    now = datetime.now().timestamp()
    out: list[dict[str, Any]] = []
    for raw in slots:
        _sleep_slot_throttle()
        slot = dict(raw)
        code = normalize_code(slot.get("code"))
        if len(code) != 6:
            out.append(slot)
            continue

        snap_px = int(slot.get("current_price") or slot.get("price") or 0)
        if slot.get("is_held") and snap_px > 0:
            slot.setdefault("bid_pressure", 0.5)
            out.append(slot)
            continue

        last = _slot_quote_enrich_at.get(code, 0.0)
        if now - last < min_interval_sec and snap_px > 0:
            slot.setdefault("bid_pressure", 0.5)
            out.append(slot)
            continue

        got_quote = False
        for attempt in range(_SLOT_API_RETRY_MAX + 1):
            try:
                q = get_current_price(
                    access_token, code, config.APP_KEY, config.APP_SECRET
                )
                px = int(q.get("price") or snap_px or 0)
                if px > 0:
                    slot["current_price"] = px
                    slot["price"] = px
                if q.get("change_rate") is not None:
                    slot["change_rate"] = float(q["change_rate"])
                _slot_quote_enrich_at[code] = now
                got_quote = True
                break
            except Exception as exc:
                logger.debug("슬롯 시세 %s 재시도(%d): %s", code, attempt + 1, exc)
                if attempt < _SLOT_API_RETRY_MAX:
                    time.sleep(_SLOT_API_RETRY_WAIT_SEC)
        if not got_quote:
            logger.warning("슬롯 시세 조회 최종 실패 %s", code)

        if fetch_bid_ladder is not None and not slot.get("is_held"):
            got_ladder = False
            for attempt in range(_SLOT_API_RETRY_MAX + 1):
                try:
                    _sleep_slot_throttle()
                    ladder = fetch_bid_ladder(
                        access_token,
                        code,
                        config.APP_KEY,
                        config.APP_SECRET,
                        levels=3,
                    )
                    bid_vol = sum(int(v) for _, v in ladder) if ladder else 0
                    slot["bid_volume_proxy"] = bid_vol
                    slot["bid_pressure"] = (
                        min(1.0, bid_vol / 50_000) if bid_vol > 0 else 0.5
                    )
                    got_ladder = True
                    break
                except Exception as exc:
                    logger.debug("슬롯 호가 %s 재시도(%d): %s", code, attempt + 1, exc)
                    if attempt < _SLOT_API_RETRY_MAX:
                        time.sleep(_SLOT_API_RETRY_WAIT_SEC)
            if not got_ladder:
                slot.setdefault("bid_pressure", 0.5)
        else:
            slot.setdefault("bid_pressure", 0.5)
        out.append(slot)
    return out


def collect_commander_slot_snapshot(
    access_token: str | None,
    commander_slots: list[dict[str, Any]],
) -> dict[str, Any]:
    """지휘관 슬롯 종목만 스냅샷 — 거래대금 TOP 스캔 없음."""
    token = access_token or get_access_token()
    slots = (
        enrich_commander_slots_with_quotes(token, commander_slots)
        if commander_slots
        else []
    )
    changes = [float(s.get("change_rate") or 0) for s in slots]
    avg_change = sum(changes) / len(changes) if changes else 0.0
    now = datetime.now()
    session = "pre_open"
    if dt_time(9, 5) <= now.time() < dt_time(12, 0):
        session = "morning"
    elif dt_time(12, 0) <= now.time() < dt_time(15, 20):
        session = "afternoon"
    elif now.time() >= dt_time(15, 20):
        session = "closed"
    return {
        "ts": now.isoformat(timespec="seconds"),
        "session": session,
        "commander_only": True,
        "slot_count": len(slots),
        "slots": slots,
        "avg_change_slots": round(avg_change, 2),
        "news_hints": [],
    }


def collect_market_snapshot(
    access_token: str | None = None,
    universe: list[dict] | None = None,
) -> dict[str, Any]:
    token = access_token or get_access_token()
    top = get_top_trading_amount_stocks(
        token, config.APP_KEY, config.APP_SECRET, limit=50
    )
    if universe:
        codes = {s["code"] for s in universe[:80]}
        extra = [s for s in universe if s["code"] in codes][:30]
        seen = {t["code"] for t in top}
        for s in extra:
            if s["code"] not in seen:
                top.append(s)
                seen.add(s["code"])

    total_amount = sum(int(s.get("trade_amount") or 0) for s in top)
    avg_change = (
        sum(float(s.get("change_rate") or 0) for s in top) / len(top) if top else 0.0
    )
    themes = _detect_themes(top)
    hot_themes = [t for t in themes if t.get("strong")]
    kosdaq = _fetch_kosdaq_proxy(token)

    now = datetime.now()
    session = "pre_open"
    if dt_time(9, 5) <= now.time() < dt_time(12, 0):
        session = "morning"
    elif dt_time(12, 0) <= now.time() < dt_time(15, 20):
        session = "afternoon"
    elif now.time() >= dt_time(15, 20):
        session = "closed"

    news_hints = _cfg("AI_MANUAL_NEWS_HINTS", [])
    if not isinstance(news_hints, list):
        news_hints = []

    return {
        "ts": now.isoformat(timespec="seconds"),
        "session": session,
        "top_count": len(top),
        "total_trade_amount": total_amount,
        "avg_change_top": round(avg_change, 2),
        "themes": themes,
        "hot_themes": hot_themes,
        "kosdaq": kosdaq,
        "news_hints": [str(x) for x in news_hints[:5]],
    }


def _rule_daily_forecast(snapshot: dict[str, Any]) -> dict[str, Any]:
    avg = float(snapshot.get("avg_change_top", 0))
    hot = snapshot.get("hot_themes") or []
    kosdaq = snapshot.get("kosdaq") or {}
    k_trend = kosdaq.get("trend", "neutral")
    session = snapshot.get("session", "morning")
    liquidity = min(100, int(snapshot.get("total_trade_amount", 0) / 50_000_000_000))

    base_mid = 1.5
    if k_trend == "bullish":
        base_mid += 1.2
    elif k_trend == "bearish":
        base_mid -= 1.0
    base_mid += avg * 0.35
    base_mid += len(hot) * 0.6
    if session == "morning" and len(hot) >= 2:
        base_mid += 0.8
    if session == "afternoon":
        base_mid *= 0.85

    base_mid = max(0.3, min(8.0, base_mid))
    spread = 1.2 + len(hot) * 0.3
    pct_low = round(max(0.0, base_mid - spread), 2)
    pct_high = round(base_mid + spread, 2)
    pct_mid = round((pct_low + pct_high) / 2, 2)

    theme_txt = ", ".join(t["theme"] for t in hot[:3]) or "뚜렷한 주도 테마 없음"
    narrative = (
        f"🧠 AI 판단: 거래대금·{session} 세션 기준, "
        f"주도({theme_txt}), 코스닥 {k_trend}({kosdaq.get('avg_change', 0):+.1f}%) → "
        f"당일 매매 기대 {pct_low:+.1f}~{pct_high:+.1f}%"
    )
    hints = snapshot.get("news_hints") or []
    if hints:
        narrative += f" · 참고: {hints[0][:40]}"

    return {
        "date": date.today().isoformat(),
        "pct_low": pct_low,
        "pct_high": pct_high,
        "pct_mid": pct_mid,
        "narrative": narrative,
        "themes": hot,
        "liquidity_score": liquidity,
        "market_mood": k_trend,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _rule_weekly_forecast(snapshot: dict[str, Any], week_start: date) -> dict[str, Any]:
    kosdaq = snapshot.get("kosdaq") or {}
    k_trend = kosdaq.get("trend", "neutral")
    hot = snapshot.get("hot_themes") or []
    events = _macro_events_for_week(week_start)

    base_mid = 4.0
    if k_trend == "bullish":
        base_mid += 3.0
    elif k_trend == "bearish":
        base_mid -= 2.0
    for ev in events:
        if ev.get("impact") == "bullish":
            base_mid += 1.5
        elif ev.get("impact") == "bearish":
            base_mid -= 1.5
    base_mid += len(hot) * 0.8
    base_mid = max(1.0, min(18.0, base_mid))

    spread = 2.5
    pct_low = round(max(0.5, base_mid - spread), 2)
    pct_high = round(base_mid + spread, 2)
    pct_mid = round((pct_low + pct_high) / 2, 2)

    ev_txt = " · ".join(e["title"] for e in events[:2]) or "주요 거시 이벤트 없음"
    narrative = (
        f"🧠 주간 AI 가이드: 코스닥 {k_trend}, {ev_txt} → "
        f"주간 종합 기대 {pct_low:+.1f}~{pct_high:+.1f}%"
    )

    return {
        "week_start": week_start.isoformat(),
        "pct_low": pct_low,
        "pct_high": pct_high,
        "pct_mid": pct_mid,
        "narrative": narrative,
        "macro_events": events,
        "kosdaq_trend": k_trend,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _compute_allocation(daily: dict[str, Any], weekly: dict[str, Any]) -> dict[str, Any]:
    """판세 예측 → 단타/스윙/장투 비중."""
    d_mid = float(daily.get("pct_mid", 0))
    w_mid = float(weekly.get("pct_mid", 0))
    hot_count = len(daily.get("themes") or [])
    mood = daily.get("market_mood", "neutral")
    kosdaq = weekly.get("kosdaq_trend", mood)

    scalp = 0.30
    swing = 0.45
    long_w = 0.25

    if hot_count >= 2 and d_mid >= 2.5:
        scalp = 0.50
        swing = 0.35
        long_w = 0.15
    elif kosdaq == "bullish" and w_mid >= 6.0:
        scalp = 0.25
        swing = 0.40
        long_w = 0.35
    elif mood == "bearish" or w_mid < 3.0:
        scalp = 0.40
        swing = 0.45
        long_w = 0.15
    elif d_mid < 1.5:
        scalp = 0.28
        swing = 0.52
        long_w = 0.20

    total = scalp + swing + long_w
    return {
        "scalp_weight": round(scalp / total, 3),
        "swing_weight": round(swing / total, 3),
        "long_weight": round(long_w / total, 3),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _maybe_llm_refine(
    snapshot: dict[str, Any],
    daily: dict[str, Any],
    weekly: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or not _cfg("AI_USE_LLM_REFINE", False):
        return daily, weekly

    try:
        prompt = {
            "snapshot": {
                "avg_change": snapshot.get("avg_change_top"),
                "themes": [t.get("theme") for t in snapshot.get("hot_themes", [])],
                "kosdaq": snapshot.get("kosdaq"),
                "news": snapshot.get("news_hints"),
            },
            "rule_daily": daily,
            "rule_weekly": weekly,
        }
        body = {
            "model": _cfg("AI_LLM_MODEL", "gpt-4o-mini"),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "한국 주식 단타·스윙 봇 전략가. JSON만 출력: "
                        '{"daily":{"pct_low", "pct_high", "narrative"}, '
                        '"weekly":{"pct_low", "pct_high", "narrative"}}'
                    ),
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "temperature": 0.4,
        }
        throttle(1.0)
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=45,
        )
        if resp.status_code != 200:
            return daily, weekly
        text = resp.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return daily, weekly
        parsed = json.loads(m.group())
        if "daily" in parsed:
            daily = {**daily, **parsed["daily"]}
            daily["pct_mid"] = round(
                (float(daily.get("pct_low", 0)) + float(daily.get("pct_high", 0))) / 2, 2
            )
        if "weekly" in parsed:
            weekly = {**weekly, **parsed["weekly"]}
            weekly["pct_mid"] = round(
                (float(weekly.get("pct_low", 0)) + float(weekly.get("pct_high", 0))) / 2,
                2,
            )
    except Exception as exc:
        logger.warning("LLM 예측 정교화 스킵: %s", exc)
    return daily, weekly


def refresh_ai_forecasts(
    *,
    access_token: str | None = None,
    universe: list[dict] | None = None,
    commander_slots: list[dict[str, Any]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """
    당일·주간 AI 예측 갱신 (trade_state.json 영속).
    - 당일: commander_slots 가 있으면 슬롯 등록 종목만 (시장 TOP 스캔 없음)
    - 주간: 기존 규칙 (universe 있으면 병합)
    """
    global _last_refresh_at, _cached_forecast

    interval = float(_cfg("AI_FORECAST_REFRESH_SEC", 600))
    now_ts = datetime.now().timestamp()
    if not force and _last_refresh_at > 0 and now_ts - _last_refresh_at < interval:
        return get_ai_forecast_cached()

    with _lock:
        data = _load_trade_state()
        ai = _ensure_ai_block(data)
        today = date.today().isoformat()
        monday = _monday_of().isoformat()

        daily = ai.get("daily") or {}
        need_daily = (
            force
            or daily.get("date") != today
            or not daily.get("updated_at")
            or "대기 중" in str(daily.get("narrative", ""))
        )
        need_weekly = (
            force
            or ai["weekly"].get("week_start") != monday
            or not ai["weekly"].get("updated_at")
        )
        stale_daily = True
        updated = daily.get("updated_at")
        if updated:
            try:
                u = datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")
                stale_daily = (datetime.now() - u).total_seconds() >= interval
            except ValueError:
                pass
        if not need_daily and not stale_daily and not need_weekly:
            _cached_forecast = dict(ai)
            return dict(ai)

        commander_daily = commander_slots is not None
        if commander_daily:
            from trading_logic import (
                assemble_commander_slots,
                compute_commander_ai_daily_forecast,
            )

            slots = list(commander_slots or [])
            snap_cmd = collect_commander_slot_snapshot(access_token, slots)
            positions = [s for s in slots if s.get("is_held")]
            if need_daily or stale_daily:
                ai["daily"] = compute_commander_ai_daily_forecast(
                    snap_cmd.get("slots") or slots,
                    positions=positions,
                )
            snapshot = snap_cmd
            if need_weekly:
                snapshot = collect_market_snapshot(access_token, universe)
                ai["weekly"] = _rule_weekly_forecast(snapshot, _monday_of())
        else:
            snapshot = collect_market_snapshot(access_token, universe)
            if need_daily or stale_daily:
                ai["daily"] = _rule_daily_forecast(snapshot)
            if need_weekly:
                ai["weekly"] = _rule_weekly_forecast(snapshot, _monday_of())

        if str((ai.get("daily") or {}).get("source")) == "commander_slots":
            ai["allocation"] = _compute_allocation(ai["daily"], ai["weekly"])
        else:
            ai["daily"], ai["weekly"] = _maybe_llm_refine(
                snapshot, ai["daily"], ai["weekly"]
            )
            ai["allocation"] = _compute_allocation(ai["daily"], ai["weekly"])
        _save_ai_block(ai)
        _cached_forecast = dict(ai)
        _last_refresh_at = now_ts
        logger.info(
            "AI 예측 갱신 — 당일 %.1f~%.1f%% · 주간 %.1f~%.1f%% · 단타비중 %.0f%%",
            ai["daily"]["pct_low"],
            ai["daily"]["pct_high"],
            ai["weekly"]["pct_low"],
            ai["weekly"]["pct_high"],
            ai["allocation"]["scalp_weight"] * 100,
        )
        return dict(ai)
