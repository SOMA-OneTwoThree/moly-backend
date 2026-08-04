"""기존 대화를 정규화 기억 source turn으로 백필하고 추출 잡을 건다.

기본은 dry-run이다. `--execute`가 있어야 DB를 쓴다. 유저별 트랜잭션이라 중간 중단 후
재실행할 수 있고, 이미 `memory_source_turn_messages`에 연결된 메시지는 건너뛴다.

실행 순서:
  1. expand 마이그레이션과 mode-aware API/consumer 배포
  2. 이 스크립트 dry-run
  3. `--execute --user UUID` 표본 → consumer drain → `--verify --user UUID`
  4. 전체 `--execute` → consumer drain → 전체 `--verify`
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text

from app.core.db import get_sessionmaker
from app.config import settings
from app.models.message import Message
from app.models.profile import Profile
from app.services import memory_repo
from db.envfile import announce, assert_dev_target


@dataclass(frozen=True, slots=True)
class Turn:
    representative_message_id: int
    message_ids: tuple[int, ...]
    committed_at: datetime


_MAPPED_IDS_SQL = text(
    "SELECT message_id FROM memory_source_turn_messages WHERE user_id=:user_id"
)

_VERIFY_SQL = text("""
SELECT
  (SELECT count(*) FROM messages m
   WHERE m.user_id=:user_id AND m.sender='user') AS inbound_total,
  (SELECT count(*) FROM messages m
   JOIN memory_source_turn_messages tm ON tm.user_id=m.user_id AND tm.message_id=m.id
   WHERE m.user_id=:user_id AND m.sender='user') AS inbound_mapped,
  COALESCE((SELECT memory_source_watermark FROM chat_contexts WHERE user_id=:user_id), 0)
    AS context_watermark,
  COALESCE((SELECT max(source_watermark) FROM memory_source_turns WHERE user_id=:user_id), 0)
    AS turns_watermark,
  (SELECT count(*) FROM async_jobs j WHERE j.user_id=:user_id
   AND j.job_type IN ('memory_extract','memory_reconcile','memory_embed','relationship_profile_refresh')
   AND j.state IN ('ready','running')) AS pending_jobs,
  (SELECT count(*) FROM async_jobs j WHERE j.user_id=:user_id
   AND j.job_type IN ('memory_extract','memory_reconcile','memory_embed','relationship_profile_refresh')
   AND j.state='dead' AND NOT EXISTS (
     SELECT 1 FROM async_jobs r WHERE r.replay_of=j.id AND r.state='succeeded'
   )) AS dead_jobs
""")


def _turns(messages: list[Message], mapped: set[int]) -> list[Turn]:
    turns: list[Turn] = []
    current: list[Message] = []
    for message in messages:
        if message.sender == "user":
            if current:
                head = current[0]
                turns.append(
                    Turn(
                        head.id,
                        tuple(m.id for m in current),
                        head.created_at or datetime.now(timezone.utc),
                    )
                )
            current = [] if message.id in mapped else [message]
        elif current and message.id not in mapped:
            current.append(message)
    if current:
        head = current[0]
        turns.append(
            Turn(
                head.id,
                tuple(m.id for m in current),
                head.created_at or datetime.now(timezone.utc),
            )
        )
    return turns


async def _user_ids(session, user: str | None) -> list[uuid.UUID]:
    q = select(Profile.id).order_by(Profile.id)
    if user:
        q = q.where(Profile.id == uuid.UUID(user))
    return list((await session.execute(q)).scalars().all())


async def run(*, user: str | None, execute: bool, verify: bool) -> None:
    env_file = os.getenv("MOLY_ENV_FILE", ".env")
    announce(env_file, settings.supabase_db_connection_string, commit=execute)
    if execute:
        assert_dev_target(env_file, settings.supabase_db_connection_string)
    totals = {"users": 0, "turns": 0, "messages": 0}
    failures: list[str] = []
    async with get_sessionmaker()() as session:
        user_ids = await _user_ids(session, user)

    for user_id in user_ids:
        async with get_sessionmaker()() as session:
            if verify:
                row = (await session.execute(_VERIFY_SQL, {"user_id": user_id})).mappings().first()
                if row is None:
                    continue
                ok = (
                    int(row["inbound_total"]) == int(row["inbound_mapped"])
                    and int(row["context_watermark"]) == int(row["turns_watermark"])
                    and int(row["pending_jobs"]) == 0
                    and int(row["dead_jobs"]) == 0
                )
                if not ok:
                    failures.append(
                        f"{user_id}: inbound={row['inbound_mapped']}/{row['inbound_total']} "
                        f"watermark={row['turns_watermark']}/{row['context_watermark']} "
                        f"pending={row['pending_jobs']} dead={row['dead_jobs']}"
                    )
                continue

            mapped = set(
                (await session.execute(_MAPPED_IDS_SQL, {"user_id": user_id})).scalars().all()
            )
            messages = list(
                (
                    await session.execute(
                        select(Message).where(Message.user_id == user_id).order_by(Message.id)
                    )
                ).scalars().all()
            )
            turns = _turns(messages, mapped)
            if not turns:
                continue
            totals["users"] += 1
            totals["turns"] += len(turns)
            totals["messages"] += sum(len(t.message_ids) for t in turns)
            if execute:
                for item in turns:
                    turn = await memory_repo.allocate_source_turn(
                        session,
                        user_id=user_id,
                        representative_message_id=item.representative_message_id,
                        message_ids=item.message_ids,
                        committed_at=item.committed_at,
                    )
                    await memory_repo.enqueue_extraction(
                        session,
                        user_id=user_id,
                        memory_generation=turn.memory_generation,
                        from_watermark=turn.watermark,
                        through_watermark=turn.watermark,
                        message_ids=turn.message_ids,
                    )
                await session.commit()

    if verify:
        if failures:
            print("검증 실패")
            for failure in failures:
                print(f"- {failure}")
            raise SystemExit(1)
        print(f"검증 통과: {len(user_ids)}명")
        return
    verb = "enqueue" if execute else "대상"
    print(
        f"{verb}: users={totals['users']} turns={totals['turns']} messages={totals['messages']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="실제 source/enqueue 쓰기")
    parser.add_argument("--verify", action="store_true", help="백필·잡 drain 완료 검증")
    parser.add_argument("--user", help="표본 유저 UUID")
    args = parser.parse_args()
    if args.execute and args.verify:
        parser.error("--execute와 --verify는 함께 쓸 수 없다")
    asyncio.run(run(user=args.user, execute=args.execute, verify=args.verify))


if __name__ == "__main__":
    main()
