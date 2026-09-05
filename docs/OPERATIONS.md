# Moly 백엔드 운영 가이드

> moly-backend가 EC2에서 어떻게 돌고, 어떻게 배포되고, 문제가 생기면 어디를 봐야 하는지.
> 대상 독자: 팀원 전체. (전환 작업 이력은 `DEPLOY_MIGRATION.md` 참고)
> 최종 갱신: 2026-07-08 (voice → backend 전환 완료 시점)

---

## 1. 전체 그림

```
iOS 앱 ── Supabase Auth 로그인(JWT) ─┐
   │                                  │
   └─ HTTPS https://voice.moly.asia ─ nginx(443, TLS) ─ 127.0.0.1:8000 ─ [moly-backend 컨테이너]
                                                                              │
EC2 systemd timer(매시 정각) ─ docker compose run --rm worker ───────────────┤ (같은 이미지, CMD만 다름)
                                                                              │
                                    Supabase Postgres(pgvector) · Anthropic · OpenAI(mem0) · FCM · Apple
```

- **계정 API는 이 서버가 아님** (2026-07-09 이관): `GET/PATCH/DELETE /me`·`POST /onboarding`·알림·푸시토큰·`POST /auth/logout`은 **moly-auth 서버**(레포 `moly-auth/backend`, Vercel `https://moly-server.vercel.app`)가 서빙. iOS는 base URL 2개(계정=버셀, 나머지=voice.moly.asia). 같은 Supabase DB를 본다.
- **서버**: AWS EC2 1대 (`i-0b2154ed899e3b85d`, 서울, t3.medium, 고정 IP `54.116.160.226`)
- **접속**: SSH 없음. AWS 콘솔 → EC2 → Connect → **Session Manager** → `sudo su -`
- **컨테이너**: `moly-backend` 1개 (FastAPI/uvicorn, 루프백 8000만 바인딩 — 외부 노출은 nginx가 담당)
- **배치 워커**: 상주하지 않음. systemd timer가 매시 정각에 같은 이미지를 워커 모드로 1회 실행
- **레포 역할**: `moly-backend`(앱 코드+CI) / `moly-infra`(compose·deploy.sh·systemd·nginx 참조본, EC2에 `/root/moly-infra`로 clone됨)

## 2. 배포 — 어떻게 나가나

**main에 merge하면 자동 배포된다. 그게 전부다.**

```
moly-backend main merge
  → GitHub Actions: Docker 빌드 → ECR push (latest + git-sha 태그)
  → SSM으로 EC2에 명령: cd /root/moly-infra && git pull && bash deploy.sh
  → deploy.sh: ECR pull → Parameter Store 시크릿으로 backend.env/FCM 파일 생성
               → compose up (변경분만 교체) → 워커 systemd 유닛 설치/갱신
  → Actions 로그에 EC2 배포 stdout/stderr 그대로 출력, 실패 시 워크플로 빨간불
```

- 진행 상황: GitHub → moly-backend → **Actions 탭**
- 배포만 다시 돌리기 (코드 변경 없이 시크릿/인프라 변경 반영): Actions에서 마지막 run → **Re-run all jobs**
- EC2에서 수동 배포: `cd /root/moly-infra && git pull --ff-only && bash deploy.sh` (멱등, 여러 번 실행 안전)
- **moly-infra를 바꿨을 때**: push만 해서는 반영 안 됨 — 다음 배포 때 git pull로 딸려감. 즉시 반영하려면 위의 재실행 또는 수동 배포.

이번 운세 승격처럼 기존 건초 광고 모델도 함께 바뀌는 변경은 `하위 호환 DB migration/검증 → 플래그 OFF
코드 배포 → infra 머지 → 검증한 동일 backend SHA 재배포 → 기능 smoke` 순서로 진행한다. 새 코드가
`reward_ad_sessions.expires_at`을 읽으므로 이번에는 DB보다 코드를 먼저 배포하지 않는다. infra 머지만 하고
끝내면 기능은 켜지지 않고 다음 무관한 backend 배포에서 예고 없이 켜진다. 운세 배포는 컨테이너 교체 전에 세 운세 테이블,
RLS·권한, 건초 광고 세션 만료 계약·전체 만료 인덱스, `messages.kind`, 운세 대화 부분 인덱스와 migration checksum을 읽기 전용
preflight로 확인한다.

## 3. 시크릿/설정값 관리

**원칙** — "이 값 하나로 계정/돈/데이터에 접근 가능한가?"
- **예 → 시크릿**: Parameter Store `/moly/prod/` (SecureString). 절대 레포/노션/슬랙에 붙여넣지 않는다
- **아니오 → 설정값**: 코드 기본값(`app/config.py`) 또는 `moly-infra/deploy.sh`의 backend.env 블록에 평문

**현재 시크릿 (Parameter Store):**
| 파라미터 | 용도 |
|---|---|
| `supabase-url` / `supabase-anon-key` / `supabase-service-role-key` | Supabase 접근 (service_role은 RLS 우회 전권 키) |
| `supabase-db-connection-string` | Postgres 연결 (asyncpg) |
| `anthropic-api-key` | 메인 대화 LLM |
| `openai-api-key` | mem0 임베딩 |
| `fcm-project-id` / `fcm-service-account` | 푸시 알림 (JSON은 통째로 저장, deploy.sh가 파일로 생성) |
| `revenuecat-webhook-auth` | RevenueCat 웹훅 Authorization 공유 시크릿 (RC 대시보드 웹훅 설정 값과 일치해야 함) |

**값만 바꿀 때**: AWS 콘솔 → Systems Manager → Parameter Store → 편집 → 저장 → **배포 재실행** (재실행해야 컨테이너에 반영됨 — deploy.sh가 env 변경을 감지해 컨테이너를 자동 재생성한다)

