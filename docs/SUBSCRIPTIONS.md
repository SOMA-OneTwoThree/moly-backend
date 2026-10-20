# 구독·무료 이용·건초

권한 판정의 원본은 [entitlement.py](../app/services/entitlement.py), 결제 동기화는 [subscription.py](../app/services/subscription.py), 가입/체험/오퍼 RPC는 [schema.sql](../db/schema.sql)이다. 이 문서는 코드 계약을 설명한다. 국가별 실제 가격·스토어 체험 제공 여부·현재 환경 설정은 별도로 확인한다.

## 상품과 API 소유권

- 기본 구독은 월간·연간 두 플랜이다. `/subscription/plans`는 기본 product ID와 지급 건초를 반환하며 국가별 가격을 계산하지 않는다. 표시 가격·통화·구매 가능 여부는 현재 스토어/RevenueCat 상품 정보를 사용한다.
- `com.geniusjun.moly.plus.monthly`와 `.yearly`, `.monthly.trial`과 `.yearly.trial`을 같은 내부 monthly/yearly 플랜으로 연결한다. `.trial`이라는 이름만으로 체험 일수나 자격을 확정하지 않는다.
- backend는 인증된 `GET /subscription`, `GET /subscription/plans`, RevenueCat 인증의 `POST /webhooks/revenuecat`를 소유한다. 계정 `/me`, 앱 체험 시작·오퍼 요청 API는 moly-auth가 DB RPC를 통해 처리한다. 직접 StoreKit 검증·복원·ASSN 경로를 따로 되살리지 않는다.

## 서로 다른 무료 이용

| 구분 | 원본·조건 | 권한 의미 |
|---|---|---|
| 런칭 무료 | `app_config.free_launch_until` 이전 | `plan=trial`, `entitlement_source=launch`, `is_subscriber=false`. 구독 전용 테마·배너 광고 제거는 제공하지 않음 |
| 가입/앱 체험 | `signup_trial_until`, 가입 시각·프로필의 체험 구간 | `entitlement_source=signup_trial`. 스토어 결제 없이 부여되는 앱 권한 |
| 스토어 무료 체험 | 유효한 스토어 구독의 `store_trial_ends_at` | `entitlement_source=store_trial`, `is_subscriber=true`. 스토어 조건에 따른 구독 상태 |
| 유료 구독 | 유효한 active/grace_period 구독과 만료 시각 | `entitlement_source=subscription`, `is_subscriber=true` |

**`signup_trial_until` 이후 신규 가입에는 자동 가입 체험과 셀프 시작 앱 체험을 모두 제공하지 않는다.** 체험 코드를 삭제한 것은 아니며 cutoff와 계정 생성 시각으로 판정한다. 설정을 지우거나 null로 바꾸는 것은 재활성화 위험이 있는 정책 변경이다. 문서의 종료 설명으로 DB 현재 값 검사를 대신하지 않는다.

- `bootstrap_user`는 cutoff 미설정/null 또는 cutoff 이전 가입에만 최초 `가입 시각+48시간`을 기록한다. cutoff 이후 가입에는 자동 체험을 주지 않는다. 기존 체험을 다시 늘리지 않는다.
- `subscription_launch_access.self_trial_available`도 cutoff 이전 가입, 가입 후 48시간 이내, 미시작, 닉네임·구독·신규회원 조건을 함께 확인한다. cutoff 이후 신규 가입자는 버튼을 눌러 앱 체험을 새로 시작할 수도 없다.
- `start_subscription_trial`은 한 번만 시작하고 구간은 **버튼을 누른 때부터가 아니라 가입 시각부터 48시간**이다. 기존 체험/유효 구독이면 중복 시작하지 않는다. 이미 부여된 유효 체험은 cutoff 변경만으로 지우지 않는다.
- `free_launch_until` 미설정·잘못된 값은 런칭 종료로 판정한다. 반대로 `signup_trial_until` 미설정은 체험 허용 쪽이므로 두 키의 실패 의미를 혼동하지 않는다.
- 런칭 이전 계정의 자동 가입 체험과 기존 회원 스토어 오퍼는 분리한다. `subscription_launch.new_member_since/legacy_offer_expires_at`와 가입 시각이 경계를 정한다. 과거 구상에 있던 별도 enabled/tester 허용 목록을 현재 구현으로 안내하지 않는다.

