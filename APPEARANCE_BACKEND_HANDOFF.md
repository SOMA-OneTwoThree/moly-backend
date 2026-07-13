# 꾸미기 시스템 백엔드 인수인계

- 작성일: 2026-07-13
- 대상: 백엔드 개발·인프라·서버 QA
- 변경 성격: 구형 계약과 호환하지 않는 breaking change
- 전체 팀 문서: [`APPEARANCE_SYSTEM_HANDOFF.md`](./APPEARANCE_SYSTEM_HANDOFF.md)
- API 기준 문서: [`API_SPEC.md` §6](./API_SPEC.md#6-상점--꾸미기)

## 1. 백엔드가 해야 할 일

이번 변경에서 백엔드가 처리해야 하는 핵심은 다음 일곱 가지다.

1. 상품 슬롯을 `theme/head/neck/body`로 변경한다.
2. `GET /shop/products`를 `themes/items`와 새 에셋 구조로 변경한다.
3. 장착 상태를 non-null `theme_id`와 nullable `head_id/neck_id/body_id`로 변경한다.
4. `theme_default` 상품과 모든 사용자의 기본 테마 장착 상태를 보장한다.
5. 신규 사용자에게 집 테마(`theme_default`)·운동 테마·선글라스(`head`) 3종의 소유권을 기본 지급한다.
6. 테마 장면·포즈별 착용 레이어를 버전별 불변 CDN URL로 제공한다.
7. 구형 `background`·`background_id`·`*_layer` 응답을 제거한다.

테마도 기존 상품처럼 `POST /shop/purchases`로 구매한다. 요청과 구매 동작은 동일하지만 성공 응답에는 주문 추적용 `order_id`가 추가된다.

## 2. 변경 전후 계약

| 항목 | 변경 전 | 변경 후 |
|---|---|---|
| 방 상품 슬롯 | `background` | `theme` |
| 카탈로그 배열 | `backgrounds` | `themes` |
| 장착 필드 | `background_id` | `theme_id` |
| 테마 해제 | `background_id: null` 가능 | `theme_id`는 항상 non-null |
| 방 에셋 | 상품 단위 `day/night` | 장면의 레이어별 `day_url/night_url` |
| 착용 에셋 | `head_layer` 등 슬롯별 단일 URL | `upright_layer_url` (upright 자세에만 착용) |
| 카드 이미지 | `thumbnail` | `thumbnail_url` |
| 상세 이미지 | 명확한 분리 없음 | 필수 `detail_url` |
| 캐시 버전 | 없음 | `asset_version` + 변경된 URL |
| 구매 성공 응답 | 상품·가격·잔액 | 기존 필드 + `order_id` |
| 신규 사용자 기본 지급 | 구형 상품 기준 | 집 테마(`theme_default`)·운동 테마·선글라스(`head`) 보유 |

### 낮·밤 기능은 삭제되지 않음

삭제되는 것은 구형 `assets.day`, `assets.night` 필드다. 기능은 테마 레이어의 `day_url`, optional `night_url`로 이동한다.

창문만 시간대에 따라 바뀌는 테마는 다음처럼 응답하면 된다.

```json
{
  "layers": [
    {
      "id": "background",
      "frame": { "x": 0, "y": 0, "width": 393, "height": 852 },
      "z_index": 0,
      "day_url": "https://cdn.example.com/theme/v1/background.png"
    },
    {
      "id": "sofa",
      "frame": { "x": 4, "y": 341, "width": 269, "height": 129 },
      "z_index": 10,
      "day_url": "https://cdn.example.com/theme/v1/sofa.png"
    },
    {
      "id": "window",
      "frame": { "x": 200, "y": 112, "width": 151, "height": 157 },
      "z_index": 20,
      "day_url": "https://cdn.example.com/theme/v1/window-day.png",
      "night_url": "https://cdn.example.com/theme/v1/window-night.png"
    }
  ]
}
```

밤에는 `window`만 `night_url`로 바뀐다. `night_url`이 없는 레이어는 계속 `day_url`을 사용한다.

## 3. 데이터 모델 요구사항

실제 테이블 구조는 백엔드 사정에 맞게 정할 수 있지만, API를 만들기 위해 다음 데이터를 표현할 수 있어야 한다.

### 상품 공통 데이터

```text
id: String, globally unique
name: String
slot: theme | head | neck | body
price_hay: Int?
asset_version: Int
thumbnail_url: URL
detail_url: URL
```

사용자별 값:

```text
owned: Bool
equipped: Bool
```

`owned`와 `equipped`는 정적 상품 데이터가 아니라 현재 인증 사용자 기준 응답 값이다.

### 테마 전용 데이터

```text
canvas.width: 393
canvas.height: 852
character_frame: x, y, width, height
character_url: URL
layers[]:
  id
  frame: x, y, width, height
  z_index
  day_url
  night_url?
```

방 안 캐릭터는 테마가 정한다. `character_frame`이 “어디에”, `character_url`이 “무엇을”이다.
테마마다 캐릭터 자세와 위치가 다를 수 있으므로 앱에 고정 매핑하지 않는다.

검증 규칙:

- 캔버스는 정확히 `393×852`다.
- `character_frame`의 모든 값은 숫자다.
- 각 레이어의 `id`는 같은 테마 안에서 고유하다.
- `z_index`는 정수다.
- 모든 레이어에 `day_url`이 필요하다.
- `night_url`은 선택 사항이다.
- 테마의 모든 레이어는 캐릭터 뒤에 표시되므로 캐릭터 전후 z-index는 저장하지 않는다.

### 착용 상품 전용 데이터

```text
upright_layer_url: URL
```

착용 아이템은 `upright` 자세에만 입힌다. 대화 화면과 상점에서 보이는 자세다.
방 안 자세는 테마마다 달라서, 상품당 하나뿐인 레이어 URL로는 표현할 수 없다.
따라서 방 안 캐릭터에는 아이템을 입히지 않고, 테마의 `character_url`을 그대로 그린다.

에셋 규격:

- `upright_layer_url`: `800×1100px` 전체 투명 PNG — 번들 기본 캐릭터(`cappy`)와 같은 캔버스

아이템 부분만 잘라낸 PNG를 제공하면 앱에서 기본 캐릭터와 정렬되지 않는다. 착용 상품에는
좌표를 저장하거나 응답할 필요가 없다. 정렬은 PNG의 투명 여백이 책임진다.

### 사용자 장착 데이터

```text
theme_id: String, non-null
head_id: String?
neck_id: String?
body_id: String?
```

- `theme_id`는 `theme` 슬롯 상품만 참조할 수 있다.
- 선택 슬롯은 각각 같은 이름의 슬롯 상품만 참조할 수 있다.
- 모든 참조 상품은 사용자가 보유해야 한다.

## 4. `theme_default` 필수 계약

서버에는 `theme_default` ID를 가진 실제 테마 상품이 있어야 한다.

필수 조건:

- 모든 사용자가 보유한 것으로 처리한다.
- 신규 사용자의 `theme_id` 기본값으로 사용한다.
- `theme_id`가 `theme_default`이면 카탈로그에서 `equipped: true`다.
- 구매 대상이 아니므로 `price_hay: null`을 권장한다.
- 다른 테마와 동일하게 완전한 `thumbnail_url`, `detail_url`, `assets.scene`을 제공한다.

iOS는 장애 시 번들 기본 테마를 표시하지만 DTO는 모든 테마 상품에 완전한 `assets.scene`을 요구한다. 따라서 “앱에 번들되어 있으니 기본 테마의 scene은 생략”하면 안 된다.

기존 사용자에게 테마 장착값이 없다면 마이그레이션 시 `theme_default`를 넣어야 한다.

### 신규 사용자 기본 지급

신규 사용자는 가입 시 다음 세 상품을 보유한 상태로 시작한다.

| 상품 | 슬롯 | 초기 장착 |
|---|---|---:|
| 집 테마(`theme_default`) | `theme` | O |
| 운동 테마 | `theme` | X |
| 선글라스 | `head` | X |

- 세 상품은 `GET /shop/products`에서 `owned: true`이고 `GET /inventory`에도 포함되어야 한다.
- 초기 장착 상태는 `theme_id: "theme_default"`, `head_id/neck_id/body_id: null`이다.
- 기본 지급 상품의 재구매 시도는 `409 ALREADY_OWNED`다.
- V2의 “장착하지 않은 상태로 지급”은 운동 테마와 선글라스에 적용된다. 새 장착 계약에서는 테마를 해제할 수 없으므로 집 테마인 `theme_default`는 항상 초기 장착된다.
- 가입 완료 응답 전까지 기본 소유권 3건과 초기 equipment가 모두 생성되어야 한다.

## 5. API 계약

### 5.1 `GET /shop/products`

응답 최상위 구조:

```json
{
  "themes": [],
  "items": []
}
```

- `themes`에는 `slot: "theme"` 상품만 포함한다.
- `items`에는 `head`, `neck`, `body` 상품만 포함한다.
- 상품 ID는 두 배열 전체에서 고유해야 한다.
- 신규 사용자의 응답에서는 집 테마(`theme_default`)·운동 테마·선글라스가 `owned: true`다.
- 초기 장착 직후에는 `theme_default`만 `equipped: true`이고 운동 테마와 선글라스는 `equipped: false`다.

#### 공통 상품 필드

```json
{
  "id": "hat_ribbon",
  "name": "리본 모자",
  "slot": "head",
  "price_hay": 1000,
  "owned": true,
  "equipped": false,
  "asset_version": 2,
  "assets": {}
}
```

| 필드 | 필수 | 설명 |
|---|---:|---|
| `id` | O | 상품 고유 ID |
| `name` | O | 화면 표시명 |
| `slot` | O | `theme/head/neck/body` |
| `price_hay` | O | 정수 또는 명시적 `null` |
| `owned` | O | 현재 사용자 보유 여부 |
| `equipped` | O | 현재 사용자 장착 여부 |
| `asset_version` | O | 정수 에셋 버전 |
| `assets` | O | 슬롯에 맞는 에셋 객체 |

#### 테마 상품 응답

```json
{
  "id": "theme_sakura",
  "name": "벚꽃",
  "slot": "theme",
  "price_hay": 4000,
  "owned": true,
  "equipped": false,
  "asset_version": 3,
  "assets": {
    "thumbnail_url": "https://cdn.example.com/theme_sakura/v3/thumb.png",
    "detail_url": "https://cdn.example.com/theme_sakura/v3/detail.png",
    "scene": {
      "canvas": { "width": 393, "height": 852 },
      "character_frame": { "x": 51, "y": 338.8, "width": 171, "height": 85.2 },
      "character_url": "https://cdn.example.com/theme_sakura/v3/character.png",
      "layers": [
        {
          "id": "background",
          "frame": { "x": 0, "y": 0, "width": 393, "height": 852 },
          "z_index": 0,
          "day_url": "https://cdn.example.com/theme_sakura/v3/background.png"
        },
        {
          "id": "window",
          "frame": { "x": 200, "y": 111.6, "width": 151.1, "height": 157 },
          "z_index": 10,
          "day_url": "https://cdn.example.com/theme_sakura/v3/window-day.png",
          "night_url": "https://cdn.example.com/theme_sakura/v3/window-night.png"
        }
      ]
    }
  }
}
```

테마 상품 검증:

- `assets.thumbnail_url`, `assets.detail_url`, `assets.scene` 필수
- `assets.scene.character_url`, `assets.scene.character_frame` 필수
- `upright_layer_url`을 보내지 않음
- 모든 scene URL은 절대 URL

#### 착용 상품 응답

```json
{
  "id": "hat_ribbon",
  "name": "리본 모자",
  "slot": "head",
  "price_hay": 1000,
  "owned": true,
  "equipped": false,
  "asset_version": 2,
  "assets": {
    "thumbnail_url": "https://cdn.example.com/hat_ribbon/v2/thumb.png",
    "detail_url": "https://cdn.example.com/hat_ribbon/v2/detail.png",
    "upright_layer_url": "https://cdn.example.com/hat_ribbon/v2/upright.png"
  }
}
```

착용 상품 검증:

- `thumbnail_url`, `detail_url`, `upright_layer_url` 모두 필수
- `scene`과 `home_layer_url`을 보내지 않음
- `upright_layer_url`은 절대 URL

### 5.2 `GET /inventory`

현재 사용자가 보유한 상품을 새 상품 구조로 반환한다.

```json
{
  "data": [
    {
      "id": "hat_ribbon",
      "name": "리본 모자",
      "slot": "head",
      "price_hay": 1000,
      "owned": true,
      "equipped": true,
      "asset_version": 2,
      "assets": {
        "thumbnail_url": "https://cdn.example.com/hat_ribbon/v2/thumb.png",
        "detail_url": "https://cdn.example.com/hat_ribbon/v2/detail.png",
        "upright_layer_url": "https://cdn.example.com/hat_ribbon/v2/upright.png"
      }
    }
  ]
}
```

`data` 안의 상품 구조는 `GET /shop/products`와 동일해야 한다. 구매 상품뿐 아니라 기본 지급 상품도 포함하며 모든 원소의 `owned`는 `true`다.

### 5.3 `GET /inventory/equipment`

```json
{
  "theme_id": "theme_default",
  "head_id": "hat_ribbon",
  "neck_id": null,
  "body_id": null
}
```

- 네 키 모두 항상 응답한다.
- `theme_id`는 문자열이며 null을 허용하지 않는다.
- 선택 슬롯은 값이 없으면 명시적 null을 반환한다.

### 5.4 `PUT /inventory/equipment`

부분 수정이 아닌 전체 교체다. 네 키를 항상 모두 요구한다.

요청:

```json
{
  "theme_id": "theme_sakura",
  "head_id": "hat_ribbon",
  "neck_id": null,
  "body_id": "body_apron"
}
```

성공 응답:

```json
{
  "theme_id": "theme_sakura",
  "head_id": "hat_ribbon",
  "neck_id": null,
  "body_id": "body_apron"
}
```

처리 요구사항:

- 요청 전체를 한 트랜잭션으로 검증하고 저장한다.
- 성공 응답은 실제 저장된 전체 장착 상태다.
- 성공한 PUT 직후 모든 GET에서 같은 상태가 보여야 한다.
- 서버가 요청과 다른 상태로 보정해야 한다면, 응답 상태에 해당하는 상품과 에셋이 같은 카탈로그에 반드시 존재해야 한다.

검증과 오류:

| 조건 | 응답 |
|---|---|
| 네 키 중 하나라도 누락 | `422 VALIDATION` |
| `theme_id`가 null 또는 빈 값 | `422 VALIDATION` |
| 존재하지 않는 상품 ID | `422 VALIDATION` |
| 요청 필드와 상품 슬롯 불일치 | `422 VALIDATION` |
| 사용자가 보유하지 않은 상품 | `422 NOT_OWNED` |
| 테마를 null로 해제하려는 요청 | `422 VALIDATION` |

### 5.5 `GET /me`

기존 응답의 `equipment`를 다음 구조로 변경한다.

```json
{
  "equipment": {
    "theme_id": "theme_default",
    "head_id": null,
    "neck_id": null,
    "body_id": null
  }
}
```

`GET /me.equipment`와 `GET /inventory/equipment`는 같은 사용자의 동일한 상태를 반환해야 한다.

### 5.6 `POST /shop/purchases`

요청 형식과 구매 동작은 유지한다. 성공 응답에는 주문 기록·CS 추적용 `order_id`가 추가된다.

요청:

```json
{ "product_id": "theme_sakura" }
```

응답:

```json
{
  "product_id": "theme_sakura",
  "order_id": "…",
  "price_hay": 4000,
  "balance_after": 640
}
```

- `order_id`는 주문 기록·CS 추적에 사용하는 주문 식별 문자열이며 서버 주문 기록과 연결되어야 한다.
- 클라이언트가 `order_id`를 사용하지 않아도 기존 필드 디코딩과 구매 흐름은 정상 동작한다.
- 구매 성공은 소유권만 추가한다.
- 구매와 장착은 별도 동작이다.
- 기본 지급 상품을 포함한 기존 보유 상품 구매는 `409 ALREADY_OWNED`, 잔액 부족은 `402 INSUFFICIENT_HAY`다.

## 6. 응답 일관성 규칙

동일 사용자의 다음 값은 항상 일치해야 한다.

```text
GET /inventory/equipment.theme_id
GET /me.equipment.theme_id
GET /shop/products에서 해당 테마의 equipped
GET /inventory에 해당 테마가 포함된 경우 그 상품의 equipped
```

선택 슬롯도 같은 규칙을 따른다.

예를 들어 `head_id == "hat_ribbon"`이면:

- `hat_ribbon`은 사용자가 보유한 상품이어야 한다.
- `hat_ribbon.slot`은 `head`여야 한다.
- 카탈로그에서 `hat_ribbon.equipped`는 `true`여야 한다.
- `GET /inventory`의 `hat_ribbon.equipped`도 `true`여야 한다.
- 다른 `head` 상품의 `equipped`는 모두 `false`여야 한다.

같은 슬롯에서 두 상품이 동시에 `equipped: true`가 되면 안 된다.

## 7. CDN 및 에셋 버전 계약

iOS 캐시는 URL 전체 문자열을 키로 사용한다. 파일 내용이 바뀌면 URL도 바꿔야 한다.

```text
올바름
/hat_ribbon/v1/home.png
/hat_ribbon/v2/home.png

잘못됨
/hat_ribbon/home.png 파일 내용만 교체
```

에셋 변경 시:

1. 새 버전 경로로 파일을 업로드한다.
2. 관련 URL을 새 경로로 변경한다.
3. `asset_version`을 증가시킨다.
4. 모든 URL의 다운로드와 이미지 디코딩을 확인한다.

CDN 필수 조건:

- URLSession의 일반 GET으로 직접 접근 가능
- 앱 전용 인증 헤더 불필요
- HTTP `2xx` 반환
- 빈 파일이 아님
- ImageIO가 인식할 수 있는 이미지
- 착용 레이어는 투명 PNG
- 동일 버전 URL의 내용은 불변
- 앱이 저장한 매니페스트에서 재사용할 수 있도록 짧은 만료 URL을 사용하지 않음

`asset_version`만 증가시키고 URL을 유지하면 캐시가 갱신되지 않는다.

## 8. 클라이언트 동작과 서버 로그 해석

장착 요청 순서:

1. iOS가 카탈로그에서 대상 상품을 찾는다.
2. 테마는 `character_url`, 모든 `day_url`, 존재하는 `night_url`을 다운로드한다.
3. 착용 아이템은 `upright_layer_url`을 다운로드한다.
4. 모든 다운로드가 성공한 뒤 `PUT /inventory/equipment`를 보낸다.
5. 성공 응답을 받은 뒤 화면 상태를 교체한다.

따라서 사용자 장착 실패를 조사할 때:

- PUT 로그가 없다면 CDN 다운로드 또는 이미지 파일 문제를 먼저 확인한다.
- PUT이 `422`라면 소유권·슬롯·누락 키 검증을 확인한다.
- PUT은 성공했지만 다음 GET이 이전 값을 반환하면 저장 트랜잭션이나 읽기 일관성 문제다.
- 서버 응답에 장착 ID가 있지만 카탈로그에 해당 상품이 없으면 iOS는 새 외형을 완성할 수 없다.

iOS는 앱 시작과 포그라운드 복귀에서 `GET /shop/products`와 `GET /inventory/equipment`를 함께 요청할 수 있다. 정상적인 중복 조회로 취급해야 한다.

## 9. 기존 데이터 마이그레이션

최소 마이그레이션 항목:

1. 상품 슬롯 `background`를 `theme`으로 변경한다.
2. `background_id` 값을 `theme_id`로 이전한다.
3. 기존 `background_id`가 null인 사용자는 `theme_default`로 채운다.
4. 모든 사용자에게 `theme_default` 소유권을 보장한다.
5. 기존 배경 상품을 `themes` 응답으로 옮긴다.
6. 기존 테마 에셋을 `393×852` scene과 레이어 배열로 변환한다.
7. 기존 착용 상품에 home/upright 두 포즈 URL을 채운다.
8. 모든 상품에 `thumbnail_url`, `detail_url`, `asset_version`을 채운다.
9. 집 테마는 `theme_default`로, 운동 테마와 선글라스는 각각 `theme`·`head` 슬롯의 실제 상품으로 시드한다.
10. 신규 사용자 생성 흐름에 세 기본 상품의 소유권과 초기 equipment를 함께 저장한다. 기존 사용자에게 세 상품 전체를 소급 지급하는 것은 요구사항이 아니지만 `theme_default` 소유권은 예외 없이 보장한다.
11. 상점 주문 기록에 외부 응답용 `order_id`를 저장하고 성공 응답에 포함한다.

구형 테마의 단일 `day/night` 이미지는 그대로 새 최상위 필드로 옮기지 않는다. 새 scene 안의 적절한 레이어로 변환한다.

예를 들어 창문 풍경만 바뀌어야 한다면:

- 방 배경 레이어: `day_url`만 제공
- 소파·테이블 등 고정 소품: `day_url`만 제공
- 창문 레이어: `day_url`과 `night_url` 모두 제공

## 10. 서버 테스트 체크리스트

### 카탈로그

- [ ] `themes/items` 배열이 항상 존재함
- [ ] `backgrounds`를 응답하지 않음
- [ ] 모든 상품에 공통 필드와 `asset_version`이 있음
- [ ] 테마는 완전한 `assets.scene`을 가짐
- [ ] 착용 상품은 upright URL을 가짐
- [ ] 테마와 아이템의 잘못된 에셋 조합이 없음
- [ ] `price_hay: null`을 유지할 수 있음
- [ ] 신규 사용자의 집 테마(`theme_default`)·운동 테마·선글라스가 `owned: true`임
- [ ] 신규 사용자의 초기 `equipped`는 `theme_default`만 true임

### 장착 상태

- [ ] 신규 사용자의 `theme_id`가 `theme_default`
- [ ] `theme_id`가 null인 응답이 없음
- [ ] PUT은 네 키를 모두 요구함
- [ ] 선택 슬롯 null이 명시적으로 응답됨
- [ ] 미보유·슬롯 불일치·존재하지 않는 ID가 거부됨
- [ ] 성공 후 equipment와 카탈로그 equipped 플래그가 일치함
- [ ] 동시 PUT에서도 슬롯당 하나의 최종 상태만 저장됨

### CDN

- [ ] 모든 URL이 `2xx`로 다운로드됨
- [ ] 동일 URL을 다시 받아도 내용이 동일함
- [ ] 변경된 에셋은 URL과 `asset_version`이 모두 변경됨
- [ ] upright 레이어가 `800×1100px` 투명 PNG임
- [ ] 테마의 `character_url`이 `character_frame`과 같은 가로세로 비율임
- [ ] `night_url`이 없는 레이어도 정상 응답됨
- [ ] 창문 등 `night_url`이 있는 레이어만 밤 이미지가 존재함

### 교차 API

- [ ] `GET /me.equipment`와 `GET /inventory/equipment`가 동일함
- [ ] 장착 ID와 `GET /shop/products.equipped`가 동일함
- [ ] `GET /inventory`의 상품 구조가 카탈로그와 동일함
- [ ] `GET /inventory`의 `equipped` 플래그가 equipment·카탈로그와 동일함
- [ ] 구매 직후 `owned`가 true가 되고 자동 장착되지는 않음
- [ ] 구매 성공 응답에 `order_id`가 있고 서버 주문 기록과 연결됨
- [ ] 기본 지급 상품 재구매가 `409 ALREADY_OWNED`임

## 11. 제거되는 구형 필드

다음 계약은 새 iOS에서 지원하지 않는다.

```text
backgrounds
slot: background
background_id
assets.day
assets.night
assets.thumbnail
assets.home_layer_url
head_layer
neck_layer
body_layer
```

낮·밤 기능이 제거되는 것이 아니다.

```text
assets.day / assets.night
→ assets.scene.layers[].day_url / night_url?
```

구형 클라이언트를 같은 서버에서 계속 지원해야 한다면 API 버전을 분리하거나 구형 응답을 병행해야 한다. 현재 iOS 구현에는 구형 디코딩이 없다.

## 12. 배포 순서

1. 최종 에셋을 버전 경로로 CDN에 업로드한다.
2. 모든 URL·픽셀 크기·투명도·응답 코드를 검증한다.
3. DB 스키마와 기존 사용자 데이터를 마이그레이션한다.
4. `theme_default`·운동 테마·선글라스 상품을 시드하고 `theme_default`의 기존 사용자 소유권·장착값을 보정한다.
5. 신규 사용자 기본 소유권 3건·초기 equipment 생성과 구매 `order_id` 저장을 배포한다.
6. 스테이징에 새 API를 배포한다.
7. 새 iOS와 카탈로그·기본 지급·장착·구매·낮밤 전환을 통합 검증한다.
8. 구형 앱 호환이 필요 없다는 전제 아래 iOS와 백엔드 운영 배포 시점을 맞춘다.

운영 배포 전 필수 확인:

- 장착된 모든 ID가 새 카탈로그에 존재한다.
- 장착된 모든 상품의 필수 CDN 에셋이 다운로드 가능하다.
- 어떤 사용자도 `theme_id: null` 상태가 아니다.
- `theme_default`가 모든 사용자에게 유효하다.
- 신규 사용자에게 기본 지급 3종이 보이고 `theme_default`만 장착되어 있다.
- 구매 성공 응답의 `order_id`가 실제 주문과 연결된다.
- 새 API가 구형 필드와 새 필드를 섞어 보내지 않는다.
