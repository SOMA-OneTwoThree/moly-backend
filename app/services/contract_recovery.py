"""범위가 이미 닫힌 약속 정리(contract_compile) dead를 dead 집계에서 빼기 위한 표시.

dead 행은 지우지 않는다(실패 기록 보존). 그런데 자동 재실행, 같은 범위를 덮는 다음 작업,
재실행 상한 뒤 모델 없이 진행만 하는 마지막 작업 중 하나가 그 범위를 닫아도 dead는 그대로
남는다. 그러면 일일 보고와 `/health/queues`가 더 기다릴 일이 없는 건을 계속 센다.

판정 기준은 하나다. 같은 세대에서 사용자의 약속 정리 진행 위치
(`chat_contexts.contract_processed_through_message_id`)가 dead 범위의 끝
(`payload.through_message_id`)에 닿았으면 그 범위는 닫혔다. 이 진행 위치는 약속 정리 작업이
그 범위를 끝냈을 때만 앞으로 간다(`worker/contract_jobs.py` `_RETENTION_ADVANCE`).
"닫혔다"는 정상 처리뿐 아니라 정책상 종결(모델 없는 마지막 작업, 마지막 시도의 후보 폐기)도
포함한다. 그런 종결은 각 작업의 로그와 결과(`result_detail`)에 따로 남는다.

dead payload는 7일 뒤 지워지므로(`jobs._SCRUB_RETENTION_SQL`) 판정 결과를 dead 행의
`result_code`에 표시로 남긴다. 실패 상태(`state`)·오류 코드(`last_error_code`)·시도 수는
그대로 둔다. `result_code`가 이미 있는 dead(즉시 종료된 결정적 실패)는 표시하지 않는다.
"""
from __future__ import annotations

from sqlalchemy import text

RANGE_CLOSED = "contract_range_closed"
_BATCH = 200

# 숫자 변환은 CASE 안에서만 한다. PostgreSQL은 조건을 어떤 순서로든 평가할 수 있지만
# CASE의 분기는 조건이 거짓이면 평가하지 않는다. 숫자가 아니면 NULL이 되어 비교가 거짓이 된다.
_GEN = ("CASE WHEN j.payload->>'memory_generation' ~ '^[0-9]{1,18}$' "
        "THEN (j.payload->>'memory_generation')::bigint END")
_THROUGH = ("CASE WHEN j.payload->>'through_message_id' ~ '^[0-9]{1,18}$' "
            "THEN (j.payload->>'through_message_id')::bigint END")

_MARK = text(f"""
WITH candidate AS (
  SELECT j.id FROM async_jobs j JOIN chat_contexts cc ON cc.user_id = j.user_id
  WHERE j.state = 'dead' AND j.job_type = 'contract_compile' AND j.queue = 'memory'
    AND j.result_code IS NULL
    AND j.payload_redacted_at IS NULL
    AND j.payload->'retention_enabled' = 'true'::jsonb
    AND (j.payload->>'retention_preparation_id') IS NULL
    AND {_THROUGH} > 0
    AND cc.memory_generation = {_GEN}
    AND cc.contract_generation = cc.memory_generation
    AND cc.contract_processed_through_message_id >= {_THROUGH}
  ORDER BY j.finished_at, j.id
  LIMIT :limit
  FOR UPDATE OF j SKIP LOCKED
)
UPDATE async_jobs j SET result_code = :marker
FROM candidate c
WHERE j.id = c.id AND j.state = 'dead' AND j.result_code IS NULL
RETURNING j.id
""")


async def mark_closed(session) -> int:
    """범위가 닫힌 dead에 표시를 남긴다. 호출한 쪽이 커밋한다. 새로 표시한 수를 돌려준다."""
    rows = (await session.execute(_MARK, {"marker": RANGE_CLOSED, "limit": _BATCH})).all()
    return len(rows)
