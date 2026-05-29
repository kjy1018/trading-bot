from __future__ import annotations

import time
from datetime import datetime

import config
from discord_control import start_discord_control_bot
from notification_manager import NotificationManager
from scheduler import get_account_ui_snapshot, get_daily_stats, get_daily_trade_history


def main() -> None:
    token = str(getattr(config, "DISCORD_BOT_TOKEN", "")).strip()
    channel_id = str(getattr(config, "DISCORD_CHANNEL_ID", "")).strip()
    if not token or not channel_id:
        raise RuntimeError("DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID 가 비어 있습니다.")

    print("[1/4] 디스코드 지휘소 봇 기동...")
    start_discord_control_bot(token, channel_id)
    # 디스코드 게이트웨이 연결/채널 fetch 대기
    time.sleep(8)

    notifier = NotificationManager()
    notifier.enabled = True

    history = get_daily_trade_history()
    print("[2/4] 실제 체결 영수증(당일 최대 2건) 발송...")
    sent = 0
    for row in history[:2]:
        name = str(row.get("종목명") or row.get("name") or "-")
        code = str(row.get("종목코드") or row.get("code") or "-")
        pnl = int(row.get("수익금액") or row.get("pnl") or 0)
        pct_raw = str(row.get("수익률") or row.get("profit_pct") or "0")
        try:
            pct = float(str(pct_raw).replace("%", "").replace("+", ""))
        except Exception:
            pct = 0.0
        side = "매도"
        notifier.send_fill(
            side=side,
            code=code,
            name=name,
            qty=int(row.get("수량") or row.get("qty") or 0),
            price=int(row.get("매도가") or row.get("exit_price") or 0),
            profit_pct=pct,
            detail=f"실현손익 {pnl:+,}원 · 실제 모의투자 체결 영수증",
        )
        sent += 1
        time.sleep(1.0)
    if sent == 0:
        notifier.send_text("오늘 체결 영수증이 없어 요약 리포트만 발송합니다.")

    print("[3/4] 실제 당일 정산서 발송...")
    stats = get_daily_stats(force_refresh=True)
    snap = get_account_ui_snapshot()
    notifier.send_daily_summary(
        tag="장마감 직후(15:35) 자동 정산 리포트",
        daily_eval_pnl=int(snap.get("daily_eval_pnl") or 0),
        daily_eval_pnl_pct=float(snap.get("daily_eval_pnl_pct") or 0.0),
        total_return_pct=float(snap.get("total_return_pct") or 0.0),
        slot_count=int((snap.get("holdings") or {}).__len__()),
        total_eval=int(snap.get("total_eval") or 0),
        cash=int(snap.get("cash") or 0),
        trade_count=int(stats.get("trade_count") or 0),
        realized_pnl=int(stats.get("total_pnl") or 0),
    )
    notifier.send_text("실계좌(모의) 기준 체결/정산 데이터 연동 점검이 완료되었습니다.")

    print(f"[4/4] 완료: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
