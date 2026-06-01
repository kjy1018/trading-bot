"""한국투자증권 OAuth2 접근 토큰 발급·파일 캐시."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

from config import APP_KEY, APP_SECRET, BASE_URL
from kis_rate import kis_request

logger = logging.getLogger(__name__)

TOKEN_PATH = "/oauth2/tokenP"
PROJECT_DIR = Path(__file__).resolve().parent
TOKEN_ISSUE_MIN_INTERVAL_SEC = 65  # EGW00133: 1분당 1회

# 접근토큰 발급 속도 제한 (재시도·캐시삭제로 악화되면 안 됨)
TOKEN_RATE_LIMIT_CODES = frozenset({"EGW00133"})

TOKEN_CACHE_NAMES = (
    "token.dat",
    "token.txt",
    "token.json",
    "access_token.json",
    "access_token.txt",
    "kis_token.json",
    "kis_token.dat",
)

_last_token_issue_monotonic: float = 0.0


class KISTokenForbiddenError(RuntimeError):
    """토큰 발급 인증 실패 (키/URL 오류 등)."""


class KISTokenRateLimitError(RuntimeError):
    """토큰 발급 속도 제한 (EGW00133 — 1분당 1회)."""


def is_forbidden_status(status_code: int | None) -> bool:
    return int(status_code or 0) == 403


def clear_token_cache() -> list[str]:
    """프로젝트 폴더 내 토큰 캐시 파일을 모두 삭제합니다."""
    deleted: list[str] = []
    for name in TOKEN_CACHE_NAMES:
        path = PROJECT_DIR / name
        if path.is_file():
            try:
                path.unlink()
                deleted.append(str(path))
            except OSError as exc:
                logger.warning("토큰 캐시 삭제 실패 %s: %s", path, exc)
    return deleted


def _token_cache_path() -> Path:
    return PROJECT_DIR / "token.json"


def _load_cached_token(*, ignore_expiry: bool = False) -> str | None:
    path = _token_cache_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = str(data.get("access_token") or "").strip()
        if not token:
            return None
        if ignore_expiry:
            return token
        expires_at = float(data.get("expires_at", 0))
        if time.time() < expires_at - 120:
            return token
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def _save_cached_token(access_token: str, expires_in: int) -> None:
    path = _token_cache_path()
    expires_at = time.time() + max(int(expires_in or 0), 3600)
    path.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "expires_at": expires_at,
                "saved_at": time.time(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _parse_token_error_response(response: requests.Response) -> tuple[str, str]:
    try:
        data = response.json()
        msg_cd = str(
            data.get("error_code") or data.get("msg_cd") or data.get("code") or ""
        ).strip()
        msg = str(
            data.get("error_description")
            or data.get("msg1")
            or data.get("message")
            or data
        ).strip()
        detail = f"[{msg_cd}] {msg}" if msg_cd else msg
        return msg_cd, detail
    except Exception:
        text = (response.text or "")[:300]
        return "", text


def _is_token_issue_rate_limit(response: requests.Response) -> bool:
    msg_cd, detail = _parse_token_error_response(response)
    if msg_cd in TOKEN_RATE_LIMIT_CODES:
        return True
    lowered = detail.lower()
    return "1분당" in detail or "egw00133" in lowered


def _ensure_api_credentials() -> None:
    if not APP_KEY or not APP_SECRET:
        raise RuntimeError(
            "APP_KEY / APP_SECRET 가 비어 있습니다. "
            "프로젝트 루트 .env 파일을 확인하거나 환경변수를 설정하세요."
        )


def _wait_token_issue_cooldown() -> None:
    global _last_token_issue_monotonic
    elapsed = time.monotonic() - _last_token_issue_monotonic
    if elapsed >= TOKEN_ISSUE_MIN_INTERVAL_SEC:
        return
    wait_sec = TOKEN_ISSUE_MIN_INTERVAL_SEC - elapsed
    logger.info("토큰 발급 간격 대기 %.0f초 (1분당 1회 제한)", wait_sec)
    time.sleep(wait_sec)


def _request_new_token(*, allow_rate_limit_wait: bool = True) -> str:
    global _last_token_issue_monotonic
    _ensure_api_credentials()
    _wait_token_issue_cooldown()

    url = f"{BASE_URL}{TOKEN_PATH}"
    headers = {"Content-Type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
    }

    with kis_request():
        response = requests.post(url, headers=headers, json=body, timeout=30)

    _last_token_issue_monotonic = time.monotonic()

    if is_forbidden_status(response.status_code):
        msg_cd, detail = _parse_token_error_response(response)
        if _is_token_issue_rate_limit(response):
            cached = _load_cached_token()
            if cached:
                logger.warning(
                    "토큰 발급 제한 %s — 기존 캐시 토큰 재사용", detail
                )
                return cached
            if allow_rate_limit_wait:
                logger.warning(
                    "토큰 발급 제한 %s — %ds 후 1회 재시도",
                    detail,
                    TOKEN_ISSUE_MIN_INTERVAL_SEC,
                )
                time.sleep(TOKEN_ISSUE_MIN_INTERVAL_SEC)
                return _request_new_token(allow_rate_limit_wait=False)
            raise KISTokenRateLimitError(
                f"토큰 발급 속도 제한: {detail}. "
                f"{TOKEN_ISSUE_MIN_INTERVAL_SEC}초 후 다시 시도하세요."
            )
        raise KISTokenForbiddenError(
            f"토큰 발급 403 Forbidden: {detail}\n"
            "확인: ① .env APP_KEY/APP_SECRET ② 모의투자 키 + openapivts URL"
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        _, detail = _parse_token_error_response(response)
        raise RuntimeError(
            f"토큰 발급 HTTP {response.status_code}: {detail}"
        ) from exc

    data = response.json()
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError(f"토큰 발급 실패: {data}")

    expires_in = int(data.get("expires_in") or 86400)
    _save_cached_token(str(access_token), expires_in)
    return str(access_token)


def invalidate_access_token() -> None:
    """캐시 파일 삭제 (만료·인증 실패 시). 속도 제한(EGW00133)에는 사용하지 마세요."""
    deleted = clear_token_cache()
    if deleted:
        logger.info("토큰 캐시 삭제: %s", deleted)


def get_access_token(force_refresh: bool = False) -> str:
    """
    접근 토큰 반환.
    - 유효한 token.json 있으면 재사용
    - force_refresh=True: 신규 발급 시도 (성공 시 덮어쓰기, EGW00133이면 캐시 유지)
    """
    if not force_refresh:
        cached = _load_cached_token()
        if cached:
            return cached

    cached_before = _load_cached_token()
    try:
        return _request_new_token()
    except KISTokenRateLimitError:
        if cached_before:
            logger.warning("발급 제한 — 직전 캐시 토큰으로 폴백")
            return cached_before
        raise


def refresh_access_token_after_forbidden() -> str:
    """
    API 호출 403(토큰 만료 등) 시 — 캐시 삭제 없이 신규 발급 시도.
    EGW00133이면 기존 캐시를 우선 사용합니다.
    """
    cached = _load_cached_token()
    try:
        return _request_new_token()
    except KISTokenRateLimitError:
        if cached:
            return cached
        raise
    except KISTokenForbiddenError:
        invalidate_access_token()
        return _request_new_token()


if __name__ == "__main__":
    print("한투 접근 토큰 발급 테스트")
    print(f"프로젝트: {PROJECT_DIR}")

    cached = _load_cached_token()
    if cached:
        print("유효한 token.json 캐시가 있습니다 — 재발급 없이 재사용합니다.")
        print(f"토큰 (앞 12자): {cached[:12]}...")
        raise SystemExit(0)

    print("캐시 없음 — 신규 발급 시도 (1분당 1회 제한 주의)")
    try:
        tok = get_access_token()
        print(f"발급 성공 (앞 12자): {tok[:12]}...")
        print(f"저장: {_token_cache_path()}")
    except KISTokenRateLimitError as e:
        print(f"발급 제한: {e}")
        print("→ 65초 후 python auth.py 를 다시 실행하세요.")
        raise SystemExit(2) from e
    except Exception as e:
        print(f"발급 실패: {e}")
        raise SystemExit(1) from e
