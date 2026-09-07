# 배너 주제 대화

이 브랜치의 구현 계약이다. 배포·실기기 QA 완료 여부는 이 문서와 별도로 확인한다. 시각 규약은 [공동 규약](BANNER_SDUI_CONTRACT.md), HTTP 필드는 [topics.yaml](../openapi/components/topics.yaml)이 원본이다.

## 흐름

1. 홈에서 `POST /banners/resolve`가 사용자별 현재 질문과 디자인을 반환한다.
2. `대화하기`는 `POST /chat/topic-entries`로 클릭한 제안을 준비한다. 배너와 같은 질문을 캐피 말풍선으로 표시한다. 일반 인사·LLM 호출·토큰 차감은 없다.
3. 사용자가 첫 답변을 보내면 `POST /chat/messages`에 `text`, `topic_entry_id`, 실제 표시 `locale`를 보낸다. 서버가 고정 질문을 조회해 **이미 건넨 질문**이라는 문맥과 실제 답변을 기존 AI 호출에 연결한다.
4. 질문·사용자 말·캐피 답변·사용량·entry 완료를 하나의 트랜잭션으로 확정한다. 아직 같은 offer이면 완료 표시한다. 후속 전송은 일반 대화다.
5. 다음 홈 조회에서 다음 질문을 고른다. 대화 중 숨겨진 배너를 갱신해 질문을 미리 소모하지 않는다.

질문은 `kind=topic_opening`, 사용자의 실제 말과 AI 답변은 기존 normal 메시지로 저장한다. 질문 자체를 사용자 사실/활동으로 추출하지 않는다. 실제 답변은 기존 기억·일기 파이프라인을 사용한다. 질문 문맥도 실제 AI 호출의 입력 토큰에 포함될 수 있다. 주제 시작은 이전 운세 대화 분류를 끝내고, 안전 대응은 유효하지 않은 선택적 주제 참조 때문에 차단하지 않는다.

## 주제 선택

| 조건 | 다음 resolve |
|---|---|
| 처음 | 첫 질문 |
| 같은 날, 미완료 | 같은 offer |
| 첫 답변 성공 **또는** 현지 날짜 경과 | 다음 질문으로 한 번 |
| 두 조건 동시 / 여러 날 미접속 | 역시 한 번 |
| 목록 끝 | 처음부터 새 offer로 순환 |
| 시간대 이동으로 이전 날짜 | 되돌리거나 다시 선택하지 않음 |

날짜는 서버 UTC 시각 + `X-App-Timezone`의 현지 자정이다. 헤더 생략만 profile 시간대를 사용한다. 대화 한도·일기의 04:00 활동일과 별개다. 날짜 high-watermark는 감소하지 않는다.

질문 원본은 공통, 진행 위치는 사용자별이다. `topic_id`는 질문의 의미, `offer_id`는 사용자에게 건넨 한 번의 제안, `entry_id`는 준비한 채팅 질문이다. 같은 주제를 다음 바퀴에 만나면 offer가 다르다.

## 상태·동시성·복구

- `user_topic_states`: 사용자+placement당 현재 offer 한 행. topic ID/revision, 증가 sequence, ko/en/ja snapshot, 날짜 high-watermark, 완료 표시를 저장한다.
- `chat_topic_entries`: pending/committed/superseded. 사용자당 pending 최대 하나, 사용자+offer당 실제 첫 답변 최대 하나. 질문/첫 사용자 메시지에 사용자 복합 FK를 둔다.
- 준비 기한은 `max(최초 준비 시점의 다음 현지 자정, 최초 준비 +30분)`. 재탭/언어 변경은 기한을 늘리지 않는다. 준비한 UTC 시각·현지 날짜·시간대·context revision을 고정한다.
- 유효하게 열린 A는 자정 후 B가 나와도 자기 기한까지 답할 수 있다. A의 성공은 B를 완료 처리하지 않는다. 실제로 B를 준비하거나 다른 일반 대화가 성공하면 이전 pending을 닫는다.
- 사용자 advisory lock과 기존 chat lease/context revision을 사용한다. 외부 추론 동안 DB 연결/락을 잡지 않는다. 정상 입장한 추론은 단순 기한 경과로 버리지 않으며 확정 때 소유권·삭제 장벽·문맥·미소비·철회를 다시 검사한다.
- 준비와 메시지는 별도 `Idempotency-Key`를 쓴다. 준비 키는 `topic-prepare:` namespace에 30일 보관하며 재시도 때 entry의 **현재 상태**를 읽는다. 첫 메시지는 기존 성공 응답 재생을 사용한다. 신규 필드 없는 메시지의 기존 request hash는 유지한다.
- pending 응답만 질문 content/locale를 갖는다. committed는 확정 질문 message_id, superseded는 질문 없는 종료 상태다. 최신 이력 병합은 메시지 ID로 중복 제거한다.
- `TOPIC_OFFER_UNAVAILABLE`(409), `TOPIC_ENTRY_UNAVAILABLE`(409/410)은 입력 보존 후 재확인/재선택한다. 타인 ID도 이 오류로 내용 없이 거절한다. `CHAT_TURN_IN_PROGRESS`/`CHAT_TURN_STALE`은 동일 요청을 유지한다.
- 네트워크·5xx 등 성공이 불명확하면 첫 답변의 text/entry/locale/key를 바꾸거나 참조 없는 요청을 자동 전송하지 않는다. 준비 실패 시 사용자가 ‘이 질문 없이 일반 대화하기’를 선택할 수 있으나, 첫 전송 미확정 상태는 먼저 복구해야 한다.
- 계정 삭제/잔존 검사에 두 테이블을 포함한다. 미응답 entry는 만료 후 30일이 지나면 기존 retention 작업이 정리한다. 답변한 entry는 메시지 삭제 시 cascade된다. 정리 작업 중단과 무관하게 런타임 기한 검사는 계속한다.

