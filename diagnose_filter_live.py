"""장중 필터 실시간 리포트 — Brain / 이격도 수렴 / 스윙 정배열 + 매수 차단 변수."""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime

import config
from auth import get_access_token
from brain import build_brain_universe, diagnose_scan_market_leaders
from intraday_attack import get_intraday_attack_phase
from ma_convergence import convergence_distance, disparity_index, score_convergence_from_bars
from market_scan import select_market_entries
from stock_ranking import get_top_trading_amount_stocks, is_common_stock_for_trade
from stock_swing import SWING_MA_LONG, SWING_MA_MID, SWING_MA_SHORT, _fetch_hourly_bars, _is_golden_alignment, _is_above_or_near_ma, _ma


def _convergence_fail_reason(stock: dict, bars: list[dict]) -> tuple[bool, str, dict | None]:
    if len(bars) < 10:
        return False, "1H봉 부족(<10)", None
    period = int(getattr(config, "MA_CONVERGENCE_MA_PERIOD", 20))
    closes = [int(b["close"]) for b in bars if int(b.get("close") or 0) > 0]
    if len(closes) < period:
        return False, f"MA{period} 산출 불가(종가 {len(closes)}개)", None
    ma = _ma(closes, period)
    if ma is None or ma <= 0:
        return False, "MA 계산 실패", None
    price = int(closes[-1])
    disp = disparity_index(float(price), float(ma))
    dist = convergence_distance(disp)
    max_dist = float(getattr(config, "MA_CONVERGENCE_MAX_DIST_PCT", 8.0))
    if dist > max_dist:
        return False, f"이격도 {disp:.2f} (100과 거리 {dist:.2f}% > 허용 {max_dist}%)", None
    result = score_convergence_from_bars(stock, bars)
    if not result:
        return False, f"이격도 {disp:.2f} — 점수 산출 실패", None
    _, enriched = result
    return True, "", enriched


def _swing_fail_reason(stock: dict, bars: list[dict], *, afternoon: bool) -> tuple[bool, str]:
    if len(bars) < SWING_MA_LONG + 5:
        return False, "1H봉 부족"
    closes = [b["close"] for b in bars]
    price = closes[-1]
    require_golden = afternoon and bool(getattr(config, "SWING_AFTERNOON_REQUIRE_GOLDEN", True))
    if require_golden:
        if not _is_golden_alignment(closes):
            ma5 = _ma(closes, SWING_MA_SHORT)
            ma20 = _ma(closes, SWING_MA_MID)
            ma60 = _ma(closes, SWING_MA_LONG)
            return False, (
                f"정배열 미충족 (MA5={ma5:.0f} MA20={ma20:.0f} MA60={ma60:.0f})"
                if ma5 and ma20 and ma60
                else "정배열 미충족"
            )
    elif bool(getattr(config, "SWING_REQUIRE_MA_PROXIMITY", True)):
        if not _is_above_or_near_ma(closes, price):
            tol = float(getattr(config, "SWING_MA_PROXIMITY_PCT", 5.0))
            return False, f"MA 근접 미충족 (±{tol}% 이내 아님)"
    return False, "눌림목/거래량/ATR 등 2차 조건 미충족"


def _print_control_vars() -> None:
    from engine_cooldown import cooldown_snapshot, is_cooldown_active
    from scheduler import (
        _buy_paused,
        _empty_slots,
        _has_distressed_holdings,
        _in_scan_window,
        _should_run_realtime_scan,
    )
    from slot_registry import count_empty_slots
    import trade_state

    now = datetime.now()
    phase = get_intraday_attack_phase(now)
    slots = trade_state.get_slots_book()
    empty_all = count_empty_slots(slots)
    empty_swing = count_empty_slots(slots, slot_type="swing")
    empty_lt = count_empty_slots(slots, slot_type="long_term")
    empty_dt = count_empty_slots(slots, slot_type="day_trading")

    print("\n" + "=" * 60)
    print("매수 차단 · 실시간 제어 변수 (별도 프로세스 기준)")
    print("=" * 60)
    print(f"시각: {now.strftime('%Y-%m-%d %H:%M:%S')} · attack_phase={phase}")
    cd = cooldown_snapshot()
    print(
        f"buy_paused={_buy_paused} · cooldown_active={is_cooldown_active()} "
        f"(남은 {cd.get('remaining_sec', 0):.0f}초, 사유={cd.get('reason') or '-'})"
    )
    print(
        f"distressed={_has_distressed_holdings()} · "
        f"scan_window={_in_scan_window(now.time())} · "
        f"should_run_scan={_should_run_realtime_scan()}"
    )
    print(
        f"빈 슬롯: 전체 {empty_all}/5 · 스윙 {empty_swing} · 장투 {empty_lt} · "
        f"단타 {empty_dt} (_empty_slots={_empty_slots()})"
    )
    print(
        f"POLLING_DISABLE_DAY_TRADING="
        f"{getattr(config, 'POLLING_DISABLE_DAY_TRADING', True)}"
    )
    print(
        "\n[참고] Streamlit 엔진 프로세스의 buy_paused/cooldown은 메모리 전용. "
        "실행 중 터미널 로그도 함께 확인하세요."
    )


