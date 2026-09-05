# Moly 인프라 현황 — 세션 인수인계 문서

> 다음 세션에서 이 문서를 붙여넣으면 현재 AWS/배포 인프라 상태를 그대로 이어갈 수 있음.
> 작성 시점 기준의 "실제 구성된 상태"를 기록. (진행 중 논의는 맨 아래 별도 섹션)

---

## 0. 한 줄 요약

Moly(AI 컴패니언 iOS 앱)의 백엔드를 AWS EC2에 Docker로 배포. GitHub main merge → 자동 빌드/배포되는 CI/CD가 이미 구축돼 돌아가는 상태. 현재 **음성(ai-voice) 구조에서 텍스트 백엔드(moly-backend) 구조로 전환하는 기획 변경 중** (아직 미적용, 논의 단계).

---

## 1. AWS 계정 / 리전

- **AWS 계정 ID**: `676972757138`
- **리전**: `ap-northeast-2` (서울) — 모든 리소스가 여기에 있음 (EC2, ECR, Parameter Store, IAM, KMS)

---

## 2. EC2

- **인스턴스 ID**: `i-0b2154ed899e3b85d`
- **이름**: moly-voice-ec2
- **OS**: Ubuntu 24.04 LTS, x86_64 (t3.medium)
- **EIP(고정 IP)**: `54.116.160.226`
- **접속 방법**: SSH 없음(22번 포트 안 열림). **SSM Session Manager**로만 접속.
  - docker 명령은 root 필요 → 접속 후 `sudo su -`
- **설치돼 있는 것**: Docker(공식 repo, compose v2), AWS CLI v2(공식 installer, apt 아님), nginx, certbot, jq, SSM 에이전트(snap)
- **보안 그룹(moly-voice-sg)**: 인바운드 80/443 (0.0.0.0/0), 22 없음. 아웃바운드 전체 허용.
- **네트워크**: VPC CIDR `10.20.0.0/16`, 퍼블릭 서브넷 + IGW, NAT 게이트웨이 없음(퍼블릭이라 불필요)

### nginx / TLS
- 도메인: **`voice.moly.asia`** (registrar: 가비아). DNS A레코드: `voice` → `54.116.160.226`
- TLS: Let's Encrypt (certbot), 자동 갱신(certbot.timer 활성), 만료 시 자동 갱신
- nginx 설정: `/etc/nginx/sites-available/default` — 443에서 받아 `127.0.0.1:8001`로 WebSocket 프록시 (`proxy_read_timeout 3600s`). **주의: 이 설정은 현재 ai-voice(8001) 기준. backend 전환 시 포트/프로토콜 수정 필요.**

---

## 3. EC2 IAM 역할 — `moly-voice-ec2-role`

EC2 인스턴스에 연결된 역할. **현재 붙어있는 권한(실제 구성 완료된 상태):**

1. **AmazonSSMManagedInstanceCore** (관리형) — SSM Session Manager/Run Command 수신
2. **인라인: Parameter Store 읽기** — `ssm:GetParameter*` on `arn:aws:ssm:ap-northeast-2:676972757138:parameter/moly/prod/*`
3. **인라인: KMS 복호화** — `kms:Decrypt` (SecureString 복호화용, `alias/aws/ssm`)
4. **인라인: ECR pull** — `ecr:GetAuthorizationToken`(Resource `*`) + `BatchCheckLayerAvailability` / `GetDownloadUrlForLayer` / `BatchGetImage` (pull 전용, push 권한 없음)

> ⚠️ 이 4번(ECR pull)은 처음에 빠뜨렸다가 나중에 추가한 것. 없으면 deploy.sh의 ECR 로그인이 AccessDenied로 실패함. 현재는 추가돼서 정상.

---

## 4. GitHub Actions용 IAM 역할 — `moly-github-actions-role`

- **역할 ARN**: `arn:aws:iam::676972757138:role/moly-github-actions-role`
- **OIDC 공급자**: `token.actions.githubusercontent.com` (audience `sts.amazonaws.com`) — IAM Identity Provider에 등록됨

