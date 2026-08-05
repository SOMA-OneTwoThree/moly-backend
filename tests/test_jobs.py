"""잡 플랫폼(W7) — claim/finalize/reaper/backoff 불변식.

실 DB에 붙지 않는다. 대신 `_FakeJobsDB`가 마이그레이션 DDL과 `app/services/jobs.py`의 SQL 의미
(상태 전이·fencing·attempt 증가 시점)를 in-memory로 재현해 **호출 흐름**을 검증하고, SQL 자체의
불변식(ready만 claim·fencing 조건·attempt 증가 위치·DELETE 없음)은 문장 구조로 따로 못 박는다.
둘을 나눈 이유: 시뮬레이터만 있으면 SQL을 바꿔도 테스트가 통과해버린다.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.services import jobs
from worker import consumer
from worker.consumer import JobCancelled, JobFatal, JobResult, JobRetry

_T0 = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────
# in-memory 시뮬레이터 — jobs.py의 각 문장을 SQL 텍스트로 식별해 같은 의미로 처리한다.
# ─────────────────────────────────────────────────────────────
class _R(dict):
    """RETURNING 행 — 이름 접근(mappings)과 위치 접근(row[0]) 둘 다 쓰는 호출측을 흉내낸다."""

    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return super().__getitem__(k)


class _Res:
    def __init__(self, rows: list[_R]) -> None:
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeJobsDB:
    """async_jobs 테이블 1개의 in-memory 재현."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, dict] = {}
        self.now = _T0
        self.seq = 0

    # -- 헬퍼 --------------------------------------------------
    def insert(self, **kw) -> uuid.UUID:
        self.seq += 1
        jid = uuid.uuid4()
        row = {
            "id": jid, "queue": "content", "job_type": "t", "user_id": None,
            "dedup_key": f"d{self.seq}", "payload": {}, "state": "ready", "priority": 100,
            "available_at": self.now, "expires_at": None, "attempt": 0, "max_attempts": 3,
            "lease_owner": None, "lease_token": None, "lease_until": None,
            "result_code": None, "result_detail": None, "last_error_code": None,
            "last_error_at": None, "created_at": self.now, "finished_at": None,
            "_seq": self.seq,
        }
        row.update(kw)
        self.rows[jid] = row
        return jid

    def _fenced(self, p: dict) -> dict | None:
        r = self.rows.get(p["id"])
        if (
            r is None
            or r["state"] != "running"
            or r["lease_owner"] != p["worker_id"]
            or r["lease_token"] != p["lease_token"]
        ):
            return None
        return r

    def _clear_lease(self, r: dict) -> None:
        r["lease_owner"] = r["lease_token"] = r["lease_until"] = None

    def _expired(self, r: dict) -> bool:
        return r["expires_at"] is not None and r["expires_at"] <= self.now

    # -- 문장 디스패치 -----------------------------------------
    def execute(self, stmt, p: dict) -> _Res:
        s = str(stmt)
        if s == str(jobs._ENQUEUE_SQL):
            return self._enqueue(p)
        if s == str(jobs._CLAIM_SQL):
            return self._claim(p)
        if s == str(jobs._SUCCESS_SQL):
            return self._success(p)
        if s == str(jobs._RETRYABLE_SQL):
            return self._retryable(p)
        if s == str(jobs._TERMINAL_SQL):
            return self._terminal(p)
        if s == str(jobs._HEARTBEAT_SQL):
            return self._heartbeat(p)
        if s == str(jobs._REAP_TERMINAL_RUNNING_SQL):
            return self._reap_terminal_running(p)
        if s == str(jobs._REAP_RETRYABLE_RUNNING_SQL):
            return self._reap_retryable_running(p)
        if s == str(jobs._REAP_TERMINAL_READY_SQL):
            return self._reap_terminal_ready(p)
        if s == str(jobs._STATS_SQL):
            return self._stats()
        # checkpoint producer가 읽는 기억 세대(잊어줘 stale 판정 기준). 잡 테이블 밖이지만
        # 같은 세션으로 오므로 여기서 받아 준다 — 기본 0(= forget 이력 없음).
        if "memory_generation" in s:
            return _Res([(getattr(self, "memory_generation", 0),)])
        # 삭제 장벽 조회(privacy.load_barrier). 기본은 행 없음 = compat 모드에서 통과.
        # 테스트가 `db.barrier_row = ("deleting", 1, op_id)`로 상태를 주입할 수 있다.
        if "FROM privacy_subject_barriers" in s:
            row = getattr(self, "barrier_row", None)
            return _Res([row] if row is not None else [])
        raise AssertionError(f"시뮬레이터가 모르는 문장: {s[:80]}")

    def _enqueue(self, p) -> _Res:
        for r in self.rows.values():  # UNIQUE (job_type, dedup_key) → ON CONFLICT DO NOTHING
            if r["job_type"] == p["job_type"] and r["dedup_key"] == p["dedup_key"]:
                return _Res([])
        jid = self.insert(
            queue=p["queue"], job_type=p["job_type"], user_id=p["user_id"],
            dedup_key=p["dedup_key"], payload=p["payload"], priority=p["priority"],
            available_at=p["available_at"] or self.now, expires_at=p["expires_at"],
            max_attempts=p["max_attempts"],
        )
        return _Res([_R(id=jid)])

    def _claim(self, p) -> _Res:
        cands = [
            r for r in self.rows.values()
            if r["queue"] == p["queue"] and r["state"] == "ready"
            and r["available_at"] <= self.now and r["attempt"] < r["max_attempts"]
            and (r["expires_at"] is None or r["expires_at"] > self.now)
        ]
        cands.sort(key=lambda r: (r["priority"], r["available_at"], r["created_at"], r["_seq"]))
        out = []
        for r in cands[: p["batch_size"]]:
            r["attempt"] += 1  # ← attempt는 claim 시점에 증가(finalize 아님)
            r["state"] = "running"
            r["lease_owner"] = p["worker_id"]
            r["lease_token"] = uuid.uuid4()
            r["lease_until"] = self.now + timedelta(seconds=p["lease_seconds"])
            out.append(_R({k: r[k] for k in (
                "id", "queue", "job_type", "user_id", "dedup_key", "payload", "attempt",
                "max_attempts", "lease_owner", "lease_token", "lease_until", "expires_at",
            )}))
        return _Res(out)

    def _success(self, p) -> _Res:
        r = self._fenced(p)
        if r is None:
            return _Res([])
        r.update(state="succeeded", result_code=p["result_code"],
                 result_detail=p["result_detail"], finished_at=self.now)
        self._clear_lease(r)
        return _Res([_R(id=r["id"])])

    def _retryable(self, p) -> _Res:
        r = self._fenced(p)
        if r is None:
            return _Res([])
        dead = r["attempt"] >= r["max_attempts"]
        r["state"] = "dead" if dead else "ready"
        r["available_at"] = r["available_at"] if dead else p["retry_at"]
        r["finished_at"] = self.now if dead else None
        r["last_error_code"] = p["error_code"]
        r["last_error_at"] = self.now
        self._clear_lease(r)
        return _Res([_R(state=r["state"], attempt=r["attempt"])])

    def _terminal(self, p) -> _Res:
        r = self._fenced(p)
        if r is None:
            return _Res([])
        r.update(state=p["state"], result_code=p["result_code"],
                 result_detail=p["result_detail"], finished_at=self.now,
                 last_error_code=p["error_code"], last_error_at=self.now)
        self._clear_lease(r)
        return _Res([_R(id=r["id"])])

    def _heartbeat(self, p) -> _Res:
        r = self._fenced(p)
        if r is None:
            return _Res([])
        r["lease_until"] = self.now + timedelta(seconds=p["lease_seconds"])
        return _Res([_R(lease_until=r["lease_until"])])

    def _reap_terminal_running(self, p) -> _Res:
        out = []
        for r in list(self.rows.values())[: None]:
            if (
                r["queue"] == p["queue"] and r["state"] == "running"
                and r["lease_until"] < self.now
                and (self._expired(r) or r["attempt"] >= r["max_attempts"])
            ):
                r["state"] = "cancelled" if self._expired(r) else "dead"
                r["last_error_code"] = "expired" if self._expired(r) else "attempts_exhausted"
                r["finished_at"] = r["last_error_at"] = self.now
                self._clear_lease(r)
                out.append(_R({k: r[k] for k in
                               ("id", "queue", "job_type", "attempt", "state", "last_error_code")}))
        return _Res(out[: p["reap_batch_size"]])

    def _reap_retryable_running(self, p) -> _Res:
        out = []
        for r in self.rows.values():
            if (
                r["queue"] == p["queue"] and r["state"] == "running"
                and r["lease_until"] < self.now and not self._expired(r)
                and r["attempt"] < r["max_attempts"]
            ):
                r.update(state="ready", available_at=self.now, finished_at=None,
                         last_error_code="lease_expired", last_error_at=self.now)
                self._clear_lease(r)
                out.append(_R(id=r["id"]))
        return _Res(out[: p["reap_batch_size"]])

    def _reap_terminal_ready(self, p) -> _Res:
        out = []
        for r in self.rows.values():
            if (
                r["queue"] == p["queue"] and r["state"] == "ready"
                and (self._expired(r) or r["attempt"] >= r["max_attempts"])
            ):
                r["state"] = "cancelled" if self._expired(r) else "dead"
                r["last_error_code"] = "expired" if self._expired(r) else "attempts_exhausted"
                r["finished_at"] = r["last_error_at"] = self.now
                out.append(_R({k: r[k] for k in
                               ("id", "queue", "job_type", "attempt", "state", "last_error_code")}))
        return _Res(out[: p["reap_batch_size"]])

    def _stats(self) -> _Res:
        agg: dict[tuple[str, str], list] = {}
        for r in self.rows.values():
            if r["state"] not in ("ready", "running", "dead"):
                continue
            agg.setdefault((r["queue"], r["state"]), []).append(r)
        out = []
        for (q, st), rs in agg.items():
            oldest = min((x["finished_at"] or x["created_at"]) for x in rs)
            out.append(_R(queue=q, state=st, n=len(rs),
                          oldest_age_s=(self.now - oldest).total_seconds()))
        return _Res(out)


