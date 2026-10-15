# 구독 출시

스위치는 `app_config.free_launch_until`(출시 시각 T) 하나다. 활성화 플래그나 테스트 계정 명단은 없다.

## 한도

| 일 한도 | 영어 | 한국어 | 일본어 |
|---|---:|---:|---:|
| 무료 (T 이후) | 40,000 | 50,000 | 60,000 |
| 가입 체험 48시간·스토어 체험·유료 구독 | 300,000 | 400,000 | 550,000 |

- T 전에는 구독·앱 체험이 없는 계정 전원이 런칭 무료(150,000)다. 런칭 무료는 배너 광고를 끄지 않는다(배너는 새 앱에만 있다). 구독·체험 한도는 T 전에도 같다.
- 가중 차감 단위이며 저장된 `profiles.language`를 쓴다. 등급이 바뀌어도 당일 사용량을 초기화하지 않는다.
- T 이후 무료는 새 개인 일기를 받지 않고 운영 일기만 받는다. 운세 상세는 유료 구독과 가입 체험에 광고 없이 제공한다.

## 신규 회원과 기존 회원

| | 출시 전 (지금 < T) | 출시 후 |
|---|---|---|
| 신규 회원: 1개월 무료 없는 페이월, 가입 후 48시간 체험(`signup_trial_until` 전 가입만) | `subscription_launch.new_member_since` 이후 가입 | T 이후 가입 |
| 기존 회원: 1개월 무료 페이월 | `new_member_since` 이전 가입 | T 이전 가입 |

- 48시간 체험을 시작한 계정은 계속 신규 회원이다.
- `app_config.signup_trial_until`(가입 체험 종료 시각 C) 이후 가입한 계정에는 48시간 체험이 없다. 가입 때 `profiles.trial_ends_at`을 비워 두고 `self_trial_available`이 false라 앱 체험도 시작할 수 없다(`POST /subscription/trial`은 409 `TRIAL_UNAVAILABLE`). C 전에 가입한 계정과 이미 시작한 체험은 그대로다. 키가 없거나 JSON null이면 C가 없는 것으로 보고 지금처럼 동작한다.
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

가입 체험 종료 C는 앱 업데이트 출시 시각이다. 함수 차이 SQL `db/changes/signup_trial_cutoff.sql`은 키가 없으면 동작이 같으므로 미리 적용해 둘 수 있다. 단 함수 본문이 배포 preflight 대상이므로 같은 스키마 계약을 가진 backend 배포 직전에 적용한다.

- 이 SQL의 `bootstrap_user`에는 기본 루틴 생성이 없다. 적용 전에 대상 DB의 `pg_get_functiondef('public.bootstrap_user(uuid,timestamptz)'::regprocedure)`에 `INSERT INTO public.routines`가 남아 있으면 `bootstrap_no_default_routines.sql`을 그 절차대로 먼저 적용한다.
- C는 미래 시각으로 미리 넣는다. 이미 지난 시각을 넣으면 C와 입력 시각 사이에 가입한 계정은 가입 체험은 받는데 앱 체험은 시작할 수 없어, 기존 앱에서 체험 시작 실패 시트가 뜬다.
- 시간대를 꼭 붙인다. 붙이지 않으면 세션 시간대(UTC)로 읽힌다. 넣은 뒤 아래 조회로 값을 확인한다.

```sql
INSERT INTO public.app_config (key, value) VALUES ('signup_trial_until', to_jsonb('2026-10-20 10:00:00+09:00'::timestamptz))
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
SELECT (value #>> '{}')::timestamptz AT TIME ZONE 'Asia/Seoul' FROM public.app_config WHERE key = 'signup_trial_until';
```

예시 시각은 자리표시다. 실제 업데이트 시각으로 바꾼다.

업데이트 출시가 늦어지면 `free_launch_until`처럼 `signup_trial_until`도 미룬다. C가 먼저 지나면 아직 체험을 안내하는 기존 앱에서 가입한 계정도 체험 없이 시작한다.
