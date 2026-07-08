# secrets/

키파일 보관소. **이 폴더는 gitignore됨**(이 README만 커밋). 실제 키는 절대 커밋되지 않음.

여기 두는 파일:

| 파일 | 용도 | .env 변수 |
| --- | --- | --- |
| `fcm-service-account.json` | FCM 푸시(아침/저녁 알림) — 키리스(ADC/WIF) 권장, 파일은 대안 | `FCM_SERVICE_ACCOUNT_FILE` |

## App Store — .p8 키는 **불필요**
우리 설계는 App Store Server API를 **조회하지 않고** JWS x5c 서명검증만 한다 → `.p8`/Key ID/Issuer ID **필요 없음**.
필요한 값은 `.env`에 두 개뿐:
- `APP_STORE_BUNDLE_ID` = 앱 번들 ID(예: `com.geniusjun.moly`)
- `APP_STORE_APP_APPLE_ID` = 앱 숫자 ID(**Production 전환 시만** — App Store Connect 앱 정보)

(추후 App Store Server API 조회를 추가한다면 그때 .p8를 이 폴더에 두고 Key ID/Issuer ID를 배선한다.)
