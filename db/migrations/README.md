# Appearance v2 DB rollout

## 새 DB (dev·CI)

`db/schema.sql` → `db/seed_and_triggers.sql` 순으로 적용하면 끝이다. 시드가 꾸미기 6종을
active로 넣으므로 가입·상점·장착이 바로 동작한다. 에셋 파일이 아직 버킷에 없으면 이미지
URL만 404이고 API 계약 자체는 정상이다.

## 기존 DB (staging·prod)

`20260713_appearance_v2_expand.sql`은 기존 API와 호환되는 additive 단계다. 먼저 적용해 둔다.

나머지는 유지보수 창에서 **연속으로** 실행한다. 시드 적용 시점부터 cutover 커밋까지는 가입이
중단된다 — `bootstrap_user`가 `slot='theme'`인 필수 상품 3종을 요구하는데 slot 전환은 cutover가
하기 때문이다.

1. 최종 에셋을 `{public_id}/v{asset_version}/…` 경로로 버킷에 올린다.
2. `scripts/verify_appearance_assets.py`로 매니페스트와 원격 이미지를 검증한다.
3. DB 백업 후 유지보수 창을 시작한다.
4. `db/seed_and_triggers.sql` 적용 — v2 `assets`·`asset_version`을 적재하고 6종을 active로 만든다.
   시드는 `slot`을 갱신하지 않는다(장착 행의 복합 FK 때문).
5. `20260713_appearance_v2_cutover.sql`을 dry-run한 뒤 적용 — `background` → `theme` 슬롯 전환,
   기존 사용자에게 `theme_default` 소급 지급·장착, 구형 구독 장착 행 삭제.
6. `moly-backend`와 `moly-auth` 새 버전을 함께 배포한다.
7. 신규 가입, 기본 지급, `/shop/products`, `/inventory`, 두 equipment 조회, 구매를 스모크 테스트한다.

cutover SQL은 최종 에셋이나 필수 기본 상품이 없으면 트랜잭션을 중단한다. 구형 assets를
새 필드로 임의 변환하거나 빈 카탈로그 상태로 진행하지 않는다.

## 에셋 교체

시드의 `ON CONFLICT`는 `asset_version`이 올라갈 때만 `assets`를 덮는다. 새 아트를 넣을 때는
`v{n+1}` 경로로 업로드하고 시드의 URL과 `asset_version`을 함께 올린다. 버전을 올리지 않고
URL만 바꾸면 iOS 캐시가 갱신되지 않는다.

## head 슬롯 분리 + rightside 자세 (`20260719_hat_glasses_rightside.sql`)

`head` 슬롯을 `hat`/`glasses`로 나눠 모자·안경 동시 착용을 허용하고, 착용 아이템에 새 자세
(`rightside`) upright 레이어를 더한다. 구버전 앱(서버가 버전을 식별할 수 없음)은 새 슬롯 값을
디코딩하지 못해 상점 응답 전체가 깨지므로, 새 서버는 레거시 경로에서 hat/glasses를 `head`로,
에셋을 구 자세로 투영하고 신버전은 `/v2/*` 경로를 쓴다.

새 DB(dev·CI): `db/schema.sql` → `db/seed_and_triggers.sql`이면 끝이다. 시드가 hat/glasses 슬롯과
`rightside` 키를 이미 담는다.

기존 DB(staging·prod): 유지보수 창에서 **연속으로** 실행한다. 구 서버는 `slot='glasses'`/`'hat'`을
만나면 `/shop/products`가 500이 된다(레거시 slot Literal 위반).

1. `rightside` upright 레이어를 `{public_id}/v{asset_version}/rightside/upright.png` 경로로 버킷에 올린다.
2. `scripts/verify_appearance_assets.py`로 매니페스트와 원격 이미지를 검증한다(파일 업로드 전에는
   `--skip-fetch`로 DTO·URL 버전만 검증). 각 상품을 v2·레거시 두 계약으로 검증한다.
3. DB 백업 후 유지보수 창을 시작한다.
4. `db/seed_and_triggers.sql` 적용 — 이미 slot을 갱신하지 않으므로(복합 FK) hat/glasses 전환은
   마이그레이션이 하고, 시드는 새 DB용 리터럴과 bootstrap_user 갱신을 제공한다.
5. `20260719_hat_glasses_rightside.sql`을 dry-run한 뒤 적용 — head → hat/glasses 슬롯 전환,
   장착 행 이전, `rightside` 자산 패치, bootstrap_user 갱신. `asset_version`은 올리지 않아 구 자세 URL은
   불변이라 구버전 앱 캐시가 유지된다.
6. `moly-backend` 새 버전을 배포한다.
7. 레거시 `/shop/products`·`/inventory`·두 equipment 조회에 hat/glasses·`rightside`가 노출되지 않는지,
   `/v2/*` 4종이 새 슬롯과 rightside upright를 반환하는지 스모크 테스트한다.