class _FakeSession:
    def __init__(self, db: _FakeJobsDB) -> None:
        self.db = db
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt, params=None):
        return self.db.execute(stmt, params or {})

    async def scalar(self, stmt, params=None):
        if stmt is jobs._SUBJECT_BLOCKED_SQL:
            return False
        result = await self.execute(stmt, params)
        row = result.first()
        return row[0] if row is not None else None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def db() -> _FakeJobsDB:
    return _FakeJobsDB()


@pytest.fixture(autouse=True)
def _mute_slack(monkeypatch):
    """dead 경보의 실제 전송 차단 + 호출 기록(테스트가 개별로 다시 덮어쓸 수 있다)."""
    sent: list[dict] = []

    async def _alert(text, *, dedup_key=None):
        sent.append({"text": text, "dedup_key": dedup_key})

    monkeypatch.setattr(jobs.slack_notify, "alert", _alert)
    return sent


# ─────────────────────────────────────────────────────────────
# 1. SQL 구조 불변식 — 시뮬레이터가 가려줄 수 없는 자리
#
# 아래 검사는 전부 _norm()으로 공백을 지운 문자열에 대고 한다. 공백에 민감하면
# `attempt = attempt + 1`처럼 의미가 같은 변형이 검사를 그냥 빠져나가고, 시뮬레이터는
# SQL 의미를 독립 재구현하므로 그것도 못 잡는다(두 층 사이의 틈).
# ─────────────────────────────────────────────────────────────
def _norm(sql) -> str:
    """SQL 문자열에서 공백을 전부 제거 — 서식 변경에 흔들리지 않게."""
    return "".join(str(sql).split())


