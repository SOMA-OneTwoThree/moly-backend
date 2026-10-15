# moly-backend

BeCappy의 대화·기억·일기·루틴·운세·상점 API와 비동기 작업을 담당한다. FastAPI 애플리케이션과
배치 tick·상주 consumer가 같은 코드와 Supabase PostgreSQL을 사용한다. 계정·온보딩·알림 설정·
탈퇴 API는 별도 `moly-auth`가 소유한다.

## 기준과 구조

| 위치 | 역할 |
|---|---|
| `app/api/` · `app/schemas/` | HTTP 라우터와 요청·응답 검증 |
| `app/services/` | 도메인 정책·외부 연동·대화 처리 |
| `app/models/` | SQLAlchemy 매핑 |
| `worker/tick.py` · `worker/consumer.py` | 시간대별 작업 예약과 lease 기반 잡 실행 |
| `db/schema.sql` · `db/schema_contract.json` | DB 구조 원본과 생성된 검증 계약 |
| `openapi/` | 공개 API 계약과 생성된 bundle |
| `content/fortune/` · `app/resources/` | 운세 편집 입력과 실행용 콘텐츠 |
| `tests/` | 외부 호출 없는 단위 검사와 명시적으로 켜는 통합 검사 |

Python 지원 범위·의존성은 [pyproject.toml](pyproject.toml), 고정 버전은 `uv.lock`을 따른다.
모델·한도·기능 플래그는 코드와 환경 설정으로 결정하며 README의 숫자로 고정하지 않는다.

## 개발과 검증

```bash
uv sync --frozen
uv run ruff check .
uv run pytest -q --ignore=tests/integration
uv run python scripts/openapi_contract.py --check
```

통합 검사는 해당 테스트의 환경 가드와 기존 개발 API·개발 Supabase 대상을 확인한 뒤 실행한다.
새 로컬 Docker/DB를 검증 환경 대신 띄우지 않는다. `.env.example`은 설정 이름의 안내이며 실제
`.env`·토큰·서비스 계정 파일은 Git에 넣지 않는다. 기본 AWS 계정은 프로젝트 계정으로 간주하지 않는다.

OpenAPI 분할 원본을 바꿨다면 `uv run python scripts/openapi_contract.py --write`로 bundle을
재생성하고 검사한다. DB 변경은 코드 배포와 별개이며 적용·검증·복구 절차를 먼저 준비한다.
작업 구현과 검증을 끝낸 뒤 마지막에 PR을 만든다. 운영 변경은 명시적으로 승인된 범위만 수행한다.

## 핵심 계약

- 재화·권한·상품 가격·대화 한도는 서버가 판정한다. 클라이언트는 도메인 DB를 직접 쓰지 않는다.
- 대화 한도·일기 귀속은 사용자 현지 04:00, 출석·루틴·광고 보상·운세는 00:00 기준이다.
- 대화는 멱등 키와 사용자별 lease로 직렬화하고 외부 모델 호출 중 DB 트랜잭션을 열어 두지 않는다.
- 생성물은 프로필의 지원 언어를 따른다. 감정 선택값은 오늘의 5종만 내부 정보로 쓰며 선제 노출하지 않는다.
- 설정 기본값·검사 성공·실제 배포 상태는 서로 다른 증거다. 운영 상태를 문서의 과거 숫자로 판단하지 않는다.

## 문서

[문서 안내](docs/README.md) · [전체 구조](docs/ARCHITECTURE.md) ·
[대화·기억](docs/ARCHITECTURE-capi.md) · [API](docs/API_SPEC.md) · [데이터 모델](docs/ERD.md) ·
[운영](docs/OPERATIONS.md) · [운세](docs/DAILY-FORTUNE.md) · [배너](docs/BANNER_SDUI.md) ·
[구독](docs/SUBSCRIPTIONS.md) · [BGM](docs/REMOTE_BGM.md)
