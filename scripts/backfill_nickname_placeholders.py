"""[초안 — coordinator 승인 후에만 실행] 기존 행 이름 → placeholder 백필.

⚠️ 이 스크립트는 **DB 쓰기**를 한다. 이번 구현 범위(forward-only, 쓰기 0)에 포함되지 않는다.
   실행 전제(반드시 순서대로):
     (1) naming.py + forward-only 코드 배포·안정화 확인
     (2) 이 스크립트 `--dry-run`(기본)으로 대상 행수·치환 diff 샘플·과치환 반례 스캔
     (3) DB 스냅샷/백업
     (4) 소량 유저 표본에 `--execute --user <id>`로 검증
     (5) coordinator 승인 후 전체 `--execute`(트랜잭션 분할)

왜 지금이 백필 골든 윈도우인가: 아직 아무 유저도 개명하지 않아 **모든 저장분의 이름 = 현재
프로필 닉네임**이다. 따라서 `naming.to_placeholder(content, 그_유저의_현재_nickname)`가 결정론적으로
정확하다. 누군가 개명하는 순간 그 유저의 과거 행은 옛 이름이 되어 정렬이 깨진다.

멱등: 이미 `{name` 포함 행은 건너뛴다(to_placeholder가 자체 skip). 대상 표면 5곳:
  messages.content · greetings.content · diaries.content · chat_contexts.memory_text · mem0.

mem0는 벡터스토어라 텍스트 UPDATE가 부적절 → 별도 옵션(스크럽 or delete_all 후 재추출) 필요.
이 초안은 관계형 4표면의 dry-run 스캔만 구현한다. mem0·실 UPDATE는 coordinator가 확장한다.
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.chat_context import ChatContext
from app.models.diary import Diary
from app.models.greeting import Greeting
from app.models.message import Message
from app.models.profile import Profile
from app.services import naming

# (모델, 텍스트 컬럼) — 관계형 4표면.
_TARGETS = (
    (Message, "content"),
    (Greeting, "content"),
    (Diary, "content"),
    (ChatContext, "memory_text"),
)


async def _nickname_map(session) -> dict:
    rows = (await session.execute(select(Profile.id, Profile.nickname))).all()
    return {r[0]: r[1] for r in rows if r[1]}


async def scan(user_id: str | None) -> None:
    """dry-run — 각 표면의 치환 대상 행수와 diff 샘플을 출력한다(쓰기 없음)."""
    async with get_sessionmaker()() as session:
        nicks = await _nickname_map(session)
        for model, col in _TARGETS:
            q = select(model)
            if user_id is not None:
                q = q.where(model.user_id == user_id)
            rows = (await session.execute(q)).scalars().all()
            changed, samples = 0, []
            for row in rows:
                nick = nicks.get(row.user_id)
                before = getattr(row, col) or ""
                after = naming.to_placeholder(before, nick)
                if after != before:
                    changed += 1
                    if len(samples) < 5:
                        samples.append((before, after))
            print(f"[{model.__tablename__}.{col}] 대상 {changed}/{len(rows)}행")
            for b, a in samples:
                print(f"  - {b!r}\n  → {a!r}")


async def execute(user_id: str | None) -> None:  # noqa: ARG001
    """실 UPDATE — coordinator가 백업·표본검증·트랜잭션 분할을 붙인 뒤 구현한다."""
    raise SystemExit(
        "실행 미구현(의도적). 백업·표본검증·트랜잭션 분할·승인 절차를 붙인 뒤에만 실행하세요."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="실 UPDATE(기본은 dry-run)")
    ap.add_argument("--user", default=None, help="특정 유저만(표본 검증용)")
    args = ap.parse_args()
    asyncio.run(execute(args.user) if args.execute else scan(args.user))


if __name__ == "__main__":
    main()