def test_claim_sql_targets_ready_only_and_skips_locked():
    """claim은 ready만 본다. lease reclaim(만료 회수)을 claim에 섞으면 안 된다."""
    sql = _norm(jobs._CLAIM_SQL)
    assert "state='ready'" in sql and "FORUPDATESKIPLOCKED" in sql
    assert "lease_until<" not in sql  # reaper 조건이 claim에 새어들지 않았는지
    assert "attempt<max_attempts" in sql


def test_attempt_increments_in_claim_and_never_in_finalize():
    """attempt 증가는 claim 한 곳뿐 — finalize가 또 올리면 재시도 횟수가 두 배로 소모된다."""
    assert "attempt=j.attempt+1" in _norm(jobs._CLAIM_SQL)
    for sql in (jobs._SUCCESS_SQL, jobs._RETRYABLE_SQL, jobs._TERMINAL_SQL):
        set_clause = _norm(sql).split("WHERE")[0].replace("max_attempts", "")
        # 대입(attempt=…)이 없어야 한다. 비교(attempt>=…)는 dead 판정에 필요하니 허용.
        assert "attempt=" not in set_clause


def test_every_finalize_path_is_fenced():
    """finalize·heartbeat은 전부 (id + running + lease_owner + lease_token) 조건이어야 한다."""
    for sql in (jobs._SUCCESS_SQL, jobs._RETRYABLE_SQL, jobs._TERMINAL_SQL, jobs._HEARTBEAT_SQL):
        s = _norm(sql)
        assert "id=:id" in s and "state='running'" in s
        assert "lease_owner=:worker_id" in s and "lease_token=:lease_token" in s


def test_jobs_service_never_deletes_rows():
    """dead 자동 삭제 금지 — 잡 행을 지우는 문장이 존재하면 안 된다(운영 replay는 새 행)."""
    import inspect

    src = inspect.getsource(jobs).upper()
    assert "DELETE FROM" not in src and "TRUNCATE" not in src


def test_dead_replay_inserts_a_new_lineage_row_without_reviving_original():
    sql = _norm(jobs._REPLAY_DEAD_SQL)
    assert sql.startswith("INSERTINTOasync_jobs")
    assert "replay_of" in sql and "j.state='dead'" in sql
    assert "UPDATE" not in sql.upper() and "DELETE" not in sql.upper()
    assert "replay:" in sql and ":operation_id" in sql
    assert "CAST(:operation_idASuuid)" in sql


def test_queue_config_matches_spec_table():
    """명세 §W7 소비자 실행값 표(잠정 초기값)."""
    expected = {
        "critical": (2, 2, 10.0, 30.0, 3),
        "interactive_async": (2, 2, 30.0, 45.0, 3),
        "content": (1, 1, 120.0, 150.0, 3),
        # 기억 색인 전용 lane. content와 섞으면 일기 배치가 도는 동안 기억이 밀린다.
        "memory": (2, 2, 120.0, 180.0, 3),
        "notification": (1, 1, 10.0, 20.0, 3),
        "maintenance": (1, 1, 60.0, 90.0, 3),
    }
    assert set(expected) == set(jobs.QUEUES)
    for q, (conc, batch, timeout, lease, attempts) in expected.items():
        c = jobs.queue_config(q)
        assert (c.concurrency, c.claim_batch, c.timeout_s, c.lease_s, c.max_attempts) == (
            conc, batch, timeout, lease, attempts
        )
        assert c.timeout_s < c.lease_s  # 핸들러가 lease보다 먼저 끊겨야 이중 실행이 안 생긴다


def test_unknown_queue_is_rejected():
    with pytest.raises(ValueError):
        jobs.queue_config("nope")


# ─────────────────────────────────────────────────────────────
# 2. enqueue 멱등
# ─────────────────────────────────────────────────────────────
async def test_enqueue_returns_id_and_is_idempotent_on_dedup_key(db):
    s = _FakeSession(db)
    first = await jobs.enqueue(
        s, queue="content", job_type="memory_extract", dedup_key="turn:1", payload={"a": 1}
    )
    second = await jobs.enqueue(
        s, queue="content", job_type="memory_extract", dedup_key="turn:1", payload={"a": 1}
    )
    assert first is not None and second is None  # UNIQUE(job_type, dedup_key) → 두 번째는 no-op
    assert len(db.rows) == 1