### 신뢰 정책 (누가 이 역할을 assume 가능한가)
Federated OIDC. `sub`가 아래 중 하나여야 함 (StringLike):
```
repo:SOMA-OneTwoThree/moly-voice:ref:refs/heads/main
repo:SOMA-OneTwoThree/moly-llm:ref:refs/heads/main
repo:SOMA-OneTwoThree/moly-infra:ref:refs/heads/main
```
> ⚠️ backend 전환 시 `repo:SOMA-OneTwoThree/moly-backend:ref:refs/heads/main` 을 여기에 추가해야 함.

### 권한 정책 (인라인, 이름: moly-github-action-permissions)
현재 완성된 상태:
- **ECRAuth**: `ecr:GetAuthorizationToken` (Resource `*`)
- **ECRPushPull**: push/pull 액션 on `repository/moly-voice` + `repository/moly-llm`
  > ⚠️ backend 전환 시 `repository/moly-backend` 를 여기에 추가해야 함.
- **SSMDeploy**: `ssm:SendCommand` + `ssm:GetCommandInvocation` + `ssm:ListCommandInvocations`
  - Resource: `arn:aws:ssm:ap-northeast-2:*:document/AWS-RunShellScript` (document는 계정부분 `*` — 공용 문서라 필수), `arn:aws:ec2:ap-northeast-2:676972757138:instance/i-0b2154ed899e3b85d`, `arn:aws:ssm:ap-northeast-2:676972757138:*`

> ⚠️ 겪었던 함정 3가지 (다 해결됨, 참고용):
> 1. EC2 역할에 ECR pull 누락 → 추가함
> 2. SSM document ARN을 `:676972757138:`로 적어서 실패 → 공용 문서라 `:*:`로 바꿔야 했음
> 3. `GetCommandInvocation` 권한 누락 → 폴링이 상태 못 읽어 "가짜 Pending" → 추가함

---

## 5. ECR (이미지 저장소)

- **레지스트리**: `676972757138.dkr.ecr.ap-northeast-2.amazonaws.com`
- **리포지토리 2개** (둘 다 Private, AES-256, Mutable 태그, Seoul):
  - `moly-voice` (URI: `.../moly-voice`)
  - `moly-llm` (URI: `.../moly-llm`)
- **Lifecycle 정책**: 각 리포에 "최근 5개 이미지만 보관, 초과분 삭제" (모두 선택 / 이미지 개수 / 5 / 만료)
- **태그 전략**: 워크플로가 `:latest` + `:<git-sha>` 두 태그로 push (latest는 deploy가 pull, sha는 롤백/추적용)

> ⚠️ backend 전환 시: `moly-backend` 리포를 새로 만들어야 함 (Private, lifecycle 5개 동일하게). moly-voice는 방치 or 삭제.

---

## 6. Parameter Store (시크릿) — `/moly/prod/` 경로

전부 **SecureString**(KMS `alias/aws/ssm` 암호화), 리전 서울. EC2 역할이 읽기+복호화 가능(검증됨).

**현재 존재하는 파라미터 (7개):**
```
/moly/prod/anthropic-api-key              ← 나중에 추가됨 (backend가 메인으로 쓸 것)
/moly/prod/deepgram-api-key               ← 음성용 (backend 전환 시 제거 대상)
/moly/prod/elevenlabs-api-key             ← 음성용 (backend 전환 시 제거 대상)
/moly/prod/groq-api-key                   ← 기존 llm용 (backend는 Anthropic 씀, 제거 대상)
/moly/prod/openai-api-key                 ← mem0 임베딩 (유지)
/moly/prod/supabase-db-connection-string  ← Postgres 연결 문자열 (유지)
/moly/prod/system-prompt                  ← 기존 llm 프롬프트 (backend 자체관리면 제거 대상)
```

### 시크릿 vs 설정값 원칙 (이 프로젝트의 규칙)
- **시크릿**(API 키, DB 연결 문자열, JWT secret 등) → Parameter Store SecureString
- **설정값**(모델명, 메모리 파라미터, 서버 URL 등) → 코드/compose에 평문 (비밀 아님)
- **판단 기준**: "이거 하나로 내 계정/돈/데이터에 접근 가능?" → 예면 시크릿
- 계정ID, EC2 ID, ECR 주소, 리전 = 비밀 아님 (레포에 넣어도 됨)

