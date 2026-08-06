"""저녁 푸시 개인화 — 슬롯·유효성·fail-closed 검수·클레임 순서·폴백(계획 v4 §8)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.core.time_utils import activity_date_for
from app.services import notify, push_personalization as pp

UID = uuid.uuid4()
_KST = ZoneInfo("Asia/Seoul")


def _profile(**over):
    p = {"id": UID, "timezone": "Asia/Seoul", "language": "ko", "nickname": "승민"}
    p.update(over)
    return SimpleNamespace(**p)


def _row(*, now: datetime | None = None, **over):
    """기본 anchor는 `now` 기준 어제다.

    ⚠️ 예전엔 `datetime.now()`를 썼다. 그런데 이 픽스처를 쓰는 테스트들은 `now`를 고정
    날짜로 주입하므로, 실제 날짜가 그 고정값을 지나가는 순간 anchor가 어긋나 실패했다
    (2026-08-06에 실제로 깨졌다). 시간에 의존하는 테스트는 시간을 주입받아야 한다.
    """
    now = now or datetime.now(timezone.utc)
    r = {
        "user_id": UID,
        "anchor_date": activity_date_for(now, "Asia/Seoul") - timedelta(days=1),
        "send_slot": time(14, 30),
        "body": "요즘 그 프로젝트는 잘 되고 있어? 나랑 얘기하자.",
        "language": "ko",
        "source_kind": "diary",
        "sent_count": 0,
    }
    r.update(over)
    return pp.PushRow(**r)


def _cfg(rollout="all", allowlist=(), sources=("diary", "transcript")):
    return pp.PushConfig(
        rollout=rollout, allowlist=frozenset(allowlist), sources=frozenset(sources)
    )


# ── 슬롯 계산 ──────────────────────────────────────────────────────────────

def test_slot_15min_floor_and_bounds():
    def local(h, m):
        return datetime(2026, 8, 4, h, m, tzinfo=_KST)

    assert pp.compute_slot(local(14, 37)) == time(14, 30)  # 15분 내림
    assert pp.compute_slot(local(8, 0)) == time(8, 0)      # 하한 경계 포함
    assert pp.compute_slot(local(19, 59)) == time(19, 45)  # 상한 직전
    assert pp.compute_slot(local(20, 0)) == time(20, 0)    # 야간 시작 → 20:00
    assert pp.compute_slot(local(23, 30)) == time(20, 0)
    assert pp.compute_slot(local(3, 50)) == time(20, 0)    # 새벽 → 20:00
    assert pp.compute_slot(local(7, 59)) == time(20, 0)    # 08:00 직전까지 야간


# ── 설정 파싱(fail-closed) ─────────────────────────────────────────────────

async def _parse(monkeypatch, raw):
    async def _get(session, keys):
        return raw

    monkeypatch.setattr(pp, "get_config_values", _get)
    return await pp.effective_push_config(None)


async def test_config_absent_is_off(monkeypatch):
    cfg = await _parse(monkeypatch, {})
    assert cfg.rollout == "off" and cfg.sources == frozenset({"diary"})


async def test_config_invalid_values_are_off(monkeypatch):
    # 미지값·불리언·문자열 "false" 전부 off — 켜는 건 오직 정확한 화이트리스트 문자열.
    for bad in ("on", True, False, "false", 1, ["all"], {"v": "all"}):
        cfg = await _parse(monkeypatch, {"push_personalization_rollout": bad})
        assert cfg.rollout == "off", bad


async def test_config_db_error_is_off(monkeypatch):
    async def _boom(session, keys):
        raise RuntimeError("db down")

    monkeypatch.setattr(pp, "get_config_values", _boom)
    cfg = await pp.effective_push_config(None)
    assert cfg.rollout == "off"  # 절대 raise 금지 — 발송 경로 앞단


async def test_config_allowlist_and_sources_parsing(monkeypatch):
    good = str(UID)
    cfg = await _parse(
        monkeypatch,
        {
            "push_personalization_rollout": "allowlist",
            "push_personalization_allowlist": [good, "not-a-uuid", 3],
            "push_personalization_sources": ["diary", "transcript", "sms", 7],
        },
    )
    assert cfg.rollout == "allowlist"
    assert cfg.allowlist == frozenset({UID})  # 무효 항목은 무시(전체 무효화 아님)
    assert cfg.sources == frozenset({"diary", "transcript"})


async def test_config_sources_invalid_falls_back_to_diary(monkeypatch):
    cfg = await _parse(monkeypatch, {"push_personalization_sources": "transcript"})
    assert cfg.sources == frozenset({"diary"})  # 비리스트 = 보수 기본값(A경로만)


def test_user_allowed_three_states():
    assert not pp.user_allowed(UID, _cfg(rollout="off"))
    assert pp.user_allowed(UID, _cfg(rollout="all"))
    assert not pp.user_allowed(UID, _cfg(rollout="allowlist"))
    assert pp.user_allowed(UID, _cfg(rollout="allowlist", allowlist=(UID,)))


# ── row_valid: D+1~D+3·언어·소스 ───────────────────────────────────────────

def test_row_valid_reuse_window_off_by_one():
    now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)  # KST 19시
    ad = activity_date_for(now, "Asia/Seoul")
    p, cfg = _profile(), _cfg()
    assert pp.row_valid(_row(anchor_date=ad - timedelta(days=1)), p, now, cfg)  # D+1
    assert pp.row_valid(_row(anchor_date=ad - timedelta(days=3)), p, now, cfg)  # D+3(마지막)
    assert not pp.row_valid(_row(anchor_date=ad - timedelta(days=4)), p, now, cfg)  # D+4 만료
    assert not pp.row_valid(_row(anchor_date=ad), p, now, cfg)  # 당일(생성일) 발송 없음
    assert not pp.row_valid(None, p, now, cfg)


def test_row_valid_language_and_source_and_rollout():
    now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    p = _profile()
    # 픽스처에도 같은 now를 준다 — 안 그러면 실제 날짜가 지나가며 조용히 깨진다.
    assert not pp.row_valid(_row(now=now, language="en"), p, now, _cfg())  # 언어 변경 → 무효
    assert not pp.row_valid(_row(now=now, source_kind="transcript"), p, now, _cfg(sources=("diary",)))
    assert not pp.row_valid(_row(now=now), p, now, _cfg(rollout="off"))
    assert not pp.row_valid(_row(now=now), p, now, _cfg(rollout="allowlist"))  # 명단 밖
    # ko-KR 같은 BCP47 태그도 resolve를 거쳐 일치 판정
    assert pp.row_valid(_row(now=now), _profile(language="ko-KR"), now, _cfg())


# ── 결정적 필터 ────────────────────────────────────────────────────────────

def test_filter_banned_and_time_words():
    ok = pp.passes_deterministic_filter
    assert ok("요즘 그 프로젝트는 잘 되고 있어? 나랑 얘기하자.", "ko")
    for bad in (
        "어제 하던 얘기 계속하자",          # 시간표현
        "죽고 싶다더니 괜찮아?",            # 민감(자해 연관)
        "병원 다녀온 건 어땠어?",           # 의료
        "빚 문제는 해결됐어?",              # 재정
        "두 줄\n문구",                      # 한 줄 위반
        "",                                 # 빈 문구
        None,
    ):
        assert not ok(bad, "ko"), bad
    assert not ok("昨日の話、続きしよう", "ja")
    assert not ok("How was your day yesterday?", "en")
    assert not ok("Are you still feeling depressed?", "en")


def test_filter_latin_word_boundary_no_false_positive():
    # skill⊂kill·studied⊂die 같은 substring 과탐이 없어야 정상 문구가 살아남는다.
    assert pp.passes_deterministic_filter("That new skill you studied sounds fun.", "en")
    assert not pp.passes_deterministic_filter("Do you want to kill time with me?", "en")


def test_filter_en_proper_noun_heuristic():
    assert not pp.passes_deterministic_filter("Did you meet Sarah again?", "en")
    assert pp.passes_deterministic_filter("I was thinking about you. Come hang out.", "en")
    assert pp.passes_deterministic_filter("Come tell me about it. I missed you.", "en")


def test_filter_length_caps():
    # 2026-08-05 사용자 피드백으로 축소(ko/ja 60·en 110) — 잠금화면 1~2줄 상한
    assert not pp.passes_deterministic_filter("가" * (pp._MAX_CHARS["ko"] + 1), "ko")
    assert pp.passes_deterministic_filter("가" * pp._MAX_CHARS["ko"], "ko")
    assert not pp.passes_deterministic_filter("a" * (pp._MAX_CHARS["en"] + 1), "en")


def test_filter_unicode_bypass_blocked():
    """LLM 출력은 입력 살균을 안 거친다 — 필터가 살균본(probe)을 검사해야 우회가 막힌다."""
    assert not pp.passes_deterministic_filter("자​해 얘기 계속하자 나랑", "ko")  # zero-width
    assert not pp.passes_deterministic_filter("Do you want to ｄｉｅ with me?", "en")  # 전각
    assert not pp.passes_deterministic_filter("How was ｙｅｓｔｅｒｄａｙ for you?", "en")
    assert not pp.passes_deterministic_filter("Did you meet Ѕarah again?", "en")  # 키릴 confusable
    # 통상 타이포그래피(스마트쿼트)는 en에서 허용 — 과탐으로 정상 문구를 죽이지 않는다.
    assert pp.passes_deterministic_filter("How’s that project going? Come talk.", "en")


def test_person_reference_heuristic_ko_ja():
    has = pp.has_person_reference
    assert has("민수씨 요즘 어때? 나랑 얘기하자.", "ko", "승민")   # X씨 = 강한 인명 시그널
    assert has("민수씨는 요즘 어때?", "ko", "승민")                # 씨/님+조사(가장 흔한 형태)
    assert has("민수님이 도와줬다며?", "ko", "승민")
    assert has("지현아 잘 지냈어?", "ko", "승민")                  # 문두 호격
    assert not has("선생님 얘기 잘 됐어?", "ko", "승민")           # 호칭 allowlist
    assert not has("선생님이 칭찬했다며? 잘했어.", "ko", "승민")   # allowlist는 조사 붙어도 유지
    assert not has("승민아 잘 지냈어?", "ko", "승민")              # 본인 이름은 허용(마스킹 대상)
    assert has("田中さんとはどう?", "ja", None)                    # ja 인명+경칭
    assert not has("皆さん元気? 話そう。", "ja", None)             # 경칭 allowlist
    # 문서화된 잔여 갭: 맨이름+조사(민수랑)·기관 축약(서울대)은 결정적으로 못 잡는다 —
    # 프롬프트+검수 LLM+카나리 DB 전수 열람이 담당(계획 §9). assert로 '의도된 수용'을 고정.
    assert not has("민수랑은 요즘 어때?", "ko", "승민")


def test_person_reference_relation_words_not_rejected():
    """프롬프트가 지시한 관계 표현('친구'·직함)은 과탐하지 않는다 — 프롬프트와 필터가
    반대 방향이면 리젝률만 오르고 카나리 지표가 오염된다(리뷰 M3)."""
    has = pp.has_person_reference
    for ok_body in (
        "친구야 잘 지냈어? 얘기하자",
        "기사님 친절했어?",
        "교수님 얘기는 어떻게 됐어?",
        "팀장님과의 면담은 잘 끝났어?",
    ):
        assert not has(ok_body, "ko", "승민"), ok_body
    assert not has("おばさんと話した？", "ja", None)
    assert not has("お疲れさんでした、話そう", "ja", None)


def test_person_reference_org_suffix_ko():
    has = pp.has_person_reference
    assert has("서울대학교 발표 준비는 잘 돼가?", "ko", "승민")   # 기관명(계획 §2-5 고유명사)
    assert has("한영고등학교 얘기 어떻게 됐어?", "ko", "승민")
    assert not has("학교 다녀온 얘기 해줘", "ko", "승민")          # 일반명사는 허용


async def test_verify_body_exact_ok_only(monkeypatch):
    """검수 판정은 'OK' 정확 일치만 — 'OK, but ...' 유보 응답은 리젝(fail-closed)."""
    answers = {}

    async def _gen(system, convo, **kw):
        return SimpleNamespace(text=answers["t"])

    monkeypatch.setattr(pp.llm, "generate", _gen)
    for text_, expected in (
        ("OK", True), ("ok", True), ("OK.", True),
        ("OKAY", False), ("OK, but 시간표현 있음", False), ("NO", False), ("", False),
    ):
        answers["t"] = text_
        assert await pp._verify_body("문구", "ko") is expected, text_


async def test_llm_token_budget_covers_reasoning_overhead(monkeypatch):
    """utility(luna)는 답변 전 reasoning으로 ~130+ 토큰을 소모한다(dev 리허설 실측:
    max_completion_tokens=120 → finish=length·본문 0자·HTTP 200 → 전량 filter(len=0) 리젝).
    두 콜사이트 모두 그 오버헤드를 덮는 예산을 넘겨야 한다 — 다시 낮추면 무음 전량 리젝."""
    seen = {}

    async def _gen(system, convo, **kw):
        seen[len(seen)] = kw.get("max_tokens")
        return SimpleNamespace(text="OK")

    monkeypatch.setattr(pp.llm, "generate", _gen)
    await pp._generate_body("소재", "ko")
    await pp._verify_body("문구", "ko")
    assert seen[0] == pp.GEN_MAX_TOKENS and seen[0] >= 256
    assert seen[1] == pp.VERIFY_MAX_TOKENS and seen[1] >= 256


async def test_rejected_body_content_never_logged(monkeypatch, caplog):
    """리젝 로그는 사유·길이만 — 문구 내용은 journald에 남으면 삭제 계약이 닿지 않는다."""
    import logging

    bad_body = "어제 병원 다녀온 얘기 하자"

    async def _diary(session, uid, target):
        return SimpleNamespace(content="소재")

    async def _gen(source_text, language, hint=None):
        return bad_body

    async def _delete(session, uid):
        pass

    monkeypatch.setattr(pp, "_personal_diary", _diary)
    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_delete_row", _delete)
    with caplog.at_level(logging.INFO, logger="moly-worker"):
        status = await pp._generate_inner(
            _GenSession(results=[], scalars=[]), _profile(), date(2026, 8, 3),
            [_msg()], [_msg()], _cfg(), {},
        )
    assert status == "rejected"
    assert bad_body not in caplog.text and "병원" not in caplog.text
    assert "reason=filter" in caplog.text


# ── 생성 플로우(fail-closed·row 불변식) ────────────────────────────────────

class _Res:
    def __init__(self, items=()):
        self._items = list(items)

    def scalars(self):
        return self

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return list(self._items)

    def scalar(self):
        return self._items[0] if self._items else None


class _GenSession:
    """generate_for_user 최소 세션 — execute 응답을 순서대로 스크립트."""

    def __init__(self, results, scalars):
        self.results = list(results)
        self.scalars_q = list(scalars)
        self.sql = []

    async def execute(self, stmt, params=None):
        self.sql.append((str(stmt), params))
        return _Res(self.results.pop(0) if self.results else [])

    async def scalar(self, stmt, params=None):
        self.sql.append((str(stmt), params))
        return self.scalars_q.pop(0) if self.scalars_q else None

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _msg(content="안녕 오늘 힘들었어", sender="user", at=None):
    return SimpleNamespace(
        content=content, sender=sender,
        created_at=at or datetime(2026, 8, 4, 5, 30, tzinfo=timezone.utc),  # KST 14:30
    )


async def test_generate_failed_deletes_row_and_releases_claim(monkeypatch):
    """생성 내부 예외 = failed + 기존 row DELETE(옛 문구 재사용 금지) + 클레임 해제."""
    deleted, released = [], []

    async def _claim(session, uid, target):
        return True

    async def _release(session, uid, target):
        released.append(uid)

    async def _boom(*a, **k):
        raise TimeoutError("llm timeout")

    async def _delete(session, uid):
        deleted.append(uid)

    monkeypatch.setattr(pp, "_claim", _claim)
    monkeypatch.setattr(pp, "_release_claim", _release)
    monkeypatch.setattr(pp, "_generate_inner", _boom)
    monkeypatch.setattr(pp, "_delete_row", _delete)
    session = _GenSession(
        results=[[], [_msg()]],  # 기존 anchor 없음 → 어제 메시지 1건
        scalars=[False],         # barriers 없음
    )
    now = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)  # KST 05:00
    status = await pp.generate_for_user(session, _profile(), now, _cfg(), {})
    assert status == "failed" and deleted == [UID] and released == [UID]


async def test_generate_idempotent_when_anchor_matches(monkeypatch):
    now = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)
    target = activity_date_for(now, "Asia/Seoul") - timedelta(days=1)
    called = []

    async def _claim(session, uid, t):
        called.append("claim")
        return True

    monkeypatch.setattr(pp, "_claim", _claim)
    session = _GenSession(results=[[target]], scalars=[])
    status = await pp.generate_for_user(session, _profile(), now, _cfg(), {})
    assert status == "already" and called == []  # 클레임·LLM 접근 없이 스킵


async def test_generate_no_yesterday_chat_keeps_existing_row(monkeypatch):
    """어제 대화 없음 = no_target — 이전 사이클 row는 D+3 재사용을 위해 무접촉."""
    deleted = []

    async def _delete(session, uid):
        deleted.append(uid)

    monkeypatch.setattr(pp, "_delete_row", _delete)
    session = _GenSession(results=[[], []], scalars=[False])  # anchor 없음, 메시지 0건
    now = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)
    status = await pp.generate_for_user(session, _profile(), now, _cfg(), {})
    assert status == "no_target" and deleted == []


async def test_generate_barrier_user_excluded(monkeypatch):
    session = _GenSession(results=[[]], scalars=[True])  # 장벽(deleting/deleted) 존재
    now = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)
    status = await pp.generate_for_user(session, _profile(), now, _cfg(), {})
    assert status == "no_target"
    # 계약 고정: 판정은 행 존재가 아니라 state — backfill로 전 유저에게 'active' 행이
    # 깔리는 새 장벽 계약(#117)에서 존재 기반이면 전원이 조용히 배제된다.
    barrier_sql = next(s for s, _ in session.sql if "privacy_subject_barriers" in s)
    assert "state <> 'active'" in barrier_sql


def test_prefetch_barrier_filter_is_state_based():
    """프리페치 NOT EXISTS도 state 기반이어야 한다(#117 계약) — SQL 컴파일로 고정."""
    from sqlalchemy import select as sa_select

    from app.models.conversational_recall import PrivacySubjectBarrier
    from app.models.push_personalization import PushPersonalization

    stmt = sa_select(PushPersonalization).where(
        ~sa_select(PrivacySubjectBarrier.user_id)
        .where(
            PrivacySubjectBarrier.user_id == PushPersonalization.user_id,
            PrivacySubjectBarrier.state != "active",
        )
        .exists()
    )
    # 실제 prefetch_rows가 쓰는 조건과 동일한 형태가 컴파일되는지만 확인(스모크) —
    # 정확한 실행 검증은 dev 리허설이 담당.
    assert "state !=" in str(stmt) or "state != " in str(stmt)
    import inspect

    src = inspect.getsource(pp.prefetch_rows)
    assert 'PrivacySubjectBarrier.state != "active"' in src


async def test_inner_rejected_when_verifier_ok_but_filter_fails(monkeypatch):
    """검수 LLM이 OK라도 결정적 필터 위반이면 reject — AND가 OR로 퇴화하지 않는다."""
    deleted = []

    async def _diary(session, uid, target):
        return SimpleNamespace(content="오늘 회사에서 힘든 일이 있었다")

    async def _gen(source_text, language, hint=None):
        return "어제 힘들었지? 얘기하자."  # 시간표현 위반

    async def _verify(body, language):
        return True  # 검수 LLM 조작 가정(인젝션) — 그래도 필터가 막아야 함

    async def _delete(session, uid):
        deleted.append(uid)

    monkeypatch.setattr(pp, "_personal_diary", _diary)
    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_verify_body", _verify)
    monkeypatch.setattr(pp, "_delete_row", _delete)
    session = _GenSession(results=[], scalars=[])
    status = await pp._generate_inner(
        session, _profile(), date(2026, 8, 3), [_msg()], [_msg()], _cfg(), {}
    )
    assert status == "rejected" and deleted == [UID]


async def test_inner_gate_passed_without_diary_never_degrades_to_transcript(monkeypatch):
    """게이트(60자) 통과인데 일기 없음 = 일기 생성 실패 케이스 — B 강등 금지, skip+DELETE."""
    deleted, gen_called = [], []

    async def _diary(session, uid, target):
        return None

    async def _gen(*a, **k):
        gen_called.append(1)
        return "문구"

    async def _delete(session, uid):
        deleted.append(uid)

    monkeypatch.setattr(pp, "_personal_diary", _diary)
    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_delete_row", _delete)
    long_msg = _msg(content="가" * 100)  # 게이트(60자) 통과
    session = _GenSession(results=[], scalars=[])
    status = await pp._generate_inner(
        session, _profile(), date(2026, 8, 3), [long_msg], [long_msg],
        _cfg(sources=("diary", "transcript")), {"diary_min_user_chars": 60},
    )
    assert status == "skipped" and deleted == [UID] and gen_called == []


async def test_inner_transcript_closed_skips_short_chatters(monkeypatch):
    async def _diary(session, uid, target):
        return None

    deleted = []

    async def _delete(session, uid):
        deleted.append(uid)

    monkeypatch.setattr(pp, "_personal_diary", _diary)
    monkeypatch.setattr(pp, "_delete_row", _delete)
    short = _msg(content="짧음")
    session = _GenSession(results=[], scalars=[])
    status = await pp._generate_inner(
        session, _profile(), date(2026, 8, 3), [short], [short],
        _cfg(sources=("diary",)), {"diary_min_user_chars": 60},
    )
    assert status == "skipped" and deleted == [UID]


async def test_inner_ok_upsert_resets_cycle_and_stores_placeholder(monkeypatch):
    """성공 upsert: SET에 sent_count=0/last_sent_on=NULL 리셋 포함 + body는 placeholder 저장."""

    async def _diary(session, uid, target):
        return SimpleNamespace(content="{유저이름}이랑 프로젝트 얘기를 했다")

    async def _gen(source_text, language, hint=None):
        assert "승민" in source_text  # LLM 입력은 render된 현재 이름(유창성)
        return "승민아 그 프로젝트 잘 되고 있어? 얘기하러 와."

    async def _verify(body, language):
        return True

    monkeypatch.setattr(pp, "_personal_diary", _diary)
    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_verify_body", _verify)
    session = _GenSession(results=[[]], scalars=[])
    status = await pp._generate_inner(
        session, _profile(), date(2026, 8, 3), [_msg()], [_msg()], _cfg(), {}
    )
    assert status == "ok"
    sql, params = session.sql[-1]
    assert "sent_count = 0" in sql and "last_sent_on = NULL" in sql
    assert params["b"].startswith("{유저이름}")  # 실명 저장 금지(placeholder 불변식)
    assert "승민" not in params["b"]
    assert params["s"] == time(14, 30) and params["k"] == "diary"


# ── 발송 게이트·클레임 순서(notify_evening_personalized) ───────────────────

def _notify_patch(monkeypatch, *, enabled=True, remaining=5000, claim_ok=True):
    calls = {"claim": [], "sent": [], "marked": []}

    async def _enabled(session, uid, t):
        return enabled

    async def _resolve(session, uid):
        return SimpleNamespace(entitlement={"tokens_remaining": remaining})

    async def _claim(session, profile, col, now=None):
        calls["claim"].append(col)
        return claim_ok

    async def _tokens(session, uid):
        return ["tok"]

    async def _send(tokens, title, body):
        calls["sent"].append((title, body))
        return 1

    async def _mark(session, uid, now, tz):
        calls["marked"].append(uid)

    from app.services import gating, push

    monkeypatch.setattr(notify, "_enabled", _enabled)
    monkeypatch.setattr(notify, "_claim_send_slot", _claim)
    monkeypatch.setattr(notify, "_tokens", _tokens)
    monkeypatch.setattr(gating, "resolve", _resolve)
    monkeypatch.setattr(push, "send", _send)
    monkeypatch.setattr(pp, "mark_sent", _mark)
    return calls


NOW = datetime(2026, 8, 5, 5, 30, tzinfo=timezone.utc)  # KST 14:30


async def test_personalized_sends_rendered_body_and_marks(monkeypatch):
    calls = _notify_patch(monkeypatch)
    row = _row(body="{유저이름}아 그 프로젝트 잘 돼가? 얘기하자.")
    n = await notify.notify_evening_personalized(None, _profile(), row, NOW)
    assert n == 1 and calls["claim"] == ["evening_notified_at"]
    title, body = calls["sent"][0]
    assert title == "캐피" and "승민아" in body and "{유저이름}" not in body
    assert calls["marked"] == [UID]


async def test_personalized_disabled_or_exhausted_skips_before_claim(monkeypatch):
    calls = _notify_patch(monkeypatch, enabled=False)
    assert await notify.notify_evening_personalized(None, _profile(), _row(), NOW) == 0
    assert calls["claim"] == [] and calls["sent"] == []
    calls = _notify_patch(monkeypatch, remaining=0)  # SOMA-291 게이트 공유
    assert await notify.notify_evening_personalized(None, _profile(), _row(), NOW) == 0
    assert calls["claim"] == []


async def test_personalized_render_filter_fails_before_claim(monkeypatch):
    """닉네임으로 유입된 금칙어도 render 후 재검사에 걸린다 — claim 미소모(디폴트 폴백 가능)."""
    calls = _notify_patch(monkeypatch)
    row = _row(body="{유저이름}아 안녕! 나랑 얘기하자.")
    bad_nick = _profile(nickname="죽돌이")  # 닉네임은 내용 검증이 없는 자유 문자열
    assert await notify.notify_evening_personalized(None, bad_nick, row, NOW) == 0
    assert calls["claim"] == [] and calls["sent"] == []


async def test_personalized_claim_lost_means_no_send(monkeypatch):
    calls = _notify_patch(monkeypatch, claim_ok=False)
    assert await notify.notify_evening_personalized(None, _profile(), _row(), NOW) == 0
    assert calls["sent"] == []  # 오늘 이미 발송(디폴트 포함) — 이중발송 불가


# ── 프리페치 가드 ──────────────────────────────────────────────────────────

class _NoQuerySession:
    async def execute(self, *a, **k):
        raise AssertionError("rollout off이면 쿼리가 나가면 안 된다")

    async def scalar(self, *a, **k):
        raise AssertionError("rollout off이면 쿼리가 나가면 안 된다")


async def test_prefetch_off_makes_zero_queries():
    assert await pp.prefetch_rows(_NoQuerySession(), NOW, _cfg(rollout="off")) == {}


async def test_prefetch_missing_table_returns_empty():
    class _S(_NoQuerySession):
        async def scalar(self, *a, **k):
            return False  # to_regclass NULL — 마이그레이션 전 배포

    assert await pp.prefetch_rows(_S(), NOW, _cfg()) == {}
