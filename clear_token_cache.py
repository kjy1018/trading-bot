#!/usr/bin/env python3
"""한투 접근 토큰 캐시 파일을 찾아 삭제합니다."""

from auth import PROJECT_DIR, TOKEN_CACHE_NAMES, clear_token_cache, get_access_token


def main() -> None:
    print(f"프로젝트 경로: {PROJECT_DIR}")
    print("삭제 대상 패턴:", ", ".join(TOKEN_CACHE_NAMES))

    deleted = clear_token_cache()
    if deleted:
        print("삭제 완료:")
        for path in deleted:
            print(f"  - {path}")
    else:
        print("삭제할 토큰 캐시 파일이 없습니다.")

    print("\n새 접근 토큰 발급 중...")
    try:
        token = get_access_token(force_refresh=True)
        print(f"신규 토큰 발급 성공 (앞 12자): {token[:12]}...")
        print(f"저장 위치: {PROJECT_DIR / 'token.json'}")
    except Exception as exc:
        print(f"토큰 발급 실패: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
