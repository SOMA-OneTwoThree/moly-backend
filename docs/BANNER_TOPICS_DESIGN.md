# 배너 주제 대화

이 브랜치의 구현 계약이다. 배포·실기기 QA 완료 여부는 이 문서와 별도로 확인한다. 시각 규약은 [공동 규약](BANNER_SDUI_CONTRACT.md), HTTP 필드는 [topics.yaml](../openapi/components/topics.yaml)이 원본이다.

## 흐름

1. 홈에서 `POST /banners/resolve`가 사용자별 현재 질문과 디자인을 반환한다.
2. 하루 서로 다른 주제 두 개까지 준비한다. 첫 준비 성공 시 배너 포인터를 즉시 다음 주제로 옮기고, 두 번째 이후에는 그 주제를 유지한다. `대화하기`는 `POST /chat/topic-entries`로 클릭한 제안을 준비한다. 배너와 같은 질문을 캐피 말풍선으로 표시한다. 일반 인사·LLM 호출·토큰 차감은 없다.
3. 사용자가 첫 답변을 보내면 `POST /chat/messages`에 `text`, `topic_entry_id`, 실제 표시 `locale`를 보낸다. 서버가 고정 질문을 조회해 **이미 건넨 질문**이라는 문맥과 실제 답변을 기존 AI 호출에 연결한다.
4. 질문·사용자 말·캐피 답변·사용량·entry 완료를 하나의 트랜잭션으로 확정한다. 아직 같은 offer이면 완료 표시한다. 후속 전송은 일반 대화다.
5. 홈 복귀 시 서버의 현재 질문을 조회한다. 답변 성공은 추천 위치·횟수를 바꾸지 않는다. 답변한 두 번째 주제를 다시 누르면 기존 대화 이력을 보여주며 질문을 다시 생성하지 않는다.

질문은 `kind=topic_opening`, 사용자의 실제 말과 AI 답변은 기존 normal 메시지로 저장한다. 질문 자체를 사용자 사실/활동으로 추출하지 않는다. 실제 답변은 기존 기억·일기 파이프라인을 사용한다. 질문 문맥도 실제 AI 호출의 입력 토큰에 포함될 수 있다. 주제 시작은 이전 운세 대화 분류를 끝내고, 안전 대응은 유효하지 않은 선택적 주제 참조 때문에 차단하지 않는다.

## 주제 선택

| 조건 | 현재/다음 배너 |
|---|---|
| 처음 | A, 오늘 준비 횟수 0 |
| A 첫 준비 성공, 답변 여부 무관 | B, 횟수 1; 대화창 질문은 A 유지 |
| B 첫 준비 성공 | B, 횟수 2 |
| 같은 제안 재탭·재시도·답변 성공 | 위치·횟수 유지 |
| 현지 날짜 경과 | 현재 배너에서 한 개 이동, 횟수 0 |
| A 준비 후 B 미클릭 상태로 다음 날 / 여러 날 미접속 | C, 한 번의 날짜 전환만 적용 |
| 목록 끝 | 처음부터 새 offer로 순환 |
| 시간대 이동으로 이전 날짜 | 날짜·횟수·위치를 되돌리지 않음 |

클릭은 준비 API의 성공 트랜잭션으로 센다. 통신 응답 유실은 같은 키로 복구하고 중복 차감하지 않는다. 준비 성공 후 화면을 닫거나 답하지 않아도 소비된다. 첫 준비와 다음 offer 생성은 같은 트랜잭션이므로 홈 재조회 여부에 따라 다음 날 결과가 달라지지 않는다.

목록은 공통 순서이며 사용자마다 포인터와 일일 횟수를 저장한다. 한도를 채운 제안이 긴급 철회되면 당일 주제 카드만 숨기고 다음 현지 날짜에 재개한다. 추천할 다른 유효 질문이 없으면 억지로 같은 질문을 새 offer로 만들지 않는다.

날짜는 서버 UTC 시각 + `X-App-Timezone`의 현지 자정이다. 헤더 생략만 profile 시간대를 사용한다. 대화 한도·일기의 04:00 활동일과 별개다. 날짜 high-watermark는 감소하지 않는다.

질문 원본은 공통, 진행 위치는 사용자별이다. `topic_id`는 질문의 의미, `offer_id`는 사용자에게 건넨 한 번의 제안, `entry_id`는 준비한 채팅 질문이다. 같은 주제를 다음 바퀴에 만나면 offer가 다르다.

## 상태·동시성·복구