async def test_enqueue_does_not_commit(db):
    """호출측 트랜잭션에 합류해야 도메인 변경과 원자적으로 등록된다."""
    s = _FakeSession(db)
    await jobs.enqueue(s, queue="content", job_type="t", dedup_key="k")
    assert s.commits == 0


async def test_enqueue_uses_queue_max_attempts(db):
    s = _FakeSession(db)
    await jobs.enqueue(s, queue="notification", job_type="t", dedup_key="k")
    assert next(iter(db.rows.values()))["max_attempts"] == 3


# ─────────────────────────────────────────────────────────────
# 3. claim — 중복 0, 슬롯 clamp
# ─────────────────────────────────────────────────────────────
async def test_concurrent_claims_never_overlap(db):
    """두 소비자가 같은 큐를 동시에 claim해도 같은 잡을 두 번 가져가지 않는다(ready만 대상)."""
    for _ in range(4):
        db.insert(queue="critical")
    a, b = _FakeSession(db), _FakeSession(db)
    got_a, got_b = await asyncio.gather(
        jobs.claim(a, "critical", worker_id="A", batch_size=4),
        jobs.claim(b, "critical", worker_id="B", batch_size=4),
    )
    ids_a = {j.id for j in got_a}
    ids_b = {j.id for j in got_b}
    assert not (ids_a & ids_b)             # 중복 클레임 0
    assert len(ids_a | ids_b) == 4         # 유실도 0
    assert all(db.rows[i]["state"] == "running" for i in ids_a | ids_b)


async def test_claim_increments_attempt_and_commits(db):
    db.insert(queue="content")
    s = _FakeSession(db)
    [job] = await jobs.claim(s, "content", worker_id="W")
    assert job.attempt == 1 and s.commits == 1  # claim은 자체 완결 트랜잭션
    assert job.lease_owner == "W" and job.lease_token is not None


async def test_claim_batch_defaults_to_queue_config(db):
    for _ in range(5):
        db.insert(queue="content")  # content claim_batch = 1
    got = await jobs.claim(_FakeSession(db), "content", worker_id="W")
    assert len(got) == 1


async def test_claim_skips_unavailable_and_expired(db):
    db.insert(queue="content", available_at=db.now + timedelta(minutes=5))  # backoff 대기
    db.insert(queue="content", expires_at=db.now - timedelta(seconds=1))    # 만료
    db.insert(queue="content", attempt=3, max_attempts=3)                   # 소진
    assert await jobs.claim(_FakeSession(db), "content", worker_id="W", batch_size=10) == []


# ─────────────────────────────────────────────────────────────
# 4. fencing — lease 잃은 늦은 소비자
# ─────────────────────────────────────────────────────────────
async def test_finalize_success_applies_domain_in_same_transaction(db):
    db.insert(queue="content")
    s = _FakeSession(db)
    [job] = await jobs.claim(s, "content", worker_id="W")
    applied = []

    async def _apply(session):
        applied.append(session)

    ok = await jobs.finalize_success(s, job, result_code="done", apply_domain=_apply)
    assert ok is True and applied == [s]
    assert db.rows[job.id]["state"] == "succeeded"
    assert db.rows[job.id]["lease_owner"] is None


async def test_late_worker_cannot_finalize_and_domain_is_not_applied(db):
    """lease를 뺏긴 소비자가 늦게 돌아와도 확정 못 하고, 도메인 반영도 하지 않는다."""
    db.insert(queue="content")
    s = _FakeSession(db)
    [job] = await jobs.claim(s, "content", worker_id="OLD")
    db.rows[job.id].update(lease_token=uuid.uuid4())  # 다른 소비자가 이어받은 상황
    applied = []

    async def _apply(session):
        applied.append(session)

    ok = await jobs.finalize_success(s, job, apply_domain=_apply)
    assert ok is False and applied == []           # 도메인 미반영
    assert s.rollbacks == 1                        # 트랜잭션은 되돌린다
    assert db.rows[job.id]["state"] == "running"   # 남의 lease를 건드리지 않았다


async def test_late_worker_retryable_finalize_is_ignored(db):
    db.insert(queue="content")
    s = _FakeSession(db)
    [job] = await jobs.claim(s, "content", worker_id="OLD")
    db.rows[job.id].update(lease_owner="NEW")
    assert await jobs.finalize_retryable(s, job, error_code="timeout") is None
    assert db.rows[job.id]["state"] == "running"


async def test_heartbeat_extends_only_own_lease(db):
    db.insert(queue="content")
    s = _FakeSession(db)
    [job] = await jobs.claim(s, "content", worker_id="W")
    assert await jobs.heartbeat(s, job) is True
    db.rows[job.id].update(lease_token=uuid.uuid4())
    assert await jobs.heartbeat(s, job) is False


async def test_finalize_terminal_rejects_non_terminal_state(db):
    db.insert(queue="content")
    s = _FakeSession(db)
    [job] = await jobs.claim(s, "content", worker_id="W")
    with pytest.raises(ValueError):
        await jobs.finalize_terminal(s, job, state="succeeded", error_code="x")


