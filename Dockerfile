FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# 의존성만 먼저 동기(레이어 캐시). package=false 라 프로젝트 자체는 빌드 안 함.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY worker ./worker

EXPOSE 8000
ENV PATH="/app/.venv/bin:$PATH"
ENV MEM0_TELEMETRY=False

# 비루트 유저로 실행(컨테이너 침투 시 권한 상승·탈출 완화).
RUN adduser --disabled-password --no-create-home --gecos "" appuser \
    && chown -R appuser /app
USER appuser

# 배포 커밋 sha 주입(빌드 아규먼트 → GIT_SHA env → settings.git_sha). /health version 노출·배포 확인용.
# 맨 뒤에 둬서 매 커밋마다 바뀌는 값이 앞 레이어(의존성·소스) 캐시를 깨지 않게 한다.
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA

# 기본 = API 서버. 배치 워커는 배포 시 CMD override: ["python", "-m", "worker"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