- `user_topic_states`: 사용자+placement당 현재 offer 한 행. topic ID/revision, 증가 sequence, ko/en/ja snapshot, 날짜 high-watermark, 실제 답변 완료 표시, daily_open_count(0..2), offer_opened를 저장한다. completed는 추천 전환 조건이 아니다.
- `chat_topic_entries`: pending/committed/superseded. 사용자당 pending 최대 하나, 사용자+offer당 실제 첫 답변 최대 하나. 질문/첫 사용자 메시지에 사용자 복합 FK를 둔다.
- 준비 기한은 `max(최초 준비 시점의 다음 현지 자정, 최초 준비 +30분)`. 재탭/언어 변경은 기한을 늘리지 않는다. 준비한 UTC 시각·현지 날짜·시간대·context revision을 고정한다.
- 유효하게 열린 A는 자정 후 B가 나와도 자기 기한까지 답할 수 있다. A의 성공은 B를 완료 처리하지 않는다. 실제로 B를 준비하거나 다른 일반 대화가 성공하면 이전 pending을 닫는다.
- 사용자 advisory lock과 기존 chat lease/context revision을 사용한다. 외부 추론 동안 DB 연결/락을 잡지 않는다. 정상 입장한 추론은 단순 기한 경과로 버리지 않으며 확정 때 소유권·삭제 장벽·문맥·미소비·철회를 다시 검사한다.
- 준비와 메시지는 별도 `Idempotency-Key`를 쓴다. 준비 키는 `topic-prepare:` namespace에 30일 보관하며 재시도 때 entry의 **현재 상태**를 읽는다. 첫 메시지는 기존 성공 응답 재생을 사용한다. 신규 필드 없는 메시지의 기존 request hash는 유지한다.
- pending 응답만 질문 content/locale를 갖는다. committed는 확정 대화의 message_id(보통 질문, 안전 응답으로 질문을 생략한 경우 첫 사용자 메시지), superseded는 답변 없는 종료 상태다. 최신 이력 병합은 메시지 ID로 중복 제거한다.
- `TOPIC_OFFER_UNAVAILABLE`(409), `TOPIC_ENTRY_UNAVAILABLE`(409/410)은 입력 보존 후 재확인/재선택한다. 타인 ID도 이 오류로 내용 없이 거절한다. `CHAT_TURN_IN_PROGRESS`/`CHAT_TURN_STALE`은 동일 요청을 유지한다.
- 네트워크·5xx 등 성공이 불명확하면 첫 답변의 text/entry/locale/key를 바꾸거나 참조 없는 요청을 자동 전송하지 않는다. 준비 실패 시 사용자가 ‘이 질문 없이 일반 대화하기’를 선택할 수 있으나, 첫 전송 미확정 상태는 먼저 복구해야 한다.
- 계정 삭제/잔존 검사에 두 테이블을 포함한다. 미응답 entry는 만료 후 30일이 지나면 기존 retention 작업이 정리한다. 답변한 entry는 메시지 삭제 시 cascade된다. 정리 작업 중단과 무관하게 런타임 기한 검사는 계속한다.

## API와 호환성

`POST /banners/resolve`: placement=`home_blind`, schema_version=1, platform, app_version, capabilities를 JSON으로 받는다. 언어/시간대는 기존 헤더다. 지원되는 주제 카드가 있을 때만 상태를 만들거나 전진시킨다. 응답 형식은 기존 BannerFeed이며 `private, no-store`다.

`POST /chat/topic-entries`: 같은 client context + banner_id + topic_ref, 별도 Idempotency-Key. topic_ref는 offer_id(UUID), offer_sequence(양의 정수), topic_id, topic_revision(SHA256), locale(ko/en/ja)다. 클라이언트 질문 원문은 받지 않는다. 현재 노출 후보와 offer를 대조한다. 첫 클릭 후 이미 B로 이동했더라도 같은 사용자·전체 참조·문맥의 유효 pending A는 복구한다. 답변한 현재 offer는 기존 entry를 반환한다. 오래된 임의 offer를 새로 준비하지 않는다.

`POST /chat/messages`: 첫 답변에만 topic_entry_id/locale. greeting_id와 운세 context_ref는 동시에 지정할 수 없다. locale는 준비된 질문의 언어로 고정한다.

`open_topic_conversation_v1` capability가 없는 앱에는 주제 카드를 제외한다. 기존 GET /banners는 읽기 전용 호환 경로이며 주제 진행을 하지 않는다. 신규 앱은 명시적 경로 미지원(404/405)·schema 미지원만 기존 경로로 복구하고, 임의의 4xx/연결 실패를 미지원으로 취급하지 않는다. 롤백 catalog에 사용자 topic ID가 없으면 상태를 보존하고 해당 주제 카드를 숨긴다.

## 질문과 디자인 편집

| 편집 대상 | 원본 |
|---|---|
| 질문·분류·ko/en/ja·순서 | `app/resources/conversation_topics/catalog.json` |
| 배경·버튼·글꼴·좌표 | `app/resources/banners/home_blind.json` |
| 이미지 bytes | 환경별 공개 `banner-assets` bucket |

현재 추천 대상은 사용자 검수한 보편적인 질문 50개와 ko/en/ja 원문이다. 철회한 질문 원문은 파일에서 삭제할 수 있다. 현재 주제는 50개이며, 삭제한 주제는 진행 위치 연결용 sequence ID/revision과 revoked 표시만 남긴다. 기존 대화 원문은 DB snapshot으로 유지한다. 생성 배치·개인화는 포함하지 않는다. 정상 질문 전환에 매일 배포나 배치는 필요 없다.

