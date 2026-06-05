"""
장투 슬롯 매수 미실행 진단.

실행: python diagnose_longterm.py
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta

import config
from auth import get_access_token
from longterm_diagnose import diagnose_longterm_entries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("diagnose_longterm")


def _print_thresholds(report: dict) -> None:
    print("\n" + "=" * 60)
    print("Active long-term thresholds")
    print("=" * 60)
    t = report.get("thresholds") or {}
    print(f"  시총≥{int(t.get('long_term_min_cap', 0)):,} · 변동성<{t.get('vol_swing_max', 0):.2f}%")
    print(
        f"  주도가점≥{t.get('leader_min', 0):.1f} 또는 flow≥{t.get('flow_min', 0):.1f}"
    )
    print(
        f"  눌림 {t.get('pullback_lo', 0):.1f}% ~ {t.get('pullback_hi', 0):.1f}% · "
        f"LONG_ENTRY_REQUIRE_MA_ALIGNED={getattr(config, 'LONG_ENTRY_REQUIRE_MA_ALIGNED', True)}"
    )
    print(f"  빈 장투 슬롯: {report.get('empty_long_term_slots')} / 전체 빈슬롯 {report.get('empty_slots_all')}")
    print(f"\n[구조] {report.get('structural_note')}")


def _print_rejections(report: dict) -> None:
    print("\n" + "=" * 60)
    print("Today's long-term filter rejections")
    print("=" * 60)
    print(f"평가 종목: {report.get('evaluated')} · 장투 통과: {report.get('passed_count')}")

    rejected = report.get("rejected") or []
    if not rejected:
        print("[INFO] 탈락 목록 없음 (통과만 존재)")
        return

    print(f"\n탈락 {len(rejected)}종목 — 상세:")
    for row in sorted(rejected, key=lambda r: str(r.get("code") or "")):
        fails = row.get("failures") or []
        steps = ", ".join(f["step"] for f in fails)
        print(f"  · {row.get('name')}({row.get('code')}) — {steps}")
        for f in fails[:3]:
            print(f"      [{f.get('step')}] {f.get('reason')}")

    print("\n단계별 탈락 횟수 (장투 게이트):")
    for step, cnt in sorted(
        (report.get("fail_by_step") or {}).items(),
        key=lambda x: -x[1],
    ):
        print(f"  · {step}: {cnt}")

    print("\nBrain FlowTracker 탈락 (유니버스 전 단계):")
    for step, cnt in sorted(
        (report.get("brain_leader_reject_steps") or {}).items(),
        key=lambda x: -x[1],
    ):
        print(f"  · {step}: {cnt}")


def _print_top_knot(report: dict) -> None:
    print("\n" + "=" * 60)
    print("Top rejection (excluding turnover / trade amount)")
    print("=" * 60)
    step = report.get("top_fail_step_ex_turnover")
    cnt = report.get("top_fail_count_ex_turnover", 0)
    if not step:
        print("거래대금/회전율 외 단일 최다 탈락 없음")
        return
    labels = {
        "mode_auto": "자동모드 장투 미분류 (스윙/단타로 분류됨)",
        "daily_ma": "일봉 MA 정배열·종가≥MA20",
        "market_cap": "시총 2조 미만",
        "volatility": "당일 변동성 과다",
        "leader_flow": "주도가점·수급점수 부족",
        "pullback_range": "눌림 등락률 구간 이탈",
    }
    print(f"  **{step}** — {cnt}건")
    print(f"  -> {labels.get(step, '매듭 후보')}")


def _print_simulation(report: dict) -> None:
    print("\n" + "=" * 60)
    print("20% relaxed filter simulation")
    print("=" * 60)
    if report.get("passed_count", 0) > 0:
        print(f"현재 통과 {report['passed_count']}건 — 시뮬레이션 생략")
        return
    tr = report.get("thresholds_relaxed") or {}
    print("완화 기준:")
    print(f"  시총≥{int(tr.get('long_term_min_cap', 0)):,}")
    print(f"  변동성<{tr.get('vol_swing_max', 0):.2f}%")
    print(f"  leader≥{tr.get('leader_min', 0):.1f} or flow≥{tr.get('flow_min', 0):.1f}")
    print(f"  눌림 {tr.get('pullback_lo', 0):.1f}% ~ {tr.get('pullback_hi', 0):.1f}%")
    print(f"  MA20 하한 완화 -{tr.get('ma20_floor_pct', 0):.1f}%")
    n = int(report.get("relaxed_simulation_pass") or 0)
    print(f"\n시뮬레이션 장투 통과: **{n}종목** (기존 0건 대비)")


def _print_recent_buys() -> None:
    print("\n" + "=" * 60)
    print("Recent long-term buys (trade_history.db)")
    print("=" * 60)
    try:
        import trade_history_db as thdb

        thdb.init_trade_history_db()
        buys = []
        for d in range(5):
            day = (date.today() - timedelta(days=d)).isoformat()
            for row in thdb.list_trades_for_date(day, side="buy"):
                buys.append(row)
        if not buys:
            print("  최근 5일 매수 체결 0건")
            return
        for row in buys[:10]:
            print(
                f"  · {row.get('trade_date')} {row.get('stock_name')}({row.get('stock_code')}) "
                f"buy @{row.get('price')}"
            )
    except Exception as exc:
        print(f"  DB 조회 실패: {exc}")


def main() -> int:
    if not config.APP_KEY or not config.APP_SECRET:
        print("APP_KEY / APP_SECRET 없음")
        return 1

    token = get_access_token()
    print(f"토큰 OK · POLLING={getattr(config, 'POLLING_STRATEGY_MODE', False)}")

    report = diagnose_longterm_entries(token, config.APP_KEY, config.APP_SECRET)

    _print_thresholds(report)
    _print_rejections(report)
    _print_top_knot(report)
    _print_simulation(report)
    _print_recent_buys()

    print("\n" + "=" * 60)
    print("결론")
    print("=" * 60)
    n = report.get("passed_count", 0)
    if n > 0:
        print(f"장투 필터 통과 {n}건 — 미매수 시 슬롯매칭·엔진·AI가드 확인")
    else:
        top = report.get("top_fail_step_ex_turnover")
        print(f"장투 후보 0건 — 주요 매듭(거래대금 제외): **{top}**")
        print(f"20% 완화 시뮬: {report.get('relaxed_simulation_pass', 0)}건")
        print(report.get("structural_note"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
