"""
매수 시점 차트 패턴 데이터셋 구축 + 손절 분류 모델 학습.

실행:
  python build_ml_dataset.py
  python build_ml_dataset.py --dataset-only
  python build_ml_dataset.py --train-only
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="ML 손절 예측 데이터셋·모델 빌드")
    parser.add_argument("--dataset-only", action="store_true", help="CSV만 생성")
    parser.add_argument("--train-only", action="store_true", help="기존 CSV로만 학습")
    parser.add_argument("--no-augment", action="store_true", help="봉 오프셋 증강 비활성")
    args = parser.parse_args()

    import config

    if not config.APP_KEY or not config.APP_SECRET:
        logger.error("APP_KEY / APP_SECRET 없음 — .env 확인")
        return 1

    token = None
    if not args.train_only:
        from auth import get_access_token

        token = get_access_token()
        logger.info("KIS 토큰 OK — 매수 시점 차트 조회 시작")

    from ml_stop_loss.dataset import build_trade_pattern_dataset
    from ml_stop_loss.train import train_stop_loss_classifier

    offsets = (0,) if args.no_augment else (0, 1, 2)

    if not args.train_only:
        path, rows = build_trade_pattern_dataset(token=token, augment_offsets=offsets)
        loss_n = sum(1 for r in rows if int(r.get("label_stop_loss") or 0) == 1)
        win_n = len(rows) - loss_n
        print(f"\n[데이터셋] {path}")
        print(f"  총 {len(rows)}행 · 손절 라벨={loss_n} · 수익 라벨={win_n}")
        for row in rows:
            flag = "LOSS" if int(row.get("label_stop_loss") or 0) else "WIN "
            print(
                f"  [{flag}] {row.get('stock_name')}({row.get('stock_code')}) "
                f"buy={row.get('buy_time')} label={row.get('label_stop_loss')}"
            )
        if len(rows) < 2:
            logger.warning(
                "체결 이력이 적습니다. data/ml_trade_seed.json 에 종목코드를 추가하세요."
            )

    if args.dataset_only:
        return 0

    try:
        report = train_stop_loss_classifier()
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    print(f"\n[모델] {report['model_path']}")
    print(
        f"  samples={report['n_samples']} "
        f"(loss={report['n_stop_loss']}, win={report['n_profit']}) "
        f"train_acc={report['train_accuracy']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
