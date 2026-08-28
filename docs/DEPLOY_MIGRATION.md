# moly-backend 배포 전환 작업 로그

> ai-voice + llm (2컨테이너) → moly-backend (단일 서비스 + 배치 워커) EC2 전환 작업.
> 기준 문서: `docs/MOLY_INFRA_HANDOFF.md` (기존 인프라 현황).
> 이 문서는 작업이 진행될 때마다 갱신된다. (작업 시작: 2026-07-08)

---

## 1. 확정된 결정 사항

| 항목 | 결정 | 비고 |
|---|---|---|
| 인프라 코드 위치 | **moly-infra 유지** | 기존 파이프라인 재활용. compose/deploy.sh만 backend용으로 갱신 |
| 도메인 | **voice.moly.asia 재활용** | DNS/인증서 그대로. nginx만 8001 WS → 8000 HTTP 프록시로 수정 |
| Supabase DB | **기존 운영 DB 유지** `tjkjspyqgmbljgyjlgaw.supabase.co` | 팀원이 기존 테이블을 밀고 ERD 기반 새 DDL 적용 완료. 새 DB(moly-db)는 미사용. supabase 관련 기존 파라미터 값 변경 불필요 |
| 워커 실행 | **systemd timer** (매시 정각 `docker compose run --rm worker`) | ARCHITECTURE §3.3 "매시 1틱 크론" 충족. 상세: §5 |
| 메인 대화 엔드포인트 | `POST /chat/messages` | 핸드오프 §12-5 미결 항목 해소 |
| FCM 서비스 계정 | Parameter Store에 JSON 통째(SecureString) → deploy.sh가 파일로 생성 → 컨테이너 마운트 | 핸드오프 §12-2 방식 채택 |

## 2. 작업 체크리스트

### 레포 코드 작업 (이 세션에서 수행)
- [x] moly-infra 최신 pull
- [x] 진행 로그 문서 생성 (이 파일)
- [x] moly-backend: `.github/workflows/deploy.yml` 작성 (IMAGE_NAME=moly-backend, moly-voice 워크플로 기반)
- [x] moly-infra: `docker-compose.yml` 재작성 (backend 단일 서비스 + worker 프로파일, healthcheck 포함)
- [x] moly-infra: `deploy.sh` 재작성 (시크릿 매핑 갱신 · FCM 파일 생성 · systemd 유닛 설치 · `--remove-orphans`로 구컨테이너 자동 제거)
- [x] moly-infra: `systemd/moly-worker.service` + `moly-worker.timer` 작성
- [x] moly-infra: `nginx/voice.moly.asia.conf` 참조 설정 작성 (EC2 수동 반영용)
- [x] moly-infra: `.gitignore` / `README.md` / `setup_ec2.sh` 갱신
- [x] 워커 운영 정리 문서 (§5, 팀원 공유용)
- [x] 문법 검증: `bash -n deploy.sh` · `docker compose config` · 워크플로 YAML 파싱 통과
- [x] moly-infra 커밋/푸시 (2082994) — 2026-07-08 완료
- [x] moly-backend PR #22 merge → 첫 배포 성공 (run 28940746491) — 2026-07-08 완료

### AWS 콘솔/CLI 수동 작업 (코드 작업 후 진행)
- [x] ECR: `moly-backend` 리포 생성 (Private, AES-256, Mutable) + lifecycle "최근 5개만 보관" — 2026-07-08 완료
- [x] IAM `moly-github-actions-role` 신뢰정책: `repo:SOMA-OneTwoThree/moly-backend:ref:refs/heads/main` 추가 — 2026-07-08 완료
- [x] IAM `moly-github-actions-role` 권한정책 ECRPushPull: `repository/moly-backend` 추가 — 2026-07-08 완료
- [x] ~~Parameter Store 값 갱신~~ 기존 DB 유지로 결정 — `supabase-url`/`supabase-anon-key`/`supabase-db-connection-string` 기존 값 그대로
- [x] Parameter Store 신규: `supabase-service-role-key` (SecureString) — 2026-07-08 완료
- [x] Parameter Store 신규: `fcm-service-account` (SecureString, JSON 통째) — 2026-07-08 완료
- [x] Parameter Store 신규: `fcm-project-id` (String, 비밀 아님) — 2026-07-08 완료
- [x] EC2 nginx: 8001 WebSocket 프록시 → 8000 HTTP 프록시 수정 + reload — 2026-07-08 완료