def main() -> int:
    if not config.APP_KEY:
        print("APP_KEY 없음")
        return 1

    token = get_access_token()
    now = datetime.now()
    phase = get_intraday_attack_phase(now)

    print("=" * 60)
    print(f"장중 필터 실시간 리포트 · {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"attack_phase={phase} · POLLING={getattr(config, 'POLLING_STRATEGY_MODE', False)}")
    print("=" * 60)

    brain = diagnose_scan_market_leaders(
        token, config.APP_KEY, config.APP_SECRET, bypass_cache=True
    )
    passed = brain.get("passed") or []
    rejected = brain.get("rejected") or []
    print(f"\n[A] Brain FlowTracker 통과: {len(passed)}종목 / TOP{brain.get('top_n')} 검사")
    for p in passed:
        print(
            f"  PASS {p.get('name')}({p.get('code')}) "
            f"등락 {p.get('change_rate', 0):+.2f}% flow={p.get('flow_score')}"
        )

    if not passed:
        by_step = Counter(str(r.get("fail_step") or "?") for r in rejected)
        print("\n  Brain 탈락 TOP:")
        for step, cnt in by_step.most_common():
            print(f"    · {step}: {cnt}건")
        print("\n  종목별 탈락 (전체):")
        for r in rejected:
            rank = f"[{r.get('flow_rank')}위] " if r.get("flow_rank") else ""
            print(
                f"    · {rank}{r.get('name')}({r.get('code')}) "
                f"[{r.get('fail_step')}] {r.get('fail_reason')}"
            )

    universe = build_brain_universe(token, config.APP_KEY, config.APP_SECRET)
    ranked = get_top_trading_amount_stocks(
        token, config.APP_KEY, config.APP_SECRET, limit=20
    )
    scan_pool = universe if universe else ranked

    conv_pass: list[dict] = []
    conv_fail: list[tuple[str, str, str]] = []
    swing_pass: list[dict] = []
    swing_fail: list[tuple[str, str, str]] = []

    print(f"\n[B] 이격도 수렴 (MA{getattr(config, 'MA_CONVERGENCE_MA_PERIOD', 20)}, "
          f"허용거리≤{getattr(config, 'MA_CONVERGENCE_MAX_DIST_PCT', 8)}%) "
          f"— {len(scan_pool)}종목 스캔")
    for stock in scan_pool[:20]:
        code = str(stock.get("code") or "")
        name = str(stock.get("name") or code)
        if not is_common_stock_for_trade(stock):
            conv_fail.append((code, name, "common_stock: ETF/비일반주"))
            continue
        try:
            bars = _fetch_hourly_bars(token, config.APP_KEY, config.APP_SECRET, code)
            ok, reason, enriched = _convergence_fail_reason(stock, bars or [])
            if ok and enriched:
                conv_pass.append(enriched)
            else:
                conv_fail.append((code, name, reason))
        except Exception as exc:
            conv_fail.append((code, name, f"API 오류: {exc}"))

    print(f"  통과: {len(conv_pass)}종목")
    for row in conv_pass:
        print(
            f"    PASS {row.get('name')}({row.get('code')}) "
            f"이격도={row.get('disparity_index')} dist={row.get('convergence_dist')} "
            f"score={row.get('entry_score')}"
        )
    if not conv_pass and conv_fail:
        reasons = Counter(r for _, _, r in conv_fail)
        print("  탈락 요약:")
        for reason, cnt in reasons.most_common(5):
            print(f"    · {reason}: {cnt}건")
        print("  종목별:")
        for code, name, reason in conv_fail:
            print(f"    · {name}({code}) — {reason}")

    afternoon = phase == "afternoon_swing"
    label = "오후 정배열 필수" if afternoon else "MA 근접(정배열 선택)"
    print(f"\n[C] 스윙 1H 필터 ({label}) — Brain 유니버스 {len(universe)}종목")
    for stock in universe:
        code = str(stock.get("code") or "")
        name = str(stock.get("name") or code)
        try:
            bars = _fetch_hourly_bars(token, config.APP_KEY, config.APP_SECRET, code)
            from stock_swing import _passes_quality_and_setup

            req_golden = afternoon and bool(getattr(config, "SWING_AFTERNOON_REQUIRE_GOLDEN", True))
            result = _passes_quality_and_setup(
                stock, bars or [], require_golden=req_golden if afternoon else None
            )
            if result:
                _, enriched = result
                swing_pass.append(enriched)
            else:
                reason = _swing_fail_reason(stock, bars or [], afternoon=afternoon)
                swing_fail.append((code, name, reason[1]))
        except Exception as exc:
            swing_fail.append((code, name, f"API: {exc}"))

    print(f"  통과: {len(swing_pass)}종목")
    for row in swing_pass:
        print(f"    PASS {row.get('name')}({row.get('code')}) setup={row.get('setup')}")
    for code, name, reason in swing_fail:
        print(f"    FAIL {name}({code}) — {reason}")

    picks, meta = select_market_entries(
        token,
        config.APP_KEY,
        config.APP_SECRET,
        universe or ranked,
        exclude_codes=set(),
        max_count=3,
    )
    print(f"\n[D] select_market_entries (실제 매수 경로) picks={len(picks)}")
    print(f"  meta: phase={meta.get('attack_phase')} pool={meta.get('pool_size')} "
          f"conv={meta.get('convergence_candidates')} swing={meta.get('swing_candidates')}")
    for p in picks:
        print(
            f"  · {p.get('name')}({p.get('code')}) track={p.get('entry_track')} "
            f"mode={p.get('trading_mode')} score={p.get('entry_score_final', p.get('entry_score'))}"
        )

    _print_control_vars()
    return 0


if __name__ == "__main__":
    sys.exit(main())
