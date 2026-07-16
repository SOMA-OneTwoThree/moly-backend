#!/usr/bin/env python3
"""채팅 멱등 JSONB를 현재 응답 모델로 검사하고 비호환 행만 선택 정리한다.

기본 실행은 읽기 전용이다. ``--delete-invalid``를 명시한 경우에만 검사에서 실패한
행을 같은 트랜잭션에서 삭제한다. 응답 본문과 원본 idempotency key는 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import asyncpg
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.schemas.chat import PostMessageResponse  # noqa: E402


def _connection_string() -> str:
    value = settings.supabase_db_connection_string
    if not value:
        raise SystemExit("SUPABASE_DB_CONNECTION_STRING이 필요합니다.")
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", value)


def is_compatible_response(payload: Any) -> bool:
    if isinstance(payload, str):  # asyncpg 기본 JSON/JSONB codec는 문자열을 반환한다.
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return False
    try:
        PostMessageResponse.model_validate(payload)
    except ValidationError:
        return False
    return True


def _key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


async def run(*, delete_invalid: bool) -> int:
    connection = await asyncpg.connect(_connection_string(), statement_cache_size=0)
    try:
        # 서버사이드 커서 스트리밍 — 전체 테이블을 메모리에 올리지 않는다(검사에 정렬 불필요).
        # 검사와 삭제를 같은 트랜잭션에서 수행해 스냅샷이 어긋나지 않게 한다.
        async with connection.transaction():
            total = 0
            invalid: list[tuple[str, str]] = []
            async for row in connection.cursor(
                "SELECT user_id::text AS user_id, key, response FROM public.idempotency_keys"
            ):
                total += 1
                if not is_compatible_response(row["response"]):
                    invalid.append((str(row["user_id"]), str(row["key"])))

            print(f"검사 {total}건: 호환 {total - len(invalid)}건, 비호환 {len(invalid)}건")
            for user_id, key in invalid:
                print(f"  invalid user_id={user_id} key_sha256={_key_fingerprint(key)}")

            if not delete_invalid:
                print("읽기 전용 검사 완료. 삭제하려면 --delete-invalid를 명시하세요.")
                return len(invalid)
            if not invalid:
                print("삭제할 비호환 행이 없습니다.")
                return 0

            await connection.executemany(
                "DELETE FROM public.idempotency_keys WHERE user_id = $1::uuid AND key = $2",
                invalid,
            )
        print(f"비호환 행 {len(invalid)}건 삭제 완료.")
        return 0
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="채팅 멱등 응답 JSONB 계약 검사")
    parser.add_argument(
        "--delete-invalid",
        action="store_true",
        help="현재 응답 모델과 호환되지 않는 행만 삭제",
    )
    args = parser.parse_args()
    # 개수를 그대로 exit code로 쓰면 256의 배수가 0으로 잘린다(모듈로 256) — 불리언으로 고정.
    invalid_count = asyncio.run(run(delete_invalid=args.delete_invalid))
    raise SystemExit(1 if invalid_count else 0)


if __name__ == "__main__":
    main()
