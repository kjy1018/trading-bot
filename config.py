import os
from pathlib import Path

from dotenv import load_dotenv

# 프로젝트 루트 .env 로드 (Streamlit/scheduler 직접 실행 시 환경변수 주입)
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

# config.py — 한국투자증권 모의투자 · 스윙 매매

APP_KEY = str(os.environ.get("APP_KEY", "")).strip()
APP_SECRET = str(os.environ.get("APP_SECRET", "")).strip()

# [수정됨] 계좌번호 앞뒤에 숨겨진 공백이 들어가서 8자리로 인식 안 되던 문제 해결 (.strip() 추가)
ACCOUNT_NO = str(os.environ.get("ACCOUNT_NO", "")).strip()  # 종합계좌번호 앞 8자리 (CANO)
ACCOUNT_PROD_CODE = "01"  # 계좌상품코드 뒤 2자리 (ACNT_PRDT_CD)

# 잔고조회(inquire-balance)에는 미사용. 주문 API 확장 시 참고용.
TRADE_PWD = str(os.environ.get("TRADE_PWD", "")).strip()
BASE_URL = "https://openapivts.koreainvestment.com:29443"

# 국내주식 실시간 체결가 WebSocket (모의: kis_devlp.yaml vops)
WS_BASE_URL = "ws://ops.koreainvestment.com:31000"
# False — 스윙/장투 폴링 전용 (WS 단타 틱 비활성)
USE_REALTIME_WEBSOCKET = False
POLLING_STRATEGY_MODE = True
WS_FAILBACK_POLL_SEC = 5.0  # WS 미사용/끊김 시 REST 폴백 주기
WS_WATCHLIST_TOP_N = 20  # 빈 슬롯 시 WS 구독할 유니버스 상위 N
WS_SCAN_DEBOUNCE_SEC = 0.8  # WS 틱 기반 재탐색 최소 간격
WS_ENGINE_WAIT_SEC = 0.05  # WS 연결 시 엔진 대기(이벤트 깨우기)
# 보안 프로그램·SSL 검사로 수신이 잠깐 멈춰도 끊기지 않도록 20초(최소 15초)
_WS_HEARTBEAT_RAW = float(os.environ.get("WS_HEARTBEAT_TIMEOUT_SEC", "20"))
WS_HEARTBEAT_TIMEOUT_SEC = max(15.0, _WS_HEARTBEAT_RAW)
# 재연결 백오프(초): 1 → 3 → 5 이후 5초 유지 (무한 재시도)
WS_RECONNECT_BACKOFF_SEC = (1.0, 3.0, 5.0)
WS_RECONNECT_RESYNC_DEBOUNCE_SEC = 3.0  # 재연결 직후 inquire_balance 동기화 최소 간격

# 멀티 슬롯 · 총 시드 (고정 종목당 150만 원 폐지 → 가변 베팅)
MAX_SIMULTANEOUS_STOCKS = 5
# 물리 슬롯 1~5 — 성격(장투/스윙/단타)은 UI 실시간 변경 · positions_state 저장
PORTFOLIO_SLOT_DEFINITIONS = [
    {"slot_uid": "1", "display_idx": 1},
    {"slot_uid": "2", "display_idx": 2},
    {"slot_uid": "3", "display_idx": 3},
    {"slot_uid": "4", "display_idx": 4},
    {"slot_uid": "5", "display_idx": 5},
]
ACCOUNT_INITIAL_PRINCIPAL = 10000000  # 디스코드 영수증 — 누적 자산 수익률(원금 대비) 기준 원금
ACCOUNT_TOTAL_SEED = 10000000  # 총 운용 시드
AUTO_TRADE_TOTAL_BUDGET = ACCOUNT_TOTAL_SEED  # 레거시 별칭 — 슬롯 고정금액 아님

