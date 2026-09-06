# DB 구조와 변경

`schema.sql`이 애플리케이션 DB 구조의 원본이다. 컬럼·기본값·CHECK·FK·인덱스·함수·트리거·RLS와
객체 권한을 함께 관리한다. `app/models/`는 애플리케이션 매핑이며 DB 전체 정의를 대체하지 않는다.

`schema_contract.json`은 빈 PostgreSQL 17에 `schema.sql`을 실행해 생성한 검증 산출물이다.
직접 편집하지 않는다. 스키마 SHA-256이 달라지면 검증이 실패하며, 구조를 다시 생성해 갱신한다.
과거 날짜별 마이그레이션·일회성 전환 SQL은 Git 이력에서 조회한다. 새 파일을 누적하지 않는다.

## 기존 환경 검증

```bash
uv run python -m db.verify --env dev
uv run python -m db.verify --env prod
```

읽기 전용 트랜잭션으로 구조만 조회한다. 컬럼 타입·NULL 허용·기본값, 검증된 제약, 유효한 인덱스,
함수 본문·소유자, 트리거, RLS, 객체 권한과 시퀀스 정의를 비교한다. 사용자 데이터나 연결 비밀값을
출력하지 않는다. 기본 검증은 혼합 버전 배포를 위해 새 테이블 등의 추가를 허용하지만 기존 객체의
제약·트리거·정책 추가와 권한 확대, 기본값 없는 필수 컬럼·UNIQUE 인덱스 추가는 거부한다.
이 검증이 모든 롤링 호환성을 증명하는 것은 아니므로 변경 SQL의 사용 경로도 검토한다.
`--strict`는 모든 추가 객체도 차이로 보고한다.

Supabase가 관리하는 Auth 테이블·역할·확장 내부 객체는 이 저장소의 소유 범위가 아니다.
애플리케이션이 Auth에 설치한 `on_auth_user_created` 트리거는 검증에 포함한다.

## 새 환경

Supabase의 `auth.users`, `anon`, `authenticated`, `service_role`과 PostgreSQL 17이 필요하다.
`vector`와 `pg_trgm` 확장은 public 스키마에 설치한다. 기존 애플리케이션 테이블이 하나라도 있으면
baseline 실행을 거부한다. **`schema.sql`은 기존 DB를 갱신하는 스크립트가 아니다.**

1. 빈 환경에 `schema.sql`을 적용한다.
2. 신규 환경의 상품·유효기간별 AI 단가를 `seed.sql`로 초기화한다. 기존 카탈로그에는 재실행하지 않는다.
3. 환경별 `app_config`, Storage와 인증·배포 설정을 운영 계약에 따라 준비한다.
4. 구조 검증과 가입·지급·탈퇴 동작을 확인한다.

캐피 날짜별 자기일기는 `capi_diaries.csv`와 `scripts/seed_capi_diaries.py`가 관리한다.
날짜 없는 임시 일기 풀을 새 환경에 다시 생성하지 않는다.

## 구조 변경 절차

1. 관련 커밋과 서비스 사용 경로를 확인한 뒤 `schema.sql`을 수정한다. NULL·기본값·권한 변경도
   정책 변경이므로 단순 정리로 처리하지 않는다.
2. 빈 로컬 DB에서 baseline과 검증 계약을 재생성하고 테스트한다.
3. 기존 DB에 필요한 차이만 담은 SQL을 리뷰 산출물로 준비한다. 대상·예상 행 수·잠금·혼합 버전
   호환·되돌리기 방법을 함께 검토한다. SQL 파일을 마이그레이션 디렉터리에 누적하지 않는다.
4. 검토한 SQL을 dev에 적용하고 검증한 뒤, 같은 절차로 prod를 적용한다. 자동 배포는 DB를 변경하지 않는다.

```bash
uv run python -m db.apply /path/to/reviewed.sql --env dev
uv run python -m db.apply /path/to/reviewed.sql --env dev --commit --expected-sha256 <검토한-SHA256>
uv run python -m db.verify --env dev
```