### 값 변경/추가 방법
- 값만 변경: AWS 콘솔 → Systems Manager → Parameter Store → 해당 파라미터 → 편집 → 저장
- 새 시크릿 추가: 파라미터 생성 → 이름 `/moly/prod/<소문자-하이픈>` → SecureString → 값
- **새 시크릿 추가 시 deploy.sh에 "파라미터명 → 환경변수명" 매핑 한 줄 추가 필요** (기존 값 수정은 콘솔만으로 끝)
- IAM 권한은 `/moly/prod/*` 와일드카드라, 새 파라미터도 자동으로 읽힘 (추가 권한 불필요)

---

## 7. moly-infra 레포 (배포 설정)

- 레포: `github.com/SOMA-OneTwoThree/moly-infra` (public)
- EC2에 `/root/moly-infra`로 clone돼 있음
- 담긴 파일:
  - **docker-compose.yml** — 현재 ai-voice + llm 두 서비스 (ECR 이미지 참조, build 없음). ai-voice는 `127.0.0.1:8001:8001`, llm은 호스트 노출 없이 내부 8000, ai-voice가 `http://llm:8000/chat` 호출.
  - **deploy.sh** — EC2에서 실행되는 배포 스크립트. 동작: 의존성 체크 → ECR 로그인(인스턴스 역할) → Parameter Store에서 `/moly/prod/` 전체 받아서 param명 마지막 세그먼트 → ENV 매핑 → .env 파일 생성(chmod 600, 시크릿 안 찍음) → SYSTEM_PROMPT는 여러 줄이라 `export` 후 compose `environment: - SYSTEM_PROMPT` 패스스루 → 해시 기반 설정 변경 감지(`.deploy-state/`) → compose pull → up -d → 바뀐 서비스만 `--force-recreate` → image prune → ps
  - **.gitignore** — ai-voice.env, llm.env, .deploy-state/
  - **setup-ec2.sh** — EC2 재현 부트스트랩 (docker/aws-cli-v2/nginx/certbot/jq 설치 + infra clone). 호스트 설치는 자동, 하지만 프로비저닝(EC2/IAM/EIP/DNS 생성)은 수동(주석으로만).
  - **DEPLOY_GUIDE.md** — 팀원 온보딩 가이드

---

## 8. GitHub Actions 워크플로 (앱 레포)

각 앱 레포(moly-voice, moly-llm)의 `.github/workflows/deploy.yml`. main push 시:
1. checkout
2. OIDC로 `moly-github-actions-role` assume (configure-aws-credentials@v4)
3. ECR 로그인
4. buildx (gha 캐시)
5. build-push (platforms linux/amd64, 태그 latest + git-sha)
6. **SSM으로 EC2 배포**: `aws ssm send-command`로 `cd /root/moly-infra && git pull --ff-only && sudo bash deploy.sh` 실행
   - CommandId 받아 폴링(10초 간격, 최대 ~10분), Success 아니면 워크플로 실패
   - stdout/stderr를 Actions 로그에 출력
   - 폴링 개선됨: `get-command-invocation` 실패를 `|| echo Pending`으로 숨기지 않음. InvocationDoesNotExist(직후 정상)는 30초 유예, 그 외 조회 실패는 드러내고 3회 누적 시 실패. 타임아웃도 구분.

> ⚠️ 겪었던 함정: SSM이 `sh`로 실행해서 `set -o pipefail`이 "Illegal option" 에러 → `./deploy.sh` 대신 **`bash deploy.sh`** 로 명시 실행해서 해결. (backend 워크플로도 이 방식 유지 필수)

> ⚠️ backend 전환 시: moly-backend 레포에 이 워크플로를 넣되 IMAGE_NAME을 `moly-backend`로. 차이는 이미지 이름 + name/concurrency group뿐.

---

## 9. GitHub 레포 목록 (SOMA-OneTwoThree 조직)

- `moly-voice` — 기존 음성 오케스트레이션 (전환 후 방치 예정)
- `moly-llm` — 기존 LLM 컨테이너 (전환 후 방치 예정)
- `moly-infra` — 배포 설정 (public, EC2에 clone됨)
- `moly-server` — Vercel 프론트/백 (Next+Supabase)
- `moly-backend` — **새로 만든 통합 백엔드** (전환 대상, 아직 배포 안 됨)

---

## 10. Supabase (참고)

- 현재 운영 DB: `tjkjspyqgmbljgyjlgaw.supabase.co` (잘 돌고 있음)
- 새로 만든 빈 DB: `qkgjlgzsharnilxnkytd.supabase.co` (moly-db, 테이블 없음 — 이전 여부 미결)
- Supabase JWT는 iOS 인증에 사용. backend가 JWKS로 JWT 검증 (PyJWT, `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`)