### 컷오버 & 검증
- [x] moly-infra 변경분 push (2082994, +7307db9 StoreKit 설정)
- [x] moly-backend main push → Actions 빌드/배포 성공 (구 컨테이너 제거·워커 타이머 활성 확인)
- [x] `https://voice.moly.asia/health` 200 확인 — `{"status":"ok","env":"production"}` (2026-07-08 12:40 UTC)
- [ ] 인증 필요한 엔드포인트 1개 스모크 테스트 (Supabase JWT)
- [x] systemd timer 동작 확인 — 12:00 UTC 첫 틱 성공("tick 완료 — 일기 0 · 아침 0 · 저녁 0")
- [x] 구 컨테이너(ai-voice/llm) 자동 제거 확인 — 배포 로그에서 Stopped/Removed 확인

### 안정화 후 선택 정리 (급하지 않음, 파괴적 작업이므로 마지막에)
- [ ] Parameter Store 구 파라미터 삭제: `deepgram-api-key`, `elevenlabs-api-key`, `groq-api-key`, `system-prompt`, `slack-webhook-url`, `internal-service-token`, `stt-provider`, `require-auth`
- [ ] ECR `moly-voice`, `moly-llm` 리포 삭제 or 방치
- [ ] IAM 신뢰정책에서 moly-voice/moly-llm 라인 제거

## 3. backend가 필요로 하는 환경변수 (app/config.py 기준)

| ENV | 출처 | 비고 |
|---|---|---|
| `SUPABASE_URL` | SSM `supabase-url` (기존 값 유지) | JWKS URL·issuer 검증에도 사용 (미설정 시 전 요청 401) |
| `SUPABASE_ANON_KEY` | SSM `supabase-anon-key` (기존 값 유지) | |
| `SUPABASE_SERVICE_ROLE_KEY` | SSM `supabase-service-role-key` (신규) | |
| `SUPABASE_DB_CONNECTION_STRING` | SSM `supabase-db-connection-string` (기존 값 유지) | asyncpg + pgbouncer, 코드가 드라이버 접두사 정규화 |
| `ANTHROPIC_API_KEY` | SSM `anthropic-api-key` (유지) | 메인 대화 |
| `OPENAI_API_KEY` | SSM `openai-api-key` (유지) | mem0 임베딩 |
| `FCM_PROJECT_ID` | SSM `fcm-project-id` (신규, 평문) | 미설정이면 푸시만 조용히 스킵 (push.py) |
| `FCM_SERVICE_ACCOUNT_FILE` | compose 고정값 `/secrets/fcm-service-account.json` | deploy.sh가 SSM JSON을 파일로 떨굼 |
| `ENVIRONMENT` | deploy.sh 고정값 `production` | `local`이 아니면 /docs 비노출, StoreKit fail-closed |
| `REVENUECAT_WEBHOOK_AUTH` | SSM `revenuecat-webhook-auth` (신규, 시크릿) | RC 웹훅 Authorization 공유 시크릿. 없으면 부팅 거부(fail-closed) |

> ~~`APP_STORE_BUNDLE_ID`/`APP_STORE_ENVIRONMENT`/`APP_STORE_APP_APPLE_ID`~~ — moly-backend#23 RevenueCat 전환으로 삭제됨 (2026-07-08)
| `SUPABASE_JWKS_URL` | 불필요 | 미설정 시 `{SUPABASE_URL}/auth/v1/.well-known/jwks.json` 자동 유도 |

나머지(모델명, 토큰 한도 등)는 config.py 기본값 사용. 필요 시 backend.env에 추가하면 됨.

## 4. 아키텍처 (전환 후)

```
iOS 앱 ── Supabase Auth(JWT) ──┐
   │                            │
   └── HTTPS voice.moly.asia ── nginx(443) ── 127.0.0.1:8000 ── moly-backend 컨테이너 (uvicorn)
                                                                    │
EC2 systemd timer(매시 정각) ── docker compose run --rm worker ────┤ 같은 이미지, CMD만 다름
                                                                    │
                                              Supabase Postgres(기존 DB, pgvector) · Anthropic · OpenAI · FCM
```

## 5. 배치 워커 운영 방식 (팀원 공유용 요약)

**무엇**: `python -m worker` — 1회 실행하면 "지금 이 시각" 틱 하나를 처리하고 종료한다(멱등).
타임존별로 로컬 04:00(전일 일기 생성) / 09:00(아침 푸시) / 21:00(저녁 푸시)에 해당하는 유저를 스캔해 처리.

**어떻게 돌리나**: EC2의 **systemd timer**가 매시 정각에 API와 같은 Docker 이미지를 워커 모드로 1회 실행한다.

