"""한국투자증권 Open API 공통 요청 헤더."""

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
)


def build_kis_headers(
    access_token: str,
    app_key: str,
    app_secret: str,
    tr_id: str,
    tr_cont: str = "",
) -> dict:
    """KIS API 필수 헤더 (appkey, appsecret, Bearer 토큰, tr_id 등)."""
    return {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
        "User-Agent": DEFAULT_USER_AGENT,
        "authorization": f"Bearer {access_token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
        "tr_cont": tr_cont,
    }
