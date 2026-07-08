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
db/                  실 스키마(schema.sql)·적용(apply.py)·검증(verify.py)·시드/가입트리거
scripts/             개발 도우미 (dev_token.py — 로컬 토큰 발급)
tests/               pytest — mock 유닛 + 실 Supabase 통합(tests/integration)
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

`.env` 필수값: `SUPABASE_URL`·`SUPABASE_ANON_KEY`·`SUPABASE_SERVICE_ROLE_KEY`·`SUPABASE_DB_CONNECTION_STRING`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`(mem0). 선택: `FCM_*`(푸시 — 키 없이 ADC/WIF 지원), `APP_STORE_*`(구독·IAP 실검증). 값이 없으면 해당 기능은 안전하게 비활성(no-op/거부)된다.

### API 손으로 테스트 (Swagger)

서버를 띄우면 `http://localhost:8000/docs`에 Swagger UI가 뜬다(로컬 전용). 전 엔드포인트가 Bearer 토큰을 요구하므로:

```bash
uv run uvicorn app.main:app --reload            # 1) 서버 → /docs
uv run python scripts/dev_token.py              # 2) 실 access token 발급 → 출력
#    → /docs 우측 상단 Authorize 🔒 에 붙여넣기 → 아무 API나 Try it out
uv run python scripts/dev_token.py --cleanup    # 3) 끝나면 테스트 유저 삭제(CASCADE)
```

> 소셜 로그인 전용(이메일·익명 비활성)이라, 스크립트가 `service_role`로 테스트 유저를 만들고 magiclink로 실 토큰을 발급한다. 시크릿은 코드에 없고 `.env`에서 읽는다(앱 런타임 엔드포인트 아님).

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

- ✅ API 전 기능 + 배치 워커 구현. 유닛 테스트 116개 + 실 Supabase 통합 테스트(전 엔드포인트 E2E, 50 체크)
- ✅ **실 DB 스키마 적용 완료** — 21테이블 + 가입 트리거 + `hay_packs` 시드 (`db/`, 3관점 보안 리뷰 반영)
- ✅ **StoreKit x5c 인증서체인 서명검증 완료**(Apple Root CA G3 내장) · **FCM 키리스 인증**(ADC/WIF, 키 다운로드 불필요)
- ✅ Swagger Authorize + 로컬 토큰 스크립트로 브라우저 수동 테스트 지원
- ⏳ 남은 시딩(0행, 코드 기본값 폴백): `shop_items`·`moly_life_ments`·`app_config` — 카피·수치 확정 필요
- ⏳ 배포/매시 크론(SOMA-151) · FCM 서비스계정 · 프로덕션 전 실 sandbox 결제 E2E
- 계약/스키마 상세 = 팀 노션(API_SPEC · ERD · ARCHITECTURE)
