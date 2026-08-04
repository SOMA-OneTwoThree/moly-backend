"""로컬 스크립트용 env 파일 해석 — **기본은 dev, prod는 명시해야 한다.**

    .env       = dev  (기본). 인자 없이 실행하면 여기로 간다 → 실수해도 피해가 dev에 그친다.
    .env.prod  = prod (실유저). `--env prod` 를 붙여야만 선택된다.

이 모듈은 **로컬 전용**이다. 서버(EC2)는 docker compose 가 `backend.env`(SSM 유래)를
실제 환경변수로 주입하므로 이 파일들을 읽지 않는다.

사용:
    from db.envfile import load_conn, announce, split_env_arg
    env, argv = split_env_arg(sys.argv[1:])
    dsn = load_conn(env)
    announce(env, dsn, commit=...)
"""
from __future__ import annotations

import re
import sys

DEFAULT = ".env"
ALIASES = {
    "dev": ".env",
    "local": ".env",
    "prod": ".env.prod",
    "production": ".env.prod",
}

# 이 저장소에서 직접 변경을 허용한 격리 개발 Supabase 프로젝트. 잘못된 .env 교체를 이름이 아니라
# 실제 DSN ref로 차단한다. 운영 ref는 코드에 둘 필요가 없다(unknown도 fail-closed).
DEV_PROJECT_REFS = {"wywzjslvxwttxkecbyis"}


def resolve(name: str | None) -> str:
    """별칭(dev/prod) 또는 직접 경로 → 실제 파일 경로. None이면 기본(dev)."""
    if not name:
        return DEFAULT
    return ALIASES.get(name.lower(), name)


def is_prod(name: str | None) -> bool:
    return resolve(name).endswith(".env.prod")


def split_env_arg(argv: list[str]) -> tuple[str | None, list[str]]:
    """`--env <이름>` 을 뽑아내고 나머지 인자를 돌려준다."""
    rest: list[str] = []
    env: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--env" and i + 1 < len(argv):
            env = argv[i + 1]
            i += 2
            continue
        if argv[i].startswith("--env="):
            env = argv[i].split("=", 1)[1]
            i += 1
            continue
        rest.append(argv[i])
        i += 1
    return env, rest


def load_conn(env_file: str | None = None) -> str:
    """대상 env 파일에서 DB DSN을 읽는다. asyncpg 용으로 `+asyncpg` 를 벗긴다."""
    path = resolve(env_file)
    try:
        lines = open(path).read().splitlines()
    except FileNotFoundError:
        raise SystemExit(f"env 파일 없음: {path}") from None
    for line in lines:
        line = line.strip()
        if line.startswith("SUPABASE_DB_CONNECTION_STRING"):
            v = line.split("=", 1)[1].strip().strip('"').strip("'")
            return re.sub(r"^postgresql\+asyncpg://", "postgresql://", v)
    raise SystemExit(f"{path} 에 SUPABASE_DB_CONNECTION_STRING 없음")


def project_ref(dsn: str) -> str:
    """DSN에서 Supabase 프로젝트 ref 추출(로그용). 실패 시 unknown."""
    m = re.search(r"postgres\.([a-z0-9]+):", dsn)
    return m.group(1) if m else "unknown"


def announce(env_file: str | None, dsn: str, *, commit: bool = False) -> None:
    """대상을 stderr로 항상 표시. 사고는 '어디에 쐈는지 몰라서' 난다."""
    path = resolve(env_file)
    tag = "PROD ⚠️  실유저" if is_prod(env_file) else "dev"
    mode = "COMMIT(실반영)" if commit else "dry-run"
    print(f"[대상] {tag} | env={path} | project={project_ref(dsn)} | {mode}", file=sys.stderr)


def assert_dev_target(env_file: str | None, dsn: str) -> None:
    """쓰기 스크립트용 fail-closed 개발 프로젝트 가드."""
    ref = project_ref(dsn)
    if is_prod(env_file) or ref not in DEV_PROJECT_REFS:
        raise SystemExit(
            f"개발 DB 쓰기 차단: 허용 ref={sorted(DEV_PROJECT_REFS)}, 실제 ref={ref}, "
            f"env={resolve(env_file)}"
        )
