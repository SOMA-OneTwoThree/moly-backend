# 운세 긍정 경험 개편 — 2026-09-12

상태: 구현 및 기존 개발 DB 통합검증 완료. 개발 API 이미지 배포 전. origin/dev 1f34fe94 기반. 운영 변경·커밋·PR 없음.

## 제품 계약

목표는 점괘의 정확성 주장이 아니라 이해하기 쉬운 해석과 기분 좋은 일상 경험이다. 기존 Astronomy Engine/점성술 규칙/78장 타로 해석 자산을 폐기하지 않는다. 현재 운영은 점성술 모듈이 남아 있으나 fortune_scores에서 사용하지 않는 상태다. 이번 변경에서 실제 생성 경로로 재연결한다.

### 점성술과 타로의 역할

기존 date_chart_longitudes의 00/06/12/18시 평균, 태양 등 birth/current 행성 범위, aspect 검출과 기존 규칙 의미 결과를 사용한다. 동일 날짜는 모든 이용자에게 UTC 기준 chart를 사용한다. 출생시각·장소를 모르는 상태에서 상승궁/하우스는 만들지 않는다. 내부 astrology snapshot에 실제 버전/기준시간대/규칙 결과/선택 신호를 남긴다. 점성술 신호 fingerprint를 점수 seed에 넣고, 기존 flow별 행운색 규칙도 실제 적용한다. 이는 점성술 길흉 점수의 단조 변환이 아니며 천문신호와 경험용 확률분포를 조합하는 방식이다. 해시 입력으로 사용했다는 사실을 과학적 예측 근거로 표현하지 않는다.

카드는 점수와 별도의 domain에서 기존 78장/정역방향/서로 다른 다섯장 로직으로 뽑는다. 카드 의미와 정역방향별 원고를 유지한다. 점성술 raw점수에 따라 분야를 함께 올리거나 카드 점수 맞춤 재추첨을 하지 않는다.

### 점수

일반: 각 항목 70..100의 고정 정수 CDF(종 모양), 전체 평균 88±0.3/중앙값 88±1 목표. 결과당 약1%만 정확히 한 항목60..69, 나머지는 일반 분포. 100회마다 강제 저점 아님. 최초: 다섯항목 각각90..100 별도 CDF. 총점은 네 분야 평균이 아니다. 저점 여부를 제외한 축 추첨 domain은 분리한다. 카드와 점수 상관을 만들지 않는다.

### 사람 간 동일성과 저장

동일 birth/date/mode/score+draw+copy version이면 사용자 ID/성별/언어/호출순서와 무관하게 다섯 점수·카드·해석의 의미·행운색이 같다. 언어는 동일 의미의 현지화만 다르다. 날짜는 사용자 시간대로 결정하되 천문계산 기준은 UTC로 통일한다.

같은 날 이미 발급한 결과는 timezone_snapshot/profile revision 변화로 무효화하지 않는다. 프로필 수정은 다음 결과부터 적용하며 API result_invalidated=false, 기존 광고권한 유지. 날짜를 변경해 다른 날 결과를 만든 뒤 되돌리면 같은 입력/같은 버전에서 결정적으로 재생성한다. 최신 한 건 구조이므로 과거 문구 버전까지 영구 복원한다는 보장은 하지 않는다. 결과 보존과 cross-user동일성은 반드시 버전과 발급시 생일을 포함한 조건부 계약이다. 삭제는 명시적 결과 reset이며 삭제한 생일을 별도 보관해 복원하지 않는다.

### 최초 이용 상태

profiles에 nullable fortune_first_date DATE 하나를 추가한다. 최초 성공 reveal 트랜잭션에서 사용자 잠금 아래 날짜를 기록한다. 결과 저장 실패시 함께 rollback. 첫날은 재조회/운세프로필삭제재등록도 최초모드. 다음날부터 일반모드; 과거 첫날로 돌아가면 동일 첫날모드이며 새 혜택이 아니다. 계정삭제시 함께 삭제한다. 운세정보삭제시 유지하는 기능이용 날짜는 삭제 안내와 검증 대상이다.

기존 daily_fortunes가 있는 계정은 DATE 0001-01-01(legacy-used sentinel)로 표시하고 `legacy`결과를 그대로 유지한다. 외부에 노출하지 않는다. migration 뒤 구writer가 만든 daily가 있고 marker가 NULL이면 reveal에서 sentinel로 승계한다. NULL이며 daily도 없을 때만 실제 최초일을 기록한다. 이미 삭제된 이력은 완전히 복원할 수 없다. 데이터 없는 기존 fortune_profile은 신규사용 가능성이 있으므로 프로필존재만으로 사용으로 간주하지 않는다.

## 문구 범위