# AI 가변 베팅 (betting_engine.py)
BET_CONVICTION_PCT_MIN = 0.30
BET_CONVICTION_PCT_MAX = 0.40
BET_CONVICTION_SCORE_MIN = 72.0
BET_SCOUT_SCORE_MAX = 58.0
BET_SCOUT_WON = 1_000_000
BET_SCOUT_TARGET_DEPLOY_WON = 2_800_000
BET_PYRAMID_ADD_MIN_WON = 1_500_000
BET_PYRAMID_ADD_MAX_WON = 2_000_000
BET_PYRAMID_MIN_PROFIT_PCT = 0.4
BET_STANDARD_PCT_OF_SEED = 0.12
BET_STANDARD_MAX_WON = 1_800_000
BET_RESERVE_CASH_PCT = 0.05
SLOT_MAX_ALLOCATION_LIMIT = 1_800_000

# 장중 신규 매수 (오버나잇 · 당일 강제청산 없음)
AUTO_TRADE_SCAN_START_TIME = "09:00"
AUTO_TRADE_SCAN_END_TIME = "15:30"

# 실시간 무한 롤링 (1H 정각 타이머 없음)
REALTIME_SCAN_INTERVAL_SEC = 300  # 폴링 모드 신규 진입 스캔 간격(초) — 매매 빈도 완화
REALTIME_ENGINE_TICK_SEC = 8.0  # 폴링 엔진 틱 (5~10초)
POLLING_POSITION_INTERVAL_SEC = 8.0  # 보유 종목 가격·청산 판정
POLLING_BALANCE_INTERVAL_SEC = 30.0  # inquire_balance 동기화
POLLING_DAILY_BARS_CACHE_SEC = 300  # 종목별 일봉 캐시
POLLING_DAILY_LOOKBACK_DAYS = 90
REALTIME_UNIVERSE_REFRESH_SEC = 600  # brain 유니버스 — 10분마다만 KIS 순위/수급 스캔
BRAIN_FLOW_CACHE_SEC = 300  # 거래대금 TOP-N + 수급 스캔 캐시 (5분)
TRADE_RANK_CACHE_SEC = 300  # 거래대금 순위 API — 5분 캐시 (코스닥 프록시·AI 공용)
BRAIN_FLOW_QUOTE_ENRICH_TOP_N = 5  # 상위 N종목만 현재가 추가 조회
REALTIME_SCAN_BATCH_SIZE = 30  # 회전 배치(스윙 1H 검증 API 부하 분산)
SCAN_INTERVAL_SEC = REALTIME_SCAN_INTERVAL_SEC  # 레거시 별칭
SCAN_ALIGN_TO_HOUR = False

# 모드별 REST 폴백 감시(폴링 모드에서는 POLLING_POSITION_INTERVAL_SEC 사용)
SCALP_MONITOR_INTERVAL_SEC = 0.0
SWING_MONITOR_INTERVAL_SEC = 8.0
LONG_MONITOR_INTERVAL_SEC = 10.0
POLLING_DISABLE_DAY_TRADING = True  # 폴링 모드에서 단타 진입·분봉 청산 비활성

# 단타 분봉 진입·청산 (1m/3m 수급)
SCALP_MIN_CHANGE_PCT = 2.0
SCALP_MAX_CHASE_CHANGE_PCT = 8.0
SCALP_MIN_VOL_SURGE_RATIO = 1.8
SCALP_STOP_LOSS_PCT = 1.5
SCALP_TAKE_PROFIT_PCT = 2.0
SCALP_TAKE_PROFIT_MIN_PCT = 3.0
SCALP_STAGNATION_MINUTES = 30
SCALP_STAGNATION_RANGE_PCT = 0.45
SCALP_FORCE_FLAT_TIME = "15:20"
SCALP_PEAK_TRAIL_MIN_PROFIT_PCT = 0.8
SCALP_PEAK_TRAIL_DROP_PCT = 0.6
SCALP_VOLUME_FADE_RATIO = 0.45
SCALP_TOP_WICK_REJECT_PCT = 3.5
SCALP_SCAN_TOP_N = 35  # 등락·거래량 상위만 분봉 심층 스캔
SCALP_USE_MARKET_ORDER = True  # 단타 진입 시장가 우선