## API와 호환성

`POST /banners/resolve`: placement=`home_blind`, schema_version=1, platform, app_version, capabilities를 JSON으로 받는다. 언어/시간대는 기존 헤더다. 지원되는 주제 카드가 있을 때만 상태를 만들거나 전진시킨다. 응답 형식은 기존 BannerFeed이며 `private, no-store`다.

`POST /chat/topic-entries`: 같은 client context + banner_id + topic_ref, 별도 Idempotency-Key. topic_ref는 offer_id(UUID), offer_sequence(양의 정수), topic_id, topic_revision(SHA256), locale(ko/en/ja)다. 클라이언트 질문 원문은 받지 않는다. 현재 노출 후보와 현재 offer를 대조하며 같은 유효 질문은 같은 entry로 수렴한다.

`POST /chat/messages`: 첫 답변에만 topic_entry_id/locale. greeting_id와 운세 context_ref는 동시에 지정할 수 없다. locale는 준비된 질문의 언어로 고정한다.

`open_topic_conversation_v1` capability가 없는 앱에는 주제 카드를 제외한다. 기존 GET /banners는 읽기 전용 호환 경로이며 주제 진행을 하지 않는다. 신규 앱은 명시적 경로 미지원(404/405)·schema 미지원만 기존 경로로 복구하고, 임의의 4xx/연결 실패를 미지원으로 취급하지 않는다. 롤백 catalog에 사용자 topic ID가 없으면 상태를 보존하고 해당 주제 카드를 숨긴다.

## 질문과 디자인 편집

| 편집 대상 | 원본 |
|---|---|
| 질문·분류·ko/en/ja·순서 | `app/resources/conversation_topics/catalog.json` |
| 배경·버튼·글꼴·좌표 | `app/resources/banners/home_blind.json` |
| 이미지 bytes | 환경별 공개 `banner-assets` bucket |

현재 catalog는 검증용 3개다. 본편 약 90개 작성, 생성 배치, 개인화는 별도다. 정상 질문 전환에 매일 배포나 배치는 필요 없다.

질문마다 canvas를 복사하지 않는다. 공통 배너에 `bindings.question = {"source":"topic.question","format":null}`, 질문 text를 `{"kind":"template","value":"{question}"}`, action을 `{"type":"open_topic_conversation_v1"}`로 둔다. 질문은 다시 template로 실행하지 않는 원문이며 action 참조는 서버가 채운다.

- 질문은 ko/en/ja 모두 짧고 자연스러운 한 질문으로 작성한다. 언어당 1..120자, NFC, 줄바꿈 없는 일반 문장이다. 배너에서 표현용 줄바꿈을 한다.
- 같은 의미의 문구 수정은 기존 versions를 보존하고 새 revision을 추가한다. revision은 `questions`의 키 정렬·공백 없는 UTF-8 JSON SHA256이다. 활성 offer/entry 원문은 바뀌지 않고 다음 제안부터 새 revision을 사용한다.
- 의미가 다른 질문은 새 ID로 sequence 말미에 추가한다. 기존 ID의 순서 변경/삭제, 공개된 version 덮어쓰기, 철회 제거는 허용하지 않는다.
- 철회는 revoked의 ID/revision으로 표시한다. 다음 선택·첫 답변 검증에서 제외한다. 다른 유효 질문이 없으면 주제 카드만 숨긴다.

```sh
uv run python scripts/validate_banners.py --assets --environment dev --previous-topics /path/to/previous-catalog.json
```

직전 배포 파일을 `--previous-topics`로 제공해 순서·기존 revision·철회 보존까지 검사한다. 생략하면 현재 파일의 자체 검증만 한다. 검증기는 보존된 모든 질문 version × ko/en/ja canvas를 조립하고 실제 이미지 메타데이터를 검사한다. 글꼴 overflow·대비·말투·AI 대화 품질은 dev TestFlight에서 별도 검수한다.

## 배포

1. 신규 API를 올리기 전에 대상 dev DB에 `20260907_banner_topic_conversation.sql`과 `20260907_topic_kind_constraint_prepare.sql` → `validate.sql` → `swap.sql` 순서의 세 제약 확장을 적용한다(모두 `db/migrations/`). 기존 운세 kind 제약 확장이 선행되어야 한다. `PYTHONPATH=. uv run python db/verify.py --env dev`로 확인한다. RLS deny-default, anon/authenticated 권한 없음이 필요하다.
2. 질문·배너를 함께 검증하고 서버 dev에 배포한다. 실행 중 `/health/banners`는 인증된 진단 경로로 banner revision과 topic_revision을 제공한다. `scripts/check_running_banners.py`가 이미지와 실행 프로세스의 두 hash를 대조한다.
3. 지원하는 dev TestFlight로 첫 진입·3개 순환·재시도·앱 복귀·자정·언어·기존 대화 회귀를 확인한다. 운영 배포/main 통합은 별도다.

롤백은 사용자 cursor/entry나 확장된 messages kind 제약을 되돌리지 않는다. 이전 버전·철회 기록을 보존한 호환 catalog로 복구한다. 대화 품질 검수는 첫 답변, 후속 2턴, 화제 전환, 답하기 싫음, 위기 표현을 포함한다.
