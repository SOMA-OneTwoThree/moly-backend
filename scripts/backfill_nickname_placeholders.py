"""기존 행 이름 → placeholder 백필 (SOMA-321/322/365).

⚠️ 이 스크립트는 **프로덕션 DB 쓰기**를 한다(불가역). 실행 전제(반드시 순서대로):
  (1) naming.py forward-only 코드 배포·안정화 확인
  (2) `--dry-run`(기본)으로 표면별 대상 행수 확인
  (3) DB 스냅샷/백업
  (4) 소량 유저 표본에 `--execute --user <id>`로 검증
  (5) coordinator 승인 후 전체 `--execute`

왜 지금이 골든 윈도우인가: 아직 아무 유저도 개명하지 않아 **모든 저장분의 이름 = 현재 프로필
닉네임**이라 `to_placeholder(content, 현재_nickname)`가 결정론적으로 정확하다. 누군가 개명하는
순간 그 유저의 과거 행은 옛 이름이 되어 정렬이 깨진다 → 실행 전 profiles 개명 흔적 조회 필수.

정확성·안전(코드 리뷰 반영):
- **행 skip 금지**: 이미 `{유저이름}` 토큰이 있어도 to_placeholder를 재적용한다. '{유저이름}과 Alex'
  같은 혼합 행의 Alex도 마스킹해야 하므로, 토큰 존재만으로 건너뛰면 실명이 잔존한다. to_placeholder는
  자연 멱등(이미 토큰이면 그 자리는 no-op)이라 재적용해도 안전. **after != before일 때만 UPDATE.**
- **lost-update 방어(낙관적 CAS)**: UPDATE ... WHERE pk AND content=before. 라이브 챗이 그 사이
  같은 행을 바꿨으면 0행 → 스킵(다음 실행서 재시도). 백필 창엔 워커·개명 정지 권장.
- **원문 미로깅**: 대화·일기 원문을 로그에 남기지 않는다 — 테이블·건수만.
- **per-user 로드**: 전량 적재(OOM) 대신 유저 단위로 조회·커밋한다.

관계형 4표면: messages.content · greetings.content · diaries.content · chat_contexts.memory_text.
mem0(벡터스토어)는 텍스트 UPDATE가 부적절 → delete_all 후 재추출이 별도(③b). 이 스크립트는 4표면만.
"""
from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, update

from app.core.db import get_sessionmaker
from app.models.chat_context import ChatContext
from app.models.diary import Diary
from app.models.greeting import Greeting
from app.models.message import Message
from app.models.profile import Profile
from app.services import naming

# (모델, 텍스트 컬럼) — 관계형 4표면. 모두 user_id로 스코프된다.
_TARGETS = (
    (Message, "content"),
    (Greeting, "content"),
    (Diary, "content"),
    (ChatContext, "memory_text"),
)


async def _nickname_map(session, user_id: str | None) -> dict:
    q = select(Profile.id, Profile.nickname)
    if user_id is not None:
        q = q.where(Profile.id == uuid.UUID(user_id))
    rows = (await session.execute(q)).all()
    return {r[0]: r[1] for r in rows if r[1]}


async def run(user_id: str | None, *, execute: bool) -> None:
    """dry-run(기본)=대상 행수만 집계. execute=낙관적 CAS로 실 UPDATE(유저 단위 커밋)."""
    counts = {model.__tablename__: 0 for model, _ in _TARGETS}
    raced = 0
    async with get_sessionmaker()() as session:
        nicks = await _nickname_map(session, user_id)
        for uid, nick in nicks.items():
            for model, col in _TARGETS:
                col_attr = getattr(model, col)
                rows = (
                    await session.execute(select(model).where(model.user_id == uid))
                ).scalars().all()
                for row in rows:
                    before = getattr(row, col) or ""
                    after = naming.to_placeholder(before, nick)
                    if after == before:
                        continue
                    counts[model.__tablename__] += 1
                    if execute:
                        pk_conds = [c == getattr(row, c.name) for c in sa_inspect(model).primary_key]
                        res = await session.execute(
                            update(model).where(*pk_conds, col_attr == before).values(**{col: after})
                        )
                        if res.rowcount == 0:  # 라이브 변경으로 CAS 실패
                            raced += 1
                            counts[model.__tablename__] -= 1
            if execute:
                await session.commit()

    verb = "변경" if execute else "대상"
    for tbl, n in counts.items():
        print(f"[{tbl}] {verb} {n}행")
    if execute and raced:
        print(f"⚠️ CAS 실패(백필 중 라이브 변경) {raced}건 — 백필 창에 쓰기 정지 후 재실행 권장")
    if execute:
        print("※ mem0(벡터스토어)는 별도 처리 필요 — delete_all 후 재추출(③b, 이 스크립트 범위 밖).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="실 UPDATE(기본은 dry-run 집계)")
    ap.add_argument("--user", default=None, help="특정 유저만(표본 검증용)")
    args = ap.parse_args()
    asyncio.run(run(args.user, execute=args.execute))


if __name__ == "__main__":
    main()