질문마다 canvas를 복사하지 않는다. 공통 배너에 `bindings.question = {"source":"topic.question","format":null}`, 질문 text를 `{"kind":"template","value":"{question}"}`, action을 `{"type":"open_topic_conversation_v1"}`로 둔다. 질문은 다시 template로 실행하지 않는 원문이며 action 참조는 서버가 채운다.

- 질문은 ko/en/ja 모두 짧고 자연스러운 한 질문으로 작성한다. 언어당 1..120자, NFC, 줄바꿈 없는 일반 문장이다. 배너에서 표현용 줄바꿈을 한다.
- 같은 의미의 문구 수정은 기존 versions를 보존하고 새 revision을 추가한다. revision은 `questions`의 키 정렬·공백 없는 UTF-8 JSON SHA256이다. 활성 offer/entry 원문은 바뀌지 않고 다음 제안부터 새 revision을 사용한다.
- 의미가 다른 질문은 새 ID로 추가한다. 기본 갱신은 sequence 말미 추가만 허용한다. 검토한 순서 재배치는 `--allow-topic-reorder`로 명시한다. 기존 사용자는 현재 ID를 유지하고 다음 전환부터 새 순서를 따른다. 삭제한 ID도 연결용으로 남기며 공개된 version 덮어쓰기·철회 제거는 금지한다. 철회된 version의 원문은 삭제할 수 있다.
- 새 주제를 추가해도 기존 주제가 자동으로 빠지지 않는다. 인기 집계·자동 제외 기능은 없으며 운영자가 제외 대상을 결정한다.
- 철회는 revoked의 ID/revision으로 표시한다. 다음 선택·첫 답변 검증에서 제외한다. 다른 유효 질문이 없으면 주제 카드만 숨긴다.

```sh
uv run python scripts/validate_banners.py --assets --environment dev --previous-topics /path/to/previous-catalog.json
```

직전 배포 파일을 `--previous-topics`로 제공해 ID·미철회 revision 원문·철회 표시 보존까지 검사한다. 순서를 검토 후 섞었을 때만 `--allow-topic-reorder`를 함께 지정한다. 이 옵션도 기존 ID 삭제를 허용하지 않는다. 생략하면 현재 파일의 자체 검증만 한다. 검증기는 보존된 모든 질문 version × ko/en/ja canvas를 조립하고 실제 이미지 메타데이터를 검사한다. 글꼴 overflow·대비·말투·AI 대화 품질은 dev TestFlight에서 별도 검수한다.

## 배포

1. 서버 배포 전에 `db/schema.sql`을 기준으로 기존 user_topic_states에 daily_open_count/offer_opened와 0..2 제약을 추가하는 차이 SQL을 별도 리뷰 산출물로 준비한다. 기존 사용자 진행 위치·질문·대화는 보존하며 새 필드는 0/false로 시작한다. 과거 클릭을 추측해 소급 차감하지 않는다. `db/README.md`의 dev 적용 절차를 따르고 `uv run python -m db.verify --env dev`로 확인한다. baseline을 기존 DB에 실행하지 않는다. RLS deny-default, anon/authenticated 권한 없음이 필요하다.
2. 질문·배너를 함께 검증하고 서버 dev에 배포한다. 실행 중 `/health/banners`는 인증된 진단 경로로 banner revision과 topic_revision을 제공한다. `scripts/check_running_banners.py`가 이미지와 실행 프로세스의 두 hash를 대조한다.
3. 지원하는 dev TestFlight로 첫 진입·하루 2개 준비·50개 순환·재시도·앱 복귀·자정·언어·기존 대화 회귀를 확인한다. 운영 배포/main 통합은 별도다.

롤백은 사용자 cursor/entry나 확장된 messages kind 제약을 되돌리지 않는다. 이전 버전·철회 기록을 보존한 호환 catalog로 복구한다. 대화 품질 검수는 첫 답변, 후속 2턴, 화제 전환, 답하기 싫음, 위기 표현을 포함한다.

## 대화 재진입

답변을 보내지 않은 주제 화면을 나가면 질문 표시를 지운다. 홈의 일반 대화로 재진입할 때 이전 질문을 복원하거나 답변에 첨부하지 않는다. 배너를 명시적으로 다시 누르면 해당 주제를 준비한다. 이미 전송한 답변의 성공 여부가 불명확한 경우에는 중복 전송 방지를 위해 기존 요청·주제 참조의 상태 복구를 유지한다.

첫 발화가 다른 화제여도 사용자 말을 우선하고 답을 강요하지 않는다. 첫 답변 성공 시 대화 완료만 기록하며 추천 횟수나 위치를 추가 변경하지 않는다.

## 준비 실패 처리

클라이언트는 준비 요청의 연결/시간 초과 또는 HTTP 408·502·503·504에 한해 250ms 뒤 동일한 요청 키와 본문으로 한 번 재시도한다. 4xx 업무 오류·만료·지원 부재에는 적용하지 않는다. 준비된 pending 질문은 부가적인 이력 조회 실패만으로 숨기지 않는다. committed 상태의 기록 복구 실패는 성공으로 간주하지 않는다. 영구적인 네트워크 장애는 재시도/일반 대화 안내로 처리한다.
