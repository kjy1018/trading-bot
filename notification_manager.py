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
    send_daily_summary_embed,
    send_fill_embed,
)


def _format_cumulative_return_text(total_eval: int, cash: int) -> str:
    return _format_signed_pct(
        _compute_cumulative_asset_return_pct(total_eval, cash)
    )

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
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = (
            f"[{tag}] 무인 운용 요약 ({ts})\n"
            f"당일 평가손익 {daily_eval_pnl:+,}원 ({daily_eval_pnl_pct:+.2f}%) · "
            f"누적 자산 수익률(원금 1천만 대비) "
            f"{_format_cumulative_return_text(total_eval, cash)}\n"
            f"주식평가 {total_eval:,}원 · 예수금 {cash:,}원 · 보유 {slot_count}슬롯"
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
