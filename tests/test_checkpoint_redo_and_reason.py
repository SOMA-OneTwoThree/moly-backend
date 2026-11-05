"""요약 형식 검사 실패 이유 코드와 dead 요약 재생성의 후속 키(합성 데이터만 사용)."""
import logging
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import checkpoint as cp, checkpoint_repo as repo, context_retention as cr, jobs
from tests.test_checkpoint_parallel_publish import env as env  # noqa: F401
from worker import checkpoint_jobs as worker
from worker.consumer import JobRetry

REDO = {"redo_of": str(uuid.UUID(int=4242)), "redo_depth": 1}


@pytest.mark.parametrize("message,code", [
    ("progress_page_proof", "progress_page_proof"),
    ("delta_schema", "delta_schema"),
    ("provider가 빈 응답을 줬다(출력 예산 부족 의심)", "empty_provider_output"),
    ("마스킹·살균 후 요약이 비었다", "empty_after_sanitize"),
    ("요약 길이 초과 — 원문을 절단해 저장하지 않는다", "summary_too_long"),
    ("요약할 메시지가 없다", "no_messages"),
    ("요약할 허용 메시지가 없다", "no_allowed_messages"),
    ("요약 입력에 중복 message id가 있다: [3, 3, 9]", "duplicate_message_ids"),
    ("message id는 양수여야 한다: [0, 4]", "nonpositive_message_ids"),
    ("구형 줄글을 구조화 상태의 원본으로 승격할 수 없다", "legacy_summary_not_promotable"),
    ("요약에 마스킹되지 않은 실명이 남았다", "name_leak"),
    ("Some English sentence with 123", "other"),
    ("UPPER_CODE", "other"),
    ("snake_but_" + "x" * 80, "other"),
    ("알 수 없는 새 문구", "other"),
    ("", "other"),
])
def test_reason_code_passes_snake_codes_maps_fixed_korean_text_and_hides_the_rest(message, code):
    assert cp.error_reason_code(cp.CheckpointError(message)) == code


def test_reason_code_without_a_text_argument_is_other():
    assert cp.error_reason_code(cp.CheckpointError()) == "other"
    assert cp.error_reason_code(cp.CheckpointError(17)) == "other"


def test_every_fixed_korean_checkpoint_message_has_a_code():
    import inspect
    import re
    source = inspect.getsource(cp)
    fixed = set(re.findall(r'CheckpointError\(f?"([^"{]*[가-힣][^"{]*)', source))
    assert fixed and all(cp.error_reason_code(cp.CheckpointError(text)) != "other" for text in fixed)


async def test_summary_invalid_logs_only_the_normalized_reason(env, caplog):
    env.model.side_effect = cp.CheckpointError("요약 입력에 중복 message id가 있다: [1, 1, 2]")
    with caplog.at_level(logging.WARNING, logger="moly-worker"):
        with pytest.raises(JobRetry, match="summary_invalid"):
            await worker.handle_checkpoint(env.job)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "reason=duplicate_message_ids" in logged
    assert "[1, 1, 2]" not in logged and "중복" not in logged


async def test_redo_progress_page_key_does_not_collide_with_the_dead_family(env):
    env.job.payload = {**env.job.payload, **REDO}
    env.complete.return_value = False
    env.model.side_effect = cp.ProgressContinuation(state=cr.empty_state(), next_chunk=1,
        manifest=cp._progress_manifest(cp.progress_batches(env.sources, previous=None, language="ko"),
                                       previous=None, language="ko"), billable=1)
    result = await worker.handle_checkpoint(env.job)
    await result.apply_domain(env.session)
    queued = env.enqueue.await_args.kwargs
    assert queued["dedup_key"].startswith("checkpoint-progress:")
    assert queued["dedup_key"].endswith(f":redo:{REDO['redo_of']}")
    assert queued["payload"]["redo_of"] == REDO["redo_of"]


async def test_redo_publish_key_does_not_collide_with_the_dead_family(env):
    env.job.payload = {**env.job.payload, **REDO}
    result = await worker.handle_checkpoint(env.job)
    await result.apply_domain(env.session)
    queued = env.enqueue.await_args.kwargs
    assert queued["dedup_key"].startswith("checkpoint-publish:")
    assert queued["dedup_key"].endswith(f":redo:{REDO['redo_of']}")
    assert queued["payload"]["mode"] == "publish_retention" and queued["payload"]["redo_depth"] == 1