## 이용 한도와 기능 권한

런칭 종료 이후 일일 **가중 사용량 단위**는 저장된 프로필 언어로 결정한다. 응답 언어 헤더나 메시지 수가 아니다. trial은 유료와 같은 한도를 쓴다.

| 프로필 언어 | free | trial/monthly/yearly |
|---|---:|---:|
| 영어 | 35,000 | 300,000 |
| 한국어 | 40,000 | 400,000 |
| 일본어 | 45,000 | 550,000 |

- free 계정은 가입한 활동일(`profiles.created_at`이 속한 로컬 04:00~다음날 04:00) 하루만 free 한도의 1.25배(영어 43,750·한국어 50,000·일본어 56,250)를 쓰고 `personal_diary_eligible`이 true다. 다음 활동일부터는 일반 free와 같다.
- 런칭 중에는 `free_launch_token_limit`을 쓰고 없으면 설정된 trial 한도를 사용한다. 이를 런칭 종료 이후 한도와 혼합하지 않는다.
- 구독 변경으로 당일 `tokens_used`를 초기화하지 않는다. 사용자 차감과 provider 비용 원장은 별도다. usage 미확인을 비용 0으로 보고하지 않는다.
- `personal_diary_eligible`은 등급 조건이며 실제 개인 일기 발행은 해당 활동일의 대화량·종료 시각 등 생성 조건을 추가 확인한다. 무료 사용자는 운영자 일기 대상이 될 수 있고, 기존 일기 열람 권한을 일괄 회수하지 않는다.
- `ads_removed`는 free/launch가 아닌 권한에서 true다. 운세 상세는 앱/스토어 체험과 구독에 포함되지만 런칭 무료만으로는 광고 해금하지 않는다. [운세 계약](DAILY-FORTUNE.md)을 함께 따른다.
- `subscriber_theme_unlocked`는 구독과 명시적으로 시작한 앱 체험에서 true다. 자동 가입 체험·런칭의 `plan=trial`만으로 구독 전용 상품을 해금하지 않는다. 상품별 서버 판정이 원본이다.
- BGM의 `subscriber_only`는 목록 메타데이터다. 서버 목록 조회 자체의 구독 필터/재생 차단과 같지 않다. [BGM 계약](REMOTE_BGM.md)을 따른다.

## 기존 회원 오퍼

- `subscription_offer_status`는 대상 여부와 실제 제공 준비를 구분한다. 현재 iOS 준비 결과는 false이며 활성 오퍼 등록은 Android만 허용한다.
- Android는 월간·연간 모두의 설정과 식별자가 준비돼야 제공 준비가 true다. 대상자여도 준비되지 않았다면 일반 유료 상품으로 몰래 대체하지 않는다.
- claim은 본인 프로필·닉네임·대상 기간을 확인하고 사용자 advisory lock 아래 멱등 처리한다. 미사용 Android claim의 플랜 변경과 이미 사용한 오퍼를 구분한다. claim만으로 결제·구독 권한이 생기지 않는다.
- 실제 스토어 오퍼 조건은 현재 스토어 설정을 함께 확인한다. DB 키를 바꾸며 새 할인·체험 정책을 도입하지 않는다.

## RevenueCat 처리와 회계 불변식