- `moly-worker.timer` — `OnCalendar=hourly` (매시 정각 트리거)
- `moly-worker.service` — `Type=oneshot`, `docker compose run --rm worker` 실행
- 유닛 파일은 moly-infra 레포 `systemd/`에 있고, **deploy.sh가 배포 때마다 자동 설치/갱신**한다 (수동 설치 불필요)

**운영 명령어** (EC2에서, `sudo su -` 후):
```bash
systemctl list-timers moly-worker.timer     # 다음 실행 시각 확인
systemctl status moly-worker.service        # 마지막 실행 결과
journalctl -u moly-worker.service -n 100    # 워커 로그 (틱 처리 건수 등)
systemctl start moly-worker.service         # 수동으로 즉시 1틱 실행 (멱등이라 안전)
```

### 왜 systemd timer인가 (선정 근거)

ARCHITECTURE §3.3과 워커 코드는 "외부에서 매시 1틱 실행"이라는 계약만 정했고, 호스트 구현 수단은 미지정이었다.

- **cron 대비**: journald 로그 통합, 실패 상태 조회, 중복 실행 방지, 재부팅 후 자동 복구 — 단일 EC2에선 사실상 표준.
- **상주 컨테이너(내부 스케줄러)**: 워커가 "1틱 후 종료" 구조라 코드 수정 필요 → 탈락.
- **EventBridge/ECS 크론 등 관리형**: EC2 1대 규모엔 부품만 늘어남. 서버가 늘거나 재시도·알림 요구가 생기면 그때 승격.
- 새로 필요한 IAM 권한 없음 — 워커는 API와 같은 이미지·env를 쓰므로 접근 권한도 기존 그대로.

## 6. 직접 해야 하는 작업 — 단계별 가이드 (컷오버 순서대로)

> 아래 순서대로 진행해야 중간에 서비스가 이상한 상태로 남지 않는다.
> STEP 1~4는 배포 트리거 전 준비, STEP 5~6이 실제 컷오버, STEP 7이 검증.

### STEP 0. 팀원에게 받을 것 (미리 확보)
- [ ] **FCM 프로젝트 ID** (Firebase 콘솔 → 프로젝트 설정 → 일반)
- [ ] **FCM 서비스 계정 JSON** (Firebase 콘솔 → 프로젝트 설정 → 서비스 계정 → 새 비공개 키 생성)
- 없어도 배포는 됨(푸시만 비활성) — 나중에 파라미터 추가 후 재배포하면 활성화

### STEP 1. Parameter Store 신규 3개 생성 (AWS 콘솔 → Systems Manager → Parameter Store, 서울 리전)

기존 DB를 유지하므로 `supabase-url`/`supabase-anon-key`/`supabase-db-connection-string`은 **편집 불필요** (기존 값 그대로).

**신규 생성 (이름 정확히):**
| 파라미터 | 타입 | 값 |
|---|---|---|
| `/moly/prod/supabase-service-role-key` | SecureString | 기존 프로젝트 Settings → API Keys → service_role |
| `/moly/prod/fcm-service-account` | SecureString | 서비스 계정 JSON **전체 내용 통째로** 붙여넣기 (여러 줄 OK) |
| `/moly/prod/fcm-project-id` | String | FCM 프로젝트 ID |

> IAM은 `/moly/prod/*` 와일드카드라 새 파라미터도 자동으로 읽힌다 — 권한 작업 불필요.

### STEP 2. ECR 리포 생성 (AWS 콘솔 → ECR, 서울 리전)
1. Create repository → 이름 `moly-backend`, **Private**, Tag immutability **Mutable**(비활성), 암호화 AES-256 (전부 기본값)
2. 생성 후 리포 클릭 → Lifecycle policy → Create rule:
   - Rule priority `1`, "최근 5개만 보관" — Image status **Any**, Match criteria **Image count more than `5`**, Action **expire**

### STEP 3. IAM 갱신 (AWS 콘솔 → IAM → Roles → `moly-github-actions-role`)
1. **Trust relationships 탭 → Edit trust policy**: `StringLike`의 `token.actions.githubusercontent.com:sub` 배열에 한 줄 추가:
   ```
   "repo:SOMA-OneTwoThree/moly-backend:ref:refs/heads/main"
   ```
2. **Permissions 탭 → 인라인 정책 `moly-github-action-permissions` 편집**: `ECRPushPull` statement의 Resource 배열에 추가:
   ```
   "arn:aws:ecr:ap-northeast-2:676972757138:repository/moly-backend"
   ```

