# 문서 안내

이 디렉터리는 현재 구조와 유지해야 할 계약만 설명한다. 과거 배포 기록·일회성 측정·폐기한 설계는
Git 이력에서 찾는다. 문서보다 실제 코드·공개 계약·DDL이 우선하며, 차이가 있으면 함께 수정한다.

| 문서 | 읽을 때 |
|---|---|
| [전체 구조](ARCHITECTURE.md) | 서버 경계·도메인·외부 연동·보안 원칙 |
| [대화·기억](ARCHITECTURE-capi.md) | 턴 처리·정보 사용 권한·기억·요약·발행 |
| [API](API_SPEC.md) | 클라이언트 공통 규칙과 도메인별 연동 주의 |
| [데이터 모델](ERD.md) | 주요 관계·유일성·날짜 키·삭제 경계 |
| [운영](OPERATIONS.md) | 배포·DB 변경·워커·푸시·장애 대응 |
| [운세](DAILY-FORTUNE.md) | 문구 생성·저장본·공개/광고 잠금·호환 배포 |
| [배너](BANNER_SDUI.md) | SDUI 카탈로그·노출 조건·주제 연결 |
| [구독](SUBSCRIPTIONS.md) | RevenueCat·체험·권한·지급·환불 |
| [BGM](REMOTE_BGM.md) | 음원 카탈로그·권한·클라이언트 재생 |

## 원본과 갱신

- 요청·응답의 정확한 필드: [OpenAPI 원본](../openapi/openapi.yaml), `openapi/paths/`,
  `openapi/components/`, [요청·응답 모델](../app/schemas/).
- DB의 정확한 컬럼·제약·함수·권한: [schema.sql](../db/schema.sql).
  ORM은 전체 DDL을 대체하지 않는다. 구조 변경 절차는 [DB 안내](../db/README.md)를 따른다.
- 운세 편집·검수 입력은 `content/fortune/`, 실행 자산은 `app/resources/fortune/`에 둔다.
- 기능 활성화·가격·한도·서버 상태는 해당 환경에서 별도로 확인한다. 코드 기본값이나 과거 검사
  결과를 현재 운영값으로 적지 않는다.
- 개인 자료·실제 사용자 사례·토큰·접속 정보·진단 원문을 문서나 Git에 넣지 않는다.
