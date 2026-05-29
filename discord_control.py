from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import discord
from discord import app_commands

import config

logger = logging.getLogger(__name__)

# 디스코드 영수증 — KIS 보유종목 수익률 미사용, 원금 대비 누적 자산 수익률만 표시
_PRINCIPAL_WON = int(getattr(config, "ACCOUNT_INITIAL_PRINCIPAL", 10_000_000))

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_client: discord.Client | None = None
_tree: app_commands.CommandTree | None = None
_channel_id: int | None = None
_started = False


def start_discord_control_bot(token: str, channel_id: str | int) -> None:
    global _started, _channel_id, _thread
    if _started:
        return
    tok = str(token or "").strip()
    if not tok:
        return
    try:
        _channel_id = int(str(channel_id))
    except (TypeError, ValueError):
        logger.warning("디스코드 채널 ID가 올바르지 않습니다: %s", channel_id)
        return
    _started = True
    _thread = threading.Thread(
        target=_run_discord_thread,
        args=(tok,),
        name="discord-control-bot",
        daemon=True,
    )
    _thread.start()


async def _sync_command_tree(tree: app_commands.CommandTree) -> None:
    """app_commands 트리를 Discord API에 등록(전역 sync)."""
    try:
        synced = await tree.sync()
        names = [cmd.name for cmd in synced]
        logger.info("디스코드 슬래시 명령 %d개 동기화 완료: %s", len(names), names)
    except discord.HTTPException as exc:
        logger.warning("디스코드 slash sync HTTP 오류: %s", exc)
    except Exception as exc:
        logger.warning("디스코드 slash sync 실패: %s", exc)


def _register_slash_commands(tree: app_commands.CommandTree) -> None:
    @tree.command(name="status", description="현재 5슬롯 상태를 조회합니다.")
    async def status_cmd(interaction: discord.Interaction) -> None:
        from scheduler import get_account_ui_snapshot, get_positions_snapshot

        positions = get_positions_snapshot()[:5]
        account = get_account_ui_snapshot()
        lines = []
        for idx, p in enumerate(positions, start=1):
            lines.append(
                f"{idx}. {p.get('name', '-')}"
                f"({p.get('code', '-')}) {float(p.get('profit_pct', 0)):+.2f}%"
            )
        if not lines:
            lines.append("보유 슬롯이 없습니다.")
        txt = (
            "현재 슬롯 상태\n"
            + "\n".join(lines)
            + "\n"
            + f"총평가 {int(account.get('total_eval', 0)):,}원 · "
            + f"현금 {int(account.get('cash', 0)):,}원"
        )
        await interaction.response.send_message(txt, ephemeral=True)

    @tree.command(name="stop", description="신규 매수 루프를 일시 정지합니다.")
    async def stop_cmd(interaction: discord.Interaction) -> None:
        from scheduler import set_buy_pause

        out = set_buy_pause(True, source="discord")
        await interaction.response.send_message(
            f"신규 매수 일시정지: {out.get('paused')}", ephemeral=True
        )

    @tree.command(name="flat", description="보유 종목을 즉시 전량 청산합니다.")
    async def flat_cmd(interaction: discord.Interaction) -> None:
        from scheduler import emergency_liquidate_all

        result = emergency_liquidate_all()
        await interaction.response.send_message(
            f"긴급 청산 실행: {result.get('sold_count', 0)}건", ephemeral=True
        )

    @tree.command(
        name="요약",
        description="현재 시점 계좌 요약(당일 평가손익·총 수익률) 영수증을 즉시 전송합니다.",
    )
    async def instant_summary_cmd(interaction: discord.Interaction) -> None:
        from scheduler import fetch_instant_daily_summary_payload

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            payload = await asyncio.to_thread(
                fetch_instant_daily_summary_payload,
                tag="장마감 직후",
            )
            channel = interaction.channel
            if channel is None or not isinstance(channel, discord.abc.Messageable):
                await interaction.followup.send(
                    "메시지를 보낼 수 없는 채널입니다.", ephemeral=True
                )
                return
            embed = make_daily_summary_embed(**payload)
            await channel.send(embed=embed)
            await interaction.followup.send(
                "요약 영수증을 채널에 전송했습니다.", ephemeral=True
            )
        except Exception as exc:
            logger.exception("/요약 즉시 조회 실패: %s", exc)
            await interaction.followup.send(
                f"요약 생성 실패: {exc}", ephemeral=True
            )


def _run_discord_thread(token: str) -> None:
    global _loop, _client, _tree
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    _client = client
    _tree = tree

    _register_slash_commands(tree)

    @client.event
    async def on_ready() -> None:
        await _sync_command_tree(tree)
        logger.info("디스코드 무선 지휘소 연결 완료: %s", client.user)

    @tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.warning("슬래시 명령 오류: %s", error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"명령 처리 오류: {error}", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"명령 처리 오류: {error}", ephemeral=True
                )
        except Exception:
            pass

    _loop.run_until_complete(client.start(token))


