"""한국투자증권 OAuth2 접근 토큰 발급·파일 캐시."""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from config import APP_KEY, APP_SECRET, BASE_URL
from kis_rate import kis_request

TOKEN_PATH = "/oauth2/tokenP"
PROJECT_DIR = Path(__file__).resolve().parent

# 삭제 대상 캐시 파일명 (프로젝트 루트)
TOKEN_CACHE_NAMES = (
    "token.dat",
    "token.txt",
    "token.json",
    "access_token.json",
    "access_token.txt",
    "kis_token.json",
    "kis_token.dat",
)


def clear_token_cache() -> list[str]:
    """프로젝트 폴더 내 토큰 캐시 파일을 모두 삭제합니다."""
    deleted: list[str] = []
    for name in TOKEN_CACHE_NAMES:
        path = PROJECT_DIR / name
        if path.is_file():
            path.unlink()
            deleted.append(str(path))
    return deleted


def _token_cache_path() -> Path:
    return PROJECT_DIR / "token.json"


def _load_cached_token() -> str | None:
    path = _token_cache_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = data.get("access_token")
        expires_at = float(data.get("expires_at", 0))
        if token and time.time() < expires_at - 120:
            return str(token)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def _save_cached_token(access_token: str, expires_in: int) -> None:
    expires_at = time.time() + max(int(expires_in or 0), 3600)
    _token_cache_path().write_text(
        json.dumps(
            {"access_token": access_token, "expires_at": expires_at},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _request_new_token() -> str:
    url = f"{BASE_URL}{TOKEN_PATH}"
    headers = {"Content-Type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
    }

    with kis_request():
        response = requests.post(url, headers=headers, json=body, timeout=30)
        response.raise_for_status()
        data = response.json()
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError(f"토큰 발급 실패: {data}")

    expires_in = int(data.get("expires_in") or 86400)
    _save_cached_token(str(access_token), expires_in)
    return str(access_token)


def invalidate_access_token() -> None:
    """캐시 파일 삭제 (만료·403 시 호출)."""
    clear_token_cache()


def get_access_token(force_refresh: bool = False) -> str:
    """
    접근 토큰 반환. 유효한 캐시가 있으면 재사용, 없거나 force_refresh 시 신규 발급.
    """
    if force_refresh:
        invalidate_access_token()
    else:
        cached = _load_cached_token()
        if cached:
            return cached

    return _request_new_token()