# ─────────────────────────────────────────────────────────────
# 5. poison job — 재시도 소진 시 반드시 dead
# ─────────────────────────────────────────────────────────────
async def test_poison_job_reaches_dead_at_max_attempts(db):
    """claim마다 attempt가 늘어 max_attempts(3)번째 실패에서 dead. 무한 재시도 없음."""
    jid = db.insert(queue="content", max_attempts=3)
    states = []
    for _ in range(3):
        s = _FakeSession(db)
        [job] = await jobs.claim(s, "content", worker_id="W")
        # next_retry_at은 인자가 없으면 실벽시계를 쓴다(운영에선 DB now()와 같은 축). 시뮬레이터
        # 시계는 _T0에 고정돼 있으므로 여기서 db.now를 넘기지 않으면 available_at이 가짜 시계보다
        # 한참 미래로 찍혀 다음 claim이 빈 손이 된다 — 작성일 이후로는 항상 실패한다.
        states.append(await jobs.finalize_retryable(s, job, error_code="boom", now=db.now))
        db.now += timedelta(minutes=5)  # backoff 경과
    assert states == ["ready", "ready", "dead"]
    assert db.rows[jid]["attempt"] == 3 and db.rows[jid]["state"] == "dead"
    assert await jobs.claim(_FakeSession(db), "content", worker_id="W") == []  # 되살아나지 않는다


async def test_crashed_job_still_reaches_dead_without_finalize(db):
    """finalize를 못 하고 죽어도(크래시) 재클레임마다 attempt가 올라 결국 dead에 도달한다."""
    jid = db.insert(queue="content", max_attempts=3)
    for _ in range(3):
        await jobs.claim(_FakeSession(db), "content", worker_id="W")  # 확정 없이 사라짐
        db.rows[jid]["lease_until"] = db.now - timedelta(seconds=1)   # lease 만료
        await jobs.reap_queue(_FakeSession(db), "content")
        db.now += timedelta(seconds=1)
    assert db.rows[jid]["state"] == "dead"
    assert db.rows[jid]["last_error_code"] == jobs.ERROR_ATTEMPTS_EXHAUSTED


# ─────────────────────────────────────────────────────────────
# 6. reaper — 3단계 분리 + 각 단계 의미
# ─────────────────────────────────────────────────────────────
async def test_reaper_commits_each_phase_separately(db):
    """(1)terminal running → (2)retryable running → (3)terminal ready, 각각 별도 커밋."""
    s = _FakeSession(db)
    out = await jobs.reap_queue(s, "content")
    assert s.commits == 3
    assert set(out) == {"terminal_running", "requeued", "terminal_ready"}


async def test_reaper_requeues_expired_lease_when_attempts_remain(db):
    jid = db.insert(queue="content", state="running", attempt=1, max_attempts=3,
                    lease_owner="dead-proc", lease_token=uuid.uuid4(),
                    lease_until=db.now - timedelta(seconds=1))
    out = await jobs.reap_queue(_FakeSession(db), "content")
    assert out["requeued"] == 1
    r = db.rows[jid]
    assert r["state"] == "ready" and r["last_error_code"] == jobs.ERROR_LEASE_EXPIRED
    assert r["lease_owner"] is None and r["attempt"] == 1  # reaper는 attempt를 올리지 않는다


async def test_reaper_kills_expired_lease_when_attempts_exhausted(db, _mute_slack):
    jid = db.insert(queue="content", state="running", attempt=3, max_attempts=3,
                    lease_owner="dead-proc", lease_token=uuid.uuid4(),
                    lease_until=db.now - timedelta(seconds=1))
    out = await jobs.reap_queue(_FakeSession(db), "content")
    assert out["terminal_running"] == 1
    assert db.rows[jid]["state"] == "dead"
    assert len(_mute_slack) == 1  # dead 경보


async def test_reaper_cancels_past_expiry_without_alert(db, _mute_slack):
    """expires_at 경과는 cancelled — 정상 만료라 dead 경보 대상이 아니다."""
    jid = db.insert(queue="content", state="running", attempt=1,
                    expires_at=db.now - timedelta(seconds=1),
                    lease_owner="w", lease_token=uuid.uuid4(),
                    lease_until=db.now - timedelta(seconds=1))
    await jobs.reap_queue(_FakeSession(db), "content")
    assert db.rows[jid]["state"] == "cancelled"
    assert db.rows[jid]["last_error_code"] == jobs.ERROR_EXPIRED
    assert _mute_slack == []


async def test_reaper_finishes_terminal_ready_rows(db, _mute_slack):
    """claim되지 않은 채 소진/만료된 ready 행도 3단계에서 정리된다."""
    dead_id = db.insert(queue="content", state="ready", attempt=3, max_attempts=3)
    cancel_id = db.insert(queue="content", state="ready",
                          expires_at=db.now - timedelta(seconds=1))
    out = await jobs.reap_queue(_FakeSession(db), "content")
    assert out["terminal_ready"] == 2
    assert db.rows[dead_id]["state"] == "dead"
    assert db.rows[cancel_id]["state"] == "cancelled"
    assert len(_mute_slack) == 1  # dead 1건만 경보


async def test_reaper_never_deletes_dead_rows(db):
    """dead 자동 삭제 금지 — 몇 주기를 돌아도 행이 남아 있어야 관측·replay가 된다."""
    jid = db.insert(queue="content", state="ready", attempt=3, max_attempts=3)
    for _ in range(3):
        await jobs.reap_queue(_FakeSession(db), "content")
        db.now += timedelta(minutes=1)
    assert jid in db.rows and db.rows[jid]["state"] == "dead"


async def test_reaper_isolates_queues(db):
    """큐 A의 적체가 큐 B 처리를 막지 않는다(큐별 독립 대상)."""
    other = db.insert(queue="critical", state="ready", attempt=3, max_attempts=3)
    out = await jobs.reap_queue(_FakeSession(db), "content")
    assert out["terminal_ready"] == 0 and db.rows[other]["state"] == "ready"


