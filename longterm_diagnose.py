"""
장투(long_term) 슬롯 진입 필터 진단·완화 시뮬레이션.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

import config
from brain import (
    _estimate_intraday_volatility,
    build_brain_universe,
    classify_trading_mode_auto,
    diagnose_scan_market_leaders,
    enrich_stock_with_brain,
)
from stock_daily import compute_ma_bundle, fetch_daily_ohlc_bars
from trading_logic import passes_polling_entry_ma_filter

logger = logging.getLogger(__name__)


def _cfg_float(name: str, default: float) -> float:
    return float(getattr(config, name, default))


def _cfg_int(name: str, default: int) -> int:
    return int(getattr(config, name, default))


def _thresholds(*, relaxed: bool = False) -> dict[str, float]:
    r = 0.20 if relaxed else 0.0
    return {
        "large_cap_won": _cfg_int("BRAIN_LARGE_CAP_WON", 2_000_000_000_000) * (1.0 - r),
        "long_term_min_cap": _cfg_int("BRAIN_LONG_TERM_MIN_MARKET_CAP", 2_000_000_000_000)
        * (1.0 - r),
        "vol_swing_max": _cfg_float("BRAIN_VOLATILITY_SWING_MAX", 5.5) * (1.0 + r),
        "leader_min": 15.0 * (1.0 - r),
        "flow_min": 40.0 * (1.0 - r),
        "ma20_floor_pct": 2.0 * (1.0 + r) if relaxed else 0.0,
        "pullback_lo": _cfg_float("PULLBACK_MAX_PCT", -13.0) * (1.0 + r),
        "pullback_hi": _cfg_float("PULLBACK_MIN_PCT", -1.0),
    }


def _passes_daily_ma_longterm(
    daily_bars: list[dict[str, Any]] | None,
    *,
    relaxed: bool = False,
) -> tuple[bool, str]:
    if not daily_bars:
        return False, "일봉 데이터 없음"
    closes = [int(b.get("close") or 0) for b in daily_bars]
    bundle = compute_ma_bundle(closes)
    ma20 = bundle.get("ma20")
    last = bundle.get("last_close")
    aligned = bool(bundle.get("ma_aligned"))
    if ma20 is None or last is None:
        return False, "MA20/종가 계산 불가"
    if relaxed:
        floor = float(ma20) * (1.0 - _thresholds(relaxed=True)["ma20_floor_pct"] / 100.0)
        if float(last) >= floor:
            return True, f"완화 MA — 종가≥MA20-{ _thresholds(relaxed=True)['ma20_floor_pct']:.1f}%"
        return False, f"종가 {last:.0f} < MA20완화하한 {floor:.0f}"
    if not _cfg_bool("LONG_ENTRY_REQUIRE_MA_ALIGNED", True):
        if float(last) >= float(ma20):
            return True, "종가≥MA20"
        return False, f"종가 {last:.0f} < MA20 {ma20:.0f}"
    if aligned and float(last) >= float(ma20):
        return True, "정배열+종가≥MA20"
    if not aligned:
        return False, "일봉 MA 정배열(5>20>60) 미충족"
    return False, f"종가 {last:.0f} < MA20 {ma20:.0f}"


def _cfg_bool(name: str, default: bool) -> bool:
    return bool(getattr(config, name, default))


def evaluate_longterm_candidate(
    stock: dict[str, Any],
    daily_bars: list[dict[str, Any]] | None,
    *,
    relaxed: bool = False,
) -> dict[str, Any]:
    """장투 진입 게이트 단계별 평가."""
    thr = _thresholds(relaxed=relaxed)
    code = str(stock.get("code") or "")
    name = str(stock.get("name") or code)
    cap = int(stock.get("market_cap") or 0)
    vol = _estimate_intraday_volatility(stock)
    leader = float(stock.get("leader_boost") or 0)
    flow = float(stock.get("flow_score") or 0)
    chg = float(stock.get("change_rate") or 0)

    failures: list[dict[str, str]] = []

    if cap < thr["long_term_min_cap"]:
        failures.append(
            {
                "step": "market_cap",
                "reason": f"시총 {cap:,} < 장투기준 {int(thr['long_term_min_cap']):,}",
            }
        )

    if vol >= thr["vol_swing_max"]:
        failures.append(
            {
                "step": "volatility",
                "reason": f"변동성 {vol:.2f}% ≥ 한도 {thr['vol_swing_max']:.2f}%",
            }
        )

    if leader < thr["leader_min"] and flow < thr["flow_min"]:
        failures.append(
            {
                "step": "leader_flow",
                "reason": (
                    f"주도가점 {leader:.1f}<{thr['leader_min']:.1f} "
                    f"且 flow {flow:.1f}<{thr['flow_min']:.1f}"
                ),
            }
        )

    lo, hi = thr["pullback_lo"], thr["pullback_hi"]
    if chg > hi or chg < lo:
        failures.append(
            {
                "step": "pullback_range",
                "reason": f"등락 {chg:+.2f}% (허용 {lo:.1f}%~{hi:.1f}%)",
            }
        )

    ma_ok, ma_detail = _passes_daily_ma_longterm(daily_bars, relaxed=relaxed)
    if not ma_ok:
        failures.append({"step": "daily_ma", "reason": ma_detail})

    mode_tags = classify_trading_mode_auto(stock)
    auto_mode = str(mode_tags.get("trading_mode") or "")
    if auto_mode != "long_term":
        failures.append(
            {
                "step": "mode_auto",
                "reason": (
                    f"자동모드={auto_mode} ({mode_tags.get('mode_rationale', '')})"
                ),
            }
        )

    passed = len(failures) == 0
    return {
        "code": code,
        "name": name,
        "passed": passed,
        "failures": failures,
        "primary_fail": failures[0]["step"] if failures else None,
        "trading_mode_auto": auto_mode,
        "cap": cap,
        "volatility": round(vol, 2),
        "leader_boost": leader,
        "flow_score": flow,
        "change_rate": chg,
        "relaxed": relaxed,
    }


def diagnose_longterm_entries(
    access_token: str,
    app_key: str,
    app_secret: str,
    *,
    fetch_daily: bool = True,
    max_daily_fetch: int = 12,
) -> dict[str, Any]:
    """유니버스·TOP거래대금 종목에 대해 장투 필터 진단."""
    import time

    brain_report: dict[str, Any] = {}
    try:
        brain_report = diagnose_scan_market_leaders(
            access_token, app_key, app_secret, bypass_cache=True
        )
    except Exception as exc:
        logger.warning("Brain 진단 API 실패 — 로컬 풀 폴백: %s", exc)
        brain_report = {
            "passed": [],
            "rejected": [],
            "leaders": [],
            "passed_count": 0,
            "summary": f"API 실패: {exc}",
        }

    pool: dict[str, dict[str, Any]] = {}
    for row in (brain_report.get("passed") or []) + (brain_report.get("rejected") or []):
        c = str(row.get("code") or "")
        if len(c) == 6:
            pool[c] = enrich_stock_with_brain(dict(row), auto_mode=True)

    for s in brain_report.get("leaders") or []:
        c = str(s.get("code") or "")
        if len(c) == 6:
            pool[c] = enrich_stock_with_brain(dict(s), auto_mode=True)

    time.sleep(1.5)
    try:
        universe = build_brain_universe(access_token, app_key, app_secret)
        for s in universe:
            c = str(s.get("code") or "")
            if len(c) == 6:
                pool.setdefault(c, enrich_stock_with_brain(dict(s), auto_mode=True))
    except Exception as exc:
        logger.warning("build_brain_universe 스킵 (API 한도): %s", exc)
        for code in getattr(config, "BRAIN_THEME_EVENTS", []) or []:
            for item in (code.get("watchlist") if isinstance(code, dict) else []):
                pass
        try:
            from brain import _theme_event_configs
            from stock_names import normalize_code

            for event in _theme_event_configs():
                for item in event.get("watchlist") or []:
                    c = normalize_code(item.get("code"))
                    if len(c) == 6:
                        pool.setdefault(
                            c,
                            enrich_stock_with_brain(
                                {"code": c, "name": item.get("name") or c},
                                auto_mode=True,
                            ),
                        )
        except Exception:
            pass

    if not pool:
        try:
            import selected_modes
            from stock_names import normalize_code

            for key in selected_modes.load_modes():
                c = normalize_code(key)
                if len(c) == 6:
                    pool[c] = enrich_stock_with_brain({"code": c, "name": c}, auto_mode=True)
        except Exception:
            pass

    daily_cache: dict[str, list[dict]] = {}
    evaluated: list[dict[str, Any]] = []
    fetch_n = 0
    for code, stock in pool.items():
        daily: list[dict] | None = None
        if fetch_daily and fetch_n < max_daily_fetch:
            try:
                from kis_rate import kis_loop_pause

                if fetch_n > 0:
                    kis_loop_pause()
                daily = fetch_daily_ohlc_bars(
                    access_token, app_key, app_secret, code, lookback_days=90
                )
                daily_cache[code] = daily or []
                fetch_n += 1
            except Exception as exc:
                daily_cache[code] = []
                logger.debug("일봉 %s: %s", code, exc)
        else:
            daily = daily_cache.get(code)

        evaluated.append(
            evaluate_longterm_candidate(stock, daily, relaxed=False)
        )

    passed = [e for e in evaluated if e.get("passed")]
    rejected = [e for e in evaluated if not e.get("passed")]

    fail_counter = Counter()
    fail_counter_ex_turnover = Counter()
    for r in rejected:
        for f in r.get("failures") or []:
            step = str(f.get("step") or "unknown")
            fail_counter[step] += 1
            if step != "turnover_spike":
                fail_counter_ex_turnover[step] += 1

    brain_reject = brain_report.get("rejected") or []
    brain_fail = Counter(str(x.get("fail_step") or "unknown") for x in brain_reject)

    relaxed_pass = 0
    if len(passed) == 0:
        for code, stock in pool.items():
            daily = daily_cache.get(code)
            if fetch_daily and not daily and fetch_n < max_daily_fetch + 8:
                try:
                    from kis_rate import kis_loop_pause

                    kis_loop_pause()
                    daily = fetch_daily_ohlc_bars(
                        access_token, app_key, app_secret, code, lookback_days=90
                    )
                    daily_cache[code] = daily or []
                except Exception:
                    daily = []
            ev = evaluate_longterm_candidate(stock, daily, relaxed=True)
            if ev.get("passed"):
                relaxed_pass += 1

    try:
        from scheduler import _empty_slots

        empty_lt = _empty_slots(trading_mode="long_term")
        empty_all = _empty_slots()
    except Exception:
        empty_lt = empty_all = -1

    top_fail = fail_counter_ex_turnover.most_common(1)
    top_fail_step = top_fail[0][0] if top_fail else None
    top_fail_count = top_fail[0][1] if top_fail else 0

    return {
        "universe_size": len(pool),
        "evaluated": len(evaluated),
        "passed_count": len(passed),
        "rejected": rejected,
        "passed": passed,
        "fail_by_step": dict(fail_counter),
        "fail_by_step_ex_turnover": dict(fail_counter_ex_turnover),
        "top_fail_step_ex_turnover": top_fail_step,
        "top_fail_count_ex_turnover": top_fail_count,
        "brain_leader_reject_steps": dict(brain_fail),
        "brain_passed_count": brain_report.get("passed_count", 0),
        "empty_long_term_slots": empty_lt,
        "empty_slots_all": empty_all,
        "relaxed_simulation_pass": relaxed_pass,
        "relaxed_pct": 20,
        "structural_note": (
            "select_market_entries()는 스윙/단타/추세폴백만 후보화 — "
            "trading_mode=long_term 태그 종목이 없으면 장투 슬롯(4·5)에 자동 매수 안 됨"
        ),
        "thresholds": _thresholds(relaxed=False),
        "thresholds_relaxed": _thresholds(relaxed=True),
    }
