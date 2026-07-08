"""실 DB 통합 테스트 — 실제 Supabase Auth 토큰 + ASGI in-process 앱 + 실 Postgres.

흐름: service_role로 테스트 유저 생성(email_confirm) → password grant로 ES256 토큰 →
httpx ASGITransport로 앱 구동 → 전 엔드포인트 호출 + DB 부수효과 검증 → 유저 삭제(CASCADE).

LLM/StoreKit 등 유료·외부서명 의존 경로는 WARN으로 분리(DB 통합 판정과 무관).
실행: PYTHONPATH=. .venv/bin/python tests/integration/run_integration.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid

import asyncpg
import httpx

# ── env ──────────────────────────────────────────────────────────────
def _env() -> dict[str, str]:
    out = {}
    for line in open(".env"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out

ENV = _env()
URL = ENV["SUPABASE_URL"].rstrip("/")
SR = ENV["SUPABASE_SERVICE_ROLE_KEY"]
ANON = ENV["SUPABASE_ANON_KEY"]
PG = re.sub(r"^postgresql\+asyncpg://", "postgresql://", ENV["SUPABASE_DB_CONNECTION_STRING"])

# 앱은 .env를 pydantic-settings로 읽음 — ENVIRONMENT=local로 docs 노출/로컬 판정
os.environ.setdefault("ENVIRONMENT", ENV.get("ENVIRONMENT", "local"))

# ── 결과 집계 ─────────────────────────────────────────────────────────
PASS, FAIL, WARN = [], [], []
def ok(name, detail=""):
    PASS.append(name)
    print(f"  ✅ {name}  {detail}")

def bad(name, detail=""):
    FAIL.append(name)
    print(f"  ❌ {name}  {detail}")

def warn(name, detail=""):
    WARN.append(name)
    print(f"  ⚠️  {name}  {detail}")

def check(name, cond, detail=""):
    (ok if cond else bad)(name, detail)
    return cond

# ── Supabase Auth ────────────────────────────────────────────────────
# 이 프로젝트는 이메일 로그인 비활성(소셜 전용) + 익명 로그인 활성.
# 익명 sign-in → 실 ES256 토큰(aud=authenticated) + auth.users 행 → 가입 트리거로 profile 생성.
async def anon_signin(hc: httpx.AsyncClient) -> tuple[str, str]:
    r = await hc.post(f"{URL}/auth/v1/signup",
                      headers={"apikey": ANON, "Content-Type": "application/json"}, json={})
    r.raise_for_status()
    j = r.json()
    return j["user"]["id"], j["access_token"]

async def delete_user(hc: httpx.AsyncClient, uid: str):
    await hc.delete(f"{URL}/auth/v1/admin/users/{uid}",
                    headers={"apikey": SR, "Authorization": f"Bearer {SR}"})

# ── 메인 ─────────────────────────────────────────────────────────────
async def main():
    from app.main import create_app
    app = create_app()

    created_uids: list[str] = []

    ext = httpx.AsyncClient(timeout=30)          # 외부 Supabase 호출용
    db = await asyncpg.connect(PG, statement_cache_size=0)
    try:
        print("\n[셋업] 익명 sign-in → 실 토큰 + 가입 트리거")
        uid, token = await anon_signin(ext)
        created_uids.append(uid)
        check("가입 트리거로 profiles 자동 생성",
              await db.fetchval("select count(*) from profiles where id=$1", uuid.UUID(uid)) == 1,
              f"uid={uid[:8]}")

        transport = httpx.ASGITransport(app=app)
        c = httpx.AsyncClient(transport=transport, base_url="http://itest",
                              headers={"Authorization": f"Bearer {token}"}, timeout=60)
        async with c:
            await run_flow(c, ext, db, uid, token)
    finally:
        print("\n[정리] 테스트 유저 삭제(CASCADE)")
        for u in created_uids:
            await delete_user(ext, u)
        gone = await db.fetchval("select count(*) from profiles where id=$1", uuid.UUID(created_uids[0])) if created_uids else 1
        check("탈퇴 CASCADE — profiles 행 제거", gone == 0)
        await db.close()
        await ext.aclose()

    print("\n" + "=" * 56)
    print(f"결과: PASS {len(PASS)} · FAIL {len(FAIL)} · WARN {len(WARN)}")
    if FAIL:
        print("실패:", ", ".join(FAIL))
    print("=" * 56)
    sys.exit(1 if FAIL else 0)


async def run_flow(c, ext, db, uid, token):
    uidU = uuid.UUID(uid)

    # ── 인증 게이트 ──
    print("\n[인증]")
    r = await c.get("/me", headers={"Authorization": "Bearer bad.token"})
    check("무효 토큰 → 401", r.status_code == 401, f"got {r.status_code}")
    r = await c.get("/health", headers={"Authorization": ""})
    check("health 무인증 200", r.status_code == 200)

    # ── 계정 ──
    print("\n[계정]")
    r = await c.get("/me")
    j = r.json()
    check("GET /me 200", r.status_code == 200, str(r.status_code))
    check("온보딩 전 onboarded=false", j.get("profile", {}).get("onboarded") is False)
    check("초기 wallet.balance=0", j.get("wallet", {}).get("balance") == 0)
    check("entitlement 존재(trial 판정)", "entitlement" in j, str(j.get("entitlement", {}).get("plan")))

    r = await c.post("/onboarding", json={"nickname": "몰리테스트", "timezone": "Asia/Seoul", "language": "ko"})
    check("POST /onboarding 200", r.status_code == 200, str(r.status_code))
    r = await c.post("/onboarding", json={"nickname": "재시도", "timezone": "Asia/Seoul", "language": "ko"})
    check("온보딩 재호출 → 409 ALREADY_ONBOARDED",
          r.status_code == 409 and r.json().get("error", {}).get("code") == "ALREADY_ONBOARDED", str(r.status_code))

    r = await c.patch("/me", json={"nickname": "수정됨"})
    check("PATCH /me 200", r.status_code == 200)
    check("닉네임 반영", (await db.fetchval("select nickname from profiles where id=$1", uidU)) == "수정됨")

    r = await c.get("/me/notifications")
    j = r.json()
    check("GET /me/notifications 기본 on", r.status_code == 200 and j.get("morning_diary") is True and j.get("evening_chat") is True, str(j))
    r = await c.patch("/me/notifications", json={"morning_diary": False})
    check("PATCH notifications 200", r.status_code == 200)
    j = (await c.get("/me/notifications")).json()
    check("morning_diary off 반영", j.get("morning_diary") is False)

    r = await c.post("/me/push-token", json={"token": f"tok_{uid[:8]}", "platform": "ios"})
    check("POST /me/push-token 2xx", r.status_code in (200, 204), str(r.status_code))
    check("user_devices 행 생성", (await db.fetchval("select count(*) from user_devices where user_id=$1", uidU)) == 1)

    # ── 경제/충전소 ──
    print("\n[경제·충전소]")
    r = await c.post("/charging-station/attendance")
    check("출석 +10 → 200", r.status_code == 200, str(r.status_code))
    bal = await db.fetchval("select hay_balance from profiles where id=$1", uidU)
    check("잔액 10 반영", bal == 10, f"balance={bal}")
    check("hay_transactions attendance 원장 기록",
          (await db.fetchval("select count(*) from hay_transactions where user_id=$1 and type='attendance'", uidU)) == 1)
    r = await c.post("/charging-station/attendance")
    check("출석 재수령 → 409 ALREADY_CLAIMED",
          r.status_code == 409 and r.json().get("error", {}).get("code") == "ALREADY_CLAIMED", str(r.status_code))
    r = await c.get("/wallet")
    check("GET /wallet balance=10", r.status_code == 200 and r.json().get("balance") == 10)
    r = await c.get("/wallet/transactions")
    check("GET /wallet/transactions 1건", r.status_code == 200 and len(r.json().get("data", [])) >= 1)
    r = await c.get("/charging-station")
    check("GET /charging-station 200", r.status_code == 200)

    # ── 루틴 ──
    print("\n[루틴]")
    rid = []
    for nm in ("아침 물 마시기", "산책하기"):
        r = await c.post("/routines", json={"name": nm, "frequency_per_week": 5})
        if r.status_code in (200, 201):
            rid.append(r.json().get("id"))
    check("루틴 2개 생성", len(rid) == 2, f"ids={len(rid)}")
    r = await c.get("/routines")
    check("GET /routines 2건", r.status_code == 200 and len(r.json().get("data", [])) >= 2)
    for i in rid:
        r = await c.post(f"/routines/{i}/complete")
        check(f"루틴 완료체크 {i[:8]}", r.status_code in (200, 201), str(r.status_code))
    r = await c.get(f"/routines/{rid[0]}/statistics")
    check("루틴 통계 200", r.status_code == 200)
    r = await c.post("/charging-station/routine-reward")
    check("루틴 2개완료 보상 +10 → 200", r.status_code == 200, str(r.status_code))
    bal = await db.fetchval("select hay_balance from profiles where id=$1", uidU)
    check("잔액 20 반영(출석10+루틴10)", bal == 20, f"balance={bal}")
    r = await c.post("/charging-station/routine-reward")
    check("루틴보상 재수령 → 409", r.status_code == 409, str(r.status_code))
    r = await c.patch(f"/routines/{rid[0]}", json={"name": "이름변경"})
    check("PATCH 루틴 2xx", r.status_code in (200, 204), str(r.status_code))
    r = await c.delete(f"/routines/{rid[1]}")
    check("DELETE 루틴(soft) 200", r.status_code in (200, 204), str(r.status_code))
    check("soft delete — deleted_at 세팅",
          (await db.fetchval("select deleted_at from routines where id=$1", uuid.UUID(rid[1]))) is not None)

    # ── 상점(카탈로그 비어있음) ──
    print("\n[상점·인벤토리]")
    r = await c.get("/shop/products")
    check("GET /shop/products 200(빈 카탈로그)", r.status_code == 200)
    r = await c.get("/inventory")
    check("GET /inventory 200(빈)", r.status_code == 200)
    r = await c.get("/inventory/equipment")
    check("GET /inventory/equipment 200(4슬롯 null)", r.status_code == 200)
    r = await c.post("/shop/purchases", json={"product_id": str(uuid.uuid4())})
    check("없는 상품 구매 → 4xx",
          r.status_code >= 400 and r.status_code < 500, str(r.status_code))

    # ── 일기(비어있음) ──
    print("\n[일기]")
    r = await c.get("/diaries")
    check("GET /diaries 200(빈)", r.status_code == 200)

    # ── 구독 ──
    print("\n[구독]")
    r = await c.get("/subscription")
    check("GET /subscription 200(무구독)", r.status_code == 200, str(r.json())[:60])
    r = await c.get("/subscription/plans")
    check("GET /subscription/plans 200", r.status_code == 200)

    # ── 광고 ──
    print("\n[광고]")
    r = await c.post("/ads/reward", json={})
    # SSV 확정 레코드 없음 → 4xx(AD_VERIFY_FAILED 등) 기대
    check("SSV 없이 /ads/reward → 4xx", 400 <= r.status_code < 500, str(r.status_code))

    # ── 리뷰 ──
    print("\n[리뷰]")
    r = await c.post("/review/prompted")
    check("POST /review/prompted 2xx", r.status_code in (200, 204), str(r.status_code))
    check("review_prompted_at 세팅",
          (await db.fetchval("select review_prompted_at from profiles where id=$1", uidU)) is not None)

    # ── 대화(LLM — WARN 분리) ──
    print("\n[대화 — LLM 실호출, 실패해도 WARN]")
    r = await c.get("/chat/state")
    check("GET /chat/state 200", r.status_code == 200, str(r.json())[:80] if r.status_code == 200 else str(r.status_code))
    try:
        r = await c.post("/chat/messages",
                         headers={"Idempotency-Key": f"idem_{uuid.uuid4().hex}"},
                         json={"text": "안녕 몰리, 오늘 기분 어때?"})
        if r.status_code == 200:
            ok("POST /chat/messages 200(LLM 응답)", str(r.json())[:60])
            cnt = await db.fetchval("select count(*) from messages where user_id=$1", uidU)
            check("messages 테이블에 대화 저장", cnt >= 1, f"rows={cnt}")
        else:
            warn("POST /chat/messages 비200", f"{r.status_code} {str(r.text)[:120]}")
    except Exception as e:
        warn("POST /chat/messages 예외(LLM/mem0)", repr(e)[:120])

    # ── 탈퇴(DELETE /me) — 마지막 ──
    print("\n[탈퇴]")
    r = await c.post("/auth/logout", json={"push_token": f"tok_{uid[:8]}"})
    check("POST /auth/logout 200", r.status_code in (200, 204), str(r.status_code))
    r = await c.delete("/me")
    check("DELETE /me 200", r.status_code in (200, 204), str(r.status_code))


if __name__ == "__main__":
    asyncio.run(main())
