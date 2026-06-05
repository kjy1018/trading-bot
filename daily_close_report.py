"""
장 마감 직후 일일 복기·시장 요약·내일 전략 리포트.

trade_history.db 체결 내역 + 지수(Yahoo) + 보유 종목 일봉 MA 분석.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime
from typing import Any

import requests

import config
import trade_history_db as thdb
from stock_daily import compute_ma_bundle, fetch_daily_ohlc_bars

logger = logging.getLogger(__name__)

_INDEX_SYMBOLS = {
    "KOSPI": "^KS11",
    "KOSDAQ": "^KQ11",
    "NASDAQ": "^IXIC",
}


def _fetch_yahoo_index(symbol: str) -> dict[str, Any]:
    """Yahoo Finance chart API — 당일·전일 종가 기준 등락."""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?range=5d&interval=1d"
    )
    out: dict[str, Any] = {
        "symbol": symbol,
        "price": None,
        "change_pct": None,
        "trend": "unknown",
        "error": None,
    }
    try:
        resp = requests.get(
            url,
            timeout=12,
            headers={"User-Agent": "mirae-bot/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            out["error"] = "empty"
            return out
        meta = result[0].get("meta") or {}
        closes = (result[0].get("indicators") or {}).get("quote", [{}])
        close_list = (closes[0] if closes else {}).get("close") or []
        close_list = [c for c in close_list if c is not None]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None and close_list:
            price = close_list[-1]
        if prev is None and len(close_list) >= 2:
            prev = close_list[-2]
        if price is not None:
            out["price"] = round(float(price), 2)
        if price is not None and prev and float(prev) != 0:
            chg = (float(price) - float(prev)) / float(prev) * 100.0
            out["change_pct"] = round(chg, 2)
            if chg >= 0.5:
                out["trend"] = "bullish"
            elif chg <= -0.5:
                out["trend"] = "bearish"
            else:
                out["trend"] = "neutral"
    except Exception as exc:
        logger.debug("지수 조회 실패 %s: %s", symbol, exc)
        out["error"] = str(exc)
    return out


def fetch_major_indices() -> dict[str, Any]:
    indices: dict[str, Any] = {}
    for label, sym in _INDEX_SYMBOLS.items():
        indices[label] = _fetch_yahoo_index(sym)
    return indices


def analyze_today_trades(trade_date: str | None = None) -> dict[str, Any]:
    """당일 trade_history.db 체결 — 수익률·패턴."""
    day = trade_date or date.today().isoformat()
    trades = thdb.list_trades_for_date(day)
    buys = [t for t in trades if str(t.get("side")) == "buy"]
    sells = [t for t in trades if str(t.get("side")) == "sell"]

    realized = sum(int(t.get("pnl") or 0) for t in sells)
    win = sum(1 for t in sells if int(t.get("pnl") or 0) > 0)
    loss = sum(1 for t in sells if int(t.get("pnl") or 0) < 0)
    flat = len(sells) - win - loss
    pcts = [float(t.get("profit_pct") or 0) for t in sells if t.get("profit_pct") is not None]
    avg_pct = round(sum(pcts) / len(pcts), 2) if pcts else 0.0

    exit_types = Counter(str(t.get("exit_type") or "청산") for t in sells)
    codes_traded = Counter(
        str(t.get("stock_code") or t.get("stock_name") or "?") for t in trades
    )

    patterns: list[str] = []
    if not trades:
        patterns.append("당일 체결 기록 없음 — 관망 또는 미체결 상태")
    else:
        if len(sells) >= 4:
            patterns.append(f"매매 빈도 높음 — 매도 {len(sells)}회 (단기 회전 성향)")
        elif len(sells) == 0 and len(buys) > 0:
            patterns.append(f"매수만 {len(buys)}건 — 신규 진입·보유 확대")
        elif len(sells) <= 2:
            patterns.append("저빈도 매매 — 스윙/중장기 보유 유지에 가까움")
        if win > loss and len(sells) >= 2:
            patterns.append(f"승률 우세 ({win}승 {loss}패)")
        elif loss > win and len(sells) >= 2:
            patterns.append(f"손실 청산 다수 ({loss}패 {win}승) — 리스크 점검 필요")
        top_exit = exit_types.most_common(1)
        if top_exit:
            patterns.append(f"주요 청산 유형: {top_exit[0][0]} ({top_exit[0][1]}회)")
        if codes_traded:
            hot = codes_traded.most_common(2)
            names = ", ".join(f"{k}({v}회)" for k, v in hot)
            patterns.append(f"활동 종목: {names}")

    return {
        "trade_date": day,
        "buy_count": len(buys),
        "sell_count": len(sells),
        "realized_pnl": realized,
        "win_count": win,
        "loss_count": loss,
        "flat_count": flat,
        "avg_sell_pct": avg_pct,
        "exit_types": dict(exit_types),
        "patterns": patterns,
        "trades": trades,
    }


def analyze_holdings_ma(
    holdings: list[dict[str, Any]],
    *,
    access_token: str,
    app_key: str,
    app_secret: str,
) -> list[dict[str, Any]]:
    """보유 종목 일봉 MA5/20/60 위치."""
    rows: list[dict[str, Any]] = []
    lookback = int(getattr(config, "POLLING_DAILY_LOOKBACK_DAYS", 90))
    for pos in holdings:
        code = str(pos.get("code") or "").strip()[-6:]
        if len(code) != 6:
            continue
        name = str(pos.get("name") or code)
        mode = str(pos.get("trading_mode") or pos.get("slot_personality") or "swing")
        current = int(pos.get("current_price") or pos.get("entry_price") or 0)
        profit_pct = float(pos.get("profit_pct") or 0.0)
        try:
            bars = fetch_daily_ohlc_bars(
                access_token, app_key, app_secret, code, lookback_days=lookback
            )
        except Exception as exc:
            logger.debug("보유 MA 조회 실패 %s: %s", code, exc)
            bars = []
        bundle = compute_ma_bundle([int(b.get("close") or 0) for b in bars])
        ma20 = bundle.get("ma20")
        ma60 = bundle.get("ma60")
        aligned = bool(bundle.get("ma_aligned"))
        vs_ma20 = None
        if ma20 and current > 0:
            vs_ma20 = round((current - float(ma20)) / float(ma20) * 100.0, 2)
        if current > 0 and ma20:
            if current >= float(ma20) * 1.01:
                ma_pos = "MA20 위 (상승 추세 유지)"
            elif current >= float(ma20) * 0.98:
                ma_pos = "MA20 부근 (지지 시험)"
            else:
                ma_pos = "MA20 하회 (약세·현금화 검토)"
        else:
            ma_pos = "MA 데이터 부족"
        rows.append(
            {
                "code": code,
                "name": name,
                "mode": mode,
                "current": current,
                "profit_pct": profit_pct,
                "ma_aligned": aligned,
                "ma20": ma20,
                "ma60": ma60,
                "vs_ma20_pct": vs_ma20,
                "ma_position_label": ma_pos,
            }
        )
    return rows


def _rule_tomorrow_strategy(
    review: dict[str, Any],
    indices: dict[str, Any],
    holdings_ma: list[dict[str, Any]],
    account: dict[str, Any],
) -> list[str]:
    """규칙 기반 내일 대응 — Gemini 없을 때."""
    lines: list[str] = []
    kospi = indices.get("KOSPI") or {}
    nasdaq = indices.get("NASDAQ") or {}
    k_chg = float(kospi.get("change_pct") or 0)
    n_chg = float(nasdaq.get("change_pct") or 0)
    realized = int(review.get("realized_pnl") or 0)

    if k_chg >= 0.8 and n_chg >= 0.3:
        lines.append("지수 동반 상승 — 보유 우량주 유지, 신규는 MA 정배열 종목만 제한적 진입")
    elif k_chg <= -1.0 or n_chg <= -1.5:
        lines.append("지수 약세 — 신규 매수 자제, MA20 하회 종목은 분할 현금화 검토")
    else:
        lines.append("지수 횡보 — 기존 포지션 유지, 추격 매수 금지")

    below_ma = [h for h in holdings_ma if h.get("vs_ma20_pct") is not None and float(h["vs_ma20_pct"]) < -2]
    if len(below_ma) >= 2:
        lines.append(
            f"보유 {len(below_ma)}종목 MA20 하회 — 내일 장 초 반 등락 확인 후 약세 종목 우선 정리"
        )
    elif holdings_ma and all(
        (h.get("vs_ma20_pct") or 0) >= 0 for h in holdings_ma if h.get("vs_ma20_pct") is not None
    ):
        lines.append("보유 종목 대부분 MA20 상단 — 추세 추종 유지, 트레일링 스탑만 점검")

    if realized < -100_000:
        lines.append("당일 실현 손실 큼 — 내일 베팅 규모 축소·Cap 한도 엄수")
    elif realized > 100_000:
        lines.append("당일 실현 이익 — 과열 추격 대신 익절 종목 현금 비중 확보")

    if not lines:
        lines.append("특이 신호 없음 — 기존 스윙/장투 규칙 유지")
    return lines


def _gemini_tomorrow_strategy(context: dict[str, Any]) -> str | None:
    key = str(getattr(config, "GEMINI_API_KEY", "") or "").strip()
    if not key or not bool(getattr(config, "ENABLE_DAILY_CLOSE_REPORT_AI", True)):
        return None
    model = str(getattr(config, "GEMINI_MODEL", "gemini-1.5-flash")).strip()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={key}"
    )
    prompt = {
        "role": "한국 주식 스윙/중장기 트레이딩 참모",
        "task": "아래 JSON을 바탕으로 내일 장 대응 전략을 한국어 4~6문장으로 작성",
        "format": "불릿 없이 문단, 구체적 행동(보유/현금화/진입제한) 포함",
        "data": context,
    }
    body = {
        "contents": [{"parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}]
    }
    try:
        resp = requests.post(url, json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        text = (
            (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [{}])[0]
            .get("text", "")
            .strip()
        )
        return re.sub(r"\s+", " ", text) if text else None
    except Exception as exc:
        logger.debug("장마감 Gemini 전략 실패: %s", exc)
        return None


def build_daily_close_report(
    *,
    trade_date: str | None = None,
    holdings: list[dict[str, Any]] | None = None,
    account: dict[str, Any] | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    """전체 리포트 dict + markdown 본문."""
    day = trade_date or date.today().isoformat()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    review = analyze_today_trades(day)
    indices = fetch_major_indices()

    token = access_token
    if not token:
        try:
            from auth import get_access_token

            token = get_access_token()
        except Exception:
            token = ""

    holdings_ma: list[dict[str, Any]] = []
    if holdings and token:
        holdings_ma = analyze_holdings_ma(
            holdings,
            access_token=token,
            app_key=config.APP_KEY,
            app_secret=config.APP_SECRET,
        )

    acct = dict(account or {})
    from report import (
        apply_report_settlement_to_account,
        normalize_settlement_for_report,
        purge_phantom_positions_if_broker_empty,
    )

    purge_phantom_positions_if_broker_empty(acct)
    settlement = normalize_settlement_for_report(acct, positions=holdings)
    acct = apply_report_settlement_to_account(acct, settlement)
    ctx = {
        "review": {
            "realized_pnl": review.get("realized_pnl"),
            "sell_count": review.get("sell_count"),
            "patterns": review.get("patterns"),
        },
        "indices": {
            k: {"change_pct": v.get("change_pct"), "trend": v.get("trend")}
            for k, v in indices.items()
        },
        "holdings": [
            {
                "name": h.get("name"),
                "profit_pct": h.get("profit_pct"),
                "ma_position": h.get("ma_position_label"),
            }
            for h in holdings_ma
        ],
        "account": {
            "cash": acct.get("cash"),
            "total_eval": acct.get("total_eval") or acct.get("stock_eval"),
        },
    }
    ai_strategy = _gemini_tomorrow_strategy(ctx)
    rule_lines = _rule_tomorrow_strategy(review, indices, holdings_ma, acct)
    tomorrow_text = ai_strategy or "\n".join(f"• {ln}" for ln in rule_lines)

    md = format_report_markdown(
        trade_date=day,
        generated_at=now,
        review=review,
        indices=indices,
        holdings_ma=holdings_ma,
        account=acct,
        tomorrow_strategy=tomorrow_text,
        ai_generated=bool(ai_strategy),
    )

    report = {
        "trade_date": day,
        "generated_at": now,
        "review": review,
        "indices": indices,
        "holdings_ma": holdings_ma,
        "account_snapshot": acct,
        "return_rate": float(settlement["return_rate"]),
        "profit_loss": int(settlement["profit_loss"]),
        "total_assets": int(settlement["total_assets"]),
        "current_deposit": int(settlement["current_deposit"]),
        "current_stock_valuation": int(settlement["current_stock_valuation"]),
        "tomorrow_strategy": tomorrow_text,
        "tomorrow_ai": bool(ai_strategy),
        "markdown": md,
    }
    try:
        thdb.save_meta_json(f"daily_close_report_{day}", report)
    except Exception as exc:
        logger.debug("리포트 DB 저장 실패: %s", exc)
    return report


def format_report_markdown(
    *,
    trade_date: str,
    generated_at: str,
    review: dict[str, Any],
    indices: dict[str, Any],
    holdings_ma: list[dict[str, Any]],
    account: dict[str, Any],
    tomorrow_strategy: str,
    ai_generated: bool,
) -> str:
    cash = int(account.get("cash") or 0)
    stock_eval = int(account.get("stock_eval") or account.get("total_eval") or 0)
    from report import normalize_settlement_for_report

    settlement = normalize_settlement_for_report(account)
    pr_pct = float(settlement["return_rate"])
    pr_pnl = int(settlement["profit_loss"])
    total_assets = int(settlement["total_assets"])
    if settlement.get("holdings_zero_override"):
        stock_eval = 0

    lines: list[str] = [
        f"# 📋 장 마감 리포트 ({trade_date})",
        f"생성: {generated_at}",
        "",
        "## 💰 정산 수익률",
        (
            f"- **{pr_pct:+.2f}%** "
            f"(예수금 {cash:,} + 주식 {stock_eval:,} = 총자산 {total_assets:,}원 · "
            f"손익 {pr_pnl:+,}원 · 원금 10,000,000원)"
        ),
        "",
        "## 1. 오늘의 복기",
    ]
    if not review.get("trades"):
        lines.append("- 당일 DB 체결 없음")
    else:
        lines.append(
            f"- 매수 {review.get('buy_count', 0)}건 · 매도 {review.get('sell_count', 0)}건 · "
            f"실현손익 **{int(review.get('realized_pnl', 0)):+,}원**"
        )
        if review.get("sell_count", 0) > 0:
            lines.append(
                f"- 매도 평균 수익률 {review.get('avg_sell_pct', 0):+.2f}% · "
                f"승 {review.get('win_count', 0)} / 패 {review.get('loss_count', 0)}"
            )
        for p in review.get("patterns") or []:
            lines.append(f"- {p}")

    lines.extend(["", "## 2. 시장 상황"])
    for label in ("KOSPI", "KOSDAQ", "NASDAQ"):
        ix = indices.get(label) or {}
        chg = ix.get("change_pct")
        px = ix.get("price")
        if chg is not None and px is not None:
            lines.append(f"- **{label}** {px:,.2f} ({chg:+.2f}%) · {ix.get('trend', '-')}")
        else:
            lines.append(f"- **{label}** 조회 실패")

    if holdings_ma:
        lines.extend(["", "### 보유 종목 MA"])
        for h in holdings_ma:
            lines.append(
                f"- {h.get('name')}({h.get('code')}) "
                f"{float(h.get('profit_pct', 0)):+.1f}% · {h.get('ma_position_label')}"
            )
    else:
        lines.append("- 보유 종목 없음")

    tag = " (AI)" if ai_generated else ""
    lines.extend(["", f"## 3. 내일 전략{tag}", tomorrow_strategy.strip()])
    return "\n".join(lines)


def load_saved_report(trade_date: str | None = None) -> dict[str, Any] | None:
    day = trade_date or date.today().isoformat()
    raw = thdb.load_meta_json(f"daily_close_report_{day}")
    return raw if isinstance(raw, dict) else None
