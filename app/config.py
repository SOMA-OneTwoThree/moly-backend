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
    # 익명 로그인 토큰 허용 여부 — 제품은 소셜 전용이라 기본 거부(is_anonymous 토큰 401).
    # 통합 테스트만 True로 오버라이드(익명 sign-in으로 토큰 발급).
    allow_anonymous_auth: bool = False
    # API 서버 전용 DB 쓰기(서비스 롤). 클라 직접 쓰기 없음(ERD §8)
    supabase_db_connection_string: str = ""

    # --- Anthropic Claude (대화·개인일기=Sonnet / self-check·기억통합=Haiku) ---
    # 대화·일기 모델은 분리한다. 일기는 핵심 훅(열람율)이라 대화 모델 A/B에 딸려 내려가면 안 된다.
    # 대화 Haiku A/B는 코드 변경 없이 ANTHROPIC_MODEL_CHAT 환경변수(SSM)로만 전환한다.
    anthropic_api_key: str = ""
    anthropic_model_chat: str = "claude-sonnet-5"
    anthropic_model_diary: str = "claude-sonnet-5"
    anthropic_model_utility: str = "claude-haiku-4-5-20251001"
    llm_max_tokens: int = 1024  # 컴패니언 응답은 짧음(1~3문장)
    # 캐시 최소 프리픽스(모델별). 이 밑이면 캐시가 조용히 안 걸린다 — 오경보 억제용 기준.
    # Haiku 4.5·Opus=4096 / Sonnet 5·Sonnet 4.6·Fable=2048 / Sonnet 4.5 이하=1024.
    chat_cache_min_prefix_tokens: int = 2048

    # --- 대화 컨텍스트(앵커 append-only + 프롬프트 캐싱) ---
    chat_recent_messages: int = 30  # 앵커 미존재/폴백 시 최근 N
    # 앵커 리셋 트리거: 세그먼트가 이만큼 커지면 최근 KEEP만 남기고 앵커를 앞당김(1회 프리픽스 변경 후 append-only).
    # 트리거(RESET) ≫ 유지(KEEP) 여야 헤드룸이 생겨 리셋 사이 여러 턴이 캐시 히트(매턴 슬라이드 방지).
    context_reset_messages: int = 40       # 트리거: 세그먼트 메시지 수
    context_reset_chars: int = 30_000      # 트리거: 세그먼트 문자 수(긴 메시지 폭발 방어)
    context_keep_messages: int = 20        # 리셋 후 유지 메시지 수 (KEEP ≪ RESET)
    context_keep_chars: int = 12_000       # 리셋 후 유지 문자 상한
    context_hard_msg_cap: int = 120        # 쿼리 안전 상한(정상 시 트리거가 먼저 걸려 안 닿음)
    # 프롬프트 캐싱: system(페르소나/기억) + 마지막 메시지에 cache_control. 기본 5m(단일 TTL).
    chat_prompt_cache_enabled: bool = True  # 킬스위치. OFF=메시지 breakpoint 제거(히스토리 청구 스케일↑ 유의)
    cache_ttl_system: str = "5m"            # "5m" | "1h"(write 2×, 워밍률 측정 후 결정)
    cache_ttl_messages: str = "5m"
    # 회계: 실비용 가중(단가÷입력단가) → billable × 입력단가 = 실제 청구액(정확). 한도=달러예산 직결.
    bill_weight_output: float = 5.0        # 출력 $15 / 입력 $3
    bill_weight_cache_read: float = 0.1    # 캐시 읽기 $0.30 / 입력 $3
    bill_weight_cache_write: float = 1.25  # 캐시 쓰기(5m) $3.75 / 입력 $3

    # --- FCM 푸시(Firebase Cloud Messaging) — 워커 아침/저녁 알림 ---
    fcm_project_id: str = ""
    fcm_service_account_file: str = ""  # service account JSON 경로(팀원 제공)

    # --- App Store(StoreKit) — JWS x5c 서명검증(구독/IAP/ASSN 웹훅) ---
    # 우리 설계는 App Store Server API 조회 없음 → .p8/Key ID/Issuer ID 불필요.
    # --- RevenueCat --- 구독·IAP 진실 소스. 대시보드 Integrations→Webhooks의 Authorization
    # 헤더 값(공유 시크릿). 요청 Authorization 헤더와 일치해야 처리(미설정 시 fail-closed 거부).
    revenuecat_webhook_auth: str = ""

    # --- mem0 (장기기억, 같은 Supabase pgvector) — 추출/임베딩은 OpenAI ---
    openai_api_key: str = ""
    embedder_model: str = "text-embedding-3-small"
    memory_llm_model: str = "gpt-4.1-mini"
    memory_collection: str = "memories"
    memory_load_top_k: int = 200  # 로드 상한(recency 로컬 랭킹)
    memory_max_render_items: int = 20  # 프롬프트에 넣을 최대 기억 수
    # 기억 스냅샷(chat_contexts.memory_text) — 핫패스 mem0 제거 + system[1] 안정(캐시 유지).
    memory_snapshot_refresh_hours: int = 6   # 이보다 오래면 갱신(mem0 재로드)
    memory_snapshot_stale_hours: int = 48    # 장애 시 이보다 오래된 스냅샷은 폐기("")
    memory_orphan_grace_hours: int = 24      # 탈퇴 고아 기억 스위퍼 유예(온보딩 레이스 방어)

    # --- 토큰 한도(임의 기본값, TBD) — app_config에 값이 오면 그게 우선 ---
    # 집계 = LLM 입력+출력 합산(kind='normal'만). 04:00 리셋.
    daily_token_limit_free: int = 20_000
    daily_token_limit_trial: int = 100_000
    daily_token_limit_subscriber: int = 100_000
    token_warning_threshold: int = 3_000  # 남은 토큰 이 값 이하면 소진 경고
    review_prompt_min_tokens: int = 50_000  # 당일 누적 이 이상 생애 최초 → 리뷰 노출
    diary_llm_min_tokens: int = 2_000  # (레거시) 토큰 기반 개인일기 임계 — diary_min_user_chars로 대체
    # 개인(관찰) 일기 게이트 = 당일 유저 메시지 문자수(토큰 카운터와 분리 → 회계 변경에 불변).
    # 낮게 시작(오늘의 ~2메시지 선택성 재현). 실 트랜스크립트로 보정 전까지 낮은 쪽 편향(얇으면 preset 폴백 있음).
    diary_min_user_chars: int = 60

    # --- 런칭 무료 기간 --- 이 시각 이전엔 구독 없이 전원 무료(구독급 경험). 이후 자동으로 정상 등급.
    # app_config로 오버라이드 가능(재배포 없이 날짜 조정). 미설정/파싱실패 = OFF(fail-safe).
    free_launch_until: str = "2026-09-01T04:00:00+09:00"  # 활동일 8/31까지(로컬 04:00 경계)
    free_launch_token_limit: int = 30_000  # 런칭 기간 일 토큰 한도(원가가중 billable 기준, $3/월 목표)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    def require_production_ready(self) -> None:
        """비-local 부팅 시 결제 웹훅 인증 설정을 강제(fail-closed).

        revenuecat_webhook_auth가 비면 RC 웹훅이 전량 401이라 구독/결제 동기가 멈춘다.
        오배포(빈 시크릿)를 부팅 실패로 차단.
        """
        if self.environment == "local":
            return
        if not self.revenuecat_webhook_auth:
            raise RuntimeError(
                "프로덕션 결제 설정 누락(fail-closed): REVENUECAT_WEBHOOK_AUTH"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
