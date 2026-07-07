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

    # --- mem0 (장기기억, 같은 Supabase pgvector) — 추출/임베딩은 OpenAI ---
    openai_api_key: str = ""
    embedder_model: str = "text-embedding-3-small"
    memory_llm_model: str = "gpt-4.1-mini"

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