**새 시크릿 추가할 때**:
1. Parameter Store에 `/moly/prod/<소문자-하이픈>` SecureString 생성 (IAM은 와일드카드라 권한 작업 불필요)
2. `moly-infra/deploy.sh`의 backend.env 블록에 `ENV_NAME=${PARAMS[파라미터명]}` 한 줄 추가 (필수값이면 `required_keys`에도)
3. moly-infra push → 배포 재실행

**설정값(비밀 아님) 추가/변경**: `moly-infra/deploy.sh` backend.env 블록에 평문 추가. 현재 평문 값: `ENVIRONMENT=production`

## 4. 배치 워커

- **뭘 하나**: 유저 타임존 기준 로컬 04:00(전일 일기 생성)·09:00(아침 푸시)·21:00(저녁 푸시) 처리. 매시 정각 1틱, 멱등(같은 틱 두 번 돌아도 안전)
- **어떻게 도나**: `moly-worker.timer`(systemd)가 매시 정각 `docker compose run --rm worker` 실행. 유닛 파일은 moly-infra `systemd/`가 원본이고 deploy.sh가 자동 설치/갱신

```bash
# EC2에서 (sudo su - 후)
systemctl list-timers moly-worker.timer     # 다음 실행 시각
systemctl status moly-worker.service        # 마지막 실행 성공/실패
journalctl -u moly-worker.service -n 100    # 워커 로그 ("tick 완료 — 일기 n · 아침 n · 저녁 n")
systemctl start moly-worker.service         # 지금 즉시 수동 1틱 (멱등이라 안전)
```

## 5. 로그 보는 법

```bash
# API 서버 로그 (요청, 에러, LLM 호출)
docker logs --tail 200 -f moly-backend

# 워커 로그
journalctl -u moly-worker.service -n 100

# nginx 접근/에러 로그 (TLS, 프록시 문제)
tail -100 /var/log/nginx/access.log
tail -100 /var/log/nginx/error.log

# 컨테이너 상태 (healthy여야 정상)
docker ps
```

## 6. 장애 대응 런북

### `https://voice.moly.asia/health`가 안 열릴 때

1. **502 Bad Gateway** → 컨테이너 문제. EC2에서 `docker ps` 확인:
   - `Restarting (1) ...` = **크래시 루프**. `docker logs --tail 50 moly-backend`로 파이썬 트레이스백 확인.
     흔한 원인: 필수 env 누락(fail-closed 가드 — 에러 메시지에 누락된 변수명이 그대로 나옴). Parameter Store/deploy.sh에 값 추가 후 배포 재실행.
   - 컨테이너가 아예 없음 = 배포 실패. Actions 마지막 run 로그 확인.
2. **연결 자체가 안 됨 (timeout)** → nginx 또는 EC2 문제. `systemctl status nginx`, EC2 콘솔에서 인스턴스 상태 확인.
3. **TLS 인증서 에러** → `certbot renew --dry-run`으로 갱신 상태 확인 (자동 갱신이 기본이라 드묾).

### 배포(Actions)가 실패할 때

- 로그의 **"EC2 배포 표준에러"** 섹션부터 본다 — deploy.sh의 실제 에러가 거기 나옴.
- `SSM 파라미터 누락: ...` → Parameter Store에 해당 이름 생성/오타 수정
- ECR `AccessDenied` → IAM 역할 권한 확인 (핸드오프 문서의 함정 목록 참고)
- 폴링 타임아웃 → EC2에서 수동 배포를 돌려 어디서 멈추는지 확인

### 롤백 (배포했더니 서비스가 이상할 때)

ECR에는 커밋별 이미지가 남아 있다(`git-sha` 태그, 최근 5개).
```bash
# EC2에서: 직전 정상 커밋의 sha 태그로 임시 고정
cd /root/moly-infra
docker compose down backend
docker run -d --name moly-backend --env-file backend.env \
  -v /root/moly-infra/secrets/fcm-service-account.json:/secrets/fcm-service-account.json:ro \
  -p 127.0.0.1:8000:8000 \
  676972757138.dkr.ecr.ap-northeast-2.amazonaws.com/moly-backend:<정상이었던-git-sha>
```
그 후 근본 원인을 고쳐서 main에 merge하면 다음 배포가 다시 latest 체제로 복귀시킨다.
(간단한 코드 문제면 그냥 `git revert` → merge가 더 깔끔하다.)

## 7. 하지 말 것

- EC2에서 `backend.env`/`secrets/` 수동 편집 — 다음 배포 때 deploy.sh가 덮어쓴다. 값 변경은 반드시 Parameter Store(시크릿) 또는 deploy.sh(설정값)에서
- `docker compose down` 후 방치 — 서비스 전체 중단. 컨테이너 재시작은 `docker compose up -d` 또는 배포 재실행으로
- 시크릿 값을 로그/이슈/슬랙에 붙여넣기
- nginx 설정 직접 수정 후 moly-infra 참조본(`nginx/voice.moly.asia.conf`) 미반영 — 다음 사람이 참조본을 믿고 작업하다 사고남

## 8. 자주 쓰는 것 모음

| 하고 싶은 것 | 방법 |
|---|---|
| 코드 배포 | main에 merge (끝) |
| 시크릿 값 변경 | Parameter Store 편집 → Actions Re-run |
| 서버 로그 | Session Manager → `docker logs -f moly-backend` |
| 워커 상태 | `systemctl status moly-worker.service` |
| 헬스체크 | `curl https://voice.moly.asia/health` |
| 무인증 401 확인 | `curl -i https://voice.moly.asia/chat/state` |
