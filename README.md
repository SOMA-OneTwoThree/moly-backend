# moly-backend

**몰리 앱**(AI 컴패니언 iOS)의 백엔드. 츤데레 카피바라 **캐피**와 한국어로 채팅하면, 캐피가 그 대화를 바탕으로 **몰래 일기를 쓰고** 유저는 다음 날 아침 훔쳐본다.

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
```

현재 저장소 계약의 기준은 `app/api` 라우트, `app/schemas` 요청·응답 모델,
`app/services` 동작과 `db/`의 canonical DDL이다.

확정 OpenAPI 원본은 `openapi/openapi.yaml`과 분할 YAML이다. 단일 파일 bundle은 직접
수정하지 않고 아래 명령으로 생성·검증한다.

```bash
uv run python scripts/openapi_contract.py --write
uv run python scripts/openapi_contract.py --check
```

## 로컬 개발

```bash
uv sync                                   # 의존성 설치(.venv)
cp .env.example .env                      # 시크릿 채우기(커밋 금지 · gitignore)
uv run uvicorn app.main:app --reload      # API 서버 → http://localhost:8000
uv run python -m worker                   # 배치 워커 1틱 (외부 크론이 매시 실행)
uv run pytest                             # 테스트
uv run ruff check .                       # 린트
```

장기기억 회상의 오프라인 임베딩 휴리스틱은 `OPENAI_API_KEY`를 설정한 뒤
`uv run python scripts/evaluate_memory_recall.py`로 확인한다(DB 쓰기 없음). 실제 mem0 랭킹·최종 답변
품질을 재현하지 않으므로 이 결과만으로 semantic rollout을 승인하지 않는다.

꾸미기 v2 최종 에셋은 운영 반영 전에 별도 검증한다. 실제 매니페스트는 API 상품 필드
(`id/name/slot/price_hay/asset_version/assets`)의 `products` 배열이며 저장소에 임시 URL을
커밋하지 않는다.

```bash
uv run python scripts/verify_appearance_assets.py /path/to/appearance.json
```

DB 전환 순서와 중단 조건은 `db/migrations/README.md`를 따른다.

채팅·상점 구매 응답 계약을 변경해 배포할 때는 진행 중인 개발자 요청을 멈춘 뒤 기존 멱등 JSONB를
먼저 읽기 전용으로 검사한다. 비호환 행이 있을 때만 두 번째 명령으로 선택 삭제하고,
배포 후 개발자 앱을 재시작해 이전 요청 키를 폐기한다.

```bash
uv run python scripts/verify_idempotency_responses.py
uv run python scripts/verify_idempotency_responses.py --delete-invalid  # 명시할 때만 DB 삭제
```

`.env` 필수값: `SUPABASE_URL`·`SUPABASE_ANON_KEY`·`SUPABASE_SERVICE_ROLE_KEY`·`SUPABASE_DB_CONNECTION_STRING`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`(mem0). 선택: `FCM_*`(푸시 — 키 없이 ADC/WIF 지원), `APP_STORE_*`(구독·IAP 실검증). 값이 없으면 해당 기능은 안전하게 비활성(no-op/거부)된다.

### API 손으로 테스트 (curl)

전 엔드포인트가 Bearer 토큰을 요구하므로:

```bash
uv run uvicorn app.main:app --reload            # 1) 서버 기동
uv run python scripts/dev_token.py              # 2) 실 access token 발급 → 출력
curl -H "Authorization: Bearer <토큰>" http://localhost:8000/chat/state   # 3) 원하는 API 호출
uv run python scripts/dev_token.py --cleanup    # 4) 끝나면 테스트 유저 삭제(CASCADE)
```

> 소셜 로그인 전용(이메일·익명 비활성)이라, 스크립트가 `service_role`로 테스트 유저를 만들고 magiclink로 실 토큰을 발급한다. 시크릿은 코드에 없고 `.env`에서 읽는다(앱 런타임 엔드포인트 아님).

## 규약

