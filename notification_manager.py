from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import requests

import config
from ai_briefing import build_ai_briefing_payload
from discord_control import (
    _compute_cumulative_asset_return_pct,
    _format_signed_pct,
    send_daily_close_report_embed,
    send_daily_summary_embed,
    send_fill_embed,
    send_holding_change_embed,
)
from holdings_watch import mark_sell_notified


def _format_cumulative_return_text(total_eval: int, cash: int) -> str:
    return _format_signed_pct(_compute_cumulative_asset_return_pct(total_eval, cash))

logger = logging.getLogger(__name__)


class NotificationManager:
    """텔레그램/카카오 웹훅 공용 알림 전송기."""

    def __init__(self) -> None:
        self.enabled = bool(getattr(config, "ENABLE_NOTIFICATIONS", False))
        self.telegram_token = str(getattr(config, "TELEGRAM_BOT_TOKEN", "")).strip()
        self.telegram_chat_id = str(getattr(config, "TELEGRAM_CHAT_ID", "")).strip()
        self.kakao_webhook = str(getattr(config, "KAKAO_WEBHOOK_URL", "")).strip()

    def send_text(self, text: str) -> None:
        if not self.enabled:
            return
        msg = str(text or "").strip()
        if not msg:
            return
        self._send_telegram(msg)
        self._send_kakao(msg)

    def send_fill(
        self,
        *,
        side: str,
        code: str,
        name: str,
        qty: int,
        detail: str,
        price: int = 0,
        profit_pct: float | None = None,
    ) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        body = f"[{ts}] {side} 체결\n{name}({code}) {qty}주\n{detail}"
        self.send_text(body)
        payload = build_ai_briefing_payload(f"{name} {code}", side=side)
        briefing = str(payload.get("briefing") or "")
        news = payload.get("news") if isinstance(payload.get("news"), list) else []
        send_fill_embed(
            side=side,
            code=code,
            name=name,
            qty=qty,
            price=int(price or _extract_price(detail)),
            profit_pct=profit_pct if profit_pct is not None else _extract_profit_pct(detail),
            detail=detail,
            briefing=briefing,
            news_links=news,  # type: ignore[arg-type]
        )
        if side == "매도":
            mark_sell_notified(code)

    def send_holding_change(
        self,
        *,
        change_kind: str,
        code: str,
        name: str,
        old_qty: int,
        new_qty: int,
        delta_qty: int,
        detail: str = "",
    ) -> None:
        """주문 체결 알림 백업 — 계좌 동기화로 감지한 보유 수량 변화."""
        if not self.enabled:
            return
        kind_txt = {
            "partial_sell": "분할/부분 매도",
            "sold_out": "전량 매도",
            "qty_increase": "수량 증가",
            "new_holding": "신규 보유",
        }.get(change_kind, "보유 변동")
        ts = datetime.now().strftime("%H:%M:%S")
        body = (
            f"[{ts}] 보유 변동 · {kind_txt}\n"
            f"{name}({code}) {int(old_qty)}주 → {int(new_qty)}주 "
            f"({int(delta_qty):+d}주)\n"
            f"{detail or '계좌 동기화 감지'}"
        )
        self.send_text(body)
        send_holding_change_embed(
            change_kind=change_kind,
            code=code,
            name=name,
            old_qty=int(old_qty),
            new_qty=int(new_qty),
            delta_qty=int(delta_qty),
            detail=detail,
        )
        if change_kind in ("partial_sell", "sold_out"):
            mark_sell_notified(code)

    def send_daily_close_report(self, report: dict[str, Any]) -> None:
        """장 마감 AI 복기·시장·내일 전략 — 텔레그램/카카오/디스코드."""
        md = str(report.get("markdown") or "").strip()
        if not md:
            return
        if self.enabled:
            self.send_text(md[:3500])
        send_daily_close_report_embed(report)

    def send_daily_summary(
        self,
        *,
        tag: str,
        daily_eval_pnl: int,
        daily_eval_pnl_pct: float,
        total_return_pct: float | None = None,
        slot_count: int = 0,
        total_eval: int = 0,
        cash: int = 0,
        trade_count: int = 0,
        realized_pnl: int = 0,
        **kwargs: Any,
    ) -> None:
        from account import compute_realized_return_metrics

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        settlement = compute_realized_return_metrics(int(cash), int(total_eval))
        ret_txt = _format_signed_pct(float(settlement["return_rate"]))
        body = (
            f"[{tag}] 일일 정산 ({ts})\n"
            f"정산 수익률 {ret_txt} "
            f"(총자산 {int(settlement['total_assets']):,}원 · "
            f"손익 {int(settlement['profit_loss']):+,}원)\n"
            f"주식 {int(total_eval):,} + 예수금 {int(cash):,} · "
            f"당일 평가손익 {daily_eval_pnl:+,}원 ({daily_eval_pnl_pct:+.2f}%) · "
            f"보유 {slot_count}슬롯"
        )
        self.send_text(body)
        payload = build_ai_briefing_payload("국내 증시 장마감 브리핑", side=tag)
        briefing = str(payload.get("briefing") or "")
        news = payload.get("news") if isinstance(payload.get("news"), list) else []
        send_daily_summary_embed(
            tag=tag,
            daily_eval_pnl=daily_eval_pnl,
            daily_eval_pnl_pct=daily_eval_pnl_pct,
            total_return_pct=total_return_pct,
            slot_count=slot_count,
            total_eval=total_eval,
            cash=cash,
            trade_count=trade_count,
            realized_pnl=realized_pnl,
            briefing=briefing,
            news_links=news,  # type: ignore[arg-type]
            **kwargs,
        )

    def _send_telegram(self, text: str) -> None:
        if not self.telegram_token or not self.telegram_chat_id:
            return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload: dict[str, Any] = {"chat_id": self.telegram_chat_id, "text": text}
        try:
            requests.post(url, json=payload, timeout=8)
        except requests.RequestException as exc:
            logger.debug("텔레그램 알림 실패: %s", exc)

    def _send_kakao(self, text: str) -> None:
        if not self.kakao_webhook:
            return
        payload = {"text": text}
        try:
            requests.post(self.kakao_webhook, json=payload, timeout=8)
        except requests.RequestException as exc:
            logger.debug("카카오 알림 실패: %s", exc)


def _extract_price(detail: str) -> int:
    try:
        import re

        nums = re.findall(r"(\d[\d,]*)원", str(detail or ""))
        if nums:
            return int(nums[0].replace(",", ""))
    except Exception:
        pass
    return 0


def _extract_profit_pct(detail: str) -> float | None:
    try:
        import re

        m = re.search(r"([+-]?\d+(?:\.\d+)?)%", str(detail or ""))
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None