# 스윙(테마주) — 매집·분할매수·목표 익절 (폴링·MA 확실 시만)
SWING_TARGET_PROFIT_PCT = 18.0
SWING_PARTIAL_EXIT_PCT_1 = 12.0
SWING_HARD_STOP_LOSS_PCT = 15.0
SWING_MIN_HOLD_BIZ_DAYS = 3  # 최소 보유 영업일 — 이전 조기 손절 억제
SWING_MA_EXIT_BREAK_PCT = 2.0  # MA20 이탈 % 이하에서만 구조 손절
SWING_ENTRY_REQUIRE_MA_ALIGNED = True
SWING_MAX_SPLIT_BUYS = 2
SWING_DIP_BUY_MIN_PCT = 3.0
SWING_DIP_BUY_MAX_PCT = 10.0
SWING_ACCUMULATION_LOOKBACK = 20
SWING_RESCUE_REQUIRE_MA_SUPPORT = True
SWING_RESCUE_MA_TOLERANCE_PCT = 1.5
SWING_RESCUE_TIMEOUT_BIZ_DAYS = 5
SWING_RESCUE_TIMEOUT_MIN_REBOUND_PCT = 1.5

# 장투(대형주) — 적립식·트레일링, 자주 매도하지 않음
LONG_MIN_HOLD_BIZ_DAYS = 10  # 최소 보유 영업일(임의 청산 억제)
LONG_DCA_INTERVAL_SEC = 86_400
LONG_DCA_SLICE_PCT = 0.06
LONG_MAX_DCA_ADDS = 8
LONG_TRAIL_MIN_PEAK_PCT = 8.0
LONG_NOISE_FILTER_PCT = 4.0
LONG_TRAILING_TIER1_DROP_PCT = 4.0
LONG_TRAILING_TIER2_DROP_PCT = 6.0
LONG_ENTRY_REQUIRE_MA_ALIGNED = True

# 6월 마스터 테스트용 장투 시한부 강제 청산
LONG_FORCE_EXIT_ENABLED = True
LONG_FORCE_EXIT_DATE = "2026-06-26"
LONG_FORCE_EXIT_TIME = "15:00"
LONG_FORCE_SPLIT_START_TIME = "14:30"
LONG_FORCE_SPLIT_END_TIME = "15:20"
LONG_FORCE_SPLIT_INTERVAL_MIN = 10
LONG_FORCE_SPLIT_TRANCHES = 5

# 감시 / UI
MONITOR_INTERVAL_SEC = 8  # 레거시 REST 감시 상한
BOOT_API_STAGGER_SEC = 8  # 기동 시 AI 부트스트랩 지연(스캔과 동시 폭주 방지)
UI_REFRESH_INTERVAL_SEC = 5
# 상단 [오늘 통합/총 예상 수익률]·슬롯 시세 fragment 자동 갱신 (F5 불필요)
COMMANDER_PNL_REFRESH_SEC = 5
# KIS REST — 초당 1회 미만 (EGW00201 방지). 1.5초 = 안전 기본값.
KIS_API_MIN_INTERVAL_SEC = 1.5
KIS_RATE_LIMIT_MAX_RETRIES = 4
KIS_RATE_LIMIT_RETRY_SEC = 2.0
ACCOUNT_REFRESH_SEC = 60  # 계좌 잔고 UI 갱신 주기
ACCOUNT_SNAPSHOT_REFRESH_SEC = 45  # 백엔드 잔고 스냅샷 (체결 확인용)
ORDER_STATUS_POLL_SEC = 10  # 체결 폴러 — 대기 주문 없을 때
ORDER_FILL_POLL_ACTIVE_SEC = 1  # 미체결 주문 있을 때 체결 확인 주기(초)

# 보유 슬롯 — 기본은 프로세스 메모리. Render Disk 등 영속 볼륨이 있으면 경로 지정.
# 예: POSITIONS_PERSIST_PATH=/var/data/positions_state.json
POSITIONS_PERSIST_PATH = os.environ.get("POSITIONS_PERSIST_PATH", "").strip()

# 매매 체결 영구 저장 (sqlite3) — trade_state.json 과 별도
TRADE_HISTORY_DB_PATH = os.environ.get("TRADE_HISTORY_DB_PATH", "").strip()

# 무인 알림(Webhook) — 텔레그램/카카오/디스코드 연동용
_ENABLE_RAW = str(os.environ.get("ENABLE_NOTIFICATIONS", "true")).strip().lower()
ENABLE_NOTIFICATIONS = _ENABLE_RAW in ("1", "true", "yes", "on")
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
KAKAO_WEBHOOK_URL = ""

