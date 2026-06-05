"""
CSV 데이터셋으로 손절 분류 모델 학습 → joblib 저장.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from config import PROJECT_DIR
from ml_stop_loss.dataset import DEFAULT_DATASET_PATH, load_dataset_rows
from ml_stop_loss.features import FEATURE_NAMES

logger = logging.getLogger(__name__)

DEFAULT_MODEL_PATH = PROJECT_DIR / "models" / "stop_loss_classifier.joblib"


def train_stop_loss_classifier(
    dataset_path: Path | None = None,
    model_path: Path | None = None,
) -> dict[str, Any]:
    dataset_path = dataset_path or DEFAULT_DATASET_PATH
    model_path = model_path or DEFAULT_MODEL_PATH
    rows = load_dataset_rows(dataset_path)

    if len(rows) < 2:
        raise ValueError(
            f"학습 데이터 부족 ({len(rows)}행). build_ml_dataset.py 먼저 실행하세요."
        )

    x_rows: list[list[float]] = []
    y_rows: list[int] = []
    for row in rows:
        try:
            vec = [float(row.get(col) or 0.0) for col in FEATURE_NAMES]
            label = int(float(row.get("label_stop_loss") or 0))
        except (TypeError, ValueError):
            continue
        x_rows.append(vec)
        y_rows.append(label)

    if len(x_rows) < 2:
        raise ValueError("유효 특징 행이 2개 미만입니다.")

    x = np.array(x_rows, dtype=np.float64)
    y = np.array(y_rows, dtype=np.int32)

    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    n_loss = int((y == 1).sum())
    n_win = int((y == 0).sum())

    clf = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            (
                "clf",
                GradientBoostingClassifier(
                    n_estimators=min(80, max(20, len(x) * 5)),
                    max_depth=3,
                    learning_rate=0.08,
                    min_samples_leaf=1,
                    random_state=42,
                ),
            ),
        ]
    )
    clf.fit(x, y)

    model_path.parent.mkdir(parents=True, exist_ok=True)
    import joblib

    joblib.dump(clf, model_path)

    train_acc = float((clf.predict(x) == y).mean())
    report = {
        "model_path": str(model_path),
        "dataset_path": str(dataset_path),
        "n_samples": len(x),
        "n_stop_loss": n_loss,
        "n_profit": n_win,
        "train_accuracy": round(train_acc, 4),
        "feature_names": list(FEATURE_NAMES),
    }
    logger.info(
        "모델 저장 %s — samples=%d (loss=%d, win=%d) train_acc=%.2f",
        model_path,
        len(x),
        n_loss,
        n_win,
        train_acc,
    )
    return report
