# 원격 배경음악

서버는 전체 활성 곡 목록을, Storage는 음원 바이트를 소유한다. 앱 내장 곡의 ID와 파일은 유지하며 원격 곡은 목록 API로 발견한다. 실제 재생·기기 다운로드 동작은 앱 검증과 구분한다.

## 목록 계약

인증된 `GET /bgm/tracks`와 `X-App-Locale`을 사용한다. 응답은 `Cache-Control: private, no-store`이며 다음 전체 snapshot이다.

```json
{"schema_version": 1, "revision": "<전체 목록 SHA256>", "tracks": []}
```

| 필드 | 계약 |
|---|---|
| `id` | 안정 ID. 소문자 영숫자로 시작, 영숫자/하이픈 최대 64자 |
| `category` | `lofi` 또는 `white_noise` |
| `title` | ko/en/ja로 해석한 제목, 1~120자 |
| `source` | `bundled` 또는 `remote` |
| `revision` | 음원 버전, 영숫자로 시작하는 영숫자/점/밑줄/하이픈 최대 64자 |
| `url/sha256/size_bytes/mime_type` | bundled은 모두 null, remote는 모두 필수 |
| `sort_order` | 0 이상 정수 |
| `subscriber_only` | 구독 전용 표시. 기본 false |

- 목록 revision은 **현지화된 전체 목록**의 canonical JSON SHA256이다. 음원 identity는 곡의 revision/hash다. 제목·정렬·언어 변경만으로 파일을 재다운로드하지 않는다.
- 한 SQL로 전체 활성 행을 읽고 한 행이라도 잘못되면 전체 요청이 실패한다. 정상 partial/empty로 위장하지 않는다. 정상 빈 목록은 실제 전체 삭제이므로 운영 시 주의한다.
- 성공 snapshot에서 사라진 곡은 목록에서 제거한다. 실패·불완전한 응답은 삭제 신호가 아니며 기존 snapshot/파일을 보존한다. 첫 성공 전에는 앱 내장 fallback을 사용할 수 있다.
- `subscriber_only=true`도 인증된 목록에 포함된다. **backend는 구독별 곡 필터나 음원 재생 차단을 하지 않는다.** 앱의 잠금·셔플·재생 paywall은 별도 구현/검증 대상이며 구버전은 새 필드를 무시할 수 있다.

## 음원과 앱 계약

- remote URL은 환경별 허용 origin의 공개 `bgm-assets` 경로만 허용한다. query/fragment·상대 경로·인증 URL을 쓰지 않는다. 운영 응답에 개발 origin을 섞지 않는다.
- 지원 MIME은 `audio/mp4`, `audio/mpeg`, `audio/wav`, 개별 파일 상한은 100 MiB다. 이 방어 한도가 모든 codec/컨테이너의 재생을 보장하지는 않는다. Storage bucket 자체 제한도 별도로 확인한다.
- API가 음원을 프록시하지 않으며 앱 Bearer/cookie를 Storage로 전달하지 않는다. 파일 이름은 새 revision/hash의 불변 경로로 만든다. 같은 URL의 bytes를 덮지 않는다.
- 앱은 임시 파일의 size/hash를 확인한 뒤 원자적으로 교체한다. 재생 중인 이전 파일은 다음 재생부터 바꾸고 사용이 끝나기 전에 삭제하지 않는다. 단곡 반복이며 Lo-fi와 백색 소음 동시 재생 계약은 아니다.
- 다운로드는 앱 실행 중 시작한다. 앱 종료/OS suspension 중 background transfer 완료를 보장하지 않는다. 최초 다운로드 전에는 원격 곡을 오프라인 재생할 수 없다.
- 로그아웃 때 공용 음원 캐시를 반드시 삭제할 필요는 없다. 개인정보 캐시와 구분한다.

## 등록·교체·제거

1. 재배포 권리, 양 플랫폼 디코딩, 반복 경계의 무음·클릭을 검수한다.
2. 환경별 공개 bucket에 새 불변 파일을 올리고 확정 bytes의 SHA256·크기·MIME와 세 언어 제목을 준비한다.
3. 한 API track 형태의 JSON을 검증한다. 아래 도구는 파일 HTTP 읽기를 수행하며 DB를 변경하지 않는다.

```bash
uv run python -m scripts.validate_bgm_publication metadata.json
uv run python -m scripts.validate_bgm_publication metadata.json --production
```

4. 공개 HTTP 200·redirect 없음·MIME·size/hash와 실제 기기 재생을 확인한 뒤 DB metadata를 트랜잭션으로 등록하고 `is_active=true`를 마지막에 설정한다.
5. 교체는 같은 ID와 새 revision/URL/hash를 사용한다. 제거는 비활성화 또는 검토한 행 삭제다. 다음 목록 조회에 반영되며 실행 중 모든 앱에 즉시 push하지 않는다.
6. 이전 snapshot을 받은 앱의 다운로드를 위해 이전 파일은 유예 기간을 두고 보존한다. 긴급 복구 때 테이블·음원을 먼저 삭제하지 않는다.

기존 계약 안의 곡 추가/메타데이터 변경은 앱·서버 재배포 없이 가능하다. 새 category/source/codec 계약은 앱 지원을 먼저 확인한다. DB 변경이 필요하면 [DB 절차](../db/README.md)에 따라 현재 계약과 필요한 SQL 차이만 검토하며 과거 seed/전환 SQL을 통째 재실행하지 않는다.

## 배너 연결

`open_music`은 선택 화면만 열며 자동 선택/재생하지 않는다. `music.daily_title`은 음악 binding을 요청한 카드에서만 사용한다. `remote_bgm_v1` 지원 앱에는 활성 DB 목록을, 그 외에는 내장 목록을 사용한다. 같은 날짜·같은 활성 ID 집합이면 추천이 같고 목록 변경 시 달라질 수 있다.

잘못되거나 빈 음악 목록은 음악 카드만 제외한다. 카드 유무는 `home_blind.json`이 결정하며 이 문서의 곡 수·과거 게시 기록으로 판단하지 않는다. [배너 계약](BANNER_SDUI.md)을 따른다.

## 원본과 검증

- API/스키마: [bgm.py](../app/api/bgm.py), [응답 스키마](../app/schemas/bgm.py).
- 조회·현지화·환경 검증: [서비스](../app/services/bgm.py).
- DB: `bgm_tracks`, `db/schema.sql`, `db/schema_contract.json`.
- 검증은 목록 정상/빈 목록/잘못된 행/개발 URL 혼입/구독 표시/파일 변경을 구분한다. metadata 검증·서버 단위시험·실기기 반복 청취는 서로 다른 증거다.