# [수정됨] 디스코드 및 제미나이 환경변수가 없을 때를 대비한 안전장치 추가
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = str(os.environ.get("DISCORD_CHANNEL_ID")).strip()

NOTIFY_MARKET_OPEN_SUMMARY_TIME = "09:05"
NOTIFY_MARKET_OPEN_SUMMARY_WINDOW_MIN = 10  # 장시작 요약 발송 허용 구간(분)
NOTIFY_MARKET_CLOSE_SUMMARY_TIME = "15:35"  # 일일 결산 영수증 (하루 1회)
NOTIFY_MARKET_CLOSE_SUMMARY_WINDOW_MIN = 10  # 결산 발송 허용 구간 — 18시 재발송 방지
# 장 마감 직후 AI 복기·시장·내일 전략 리포트
ENABLE_DAILY_CLOSE_REPORT = True
DAILY_CLOSE_REPORT_TIME = "15:31"  # 15:30 마감 직후
DAILY_CLOSE_REPORT_WINDOW_MIN = 20
ENABLE_DAILY_CLOSE_REPORT_AI = True  # GEMINI_API_KEY 있으면 내일 전략 LLM 보강
ENABLE_AI_BRIEFING = True
AI_BRIEFING_NEWS_HOURS = 24
AI_BRIEFING_TOP_K = 3

# [수정됨] 제미나이 API 키 안전장치 추가
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-1.5-flash"

# 스윙 손익 (고정 % 손절은 ATR 도입 후 보조 상한으로만 사용)
TARGET_PROFIT_PCT = 12.0
TARGET_PROFIT_MAX_PCT = 15.0
TARGET_LOSS_PCT = -6.0
STOP_LOSS_PCT = TARGET_LOSS_PCT

# ATR 가변 손절 (1시간봉 Wilder ATR)
ATR_PERIOD = 14
ATR_STOP_MULT = 2.0
ATR_MIN_LOSS_PCT = 3.0
ATR_MAX_LOSS_PCT = 15.0

# SOR 분할 매도
SOR_MAX_SPLITS = 3

# 계단식 트레일링 스톱 (고점 수익률 기준)
TRAILING_MIN_PEAK_PROFIT_PCT = 5.0
TRAILING_TIER1_DROP_PCT = 2.0
TRAILING_TIER2_DROP_PCT = 3.0
TRAILING_TIER3_DROP_PCT = 5.0

# ── 활성주 유니버스 (코스피·코스닥 · 5일 평균 거래대금 · 리스크 필터) ──
SWING_MIN_AVG_TRADE_VALUE_5D = 10_000_000_000  # 100억 (코스닥 상위 약 5%)
SWING_MIN_STOCK_PRICE = 1_000  # 동전주 제외
SWING_LIQUIDITY_LOOKBACK_DAYS = 5
SWING_UNIVERSE_MAX_RANK_PAGES = 80  # 거래대금순 연속조회 (30×N 종목)
SWING_LIQUIDITY_MAX_CHECKS = 300  # 5일 거래대금 검증 상한 (API 부하·정각 스캔 시간)
# API 제외: 우선주·관리·투자위험·투자경고·환기·불성실공시 (1=제외)
SWING_RANK_EXCLUDE_CLS = "1111100000"

# 1시간봉 정배열·눌림목 (진입 조건 — 변경 금지)
SWING_HOURLY_LOOKBACK_DAYS = 14
SWING_HOURLY_TARGET_BARS = 70
SWING_HOURLY_TRADING_DAYS_FETCH = 12
SWING_MA_SHORT = 5
SWING_MA_MID = 20
SWING_MA_LONG = 60
SWING_MAX_CHASE_CHANGE_PCT = 5.0
SWING_PULLBACK_BARS = 6
SWING_VOLUME_DRY_RATIO = 0.5
SWING_NEAR_MA5_PCT = 2.5
SWING_NEAR_MA20_PCT = 3.5
SWING_USE_LIMIT_AT_CURRENT = True

