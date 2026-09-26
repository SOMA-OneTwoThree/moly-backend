# 구독 출시

스위치는 `app_config.free_launch_until`(출시 시각 T) 하나다. 활성화 플래그나 테스트 계정 명단은 없다.

## 한도

| 일 한도 | 영어 | 한국어 | 일본어 |
|---|---:|---:|---:|
| 무료 (T 이후) | 40,000 | 50,000 | 60,000 |
| 가입 체험 48시간·스토어 체험·유료 구독 | 300,000 | 400,000 | 550,000 |

- T 전에는 구독·앱 체험이 없는 계정 전원이 런칭 무료(150,000)다. 구독·체험 한도는 T 전에도 같다.
- 가중 차감 단위이며 저장된 `profiles.language`를 쓴다. 등급이 바뀌어도 당일 사용량을 초기화하지 않는다.
- T 이후 무료는 새 개인 일기를 받지 않고 운영 일기만 받는다. 운세 상세는 유료 구독과 가입 체험에 광고 없이 제공한다.

## 신규 회원과 기존 회원

| | 출시 전 (지금 < T) | 출시 후 |
|---|---|---|
| 신규 회원: 1개월 무료 없는 페이월, 가입 후 48시간 체험 | `subscription_launch.new_member_since` 이후 가입 | T 이후 가입 |
| 기존 회원: 1개월 무료 페이월 | `new_member_since` 이전 가입 | T 이전 가입 |

- 48시간 체험을 시작한 계정은 계속 신규 회원이다.
- 구버전 앱은 `/me`의 `subscription_rollout`을 읽지 않고 체험·오퍼 API도 부르지 않는다. 그래서 출시 전에 구독을 열어 둬도 구버전 사용자에게는 달라지는 것이 없다.
- 새 앱을 쓰는 테스터·심사자는 `new_member_since` 이전 계정으로 기존 회원 페이월을, 이후 계정으로 신규 회원 페이월을 본다. 심사 노트에 두 데모 계정을 적는다.
- iOS의 1개월 무료는 샌드박스 Apple ID가 무료 체험을 쓴 적이 없어야 보인다. Android 오퍼는 `subscription_launch.offers.android`의 `ready`가 모두 true여야 한다.
- 판정은 DB 함수 `subscription_launch_access`가 한다. auth `/me`는 그 결과를 그대로 내려준다.

## 출시 당일

T와 기존 회원 오퍼 마감을 한 트랜잭션에서 함께 바꾼다.

```sql
BEGIN;
UPDATE public.app_config SET value = to_jsonb('<출시 시각>'::text) WHERE key = 'free_launch_until';
UPDATE public.app_config SET value = value || jsonb_build_object('legacy_offer_expires_at', '<오퍼 마감>')
WHERE key = 'subscription_launch';
COMMIT;
```

출시가 늦어지면 `free_launch_until`을 미룬다. T가 지나면 런칭 무료가 끝나므로 출시 전에 날짜가 지나지 않게 한다.
