"""
손절 확률 예측 — 학습된 scikit-learn 모델 (joblib).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from config import APP_KEY, APP_SECRET, PROJECT_DIR
from ml_stop_loss.features import (
    FEATURE_NAMES,
    extract_chart_features,
    features_to_vector,
    fetch_hourly_bars_for_pick,
)

logger = logging.getLogger(__name__)

_model_cache: Any = None
_model_path_loaded: Path | None = None


def _default_model_path() -> Path:
    try:
        import config as cfg

        raw = getattr(cfg, "ML_STOP_LOSS_MODEL_PATH", None)
        if raw:
            return Path(raw)
    except ImportError:
        pass
    return PROJECT_DIR / "models" / "stop_loss_classifier.joblib"


def _reject_threshold() -> float:
    try:
        import config as cfg

        return float(getattr(cfg, "ML_STOP_LOSS_REJECT_PROB", 0.60))
    except ImportError:
        return 0.60


def _ml_enabled() -> bool:
    try:
        import config as cfg

        return bool(getattr(cfg, "ML_STOP_LOSS_ENABLED", True))
    except ImportError:
        return True


def load_model(
    path: Path | None = None,
    *,
    force_reload: bool = False,
    log_load: bool = False,
) -> Any | None:
    global _model_cache, _model_path_loaded
    path = path or _default_model_path()
    if not force_reload and _model_cache is not None and _model_path_loaded == path:
        if log_load:
            logger.info("[AI 가드] 모델 캐시 사용 — %s", path)
        return _model_cache
    if not path.is_file():
        _model_cache = None
        _model_path_loaded = path
        if log_load:
            logger.warning("[AI 가드] 모델 파일 없음 — %s", path)
        return None
    try:
        import joblib

        _model_cache = joblib.load(path)
        _model_path_loaded = path
        if log_load:
            logger.info("[AI 가드] joblib.load() 완료 — %s", path)
        return _model_cache
    except Exception as exc:
        logger.warning("[AI 가드] joblib.load() 실패 %s: %s", path, exc)
        _model_cache = None
        _model_path_loaded = path
        return None


def evaluate_ml_buy_guard(
    pick: dict[str, Any],
    *,
    token: str | None = None,
    hourly_bars: list[dict] | None = None,
    threshold: float | None = None,
    log: bool = True,
) -> dict[str, Any]:
    """
    매수 후보 차트 패턴 → joblib 모델 predict → 손절 위험 판정.
    반환: reject, prob, pred_class, detail, dashboard_message 등.
    """
    code = str(pick.get("code") or "").strip()[-6:]
    name = str(pick.get("name") or code)
    thr = _reject_threshold() if threshold is None else float(threshold)
    out: dict[str, Any] = {
        "enabled": _ml_enabled(),
        "code": code,
        "name": name,
        "reject": False,
        "prob": None,
        "pred_class": None,
        "threshold": thr,
        "detail": "",
        "dashboard_message": "",
        "model_loaded": False,
    }

    if not out["enabled"]:
        out["detail"] = "ML 손절 게이트 비활성"
        return out

    if len(code) != 6:
        out["detail"] = "종목코드 무효"
        return out

    if log:
        logger.info("[AI 가드] 매수 후보 평가 시작 — %s(%s)", name, code)

    model = load_model(log_load=log)
    if model is None:
        out["detail"] = "ML 모델 미로드 — 게이트 스킵"
        if log:
            logger.warning("[AI 가드] %s(%s) — 모델 없음, 매수 게이트 스킵", name, code)
        return out
    out["model_loaded"] = True

    bars = hourly_bars
    if bars is None and token and APP_KEY and APP_SECRET:
        try:
            bars = fetch_hourly_bars_for_pick(token, APP_KEY, APP_SECRET, pick)
            if log:
                logger.info(
                    "[AI 가드] %s(%s) — 60분봉 %d개 로드",
                    name,
                    code,
                    len(bars or []),
                )
        except Exception as exc:
            logger.warning("[AI 가드] %s(%s) 차트 조회 실패: %s", name, code, exc)
            bars = []

    feats = extract_chart_features(bars, pick=pick)
    vec = features_to_vector(feats)
    if log:
        logger.info(
            "[AI 가드] %s(%s) — 차트 패턴 특징 %d차원 (RSI=%.1f, pullback=%.2f%%)",
            name,
            code,
            len(vec),
            float(feats.get("rsi_14") or 0),
            float(feats.get("pullback_from_5bar_high_pct") or 0),
        )

    try:
        pred_class = int(model.predict([vec])[0])
        out["pred_class"] = pred_class
        prob: float | None = None
        if hasattr(model, "predict_proba"):
            proba = model.predict_proba([vec])[0]
            classes = list(getattr(model, "classes_", [0, 1]))
            if 1 in classes:
                prob = float(proba[classes.index(1)])
            else:
                prob = float(proba[-1])
            if log:
                logger.info(
                    "[AI 가드] %s(%s) — model.predict=%d · predict_proba=%s · 손절확률=%.1f%%",
                    name,
                    code,
                    pred_class,
                    [round(float(p), 4) for p in proba],
                    (prob or 0) * 100.0,
                )
        else:
            prob = float(pred_class)
            if log:
                logger.info(
                    "[AI 가드] %s(%s) — model.predict=%d (확률 미지원)",
                    name,
                    code,
                    pred_class,
                )
        out["prob"] = prob
    except Exception as exc:
        out["detail"] = f"예측 실패: {exc}"
        logger.warning("[AI 가드] %s(%s) 예측 실패: %s", name, code, exc)
        return out

    if prob is None:
        out["detail"] = "손절 확률 산출 불가 — 게이트 스킵"
        return out

    if prob >= thr:
        out["reject"] = True
        out["detail"] = (
            f"손절 확률 {prob * 100:.1f}% ≥ {thr * 100:.0f}% — 매수 보류"
        )
        out["dashboard_message"] = "[AI 가드] 위험 신호 감지: 매수 보류"
        if log:
            logger.warning(
                "[AI 가드] %s(%s) — 위험 신호 감지 · 손절확률 %.1f%% ≥ %.0f%% · 매수 큐 진입 차단",
                name,
                code,
                prob * 100.0,
                thr * 100.0,
            )
    else:
        out["detail"] = f"손절 확률 {prob * 100:.1f}% < {thr * 100:.0f}% — 매수 허용"
        if log:
            logger.info(
                "[AI 가드] %s(%s) — 안전 구간 · 손절확률 %.1f%% < %.0f%% · 매수 허용",
                name,
                code,
                prob * 100.0,
                thr * 100.0,
            )
    return out


def predict_stop_loss_probability(
    pick: dict[str, Any],
    *,
    token: str | None = None,
    hourly_bars: list[dict] | None = None,
    model: Any | None = None,
) -> float | None:
    """손절(클래스 1) 확률 0.0~1.0."""
    if model is not None:
        code = str(pick.get("code") or "").strip()[-6:]
        if len(code) != 6:
            return None
        bars = hourly_bars
        if bars is None and token and APP_KEY and APP_SECRET:
            try:
                bars = fetch_hourly_bars_for_pick(token, APP_KEY, APP_SECRET, pick)
            except Exception:
                bars = []
        feats = extract_chart_features(bars, pick=pick)
        vec = features_to_vector(feats)
        try:
            if hasattr(model, "predict_proba"):
                proba = model.predict_proba([vec])[0]
                classes = list(getattr(model, "classes_", [0, 1]))
                if 1 in classes:
                    return float(proba[classes.index(1)])
                return float(proba[-1])
            return float(model.predict([vec])[0])
        except Exception:
            return None
    return evaluate_ml_buy_guard(
        pick, token=token, hourly_bars=hourly_bars, log=False
    ).get("prob")


def should_reject_buy_by_ml(
    pick: dict[str, Any],
    *,
    token: str | None = None,
    hourly_bars: list[dict] | None = None,
    threshold: float | None = None,
) -> tuple[bool, float | None, str]:
    """(거부 여부, 손절확률, 사유)."""
    result = evaluate_ml_buy_guard(
        pick,
        token=token,
        hourly_bars=hourly_bars,
        threshold=threshold,
        log=True,
    )
    return (
        bool(result.get("reject")),
        result.get("prob"),
        str(result.get("detail") or ""),
    )