# ── 중앙 판단 뇌 (Brain Classifier) ──
BRAIN_SCALP_VOL_RATIO_PCT = 500.0  # 전일 대비 거래량 500%+
BRAIN_LONG_TERM_MIN_MARKET_CAP = 2_000_000_000_000  # 장투: 시총 2조+
BRAIN_INST_BUY_STREAK_MIN = 1  # 외인/기관 순매수 연속(일) — API 확장 시 상향

# 모드별 예상 매도가 (trading_logic.py)
TARGET_SCALP_MIN_PCT = 3.0
TARGET_SCALP_MAX_PCT = 5.0
TARGET_SWING_MIN_PCT = 10.0
TARGET_SWING_MAX_PCT = 15.0
TARGET_LONG_CEILING_PCT = 20.0

# stock_ranking.py (단타 주도주 API — 스윙 선정에는 미사용)
TOP_RANK_COUNT = 20

# ── brain.py — 수급·테마·자동 모드 (신규 뇌) ──
BRAIN_FLOW_TOP_N = 20
BRAIN_LARGE_CAP_WON = 2_000_000_000_000
BRAIN_MID_CAP_WON = 500_000_000_000
BRAIN_VOLATILITY_SCALP_MIN = 6.0
BRAIN_VOLATILITY_SWING_MAX = 5.5
BRAIN_MIN_RECOMMEND_SCORE = 22.0
BRAIN_TURNOVER_SPIKE_MIN_MULT = 5.0
BRAIN_TURNOVER_AVG_DAYS = 20
BRAIN_FIN_DEBT_RATIO_MAX = 150.0
BRAIN_FIN_RESERVE_RATIO_MIN = 500.0
BRAIN_REQUIRE_NO_IMPAIRMENT = True
BRAIN_REQUIRE_RISK_EXCLUDED = True
BRAIN_PULLBACK_CHANGE_MIN = -2.5
BRAIN_PULLBACK_CHANGE_MAX = 4.0
BRAIN_LEADER_SECTOR_CODES = (
    "000660",
    "005930",
    "035420",
    "035720",
    "373220",
    "207940",
)
BRAIN_EXTRA_NEWS_HINTS = [
    "스페이스X 6월 11일 상장 전 우주항공 수급",
    "반도체 랠리 · HBM · AI 수요",
]
BRAIN_THEME_EVENTS = [
    {
        "event_id": "spacex_ipo_2026",
        "title": "스페이스X 상장",
        "event_date": "2026-06-11",
        "keywords": ["스페이스X", "SpaceX", "우주항공"],
        "watchlist": [
            {"code": "041190", "name": "미래에셋벤처투자", "momentum_bonus": 35},
            {"code": "211270", "name": "AP위성", "momentum_bonus": 32},
        ],
        "accumulate_start_days_before": 14,
        "accumulate_end_days_before": 1,
        "exit_on_event_day": True,
        "staged_buy_slices": 3,
    },
]

# ── AI 종합 예측 (market_ai.py) ──
AI_FORECAST_REFRESH_SEC = 600  # 장중 예측 재분석 주기(초)
AI_USE_LLM_REFINE = False  # True + OPENAI_API_KEY 시 LLM 정교화
AI_LLM_MODEL = "gpt-4o-mini"
# 장전·당일 참고 헤드라인 (수동·추후 RSS 연동)
AI_MANUAL_NEWS_HINTS = [
    "장전: 글로벌 AI·우주항공 모멘텀 점검",
]
# 주간 거시 캘린더 (active_weeks=ISO주차, 비우면 매주 적용)
AI_MACRO_CALENDAR = [
    {
        "title": "6월 중순 스페이스X 상장·우주항공 빅이벤트",
        "impact": "bullish",
        "note": "장투·스윙 비중 확대 · 단타는 이벤트 전후 변동성 대응",
        "active_weeks": [24, 25, 26],
    },
    {
        "title": "스페이스X IPO·우주항공 테마",
        "impact": "bullish",
        "note": "우주항공·방산 밸류체인 수급 기대",
        "active_weeks": [],
    },
    {
        "title": "코스닥 밸류업·정책 모멘텀",
        "impact": "bullish",
        "note": "중소형 성장주 순환매",
        "active_weeks": [],
    },
    {
        "title": "금리·환율 변동성",
        "impact": "bearish",
        "note": "리스크오프 시 단타 비중 확대",
        "active_weeks": [],
    },
]