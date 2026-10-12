# 피드백 알림의 한국어 번역

## 동작

인앱 피드백 저장과 204 응답 계약은 유지한다. 기존 FastAPI `BackgroundTasks`에서 필요한 경우 원문을 한국어로 번역한 뒤 원문과 번역을 한 Slack 메시지로 보낸다. 번역 실패가 원문 전달을 막지 않는다. 새 잡 종류·큐·DB 컬럼·client idempotency 계약은 추가하지 않는다.

원문은 자르거나 번역으로 대체하지 않는다. 별도 연락처·닉네임·사용자 식별자 필드는 번역 모델에 보내지 않는다. 사용자가 피드백 본문 안에 직접 적은 내용은 원문의 일부이며 별도 개인정보 추론·삭제는 하지 않는다. 번역은 기계 번역임을 표시하며, 제공할 수 없는 경우 원문과 짧은 번역 미제공 표시만 보낸다.

## 호출 범위와 비용

- 비문자 입력과 명확한 한국어 입력은 번역 호출을 생략한다. 간단한 문자 판별만 사용하며 별도의 언어 판별 LLM은 호출하지 않는다.
- 외국어·혼합 입력은 한 번 번역한다. 프로필 locale만으로 본문 언어를 단정하지 않는다.
- 기존 utility 모델과 기존 usage ledger를 사용한다. purpose는 `feedback_translate`, lane은 background이며 사용자 quota를 차감하지 않는다.
- feedback 전용 어댑터에서 SDK 재시도를 끄고 create를 최대 한 번 호출한다. 전체 번역 timeout과 출력 상한을 둔다. 대화용 전역 클라이언트 설정이나 LLM wrapper 동작을 바꾸지 않는다.
- 번역 호출은 전체 5초, 출력 2,048 tokens/4,000자로 제한하고 원장 종료 기록에는 별도 1초 한도를 둔다. [공식 Python SDK의 요청별 옵션](https://developers.openai.com/api/reference/python)에 따라 `with_options(max_retries=0)`을 사용한다.
- provider/model 미지원, 키 미설정, timeout, 빈 응답, 출력 잘림·상한 초과는 원문으로 fallback한다. fallback을 위한 재번역은 하지 않는다.
- 원문과 동일한 번역은 중복해 표시하지 않는다. Slack webhook이 없으면 유료 번역도 수행하지 않는다.

## 원문·주입·Slack 경계

번역 system 지침은 입력을 번역할 데이터로만 취급하며, 입력 안의 지시·역할 변경·외부 조회 요구를 따르지 않도록 한다. 도구나 대화 이력은 제공하지 않는다. 출력의 사실성은 완벽하게 보장할 수 없으므로 원문을 함께 제공해 검토할 수 있게 한다.

원문, 번역, 닉네임, 연락처 등 사용자 제어 문자열은 Slack의 `&`, `<`, `>`를 escape한다. 사용자·채널·전체 알림 mention 구문과 링크 마크업이 실행되지 않게 하고 링크 unfurl도 끈다. 입력 원문 텍스트의 내용은 유지한다.

오류 로그에는 상태 코드·오류 유형·처리 결과만 남긴다. SDK 예외 repr, webhook URL, 응답 body, 피드백 원문, 연락처, 사용자 식별자를 추가 로그에 넣지 않는다. Slack 전송 실패는 피드백 저장 성공을 되돌리지 않는다.

HTTPX INFO의 요청 URL과 OpenAI/httpcore DEBUG의 원문 노출도 차단한다. 설치 SDK의 실제 전송 logger에 필터를 붙이고 `ContextVar`로 해당 알림/번역 호출에서만 적용한다. 다른 동시 요청의 로그 레벨을 바꾸지 않는다.

## 중복과 내구성

한 background task는 원문/번역을 합친 Slack 메시지 하나만 만든다. 원문을 먼저 보내고 번역을 별도 후속 전송하지 않는다. 동일 POST 재전송은 기존처럼 별도 피드백 저장·알림이 될 수 있다. 본문 hash로 정당한 반복 제출을 제거하지 않는다.

BackgroundTasks는 durable queue가 아니다. 저장 후 프로세스가 종료되면 알림이 유실될 수 있고, Slack 성공 여부가 불명확할 때 exactly-once를 보장하지 않는다. 이번 변경은 기존 내구성·요청 중복 계약을 확장하지 않는다. 번역에는 별도 자동 재시도를 추가하지 않는다.

## 수용 기준

- 한국어, 영어, 일본어, 혼합 언어, emoji/숫자, 최대 길이 원문에서 생략/호출 경로가 맞고 원문이 보존된다.
- 비한국어 정상 번역은 원문+한국어 한 메시지이며 별도 연락처·닉네임·식별자 필드와 대화 이력이 모델 입력에 없다.
- timeout, SDK 오류, 빈 응답, 잘림, 과도한 출력, 미지원 모델, webhook 없음에서 유료 호출 수·fallback·Slack 전송 수가 계약을 따른다.
- 명령형·역할 변경 입력도 번역 데이터로 전달되고 모델 도구는 없다. mention/link 특수 구문은 원문과 번역 모두에서 escape된다.
- API 응답과 저장은 번역/Slack 오류에 영향받지 않는다. 번역 모델 호출은 최대 한 번이며 SDK 재시도도 꺼져 있다.
- 사용량은 기존 ledger에 별도 purpose로 계측하며, 계측 실패가 원문 알림을 막지 않는다. 사용자 quota는 바뀌지 않는다.
- 테스트는 합성 피드백만 사용하고 Slack은 mock/dry-run으로 처리한다. 실제 사용자 내용이나 비밀값을 문서·PR·로그에 넣지 않는다.

## 적용 범위

개발 소스 기준은 `6fb3ea72`이며 원본 작업 디렉터리와 기존 미추적 문서를 보존한다. 구현·검증·수정 완료 뒤 최종 PR을 만든다. 운영 DB·운영 배포·실제 Slack 발송은 이 작업에서 수행하지 않는다. 알림 정책의 지속 incident 상태는 별도 요구이며, 피드백에 같은 상태 머신이나 새 consumer rollout을 붙이지 않는다.

## 개발에서 번역 품질 확인

`POST /dev/feedback-translation-eval`은 development + `enable_dev_routes` + 기존 operator allowlist에서만 사용할 수 있다. 입력은 고정 `case_id` 다섯 개(`ko`, `en`, `ja`, `mixed`, `instruction`)뿐이며 임의 본문은 422로 거절한다. 서버의 새 합성 본문을 제품과 같은 `needs_translation`/`_translate` 및 같은 prompt로 실행한다. 사용자 데이터 조회·피드백 저장·Slack 전송은 하지 않는다.

평가 route는 `gpt-6-luna`와 다른 utility 설정을 호출 전에 거절한다. 실제 실험 전 공식 모델 지원·가격과 배포 SHA를 다시 확인한다. 응답에는 configured model/배포 SHA와 `skipped`/`translated`/`fallback`을 구분한다. 응답의 configured model을 실제 model snapshot이나 실측 usage로 주장하지 않는다. 원장에는 고정 `feedback-translation-eval-v1` experiment ID를 붙이고 제품 호출에는 experiment ID를 붙이지 않는다.

첫 비교는 한국어 생략 1건과 나머지 4건을 각 1회 실행한다. 총 provider 시도 상한 10회·예산 $1 안에서만 필요한 후속 검증을 하며 자동 재시도나 무작정 반복하지 않는다. 정확한 뜻·불확실성·기술 용어 보존, 자연스러운 한국어, 혼합 입력의 기존 한국어 보존, 지시문도 번역할 데이터로 취급하는지를 읽고 판단한다. fallback은 원문 보존의 정상 실패 경로지만 번역 품질 성공으로 세지 않는다. 이 문서의 설계·단위/DB 검증만으로 실제 모델 품질이 검증됐다고 주장하지 않는다.
