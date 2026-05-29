"""
국내 주식 6자리 코드 → 한글 종목명 (UI·포지션 표시용).

우선순위: 유효 한글 name → 런타임 유니버스 캐시 → 내장 마스터 → KIS 현재가 API
"""

from __future__ import annotations

import re
import threading
from typing import Any

_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")

# 주요 코스피·코스닥 (API 미반환·코드=이름 저장 시 UI 폴백)
_KRX_NAME_MASTER: dict[str, str] = {
    "000270": "기아",
    "000660": "SK하이닉스",
    "000720": "현대건설",
    "000810": "삼성화재",
    "001040": "CJ",
    "003550": "LG",
    "003670": "포스코퓨처엠",
    "005380": "현대차",
    "005490": "POSCO홀딩스",
    "005930": "삼성전자",
    "006400": "삼성SDI",
    "006800": "미래에셋증권",
    "009150": "삼성전기",
    "009540": "HD한국조선해양",
    "010120": "LS ELECTRIC",
    "010130": "고려아연",
    "010140": "삼성중공업",
    "011200": "HMM",
    "012330": "현대모비스",
    "015760": "한국전력",
    "017670": "SK텔레콤",
    "018260": "삼성에스디에스",
    "028260": "삼성물산",
    "030200": "KT",
    "032830": "삼성생명",
    "033780": "KT&G",
    "034020": "두산에너빌리티",
    "035420": "NAVER",
    "035720": "카카오",
    "036570": "엔씨소프트",
    "042660": "한화오션",
    "047050": "포스코인터내셔널",
    "051910": "LG화학",
    "055550": "신한지주",
    "064350": "현대로템",
    "066570": "LG전자",
    "068270": "셀트리온",
    "086790": "하나금융지주",
    "096770": "SK이노베이션",
    "105560": "KB금융",
    "138040": "메리츠금융지주",
    "207940": "삼성바이오로직스",
    "247540": "에코프로비엠",
    "259960": "크래프톤",
    "272210": "한화시스템",
    "316140": "우리금융지주",
    "323410": "카카오뱅크",
    "352820": "하이브",
    "373220": "LG에너지솔루션",
    "004800": "효성",
    "004020": "현대제철",
    "011070": "LG이노텍",
    "024110": "기업은행",
    "000100": "유한양행",
    "003490": "대한항공",
    "009830": "한화솔루션",
    "010950": "S-Oil",
    "011790": "SKC",
    "012450": "한화에어로스페이스",
    "021240": "코웨이",
    "028300": "HLB",
    "034730": "SK",
    "042700": "한미반도체",
    "051900": "LG생활건강",
    "086520": "에코프로",
    "090430": "아모레퍼시픽",
    "091990": "셀트리온헬스케어",
    "096530": "씨젠",
    "122870": "와이지엔터테인먼트",
    "161390": "한국타이어앤테크놀로지",
    "180640": "한진칼",
    "196170": "알테오젠",
    "214150": "클래시스",
    "226950": "올릭스",
    "263750": "펄어비스",
    "293490": "카카오게임즈",
    "298380": "에이비엘바이오",
    "329180": "HD현대중공업",
    "357780": "솔브레인",
    "403870": "HPSP",
    "439260": "대한조선",
    "454910": "두산로보틱스",
}

_runtime_cache: dict[str, str] = {}
_cache_lock = threading.Lock()
_api_fail_until: dict[str, float] = {}


def normalize_code(code: str | int | None) -> str:
    text = str(code or "").strip()
    digits = "".join(c for c in text if c.isdigit())
    if len(digits) < 6:
        return digits.zfill(6) if digits else ""
    return digits[-6:]


def is_valid_korean_name(name: str | None, code: str) -> bool:
    """코드와 동일한 숫자열이 아닌 한글 종목명."""
    text = str(name or "").strip()
    if not text:
        return False
    norm = normalize_code(code)
    if text == norm or text == code:
        return False
    if text.isdigit() and len(text) <= 6:
        return False
    return bool(_HANGUL_RE.search(text))


def register_universe_names(stocks: list[dict[str, Any]] | None) -> None:
    """유니버스 스캔 결과에서 코드→이름 캐시."""
    if not stocks:
        return
    with _cache_lock:
        for row in stocks:
            code = normalize_code(row.get("code"))
            if len(code) != 6:
                continue
            name = str(row.get("name") or "").strip()
            if is_valid_korean_name(name, code):
                _runtime_cache[code] = name


def lookup_master(code: str) -> str | None:
    code = normalize_code(code)
    if len(code) != 6:
        return None
    with _cache_lock:
        hit = _runtime_cache.get(code) or _KRX_NAME_MASTER.get(code)
    return hit


def resolve_stock_name(
    code: str,
    raw_name: str | None = None,
    *,
    access_token: str | None = None,
    allow_api: bool = True,
) -> str:
    """한글 종목명 (표시·저장용)."""
    norm = normalize_code(code)
    if len(norm) != 6:
        return str(raw_name or code or "").strip() or "-"

    raw = str(raw_name or "").strip()
    if is_valid_korean_name(raw, norm):
        with _cache_lock:
            _runtime_cache[norm] = raw
        return raw

    cached = lookup_master(norm)
    if cached:
        return cached

    if allow_api and access_token:
        import time

        now = time.time()
        with _cache_lock:
            skip_until = _api_fail_until.get(norm, 0.0)
        if now >= skip_until:
            try:
                import config
                from stock import get_current_price

                quote = get_current_price(
                    access_token, norm, config.APP_KEY, config.APP_SECRET
                )
                api_name = str(quote.get("name") or "").strip()
                if is_valid_korean_name(api_name, norm):
                    with _cache_lock:
                        _runtime_cache[norm] = api_name
                    return api_name
            except Exception:
                with _cache_lock:
                    _api_fail_until[norm] = now + 120.0

    return cached or raw or norm


def is_code_only_display(text: str | None, code: str) -> bool:
    """표시 문자열이 종목코드만 노출되는지."""
    norm = normalize_code(code)
    raw = str(text or "").strip()
    if not raw:
        return True
    if raw == norm:
        return True
    if raw.isdigit() and len(raw) <= 6:
        return True
    if norm in raw and not _HANGUL_RE.search(raw):
        return True
    return False


def format_stock_label(
    code: str,
    name: str | None = None,
    *,
    access_token: str | None = None,
) -> str:
    """UI용 — 예: 셀트리온 (068270)"""
    norm = normalize_code(code)
    if len(norm) != 6:
        return str(name or code or "-")
    resolved = resolve_stock_name(norm, name, access_token=access_token)
    if is_valid_korean_name(resolved, norm):
        return f"{resolved} ({norm})"
    master = lookup_master(norm)
    if master:
        return f"{master} ({norm})"
    return norm or str(code)


def enrich_position(
    pos: dict[str, Any],
    *,
    access_token: str | None = None,
    allow_api: bool = True,
) -> dict[str, Any]:
    """포지션 dict에 name·display_name 보강."""
    out = dict(pos)
    code = normalize_code(out.get("code"))
    if len(code) != 6:
        return out
    out["code"] = code
    resolved = resolve_stock_name(
        code, out.get("name"), access_token=access_token, allow_api=allow_api
    )
    out["name"] = resolved
    label = format_stock_label(code, resolved, access_token=access_token)
    if is_code_only_display(label, code):
        master = lookup_master(code)
        if master:
            label = f"{master} ({code})"
    out["display_name"] = label
    return out