2026-09-13 사용자 요청으로 부분 수정 계획을 폐기하고, 총평·총평 상세·해볼 것·조심할 것·네 분야 해석을 한국어·영어·일본어 모두 새로 작성한다. 일반 원고는 언어별 780묶음이며 첫 방문 6세트도 새 원고로 구성한다. 새 집필·검수 기준과 진행 상태는 [문구 전면 재집필](fortune-editorial-reset-2026-09-13.md)을 따른다. 점수·점성술·타로 선택·멱등성은 이 문구 작업에서 변경하지 않는다. 점수 1점별 원고는 만들지 않으며 내부 카드와 점성술 정보는 공개하지 않는다. 기존 문단·구두점 계약과 birth/date/mode/version에 따른 첫 방문 선택 방식은 유지한다.

검수: 짧은 세 문장 각각 독립이해→타로의미 대응→전체 조합→한영일 의미동등→반복/과장/누락 검사. 작성자와 별도 판단자가 실제 검수한 범위만 검수완료로 기록한다. 점수높음=돈/관계/건강 성공 보장 금지.

## 구현 순서와 파일

1. 이 문서 독립 검증/수정, DAILY-FORTUNE.md 링크 및 현재정책 갱신.
2. fortune_scores: 점성술 연결/경험분포/첫모드/버전. fortune_copy_selection: 사용자ID seed 제거, wire v4 유지하면서 draw algorithm 별도버전. fortune_catalog: 점성술 색 검증 및 첫모드원고.
3. Profile ORM/schema.sql/검토 SQL: 최소 지속상태. fortune service: 최초원자성/같은날고정/삭제와 재생성. 공개API schema 유지, 프로필 invalidation 동작 안내 확인.
4. 원고/manifest/검수hash/생성문서 갱신. 정책·삭제안내 등 클라후속 필요부분 명시.
5. 기존 개발 Supabase 대상으로 마이그레이션 dry-run→확정SQL 적용→전용 합성계정 통합검증→모든 합성데이터 정리. 로컬 Docker/DB/unit pytest 금지. 운영DB 접근/변경 없음.
6. PR/커밋 없이 변경과 검증결과 보고. dev서버 실제 배포는 기존 승인경로 유무를 확인하며 로컬 브랜치 검증을 배포완료라 표현하지 않는다.

## 검증 게이트

분포: 각축 평균/중앙/경계/빈도, 전체1%저점 비율, 첫모드90..100, 축상관, 결정성. 사람간: 다른 UUID 같은 생일/날짜/모드/버전 완전일치, locale 의미선택일치. DB: 동시최초/응답유실재시도/rollback/프로필수정/삭제재등록/날짜왕복/광고잠금보존/legacy당일유지/다음날전환. 점성술: 실제 경로호출과 신호저장/UTC고정/기존 계산 보존. 원고: 전체키수·hash·manifest·삼언어·독립독해·조합 검수.

## 배포/복구

새 nullable컬럼은 선적용 가능한 additive 변경. 운영 SQL은 전달만 하고 실행하지 않는다. oldreader가 읽는 v4 카드 wire는 보존하고 새알고리즘은 별도버전 기록. oldwriter는 같은날프로필변경을 재생성하므로 혼합배포 기간에 완전한 신규 멱등성을 주장하지 않는다. 운영활성화는 모든노드 호환배포후 정해진 다음날 전환이 필요하다. 이전이미지 롤백시 새정책 최초상태/점수 보존 문제를 별도 검증한다. 새데이터를 지우거나 신규컬럼을 즉시 DROP하지 않는다.

독립 설계 검증: design_review가 기존코드와 대조. 최초일 sentinel/구writer catch-up/첫claim 조건 보완 후 구현 진행. 운영 활성 gate와 계약 생성 방식은 검증 후 결과에 별도 기록한다.

## 최종 구현·검증 결과