async def _send_embed_async(
    *,
    title: str,
    description: str,
    color: int,
    fields: list[tuple[str, str, bool]] | None = None,
) -> None:
    if _client is None or _channel_id is None:
        return
    channel = _client.get_channel(_channel_id)
    if channel is None:
        try:
            channel = await _client.fetch_channel(_channel_id)
        except Exception:
            return
    if channel is None:
        return
    embed = discord.Embed(title=title, description=description, color=color)
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)
    await channel.send(embed=embed)


async def send_daily_summary_embed_to_channel(
    channel: discord.abc.Messageable,
    **kwargs: Any,
) -> None:
    """지정 채널에 일일 요약 임베드 전송 (/요약 등)."""
    embed = make_daily_summary_embed(**kwargs)
    await channel.send(embed=embed)


def _submit_embed(
    *,
    title: str,
    description: str,
    color: int,
    fields: list[tuple[str, str, bool]] | None = None,
) -> None:
    if _loop is None or _client is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _send_embed_async(
                title=title,
                description=description,
                color=color,
                fields=fields,
            ),
            _loop,
        )
    except Exception as exc:
        logger.debug("디스코드 전송 실패: %s", exc)


def send_fill_embed(
    *,
    side: str,
    code: str,
    name: str,
    qty: int,
    price: int,
    profit_pct: float | None = None,
    detail: str = "",
    briefing: str = "",
    news_links: list[dict[str, str]] | None = None,
) -> None:
    pnl_txt = "-" if profit_pct is None else f"{profit_pct:+.2f}%"
    _submit_embed(
        title=f"{side} 체결",
        description=f"{name}({code})",
        color=0x1ABC9C if side in {"매수", "추가매수"} else 0xE74C3C,
        fields=[
            ("수량", f"{int(qty)}주", True),
            ("체결가", f"{int(price):,}원", True),
            ("현재 수익률", pnl_txt, True),
            ("비고", detail or "-", False),
            ("AI 참모 브리핑", briefing or "브리핑 대기", False),
            ("📢 관련 핵심 뉴스 출처", _format_news_links(news_links), False),
        ],
    )


def _pnl_embed_color(daily_pnl: int) -> int:
    if daily_pnl > 0:
        return 0xE74C3C
    if daily_pnl < 0:
        return 0x3498DB
    return 0x5865F2


def _format_signed_pct(value: float) -> str:
    """양수 + / 음수 - 부호를 명시한 % 문자열."""
    pct = float(value)
    if pct > 0:
        return f"+{pct:.2f}%"
    if pct < 0:
        return f"{pct:.2f}%"
    return "+0.00%"


def _compute_cumulative_asset_return_pct(
    total_eval: int,
    cash: int,
    *,
    principal: int | None = None,
) -> float:
    """
    누적 자산 수익률(원금 대비) — KIS 보유종목 수익률 미사용.
    ((총 평가금액 + 예수금) - 원금) / 원금 * 100
    """
    base = int(principal if principal is not None else _PRINCIPAL_WON)
    if base <= 0:
        return 0.0
    total_assets = int(total_eval) + int(cash)
    return round((total_assets - base) / base * 100.0, 2)


def _format_daily_summary_description(
    *,
    daily_eval_pnl: int,
    daily_eval_pnl_pct: float,
    cumulative_return_pct: float,
) -> str:
    return (
        f"📊 **당일 평가손익** `{int(daily_eval_pnl):+,}원` "
        f"(`{_format_signed_pct(daily_eval_pnl_pct)}`)\n"
        f"💰 **누적 자산 수익률(원금 1천만 대비)** "
        f"`{_format_signed_pct(cumulative_return_pct)}`"
    )