---

## 11. 배포가 실제로 도는 방식 (요약)

```
개발자가 앱 레포 main에 merge
  → GitHub Actions: 이미지 빌드 → ECR push (latest + sha)
  → SSM SendCommand → EC2에서 `cd /root/moly-infra && git pull && sudo bash deploy.sh`
  → deploy.sh: ECR pull → Parameter Store 시크릿 주입 → compose up (바뀐 것만 교체)
  → nginx(443)가 컨테이너로 프록시 → 서비스 동작
```
현재 이 파이프라인은 **정상 작동 확인됨** (ai-voice/llm 구조 기준).

---

## 12. 진행 중인 전환 (다음 세션에서 이어갈 것)

**기획 변경: 음성(ai-voice+llm) → 텍스트 백엔드(moly-backend) 단일 서비스**

- iOS 앱이 Supabase 로그인 → JWT 획득 → **EC2 backend로 직접 HTTP 요청**(패턴 A). backend가 JWT 검증(이미 구현돼 있음, PyJWT+JWKS).
- moly-backend = FastAPI 모놀리스. 엔드포인트 40여 개(대화/일기/루틴/지갑/상점/구독/광고/리뷰). Python 3.12, uv, 포트 8000, Dockerfile 이미 있음, `/health` 있음(무인증 정적 200).
- **같은 이미지 2프로세스**: API(`uvicorn app.main:app`) + 배치 워커(`python -m worker`, 매시 1틱, 멱등).
- 외부 의존: Supabase Postgres(+pgvector, asyncpg, pgbouncer 풀링), Anthropic Claude(메인 대화), OpenAI(mem0 임베딩), mem0(장기기억, 같은 pgvector), FCM(푸시), Apple StoreKit(결제), AdMob(광고).

### 전환 시 결정해야 할 미해결 항목
1. **배치 워커 실행 방식** — EC2 cron/systemd timer로 매시 `docker run ... python -m worker` 유력. 워커가 정확히 뭘 하는지/주기/누락 시 영향 확인 필요.
2. **FCM 서비스 계정 JSON** — `FCM_SERVICE_ACCOUNT_FILE`가 파일 경로. Parameter Store에 JSON 통째로 넣고 deploy.sh가 파일로 떨궈 마운트하는 방식(우리 패턴) 유력.
3. **어느 Supabase DB 쓸지 + 마이그레이션** — backend가 SQLAlchemy로 테이블 사용. 빈 DB면 마이그레이션(alembic 등) 필요. backend에 마이그레이션 도구 있는지 확인 필요.
4. **도메인** — `voice.moly.asia` 재활용 vs `api.moly.asia` 신규.
5. **메인 대화 엔드포인트 정확한 경로** — 보고서에서 잘려나옴(POST /chat 추정).

### 전환 시 인프라 변경 요약 (뼈대는 재활용)
- ECR: `moly-backend` 리포 신규 생성 (lifecycle 5개)
- IAM(github-actions-role): 신뢰정책에 moly-backend 레포 추가, 권한정책 ECRPushPull에 moly-backend 추가
- Parameter Store: 음성 키(deepgram/elevenlabs/groq) 제거, backend 시크릿 추가
  (추가 예정: supabase-service-role-key, supabase-anon-key, fcm-service-account. 유지: anthropic-api-key, openai-api-key, supabase-db-connection-string)
- compose: ai-voice/llm 제거, moly-backend 단일 서비스 (127.0.0.1:8000 루프백)
- nginx: WebSocket(8001) → 일반 HTTP 프록시(8000)로 수정
- deploy.sh: 시크릿 매핑 갱신, FCM 파일 처리 추가, (워커 처리)
- 워크플로: moly-backend 레포에 deploy.yml (IMAGE_NAME=moly-backend, bash 실행 유지)

### 재편 예정 시크릿 목록 (backend 기준 6종)
```
supabase-db-connection-string   (유지)
supabase-service-role-key       (신규)
supabase-anon-key               (신규)
anthropic-api-key               (유지, 이제 메인)
openai-api-key                  (유지, mem0 임베딩)
fcm-service-account             (신규, 파일형)
```