- 최초 marker: profiles.fortune_first_date DATE NULL 한 컬럼. legacy sentinel 0001-01-01. 비활성 기간 신규 legacy 생성도 같은 트랜잭션에 sentinel 기록(삭제 뒤 신규로 판정되는 오류 방지). 계정탈퇴시 삭제, 운세프로필 삭제시 이용일만 보존.
- 점성술: 기존 4시점평균/aspect규칙을 실제 연결, 신호 정수basis-point/의미결과를 canonical JSON으로 저장. 점수 domain과 카드 domain 분리. 기존 flow별 행운색 사용, coral은 orange로 치환.
- 카드 추첨 wire v4 유지, 새 draw_algorithm_version=fortune-birth-draw.v1. 기존 UUID 함수 경로는 이전정책/회귀fixture 전용, 새운세는 birth/date/mode만 사용.
- 첫 방문 6완성세트×3언어, 다섯 카드의 의미에 대응. 1점단위 템플릿 아님. 다음날 일반78장 카드 추첨.
- 이전 원고 검수 기록(전면 반려 전): short 1,404문장 중 210개 수정, 본문 2,340묶음 자동 분류 후 73개 표시 항목 직접 독해, 18개 언어별 본문 수정. 이 기록은 새 원고의 검수 근거가 아니다. 현재 전면 재집필의 완료 여부는 위 재집필 문서에서 별도로 관리한다.
- 분포: 일반73,000+첫방문73,000=146,000 결과. 일반축 평균88.015..88.040/중앙88, 저점포함결과0.9726%, 최대한축만60대. 첫방문최소90최대100/평균약95. 상관계수절댓값 최대0.00649. 개발DB접속대상확인 후 클라이언트 계산검증이며 배포된 API를146,000번 호출한 것이 아니다.
- 실제 기존 개발 Supabase 통합25개 통과(38.21초): 첫방문/일반 cross-user KO/EN/JA, concurrent4, failedcommitrollback, delete/recreate, birthday/timezoneimmutable, legacytoday/nextday, gateoff/gateon, 잘못된활성값, 활성전이용후삭제. 합성계정과 결과/개인정보원장/차단행 정리assertion 통과.
- 개발 DB schema.verify 통과. baseline은 한컬럼 additive수정. schema_contract는 기존계약에 실제개발카탈로그에서 읽은 신규컬럼 메타데이터를 결합하고 나머지호환대조0차이 확인으로 생성. 로컬빈DB생성은 사용자금지에 따라 수행하지 않았고 CI --check-generated가 이후독립검증한다.
- PR/커밋/운영DB변경/개발API이미지배포 없음. 변경브랜치 feat/fortune-positive-experience. 현재 devAPI가 이작업이미지를 제공한다고 주장하지 않는다.

## 운영 전달 절차 — 이번 작업에서 실행하지 않음

1. `db/changes/fortune_positive_experience.sql` 검토 SHA256 3501cc01208eee6cc5491cf3866595b5e198e7b258426953b61ac323a521ae2c. nullable컬럼추가+현재결과보유계정sentinel backfill. dev dry-run/commit 완료. 운영은 기존 db.apply --env prod 승인절차로 별도진행. 기준행수는 실행전 profiles NULL+daily EXISTS 집계하고 lock2초/statement60초 실패시 rollback.
2. 새코드는 development에서 즉시새정책, production에서는 app_config `fortune_experience_start_date` 문자열ISO날짜가 없거나잘못되면 기존점수/UUID카드정책 유지. 새환경변수/SSM항목추가 없음. 기존정책 점수함수는 fortune_scores_legacy.py에 보존.
3. 운영두노드가 새호환이미지임을 확인한 뒤 충분히미래의 전환일(전세계시간대에서 아직오지않은 날짜)을 app_config에 설정. 예시SQL은 아래틀의 날짜를 정한뒤 실행한다. **배포만으로 운영새점수/첫방문이 켜지지 않음.** 원고개선은 새이미지의 새결과에 적용된다.

```sql
-- 아래 YYYY-MM-DD를 실제 승인한 미래전환일로 바꾼 뒤 실행
INSERT INTO public.app_config(key,value)
VALUES ('fortune_experience_start_date', to_jsonb('YYYY-MM-DD'::text))
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value;
```

4. 전환일은 최초생성되는 결과부터 적용하며 기존당일결과를 강제로교체하지 않음. 이전날짜구버전결과와 신규결과 사이의 동일성은 버전차이예외. 운영개별사용자실기기시험/서버배포는 아직수행하지 않음.
5. 문제가있으면 호환이미지에서 활성일을미루거나키를해제해 미래생성만기존정책으로돌릴수 있음. 저장된새결과/marker는 삭제하지않음. 이전이미지로단순rollback하거나 컬럼DROP을복구수단으로쓰지않음. 잠금/광고상태도보존. 새날짜왕복시 과거copyversion영구복원은 최신한건저장범위밖임.

## 클라이언트 전달

공개응답 schema3/점수숫자/문구배열형태 유지. 최초전용 화면필수아님. 생일·성별수정후 result_invalidated=false로 당일결과유지, 변경사항은 다음날반영한다는 안내문구는 클라후속반영 필요. 운세정보삭제시 생일·결과는삭제하지만 첫이용여부중복방지용 날짜는계정탈퇴전까지 유지하므로 삭제안내와 개인정보정책 대조도 필요. 이 작업에서는 클라파일을 수정하지 않았다.

검증 산출물: [분포·개발DB 검증 결과](experience-validation.json), [현재 원고 검수 기록](review.json), [이전 부분 수정 이력](experience-editorial-review.json), [첫 방문 전체 문구](first-visit.md). 최종 dev /health 및 /health/ready는 기존배포버전1f34fe94에서200/DB정상 확인. 첫 readiness 요청은timeout이었으나 재확인정상; 신규이미지 검증과구분한다.
