"""잡 소비자(W7) — `async_jobs`를 상주 소비하는 프로세스. `python -m worker.consumer`.

기존 15분 전역 틱(`worker/tick.py`)을 **대체하지 않는다.** 명세 §2 순서: 소비자를 먼저 상주 실행해
`/health/queues`의 큐별 ready/running/dead·oldest age가 보인 뒤, 기존 틱의 producer/handler를
큐로 하나씩 옮기고 그때 비활성화한다. 겹치는 동안은 `(job_type, dedup_key)` + ON CONFLICT DO NOTHING이
구·신 producer를 합쳐준다. 지금 등록된 핸들러는 **기억 잡 3종**(W8)과 **대화 요약 checkpoint**
(W11)이고, 기존 틱의 나머지 producer/handler 이관은 후속 작업이다.

구조: 큐마다 독립 루프 + 고정 슬롯. content가 밀려도 critical/notification 슬롯을 빌려 쓰지 않는다
(큐 A 적체가 큐 B를 막지 않는다). claim batch는 항상 그 큐의 **빈 슬롯 수 이하로 clamp**한다.
외부 호출(핸들러) 중에는 DB 세션도 row lock도 쥐지 않는다 — 세션은 claim/finalize 순간에만 연다.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.core.db import get_sessionmaker
from app.services import jobs
from app.services.jobs import ClaimedJob, QueueConfig

_log = logging.getLogger("moly-worker")


@dataclass(frozen=True, slots=True)
class JobResult:
    """핸들러 성공 결과.

    `apply_domain`은 **fencing UPDATE가 1행일 때만** 같은 트랜잭션에서 실행된다 — 도메인 반영과
    후속 잡 enqueue를 여기에 담으면 lease를 잃은 늦은 소비자가 잘못 확정하는 일이 없다.
    """

    result_code: str = "ok"
    result_detail: dict | None = None
    apply_domain: Callable[[Any], Awaitable[None]] | None = None


class JobRetry(Exception):
    """재시도 가능 실패(timeout·429·일시 네트워크/DB). backoff 후 재시도, attempt 소진 시 dead."""

    def __init__(self, error_code: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.retry_after_s = retry_after_s  # provider Retry-After(있으면 그보다 이르게 재시도 안 함)


class JobFatal(Exception):
    """재시도 불가(스키마 검증 실패·미지원 payload) — 즉시 dead."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class JobCancelled(Exception):
    """처리 의미가 사라짐(대상 유저 탈퇴·expires_at 경과) — cancelled. 경보 대상이 아니다."""

    def __init__(self, error_code: str = "cancelled") -> None:
        super().__init__(error_code)
        self.error_code = error_code


Handler = Callable[[ClaimedJob], Awaitable[JobResult | None]]

_REGISTRY: dict[str, Handler] = {}


def register(job_type: str, handler: Handler) -> None:
    """job_type → 핸들러. 같은 job_type 중복 등록은 조용한 덮어쓰기 대신 실패시킨다."""
    if job_type in _REGISTRY:
        raise ValueError(f"이미 등록된 job_type: {job_type!r}")
    _REGISTRY[job_type] = handler


def registered_types() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


async def _finalize(what: str, job: ClaimedJob, fn: Callable[[Any], Awaitable[Any]]) -> None:
    """finalize 공통 래퍼 — 세션 1개, 실패는 삼킨다.

    finalize 자체가 실패하면(DB 장애) 확정하지 않고 둔다: lease가 만료돼 reaper가 회수하고
    attempt는 claim 때 이미 증가했으므로 무한 재시도가 되지 않는다.
    """
    try:
        async with get_sessionmaker()() as session:
            await fn(session)
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "잡 %s 확정 실패(lease 만료 후 reaper 회수) — job_id=%s: %r", what, job.id, e
        )


async def _finalize_retry(
    job: ClaimedJob, error_code: str, retry_after_s: float | None = None
) -> None:
    await _finalize(
        "재시도", job,
        lambda s: jobs.finalize_retryable(
            s, job, error_code=error_code, retry_after_s=retry_after_s
        ),
    )


async def _finalize_terminal(job: ClaimedJob, state: str, error_code: str) -> None:
    await _finalize(
        state, job, lambda s: jobs.finalize_terminal(s, job, state=state, error_code=error_code)
    )


