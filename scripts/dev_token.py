#!/usr/bin/env python3
"""로컬 개발용 — Swagger에서 개별 API를 손으로 테스트하기 위한 실 토큰 발급 스크립트.

이 앱은 소셜 전용(이메일·익명 로그인 비활성)이라 브라우저만으로는 토큰을 못 얻는다.
이 스크립트가 service_role로 테스트 유저를 만들고 magiclink를 admin 발급→verify 해서
실제 Supabase access token(ES256)을 출력한다. 그 토큰을 Swagger "Authorize 🔒"에 넣으면
전 엔드포인트를 실 DB에 대고 눌러볼 수 있다.

⚠️ 앱 런타임과 무관한 **로컬 전용 CLI**다(엔드포인트 아님, 배포 안 됨, 네트워크로 안 닿음).
   시크릿은 코드에 없다 — 전부 .env에서 읽는다(.env는 gitignore). 키 없으면 동작하지 않는다.
   이미 커밋된 tests/integration/run_integration.py 와 동일한 보안 자세.

사용법:
    uv run python scripts/dev_token.py            # 토큰 발급 → 출력
    uv run python scripts/dev_token.py --cleanup  # 이 스크립트가 만든 테스트 유저 삭제(CASCADE)
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

import httpx

# 이 스크립트가 만드는 테스트 유저 식별 접두어(cleanup은 이 접두어로만 삭제).
DEV_EMAIL_PREFIX = "dev-swagger+"
DEV_EMAIL = f"{DEV_EMAIL_PREFIX}fixed@moly.test"


def load_env() -> dict[str, str]:
    """레포 루트 .env 를 읽는다(값은 커밋되지 않는 로컬 파일)."""
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if not env_path.exists():
        sys.exit(f"[에러] .env 없음: {env_path} — service_role 키가 있어야 동작합니다.")
    env: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def config(env: dict[str, str]) -> tuple[str, str, str]:
    url = env.get("SUPABASE_URL", "").rstrip("/")
    sr = env.get("SUPABASE_SERVICE_ROLE_KEY", "")
    anon = env.get("SUPABASE_ANON_KEY", "")
    if not (url and sr and anon):
        sys.exit("[에러] .env 에 SUPABASE_URL·SERVICE_ROLE_KEY·ANON_KEY 가 모두 있어야 합니다.")
    return url, sr, anon


def admin_headers(sr: str) -> dict[str, str]:
    return {"apikey": sr, "Authorization": f"Bearer {sr}", "Content-Type": "application/json"}


def find_user_id(c: httpx.Client, url: str, sr: str, email: str) -> str | None:
    r = c.get(f"{url}/auth/v1/admin/users?per_page=200", headers=admin_headers(sr))
    if r.status_code != 200:
        return None
    for u in r.json().get("users", []):
        if (u.get("email") or "").lower() == email.lower():
            return u.get("id")
    return None


def ensure_user(c: httpx.Client, url: str, sr: str, email: str) -> str:
    """테스트 유저 생성(있으면 재사용). 실 auth.users → 가입 트리거로 profiles 생성."""
    r = c.post(
        f"{url}/auth/v1/admin/users",
        headers=admin_headers(sr),
        json={"email": email, "password": uuid.uuid4().hex, "email_confirm": True},
    )
    if r.status_code in (200, 201):
        return r.json()["id"]
    uid = find_user_id(c, url, sr, email)
    if uid:
        return uid
    sys.exit(f"[에러] 테스트 유저 생성 실패: {r.status_code} {r.text[:200]}")


def mint_token(c: httpx.Client, url: str, sr: str, anon: str, email: str) -> dict:
    """magiclink admin 발급 → verify. 이메일·익명 로그인 없이 실 access token 획득."""
    r = c.post(f"{url}/auth/v1/admin/generate_link",
               headers=admin_headers(sr),
               json={"type": "magiclink", "email": email})
    hashed = r.json().get("hashed_token") if r.status_code == 200 else None
    if not hashed:
        sys.exit(f"[에러] generate_link 실패: {r.status_code} {r.text[:200]}")
    r2 = c.post(f"{url}/auth/v1/verify",
                headers={"apikey": anon, "Content-Type": "application/json"},
                json={"type": "magiclink", "token_hash": hashed})
    d = r2.json()
    if not d.get("access_token"):
        sys.exit(f"[에러] verify 실패: {r2.status_code} {r2.text[:200]}")
    return d


def cmd_cleanup(c: httpx.Client, url: str, sr: str) -> None:
    r = c.get(f"{url}/auth/v1/admin/users?per_page=200", headers=admin_headers(sr))
    users = r.json().get("users", []) if r.status_code == 200 else []
    deleted = []
    for u in users:
        email = u.get("email") or ""
        if email.startswith(DEV_EMAIL_PREFIX) or email.startswith("devtest+"):
            c.delete(f"{url}/auth/v1/admin/users/{u['id']}", headers=admin_headers(sr))
            deleted.append(email)
    print(f"[cleanup] 삭제 {len(deleted)}건(CASCADE): {deleted}")


def cmd_token(c: httpx.Client, url: str, sr: str, anon: str) -> None:
    uid = ensure_user(c, url, sr, DEV_EMAIL)
    tok = mint_token(c, url, sr, anon, DEV_EMAIL)
    at = tok["access_token"]
    print("=" * 60)
    print(f"user_id : {uid}")
    print(f"email   : {DEV_EMAIL}")
    print(f"expires : {tok.get('expires_in')}s")
    print("-" * 60)
    print("access_token (아래 한 줄을 복사):")
    print(at)
    print("-" * 60)
    print("1) http://localhost:8000/docs → 우측 상단 Authorize 🔒 → 위 토큰 붙여넣기")
    print("2) 아무 API나 Try it out 으로 실 DB에 대고 테스트")
    print("3) 끝나면:  uv run python scripts/dev_token.py --cleanup")
    print("=" * 60)


def main() -> None:
    ap = argparse.ArgumentParser(description="로컬 Swagger 테스트용 실 토큰 발급/정리")
    ap.add_argument("--cleanup", action="store_true", help="이 스크립트가 만든 테스트 유저 삭제")
    args = ap.parse_args()

    url, sr, anon = config(load_env())
    with httpx.Client(timeout=30) as c:
        if args.cleanup:
            cmd_cleanup(c, url, sr)
        else:
            cmd_token(c, url, sr, anon)


if __name__ == "__main__":
    main()