async def test_plain_jobs_keep_their_original_child_keys(env):
    result = await worker.handle_checkpoint(env.job)
    await result.apply_domain(env.session)
    assert ":redo:" not in env.enqueue.await_args.kwargs["dedup_key"]


async def test_enqueue_checkpoint_redo_builds_a_fresh_row_keyed_by_the_dead_job(monkeypatch):
    sources = [cp.SourceMessage(1, "user", "normal", "Synthetic source " * 180),
               cp.SourceMessage(2, "moly", "normal", "Synthetic response")]
    plan = replace(cp.plan(sources, previous=None, memory_generation=2,
        version=cp.RETENTION_SUMMARIZER_VERSION, reason="input_pressure", through_message_id=2),
        privacy_epoch=3)
    made = []

    async def enqueue(session, **kwargs):
        made.append(kwargs)
        return uuid.uuid4()
    monkeypatch.setattr(repo, "compat_binding_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(cr, "privacy_epoch_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(repo, "reserve_checkpoint_dependencies", AsyncMock())
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    dead = uuid.uuid4()
    assert await repo.enqueue_checkpoint(SimpleNamespace(), user_id=uuid.uuid4(), plan=plan,
                                         redo=(str(dead), 2))
    assert made[0]["dedup_key"] == f"checkpoint-redo:{dead}"
    assert made[0]["payload"] == {**plan.payload(), "packing_version": cp.PACKING_VERSION,
                                  "redo_of": str(dead), "redo_depth": 2}
    assert await repo.enqueue_checkpoint(SimpleNamespace(), user_id=uuid.uuid4(), plan=plan)
    assert made[1]["dedup_key"].startswith("user:") and "redo_of" not in made[1]["payload"]


# ── 요약 처리기가 남길 수 있는 dead 코드는 모두 "다시 만듦" 또는 "이유 있는 제외" 중 하나다 ──

def _handler_codes():
    """worker/checkpoint_jobs.py가 JobRetry·JobFatal로 남길 수 있는 오류 코드 전부(원문에서 읽는다)."""
    import inspect
    import re
    source = inspect.getsource(worker)
    calls = re.findall(r"\bJob(?:Retry|Fatal)\(", source)
    literal = re.findall(r'\bJob(?:Retry|Fatal)\(\s*"([a-z0-9_]+)"', source)
    # 코드가 문자열 그대로가 아닌 호출이 생기면 이 목록이 빠짐없다는 보장이 깨진다.
    assert len(calls) == len(literal), "JobRetry/JobFatal must use a literal error code"
    return set(literal)


# 소비자(worker/consumer.py)와 회수 작업(app/services/jobs.py)이 dead에 남기는 코드.
_CONSUMER_DEAD_CODES = {"unexpected_error", "timeout", jobs.ERROR_LEASE_EXPIRED,
                        jobs.ERROR_ATTEMPTS_EXHAUSTED, "unknown_job_type"}


def test_every_checkpoint_dead_code_is_recreated_or_deliberately_excluded():
    from worker import memory_sweep_jobs as sweep
    redo, excluded = sweep._CHECKPOINT_REDO_CODES, sweep._CHECKPOINT_NO_REDO_CODES
    codes = _handler_codes() | _CONSUMER_DEAD_CODES
    assert {"summary_name_leak", "summary_length", "summary_packing_length_snapshot",
            "summary_invalid", "summary_llm_failed"} <= codes
    assert not redo & set(excluded), "a code cannot be both re-created and excluded"
    missing = codes - redo - set(excluded)
    assert not missing, f"decide re-create or exclude (with a reason) for: {sorted(missing)}"
    assert all(isinstance(reason, str) and reason.strip() for reason in excluded.values())
    # 목록에만 있고 실제로는 나오지 않는 코드가 쌓이지 않게 한다.
    assert not (redo | set(excluded)) - codes


def test_fatal_payload_errors_stay_excluded():
    from worker import memory_sweep_jobs as sweep
    for code in ("invalid_payload", "unsupported_payload_schema", "invalid_baseline_binding",
                 "checkpoint_packing_version", "checkpoint_packing_cap", "checkpoint_packing_attempts",
                 "checkpoint_packing_control_invalid", "checkpoint_packing_mode", "checkpoint_packing_state",
                 "checkpoint_completed_proof_invalid"):
        assert code in sweep._CHECKPOINT_NO_REDO_CODES and code not in sweep._CHECKPOINT_REDO_CODES
