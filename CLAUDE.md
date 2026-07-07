# moly-backend

**몰리 앱**(AI 컴패니언 iOS)의 **백엔드**. 츤데레 카피바라 몰리와 한국어 채팅 → 몰리가 몰래 일기 작성. 프론트 = `moly-ios`(별도 레포). GitHub `SOMA-OneTwoThree/moly-backend`.

## 스택
Python 3.12 · **FastAPI** 모듈러 모놀리스 1서비스 + 배치 워커(같은 코드, 프로세스만 분리) · **uv** · Supabase(Auth+Postgres+pgvector) · Anthropic Claude(Sonnet=대화·개인일기 / Haiku=self-check·기억통합) · mem0 OSS(장기기억).

## 문서 (단일 소스 = 팀 노션. 로컬 `docs/`는 gitignore된 작업 사본, 노션 링크 TBD)
- API_SPEC(앱↔서버 계약·가격) · ERD(DB 스키마·enum·RLS, 짝 `ERD.dbml`) · ARCHITECTURE(구조). 충돌 시 노션(API_SPEC) 우선.
- 현황판 `docs/DEV_STATUS.md`(노션 공유용): 엔드포인트 구현 상태·규약·실행법

## 핵심 원칙
- 서버가 진실(재화·토큰·구독·가격). **클라 DB 직접 쓰기 전면 금지 — 모든 쓰기 API 경유**(RLS는 읽기+심층방어).
- HTTP 요청-응답만(스트리밍·WS·폴링 없음). 서버 선발신 = APNs 푸시뿐.
- 하루 경계 = 유저 로컬 04:00(`activity_date`). 미확정 수치는 전부 `app_config`(배포 없이 조정).

## 구조
- `app/` — API 서버. `main.py`(app factory) · `config.py`(pydantic-settings) · `api/`(라우터) · `core/`(공통·보안)
- `worker/` — 배치 워커(1시간 틱: 04:00 일기·기억통합 / 09:00·21:00 푸시). 같은 코드, entrypoint만 분리
- `tests/` pytest · `docs/` 계약·스키마·구조
- 모듈(예정): auth · chat · diary · economy · subscription · shop · routine · ads · account · gating

## 작업 규칙
- 개발 순서: **설계 → 계획 검증 → 구현 → 구현 검증(보안 특히)**. 검증은 대기업 수준으로 다방면·꼼꼼히.
- **PR 전 사용자 승인 필수**, 머지도 사용자. PR 전 git 안전 검증(시크릿·불필요 파일).
- 시크릿 코드/.env 커밋 금지. 페르소나 프롬프트 = **코드가 단일 소스**(외부 오버라이드 금지).
- 커밋/PR에 "Generated with Claude Code" 류 문구 금지. **불필요한 문서 생성 금지.**

## 진행상황
- [x] 레포 연동 · docs 3개 확정 · 기본 세팅(uv/FastAPI 스켈레톤 + health + Docker/env, moly-llm·voice 이관)
- [ ] 설계: DB 접근 계층(드라이버/ORM) · Supabase JWT 검증(JWKS vs remote getUser) · 모듈 계약
- [ ] 구현: auth → chat → diary … · 배포 타깃 확정(Fly/Render/Railway/ECS — ARCHITECTURE §11)
