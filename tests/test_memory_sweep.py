"""멈춘 기억 파이프라인 자동 재개.

여기서 지키는 것은 하나다: **죽은 잡은 replay로만 되살릴 수 있다.**

`ingest_dedup_key`는 (user, turn)만으로 정해지고 `enqueue`의 ON CONFLICT는 잡 상태와 무관하게
영구적이다. 그래서 turn N의 잡이 한 번 죽으면 어떤 코드도 그 turn을 다시 걸 수 없고, chat은
`ingest >= source`일 때만 새 잡을 걸므로 그 사용자의 기억은 **영원히 멈춘다**. 증상은 에러가
아니라 침묵이라 손으로 찾기 전에는 드러나지 않는다.

sweep이 `enqueue` 대신 `replay_dead`를 쓰는 이유가 이것이다. 이 파일은 그 선택이 코드에서
유지되는지 본다 — dev 실 DB에서 재개까지 확인한 것은 별도다(세션 기록).
"""
from __future__ import annotations

import inspect
import uuid

from worker import memory_sweep_jobs as sweep


def test_sweep_uses_replay_not_enqueue():
    """`enqueue_ingest`로 되살리려 하면 dedup 벽에 막혀 **아무 일도 일어나지 않는다**.

    실측: 죽은 turn에 `enqueue_ingest`를 다시 부르면 None이 돌아온다.
    """
    src = inspect.getsource(sweep)
    assert "replay_dead" in src, "sweep이 replay를 쓰지 않는다 — dedup 벽을 못 넘는다"
    assert "enqueue_ingest" not in src, (
        "sweep이 enqueue_ingest를 쓴다. 죽은 turn은 dedup_key가 같아 재등록이 조용히 무시된다"
    )


def test_sweep_only_targets_users_whose_cursor_is_behind():
    """커서가 따라잡힌 사용자는 잡이 죽었어도 되살릴 이유가 없다 — 체인이 이미 지나갔다."""
    sql = str(sweep._STALLED)
    assert "ingest_through_turn_seq < s.source_through_turn_seq" in sql
    assert "state = 'dead'" in sql
    assert "bootstrap_status = 'ready'" in sql, "bootstrap 미완 사용자는 live ingest 대상이 아니다"


def test_sweep_skips_already_replayed_jobs():
    """같은 잡을 매 틱 다시 replay하면 잡이 무한히 불어난다."""
    sql = str(sweep._STALLED)
    assert "r.replay_of = j.id" in sql
    assert "'ready', 'running', 'succeeded'" in sql


def test_replay_operation_id_is_deterministic():
    """operation_id가 매번 달라지면 멱등 가드가 무의미해진다."""
    jid = uuid.UUID("11111111-2222-3333-4444-555555555555")
    a = uuid.uuid5(sweep._REPLAY_NAMESPACE, str(jid))
    b = uuid.uuid5(sweep._REPLAY_NAMESPACE, str(jid))
    assert a == b
    assert a != uuid.uuid5(sweep._REPLAY_NAMESPACE, str(uuid.uuid4()))


def test_sweep_is_registered_with_the_consumer():
    """핸들러가 등록되지 않으면 잡은 만들어지고 아무도 집어가지 않는다."""
    from worker import consumer

    consumer._register_handlers()
    assert sweep.JOB_MEMORY_SWEEP in consumer._REGISTRY, (
        f"{sweep.JOB_MEMORY_SWEEP}가 consumer._register_handlers의 import 목록에 없다 — "
        "잡은 만들어지지만 아무도 집어가지 않는다"
    )
