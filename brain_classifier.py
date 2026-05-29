"""
중앙 판단 뇌 (Brain Classifier)
종목·시장 상태를 진단해 포지션 유형(단타 / 스윙 / 장투)을 태깅합니다.

향후 시간대별·수급별 '8대 주식 상식' 규칙은 ModeRule 프로토콜로 모드별 등록해 결합합니다.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class TradingMode(str, Enum):
    SCALPING = "scalping"
    SWING = "swing"
    LONG_TERM = "long_term"


MODE_LABEL_KO: dict[TradingMode, str] = {
    TradingMode.SCALPING: "단타",
    TradingMode.SWING: "스윙",
    TradingMode.LONG_TERM: "장투",
}

MODE_BADGE_CSS: dict[TradingMode, str] = {
    TradingMode.SCALPING: "mode-scalping",
    TradingMode.SWING: "mode-swing",
    TradingMode.LONG_TERM: "mode-longterm",
}


@dataclass(frozen=True)
class ModePolicy:
    """모드별 운용 원칙 (다음 단계 규칙 엔진이 참조)."""

    chart_timeframe: str
    hold_days_min: int
    hold_days_max: int
    exit_same_day: bool
    buy_time_hint: str
    description: str


MODE_POLICIES: dict[TradingMode, ModePolicy] = {
    TradingMode.SCALPING: ModePolicy(
        chart_timeframe="1m/3m",
        hold_days_min=0,
        hold_days_max=0,
        exit_same_day=True,
        buy_time_hint="장중 체결 강도 급증 구간",
        description="분봉 타격 · 당일 청산",
    ),
    TradingMode.SWING: ModePolicy(
        chart_timeframe="60m",
        hold_days_min=2,
        hold_days_max=5,
        exit_same_day=False,
        buy_time_hint="오후 시간대 1H 눌림목",
        description="2~5일 보유 · 1H 정배열 눌림목",
    ),
    TradingMode.LONG_TERM: ModePolicy(
        chart_timeframe="1D",
        hold_days_min=30,
        hold_days_max=365,
        exit_same_day=False,
        buy_time_hint="일봉 추세·수급 호재",
        description="장기 홀딩 · 메가 트렌드",
    ),
}


@dataclass
class ModeDecision:
    mode: TradingMode
    label: str
    badge_class: str
    confidence: float
    policy: ModePolicy
    rationale: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    ruleset_id: str = "brain_v1"

    def to_position_tags(self) -> dict[str, Any]:
        """포지션·픽 dict에 병합할 태그 필드."""
        return {
            "trading_mode": self.mode.value,
            "mode_label": self.label,
            "mode_badge_class": self.badge_class,
            "mode_confidence": round(self.confidence, 1),
            "mode_hold_hint": (
                f"{self.policy.hold_days_min}~{self.policy.hold_days_max}일"
                if not self.policy.exit_same_day
                else "당일청산"
            ),
            "mode_chart": self.policy.chart_timeframe,
            "mode_policy": self.policy.description,
            "mode_rationale": " · ".join(self.rationale[:3]),
        }


@dataclass
class ClassificationContext:
    """판단 입력 — 스캔 시점 종목·봉·시장 스냅샷."""

    stock: dict[str, Any]
    hourly_bars: list[dict] | None = None
    swing_setup_passed: bool = False
    now: datetime = field(default_factory=datetime.now)
    market_regime: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ModeRule(ABC):
    """확장용 모드별 규칙 (8대 상식 등록 지점)."""

    rule_id: str = "base"

    @abstractmethod
    def score(self, ctx: ClassificationContext) -> float:
        """0~100 — 높을수록 해당 모드 적합."""

    @abstractmethod
    def describe(self, ctx: ClassificationContext) -> str | None:
        """판단 근거 한 줄 (없으면 None)."""


class ScalpingVolumeExplosionRule(ModeRule):
    rule_id = "scalp_vol_explosion"

    def __init__(self, min_vol_ratio_pct: float = 500.0) -> None:
        self.min_vol_ratio_pct = min_vol_ratio_pct

    def score(self, ctx: ClassificationContext) -> float:
        s = ctx.stock
        vol_pct = float(s.get("prdy_vrss_vol_rate") or 0)
        if vol_pct >= self.min_vol_ratio_pct:
            return min(100.0, 40.0 + vol_pct / 20.0)
        ratio = float(s.get("volume_ratio_vs_prev") or 0)
        if ratio >= self.min_vol_ratio_pct / 100.0:
            return min(100.0, 50.0 + ratio * 10.0)
        change = abs(float(s.get("change_rate") or 0))
        if vol_pct >= 200 and change >= 5.0:
            return 55.0
        return 0.0

    def describe(self, ctx: ClassificationContext) -> str | None:
        v = float(ctx.stock.get("prdy_vrss_vol_rate") or 0)
        if v >= self.min_vol_ratio_pct:
            return f"전일대비 거래량 {v:.0f}% 폭발"
        return None


class ScalpingExecutionStrengthRule(ModeRule):
    rule_id = "scalp_exec_strength"

    def score(self, ctx: ClassificationContext) -> float:
        bars = ctx.hourly_bars or []
        if len(bars) < 4:
            chg = float(ctx.stock.get("change_rate") or 0)
            return 35.0 if chg >= 7.0 else 0.0
        vols = [int(b.get("volume") or 0) for b in bars[-4:]]
        if max(vols) <= 0:
            return 0.0
        last_v, prev_avg = vols[-1], sum(vols[:-1]) / max(len(vols) - 1, 1)
        if prev_avg > 0 and last_v / prev_avg >= 2.5:
            return 45.0
        chg = float(ctx.stock.get("change_rate") or 0)
        if chg >= 8.0:
            return 40.0
        return 0.0

    def describe(self, ctx: ClassificationContext) -> str | None:
        if float(ctx.stock.get("change_rate") or 0) >= 7.0:
            return "당일 등락·체결 강도 급증"
        return None


class SwingPullbackRule(ModeRule):
    rule_id = "swing_1h_pullback"

    def score(self, ctx: ClassificationContext) -> float:
        if ctx.swing_setup_passed:
            return 85.0
        bars = ctx.hourly_bars or []
        if len(bars) < 20:
            return 0.0
        closes = [b["close"] for b in bars]
        if not _hourly_golden_alignment(closes):
            return 0.0
        if not _volume_dry_after_wave(bars):
            return 25.0
        return 60.0

    def describe(self, ctx: ClassificationContext) -> str | None:
        if ctx.swing_setup_passed:
            return "60분봉 정배열 첫 눌림목"
        return None


class SwingAfternoonRule(ModeRule):
    rule_id = "swing_afternoon"

    def score(self, ctx: ClassificationContext) -> float:
        h = ctx.now.hour
        if 13 <= h < 15 and ctx.swing_setup_passed:
            return 25.0
        if ctx.swing_setup_passed:
            return 10.0
        return 0.0

    def describe(self, ctx: ClassificationContext) -> str | None:
        if 13 <= ctx.now.hour < 15:
            return "오후 매수 시간대"
        return None


class LongTermLargeCapRule(ModeRule):
    rule_id = "long_large_cap"

    def __init__(self, min_cap: int = 2_000_000_000_000) -> None:
        self.min_cap = min_cap

    def score(self, ctx: ClassificationContext) -> float:
        cap = int(ctx.stock.get("market_cap") or 0)
        if cap >= self.min_cap:
            return 50.0
        if cap >= self.min_cap // 2:
            return 25.0
        return 0.0

    def describe(self, ctx: ClassificationContext) -> str | None:
        cap = int(ctx.stock.get("market_cap") or 0)
        if cap >= self.min_cap:
            return f"시총 {cap // 100_000_000_000}천억+ 대형주"
        return None


class LongTermInstitutionFlowRule(ModeRule):
    rule_id = "long_inst_flow"

    def score(self, ctx: ClassificationContext) -> float:
        frgn = int(ctx.stock.get("frgn_ntby_qty") or 0)
        prog = int(ctx.stock.get("pgtr_ntby_qty") or 0)
        inst_streak = int(ctx.stock.get("inst_buy_streak") or 0)
        score = 0.0
        if frgn > 0:
            score += 20.0
        if prog > 0:
            score += 20.0
        if inst_streak >= 2:
            score += 30.0
        elif inst_streak == 1:
            score += 15.0
        return min(100.0, score)

    def describe(self, ctx: ClassificationContext) -> str | None:
        if int(ctx.stock.get("frgn_ntby_qty") or 0) > 0:
            return "외인 순매수"
        if int(ctx.stock.get("pgtr_ntby_qty") or 0) > 0:
            return "기관·프로그램 순매수"
        return None


class LongTermMegaTrendRule(ModeRule):
    """메가 트렌드 호재 — 향후 뉴스/테마 API 연동 슬롯."""

    rule_id = "long_mega_trend"

    def score(self, ctx: ClassificationContext) -> float:
        if ctx.stock.get("mega_trend"):
            return 40.0
        chg = float(ctx.stock.get("change_rate") or 0)
        cap = int(ctx.stock.get("market_cap") or 0)
        if cap >= 1_000_000_000_000 and 0 < chg < 4.0:
            return 20.0
        return 0.0

    def describe(self, ctx: ClassificationContext) -> str | None:
        if ctx.stock.get("mega_trend"):
            return "메가 트렌드 호재"
        return None


def _hourly_golden_alignment(closes: list[int]) -> bool:
    if len(closes) < 65:
        return False

    def ma(p: int) -> float:
        return sum(closes[-p:]) / p

    ma5, ma20, ma60 = ma(5), ma(20), ma(60)
    return ma5 > ma20 > ma60


def _volume_dry_after_wave(bars: list[dict]) -> bool:
    """1차 파동 후 거래량 감소(숨고르기) 근사."""
    if len(bars) < 10:
        return False
    closes = [b["close"] for b in bars]
    vols = [int(b.get("volume") or 0) for b in bars]
    for i in range(-8, -3):
        if i - 1 < -len(closes):
            continue
        if closes[i - 1] > 0:
            surge = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
            if surge >= 5.0:
                if len(vols) >= 3 and vols[-2] <= vols[-3] * 0.55:
                    return True
    return vols[-2] <= vols[-4] * 0.5 if len(vols) >= 4 else False


class BrainClassifier:
    """
    중앙 판단 뇌 — Determine Trading Mode.

    사용 흐름:
      ctx = ClassificationContext(stock=..., hourly_bars=..., swing_setup_passed=True)
      decision = brain.classify(ctx)
      tagged = brain.tag_stock(stock, decision)
    """

    def __init__(
        self,
        *,
        scalp_vol_min_pct: float = 500.0,
        long_term_min_cap: int = 2_000_000_000_000,
    ) -> None:
        self.scalp_vol_min_pct = scalp_vol_min_pct
        self.long_term_min_cap = long_term_min_cap
        self._rules: dict[TradingMode, list[ModeRule]] = {
            TradingMode.SCALPING: [
                ScalpingVolumeExplosionRule(scalp_vol_min_pct),
                ScalpingExecutionStrengthRule(),
            ],
            TradingMode.SWING: [
                SwingPullbackRule(),
                SwingAfternoonRule(),
            ],
            TradingMode.LONG_TERM: [
                LongTermLargeCapRule(long_term_min_cap),
                LongTermInstitutionFlowRule(),
                LongTermMegaTrendRule(),
            ],
        }
        self._hooks_post: list[Callable[[ClassificationContext, ModeDecision], None]] = []

    def register_rule(self, mode: TradingMode, rule: ModeRule) -> None:
        """8대 상식 등 확장 규칙 등록."""
        self._rules.setdefault(mode, []).append(rule)

    def register_post_hook(
        self, fn: Callable[[ClassificationContext, ModeDecision], None]
    ) -> None:
        self._hooks_post.append(fn)

    def _score_mode(self, mode: TradingMode, ctx: ClassificationContext) -> tuple[float, list[str]]:
        total = 0.0
        notes: list[str] = []
        for rule in self._rules.get(mode, []):
            s = rule.score(ctx)
            total += s
            desc = rule.describe(ctx)
            if desc and s > 0:
                notes.append(desc)
        return total, notes

    def classify(self, ctx: ClassificationContext) -> ModeDecision:
        scores: dict[str, float] = {}
        rationales: dict[TradingMode, list[str]] = {}

        for mode in TradingMode:
            sc, notes = self._score_mode(mode, ctx)
            scores[mode.value] = sc
            rationales[mode] = notes

        # 스윙 파이프라인 통과 종목 — 스윙 최소 가중 (1H 눌림목 원칙 유지)
        if ctx.swing_setup_passed:
            scores[TradingMode.SWING.value] = max(
                scores[TradingMode.SWING.value], 70.0
            )

        best_mode = max(TradingMode, key=lambda m: scores[m.value])
        best_score = scores[best_mode.value]

        if best_score < 30.0:
            if ctx.swing_setup_passed:
                best_mode = TradingMode.SWING
            else:
                best_mode = max(TradingMode, key=lambda m: scores[m.value])
            best_score = max(scores[best_mode.value], 30.0)

        total_sc = sum(scores.values()) or 1.0
        confidence = min(99.0, best_score / total_sc * 100.0)

        policy = MODE_POLICIES[best_mode]
        decision = ModeDecision(
            mode=best_mode,
            label=MODE_LABEL_KO[best_mode],
            badge_class=MODE_BADGE_CSS[best_mode],
            confidence=confidence,
            policy=policy,
            rationale=rationales.get(best_mode, []) or [policy.description],
            scores=scores,
        )

        for hook in self._hooks_post:
            try:
                hook(ctx, decision)
            except Exception:
                logger.exception("brain post_hook")

        return decision

    def tag_stock(
        self,
        stock: dict[str, Any],
        decision: ModeDecision | None = None,
        *,
        hourly_bars: list[dict] | None = None,
        swing_setup_passed: bool = False,
    ) -> dict[str, Any]:
        """스캔 결과 dict에 모드 태그 부착."""
        if decision is None:
            ctx = ClassificationContext(
                stock=stock,
                hourly_bars=hourly_bars,
                swing_setup_passed=swing_setup_passed
                or bool(stock.get("swing_setup_passed")),
            )
            decision = self.classify(ctx)
        tagged = {**stock, **decision.to_position_tags()}
        return tagged

    def tag_pick(
        self,
        pick: dict[str, Any],
        *,
        hourly_bars: list[dict] | None = None,
    ) -> dict[str, Any]:
        """1H 눌림목 픽 — 판단 뇌 통과 후 태깅."""
        return self.tag_stock(
            pick,
            hourly_bars=hourly_bars,
            swing_setup_passed=True,
        )


_brain_singleton: BrainClassifier | None = None


def get_brain_classifier() -> BrainClassifier:
    global _brain_singleton
    if _brain_singleton is None:
        try:
            import config as cfg

            _brain_singleton = BrainClassifier(
                scalp_vol_min_pct=float(
                    getattr(cfg, "BRAIN_SCALP_VOL_RATIO_PCT", 500.0)
                ),
                long_term_min_cap=int(
                    getattr(cfg, "BRAIN_LONG_TERM_MIN_MARKET_CAP", 2_000_000_000_000)
                ),
            )
        except ImportError:
            _brain_singleton = BrainClassifier()
    return _brain_singleton


def determine_trading_mode(
    stock: dict[str, Any],
    *,
    hourly_bars: list[dict] | None = None,
    swing_setup_passed: bool = False,
) -> ModeDecision:
    """편의 함수 — 모드 판단만 수행."""
    brain = get_brain_classifier()
    ctx = ClassificationContext(
        stock=stock,
        hourly_bars=hourly_bars,
        swing_setup_passed=swing_setup_passed,
    )
    return brain.classify(ctx)
