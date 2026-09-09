# moly-backend

**BeCappy**(AI 컴패니언 iOS·Android 앱)의 백엔드. 카피바라 **캐피**와 여러 언어로 대화하고, 장기 기억·일기·루틴·꾸미기를 통해 관계를 이어간다.

- **모듈러 모놀리스**(FastAPI) 1서비스 + **배치 워커**(같은 코드, 프로세스만 분리)
- 통신은 전부 **HTTP 요청-응답(JSON)** — 스트리밍·WebSocket·폴링 없음. 서버가 먼저 보내는 건 FCM 푸시뿐
- **서버가 진실**: 재화·토큰·구독·가격은 서버가 원본, 클라의 DB 직접 쓰기 없음(모든 쓰기 API 경유)

## 스택

| 영역 | 사용 |
|---|---|
| 언어/프레임워크 | Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · uv |
| 데이터 | Supabase (Auth + Postgres + pgvector) |
| LLM·기억 | OpenAI GPT-5.6(luna=대화·utility, terra=일기) · `vecs.moly_memories_v2` + `mem0_memory_registry` |
| 외부 | FCM(푸시) · AdMob(리워드 SSV) · RevenueCat(구독·IAP) |

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
worker/              배치 워커 (15분 크론 1틱, SOMA-348)
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
uv run python -m worker                   # 배치 워커 1틱 (외부 크론이 15분마다 실행)
uv run pytest                             # 테스트
uv run ruff check .                       # 린트
```

꾸미기 v2 최종 에셋은 운영 반영 전에 별도 검증한다. 실제 매니페스트는 API 상품 필드
(`id/name/slot/price_hay/asset_version/assets`)의 `products` 배열이며 저장소에 임시 URL을
커밋하지 않는다.

```bash
uv run python scripts/verify_appearance_assets.py /path/to/appearance.json
```

DB 구조·검증·변경 절차는 `db/README.md`를 따른다. 구조 원본은 `db/schema.sql`이다.

채팅·상점 구매 응답 계약을 변경해 배포할 때는 진행 중인 개발자 요청을 멈춘 뒤 기존 멱등 JSONB를
먼저 읽기 전용으로 검사한다. 비호환 행이 있을 때만 두 번째 명령으로 선택 삭제하고,
배포 후 개발자 앱을 재시작해 이전 요청 키를 폐기한다.

```bash
uv run python scripts/verify_idempotency_responses.py
uv run python scripts/verify_idempotency_responses.py --delete-invalid  # 명시할 때만 DB 삭제
```

`.env` 필수값: `SUPABASE_URL`·`SUPABASE_PUBLISHABLE_KEY`·`SUPABASE_SECRET_KEY`·`SUPABASE_DB_CONNECTION_STRING`, `OPENAI_API_KEY`. 선택: `ANTHROPIC_API_KEY`(모델 롤백), `FCM_*`(푸시 — 키 없이 ADC/WIF 지원), `REVENUECAT_WEBHOOK_AUTH`(구독·IAP). 값이 없으면 해당 외부 기능은 안전하게 비활성(no-op/거부)된다.

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

외부 **15분 크론**(SOMA-348)이 `python -m worker`를 1틱 실행(멱등). 유저 로컬시각 기준:
- **매 대화 후 비동기** — 기억 후보 추출·임베딩·유효성 판정·관계와 사용자별 대화 약속 갱신
- **04:00** — 전일 일기 생성(개인/캐피)
- **09:00** — 아침 일기 FCM 푸시 · **20:00** — 저녁 안부 푸시
- **매 틱** — RC 웹훅 inbox 드레인(pending 처리 + 미해결 failed·장기 pending Slack 재요약, SOMA-372)

### 캐피 자기일기 — 주간 원고

개인 일기 조건에 미달한 사용자는 해당 활동일이 속한 주의 미수령 운영자 원고를 순서대로 받는다.
개인 일기를 받는 날은 원고를 소비하지 않는다. 원고가 없거나 모두 받았으면 일기를 만들지 않는다.
주 기준은 월요일~일요일이며, 일요일 일기가 월요일 아침 공개되더라도 이전 주 원고를 사용한다.
미수령분은 다음 주로 이월하지 않는다. 지급은 생성 커밋 시 확정하며 열람·푸시 성공과 무관하다.

```bash
uv run python scripts/make_capi_diary_template.py --weekly --week-start 2026-09-14 --count 3
# db/capi_diaries_weekly.csv의 모든 본문을 작성한다.
uv run python scripts/seed_capi_diaries.py db/capi_diaries_weekly.csv --env dev
uv run python scripts/seed_capi_diaries.py db/capi_diaries_weekly.csv --env dev --commit
```

CSV는 `week_start_date,sequence_no,weather,content`다. 같은 주/순서/내용의 재실행은 변경하지
않으며 기존 원고 수정·재활성화는 하지 않는다. 새 원고는 기존 최대 순서 뒤로만 추가한다.
등록 후 회수는 `is_active=false`로 처리하고 이미 지급한 일기는 유지한다.

`app_config.diary_weekly_start_date`(ISO 월요일 날짜)부터 주간 선택을 사용한다. 미설정 또는
그 이전 활동일은 기존 날짜별 CSV(`diary_date,weather,content`)를 사용한다. 잘못된 전환 설정은
일기 생성을 중단한다. 전환 이후 주간 원고가 없다고 날짜별 원고로 돌아가지 않는다.
DB 변경과 운영 머지 전 적용 순서는 [운영 절차](docs/OPERATIONS.md#주간-운영자-일기-db-전환)를 따른다.

## 배포 · 웹훅

- 컨테이너 1이미지 → API/워커 2프로세스(entrypoint만 분리). 매니지드 플랫폼 + 15분 크론
- ⚠️ RC 웹훅 배포 = **no-mixed-webhook**(SOMA-372): 구버전 pod는 inbox 미저장이라, 배포 중
  웹훅 유입을 일시중단하거나 신버전 전용 라우팅으로 전환한 뒤 배포한다(배포 런북).
- 공개 웹훅(배포 후 URL을 각 콘솔에 등록): `POST /webhooks/revenuecat`은 대시보드에 설정한 Authorization 값과 서버 secret을 정확히 비교하고, `GET /webhooks/ad-ssv`는 AdMob SSV 서명을 검증한다.

## 저장소 상태 (2026-08-26 확인)

- ✅ API·상주 잡 소비자·15분 배치 워커 운영 중
- ✅ 대화·기억 구조 운영 전환 완료. 장기 기억은 pgvector와 수명 장부 한 구조만 사용
- ✅ 에이전트 100% 적용. 공개 도구는 `recall_diaries`, `get_routines` 두 개
- ✅ 일기 앱 계약은 `/diaries` 세 경로. 상점 v2는 모자·안경 독립 장착 계약으로 유지
- ✅ iOS·Android 건초팩 상품 ID를 모두 처리하고, Meta 설치 리퍼러 복호화 API를 제공
- ✅ 저녁 푸시는 날짜·언어별 1회성 공지를 `app_config`로 예약할 수 있음
- ✅ 로컬·격리 개발 환경에서만 Swagger와 `/dev/*` 수동 테스트 지원
- 문서의 역할과 기준 우선순위는 `docs/README.md`에서 확인한다.
