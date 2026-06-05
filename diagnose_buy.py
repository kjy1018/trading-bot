"""
매수·보유 관리 진단 — scan_market_leaders + 보유 손익 + 매수 경로 체크리스트.

실행: python diagnose_buy.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime

import config
from auth import get_access_token
from brain import (
    audit_config_codes_vs_brain_sources,
    build_brain_universe,
    diagnose_scan_market_leaders,
    explain_universe_load_paths,
    scan_market_leaders,
)
from market_scan import select_market_entries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("diagnose_buy")


def _print_active_entry_thresholds() -> None:
    """diagnose — config.py 완화값이 로드됐는지 확인."""
    lo = float(getattr(config, "BRAIN_PULLBACK_CHANGE_MIN", -10.0))
    hi = float(getattr(config, "BRAIN_PULLBACK_CHANGE_MAX", -1.0))
    pmin = float(getattr(config, "PULLBACK_MIN_PCT", hi))
    pmax = float(getattr(config, "PULLBACK_MAX_PCT", lo))
    top_mult = float(getattr(config, "BRAIN_TURNOVER_SPIKE_TOP_MIN_MULT", 1.5))
    default_mult = float(getattr(config, "BRAIN_TURNOVER_SPIKE_MIN_MULT", 2.5))
    top_rank = int(getattr(config, "BRAIN_TURNOVER_SPIKE_TOP_RANK", 3))
    print("\n" + "=" * 60)
    print("Active entry thresholds (from config.py)")
    print("=" * 60)
    print(
        f"PULLBACK_MIN_PCT={pmin}% · PULLBACK_MAX_PCT={pmax}% "
        f"-> BRAIN_PULLBACK [{lo}% ~ {hi}%]"
    )
    ok_pullback = pmin == -1.0 and pmax == -10.0 and lo == -10.0 and hi == -1.0
    print(
        f"  pullback relaxed: {'OK' if ok_pullback else 'MISMATCH — check config import'}"
    )
    print(
        f"turnover: TOP{top_rank}={top_mult}x (skip if below) · rank 4+={default_mult}x"
    )
    print(
        f"  turnover 4+ relaxed: {'OK' if default_mult == 2.5 else f'got {default_mult}, expected 2.5'}"
    )


def _load_positions_for_diagnose() -> dict[str, dict]:
    """positions_state.json / 메모리 슬롯에서 보유 포지션 추출."""
    try:
        from scheduler import _get_all_positions

        live = _get_all_positions()
        if live:
            return live
    except Exception:
        pass
    try:
        import trade_state

        return trade_state.load_persisted_positions()
    except Exception:
        return {}


def _print_ml_stop_loss_gate() -> None:
    from pathlib import Path

    import config
    from ml_stop_loss.dataset import load_dataset_rows
    from ml_stop_loss.predictor import load_model

    print("\n" + "=" * 60)
    print("M) ML stop-loss gate (chart pattern classifier)")
    print("=" * 60)
    enabled = bool(getattr(config, "ML_STOP_LOSS_ENABLED", True))
    thr = float(getattr(config, "ML_STOP_LOSS_REJECT_PROB", 0.60))
    model_path = Path(getattr(config, "ML_STOP_LOSS_MODEL_PATH", ""))
    ds_path = Path(getattr(config, "ML_STOP_LOSS_DATASET_PATH", ""))
    print(f"enabled={enabled} · reject_if_prob>={thr:.0%} · model={model_path.name}")
    rows = load_dataset_rows(ds_path) if ds_path.is_file() else []
    if rows:
        loss_n = sum(1 for r in rows if int(float(r.get("label_stop_loss") or 0)) == 1)
        print(f"dataset: {ds_path.name} — {len(rows)} rows (loss={loss_n}, win={len(rows)-loss_n})")
    else:
        print(f"dataset: missing — run python build_ml_dataset.py")
    model = load_model(model_path) if model_path.is_file() else None
    print(f"model loaded: {'yes' if model else 'no'}")


def _print_holdings_management() -> None:
    from scheduler import diagnose_holdings_management

    positions = _load_positions_for_diagnose()
    report = diagnose_holdings_management(positions)
    print("\n" + "=" * 60)
    print("H) Holdings management (panic stop / DCA priority)")
    print("=" * 60)
    print(
        f"PANIC_STOP={report.get('panic_threshold_pct')}% · "
        f"sell_fraction={report.get('panic_sell_fraction')} · "
        f"block_new_buys={report.get('block_new_buys')}"
    )
    print(report.get("summary", ""))
    if not report.get("holdings"):
        print("[INFO] no open positions")
        return
    for row in report.get("holdings") or []:
        flag = " [DISTRESSED]" if row.get("distressed") else ""
        print(
            f"  · {row.get('name')}({row.get('code')}) "
            f"{row.get('profit_pct'):+.2f}% mode={row.get('mode')}"
            f"{flag}"
        )
        print(f"      -> {row.get('action')}")
        if row.get("panic_stop_stage"):
            print(f"      panic_stop_stage={row.get('panic_stop_stage')}")


def _print_universe_sources_audit() -> None:
    print("\n" + "=" * 60)
    print("0) Universe load paths (no load_universe / no universe.json)")
    print("=" * 60)
    paths = explain_universe_load_paths()
    print(paths.get("note", ""))
    print("\nPipeline:")
    for line in paths.get("pipeline") or []:
        print(f"  {line}")
    print("\nFiles read for CONFIG (not universe stock list):")
    for name, info in (paths.get("files_not_universe_list") or {}).items():
        ex = "OK" if info.get("exists") else "MISSING"
        print(f"  [{ex}] {name}: {info.get('path')}")
        print(f"       role: {info.get('role')}")
    print(f"\nBRAIN_THEME_EVENTS watchlist: {paths.get('config_py_theme_watchlist')}")

    audit = audit_config_codes_vs_brain_sources()
    print("\n--- Config stock codes vs Brain sources ---")
    print(audit.get("summary", ""))
    cfg = audit.get("config_codes") or {}
    print(f"  from selected_modes: {cfg.get('from_selected_modes')}")
    print(f"  from slots_config: {cfg.get('from_slots_config')}")
    print(f"  NOT in theme watchlist (need FlowTracker): {audit.get('codes_not_in_theme_watchlist')}")
    for row in audit.get("per_code") or []:
        print(f"  · {row.get('code')}: {row.get('enters_brain_via')}")


def _print_brain_report(report: dict) -> None:
    print("\n" + "=" * 60)
    print("1) scan_market_leaders() - Brain FlowTracker filter")
    print("=" * 60)
    print(f"시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"TOP N={report.get('top_n')} · 시세 enrich={report.get('enrich_n')}")
    policy = report.get("turnover_policy") or {}
    lo = float(getattr(config, "BRAIN_PULLBACK_CHANGE_MIN", -10.0))
    hi = float(getattr(config, "BRAIN_PULLBACK_CHANGE_MAX", -1.0))
    print(
        f"pullback window: PULLBACK_MIN_PCT={getattr(config, 'PULLBACK_MIN_PCT', hi)}% "
        f"PULLBACK_MAX_PCT={getattr(config, 'PULLBACK_MAX_PCT', lo)}% "
        f"(applied {lo}% ~ {hi}%)"
    )
    if policy:
        print(
            f"회전율 임계: TOP{policy.get('top_rank', 3)}="
            f"{policy.get('top_min_mult', 1.5)}x (미달 시 skip) · "
            f"4위~={policy.get('default_min_mult', 5.0)}x"
        )
    print(f"거래대금 TOP 조회: {report.get('ranked_count')}종목")
    print(report.get("summary", ""))

    passed = report.get("passed") or []
    if passed:
        print(f"\n[PASS] all filters ({len(passed)} stocks):")
        for p in passed:
            mult = p.get("turnover_spike_mult")
            min_mult = p.get("turnover_spike_min_mult")
            flow_rank = p.get("flow_rank")
            tier = "TOP3" if p.get("turnover_spike_relaxed") else "4위~"
            if p.get("turnover_spike_bypass"):
                mult_txt = f" · 회전율 bypass({tier})"
            elif mult is not None:
                mult_txt = f" · 회전율 {mult}x>={min_mult}x({tier})"
            else:
                mult_txt = ""
            print(
                f"  · [{flow_rank}위] {p.get('name')}({p.get('code')}) "
                f"등락 {p.get('change_rate', 0):+.2f}% "
                f"flow={p.get('flow_score', 0)}{mult_txt}"
            )
    else:
        print("\n[FAIL] no stock passed Brain filters")

    rejected = report.get("rejected") or []
    if rejected:
        by_step: dict[str, int] = {}
        for r in rejected:
            step = str(r.get("fail_step") or "unknown")
            by_step[step] = by_step.get(step, 0) + 1
        print(f"\n탈락 {len(rejected)}건 — 단계별:")
        for step, cnt in sorted(by_step.items(), key=lambda x: -x[1]):
            print(f"  · {step}: {cnt}건")
        print("\n상위 10건 탈락 사유:")
        for r in rejected[:10]:
            rank_txt = f"[{r.get('flow_rank')}위] " if r.get("flow_rank") else ""
            print(
                f"  · {rank_txt}{r.get('name')}({r.get('code')}) "
                f"[{r.get('fail_step')}] {r.get('fail_reason')}"
            )


def _print_market_scan(token: str, universe: list) -> None:
    print("\n" + "=" * 60)
    print("2) select_market_entries() - actual buy scan path")
    print("=" * 60)
    picks, meta = select_market_entries(
        token,
        config.APP_KEY,
        config.APP_SECRET,
        universe,
        exclude_codes=set(),
        max_count=3,
        batch_offset=0,
    )
    print(f"유니버스 {meta.get('universe_size')} · 단타후보 {meta.get('scalp_candidates')} · "
          f"스윙후보 {meta.get('swing_candidates')}")
    if meta.get("polling_mode"):
        print("폴링 모드: 단타 스캔 OFF, 스윙만")
    if picks:
        print(f"\n[PASS] buy candidates {len(picks)}:")
        for p in picks:
            print(
                f"  · {p.get('name')}({p.get('code')}) "
                f"mode={p.get('trading_mode')} "
                f"basis={p.get('entry_basis', '-')}"
            )
    else:
        print("\n[FAIL] select_market_entries returned no picks")


def _print_buy_block_diagnosis(
    picks: list[dict],
    *,
    brain_pass_count: int,
    scan_meta: dict | None = None,
) -> None:
    """매수 미실행 원인 — 필터 통과 수 / AI 가드 / distressed 보류."""
    from scheduler import (
        _buy_paused,
        _empty_slots,
        _has_distressed_holdings,
        _should_run_realtime_scan,
        diagnose_pick_entry_checklist,
    )

    print("\n" + "=" * 60)
    print("D) Buy block diagnosis (filters / AI guard / distressed)")
    print("=" * 60)

    scan_meta = scan_meta or {}
    market_picks = len(picks)
    print(f"Brain FlowTracker 통과: {brain_pass_count}종목")
    print(
        f"select_market_entries 후보: {market_picks}종목 "
        f"(pool={scan_meta.get('pool_size', '-')}, "
        f"penalized={scan_meta.get('penalized_count', 0)})"
    )
    if scan_meta.get("today_stop_loss_codes"):
        print(f"  당일 손절 페널티 대상: {scan_meta.get('today_stop_loss_codes')}")

    distressed = _has_distressed_holdings()
    scan_ok = _should_run_realtime_scan()
    print(
        f"_has_distressed_holdings()={distressed} · "
        f"_should_run_realtime_scan()={scan_ok} · "
        f"buy_paused={_buy_paused} · empty_slots={_empty_slots()}"
    )
    if distressed:
        print(
            f"  -> 손실 {getattr(config, 'PANIC_STOP_LOSS_PCT', -5)}% 이하 보유 — "
            "신규 자동 매수 스캔 보류"
        )
    if not scan_ok and not _buy_paused and _empty_slots() > 0:
        if distressed:
            print("  -> 자동 스캔 중단 원인: distressed 보유 (위)")
        else:
            print("  -> 자동 스캔 중단: 장외/디바운스/기타 (엔진 타이밍)")

    if not picks:
        if brain_pass_count > 0 and market_picks == 0:
            print("\n[원인] Brain 통과 O -> 스윙/단타 차트 조건에서 후보 0건")
        elif brain_pass_count == 0:
            print("\n[원인] Brain 필터에서 전부 탈락")
        return

    ml_blocked: list[dict] = []
    filter_blocked: list[dict] = []
    queue_ok: list[dict] = []

    for pick in picks:
        checklist = diagnose_pick_entry_checklist(pick)
        failed = [s for s in checklist if not s.get("ok")]
        if not failed:
            queue_ok.append(pick)
            continue
        steps = {str(s.get("step")) for s in failed}
        if "ml_stop_loss" in steps:
            ml_blocked.append({"pick": pick, "failed": failed})
        else:
            filter_blocked.append({"pick": pick, "failed": failed})

    print(f"\n후보 {market_picks}건 게이트 분류:")
    print(f"  · 큐 진입 가능: {len(queue_ok)}건")
    print(f"  · AI 가드(ML) 차단: {len(ml_blocked)}건")
    print(f"  · 기존 필터/시스템 차단: {len(filter_blocked)}건")

    for row in ml_blocked:
        p = row["pick"]
        fail = next(s for s in row["failed"] if s.get("step") == "ml_stop_loss")
        print(
            f"  [AI 가드] {p.get('name')}({p.get('code')}) — {fail.get('detail')}"
        )
    for row in filter_blocked:
        p = row["pick"]
        fail = row["failed"][0]
        print(
            f"  [필터] {p.get('name')}({p.get('code')}) — "
            f"{fail.get('step')}: {fail.get('detail')}"
        )
    for p in queue_ok:
        print(f"  [OK] {p.get('name')}({p.get('code')}) — 큐 진입 가능")

    if queue_ok and distressed:
        print(
            "\n[주의] 후보+게이트 통과했으나 _has_distressed_holdings=True — "
            "엔진 자동 스캔/매수는 보류 중 (수동 매수는 별도)"
        )
    elif queue_ok and not scan_ok:
        print(
            "\n[주의] 후보+게이트 통과 — 자동 매수는 장중 스캔 타이밍/엔진 확인 필요"
        )


def _print_buy_checklist(picks: list) -> None:
    if not picks:
        return
    from scheduler import (
        _buy_paused,
        _empty_slots,
        _get_held_codes,
        _in_scan_window,
        diagnose_pick_entry_checklist,
    )

    print("\n" + "=" * 60)
    print("3) buy queue checklist (_enqueue_pick_entry gates)")
    print("=" * 60)
    now = datetime.now()
    slots = _empty_slots()
    held = _get_held_codes()
    in_window = _in_scan_window(now.time())
    print(
        f"시스템: buy_paused={_buy_paused} · 빈슬롯={slots} · "
        f"보유={len(held)} · 장중창={in_window} ({now.strftime('%H:%M')})"
    )
    if _buy_paused:
        print("[WARN] buy_paused - system setting")
    if slots <= 0:
        print("[WARN] no empty slots - system state")
    if not in_window:
        print("[WARN] outside scan window - engine scans only, no buy queue")

    for pick in picks:
        code = pick.get("code")
        name = pick.get("name") or code
        print(f"\n--- {name}({code}) ---")
        checklist = diagnose_pick_entry_checklist(pick)
        all_ok = all(s.get("ok") for s in checklist)
        for s in checklist:
            mark = "OK" if s.get("ok") else "NG"
            detail = s.get("detail") or ""
            print(f"  {mark} {s.get('step')}: {detail}")
        if all_ok:
            print("  -> buy queue entry OK")
        else:
            blocked = next(s for s in checklist if not s.get("ok"))
            print(f"  -> blocked: {blocked.get('step')} - {blocked.get('detail')}")


def main() -> int:
    if not config.APP_KEY or not config.APP_SECRET:
        print("APP_KEY / APP_SECRET 없음 — .env 확인")
        return 1

    token = get_access_token()
    print(f"토큰 OK · POLLING={getattr(config, 'POLLING_STRATEGY_MODE', False)}")

    _print_active_entry_thresholds()
    _print_ml_stop_loss_gate()
    _print_holdings_management()
    _print_universe_sources_audit()

    report = diagnose_scan_market_leaders(
        token, config.APP_KEY, config.APP_SECRET, bypass_cache=True
    )
    _print_brain_report(report)

    passed = report.get("passed") or []
    print(f"\nscan_market_leaders() 진단 통과: {len(passed)}종목 (재호출 생략 — API 한도 절약)")
    if passed:
        top = passed[0]
        print(f"  1위: {top.get('name')}({top.get('code')})")

    print("\n유니버스 빌드 중 (build_brain_universe)...")
    universe = build_brain_universe(token, config.APP_KEY, config.APP_SECRET)
    print(f"유니버스 {len(universe)}종목")
    picks, scan_meta = select_market_entries(
        token,
        config.APP_KEY,
        config.APP_SECRET,
        universe,
        exclude_codes=set(),
        max_count=3,
        batch_offset=0,
    )
    print("\n" + "=" * 60)
    print("2) select_market_entries() - actual buy scan path")
    print("=" * 60)
    print(
        f"유니버스 {scan_meta.get('universe_size')} · "
        f"단타후보 {scan_meta.get('scalp_candidates')} · "
        f"스윙후보 {scan_meta.get('swing_candidates')} · "
        f"최종 picks {len(picks)}"
    )
    if scan_meta.get("polling_mode"):
        print("폴링 모드: 단타 스캔 OFF, 스윙만")
    if picks:
        print(f"\n[PASS] buy candidates {len(picks)}:")
        for p in picks:
            print(
                f"  · {p.get('name')}({p.get('code')}) "
                f"mode={p.get('trading_mode')} "
                f"score_final={p.get('entry_score_final', p.get('entry_score', '-'))} "
                f"penalty={p.get('loss_penalty', 0)}"
            )
    else:
        print("\n[FAIL] select_market_entries returned no picks")

    _print_buy_block_diagnosis(
        picks,
        brain_pass_count=int(report.get("passed_count") or 0),
        scan_meta=scan_meta,
    )
    _print_buy_checklist(picks)

    print("\n" + "=" * 60)
    print("결론")
    print("=" * 60)
    brain_ok = bool(report.get("passed_count"))
    if not brain_ok:
        print("1) 필터 통과: Brain 0건")
    else:
        print(f"1) 필터 통과: Brain {report.get('passed_count')}건 · 매수후보 {len(picks)}건")

    from scheduler import (
        _has_distressed_holdings,
        diagnose_pick_entry_checklist,
    )

    if picks:
        ml_n = sum(
            1
            for p in picks
            if any(
                s.get("step") == "ml_stop_loss" and not s.get("ok")
                for s in diagnose_pick_entry_checklist(p)
            )
        )
        other_n = sum(
            1
            for p in picks
            if not all(s.get("ok") for s in diagnose_pick_entry_checklist(p))
        ) - ml_n
        ok_n = len(picks) - ml_n - other_n
        print(f"2) 차단: AI 가드 {ml_n}건 · 기존필터 {other_n}건 · 통과 {ok_n}건")
        if ok_n > 0 and _has_distressed_holdings():
            print("3) distressed=True — 자동 매수 스캔 보류 (게이트 통과해도 엔진이 안 삼)")
        elif ok_n > 0:
            print("3) distressed=False — 후보 통과 시 엔진 스캔/장중 타이밍 확인")
    else:
        print("2) 후보 없음 — Brain 이후 스윙/단타 차트 조건 미충족")
        if _has_distressed_holdings():
            print("3) distressed=True — 보유 손실 종목 우선 관리 중")

    return 0


if __name__ == "__main__":
    sys.exit(main())