### STEP 4. (선택) 로컬에서 이미지 빌드 확인 — ✅ 2026-07-08 완료
```bash
cd moly-backend && docker build -t moly-backend:test .
```
빌드 성공 + 컨테이너 부팅 후 `/health` 200 확인 완료.

### STEP 5. moly-infra 커밋/푸시
이 세션에서 변경한 파일: `docker-compose.yml`, `deploy.sh`, `systemd/`, `nginx/`, `.gitignore`, `README.md`, `setup_ec2.sh`
**⚠️ 이걸 push하는 순간, 다음 deploy.sh 실행부터 신 스택 기준으로 동작한다** (구 ai-voice/llm 컨테이너는 다음 배포 때 `--remove-orphans`로 제거됨). STEP 1~3이 끝난 뒤에 push할 것.

### STEP 6. moly-backend 커밋/푸시 = 첫 배포
`.github/workflows/deploy.yml` + `docs/` 를 main에 push → Actions가 자동으로:
빌드 → ECR push → EC2에서 `git pull && bash deploy.sh` (새 compose 적용, 구 컨테이너 제거, 워커 타이머 설치)
- Actions 로그에서 "배포 성공" 확인. 실패하면 로그의 EC2 표준에러 섹션 확인.

### STEP 7. EC2 nginx 전환 (SSM Session Manager 접속 → `sudo su -`)
```bash
# 백업 → 참조본 반영 → 검증 → 리로드
cp /etc/nginx/sites-available/default /etc/nginx/sites-available/default.bak-voice
cp /root/moly-infra/nginx/voice.moly.asia.conf /etc/nginx/sites-available/default
nginx -t && systemctl reload nginx
```
> 참조본의 인증서 경로가 실제와 같은지 먼저 확인: `ls /etc/letsencrypt/live/` 결과가 `voice.moly.asia`인지. 다르면 참조본의 ssl_certificate 두 줄을 기존 default 파일 값으로 맞춘다.

### STEP 8. 검증
```bash
# 외부에서
curl -i https://voice.moly.asia/health          # 200 기대
# EC2에서
docker ps                                        # moly-backend만 떠 있는지 (ai-voice/llm 없어야)
systemctl list-timers moly-worker.timer          # 다음 정각 예약 확인
systemctl start moly-worker.service              # 수동 1틱 (멱등, 안전)
journalctl -u moly-worker.service -n 50          # "tick 완료" 로그 확인
```
- iOS 앱(또는 curl + Supabase JWT)으로 인증 엔드포인트 1개 스모크: `GET /chat/state` 등

### STEP 9. 안정화 후 정리 (급하지 않음 — §2 마지막 체크리스트 참고)

## 7. 작업 일지

- **2026-07-08**: 전환 작업 시작. 핸드오프 문서 + backend 코드 분석. 미결 4항목(인프라 위치/도메인/DB/워커) 결정 완료(§1). moly-infra 최신 pull. 이 문서 생성.
- **2026-07-08 (컷오버)**: STEP 0~8 완료. Parameter Store 신규 3종 → ECR/IAM → 로컬 빌드 검증 → moly-infra push → PR #22 merge로 첫 배포 → nginx 전환 → **health 200 확인, 전환 성공**.
  - **트러블슈팅**: 첫 배포 후 컨테이너 크래시 루프 — 컷오버 당일 merge된 팀원의 StoreKit x5c 서명검증(`05a03a7`)이 비-local 부팅 시 `APP_STORE_BUNDLE_ID`/`APP_STORE_ENVIRONMENT=Production`/`APP_STORE_APP_APPLE_ID`를 강제(fail-closed). deploy.sh에 평문 3종 주입(`7307db9`)으로 해소. §3 표에도 반영.
  - 워커 systemd timer 정각 첫 틱 성공 확인.

## 8. 팀원 논의 필요 (서비스 로직 — 인프라에서 안 건드림)

- ~~앱 심사/TestFlight Sandbox 영수증 폴백~~ → **해소됨**: moly-backend#23에서 영수증 검증을 RevenueCat에 위임(직접 StoreKit 제거). Sandbox/Production 구분은 RC가 처리.
- **RevenueCat 웹훅 설정**: RC 대시보드 → 프로젝트 → Integrations → Webhooks에 URL `https://voice.moly.asia/…`(웹훅 엔드포인트 경로)과 Authorization 헤더 값 등록 필요. 그 값이 SSM `revenuecat-webhook-auth`와 일치해야 함 — RC 셋업 담당 팀원과 값 맞출 것.
