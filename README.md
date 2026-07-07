# moly-backend

**몰리 앱**(AI 컴패니언 iOS)의 백엔드. 츤데레 카피바라 **몰리**와 한국어로 채팅하면, 몰리가 그 대화를 바탕으로 **몰래 일기를 쓰고** 유저는 다음 날 아침 훔쳐본다.

- **모듈러 모놀리스**(FastAPI) 1서비스 + **배치 워커**(같은 코드, 프로세스만 분리)
- 통신은 전부 **HTTP 요청-응답(JSON)** — 스트리밍·WebSocket·폴링 없음. 서버가 먼저 보내는 건 FCM 푸시뿐
- **서버가 진실**: 재화·토큰·구독·가격은 서버가 원본, 클라의 DB 직접 쓰기 없음(모든 쓰기 API 경유)

## 스택

| 영역 | 사용 |
|---|---|
| 언어/프레임워크 | Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · uv |
| 데이터 | Supabase (Auth + Postgres + pgvector) |
| LLM | Anthropic Claude (Sonnet=대화·개인일기 / Haiku=self-check) · mem0(장기기억) |
| 외부 | FCM(푸시) · AdMob(리워드 SSV) · Apple StoreKit(구독·IAP) |

## 구조

```
app/
  main.py            FastAPI app factory (라우터 등록)
  config.py          설정(pydantic-settings, .env)
  core/              db · security(JWKS 인증) · errors(공통 규약) · time_utils(activity_date)
  models/            SQLAlchemy 모델 (ERD 매핑)
  services/          도메인 로직 (account·chat·diary·economy·routine·shop·subscription·ads·review + hay_ledger·gating·llm·memory 등)
  schemas/           요청 스키마 (pydantic)
  api/               라우터 (엔드포인트)
worker/              배치 워커 (매시 크론 1틱)
tests/               pytest (mock 유닛)
docs/                계약·스키마·현황 (노션 동기화 사본)
```

## 로컬 개발

```bash
uv sync                                   # 의존성 설치(.venv)
cp .env.example .env                      # 시크릿 채우기(커밋 금지 · gitignore)
uv run uvicorn app.main:app --reload      # API 서버 → http://localhost:8000/docs
uv run python -m worker                   # 배치 워커 1틱 (외부 크론이 매시 실행)
uv run pytest                             # 테스트
uv run ruff check .                       # 린트
```

`.env` 필수값: `SUPABASE_URL`·`SUPABASE_ANON_KEY`·`SUPABASE_SERVICE_ROLE_KEY`·`SUPABASE_DB_CONNECTION_STRING`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`(mem0). 선택: `FCM_*`(푸시). 값이 없으면 해당 기능은 안전하게 비활성(no-op/거부)된다.

## 규약

- **인증**: 전 엔드포인트 `Authorization: Bearer <Supabase JWT>` (웹훅·`/health` 제외). 서버가 JWKS(ES256)로 로컬 검증
- **에러**: `{ "error": { "code", "message", "details" } }` 통일. 프론트는 `code`로 화면 분기 (코드 목록 = `docs/DEV_STATUS.md`)
- **하루 경계**: `activity_date` = 유저 로컬 **04:00** (토큰 리셋·출석·일기 귀속의 공통 키)
- **멱등**: `POST /chat/messages`는 `Idempotency-Key` 필수, 결제/광고는 트랜잭션ID로 자연 멱등

## 배치 워커

외부 **매시 크론**이 `python -m worker`를 1틱 실행(멱등). 유저 로컬시각 기준:
- **04:00** — 전일 일기 생성(개인/몰리) + mem0 기억 통합
- **09:00** — 아침 일기 FCM 푸시 · **21:00** — 저녁 안부 푸시

## 배포 · 웹훅

- 컨테이너 1이미지 → API/워커 2프로세스(entrypoint만 분리). 매니지드 플랫폼 + 매시 크론
- 공개 웹훅(배포 후 URL을 각 콘솔에 등록): `POST /webhooks/appstore`(Apple ASSN) · `GET /webhooks/ad-ssv`(AdMob SSV) — 서명이 인증

## 상태 (2026-07-08)

- ✅ API 전 기능 + 워커 구현, 테스트 114개, 3관점 보안 리뷰 반영
- ⏳ **실 DB 검증 대기**: 팀원이 ERD 스키마 적용(옛 테이블 교체 + 신규 `idempotency_keys`·`ad_rewards`·`subscription_hay_grants`·`iap_purchases`)
- ⚠️ **StoreKit 결제 검증은 비로컬 환경 fail-closed** — 프로덕션 전 Apple x5c 인증서체인 서명검증 구현 필요
- 계약/스키마 상세 = 팀 노션(API_SPEC · ERD · ARCHITECTURE)
