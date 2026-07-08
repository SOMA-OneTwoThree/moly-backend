# secrets/

키파일 보관소. **이 폴더는 gitignore됨**(이 README만 커밋). 실제 키는 절대 커밋되지 않음.

여기 두는 파일:

| 파일 | 용도 | .env 변수 |
| --- | --- | --- |
| `AuthKey_XXXXXXXXXX.p8` | App Store Server API 키(구독/IAP 검증·조회) | `APP_STORE_PRIVATE_KEY_FILE` |
| `fcm-service-account.json` | FCM 푸시(아침/저녁 알림) | `FCM_SERVICE_ACCOUNT_FILE` |

## App Store 키 받는 법
1. **App Store Connect** → 우상단 **사용자 및 접근(Users and Access)** → **통합(Integrations)** 탭 → 좌측 **In-App Purchase**(키).
2. **키 생성(+)** → 다운로드하면 `AuthKey_<KEYID>.p8` 파일. **다운로드는 1회만 가능** — 이 폴더에 저장.
3. 필요한 3값:
   - **Key ID** = 파일명의 `<KEYID>` 10자리(키 목록에도 표시).
   - **Issuer ID** = 같은 페이지 상단의 UUID(계정당 1개, 모든 키 공통).
   - **Bundle ID** = 앱 번들 ID(예: `com.geniusjun.moly`).
4. 위 3값을 `.env`의 `APP_STORE_KEY_ID` / `APP_STORE_ISSUER_ID` / `APP_STORE_BUNDLE_ID`에 기입, 파일 경로를 `APP_STORE_PRIVATE_KEY_FILE`에.
