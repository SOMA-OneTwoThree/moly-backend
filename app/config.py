import os
from functools import lru_cache

# mem0 텔레메트리(phone-home) 비활성 — mem0 import 전에 꺼야 적용(telemetry가 import 시 1회 읽음).
# 과거 moly-llm에서 세션시작 로드 지연(ReadTimeout)의 주원인. infra 명시값 우선(setdefault).
os.environ.setdefault("MEM0_TELEMETRY", "False")

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "moly-backend"
    environment: str = "local"
    # /dev 라우트(일기 강제삭제·유료 모델 평가 등 위험)는 명시 opt-in만 등록(기본 off). environment
    # 추정과 분리 — ENVIRONMENT 누락으로 local로 새어도 /dev가 노출되지 않게 한다(fail-closed, SOMA-376).
    enable_dev_routes: bool = False

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

    # --- 대화·일기·utility LLM 모델 ---
    # provider는 model-id 프리픽스로 라우팅(llm.py): gpt-* → OpenAI, claude-* → Anthropic.
    # 활성 = OpenAI GPT-5.6(2026-07 전환): chat=luna(가성비, 품질 terra급) / diary=terra(품질 고정) / utility=luna.
    # 대화·일기 모델은 분리한다(일기는 핵심 훅=열람율이라 대화 모델에 딸려 내려가면 안 됨).
    model_chat: str = "gpt-5.6-luna"
    model_diary: str = "gpt-5.6-terra"
    model_utility: str = "gpt-5.6-luna"
    # dormant(Anthropic 복귀·재사용용) — model_* 를 claude-* 로 되돌리면 prefix 라우팅이
    # _generate_anthropic 경로로 자동 복귀한다(코드 변경 없이 config만으로 왕복). SSM 오버라이드 가능.
    anthropic_api_key: str = ""
    anthropic_model_chat: str = "claude-sonnet-5"
    anthropic_model_diary: str = "claude-sonnet-5"
    anthropic_model_utility: str = "claude-haiku-4-5-20251001"
    llm_max_tokens: int = 1024  # 컴패니언 응답은 짧음(1~3문장). OpenAI엔 max_completion_tokens로 전달.
    # LLM 호출 per-request 타임아웃(초). SDK 기본(수 분)은 너무 길어 요청이 무한 대기할 수 있다.
    # 챗은 커넥션을 쥐지 않고(SOMA-374 2단계) 부르지만, 무응답 요청을 유한 시간에 끊어 재시도로 넘긴다.
    llm_timeout_s: float = 60.0
    # 캐시 최소 프리픽스(Anthropic 오경보 억제 기준. OpenAI는 자동캐시라 이 경보를 provider로 skip).
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
    # --- 대화 요약 checkpoint(W11) --- 리셋으로 버려질 구간을 요약해 남기고, 다음 턴은
    # 최신 checkpoint 하나 + 그 이후 메시지만 쓴다. 트리거(context_reset_*)·보존 tail
    # (context_keep_*)은 위 값을 그대로 쓴다 — 여기서 새 수치를 만들지 않는다.
    context_checkpoint_enabled: bool = False  # 킬스위치. False면 잡을 걸지도 저장하지도 않는다
    # 매 N번째 checkpoint는 이전 요약 대신 **원본**으로 다시 요약해 누적 왜곡을 계측한다.
    # ⚠️ 10은 품질 근거가 없는 운영 초기값이다 — 재검증본과 체인본의 차이를 재고 **측정 후 조정 필요**.
    context_checkpoint_reverify_every: int = 10
    # 재검증 1회가 읽을 원본 메시지 상한. 넘으면 재검증을 건너뛰고 체인 요약으로 진행한다
    # (부분 이력을 전체인 양 요약하지 않기 위함). ⚠️ 잠정값 — 실제 이력 길이 보고 **측정 후 조정 필요**.
    context_checkpoint_reverify_max_messages: int = 400
    # 프롬프트 캐싱: system(페르소나/기억) + 마지막 메시지에 cache_control. 기본 5m(단일 TTL).
    chat_prompt_cache_enabled: bool = True  # 킬스위치. OFF=메시지 breakpoint 제거(히스토리 청구 스케일↑ 유의)
    cache_ttl_system: str = "5m"            # "5m" | "1h"(write 2×, 워밍률 측정 후 결정)
    cache_ttl_messages: str = "5m"
    # 회계: 실비용 가중(단가÷입력단가) → billable × 입력단가 = 실제 청구액(정확). 한도=달러예산 직결.
    # provider마다 단가비율이 달라 가중치도 provider별 — _billable(chat.py)이 model prefix로 선택한다.
    # Anthropic(Sonnet $3/$15, 캐시read $0.30·write5m $3.75): out 5.0 / read 0.1 / write 1.25.
    bill_weight_output: float = 5.0        # 출력 $15 / 입력 $3
    bill_weight_cache_read: float = 0.1    # 캐시 읽기 $0.30 / 입력 $3
    bill_weight_cache_write: float = 1.25  # 캐시 쓰기(5m) $3.75 / 입력 $3
    # OpenAI(GPT-5.6 공식 요금표 2026-08-03, Standard·short context). 전 tier 입력 대비 비율 동일 —
    # 출력 6.0 / 캐시 읽기 0.1 / 캐시 쓰기 1.25. 예: luna $0.20·$0.02·$0.25·$1.20, terra는 ×10.
    # (캐시 쓰기는 무료가 아니다. 다만 API가 쓰기 토큰을 안 주므로 llm.py가 추정한다 — 그 주석 참조.)
    bill_weight_output_openai: float = 6.0        # 출력 $1.20 / 입력 $0.20 (luna), 전 tier 동일 비율
    bill_weight_cache_read_openai: float = 0.1    # 캐시 읽기 = 입력 단가의 10%(90% 할인)
    bill_weight_cache_write_openai: float = 1.25  # 캐시 쓰기 = 입력 단가의 125%
    # 턴 회계 v2 킬스위치 — True면 턴 내 모든 LLM 호출(주 chat + 한자 복원 등)을 합산해 차감한다.
    # False면 기존 동작(주 chat 호출만 차감, 나머지는 계측·로그만) — 롤백 경로. 스키마는 동일.
    turn_usage_v2_enabled: bool = True

    # --- 워커 배치 스케일링(SOMA-349) ---
    # 프로필을 키셋 페이지네이션으로 배치 처리(전량 메모리 적재 회피).
    worker_batch_size: int = 200
    # 배치 내 동시 처리 유저 수 상한(세마포어). 기본 1 = 실질 순차(유저별 독립 세션·타임아웃만).
    # >1로 올리려면 Phase 2 확인 필요(DB 풀 사이즈·pgbouncer 상한·mem0 직렬 이미 반영).
    worker_max_concurrency: int = 1
    # 유저 1명 처리 상한(초). 외부 API 지연이 배치를 장시간 막지 않게. 초과 시 스킵(다음 틱 재시도).
    worker_user_timeout_s: float = 120.0

    # --- 잡 플랫폼(async_jobs, W7) ---
    # ⚠️ 이 블록은 전부 **env 전용**이다. `app_config` hot override 대상에 넣지 않는다(명세 §W7):
    # 소비자 동시성·lease는 프로세스 기동값이라 런타임에 바뀌면 이미 잡힌 lease와 어긋난다.
    # ⚠️ 아래 수치는 전부 **보수적 초기값**이고 처리량 근거가 아직 없다 —
    #    DB pool wait p95 · queue oldest age · lease 만료율 · provider p95를 본 뒤 **측정 후 조정 필요**.
    job_backoff_base_s: float = 2.0     # equal-jitter 지수 backoff 기준(0초 연속 재시도 방지)
    job_backoff_cap_s: float = 60.0     # backoff 상한(긴 장애에서 무한정 대기 방지)
    job_reaper_interval_s: float = 10.0  # 최단 lease(20s)보다 짧게 — 회수 지연을 한 lease 안으로 제한
    job_reaper_batch_size: int = 50     # statement당 상한(무제한 UPDATE 방지)
    job_idle_sleep_s: float = 1.0       # claim 0건일 때 폴링 간격(빈 큐 스핀 방지)
    # 큐별 소비자 실행값(consumer 1개당, 두 EC2 각각 동일). content가 밀려도 critical/notification
    # 슬롯을 빌려 쓰지 않는다 — 큐 A 적체가 큐 B를 막지 않게 슬롯을 고정 분리한다.
    job_critical_concurrency: int = 2           # 결제 — 분리된 예약 슬롯, 짧은 DB/provider 처리
    job_critical_claim_batch: int = 2
    job_critical_timeout_s: float = 10.0
    job_critical_lease_s: float = 30.0
    job_critical_max_attempts: int = 3
    job_interactive_async_concurrency: int = 2  # 대화 후속 — 지연을 content와 격리
    job_interactive_async_claim_batch: int = 2
    job_interactive_async_timeout_s: float = 30.0
    job_interactive_async_lease_s: float = 45.0
    job_interactive_async_max_attempts: int = 3
    job_content_concurrency: int = 1            # 일기·요약·반추 — 현행 worker_user_timeout_s=120 준용
    job_content_claim_batch: int = 1
    job_content_timeout_s: float = 120.0
    job_content_lease_s: float = 150.0
    job_content_max_attempts: int = 3
    job_notification_concurrency: int = 1       # 저녁 푸시 — marker 선점 전 장애만 bounded retry
    job_notification_claim_batch: int = 1
    job_notification_timeout_s: float = 10.0
    job_notification_lease_s: float = 20.0
    job_notification_max_attempts: int = 3
    job_maintenance_concurrency: int = 1        # 유저 경로보다 낮은 우선순위
    job_maintenance_claim_batch: int = 1
    job_maintenance_timeout_s: float = 60.0
    job_maintenance_lease_s: float = 90.0
    job_maintenance_max_attempts: int = 3

    # --- FCM 푸시(Firebase Cloud Messaging) — 워커 아침/저녁 알림 ---
    fcm_project_id: str = ""
    fcm_service_account_file: str = ""  # service account JSON 경로(팀원 제공)
    # 아침 일기 푸시 킬스위치(SOMA-338). 현재 차단 → 저녁 안부만 발송. 코드는 유지, True로 되돌리면 재개.
    morning_push_enabled: bool = False

    # --- App Store(StoreKit) — JWS x5c 서명검증(구독/IAP/ASSN 웹훅) ---
    # 우리 설계는 App Store Server API 조회 없음 → .p8/Key ID/Issuer ID 불필요.
    # --- RevenueCat --- 구독·IAP 진실 소스. 대시보드 Integrations→Webhooks의 Authorization
    # 헤더 값(공유 시크릿). 요청 Authorization 헤더와 일치해야 처리(미설정 시 fail-closed 거부).
    revenuecat_webhook_auth: str = ""

    # --- Slack (운영 알림) — 워커 일일 요약 ---
    # Incoming Webhook URL(/moly/prod/slack-webhook → SLACK_WEBHOOK_URL 환경변수). 비면 no-op.
    slack_webhook_url: str = ""

    # --- mem0 (장기기억, 같은 Supabase pgvector) — 추출/임베딩은 OpenAI ---
    openai_api_key: str = ""
    embedder_model: str = "text-embedding-3-small"
    memory_llm_model: str = "gpt-4.1-mini"
    # 대화 모델 A/B 테스트(dev 전용, /dev/chat-eval). OpenAI는 위 키 재사용, Gemini만 별도 키.
    gemini_api_key: str = ""
    memory_collection: str = "memories"
    memory_load_top_k: int = 200  # 로드 상한(recency 로컬 랭킹)
    memory_max_render_items: int = 20  # 프롬프트에 넣을 최대 기억 수
    # 기억 스냅샷(chat_contexts.memory_text) — 핫패스 mem0 제거 + system[1] 안정(캐시 유지).
    memory_snapshot_refresh_hours: int = 6   # 이보다 오래면 갱신(mem0 재로드)
    memory_snapshot_stale_hours: int = 48    # 장애 시 이보다 오래된 스냅샷은 폐기("")
    memory_orphan_grace_hours: int = 24      # 탈퇴 고아 기억 스위퍼 유예(온보딩 레이스 방어)
    # "잊어줘" 실행 킬스위치(W10). **기본 off** — 확인 UX·범위가 제품 미결이라(명세 §5 게이트 #5)
    # 그전까지는 분류만 하고 아무것도 쓰지도 지우지도 않는다. 정책이 정해지면 켜기만 하면 된다.
    # 켠 뒤에도 normalized 유저에게만 실행된다(legacy 유저에게 성공을 가장하지 않는다).
    memory_forget_enabled: bool = False

    # --- 토큰 한도(임의 기본값, TBD) — app_config에 값이 오면 그게 우선 ---
    # 집계 = LLM 입력+출력 합산(kind='normal'만). 04:00 리셋.
    daily_token_limit_free: int = 20_000
    daily_token_limit_trial: int = 100_000
    daily_token_limit_subscriber: int = 100_000
    token_warning_threshold: int = 8_000  # 남은 토큰 이 값 이하면 소진 경고(터라 턴당 ~1.7k 기준 ~4~5턴 여유)
    review_prompt_min_tokens: int = 15_000  # 당일 누적 이 이상 생애 최초 → 리뷰 노출
    diary_llm_min_tokens: int = 2_000  # (레거시) 토큰 기반 개인일기 임계 — diary_min_user_chars로 대체
    # 개인(관찰) 일기 게이트 = 당일 유저 메시지 문자수(토큰 카운터와 분리 → 회계 변경에 불변).
    # 낮게 시작(오늘의 ~2메시지 선택성 재현). 실 트랜스크립트로 보정 전까지 낮은 쪽 편향(얇으면 preset 폴백 있음).
    diary_min_user_chars: int = 60

    # --- 런칭 무료 기간 --- 이 시각 이전엔 구독 없이 전원 무료(구독급 경험). 이후 자동으로 정상 등급.
    # app_config로 오버라이드 가능(재배포 없이 날짜 조정). 미설정/파싱실패 = OFF(fail-safe).
    free_launch_until: str = "2026-09-01T04:00:00+09:00"  # 활동일 8/31까지(로컬 04:00 경계)
    # 런칭 기간 일 토큰 한도(원가가중 billable 기준). luna 입력 $0.20/M 기준 ~월 $0.90/인 상한.
    free_launch_token_limit: int = 150_000

    # --- 모니터링·알림 (observability, SOMA-301) ---
    # 배포 이미지 커밋 sha — deploy가 GIT_SHA env로 주입. /health 버전 노출·배포 반영 확인용.
    git_sha: str = "unknown"
    # Slack severity 라우팅: 크리티컬(alerts)/상태·요약(status) 분리. 미설정 시 slack_webhook_url 폴백.
    slack_alert_webhook_url: str = ""   # 즉시 크리티컬(#moly-alerts) — down·배치실패·비용급증
    slack_status_webhook_url: str = ""  # 상태·요약·배포(#moly-status) — 조용한 채널
    alert_dedup_window_sec: int = 300   # 같은 알림키 억제 창(상관 스톰·flapping 스팸 방지)
    # 심층/합성 헬스 엔드포인트 인증(헤더 X-Health-Token 상수시간 비교). 비-local에서 비면 403(fail-closed).
    health_token: str = ""
    # 워커 데드맨(Healthchecks.io ping URL). 비면 no-op. **결과 정상일 때만** 핑(프로세스 생존 아님).
    worker_ping_url: str = ""
    # LLM 비용 이상치 경보 — 당일 누적 billable 합계가 이 값 초과 시 Slack(하루 1회). 0=비활성.
    # 기본값은 실트래픽 후 재보정(현행 헤비 유저 다수 × 150k 상한 여유).
    daily_billable_alert_threshold: int = 5_000_000
    # 합성 대화 모니터가 실제 LLM을 호출할지(비용 발생). False면 DB·설정 도달성만 확인.
    synthetic_check_llm: bool = True

    # --- 현재 턴 컨텍스트(프롬프트 삽입, SOMA-미정) — 킬스위치, 기본 off ---
    # 챗 프롬프트에 "지금 시각·오늘 첫 대화·함께한 일수·장착 아이템·루틴 진행" 블록 삽입 여부.
    current_turn_context_enabled: bool = False
    # last_active_bucket(최근 접속 후 경과) 렌더 여부 — 별도 스위치(단계적 롤아웃 대비).
    current_context_last_active_enabled: bool = False

    # --- 도구 루프(agent, W5) — 환경/배포 기본값. 전 키가 `app_config` override 대상이다 ---
    # 해석·검증은 app/services/agent/config.py(별도 계약, limits.py의 토큰 키와 섞지 않는다).
    # 여기 값은 DB override가 없거나 불량일 때의 fallback일 뿐이다.
    agent_enabled: bool = False                 # 킬스위치. False면 기존 단발 경로 그대로
    agent_turn_deadline_s: float = 5.0          # §0.1 하드 제약(응답 5초)
    agent_final_reserve_s: float = 2.5          # 최종 호출용 선예약(**측정 필요**)
    agent_max_tool_rounds: int = 1              # 라운드 상한(1 고정)
    agent_max_tool_calls_per_turn: int = 3      # 한 라운드 fan-out 상한
    # 아래 두 값은 임의 설정이 아니라 비용 부등식 `7.25D + 1.25T <= 2307`의 해다(§3.1.3).
    # **단독으로 바꾸지 말 것** — agent/config.py가 조합을 다시 검증해 위반이면 기본값으로 되돌린다.
    agent_decide_max_tokens: int = 192          # step 1 출력 상한(도구 결정)
    agent_tool_result_budget_tokens: int = 600  # 한 턴 도구 결과 **합계** 예산
    agent_tool_timeout_ms: int = 800            # 도구별 상한
    agent_tool_inflight: int = 8                # 프로세스 전체 동시 도구 수(**측정 필요**)
    agent_canary_pct: float = 0.0               # 카나리 비율(0.01% 단위, 비용 캡이 아니다)

    model_config = SettingsConfigDict(
        # 로컬 기본 = .env(dev). 프로덕션을 로컬에서 띄우려면 MOLY_ENV_FILE=.env.prod 로 명시한다.
        # 서버(EC2)는 docker compose가 backend.env를 실제 환경변수로 주입하고, 환경변수가
        # dotenv보다 우선하므로 이 값은 서버 동작에 영향이 없다.
        env_file=os.getenv("MOLY_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        protected_namespaces=(),  # model_chat 등 model_ 프리픽스 필드 허용(pydantic 예약 네임스페이스 해제)
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
        # 활성 모델(chat·diary·utility) 중 하나라도 그 provider면 키를 강제 — 부분 롤백
        # (예: chat만 claude, diary는 gpt 유지) 시 04:00 일기 배치가 빈 키로 죽는 걸 막는다.
        active_models = (self.model_chat, self.model_diary, self.model_utility)
        if any(m.startswith("gpt-") for m in active_models) and not self.openai_api_key:
            raise RuntimeError(
                "프로덕션 LLM 설정 누락(fail-closed): OPENAI_API_KEY (활성 모델에 gpt-*)"
            )
        if any(m.startswith("claude-") for m in active_models) and not self.anthropic_api_key:
            raise RuntimeError(
                "프로덕션 LLM 설정 누락(fail-closed): ANTHROPIC_API_KEY (활성 모델에 claude-*)"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
