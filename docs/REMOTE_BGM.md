# 원격 배경음악

기존 Lo-fi 6개는 앱 설치 파일과 안정 ID를 보존한다. 신규 백색 소음은 운영자가 파일을
Storage에 올린 뒤 DB에 등록하고, 앱이 전체 목록 API로 발견하여 모바일 데이터에서도
자동 다운로드한다. 최초 다운로드 전에는 오프라인 재생되지 않는다. 기본 seed에는 기존 Lo-fi만 포함한다. 개발 환경에는 사용자 제공 원본 WAV 4개·MP3 3개를
별도 등록했다. 가짜 음원이나 빈 백색 소음 행은 없다.

## API와 재생 계약

`GET /bgm/tracks`, 기존 Supabase Bearer 인증, `X-App-Locale`(ko/en/ja).
응답은 `{schema_version: 1, revision: "64자리 SHA256", tracks: [...]}`다.
각 track은 `id`, `category`(lofi/white_noise), 현지화된 `title`, `source`(bundled/remote),
음원 `revision`, `url`, `sha256`, `size_bytes`, `mime_type`, `sort_order`를 갖는다.
bundled의 원격 메타데이터 4개는 null, remote는 모두 필수다. 설치 파일은 id로 찾는다.
응답 revision은 현지화된 전체 목록의 해시이며 파일 identity는 track revision/sha256이다.
제목/정렬/언어 변경만으로 음원을 다시 다운로드하지 않는다.

정상 200 snapshot은 bundled 포함 전체 활성 목록의 권위다. 사라진 곡은 목록에서 제거한다.
재생 중인 파일은 사용이 끝난 뒤 정리한다. 내장 파일은 패키지에 남지만 목록에서는 제거한다.
첫 성공 snapshot 전에는 기존 Lo-fi 6개가 fallback이다. 통신 실패, 파싱 실패, 불완전한
메타데이터는 삭제 신호가 아니며 기존 snapshot/파일을 보존한다. 서버는 한 SQL에서 전체
활성 행을 읽고, 한 행이라도 잘못되면 정상 partial/empty 응답 대신 오류를 반환한다.
빈 활성 목록은 유효한 의도적 전체 삭제이므로 운영 시 주의한다.

파일 다운로드 완료와 size/SHA256 검증 전에는 기존 파일을 대체하지 않는다. 임시 파일을
검증한 후 원자적으로 교체하고 재생 중인 이전 버전은 다음 재생부터 교체한다. 모든 곡은
단곡 무한 반복이며 Lo-fi와 백색 소음의 동시 재생은 없다. 다운로드는 앱 실행 중 시작하며
앱 종료/OS suspension 중 완료를 보장하는 background transfer 계약은 아니다.

## 파일 등록과 교체

기존 환경별 Supabase Storage의 공개 읽기 `bgm-assets` bucket을 사용한다. 이 문서는 실제
bucket을 생성하거나 권한을 바꾸지 않는다. dev/prod origin을 분리한다. API 서버는 음원
바이트를 프록시하지 않으며 앱 인증 토큰을 Storage로 전달하지 않는다.

1. 최종 음원의 재배포 가능 여부, 양 플랫폼 디코딩, 끝/시작의 무음·클릭을 검수한다.
2. `{id}/{revision}/audio.m4a` 등의 새 immutable 경로에 업로드한다. 같은 URL 덮어쓰기 금지.
3. 확정 bytes의 SHA256, size, MIME와 3개 언어 제목을 준비한다. 초기 지원 MIME은
   audio/mp4, audio/mpeg, audio/wav, 개별 파일 최대 100 MiB다. 이는 다운로드 방어 한도이며
   모든 codec/컨테이너 변형의 재생 보장이 아니다. 실제 파일 준비 후 iOS/Android 검수 필요.
4. API track 형태의 JSON으로 `uv run python -m scripts.validate_bgm_publication metadata.json`
   를 실행한다. 운영은 `--production`. HTTP 200/redirect 없음/MIME/크기/hash를 읽기 전용으로
   검증한다. 이 도구는 DB를 수정하지 않는다.
5. 검증 후 트랜잭션으로 metadata를 등록/교체하고 `is_active=true`를 마지막에 설정한다.
   교체는 같은 id, 새 revision/URL/hash를 사용한다. 제거는 is_active=false 또는 행 삭제다.