- **인증**: 전 엔드포인트 `Authorization: Bearer <Supabase JWT>` (웹훅·`/health` 제외). 서버가 JWKS(ES256)로 로컬 검증
- **에러**: `{ "error": { "code", "message", "details" } }` 통일. 프론트는 `code`로 화면 분기
- **하루 경계**: 토큰 리셋·일기 귀속 = `activity_date`(유저 로컬 **04:00**) / 출석·루틴·광고 보상 = `reward_date`(유저 로컬 **00:00**)
- **클라이언트 주의**: 로컬 00:00~03:59에는 두 기준일이 서로 다를 수 있다. 하나의 “오늘”로 합치지 말고 각 도메인의 기준일을 사용한다. `/charging-station`의 `activity_date` 필드도 값의 의미는 00:00 경계 `reward_date`다.
- **멱등**: `POST /chat/messages`는 `Idempotency-Key` 필수, 결제/광고는 트랜잭션ID로 자연 멱등

## 배치 워커

외부 **매시 크론**이 `python -m worker`를 1틱 실행(멱등). 유저 로컬시각 기준:
- **매 틱** — `MEMORY_INGESTION_ENABLED=true`일 때 완료된 활동일의 mem0 기억 추출
  (실패 시 1시간 후 재시도, 설정된 최대 횟수에서 중단)
- **04:00** — 전일 일기 생성(개인/캐피)
- **09:00** — 아침 일기 FCM 푸시 · **20:00** — 저녁 안부 푸시

### 캐피 자기일기 — 날짜별 지정

임계 미달·미접속 날 나가는 캐피 자기일기(`source=preset`)는 **날짜별로 직접 지정**할 수 있다.
생성 틱이 그날 `diary_date` 지정본을 우선 쓰고, 없으면 `moly_life_ments`의 랜덤 폴백 풀(`diary_date IS NULL`)로 떨어진다.

- `diary_date` = 그 일기가 **담는 날짜**(= `diaries.diary_date`). 예) `2026-07-17` 행 = **7/17 일기 → 7/18 아침 발행**.
- 지정본은 그날 **04:00 생성 틱 전까지** 들어가 있어야 반영된다(하루이틀 미리 채워두기). 빈 날은 랜덤 풀이 대신 나가 일기는 절대 비지 않는다.

```bash
# 1) 템플릿 생성(오늘부터 30일치 — diary_date·weather 채움, content만 빈칸)
uv run python scripts/make_capi_diary_template.py --start 2026-07-17 --days 30 --out db/capi_diaries.csv

# 2) db/capi_diaries.csv 의 content 칸에 일기를 써넣는다(weather는 기본 sunny, 손 안 대도 됨)

# 3) DB 반영 — content 채운 행만 업서트(멱등). 먼저 dry-run으로 확인
uv run python scripts/seed_capi_diaries.py db/capi_diaries.csv            # dry-run(ROLLBACK)
uv run python scripts/seed_capi_diaries.py db/capi_diaries.csv --commit   # 실제 반영
```

> ⚠️ 최초 1회 마이그레이션 선행 필요: `python db/apply.py db/migrations/20260717_capi_dated_diary.sql --commit` (`moly_life_ments.diary_date` 컬럼 추가). Supabase 대시보드에서 행을 직접 추가해도 동일하게 동작한다.

## 배포 · 웹훅

- 컨테이너 1이미지 → API/워커 2프로세스(entrypoint만 분리). 매니지드 플랫폼 + 매시 크론
- 공개 웹훅(배포 후 URL을 각 콘솔에 등록): `POST /webhooks/revenuecat`은 대시보드에 설정한 Authorization 값과 서버 secret을 정확히 비교하고, `GET /webhooks/ad-ssv`는 AdMob SSV 서명을 검증한다.

## 상태 (2026-07-08)

- ✅ API 전 기능 + 배치 워커 구현. 유닛 테스트 116개 + 실 Supabase 통합 테스트(전 엔드포인트 E2E, 50 체크)
- ✅ **실 DB 스키마 적용 완료** — 21테이블 + 가입 트리거 + `hay_packs` 시드 (`db/`, 3관점 보안 리뷰 반영)
- ✅ **StoreKit x5c 인증서체인 서명검증 완료**(Apple Root CA G3 내장) · **FCM 키리스 인증**(ADC/WIF, 키 다운로드 불필요)
- ✅ 로컬 토큰 스크립트(dev_token.py) + curl로 수동 테스트 지원
- ⏳ 남은 시딩(0행, 코드 기본값 폴백): `shop_items`·`moly_life_ments`·`app_config` — 카피·수치 확정 필요
- ⏳ 배포/매시 크론(SOMA-151) · FCM 서비스계정 · 프로덕션 전 실 sandbox 결제 E2E
- 계약/스키마 상세 = 팀 노션(API_SPEC · ERD · ARCHITECTURE)