# ─────────────────────────────────────────────────────────────
# 7. dead 경보 — PII 없음, dedup, 실패해도 상태 유지
# ─────────────────────────────────────────────────────────────
async def test_dead_alert_has_dedup_key_and_no_pii(db, _mute_slack):
    uid = uuid.uuid4()
    db.insert(queue="critical", job_type="rc_webhook", user_id=uid,
              payload={"email": "a@b.c"}, max_attempts=1)  # 1회 시도 → 첫 실패가 곧 dead
    s = _FakeSession(db)
    [job] = await jobs.claim(s, "critical", worker_id="W")
    await jobs.finalize_retryable(s, job, error_code="db_timeout")
    assert len(_mute_slack) == 1
    sent = _mute_slack[0]
    assert sent["dedup_key"] == "job_dead:critical:rc_webhook:db_timeout"
    assert str(uid) not in sent["text"] and "a@b.c" not in sent["text"]


async def test_slack_failure_does_not_roll_back_dead_state(db, monkeypatch):
    """경보 전송 실패가 job 상태를 되돌리면 안 된다(전이는 이미 commit)."""
    async def _boom(*a, **k):
        raise RuntimeError("slack down")

    monkeypatch.setattr(jobs.slack_notify, "alert", _boom)
    jid = db.insert(queue="content", attempt=0, max_attempts=1)
    s = _FakeSession(db)
    [job] = await jobs.claim(s, "content", worker_id="W")
    assert await jobs.finalize_retryable(s, job, error_code="boom") == "dead"
    assert db.rows[jid]["state"] == "dead"


# ─────────────────────────────────────────────────────────────
# 8. backoff — equal jitter
# ─────────────────────────────────────────────────────────────
def test_backoff_equal_jitter_bounds():
    """delay ∈ [raw/2, raw], raw = base * 2**(attempt-1) (attempt는 claim 때 증가된 값)."""
    for attempt, raw in ((1, 2.0), (2, 4.0), (3, 8.0)):
        for _ in range(50):
            delay = (jobs.next_retry_at(attempt, now=_T0) - _T0).total_seconds()
            assert raw / 2 <= delay <= raw


def test_backoff_is_capped():
    delay = (jobs.next_retry_at(30, now=_T0) - _T0).total_seconds()
    assert 30.0 <= delay <= 60.0  # cap 60s의 equal jitter


def test_backoff_respects_retry_after():
    """provider Retry-After가 계산값보다 늦으면 그쪽을 쓴다(둘 중 늦은 쪽)."""
    at = jobs.next_retry_at(1, now=_T0, retry_after_s=120)
    assert at == _T0 + timedelta(seconds=120)


def test_backoff_ignores_smaller_retry_after():
    at = jobs.next_retry_at(3, now=_T0, retry_after_s=0.5)
    assert (at - _T0).total_seconds() >= 4.0


# ─────────────────────────────────────────────────────────────
# 9. 큐 상태(이관 게이트 지표)
# ─────────────────────────────────────────────────────────────
async def test_queue_stats_reports_all_queues_with_dead_age(db):
    db.insert(queue="content")                                   # ready
    db.insert(queue="content", state="running")
    db.insert(queue="content", state="dead",
              finished_at=db.now - timedelta(seconds=90))
    db.insert(queue="content", state="succeeded")                # 집계 대상 아님
    stats = await jobs.queue_stats(_FakeSession(db))
    assert set(stats) == set(jobs.QUEUES)                        # 빈 큐도 0으로 노출
    assert stats["content"] == {"ready": 1, "running": 1, "dead": 1, "oldest_dead_age_s": 90}
    assert stats["critical"]["dead"] == 0 and stats["critical"]["oldest_dead_age_s"] is None


# ─────────────────────────────────────────────────────────────
# 10. 소비자 디스패치 — 실패 분류
# ─────────────────────────────────────────────────────────────
def _patch_sessions(monkeypatch, db):
    monkeypatch.setattr(consumer, "get_sessionmaker", lambda: lambda: _FakeSession(db))


async def _claim_one(db, queue="content"):
    return (await jobs.claim(_FakeSession(db), queue, worker_id="W"))[0]


async def test_unknown_job_type_goes_dead_immediately(db, monkeypatch, _mute_slack):
    """미지원 job_type은 재시도해도 같다 — 즉시 dead(경보)."""
    jid = db.insert(queue="content", job_type="not_registered")
    _patch_sessions(monkeypatch, db)
    await consumer.run_job(await _claim_one(db), jobs.queue_config("content"))
    assert db.rows[jid]["state"] == "dead"
    assert db.rows[jid]["last_error_code"] == "unknown_job_type"
    assert len(_mute_slack) == 1


async def test_handler_success_finalizes_with_result_and_domain(db, monkeypatch):
    jid = db.insert(queue="content", job_type="ok_job")
    applied = []

    async def _handler(job):
        async def _apply(session):
            applied.append(job.id)
        return JobResult(result_code="written", result_detail={"n": 1}, apply_domain=_apply)

    monkeypatch.setitem(consumer._REGISTRY, "ok_job", _handler)
    _patch_sessions(monkeypatch, db)
    await consumer.run_job(await _claim_one(db), jobs.queue_config("content"))
    assert db.rows[jid]["state"] == "succeeded" and db.rows[jid]["result_code"] == "written"
    assert applied == [jid]


