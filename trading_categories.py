"""
사용자 고정 매매 카테고리 — long_term / swing / day_trading

이 3가지 명칭만 포트폴리오·매수/매도 분기의 기준값으로 사용한다.
(구버전 scalping 등은 읽기 시에만 day_trading 으로 정규화)
"""

from __future__ import annotations

# === 사용자 설정 — 변경 금지 ===
LONG_TERM = "long_term"
SWING = "swing"
DAY_TRADING = "day_trading"

TRADING_CATEGORIES: tuple[str, str, str] = (LONG_TERM, SWING, DAY_TRADING)

CATEGORY_LABEL_KO: dict[str, str] = {
    LONG_TERM: "장투",
    SWING: "스윙",
    DAY_TRADING: "단타",
}

LABEL_TO_CATEGORY: dict[str, str] = {
    "장투": LONG_TERM,
    "스윙": SWING,
    "단타": DAY_TRADING,
}

_LEGACY_CATEGORY_ALIASES: dict[str, str] = {
    "scalping": DAY_TRADING,
    "scalp": DAY_TRADING,
    "danta": DAY_TRADING,
    "short": DAY_TRADING,
    "daytrade": DAY_TRADING,
    "day-trading": DAY_TRADING,
    "longterm": LONG_TERM,
    "long": LONG_TERM,
    "장투": LONG_TERM,
    "스윙": SWING,
    "단타": DAY_TRADING,
}

# slot_uid 레거시 (AI가 추가했던 _1 접미 등)
_LEGACY_SLOT_UID_MAP: dict[str, str] = {
    "long_term_1": "1",
    "long_term": "1",
    "swing_1": "2",
    "swing_2": "3",
    "swing": "2",
    "scalping_1": "4",
    "scalping_2": "5",
    "day_trading_1": "4",
    "day_trading_2": "5",
    "day_trading": "4",
}


def normalize_trading_category(raw: object, *, fallback: str = SWING) -> str:
    """모드·슬롯 타입 → long_term | swing | day_trading."""
    text = str(raw or "").strip()
    lowered = text.lower()
    if lowered in TRADING_CATEGORIES:
        return lowered
    if lowered in _LEGACY_CATEGORY_ALIASES:
        return _LEGACY_CATEGORY_ALIASES[lowered]
    if text in LABEL_TO_CATEGORY:
        return LABEL_TO_CATEGORY[text]
    fb = str(fallback or SWING).strip().lower()
    if fb in TRADING_CATEGORIES:
        return fb
    if fb in _LEGACY_CATEGORY_ALIASES:
        return _LEGACY_CATEGORY_ALIASES[fb]
    return SWING


def category_matches(slot_type: object, trading_mode: object) -> bool:
    return normalize_trading_category(slot_type) == normalize_trading_category(trading_mode)


def label_for_category(category: object, *, fallback: str = "장투") -> str:
    cat = normalize_trading_category(category, fallback=LONG_TERM)
    return CATEGORY_LABEL_KO.get(cat, fallback)


def migrate_legacy_slot_uid(raw: object) -> str | None:
    """구 slot_id → slot_uid(display_idx 문자열 '1'~'5')."""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.isdigit() and text in {"1", "2", "3", "4", "5"}:
        return text
    return _LEGACY_SLOT_UID_MAP.get(text) or _LEGACY_SLOT_UID_MAP.get(text.lower())


def is_trading_category(raw: object) -> bool:
    try:
        return normalize_trading_category(raw) in TRADING_CATEGORIES
    except Exception:
        return False