async def run_job(job: ClaimedJob, cfg: QueueConfig) -> None:
    """claim된 잡 1건 실행 + 확정. 어떤 경로로도 예외를 밖으로 올리지 않는다.

    재시도 분류(명세 §W7): timeout·429·일시 네트워크/DB = backoff 재시도 / 스키마 검증 실패·미지원
    payload = 즉시 dead / 대상 유저 삭제·expires_at 경과 = cancelled.
    (예외 객체는 except 블록을 벗어나면 사라지므로 오류 코드를 지역 변수로 먼저 꺼내 넘긴다.)
    """
    handler = _REGISTRY.get(job.job_type)
    if handler is None:
        # 미지원 job_type = 배포 스큐 또는 오타 producer. 재시도해도 같으므로 즉시 dead(경보 남김).
        await _finalize_terminal(job, "dead", "unknown_job_type")
        return
    try:
        result = await asyncio.wait_for(handler(job), timeout=cfg.timeout_s)
    except (asyncio.TimeoutError, TimeoutError):
        _log.warning(
            "잡 타임아웃(%.0fs) — job_id=%s job_type=%s attempt=%s",
            cfg.timeout_s, job.id, job.job_type, job.attempt,
        )
        await _finalize_retry(job, "timeout")
    except JobRetry as exc:
        await _finalize_retry(job, exc.error_code, exc.retry_after_s)
    except JobFatal as exc:
        await _finalize_terminal(job, "dead", exc.error_code)
    except JobCancelled as exc:
        await _finalize_terminal(job, "cancelled", exc.error_code)
    except Exception as exc:  # noqa: BLE001  # 미분류 예외는 일시 장애로 보고 재시도(소진되면 dead)
        _log.exception("잡 처리 예외 — job_id=%s job_type=%s: %r", job.id, job.job_type, exc)
        await _finalize_retry(job, "unexpected_error")
    else:
        res = result or JobResult()
        await _finalize(
            "성공", job,
            lambda s: jobs.finalize_success(
                s, job, result_code=res.result_code, result_detail=res.result_detail,
                apply_domain=res.apply_domain,
            ),
        )


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """stop이 오면 즉시 깨는 sleep(종료 지연 방지)."""
    with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


@dataclass
class _Slots:
    """큐 1개의 인플라이트 태스크 집합(= 점유 슬롯)."""

    tasks: set[asyncio.Task] = field(default_factory=set)

    def spawn(self, coro: Awaitable[None]) -> None:
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def free(self, concurrency: int) -> int:
        return concurrency - len(self.tasks)


async def queue_loop(queue: str, worker_id: str, stop: asyncio.Event) -> None:
    """한 큐의 claim 루프. 빈 슬롯만큼만 claim하고, 없으면 짧게 쉬며 폴링한다."""
    cfg = jobs.queue_config(queue)
    slots = _Slots()
    idle = settings.job_idle_sleep_s
    while not stop.is_set():
        free = slots.free(cfg.concurrency)
        if free <= 0:
            await _sleep_or_stop(stop, idle)
            continue
        try:
            async with get_sessionmaker()() as session:
                claimed = await jobs.claim(
                    session, queue, worker_id=worker_id, batch_size=min(cfg.claim_batch, free)
                )
        except Exception as e:  # noqa: BLE001  # DB 일시 장애로 소비자가 죽으면 안 된다
            _log.warning("claim 실패(queue=%s): %r", queue, e)
            await _sleep_or_stop(stop, idle)
            continue
        if not claimed:
            await _sleep_or_stop(stop, idle)
            continue
        for job in claimed:
            slots.spawn(run_job(job, cfg))
    if slots.tasks:  # graceful drain — 진행 중 잡은 마치고 종료(lease 만료 회수 최소화)
        await asyncio.gather(*list(slots.tasks), return_exceptions=True)


async def reaper_loop(stop: asyncio.Event, queues: tuple[str, ...] = jobs.QUEUES) -> None:
    """모든 큐에 대해 주기적으로 reaper 3단계. 한 큐의 실패가 다른 큐를 막지 않는다."""
    while not stop.is_set():
        for queue in queues:
            try:
                async with get_sessionmaker()() as session:
                    await jobs.reap_queue(session, queue)
            except Exception as e:  # noqa: BLE001
                _log.warning("reaper 실패(queue=%s): %r", queue, e)
        await _sleep_or_stop(stop, settings.job_reaper_interval_s)


def default_worker_id() -> str:
    """lease 소유자 식별자 — 호스트+PID(두 EC2·재기동을 구분)."""
    return f"{socket.gethostname()}:{os.getpid()}"


async def run_consumer(
    *,
    worker_id: str | None = None,
    queues: tuple[str, ...] = jobs.QUEUES,
    stop: asyncio.Event | None = None,
) -> None:
    """큐 루프 + reaper 루프를 함께 돈다. stop이 set되면 드레인 후 반환."""
    # import 시 핸들러 등록(순환 import 회피) — 기억 잡 3종(W8) + 대화 요약(W11).
    from worker import checkpoint_jobs, memory_jobs  # noqa: F401

    stop = stop or asyncio.Event()
    wid = worker_id or default_worker_id()
    _log.info("잡 소비자 시작 — worker_id=%s queues=%s handlers=%s", wid, queues, registered_types())
    await asyncio.gather(
        *(queue_loop(q, wid, stop) for q in queues),
        reaper_loop(stop, queues),
    )
    _log.info("잡 소비자 종료 — worker_id=%s", wid)


async def _main_async() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):  # systemd stop·Ctrl-C → 드레인 종료
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await run_consumer(stop=stop)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