async def test_handler_timeout_is_retryable(db, monkeypatch):
    jid = db.insert(queue="content", job_type="slow_job")

    async def _handler(job):
        await asyncio.sleep(5)

    monkeypatch.setitem(consumer._REGISTRY, "slow_job", _handler)
    _patch_sessions(monkeypatch, db)
    cfg = replace(jobs.queue_config("content"), timeout_s=0.01)
    await consumer.run_job(await _claim_one(db), cfg)
    assert db.rows[jid]["state"] == "ready" and db.rows[jid]["last_error_code"] == "timeout"


async def test_job_retry_error_reschedules_with_retry_after(db, monkeypatch):
    jid = db.insert(queue="content", job_type="rate_limited")

    async def _handler(job):
        raise JobRetry("rate_limited", retry_after_s=90)

    monkeypatch.setitem(consumer._REGISTRY, "rate_limited", _handler)
    _patch_sessions(monkeypatch, db)
    # retry_at은 시뮬레이터 시계가 아니라 실제 벽시계 기준으로 계산된다(next_retry_at).
    before = datetime.now(timezone.utc)
    await consumer.run_job(await _claim_one(db), jobs.queue_config("content"))
    r = db.rows[jid]
    assert r["state"] == "ready" and r["last_error_code"] == "rate_limited"
    assert r["available_at"] >= before + timedelta(seconds=90)  # Retry-After가 반영됐다


async def test_job_fatal_error_goes_dead(db, monkeypatch, _mute_slack):
    jid = db.insert(queue="content", job_type="bad_payload")

    async def _handler(job):
        raise JobFatal("schema_invalid")

    monkeypatch.setitem(consumer._REGISTRY, "bad_payload", _handler)
    _patch_sessions(monkeypatch, db)
    await consumer.run_job(await _claim_one(db), jobs.queue_config("content"))
    assert db.rows[jid]["state"] == "dead" and db.rows[jid]["attempt"] == 1
    assert len(_mute_slack) == 1


async def test_job_cancelled_is_terminal_without_alert(db, monkeypatch, _mute_slack):
    """대상 유저 탈퇴 등 — 처리 의미가 사라진 잡은 cancelled(경보 없음)."""
    jid = db.insert(queue="content", job_type="gone_user")

    async def _handler(job):
        raise JobCancelled("user_deleted")

    monkeypatch.setitem(consumer._REGISTRY, "gone_user", _handler)
    _patch_sessions(monkeypatch, db)
    await consumer.run_job(await _claim_one(db), jobs.queue_config("content"))
    assert db.rows[jid]["state"] == "cancelled" and _mute_slack == []


async def test_unexpected_exception_is_retryable(db, monkeypatch):
    jid = db.insert(queue="content", job_type="boom_job")

    async def _handler(job):
        raise RuntimeError("네트워크 끊김")

    monkeypatch.setitem(consumer._REGISTRY, "boom_job", _handler)
    _patch_sessions(monkeypatch, db)
    await consumer.run_job(await _claim_one(db), jobs.queue_config("content"))
    assert db.rows[jid]["state"] == "ready"
    assert db.rows[jid]["last_error_code"] == "unexpected_error"


async def test_finalize_failure_leaves_job_for_reaper(db, monkeypatch):
    """finalize 자체가 실패하면(DB 장애) 확정하지 않고 둔다 — lease 만료 후 reaper가 회수."""
    jid = db.insert(queue="content", job_type="ok_job")

    async def _handler(job):
        return JobResult()

    class _BrokenSession(_FakeSession):
        async def execute(self, stmt, params=None):
            raise RuntimeError("db down")

    monkeypatch.setitem(consumer._REGISTRY, "ok_job", _handler)
    job = await _claim_one(db)
    monkeypatch.setattr(consumer, "get_sessionmaker", lambda: lambda: _BrokenSession(db))
    await consumer.run_job(job, jobs.queue_config("content"))  # 예외가 새어나오지 않는다
    assert db.rows[jid]["state"] == "running"  # 확정 안 됨 → reaper 대상


async def test_register_rejects_duplicate_job_type():
    async def _h(job):
        return None

    consumer.register("dup_test_job", _h)
    try:
        with pytest.raises(ValueError):
            consumer.register("dup_test_job", _h)
        assert "dup_test_job" in consumer.registered_types()
    finally:
        consumer._REGISTRY.pop("dup_test_job", None)


