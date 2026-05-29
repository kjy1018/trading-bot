"""
ATR(Average True Range) 기반 가변 손절가 계산 (1시간봉 OHLC).
Wilder ATR(period) — 탐색 단계에서 산출한 값으로 매수 직후 stop_loss_price 고정.
"""

from __future__ import annotations


def _true_range(high: float, low: float, prev_close: float) -> float:
    return max(
        high - low,
        abs(high - prev_close),
        abs(low - prev_close),
    )


def wilder_atr(
    ohlc: list[dict],
    period: int = 14,
) -> float | None:
    """
    ohlc: 각 원소에 open, high, low, close (float/int)
    최근 period+1 봉 이상 필요.
    """
    if len(ohlc) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(ohlc)):
        h = float(ohlc[i]["high"])
        l = float(ohlc[i]["low"])
        pc = float(ohlc[i - 1]["close"])
        trs.append(_true_range(h, l, pc))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def atr_stop_price(
    entry: int,
    atr: float,
    *,
    mult: float = 2.0,
    min_loss_pct: float = 3.0,
    max_loss_pct: float = 15.0,
) -> int:
    """
    손절 트리거 가격 (현재가가 이 가격 이하이면 손절).
    - raw = entry - mult * ATR
    - 너무 타이트하면 최소 min_loss_pct% 손실까지 허용(손절가 상한)
    - 너무 넓으면 max_loss_pct% 이상 손실은 제한(손절가 하한)
    """
    if entry <= 0 or atr <= 0:
        return max(1, int(round(entry * (1 - max_loss_pct / 100))))
    raw = entry - mult * atr
    cap_tight = entry * (1 - min_loss_pct / 100)
    floor_wide = entry * (1 - max_loss_pct / 100)
    stop = min(cap_tight, max(raw, floor_wide))
    return max(1, int(round(stop)))