`apply`는 기본 실행 후 rollback이다. SQL 내부의 COMMIT/END도 외부 트랜잭션을 종료할 수 없게
실행한다. 시퀀스 증가와 외부 호출처럼 PostgreSQL rollback 대상이 아닌 효과는 복구되지 않으므로
검토 없이 live 데이터 쓰기를 시험하는 용도로 쓰지 않는다. 쓰기는 dev 프로젝트 ref를 확인하며, prod 반영에는
`--env prod --commit --allow-prod`가 모두 필요하다. 운영 반영은 검토·승인한 SQL만 실행한다.
대상 보호 검사는 URL의 사용자 이름에서 프로젝트 ref를 읽는다. 연결 대상을 덮어쓸 수 있는
query parameter나 fragment가 있는 주소는 dev 쓰기 대상으로 승인하지 않는다.
`--expected-sha256`은 검토 이후 파일이 바뀌었는지 확인한다. 기존 `schema_migrations` 기록은
보존하지만 새 변경을 이 원장에 누적하거나 배포 성공 조건으로 사용하지 않는다.

기본 트랜잭션 제한은 lock 2초, statement 60초, idle 60초다. lock을 얻지 못하면 제한을 늘리지
않고 rollback 후 한가한 시점에 재시도한다. `CREATE/DROP INDEX CONCURRENTLY`와 `VACUUM`은
트랜잭션 실행기에 넣지 않는다. 별도 검토한 절차로 실행하고 `indisvalid`·`indisready`와 구조
계약을 사후 확인한다. `IF NOT EXISTS`만으로 깨진 인덱스가 정상이라는 결론을 내리지 않는다.

## 로컬 재생성과 검증

`db/testing/supabase.sql`은 폐기 가능한 로컬 테스트 DB용 최소 Auth 의존성이다. Supabase에
실행하지 않는다. 연결 대상은 loopback의 `moly_schema_*` 이름이어야 생성 모드가 동작한다.
아래 명령의 연결 환경변수는 로컬 테스트 DB만 가리켜야 한다.

```bash
psql -X -v ON_ERROR_STOP=1 -f db/testing/supabase.sql
uv run python -m db.schema_contract --generate
psql -X -v ON_ERROR_STOP=1 -f db/seed.sql
uv run python -m db.schema_contract --strict
uv run pytest -q tests/integration/test_schema_bootstrap_local.py
```

`psql`은 `PGHOST/PGPORT/PGUSER/PGDATABASE`, 검증 도구는 `SUPABASE_DB_CONNECTION_STRING`,
테스트는 `MOLY_SCHEMA_TEST_DSN`을 사용한다. 모두 같은 폐기용 DB를 지정한다.
`--check-generated`는 또 다른 빈 DB에 스키마를 생성해 커밋된 계약과 일치하는지 검증한다.

## 반드시 보존하는 정책

- `products.price_hay=NULL`은 비매품이다. 0은 건초 원장 제약과 충돌하므로 허용하지 않는다
  (`d18e82b`). 건초 상품의 `public_id`·`asset_version`은 현재 prod 제약을 따른다.
- `name_i18n`의 SQL NULL은 번역 fallback 대상이다. JSON `null`과 다르며 기존 유저 루틴을
  이름만 보고 번역 데이터로 덮어쓰지 않는다 (`5856382`, `c272dfe`).
- 결제금액·가격표의 NULL은 미확인 또는 비해당 값이다. 0원으로 대체하지 않는다
  (`30579b0`, `1f9bc80`).
- 가입 기본 언어는 `en`, 지원 지역 태그는 `ko/en/ja`로 정규화한다. 온보딩이 보낸 한국어는
  `ko`로 저장한다 (`eedddd4`). 가입 선물은 기본 집·선글라스이며 운동 테마는 비활성이다
  (`5c6fe14`).
- 삭제 장벽은 프로필 삭제 후에도 남는다. FK CASCADE를 추가하지 않는다. 기억 벡터는
  `delete_user_memories` RPC로 정리하며 재실행 시 0건 반환이 정상이다 (`d9637fe`).
- 비용·재처리·계약의 nullable 참조, 삭제 장벽의 active 상태에서 비어 있는 `operation_id`와
  미완료 작업의 커서는 빈 값을 채워 넣는 대상으로 보지 않는다.
