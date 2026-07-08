import os
from functools import lru_cache

# mem0 텔레메트리(phone-home) 비활성 — mem0 import 전에 꺼야 적용(telemetry가 import 시 1회 읽음).
# 과거 moly-llm에서 세션시작 로드 지연(ReadTimeout)의 주원인. infra 명시값 우선(setdefault).
os.environ.setdefault("MEM0_TELEMETRY", "False")

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "moly-backend"
    environment: str = "local"

    # --- Supabase (Auth + Postgres + pgvector) ---
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    # JWT 검증(JWKS 로컬 검증) — 미설정 시 remote getUser 폴백(auth 설계 단계에서 확정)
    supabase_jwks_url: str = ""
    # API 서버 전용 DB 쓰기(서비스 롤). 클라 직접 쓰기 없음(ERD §8)
    supabase_db_connection_string: str = ""

    # --- Anthropic Claude (대화·개인일기=Sonnet / self-check·기억통합=Haiku) ---
    anthropic_api_key: str = ""
    anthropic_model_chat: str = "claude-sonnet-5"
    anthropic_model_utility: str = "claude-haiku-4-5-20251001"
    llm_max_tokens: int = 1024  # 컴패니언 응답은 짧음(1~3문장)

    # --- 대화 컨텍스트 ---
    chat_recent_messages: int = 30  # 프롬프트에 넣을 최근 메시지 수

    # --- FCM 푸시(Firebase Cloud Messaging) — 워커 아침/저녁 알림 ---
    fcm_project_id: str = ""
    fcm_service_account_file: str = ""  # service account JSON 경로(팀원 제공)

    # --- App Store(StoreKit) — JWS x5c 서명검증(구독/IAP/ASSN 웹훅) ---
    # 우리 설계는 App Store Server API 조회 없음 → .p8/Key ID/Issuer ID 불필요.
    # 검증에 필요한 것: Bundle ID (+ 프로덕션은 App Apple ID). 루트 CA는 코드 내장.
    app_store_bundle_id: str = ""
    app_store_environment: str = "Sandbox"  # Sandbox | Production
    app_store_app_apple_id: int | None = None  # 프로덕션 알림 검증 시 필요(앱 숫자 ID)

    # --- mem0 (장기기억, 같은 Supabase pgvector) — 추출/임베딩은 OpenAI ---
    openai_api_key: str = ""
    embedder_model: str = "text-embedding-3-small"
    memory_llm_model: str = "gpt-4.1-mini"
    memory_collection: str = "memories"
    memory_load_top_k: int = 200  # 로드 상한(recency 로컬 랭킹)
    memory_max_render_items: int = 20  # 프롬프트에 넣을 최대 기억 수

    # --- 토큰 한도(임의 기본값, TBD) — app_config에 값이 오면 그게 우선 ---
    # 집계 = LLM 입력+출력 합산(kind='normal'만). 04:00 리셋.
    daily_token_limit_free: int = 20_000
    daily_token_limit_trial: int = 100_000
    daily_token_limit_subscriber: int = 100_000
    token_warning_threshold: int = 3_000  # 남은 토큰 이 값 이하면 소진 경고
    review_prompt_min_tokens: int = 50_000  # 당일 누적 이 이상 생애 최초 → 리뷰 노출
    diary_llm_min_tokens: int = 2_000  # 당일 누적 이 이상 → 개인(관찰) 일기

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