# ─────────────────────────────────────────────────────────────
# 11. 소비자 루프 — 슬롯 clamp / 종료
# ─────────────────────────────────────────────────────────────
async def test_queue_loop_claims_within_free_slots_and_stops(db, monkeypatch):
    """claim batch는 그 큐의 빈 슬롯 수 이하로 clamp된다(다른 큐 슬롯을 빌려 쓰지 않는다)."""
    for _ in range(5):
        db.insert(queue="interactive_async", job_type="noop_job")
    seen: list[int] = []
    real_claim = jobs.claim

    async def _spy(session, queue, *, worker_id, batch_size=None, lease_s=None):
        seen.append(batch_size)
        return await real_claim(
            session, queue, worker_id=worker_id, batch_size=batch_size, lease_s=lease_s
        )

    async def _handler(job):
        return JobResult()

    monkeypatch.setitem(consumer._REGISTRY, "noop_job", _handler)
    monkeypatch.setattr(consumer.jobs, "claim", _spy)
    monkeypatch.setattr(consumer.settings, "job_idle_sleep_s", 0.01)
    _patch_sessions(monkeypatch, db)

    stop = asyncio.Event()
    task = asyncio.ensure_future(consumer.queue_loop("interactive_async", "W", stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    cfg = jobs.queue_config("interactive_async")
    assert seen and all(1 <= b <= cfg.claim_batch for b in seen)
    assert all(r["state"] == "succeeded" for r in db.rows.values())


async def test_reaper_loop_stops_on_event(db, monkeypatch):
    calls: list[str] = []

    async def _reap(session, queue, **k):
        calls.append(queue)
        return {}

    monkeypatch.setattr(consumer.jobs, "reap_queue", _reap)
    monkeypatch.setattr(consumer.settings, "job_reaper_interval_s", 0.01)
    _patch_sessions(monkeypatch, db)
    stop = asyncio.Event()
    task = asyncio.ensure_future(consumer.reaper_loop(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert set(calls) == set(jobs.QUEUES)  # 모든 큐를 돈다


# ─────────────────────────────────────────────────────────────
# 11. heartbeat·lease 상실·삭제 장벽 상태표 (재설계 15장 2단계)
# ─────────────────────────────────────────────────────────────
async def test_heartbeat_extends_lease_during_long_handler(db, monkeypatch):
    """긴 handler가 도는 동안 lease가 연장된다 — 예전엔 heartbeat를 아무도 부르지 않았다."""
    jid = db.insert(queue="content", job_type="slow")
    beats = {"n": 0}

    async def _handler(job):
        await asyncio.sleep(0.05)
        return consumer.JobResult()

    real_heartbeat = jobs.heartbeat

    async def _counting(session, job):
        beats["n"] += 1
        return await real_heartbeat(session, job)

    monkeypatch.setitem(consumer._REGISTRY, "slow", _handler)
    monkeypatch.setattr(jobs, "heartbeat", _counting)
    _patch_sessions(monkeypatch, db)
    cfg = replace(jobs.queue_config("content"), heartbeat_s=0.01, timeout_s=2.0)
    await consumer.run_job(await _claim_one(db), cfg)
    assert beats["n"] >= 1
    assert db.rows[jid]["state"] == "succeeded"


async def test_lease_lost_cancels_handler_and_does_not_finalize(db, monkeypatch):
    """fencing이 깨지면 확정하지 않는다 — 남의 실행 결과를 덮어쓰면 안 된다."""
    jid = db.insert(queue="content", job_type="slow")
    started = asyncio.Event()

    async def _handler(job):
        started.set()
        await asyncio.sleep(5)  # heartbeat가 먼저 실패한다
        return consumer.JobResult()

    async def _dead_heartbeat(session, job):
        return False  # 다른 소유자가 가져갔다

    monkeypatch.setitem(consumer._REGISTRY, "slow", _handler)
    monkeypatch.setattr(jobs, "heartbeat", _dead_heartbeat)
    _patch_sessions(monkeypatch, db)
    cfg = replace(jobs.queue_config("content"), heartbeat_s=0.01, timeout_s=5.0)
    await consumer.run_job(await _claim_one(db), cfg)
    assert started.is_set()
    # running 그대로 — 확정하지 않았다. reaper가 회수한다.
    assert db.rows[jid]["state"] == "running"


async def test_deleting_barrier_cancels_normal_job(db, monkeypatch):
    jid = db.insert(queue="content", job_type="noop", user_id=uuid.uuid4())
    db.barrier_row = ("deleting", 1, uuid.uuid4())

    async def _handler(job):
        raise AssertionError("삭제 중인 사용자의 일반 잡이 실행되면 안 됨")

    monkeypatch.setitem(consumer._REGISTRY, "noop", _handler)
    _patch_sessions(monkeypatch, db)
    await consumer.run_job(await _claim_one(db), jobs.queue_config("content"))
    assert db.rows[jid]["state"] == "cancelled"
    assert db.rows[jid]["last_error_code"] == "subject_deleting"


async def test_active_barrier_lets_normal_job_run(db, monkeypatch):
    """`active` 행이 있어도 일반 잡은 돌아야 한다 — 행 존재만으로 막으면 전 사용자가 멈춘다."""
    jid = db.insert(queue="content", job_type="noop", user_id=uuid.uuid4())
    db.barrier_row = ("active", 0, None)

    async def _handler(job):
        return consumer.JobResult()

    monkeypatch.setitem(consumer._REGISTRY, "noop", _handler)
    _patch_sessions(monkeypatch, db)
    await consumer.run_job(await _claim_one(db), jobs.queue_config("content"))
    assert db.rows[jid]["state"] == "succeeded"


def test_memory_queue_is_separate_from_content():
    """같은 큐를 쓰면 일기 300건이 도는 동안 기억 색인이 통째로 밀린다(감사 지적).

    content concurrency가 1이라 둘이 서로를 막는다.
    """
    assert jobs.QUEUE_MEMORY != jobs.QUEUE_CONTENT
    assert jobs.queue_config(jobs.QUEUE_MEMORY).concurrency >= 2


def test_mem0_jobs_go_to_the_memory_queue():
    import inspect

    from app.services import memory_pipeline

    for fn in (memory_pipeline.enqueue_ingest, memory_pipeline.enqueue_consolidate):
        assert "QUEUE_MEMORY" in inspect.getsource(fn), f"{fn.__name__}이 memory 큐를 안 쓴다"
