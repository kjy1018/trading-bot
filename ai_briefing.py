from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from html import unescape
from typing import Any
from urllib.parse import quote_plus
from xml.etree import ElementTree

import requests

import config

logger = logging.getLogger(__name__)


def _clean(text: str) -> str:
    txt = unescape(str(text or ""))
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _fetch_google_news(query: str, top_k: int) -> list[dict[str, str]]:
    url = (
        "https://news.google.com/rss/search"
        f"?q={quote_plus(query)}+when:{int(getattr(config, 'AI_BRIEFING_NEWS_HOURS', 24))}h"
        "&hl=ko&gl=KR&ceid=KR:ko"
    )
    out: list[dict[str, str]] = []
    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.text)
        for item in root.findall(".//item")[:top_k]:
            out.append(
                {
                    "title": _clean(item.findtext("title", default="")),
                    "source": _clean(item.findtext("source", default="GoogleNews")),
                    "link": _clean(item.findtext("link", default="")),
                }
            )
    except Exception as exc:
        logger.debug("구글 뉴스 조회 실패(%s): %s", query, exc)
    return out


def _fetch_naver_news(query: str, top_k: int) -> list[dict[str, str]]:
    url = f"https://search.naver.com/search.naver?where=news&query={quote_plus(query)}"
    out: list[dict[str, str]] = []
    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        html = resp.text
        titles = re.findall(r'class="news_tit"[^>]*title="([^"]+)"', html)
        links = re.findall(r'class="news_tit"[^>]*href="([^"]+)"', html)
        for t, l in zip(titles[:top_k], links[:top_k]):
            out.append({"title": _clean(t), "source": "NaverNews", "link": _clean(l)})
    except Exception as exc:
        logger.debug("네이버 뉴스 조회 실패(%s): %s", query, exc)
    return out


def fetch_related_news(query: str, *, top_k: int = 3) -> list[dict[str, str]]:
    naver = _fetch_naver_news(query, top_k)
    google = _fetch_google_news(query, top_k)
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in [*naver, *google]:
        title = row.get("title", "")
        link = row.get("link", "")
        if not title or title in seen:
            continue
        if not link:
            continue
        seen.add(title)
        merged.append(
            {
                "title": _clean(title),
                "source": _clean(row.get("source", "")),
                "link": _clean(link),
            }
        )
        if len(merged) >= top_k:
            break
    return merged


def _gemini_summarize(symbol: str, side: str, news: list[dict[str, str]]) -> str | None:
    key = str(getattr(config, "GEMINI_API_KEY", "")).strip()
    if not key:
        return None
    model = str(getattr(config, "GEMINI_MODEL", "gemini-1.5-flash")).strip()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={key}"
    )
    prompt = {
        "symbol": symbol,
        "side": side,
        "news": news,
        "task": "3줄 요약: 등락 원인/향후 전망/지휘관 대응 권고",
    }
    body = {"contents": [{"parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}]}
    try:
        resp = requests.post(url, json=body, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        text = (
            (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [{}])[0]
            .get("text", "")
            .strip()
        )
        return _clean(text) if text else None
    except Exception as exc:
        logger.debug("Gemini 브리핑 실패: %s", exc)
        return None


def build_ai_briefing(symbol: str, *, side: str = "", top_k: int | None = None) -> str:
    if not bool(getattr(config, "ENABLE_AI_BRIEFING", True)):
        return "📌 등락 원인: 브리핑 비활성\n🔮 향후 전망: 브리핑 비활성\n⚠️ 지휘관 대응 권고: 브리핑 비활성"
    k = int(top_k or getattr(config, "AI_BRIEFING_TOP_K", 3))
    news = fetch_related_news(symbol, top_k=max(1, k))
    llm = _gemini_summarize(symbol, side, news)
    if llm:
        return llm
    heads = [n.get("title", "") for n in news if n.get("title")]
    head = heads[0] if heads else "관련 기사 부족"
    now = datetime.now().strftime("%H:%M")
    return (
        f"📌 등락 원인: {head}\n"
        f"🔮 향후 전망: 뉴스 모멘텀과 기술적 레벨 재확인 필요 ({now})\n"
        "⚠️ 지휘관 대응 권고: Cap 한도·마지노선 이탈 시 기계적 청산 유지"
    )


def build_ai_briefing_payload(
    symbol: str,
    *,
    side: str = "",
    top_k: int | None = None,
) -> dict[str, Any]:
    """
    브리핑 텍스트 + 뉴스 원문 링크 묶음 반환.
    실패 시에도 기본값으로 복구하여 메인 엔진 알림은 중단되지 않는다.
    """
    k = int(top_k or getattr(config, "AI_BRIEFING_TOP_K", 3))
    try:
        news = fetch_related_news(symbol, top_k=max(1, k))
    except Exception as exc:
        logger.debug("뉴스 수집 실패(%s): %s", symbol, exc)
        news = []
    try:
        briefing = build_ai_briefing(symbol, side=side, top_k=k)
    except Exception as exc:
        logger.debug("브리핑 생성 실패(%s): %s", symbol, exc)
        briefing = (
            "📌 등락 원인: 브리핑 생성 실패\n"
            "🔮 향후 전망: 데이터 수신 재시도 필요\n"
            "⚠️ 지휘관 대응 권고: 기존 리스크 규칙 유지"
        )
    return {"briefing": briefing, "news": news}