1. 웹훅 Authorization을 상수 시간 비교로 검증한 뒤 본문을 파싱한다. 미설정·불일치면 401이다. 본문 상한은 256 KiB, event ID는 필수·255자 이하이며 내구 inbox의 멱등키다.
2. inbox를 먼저 커밋하고 소비한다. 수신이 저장된 뒤 처리 실패는 durable pending/failed와 재처리로 관리한다. HTTP 200이 결제·보너스 처리가 모두 끝났다는 뜻은 아니다.
3. 구독 active/grace_period의 미래 만료 시각으로 권한을 판정한다. 자동 갱신 취소는 즉시 권한 회수가 아니며 만료까지 유지한다. 환불/철회는 해당 이벤트와 거래 근거에 따른다.
4. 역순 이벤트가 최신 상태를 되돌리지 않도록 시각·연장 예외를 검증한다. TRANSFER는 RC 현재 소유권을 확인해 접근 권한을 옮기되, 기존 payment·보너스 원장의 소유자를 새 사용자로 바꾸지 않는다.
5. 검증된 실제 유료 거래만 payment·보너스를 만든다. 무료 체험·가격 0 이벤트를 매출로 기록하지 않는다. 관련 거래/구독 과거 이력이 모호하면 보류·검토하며 수치를 맞추기 위해 지급하지 않는다.
6. 월간 최초 1,000·연간 최초 4,000 건초는 **사용자+플랜별 최초 유료 지급 한 번**이다. 갱신 때마다 주거나 store trial 시작에 지급하지 않는다. DB 유일성·검증된 store chain 근거를 유지한다.
7. 환불 회수는 실제 지급 원장 금액을 사용하며 멱등 처리한다. 내부 잔액은 부채로 음수가 될 수 있지만 API 지갑 잔액은 0 하한이다. 환불 취소도 이력을 삭제하지 않고 역분개한다.
8. 미등록 상품·누락 거래 ID·이력 보류는 환경/대상/권한을 확인한 뒤 처리한다. SANDBOX를 PRODUCTION 장애와 분리하되 sandbox라는 이유만으로 결제 증거를 삭제하거나 보류를 성공 처리하지 않는다.

재시도·alert·retention은 [OPERATIONS.md](OPERATIONS.md)를 따른다. raw 웹훅·식별자·provider 본문을 공개 PR/Slack 설명에 그대로 붙이지 않는다.

## 건초와 상점

원본: [economy.py](../app/services/economy.py), [hay_ledger.py](../app/services/hay_ledger.py), 상점 서비스와 지급 RPC.

- 출석 20, 루틴 두 개 완료 보상 20, 광고 한 번 20·하루 최대 5회다. 세 경로 합계 상한은 140이며 구독 최초 지급/IAP와 별도다. 과거 기획의 연속 출석 추가 보상이나 가입 이벤트를 현재 기능으로 간주하지 않는다.
- 보상일은 사용자 현지 달력일이며 타임존 역행 방어·사용자 락·일일 원장을 함께 적용한다. 앱이 직접 잔액을 증감하지 않는다.
- 보상 광고는 서버가 확인한 SSV로만 지급한다. 클라이언트 완료 콜백만으로 지급하지 않고 session·transaction 멱등성을 유지한다.
- 상점 구매·환불·보상은 지갑과 원장을 같은 트랜잭션에서 처리한다. 영구 구매 멱등키·결제 원장·소유권 이력을 TTL 정리에 포함하지 않는다.

## 변경과 검증

- 가격·상품·체험 cutoff·오퍼·배너/테마 권한은 별개의 정책이다. 구독 코드 변경에 seed/config 전체 덮어쓰기나 새 체험을 묶지 않는다.
- DB 함수·권한·상품 mapping을 코드와 대조하고, 신규/기존/체험 만료/구독 active·grace·취소·환불·TRANSFER·역순·가격 0·보너스 중복을 회귀 검증한다.
- 합성 fixture/가짜 RC 시험, 실제 inbox, 실제 스토어 구매·체험·복원을 구분한다. 단위시험 통과로 실결제/현지 가격을 인증하지 않는다.
- schema/RPC가 필요한 배포는 [운영 절차](OPERATIONS.md#배포-순서와-복구)와 [DB 절차](../db/README.md)를 따른다. 복구 때 구독/원장/소유권 이력을 삭제하지 않는다.
