# moly-backend

몰리 앱 백엔드 — FastAPI 모듈러 모놀리스 + 배치 워커. 계약·스키마·구조는 `docs/`(API_SPEC · ERD · ARCHITECTURE)가 단일 소스.

## 개발 환경

```bash
uv sync                                   # 의존성 설치(.venv)
cp .env.example .env                      # 시크릿 채우기(커밋 금지)
uv run uvicorn app.main:app --reload      # API 서버 → http://localhost:8000 (/docs)
uv run python -m worker                   # 배치 워커(현재 스텁)
uv run pytest                             # 테스트
uv run ruff check .                       # 린트
```

## 구조

- `app/` API 서버 — `main`(app factory) · `config`(설정) · `api/`(라우터) · `core/`(공통·보안)
- `worker/` 배치 워커(같은 코드, entrypoint만 분리)
- `tests/` pytest · `docs/` 계약·스키마·아키텍처(단일 소스)