6. 앱 다음 조회에서 반영된다. DB 갱신 즉시 실행 중 모든 앱에 push하는 기능은 없다.
   이전 파일은 곧바로 Storage에서 삭제하지 말고 구 snapshot 다운로드의 유예 기간을 둔다.

목록과 음원은 사용자별 자산이 아니므로 회원 테이블을 변경하지 않는다. 로그아웃 시 음원
파일 삭제를 필수로 요구하지 않는다. 음원 추가를 위해 앱 코드/서버 재배포는 필요 없지만,
새 category/codec/source 계약은 별도 앱 지원이 필요하다.

## 배너

현재 `home_blind.json`에는 음악 배너가 없으며 이번 변경에서도 그대로 유지한다.
`open_music` 행동은 자동 선택/재생 없이 기존 음악 화면을 연다. 구버전은 Lo-fi 6곡 추천을
그대로 사용한다. `remote_bgm_v1` capability를 보내는 지원 앱에만, 실제 음악 binding을
요청한 경우 DB 활성 목록에서 제목을 고른다. DB I/O는 서비스에서 하고 manifest 검증과
렌더러는 순수 함수로 유지한다. 잘못된/빈 목록은 음악 카드만 제외하며 다른 카드를 유지한다.
같은 날짜와 같은 활성 ID 집합에서는 같은 곡을 고른다. 당일 목록 변경 시 추천이 바뀔 수 있다.
음악 노출 재개는 별도 작업이며 현재 5장 상한과 라인업을 다시 검토해야 한다.

## DB 적용과 검증

`db/README.md` 규약대로 schema.sql과 모델을 갱신하고 PostgreSQL 17의 새 로컬 DB에서
schema_contract.json을 재생성한다. 기존 환경에는 `db/changes/remote_bgm.sql`만 리뷰 후
적용한다. 기존 회원 데이터 변경은 없으며 bgm_tracks 새 테이블과 6개 seed 행만 추가한다.
이 SQL은 자동배포에 연결하지 않는다. 새 서버보다 DB를 먼저 준비한다. 구버전 서버는
새 테이블을 읽지 않으므로 롤링 호환이다. 롤백은 앱/서버 기능을 되돌리고 새 테이블은
보존하는 방식이 안전하다. 실제 삭제는 소비자가 없음을 확인한 별도 운영 작업이다.

## 2026-09-22 개발 음원 등록

- 사용자 제공 순서의 7개 원본: 잔잔한 빗소리, 거센 빗소리, 파도 소리, 숲속 소리, 도시 소리, 불 타는 소리, 백색 소음.
- 파일은 재압축·음량 보정·경계 편집 없이 사용. 총 64,385,278 bytes.
- [개발 등록 메타데이터](remote-bgm/dev-publication.json): 원본명, 3개 언어 제목, immutable URL, SHA256, 크기, MIME, 정렬. 운영 URL로 재사용하지 않는다.
- 개발 `bgm-assets`는 공개 읽기, 파일당 50 MiB, audio/mp4·audio/mpeg·audio/wav만 허용. 프로젝트 제한 때문에 100 MiB 버킷 생성은 거부돼 50 MiB로 생성했다. API의 100 MiB는 클라이언트 검증 상한이며 Storage 한도와는 별개다. 이번 원본은 모두 약 11 MiB 이하이다.
- 공개 URL 7개 모두 HTTP 200, redirect 없음, MIME·크기·SHA256 검증 후 트랜잭션으로 활성화. 기존 내장 6곡은 유지한다.
- 원본 간 음량 차이 및 일부 반복 경계의 저레벨 구간/샘플 불연속을 발견했다. 사용자가 원본으로 폰에서 들어본 뒤 보정 여부를 결정하기로 했다.
- 개발 DB만 변경했다. 운영 DB·Storage에는 등록하지 않았다.

### 2026-09-23 영어 제목 통일

사용자 요청에 따라 백색 소음 7개도 Lo-fi처럼 모든 앱 언어에서 영어 제목을 사용한다.
Light Rain, Heavy Rain, Ocean Waves, Forest Sounds, City Sounds, Crackling Fire, White Noise 순서다.
개발 DB의 title과 ko/en/ja title_i18n만 변경했다. 파일 URL·revision·hash·정렬은 그대로다.
한·영·일 카탈로그 서비스 응답을 개발 DB에서 읽어 7개 게시 metadata와 일치함을 확인했다.
