"""
brain.py — AI급 시장 판단 뇌 (3대 축)

[OLD] 가격·코스피200·5일 거래대금만으로 느리게 거르는 구시대 필터 → 폐기
[NEW]
  1) FlowTracker      — 당일 거래대금 TOP-N × 외인/기관 순매수 강도 → 주도주
  2) NewsMomentum     — 키워드·테마·디데이 타임라인 가중치
  3) ModeAutoClassifier — 시총·변동성 기반 단타/스윙/장투 자동 배지

scheduler / market_scan / stock_swing 은 이 모듈만 호출하면 된다.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

from brain_classifier import (
    MODE_BADGE_CSS,
    MODE_LABEL_KO,
    MODE_POLICIES,
    TradingMode,
    get_brain_classifier,
)
from stock_names import normalize_code
from stock_ranking import get_top_trading_amount_stocks, is_common_stock_for_trade
from kis_rate import kis_loop_pause
from stock_universe import fetch_avg_trade_value, fetch_quote_with_risk, is_excluded_risk_stock

logger = logging.getLogger(__name__)

_brain_scan_cache_at: float = 0.0
_brain_scan_cache: list[dict[str, Any]] = []


def _cfg(name: str, default: Any) -> Any:
    try:
        import config as cfg

        return getattr(cfg, name, default)
    except ImportError:
        return default


# ── 레거시 주도주 힌트 (반도체·빅테크) — 이름/코드 매칭 가산 ──
_LEADER_SECTOR_CODES: frozenset[str] = frozenset(
    normalize_code(c)
    for c in _cfg(
        "BRAIN_LEADER_SECTOR_CODES",
        ("000660", "005930", "035420", "035720", "373220", "207940"),
    )
)

_LEADER_NAME_KEYWORDS: tuple[str, ...] = tuple(
    _cfg(
        "BRAIN_LEADER_NAME_KEYWORDS",
        (
            "하이닉스",
            "반도체",
            "네이버",
            "카카오",
            "플랫폼",
            "AI",
            "엔비디아",
            "HBM",
            "파운드리",
        ),
    )
)

_NON_LEADER_PENALTY_NAMES: tuple[str, ...] = tuple(
    _cfg(
        "BRAIN_NON_LEADER_PENALTY_NAMES",
        ("전자", "SDI", "화학", "건설", "철강"),
    )
)


class ThemePhase(str, Enum):
    WATCH = "watch"
    ACCUMULATE = "accumulate"
    PRE_EXIT = "pre_exit"
    EVENT_DAY = "event_day"
    POST_EVENT = "post_event"


@dataclass
class ThemeEventPlan:
    event_id: str
    title: str
    event_date: date
    phase: ThemePhase
    days_until: int
    codes: list[str] = field(default_factory=list)
    accumulate: bool = False
    prepare_exit: bool = False
    note: str = ""


@dataclass
class BrainScore:
    code: str
    flow_rank: int
    flow_score: float
    momentum_score: float
    theme_score: float
    leader_boost: float
    total_score: float
    themes: list[str] = field(default_factory=list)
    theme_phase: str | None = None
    rationale: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════
# [1] 실시간 수급·거래대금 트래커
# ═══════════════════════════════════════════════════════════════════


def _institutional_flow_strength(detail: dict[str, Any]) -> float:
    """
    외인·기관(프로그램) 당일 순매수 강도 0~100.
    inquire-price 의 frgn_ntby_qty, pgtr_ntby_qty 활용.
    """
    frgn = int(detail.get("frgn_ntby_qty") or 0)
    inst = int(detail.get("pgtr_ntby_qty") or 0)
    price = max(int(detail.get("price") or 1), 1)
    # 수량 → 대략적 금액 강도 (절대값 스케일)
    frgn_won = abs(frgn) * price
    inst_won = abs(inst) * price
    score = 0.0
    if frgn > 0:
        score += min(40.0, math.log10(frgn_won + 1) * 8)
    elif frgn < 0:
        score -= min(25.0, math.log10(frgn_won + 1) * 5)
    if inst > 0:
        score += min(40.0, math.log10(inst_won + 1) * 8)
    elif inst < 0:
        score -= min(20.0, math.log10(inst_won + 1) * 4)
    if frgn > 0 and inst > 0:
        score += 15.0
    return max(0.0, min(100.0, score))


def _leader_sector_boost(stock: dict[str, Any]) -> tuple[float, list[str]]:
    """반도체·빅테크 주도주 가산 / 비주도 대형주 감점."""
    code = normalize_code(stock.get("code"))
    name = str(stock.get("name") or "")
    notes: list[str] = []
    boost = 0.0
    if code in _LEADER_SECTOR_CODES and code not in ("006400", "066570"):
        boost += 22.0
        notes.append("주도 섹터 대장주")
    for kw in _LEADER_NAME_KEYWORDS:
        if kw in name:
            boost += 12.0
            notes.append(f"주도 키워드:{kw}")
            break
    for bad in _NON_LEADER_PENALTY_NAMES:
        if bad in name and boost < 15:
            boost -= 18.0
            notes.append(f"비주도주 감점({bad})")
            break
    return boost, notes


def _passes_financial_safety(stock: dict[str, Any]) -> bool:
    """무인 운용용 재무 안전망. 값이 있으면 강제 필터링."""
    if _cfg("BRAIN_REQUIRE_RISK_EXCLUDED", True):
        if is_excluded_risk_stock(stock):
            return False
    debt = stock.get("debt_ratio")
    reserve = stock.get("reserve_ratio")
    impaired = stock.get("capital_impairment")
    if debt is not None:
        try:
            if float(debt) > float(_cfg("BRAIN_FIN_DEBT_RATIO_MAX", 150.0)):
                return False
        except (TypeError, ValueError):
            pass
    if reserve is not None:
        try:
            if float(reserve) < float(_cfg("BRAIN_FIN_RESERVE_RATIO_MIN", 500.0)):
                return False
        except (TypeError, ValueError):
            pass
    if _cfg("BRAIN_REQUIRE_NO_IMPAIRMENT", True) and impaired is not None:
        try:
            if float(impaired) > 0:
                return False
        except (TypeError, ValueError):
            if str(impaired).strip().upper() in {"Y", "TRUE", "1"}:
                return False
    return True


def _turnover_spike_min_mult(stock: dict[str, Any]) -> float:
    """거래대금 flow_rank 기준 회전율 급증 임계값 (TOP3=1.5x, 그 외=5.0x)."""
    flow_rank = int(stock.get("flow_rank") or 999)
    top_rank = int(_cfg("BRAIN_TURNOVER_SPIKE_TOP_RANK", 3))
    if flow_rank <= top_rank:
        return float(_cfg("BRAIN_TURNOVER_SPIKE_TOP_MIN_MULT", 1.5))
    return float(_cfg("BRAIN_TURNOVER_SPIKE_MIN_MULT", 5.0))


def _is_turnover_spike_top_rank(stock: dict[str, Any]) -> bool:
    flow_rank = int(stock.get("flow_rank") or 999)
    top_rank = int(_cfg("BRAIN_TURNOVER_SPIKE_TOP_RANK", 3))
    return flow_rank <= top_rank


def _evaluate_turnover_spike(stock: dict[str, Any]) -> tuple[bool, str | None]:
    """
    회전율 급증 필터 — 거래대금 TOP3(flow_rank)는 1.5x, 4위~는 5.0x.
    TOP3 + 시총 미수신(cap=0)이면 거래대금만으로 통과( enrich quote 폴백 ).
    """
    min_mult = _turnover_spike_min_mult(stock)
    relaxed = _is_turnover_spike_top_rank(stock)
    stock["turnover_spike_min_mult"] = min_mult
    stock["turnover_spike_relaxed"] = relaxed

    trade_amt = int(stock.get("trade_amount") or 0)
    cap = int(stock.get("market_cap") or 0)
    if trade_amt <= 0 or cap <= 0:
        if relaxed and trade_amt > 0:
            stock["turnover_spike_bypass"] = True
            return True, None
        return False, f"거래대금/시총 부족 (amt={trade_amt:,}, cap={cap:,})"

    now_turnover = trade_amt / cap
    avg_trade = float(stock.get("avg_trade_value_20d") or 0.0)
    if avg_trade <= 0:
        avg_trade = float(stock.get("avg_trade_value_5d") or 0.0)
    if avg_trade <= 0:
        if relaxed:
            stock["turnover_spike_bypass"] = True
            return True, None
        return False, "20일/5일 평균 거래대금 없음"
    avg_turnover = avg_trade / cap
    if avg_turnover <= 0:
        if relaxed:
            stock["turnover_spike_bypass"] = True
            return True, None
        return False, "평균 회전율 계산 불가"

    mult = now_turnover / avg_turnover
    stock["turnover_ratio_now"] = round(now_turnover, 6)
    stock["turnover_ratio_avg"] = round(avg_turnover, 6)
    stock["turnover_spike_mult"] = round(mult, 2)

    if mult >= min_mult:
        return True, None
    if relaxed:
        stock["turnover_spike_bypass"] = True
        return True, None
    tier = f"{min_mult:.1f}x"
    return False, f"회전율 급증 {mult:.2f}x < {tier}"


def _passes_turnover_spike(stock: dict[str, Any]) -> bool:
    """거래대금/시총 회전율이 20일 평균 대비 급증한 종목만 통과."""
    passed, _ = _evaluate_turnover_spike(stock)
    return passed


def _passes_pullback_entry_window(stock: dict[str, Any]) -> bool:
    """
    눌림목 등락률 필터 — PULLBACK_MIN_PCT(-1%) ~ PULLBACK_MAX_PCT(-10%) 구간.
    MA120/240 있으면 장기선 이격(-1.5~+5%) 추가 검사.
    """
    price = int(stock.get("price") or 0)
    if price <= 0:
        return False
    ma120 = stock.get("ma120")
    ma240 = stock.get("ma240")
    change = float(stock.get("change_rate") or 0.0)
    lo = float(_cfg("BRAIN_PULLBACK_CHANGE_MIN", -10.0))
    hi = float(_cfg("BRAIN_PULLBACK_CHANGE_MAX", -1.0))
    if ma120 is not None or ma240 is not None:
        try:
            m120 = float(ma120) if ma120 is not None else 0.0
            m240 = float(ma240) if ma240 is not None else 0.0
            base = max(m120, m240, 1.0)
            # 장기선 위에서 너무 멀리 이격된 추격구간 배제(눌림목만 허용)
            dist = (price - base) / base * 100.0
            return -1.5 <= dist <= 5.0 and lo <= change <= hi
        except (TypeError, ValueError):
            pass
    return lo <= change <= hi


def _merge_rank_row_into_detail(detail: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """거래대금 TOP row 시세·거래대금을 enrich quote에 보존 (회전율 필터용)."""
    merged = dict(detail)
    for key in ("trade_amount", "change_rate", "name", "raw_code", "flow_rank"):
        if merged.get(key) in (None, "", 0) and row.get(key) not in (None, "", 0):
            merged[key] = row[key]
    if not merged.get("raw_code") and merged.get("code"):
        merged["raw_code"] = f"A{merged['code']}"
    return merged


def scan_market_leaders(
    access_token: str,
    app_key: str,
    app_secret: str,
    *,
    top_n: int | None = None,
) -> list[dict[str, Any]]:
    """
    당일 거래대금 상위 N → 수급 강도 순 재정렬 → 주도주 리스트.

    OLD: build_active_universe 가 300종목 5일 평균 검증
    NEW: TOP-N 즉시 스캔 + 외인/기관 순매수 (SK하이닉스·네이버류 우선)
    """
    global _brain_scan_cache_at, _brain_scan_cache

    cache_sec = float(_cfg("BRAIN_FLOW_CACHE_SEC", 60))
    now = time.time()
    if _brain_scan_cache and (now - _brain_scan_cache_at) < cache_sec:
        return [dict(x) for x in _brain_scan_cache]

    n = int(top_n or _cfg("BRAIN_FLOW_TOP_N", 20))
    enrich_n = int(_cfg("BRAIN_FLOW_QUOTE_ENRICH_TOP_N", 5))
    ranked = get_top_trading_amount_stocks(
        access_token, app_key, app_secret, limit=max(n, 25)
    )
    if not ranked:
        return []

    preliminary: list[dict[str, Any]] = []
    for rank_idx, row in enumerate(ranked[:n], start=1):
        code = str(row.get("code") or "")
        if not code:
            continue
        row_base = dict(row)
        row_base.setdefault("raw_code", f"A{code}")
        trade_amt = int(row.get("trade_amount") or 0)
        liquidity = min(50.0, math.log10(max(trade_amt, 1)) * 5) if trade_amt else 0
        row_base["flow_rank"] = rank_idx
        row_base["liquidity_score"] = round(liquidity, 2)
        preliminary.append(row_base)

    preliminary.sort(
        key=lambda s: float(s.get("liquidity_score") or 0),
        reverse=True,
    )

    enriched: list[dict[str, Any]] = []
    for brain_rank, row in enumerate(preliminary, start=1):
        time.sleep(0.25)
        code = str(row.get("code") or "")
        if brain_rank <= enrich_n:
            if brain_rank > 1:
                kis_loop_pause()
            detail = fetch_quote_with_risk(access_token, app_key, app_secret, code)
            if not detail:
                detail = dict(row)
            avg20 = fetch_avg_trade_value(
                access_token,
                app_key,
                app_secret,
                code,
                days=max(5, int(_cfg("BRAIN_TURNOVER_AVG_DAYS", 20))),
            )
            if avg20 is not None:
                detail["avg_trade_value_20d"] = float(avg20)
        else:
            detail = dict(row)
            detail["flow_score"] = round(
                min(40.0, abs(float(row.get("change_rate") or 0)) * 2.5), 2
            )
        detail = _merge_rank_row_into_detail(detail, row)
        if not is_common_stock_for_trade(detail):
            continue
        flow = _institutional_flow_strength(detail)
        trade_amt = int(detail.get("trade_amount") or row.get("trade_amount") or 0)
        liquidity = min(50.0, math.log10(max(trade_amt, 1)) * 5) if trade_amt else 0
        leader_boost, leader_notes = _leader_sector_boost(detail)
        detail["flow_rank"] = int(row.get("flow_rank") or brain_rank)
        detail["flow_score"] = round(flow, 2)
        detail["liquidity_score"] = round(liquidity, 2)
        detail["leader_boost"] = round(leader_boost, 2)
        detail["brain_flow_notes"] = leader_notes
        if not _passes_financial_safety(detail):
            continue
        if not _passes_turnover_spike(detail):
            continue
        if not _passes_pullback_entry_window(detail):
            continue
        enriched.append(detail)

    enriched.sort(
        key=lambda s: (
            float(s.get("leader_boost") or 0)
            + float(s.get("flow_score") or 0)
            + float(s.get("liquidity_score") or 0) * 0.6
        ),
        reverse=True,
    )
    for i, s in enumerate(enriched, start=1):
        s["brain_rank"] = i
    logger.info(
        "Brain FlowTracker — 거래대금 TOP %d → 주도주 %d종목 (시세 enrich %d) · 1위 %s",
        n,
        len(enriched),
        enrich_n,
        enriched[0].get("name") if enriched else "-",
    )
    _brain_scan_cache = [dict(x) for x in enriched]
    _brain_scan_cache_at = now
    return [dict(x) for x in enriched]


def _financial_safety_fail_reason(stock: dict[str, Any]) -> str | None:
    """재무 안전망 미통과 사유 (통과 시 None)."""
    if _cfg("BRAIN_REQUIRE_RISK_EXCLUDED", True):
        if is_excluded_risk_stock(stock):
            return "리스크 제외 종목(관리·투자주의 등)"
    debt = stock.get("debt_ratio")
    reserve = stock.get("reserve_ratio")
    impaired = stock.get("capital_impairment")
    debt_max = float(_cfg("BRAIN_FIN_DEBT_RATIO_MAX", 150.0))
    reserve_min = float(_cfg("BRAIN_FIN_RESERVE_RATIO_MIN", 500.0))
    if debt is not None:
        try:
            if float(debt) > debt_max:
                return f"부채비율 {float(debt):.1f}% > {debt_max:.0f}%"
        except (TypeError, ValueError):
            pass
    if reserve is not None:
        try:
            if float(reserve) < reserve_min:
                return f"유보율 {float(reserve):.1f}% < {reserve_min:.0f}%"
        except (TypeError, ValueError):
            pass
    if _cfg("BRAIN_REQUIRE_NO_IMPAIRMENT", True) and impaired is not None:
        try:
            if float(impaired) > 0:
                return f"자본잠식 {impaired}"
        except (TypeError, ValueError):
            if str(impaired).strip().upper() in {"Y", "TRUE", "1"}:
                return "자본잠식(Y)"
    return None


def _turnover_spike_fail_reason(stock: dict[str, Any]) -> str | None:
    passed, reason = _evaluate_turnover_spike(stock)
    return None if passed else reason


def _pullback_entry_fail_reason(stock: dict[str, Any]) -> str | None:
    price = int(stock.get("price") or 0)
    if price <= 0:
        return "시세 없음"
    ma120 = stock.get("ma120")
    ma240 = stock.get("ma240")
    change = float(stock.get("change_rate") or 0.0)
    lo = float(_cfg("BRAIN_PULLBACK_CHANGE_MIN", -10.0))
    hi = float(_cfg("BRAIN_PULLBACK_CHANGE_MAX", -1.0))
    if ma120 is not None or ma240 is not None:
        try:
            m120 = float(ma120) if ma120 is not None else 0.0
            m240 = float(ma240) if ma240 is not None else 0.0
            base = max(m120, m240, 1.0)
            dist = (price - base) / base * 100.0
            if not (-1.5 <= dist <= 5.0):
                return f"장기선 이격 {dist:.2f}% (허용 -1.5~5.0%)"
            if not (lo <= change <= hi):
                return f"등락률 {change:.2f}% (허용 {lo}~{hi}%)"
            return None
        except (TypeError, ValueError):
            pass
    if lo <= change <= hi:
        return None
    return f"등락률 {change:.2f}% (허용 {lo}~{hi}%)"


def diagnose_scan_market_leaders(
    access_token: str,
    app_key: str,
    app_secret: str,
    *,
    top_n: int | None = None,
    bypass_cache: bool = True,
) -> dict[str, Any]:
    """
    scan_market_leaders() 1회 수동 진단 — 종목별 필터 통과/탈락 사유.
    bypass_cache=True: 캐시 무시하고 최신 시세 기준.
    """
    n = int(top_n or _cfg("BRAIN_FLOW_TOP_N", 20))
    enrich_n = int(_cfg("BRAIN_FLOW_QUOTE_ENRICH_TOP_N", 5))
    ranked = get_top_trading_amount_stocks(
        access_token, app_key, app_secret, limit=max(n, 25)
    )
    report: dict[str, Any] = {
        "top_n": n,
        "enrich_n": enrich_n,
        "ranked_count": len(ranked),
        "passed": [],
        "rejected": [],
        "leaders": [],
        "turnover_policy": {
            "top_rank": int(_cfg("BRAIN_TURNOVER_SPIKE_TOP_RANK", 3)),
            "top_min_mult": float(_cfg("BRAIN_TURNOVER_SPIKE_TOP_MIN_MULT", 1.5)),
            "default_min_mult": float(_cfg("BRAIN_TURNOVER_SPIKE_MIN_MULT", 5.0)),
        },
    }
    if not ranked:
        report["summary"] = "거래대금 TOP 목록 조회 실패"
        return report

    preliminary: list[dict[str, Any]] = []
    for rank_idx, row in enumerate(ranked[:n], start=1):
        code = str(row.get("code") or "")
        if not code:
            continue
        row_base = dict(row)
        row_base.setdefault("raw_code", f"A{code}")
        trade_amt = int(row.get("trade_amount") or 0)
        liquidity = min(50.0, math.log10(max(trade_amt, 1)) * 5) if trade_amt else 0
        row_base["flow_rank"] = rank_idx
        row_base["liquidity_score"] = round(liquidity, 2)
        preliminary.append(row_base)

    preliminary.sort(
        key=lambda s: float(s.get("liquidity_score") or 0),
        reverse=True,
    )

    enriched: list[dict[str, Any]] = []
    for brain_rank, row in enumerate(preliminary, start=1):
        time.sleep(0.25)
        code = str(row.get("code") or "")
        name = str(row.get("name") or code)
        entry: dict[str, Any] = {
            "code": code,
            "name": name,
            "flow_rank": int(row.get("flow_rank") or brain_rank),
            "enriched": brain_rank <= enrich_n,
            "passed": False,
            "fail_step": None,
            "fail_reason": None,
        }

        if brain_rank <= enrich_n:
            if brain_rank > 1:
                kis_loop_pause()
            detail = fetch_quote_with_risk(access_token, app_key, app_secret, code)
            if not detail:
                detail = dict(row)
            avg20 = fetch_avg_trade_value(
                access_token,
                app_key,
                app_secret,
                code,
                days=max(5, int(_cfg("BRAIN_TURNOVER_AVG_DAYS", 20))),
            )
            if avg20 is not None:
                detail["avg_trade_value_20d"] = float(avg20)
        else:
            detail = dict(row)
            detail["flow_score"] = round(
                min(40.0, abs(float(row.get("change_rate") or 0)) * 2.5), 2
            )

        detail = _merge_rank_row_into_detail(detail, row)
        if not is_common_stock_for_trade(detail):
            entry["fail_step"] = "common_stock"
            entry["fail_reason"] = "일반주(A접두) 아님 또는 ETF/ETN"
            report["rejected"].append(entry)
            continue

        flow = _institutional_flow_strength(detail)
        trade_amt = int(detail.get("trade_amount") or row.get("trade_amount") or 0)
        liquidity = min(50.0, math.log10(max(trade_amt, 1)) * 5) if trade_amt else 0
        leader_boost, leader_notes = _leader_sector_boost(detail)
        detail["flow_rank"] = int(row.get("flow_rank") or brain_rank)
        detail["flow_score"] = round(flow, 2)
        detail["liquidity_score"] = round(liquidity, 2)
        detail["leader_boost"] = round(leader_boost, 2)
        detail["brain_flow_notes"] = leader_notes
        entry["change_rate"] = float(detail.get("change_rate") or 0.0)
        entry["flow_score"] = detail["flow_score"]
        entry["flow_rank"] = int(detail.get("flow_rank") or 0)

        fin_reason = _financial_safety_fail_reason(detail)
        if fin_reason:
            entry["fail_step"] = "financial_safety"
            entry["fail_reason"] = fin_reason
            report["rejected"].append(entry)
            continue

        turn_reason = _turnover_spike_fail_reason(detail)
        if turn_reason:
            entry["fail_step"] = "turnover_spike"
            entry["fail_reason"] = turn_reason
            entry["turnover_spike_min_mult"] = detail.get("turnover_spike_min_mult")
            entry["turnover_spike_relaxed"] = detail.get("turnover_spike_relaxed")
            if detail.get("turnover_spike_mult") is not None:
                entry["turnover_spike_mult"] = detail["turnover_spike_mult"]
            report["rejected"].append(entry)
            continue

        pb_reason = _pullback_entry_fail_reason(detail)
        if pb_reason:
            entry["fail_step"] = "pullback_entry"
            entry["fail_reason"] = pb_reason
            report["rejected"].append(entry)
            continue

        entry["passed"] = True
        entry["leader_boost"] = leader_boost
        entry["turnover_spike_min_mult"] = detail.get("turnover_spike_min_mult")
        entry["turnover_spike_relaxed"] = detail.get("turnover_spike_relaxed")
        entry["turnover_spike_bypass"] = detail.get("turnover_spike_bypass")
        if detail.get("turnover_spike_mult") is not None:
            entry["turnover_spike_mult"] = detail["turnover_spike_mult"]
        report["passed"].append(entry)
        enriched.append(detail)

    enriched.sort(
        key=lambda s: (
            float(s.get("leader_boost") or 0)
            + float(s.get("flow_score") or 0)
            + float(s.get("liquidity_score") or 0) * 0.6
        ),
        reverse=True,
    )
    for i, s in enumerate(enriched, start=1):
        s["brain_rank"] = i
    report["leaders"] = enriched
    report["passed_count"] = len(enriched)
    report["rejected_count"] = len(report["rejected"])
    report["summary"] = (
        f"거래대금 TOP {n} → 주도주 {len(enriched)}종목 통과 "
        f"(탈락 {len(report['rejected'])}) · "
        f"회전율 TOP{report['turnover_policy']['top_rank']}="
        f"{report['turnover_policy']['top_min_mult']:.1f}x/skip · "
        f"그외={report['turnover_policy']['default_min_mult']:.1f}x"
    )
    return report


# ═══════════════════════════════════════════════════════════════════
# [2] 뉴스·키워드 모멘텀 + 테마 디데이 엔진
# ═══════════════════════════════════════════════════════════════════


def _keyword_groups() -> dict[str, tuple[str, ...]]:
    return dict(
        _cfg(
            "BRAIN_NEWS_KEYWORD_GROUPS",
            {
                "스페이스X·우주항공": (
                    "스페이스X",
                    "SpaceX",
                    "스페이스",
                    "우주",
                    "항공",
                    "위성",
                    "로켓",
                    "갤럭시",
                    "AP위성",
                    "쎄트렉",
                ),
                "반도체 랠리": (
                    "반도체",
                    "HBM",
                    "AI반도체",
                    "파운드리",
                    "하이닉스",
                    "메모리",
                    "랠리",
                ),
                "빅테크·플랫폼": (
                    "네이버",
                    "카카오",
                    "플랫폼",
                    "빅테크",
                    "클라우드",
                ),
            },
        )
    )


def _manual_news_hints() -> list[str]:
    return list(_cfg("AI_MANUAL_NEWS_HINTS", [])) + list(
        _cfg("BRAIN_EXTRA_NEWS_HINTS", [])
    )


def score_news_momentum(stock: dict[str, Any]) -> tuple[float, list[str]]:
    """종목명·코드·힌트 텍스트 기반 테마 모멘텀 0~100."""
    name = str(stock.get("name") or "")
    code = normalize_code(stock.get("code"))
    blob = " ".join(
        [name, code]
        + _manual_news_hints()
        + [str(stock.get("setup") or "")]
    ).upper()

    score = 0.0
    themes: list[str] = []
    for theme, keywords in _keyword_groups().items():
        hits = sum(1 for kw in keywords if kw.upper() in blob or kw in name)
        if hits:
            add = min(35.0, 12.0 * hits)
            score += add
            themes.append(theme)

    # 설정된 테마 워치리스트 코드 직접 가산
    for event in _theme_event_configs():
        for item in event.get("watchlist") or []:
            if str(item.get("code")) == code:
                score += float(item.get("momentum_bonus", 28))
                themes.append(event.get("title", "테마"))
    return min(100.0, score), themes


def _theme_event_configs() -> list[dict[str, Any]]:
    return list(
        _cfg(
            "BRAIN_THEME_EVENTS",
            [
                {
                    "event_id": "spacex_ipo_2026",
                    "title": "스페이스X 상장",
                    "event_date": "2026-06-11",
                    "keywords": ["스페이스X", "SpaceX", "우주항공"],
                    "watchlist": [
                        {
                            "code": "041190",
                            "name": "미래에셋벤처투자",
                            "momentum_bonus": 35,
                        },
                        {
                            "code": "211270",
                            "name": "AP위성",
                            "momentum_bonus": 32,
                        },
                    ],
                    "accumulate_start_days_before": 14,
                    "accumulate_end_days_before": 1,
                    "exit_on_event_day": True,
                    "staged_buy_slices": 3,
                },
            ],
        )
    )


def resolve_theme_phase(code: str, now: date | None = None) -> ThemeEventPlan | None:
    """디데이 타임라인 — 분할 매수·당일 청산 준비 구간."""
    today = now or date.today()
    norm = normalize_code(code)
    for event in _theme_event_configs():
        event_date = date.fromisoformat(str(event["event_date"]))
        days_until = (event_date - today).days
        codes = [
            normalize_code(x.get("code"))
            for x in (event.get("watchlist") or [])
        ]
        if norm not in codes:
            continue

        start_acc = int(event.get("accumulate_start_days_before", 14))
        end_acc = int(event.get("accumulate_end_days_before", 1))
        phase = ThemePhase.WATCH
        accumulate = False
        prepare_exit = False
        note = ""

        if days_until < 0:
            phase = ThemePhase.POST_EVENT
            note = "이벤트 경과 · 모멘텀 축소"
        elif days_until == 0:
            phase = ThemePhase.EVENT_DAY
            prepare_exit = bool(event.get("exit_on_event_day", True))
            note = "디데이 — 청산 준비·트레일링 강화"
        elif days_until == 1:
            phase = ThemePhase.PRE_EXIT
            prepare_exit = True
            note = "상장 전날 — 이익 실현·비중 축소 우선"
        elif start_acc >= days_until >= end_acc:
            phase = ThemePhase.ACCUMULATE
            accumulate = True
            note = f"D-{days_until} 분할 매수 구간"
        else:
            phase = ThemePhase.WATCH
            note = f"D-{days_until} 테마 감시"

        return ThemeEventPlan(
            event_id=str(event.get("event_id")),
            title=str(event.get("title")),
            event_date=event_date,
            phase=phase,
            days_until=days_until,
            codes=codes,
            accumulate=accumulate,
            prepare_exit=prepare_exit,
            note=note,
        )
    return None


def theme_timeline_hints(now: date | None = None) -> list[dict[str, Any]]:
    """스케줄러/UI용 활성 테마 이벤트 요약."""
    today = now or date.today()
    out: list[dict[str, Any]] = []
    for event in _theme_event_configs():
        event_date = date.fromisoformat(str(event["event_date"]))
        days_until = (event_date - today).days
        out.append(
            {
                "event_id": event.get("event_id"),
                "title": event.get("title"),
                "event_date": event_date.isoformat(),
                "days_until": days_until,
                "watchlist": event.get("watchlist"),
            }
        )
    return out


# ═══════════════════════════════════════════════════════════════════
# [3] 종목별 매매 모드 자동 매칭
# ═══════════════════════════════════════════════════════════════════


def _estimate_intraday_volatility(stock: dict[str, Any]) -> float:
    """당일 등락·거래량 급등 근사 변동성 (%)."""
    chg = abs(float(stock.get("change_rate") or 0))
    vol_pct = float(stock.get("prdy_vrss_vol_rate") or 0)
    return chg + min(15.0, vol_pct / 50.0)


def classify_trading_mode_auto(stock: dict[str, Any]) -> dict[str, Any]:
    """
    시총·변동성·테마 모멘텀 → trading_mode 자동 결정.

    - 대형 안정 주도주 → 장투/스윙
    - 소형·고변동 테마주 → 스윙/단타
    """
    cap = int(stock.get("market_cap") or 0)
    vol = _estimate_intraday_volatility(stock)
    momentum, themes = score_news_momentum(stock)
    flow = float(stock.get("flow_score") or 0)
    leader = float(stock.get("leader_boost") or 0)
    theme_plan = resolve_theme_phase(str(stock.get("code") or ""))

    large_cap = int(_cfg("BRAIN_LARGE_CAP_WON", 2_000_000_000_000))
    mid_cap = int(_cfg("BRAIN_MID_CAP_WON", 500_000_000_000))
    vol_scalp = float(_cfg("BRAIN_VOLATILITY_SCALP_MIN", 6.0))
    vol_swing = float(_cfg("BRAIN_VOLATILITY_SWING_MAX", 5.5))

    mode = TradingMode.SWING
    rationale: list[str] = []

    if theme_plan and theme_plan.phase in (
        ThemePhase.ACCUMULATE,
        ThemePhase.PRE_EXIT,
        ThemePhase.EVENT_DAY,
    ):
        mode = TradingMode.DAY_TRADING if vol >= vol_scalp else TradingMode.SWING
        rationale.append(f"테마 {theme_plan.title} ({theme_plan.note})")
    elif cap >= large_cap and vol < vol_swing and (leader >= 15 or flow >= 40):
        mode = TradingMode.LONG_TERM
        rationale.append("대형 주도주 · 안정 수급 → 장투")
    elif cap >= large_cap and vol < vol_scalp + 2:
        mode = TradingMode.SWING
        rationale.append("대형주 · 완만 변동 → 스윙")
    elif cap < mid_cap or vol >= vol_scalp or momentum >= 45:
        mode = TradingMode.DAY_TRADING if vol >= vol_scalp else TradingMode.SWING
        rationale.append("소형/고변동/테마 → 단타·스윙")
    elif momentum >= 30:
        mode = TradingMode.SWING
        rationale.append("테마 모멘텀 → 스윙")

    policy = MODE_POLICIES[mode]
    tags = {
        "trading_mode": mode.value,
        "mode_label": MODE_LABEL_KO[mode],
        "mode_badge_class": MODE_BADGE_CSS[mode],
        "mode_confidence": round(
            min(99.0, 50 + leader * 0.5 + flow * 0.3 + momentum * 0.2), 1
        ),
        "mode_hold_hint": (
            "당일청산"
            if policy.exit_same_day
            else f"{policy.hold_days_min}~{policy.hold_days_max}일"
        ),
        "mode_chart": policy.chart_timeframe,
        "mode_policy": policy.description,
        "mode_rationale": " · ".join(rationale[:3]) or policy.description,
        "brain_auto_mode": True,
        "brain_themes": themes,
    }
    if theme_plan:
        tags["theme_phase"] = theme_plan.phase.value
        tags["theme_event_id"] = theme_plan.event_id
        tags["theme_days_until"] = theme_plan.days_until
        tags["theme_accumulate"] = theme_plan.accumulate
        tags["theme_prepare_exit"] = theme_plan.prepare_exit
    return tags


# ═══════════════════════════════════════════════════════════════════
# 통합 스코어 · 유니버스 · 테마 액션
# ═══════════════════════════════════════════════════════════════════


def score_stock(stock: dict[str, Any]) -> BrainScore:
    code = normalize_code(stock.get("code"))
    flow = float(stock.get("flow_score") or 0)
    leader_boost, leader_notes = _leader_sector_boost(stock)
    momentum, themes = score_news_momentum(stock)
    theme_plan = resolve_theme_phase(code)
    theme_bonus = 0.0
    if theme_plan and theme_plan.phase == ThemePhase.ACCUMULATE:
        theme_bonus = 25.0
    elif theme_plan and theme_plan.phase == ThemePhase.PRE_EXIT:
        theme_bonus = 10.0

    liquidity = float(stock.get("liquidity_score") or 0)
    total = (
        flow * 0.35
        + momentum * 0.30
        + liquidity * 0.15
        + leader_boost * 0.20
        + theme_bonus
    )
    rationale = list(leader_notes)
    if themes:
        rationale.append("테마:" + ",".join(themes[:2]))
    if theme_plan:
        rationale.append(theme_plan.note)

    return BrainScore(
        code=code,
        flow_rank=int(stock.get("flow_rank") or stock.get("brain_rank") or 99),
        flow_score=flow,
        momentum_score=momentum,
        theme_score=theme_bonus,
        leader_boost=leader_boost,
        total_score=round(total, 2),
        themes=themes,
        theme_phase=theme_plan.phase.value if theme_plan else None,
        rationale=rationale,
    )


def enrich_stock_with_brain(stock: dict[str, Any], *, auto_mode: bool = True) -> dict[str, Any]:
    """스캔·UI·주문 직전 — 뇌 점수 + (선택) 자동 모드 태그."""
    scored = score_stock(stock)
    out = dict(stock)
    out["brain_score"] = scored.total_score
    out["brain_themes"] = scored.themes
    out["brain_rationale"] = " · ".join(scored.rationale)
    out["mega_trend"] = scored.momentum_score >= 40 or bool(scored.themes)

    if auto_mode and not out.get("selected_trading_mode"):
        out.update(classify_trading_mode_auto(out))
    elif auto_mode and out.get("selected_trading_mode"):
        # UI 수동 선택이 있으면 유지
        pass
    else:
        brain = get_brain_classifier()
        out = brain.tag_stock(out)
    return out


def build_brain_universe(
    access_token: str,
    app_key: str,
    app_secret: str,
    *,
    log_breakdown: bool = True,
) -> list[dict[str, Any]]:
    """
    신규 유니버스 빌더 — FlowTracker + 테마 워치리스트 병합.

    JSON 유니버스 파일을 읽지 않음. KIS API + config.BRAIN_THEME_EVENTS.
    실패 시 레거시 build_active_universe 폴백.
    """
    leaders = scan_market_leaders(access_token, app_key, app_secret)
    by_code: dict[str, dict[str, Any]] = {}

    for stock in leaders:
        enriched = enrich_stock_with_brain(stock, auto_mode=True)
        by_code[enriched["code"]] = enriched

    theme_added = 0
    theme_skipped: list[dict[str, str]] = []
    for event in _theme_event_configs():
        for item in event.get("watchlist") or []:
            time.sleep(0.25)
            code = normalize_code(item.get("code"))
            if len(code) != 6 or code in by_code:
                if len(code) == 6 and code in by_code:
                    theme_skipped.append(
                        {"code": code, "reason": "already_in_leaders"}
                    )
                continue
            if theme_added > 0:
                kis_loop_pause()
            detail = fetch_quote_with_risk(access_token, app_key, app_secret, code)
            if not detail:
                detail = {
                    "code": code,
                    "name": item.get("name", code),
                    "price": 0,
                    "raw_code": f"A{code}",
                }
            detail["theme_watchlist"] = True
            detail["flow_score"] = detail.get("flow_score") or 30.0
            if not _passes_financial_safety(detail):
                theme_skipped.append(
                    {
                        "code": code,
                        "name": str(item.get("name") or code),
                        "reason": _financial_safety_fail_reason(detail) or "financial",
                    }
                )
                continue
            if not _passes_pullback_entry_window(detail):
                theme_skipped.append(
                    {
                        "code": code,
                        "name": str(item.get("name") or code),
                        "reason": _pullback_entry_fail_reason(detail) or "pullback",
                    }
                )
                continue
            by_code[code] = enrich_stock_with_brain(detail, auto_mode=True)
            theme_added += 1

    universe = sorted(
        by_code.values(),
        key=lambda s: float(s.get("brain_score") or 0),
        reverse=True,
    )
    if log_breakdown:
        logger.info(
            "build_brain_universe — leaders=%d · theme_added=%d · theme_skip=%d · total=%d",
            len(leaders),
            theme_added,
            len(theme_skipped),
            len(universe),
        )
        for skip in theme_skipped[:6]:
            logger.info(
                "  theme_skip %s(%s): %s",
                skip.get("name", skip.get("code")),
                skip.get("code"),
                skip.get("reason"),
            )

    if universe:
        logger.info(
            "Brain universe %d종목 · 1위 %s (score=%.1f)",
            len(universe),
            universe[0].get("name"),
            float(universe[0].get("brain_score") or 0),
        )
        return universe

    logger.warning(
        "Brain universe 비어 있음 — 레거시 활성주 풀 폴백 (leaders=0 theme_added=0)"
    )
    from stock_universe import build_active_universe

    legacy = build_active_universe(access_token, app_key, app_secret)
    return [enrich_stock_with_brain(s, auto_mode=True) for s in legacy[:40]]


def explain_universe_load_paths() -> dict[str, Any]:
    """
    Universe 로드 경로 설명 — load_universe() / universe.json 은 없음.

    Brain·스케줄러가 실제로 참조하는 파일·API를 반환 (진단·로그용).
    """
    import selected_modes
    import trade_state
    from bot_config_reload import SLOTS_CONFIG_FILE, load_slots_config_file
    from pathlib import Path

    project = Path(__file__).resolve().parent
    theme_codes: list[str] = []
    for event in _theme_event_configs():
        for item in event.get("watchlist") or []:
            code = normalize_code(item.get("code"))
            if len(code) == 6:
                theme_codes.append(code)

    slots_cfg = load_slots_config_file()
    modes_path = selected_modes.SELECTED_MODES_FILE
    positions_path = trade_state.POSITIONS_STATE_FILE

    return {
        "load_universe_exists": False,
        "note": (
            "Universe는 JSON 파일에서 load 하지 않습니다. "
            "scheduler._reload_universe_cache → get_swing_universe → build_brain_universe "
            "(KIS API + config.BRAIN_THEME_EVENTS)."
        ),
        "pipeline": [
            "scheduler._reload_universe_cache / _get_universe_cached",
            "stock_swing.get_swing_universe",
            "brain.build_brain_universe",
            "  ├─ brain.scan_market_leaders (KIS 거래대금 TOP)",
            "  ├─ config.BRAIN_THEME_EVENTS watchlist",
            "  └─ fallback: stock_universe.build_active_universe",
        ],
        "files_not_universe_list": {
            "selected_modes.json": {
                "path": str(modes_path),
                "exists": modes_path.is_file(),
                "role": "슬롯/종목 매매 모드(UI) — 유니버스 종목 목록 아님",
            },
            "slots_config.json": {
                "path": str(SLOTS_CONFIG_FILE),
                "exists": SLOTS_CONFIG_FILE.is_file(),
                "role": "슬롯 성격 스냅샷 — 유니버스 종목 목록 아님",
            },
            "positions_state.json": {
                "path": str(positions_path),
                "exists": positions_path.is_file(),
                "role": "보유·슬롯 상태 — 부트스트랩 시 모드만 config 우선",
            },
        },
        "config_py_theme_watchlist": theme_codes,
        "market_scan_py": str(project / "market_scan.py"),
        "legacy_fallback_module": "stock_universe.build_active_universe",
    }


def _config_stock_codes_from_json() -> dict[str, list[str]]:
    """selected_modes / slots_config 에서 6자리 종목코드 추출."""
    import re

    import selected_modes
    from bot_config_reload import load_slots_config_file

    codes: set[str] = set()
    from_modes: list[str] = []
    from_slots: list[str] = []

    modes = selected_modes.load_modes()
    for key in modes:
        if re.fullmatch(r"\d{6}", str(key)):
            codes.add(str(key))
            from_modes.append(str(key))

    slots_data = load_slots_config_file()
    for row in slots_data.get("slots") or []:
        if not isinstance(row, dict):
            continue
        c = normalize_code(row.get("code"))
        if len(c) == 6:
            codes.add(c)
            from_slots.append(c)

    return {
        "all": sorted(codes),
        "from_selected_modes": sorted(set(from_modes)),
        "from_slots_config": sorted(set(from_slots)),
    }


def audit_config_codes_vs_brain_sources() -> dict[str, Any]:
    """
    JSON 설정 종목이 Brain 유니버스 소스(테마·FlowTracker)에 포함되는지 점검.
    """
    paths = explain_universe_load_paths()
    theme_set = set(paths.get("config_py_theme_watchlist") or [])
    cfg = _config_stock_codes_from_json()
    all_codes = cfg["all"]

    per_code: list[dict[str, Any]] = []
    for code in all_codes:
        in_theme = code in theme_set
        per_code.append(
            {
                "code": code,
                "in_BRAIN_THEME_EVENTS": in_theme,
                "enters_brain_via": (
                    "theme_watchlist (config.py)"
                    if in_theme
                    else "scan_market_leaders only (당일 TOP+필터 통과 시)"
                ),
            }
        )

    missing_theme = [c for c in all_codes if c not in theme_set]
    return {
        "paths": paths,
        "config_codes": cfg,
        "per_code": per_code,
        "summary": (
            f"설정 종목 {len(all_codes)}개 · 테마 워치리스트 {len(theme_set)}개 · "
            f"테마 외 종목 {len(missing_theme)}개 (FlowTracker 통과 필요)"
        ),
        "codes_not_in_theme_watchlist": missing_theme,
    }


def rank_universe_for_scan(universe: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """market_scan 진입 전 — brain_score 내림차순."""
    scored = [enrich_stock_with_brain(dict(s), auto_mode=True) for s in universe]
    scored.sort(key=lambda s: float(s.get("brain_score") or 0), reverse=True)
    return scored


@dataclass
class ThemeAction:
    code: str
    action: str  # staged_buy | prepare_exit | tighten_trail
    reason: str
    budget_factor: float = 1.0


def plan_theme_actions(
    positions: dict[str, dict[str, Any]],
    *,
    empty_slots: int,
    now: date | None = None,
) -> list[ThemeAction]:
    """
    스케줄러용 — 테마 디데이 분할 매수·청산 준비.

    - ACCUMULATE: 빈 슬롯 있으면 분할 매수 후보 (budget_factor < 1)
    - PRE_EXIT / EVENT_DAY: 보유 시 청산/트레일링 강화 신호
    """
    actions: list[ThemeAction] = []
    today = now or date.today()
    held = set(positions.keys())

    for event in _theme_event_configs():
        event_date = date.fromisoformat(str(event["event_date"]))
        days_until = (event_date - today).days
        slices = max(1, int(event.get("staged_buy_slices", 3)))

        for item in event.get("watchlist") or []:
            code = normalize_code(item.get("code"))
            plan = resolve_theme_phase(code, today)
            if not plan:
                continue

            if plan.accumulate and code not in held and empty_slots > 0:
                factor = 1.0 / slices
                actions.append(
                    ThemeAction(
                        code=code,
                        action="staged_buy",
                        reason=plan.note,
                        budget_factor=factor,
                    )
                )
            pos = positions.get(code)
            if pos and plan.prepare_exit:
                actions.append(
                    ThemeAction(
                        code=code,
                        action="prepare_exit",
                        reason=plan.note,
                    )
                )
            if pos and plan.phase == ThemePhase.EVENT_DAY:
                actions.append(
                    ThemeAction(
                        code=code,
                        action="tighten_trail",
                        reason="디데이 — 변동성 대응",
                    )
                )
    return actions


def pick_brain_recommendations(
    universe: list[dict[str, Any]],
    *,
    exclude_codes: set[str] | None = None,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """UI 슬롯 추천 풀 — 비주도주 하위 제외."""
    exclude = exclude_codes or set()
    min_score = float(_cfg("BRAIN_MIN_RECOMMEND_SCORE", 25.0))
    rows = rank_universe_for_scan(universe)
    picks: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("code") or "")
        if code in exclude:
            continue
        if float(row.get("brain_score") or 0) < min_score and not row.get(
            "theme_watchlist"
        ):
            continue
        if float(row.get("leader_boost") or 0) < -10:
            continue
        picks.append(row)
        if len(picks) >= limit:
            break
    return picks