def make_daily_summary_embed(
    *,
    tag: str,
    daily_eval_pnl: int,
    daily_eval_pnl_pct: float,
    slot_count: int,
    total_eval: int,
    cash: int,
    stock_eval: int | None = None,
    account_total_eval: int = 0,
    bot_operating_seed: int = 0,
    bot_deployed_won: int = 0,
    bot_market_value_won: int = 0,
    trade_count: int = 0,
    realized_pnl: int = 0,
    briefing: str = "",
    news_links: list[dict[str, str]] | None = None,
    total_return_pct: float | None = None,
) -> discord.Embed:
    # API·스냅샷의 total_return_pct는 무시하고, 임베드에서 원금 대비 수익률만 재계산
    _ = total_return_pct
    stock_eval_amt = int(stock_eval if stock_eval is not None else total_eval)
    cash_amt = int(cash)
    cumulative_return_pct = _compute_cumulative_asset_return_pct(
        stock_eval_amt, cash_amt
    )
    total_assets = stock_eval_amt + cash_amt
    seed = int(bot_operating_seed or getattr(config, "ACCOUNT_TOTAL_SEED", 7_500_000))
    deployed = int(bot_deployed_won)
    bot_mv = int(bot_market_value_won)
    seed_note = (
        f"실투입 {deployed:,}원 / 시드 {seed:,}원"
        if seed > 0
        else f"실투입 {deployed:,}원"
    )
    if seed > 0 and deployed > 0:
        use_pct = round(deployed / seed * 100.0, 1)
        seed_note += f" ({use_pct}%)"

    footnote = f"실현손익 {int(realized_pnl):+,}원 · 당일 매매 {int(trade_count)}회"
    embed = discord.Embed(
        title=f"{tag} · 계좌 영수증",
        description=_format_daily_summary_description(
            daily_eval_pnl=daily_eval_pnl,
            daily_eval_pnl_pct=daily_eval_pnl_pct,
            cumulative_return_pct=cumulative_return_pct,
        ),
        color=_pnl_embed_color(daily_eval_pnl),
    )
    embed.add_field(
        name="주식 평가금액 (KIS)",
        value=f"{stock_eval_amt:,}원",
        inline=True,
    )
    embed.add_field(
        name="예수금 (KIS)",
        value=f"{cash_amt:,}원",
        inline=True,
    )
    embed.add_field(
        name="합계 (주식+예수금)",
        value=f"{total_assets:,}원",
        inline=True,
    )
    embed.add_field(
        name="봇 운용 시드 (설정)",
        value=f"{seed:,}원",
        inline=True,
    )
    embed.add_field(
        name="슬롯 실투입 (봇)",
        value=seed_note,
        inline=True,
    )
    embed.add_field(
        name="슬롯 시가총액 (봇)",
        value=f"{bot_mv:,}원 · {int(slot_count)}슬롯",
        inline=True,
    )
    if account_total_eval > 0 and account_total_eval != total_assets:
        embed.add_field(
            name="참고 · KIS 계좌총평가",
            value=(
                f"tot_evlu_amt {account_total_eval:,}원 "
                f"(계좌 전체·봇 합계와 다를 수 있음)"
            ),
            inline=False,
        )
    embed.add_field(name="참고", value=footnote, inline=False)
    embed.add_field(
        name="AI 참모 브리핑", value=briefing or "브리핑 대기", inline=False
    )
    embed.add_field(
        name="📢 관련 핵심 뉴스 출처",
        value=_format_news_links(news_links),
        inline=False,
    )
    return embed


def send_daily_summary_embed(
    *,
    tag: str,
    daily_eval_pnl: int,
    daily_eval_pnl_pct: float,
    slot_count: int,
    total_eval: int,
    cash: int,
    trade_count: int = 0,
    realized_pnl: int = 0,
    briefing: str = "",
    news_links: list[dict[str, str]] | None = None,
    total_return_pct: float | None = None,
    **kwargs: Any,
) -> None:
    embed = make_daily_summary_embed(
        tag=tag,
        daily_eval_pnl=daily_eval_pnl,
        daily_eval_pnl_pct=daily_eval_pnl_pct,
        slot_count=slot_count,
        total_eval=total_eval,
        cash=cash,
        trade_count=trade_count,
        realized_pnl=realized_pnl,
        briefing=briefing,
        news_links=news_links,
        total_return_pct=total_return_pct,
        **kwargs,
    )
    if _loop is None or _client is None or _channel_id is None:
        return

    async def _send() -> None:
        channel = _client.get_channel(_channel_id)
        if channel is None:
            try:
                channel = await _client.fetch_channel(_channel_id)
            except Exception:
                return
        if channel is None:
            return
        await channel.send(embed=embed)

    try:
        asyncio.run_coroutine_threadsafe(_send(), _loop)
    except Exception as exc:
        logger.debug("디스코드 일일 요약 전송 실패: %s", exc)


def _short_title(title: str, max_len: int = 15) -> str:
    t = str(title or "").strip()
    if len(t) <= max_len:
        return t
    return t[:max_len] + "..."


def _format_news_links(news_links: list[dict[str, str]] | None) -> str:
    try:
        rows = news_links or []
        out: list[str] = []
        for row in rows[:3]:
            title = _short_title(str(row.get("title") or "뉴스"))
            link = str(row.get("link") or "").strip()
            if not link:
                continue
            out.append(f"🔗 [{title}]({link})")
        return "\n".join(out) if out else "관련 뉴스 링크 없음"
    except Exception:
        return "관련 뉴스 링크 없음"
