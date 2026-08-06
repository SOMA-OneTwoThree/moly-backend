"""저녁 푸시 개인화 — 슬롯·유효성·fail-closed 검수·클레임 순서·폴백(계획 v4 §8).

v2(2026-08-06): 소스=대화 원문 단일·매일 재생성(시점어 해금)·가드레일 축소(민감어+인명+
3인칭 자기 지칭만)·생성 모델 terra. 해제된 검사(시간표현·en 대문자·기관명)는 "이제 통과한다"를
테스트로 고정한다 — 실수로 재도입되면 개인화 품질 회귀다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone
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
        "source_kind": "transcript",
        "sent_count": 0,
        "generated_at": now,  # v2 신선도: 오늘 생성분(디폴트 신선)
    }
    r.update(over)
    return pp.PushRow(**r)


def _cfg(rollout="all", allowlist=()):
    return pp.PushConfig(rollout=rollout, allowlist=frozenset(allowlist))


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
    assert cfg.rollout == "off"


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


async def test_config_allowlist_parsing_and_legacy_sources_ignored(monkeypatch):
    good = str(UID)
    cfg = await _parse(
        monkeypatch,
        {
            "push_personalization_rollout": "allowlist",
            "push_personalization_allowlist": [good, "not-a-uuid", 3],
            # v1의 sources 키가 DB에 남아 있어도 파싱 오류 없이 무시돼야 한다(v2 폐기).
            "push_personalization_sources": ["diary", "transcript"],
        },
    )
    assert cfg.rollout == "allowlist"
    assert cfg.allowlist == frozenset({UID})  # 무효 항목은 무시(전체 무효화 아님)


def test_user_allowed_three_states():
    assert not pp.user_allowed(UID, _cfg(rollout="off"))
    assert pp.user_allowed(UID, _cfg(rollout="all"))
    assert not pp.user_allowed(UID, _cfg(rollout="allowlist"))
    assert pp.user_allowed(UID, _cfg(rollout="allowlist", allowlist=(UID,)))


# ── row_valid: D+1~D+3·언어 ────────────────────────────────────────────────

def test_row_valid_reuse_window_off_by_one():
    now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)  # KST 19시
    ad = activity_date_for(now, "Asia/Seoul")
    p, cfg = _profile(), _cfg()
    assert pp.row_valid(_row(now=now, anchor_date=ad - timedelta(days=1)), p, now, cfg)  # D+1
    assert pp.row_valid(_row(now=now, anchor_date=ad - timedelta(days=3)), p, now, cfg)  # D+3(마지막)
    assert not pp.row_valid(_row(now=now, anchor_date=ad - timedelta(days=4)), p, now, cfg)  # D+4 만료
    assert not pp.row_valid(_row(now=now, anchor_date=ad), p, now, cfg)  # 당일(생성일) 발송 없음
    assert not pp.row_valid(None, p, now, cfg)


def test_row_valid_requires_today_generation():
    """v2 신선도: 오늘 재생성분만 발송. 05시 생성이 빠진 날(워커 다운) 어제 몸체의
    '어제'는 거짓이 된다 — 그날은 디폴트 폴백. generated_at 부재(이상 데이터)도 무효."""
    now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)  # KST 19시, 활동일 8/5
    p, cfg = _profile(), _cfg()
    assert pp.row_valid(_row(now=now), p, now, cfg)  # 오늘 05시 생성 → 발송
    stale = _row(now=now, generated_at=now - timedelta(days=1))  # 어제 생성분
    assert not pp.row_valid(stale, p, now, cfg)
    assert not pp.row_valid(_row(now=now, generated_at=None), p, now, cfg)
    # naive datetime(테스트·이상 경로)은 UTC로 간주해 판정한다
    naive_today = _row(now=now, generated_at=now.replace(tzinfo=None))
    assert pp.row_valid(naive_today, p, now, cfg)


def test_row_valid_language_and_rollout():
    now = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    p = _profile()
    # 픽스처에도 같은 now를 준다 — 안 그러면 실제 날짜가 지나가며 조용히 깨진다.
    assert not pp.row_valid(_row(now=now, language="en"), p, now, _cfg())  # 언어 변경 → 무효
    assert not pp.row_valid(_row(now=now), p, now, _cfg(rollout="off"))
    assert not pp.row_valid(_row(now=now), p, now, _cfg(rollout="allowlist"))  # 명단 밖
    # v1의 diary 소스 행이 남아 있어도 창 내면 유효 — 다음 05시 재생성이 transcript로 교체
    assert pp.row_valid(_row(now=now, source_kind="diary"), p, now, _cfg())
    # ko-KR 같은 BCP47 태그도 resolve를 거쳐 일치 판정
    assert pp.row_valid(_row(now=now), _profile(language="ko-KR"), now, _cfg())


# ── 결정적 필터(v2: 민감어+3인칭+형식만) ───────────────────────────────────

def test_filter_banned_words_and_format():
    ok = pp.passes_deterministic_filter
    assert ok("요즘 그 프로젝트는 잘 되고 있어? 나랑 얘기하자.", "ko")
    for bad in (
        "죽고 싶다더니 괜찮아?",            # 민감(자해 연관)
        "병원 다녀온 건 어땠어?",           # 의료
        "빚 문제는 해결됐어?",              # 재정
        "두 줄\n문구",                      # 한 줄 위반
        "",                                 # 빈 문구
        None,
    ):
        assert not ok(bad, "ko"), bad
    assert not ok("Are you still feeling depressed?", "en")


def test_filter_time_words_now_allowed():
    """v2: 매일 재생성으로 시점이 항상 사실 — 시간표현 해금이 개인화의 핵심 레버다.
    이 검사들이 다시 리젝되면 when 라벨('어제 말한 면접 어떻게 됐어?')이 통째로 죽는다."""
    ok = pp.passes_deterministic_filter
    assert ok("어제 말한 면접 어떻게 됐어?", "ko")
    assert ok("며칠 전 빙수 얘기하다 웃었던 거 생각났어.", "ko")
    assert ok("昨日の話の続き気になってた。", "ja")
    assert ok("How did that interview from yesterday go?", "en")


def test_filter_proper_nouns_now_allowed_en():
    """v2: en 대문자=고유명사 휴리스틱 제거 — 작품·지명 인용 허용. en 인명은 이름 사전
    (has_person_reference) + 검수 LLM '애매하면 NO' 극성이 담당."""
    assert pp.passes_deterministic_filter("Did you finish that Zelda quest?", "en")
    assert pp.passes_deterministic_filter("I was thinking about you. Come hang out.", "en")


def test_filter_mixed_script_confusable_blocked():
    """보안 리뷰(2026-08-06) 실증 대응: 키릴 Ѕ(U+0405)는 NFKC로도 라틴 S로 안 접혀
    'Ѕuicide'가 라틴 금칙어 정규식을 우회한다 — 한 단어 내 라틴+confusable 혼합은 언어
    무관 리젝. 전면 비ASCII 금지가 아니라 고유명사·타이포그래피 허용과 양립한다."""
    ok = pp.passes_deterministic_filter
    assert not ok("Do you want to Ѕuicide?", "en")       # 키릴 Ѕ
    assert not ok("Was the hоspital visit okay?", "en")  # 키릴 о
    assert not ok("그때 그 hоspital 얘기 어떻게 됐어?", "ko")  # ko 문구 속 혼합 단어도 차단
    assert ok("Did you finish that Zelda quest?", "en")
    assert ok("어제 말한 면접 어떻게 됐어?", "ko")
    assert ok("昨日の話の続き気になってた。", "ja")


def test_person_reference_en_gazetteer():
    """en 인명 결정적 게이트(보안 리뷰 MEDIUM 대응) — 흔한 이름 사전, 대문자 토큰만.
    작품·지명·브랜드는 통과(사전에서 의도적 제외), 본인 닉네임은 허용."""
    has = pp.has_person_reference
    assert has("Did you meet Sarah again?", "en", None)
    assert has("How did it go with Jamie?", "en", None)
    assert not has("Did you finish that Zelda quest?", "en", None)
    assert not has("How was the Netflix show?", "en", None)
    assert not has("Sarah, how was that ramen place?", "en", "Sarah")  # 본인 이름
    assert not has("I saw a sarah crossing sign.", "en", None)  # 소문자 일반 토큰은 무시


def test_filter_self_third_person_blocked():
    """run100 실측: 프롬프트 금지만으로 '캐피는~' 3인칭이 3건 샜다 — 결정적으로 막는다.
    ja는 장음 없는 'キャピ' 실측 표기까지, en은 'Cappy' 단어 자체를 막는다(1인칭 페르소나가
    본문에서 자기 이름을 부를 정상 경로 없음 + 검수가 Cappy를 허용해 못 잡음 — 리뷰 Major)."""
    ok = pp.passes_deterministic_filter
    assert not ok("화해했다는 소식 캐피는 오래 기억할게", "ko")
    assert not ok("캐피가 문득 궁금해졌어", "ko")
    assert not ok("キャピーはずっと応援してるよ", "ja")
    assert not ok("キャピはちゃんと覚えてるよ", "ja")  # 장음 없는 실측 표기
    assert not ok("Cappy misses you already.", "en")
    assert ok("나도 결말이 궁금해졌어.", "ko")  # 1인칭은 정상
    assert ok("A capybara nap sounds nice.", "en")  # 단어 경계 — capybara는 무관


def test_filter_length_caps():
    # 2026-08-05 사용자 피드백으로 축소(ko/ja 60·en 110) — 잠금화면 1~2줄 상한
    assert not pp.passes_deterministic_filter("가" * (pp._MAX_CHARS["ko"] + 1), "ko")
    assert pp.passes_deterministic_filter("가" * pp._MAX_CHARS["ko"], "ko")
    assert not pp.passes_deterministic_filter("a" * (pp._MAX_CHARS["en"] + 1), "en")


def test_filter_latin_word_boundary_no_false_positive():
    # skill⊂kill·studied⊂die 같은 substring 과탐이 없어야 정상 문구가 살아남는다.
    assert pp.passes_deterministic_filter("That new skill you studied sounds fun.", "en")
    assert not pp.passes_deterministic_filter("Do you want to kill time with me?", "en")


def test_filter_unicode_bypass_blocked():
    """LLM 출력은 입력 살균을 안 거친다 — 필터가 살균본(probe)을 검사해야 우회가 막힌다.
    (금칙어 우회 차단은 v2에서도 유지 — 해제된 건 시간표현·고유명사뿐.)"""
    assert not pp.passes_deterministic_filter("자​해 얘기 계속하자 나랑", "ko")  # zero-width
    assert not pp.passes_deterministic_filter("Do you want to ｄｉｅ with me?", "en")  # 전각
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
    # 문서화된 잔여 갭: 맨이름+조사(민수랑)는 결정적으로 못 잡는다 — 프롬프트+검수 LLM+
    # 카나리 DB 전수 열람이 담당(계획 §9). assert로 '의도된 수용'을 고정.
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
    # 부사 'ちゃんと(제대로)'는 경칭 아님 — 캐릭터명 문구 오탐 방지(2026-08-05 실측 고정)
    assert not has("キャピはちゃんと覚えてるよ", "ja", None)
    assert has("ミサキちゃんに会えた？", "ja", None)  # 진짜 인명+ちゃん은 여전히 차단


def test_person_reference_org_names_now_allowed():
    """v2: 기관·학교명 등 비인명 고유명사 허용(사용자 결정 2026-08-06) —
    '가드레일은 사람 이름·민감어만'. 재도입되면 개인화 품질 회귀."""
    has = pp.has_person_reference
    assert not has("서울대학교 발표 준비는 잘 돼가?", "ko", "승민")
    assert not has("한영고등학교 얘기 어떻게 됐어?", "ko", "승민")
    assert not has("학교 다녀온 얘기 해줘", "ko", "승민")


# ── when 라벨·생성 모델 ────────────────────────────────────────────────────

def test_when_label_two_stage():
    """D+1='어제', D+2 이후='며칠 전' 2단계(사용자 결정: '이틀 전/사흘 전'은 알림 어휘로
    부자연). 라벨이 사실과 어긋나면 안 되므로 경계(1→2)를 고정한다."""
    assert pp.when_label(1, "ko") == "어제"
    assert pp.when_label(2, "ko") == "며칠 전"
    assert pp.when_label(3, "ko") == "며칠 전"
    assert pp.when_label(1, "ja") == "昨日"
    assert pp.when_label(2, "ja") == "この前"
    assert pp.when_label(1, "en") == "yesterday"
    assert pp.when_label(3, "en") == "a few days ago"


async def test_generate_body_injects_when_and_uses_diary_model(monkeypatch):
    """v2: 생성 모델 = diary(terra) + 시스템 프롬프트에 when 라벨 주입 확인."""
    seen = {}

    async def _gen(system, convo, **kw):
        seen["system"] = system
        seen["model"] = kw.get("model")
        return SimpleNamespace(text="문구")

    monkeypatch.setattr(pp.llm, "generate", _gen)
    from app.config import settings

    await pp._generate_body("소재", "ko", 1)
    assert seen["model"] == settings.model_diary
    assert "어제 나눈 대화다" in seen["system"]
    await pp._generate_body("소재", "ko", 2)
    assert "며칠 전 나눈 대화다" in seen["system"]


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
    """reasoning 계열 모델은 답변 전 reasoning으로 ~130+ 토큰을 소모한다(dev 리허설 실측:
    max_completion_tokens=120 → finish=length·본문 0자·HTTP 200 → 전량 filter(len=0) 리젝).
    두 콜사이트 모두 그 오버헤드를 덮는 예산을 넘겨야 한다 — 다시 낮추면 무음 전량 리젝."""
    seen = {}

    async def _gen(system, convo, **kw):
        seen[len(seen)] = kw.get("max_tokens")
        return SimpleNamespace(text="OK")

    monkeypatch.setattr(pp.llm, "generate", _gen)
    await pp._generate_body("소재", "ko", 1)
    await pp._verify_body("문구", "ko")
    assert seen[0] == pp.GEN_MAX_TOKENS and seen[0] >= 256
    assert seen[1] == pp.VERIFY_MAX_TOKENS and seen[1] >= 256


# ── 생성 플로우(fail-closed·row 불변식·매일 재생성) ────────────────────────

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


NOW_GEN = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)  # KST 8/5 05:00, 활동일 8/5
AD = activity_date_for(NOW_GEN, "Asia/Seoul")  # 2026-08-05
TARGET = AD - timedelta(days=1)  # 2026-08-04


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
        results=[[], [_msg()]],  # 기존 row 없음 → 어제 메시지 1건
        scalars=[False],         # barriers 없음
    )
    status = await pp.generate_for_user(session, _profile(), NOW_GEN, _cfg(), {})
    assert status == "failed" and deleted == [UID] and released == [UID]


async def test_generate_idempotent_when_anchor_matches(monkeypatch):
    called = []

    async def _claim(session, uid, t):
        called.append("claim")
        return True

    monkeypatch.setattr(pp, "_claim", _claim)
    session = _GenSession(
        results=[[(TARGET, NOW_GEN)], [_msg()]],  # row(anchor=어제) + 어제 메시지 존재
        scalars=[False],
    )
    status = await pp.generate_for_user(session, _profile(), NOW_GEN, _cfg(), {})
    assert status == "already" and called == []  # 클레임·LLM 접근 없이 스킵


async def test_generate_no_chat_and_no_row_is_no_target(monkeypatch):
    deleted = []

    async def _delete(session, uid):
        deleted.append(uid)

    monkeypatch.setattr(pp, "_delete_row", _delete)
    session = _GenSession(results=[[], []], scalars=[False])  # row 없음, 어제 메시지 0건
    status = await pp.generate_for_user(session, _profile(), NOW_GEN, _cfg(), {})
    assert status == "no_target" and deleted == []


async def test_generate_regen_within_window_preserving_cycle(monkeypatch):
    """어제 대화 없음 + D+2 유효 row = anchor 대화로 오늘 몫 재생성(reset=False).
    시점어 해금의 대가 — 어제 만든 몸체를 오늘 보내면 '어제'가 거짓이 된다."""
    captured = {}

    async def _claim(session, uid, t):
        captured["claim_date"] = t
        return True

    async def _release(session, uid, t):
        pass

    async def _inner(session, profile, anchor, ad, user_msgs, messages, reset):
        captured.update(anchor=anchor, ad=ad, reset=reset, n_msgs=len(messages))
        return "ok"

    monkeypatch.setattr(pp, "_claim", _claim)
    monkeypatch.setattr(pp, "_release_claim", _release)
    monkeypatch.setattr(pp, "_generate_inner", _inner)
    anchor = AD - timedelta(days=2)
    gen_yesterday = NOW_GEN - timedelta(days=1)  # 어제 몫 생성 기록 → 오늘 재생성 필요
    session = _GenSession(
        results=[[(anchor, gen_yesterday)], [], [_msg()]],  # row, 어제 msgs 0, anchor msgs 1
        scalars=[False],
    )
    status = await pp.generate_for_user(session, _profile(), NOW_GEN, _cfg(), {})
    assert status == "ok"
    assert captured["anchor"] == anchor and captured["ad"] == AD
    assert captured["reset"] is False and captured["claim_date"] == anchor


async def test_generate_regen_idempotent_same_day(monkeypatch):
    """오늘 몫 재생성 완료(generated_at 활동일 == 오늘) = already — 15분 케이던스 멱등.
    현실 시나리오 고정: 05:00 틱이 05:07에 생성 → 05:15 틱이 already로 스킵."""
    called = []

    async def _claim(session, uid, t):
        called.append(1)
        return True

    monkeypatch.setattr(pp, "_claim", _claim)
    anchor = AD - timedelta(days=2)
    gen_at_0507 = NOW_GEN + timedelta(minutes=7)
    tick_0515 = NOW_GEN + timedelta(minutes=15)
    session = _GenSession(results=[[(anchor, gen_at_0507)], []], scalars=[False])
    status = await pp.generate_for_user(session, _profile(), tick_0515, _cfg(), {})
    assert status == "already" and called == []


async def test_generate_regen_expired_window_is_no_target(monkeypatch):
    """D+4 row는 재생성하지 않는다(발송 창 밖) — row_valid 만료와 같은 산술."""
    session = _GenSession(
        results=[[(AD - timedelta(days=4), NOW_GEN - timedelta(days=1))], []],
        scalars=[False],
    )
    status = await pp.generate_for_user(session, _profile(), NOW_GEN, _cfg(), {})
    assert status == "no_target"


async def test_generate_regen_anchor_messages_gone_deletes_row(monkeypatch):
    """anchor 대화가 사라졌으면(삭제 계약 등) 소스가 없다 — row DELETE 후 no_target."""
    deleted = []

    async def _delete(session, uid):
        deleted.append(uid)

    monkeypatch.setattr(pp, "_delete_row", _delete)
    anchor = AD - timedelta(days=2)
    session = _GenSession(
        results=[[(anchor, NOW_GEN - timedelta(days=1))], [], []],  # anchor msgs도 0건
        scalars=[False],
    )
    status = await pp.generate_for_user(session, _profile(), NOW_GEN, _cfg(), {})
    assert status == "no_target" and deleted == [UID]


async def test_generate_barrier_user_excluded(monkeypatch):
    session = _GenSession(results=[[]], scalars=[True])  # 장벽(deleting/deleted) 존재
    status = await pp.generate_for_user(session, _profile(), NOW_GEN, _cfg(), {})
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


# ── _generate_inner(v2: 소스=원문·리셋/보존 분기) ──────────────────────────

async def test_inner_rejected_when_verifier_ok_but_filter_fails(monkeypatch):
    """검수 LLM이 OK라도 결정적 필터 위반이면 reject — AND가 OR로 퇴화하지 않는다."""
    deleted = []

    async def _gen(source_text, language, days_ago, hint=None, **kw):
        return "병원 다녀온 얘기 계속하자."  # 민감어(의료) 위반

    async def _verify(body, language, **kw):
        return True  # 검수 LLM 조작 가정(인젝션) — 그래도 필터가 막아야 함

    async def _delete(session, uid):
        deleted.append(uid)

    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_verify_body", _verify)
    monkeypatch.setattr(pp, "_delete_row", _delete)
    session = _GenSession(results=[], scalars=[])
    status = await pp._generate_inner(
        session, _profile(), TARGET, AD, [_msg()], [_msg()], True
    )
    assert status == "rejected_filter" and deleted == [UID]


async def test_inner_rejected_body_content_never_logged(monkeypatch, caplog):
    """리젝 로그는 사유·길이만 — 문구 내용은 journald에 남으면 삭제 계약이 닿지 않는다."""
    import logging

    bad_body = "병원 다녀온 얘기 하자"

    async def _gen(source_text, language, days_ago, hint=None, **kw):
        return bad_body

    async def _delete(session, uid):
        pass

    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_delete_row", _delete)
    with caplog.at_level(logging.INFO, logger="moly-worker"):
        status = await pp._generate_inner(
            _GenSession(results=[], scalars=[]), _profile(), TARGET, AD,
            [_msg()], [_msg()], True,
        )
    assert status == "rejected_filter"
    assert bad_body not in caplog.text and "병원" not in caplog.text
    assert "reason=filter" in caplog.text


async def test_inner_empty_source_skips_and_deletes(monkeypatch):
    """살균 후 소스가 공백이면 skip + row DELETE — LLM 호출 없이 fail-closed."""
    deleted, gen_called = [], []

    async def _gen(*a, **k):
        gen_called.append(1)
        return "문구"

    async def _delete(session, uid):
        deleted.append(uid)

    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_delete_row", _delete)
    empty = _msg(content="​​")  # 살균되면 공백
    session = _GenSession(results=[], scalars=[])
    status = await pp._generate_inner(
        session, _profile(), TARGET, AD, [empty], [empty], True
    )
    assert status == "skipped" and deleted == [UID] and gen_called == []


async def test_inner_source_is_transcript_with_speaker_labels(monkeypatch):
    """v2 소스 = 대화 원문(발화자 라벨 포함, render된 현재 이름). 일기 조회는 없어야 한다."""
    captured = {}

    async def _gen(source_text, language, days_ago, hint=None, **kw):
        captured["source"] = source_text
        captured["days_ago"] = days_ago
        return "빙수 얘기하다 웃었던 거 생각났어."

    async def _verify(body, language, **kw):
        return True

    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_verify_body", _verify)
    msgs = [
        _msg(content="{유저이름}, 오늘 빙수 먹었어", sender="user"),
        _msg(content="맛있었겠다!", sender="moly"),
    ]
    session = _GenSession(results=[[]], scalars=[])
    status = await pp._generate_inner(
        session, _profile(), TARGET, AD, [msgs[0]], msgs, True
    )
    assert status == "ok"
    assert "승민: " in captured["source"] and "캐피: " in captured["source"]
    assert "빙수" in captured["source"] and "{유저이름}" not in captured["source"]
    assert captured["days_ago"] == 1


async def test_inner_ok_upsert_resets_cycle_and_stores_placeholder(monkeypatch):
    """새 사이클(reset=True) upsert: sent_count=0/last_sent_on=NULL 리셋 + placeholder 저장."""

    async def _gen(source_text, language, days_ago, hint=None, **kw):
        assert "승민" in source_text  # LLM 입력은 render된 현재 이름(유창성)
        return "승민아 그 프로젝트 잘 되고 있어?"

    async def _verify(body, language, **kw):
        return True

    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_verify_body", _verify)
    session = _GenSession(results=[[]], scalars=[])
    status = await pp._generate_inner(
        session, _profile(), TARGET, AD, [_msg()], [_msg()], True
    )
    assert status == "ok"
    sql, params = session.sql[-1]
    assert "sent_count = 0" in sql and "last_sent_on = NULL" in sql
    assert "'transcript'" in sql  # v2 소스 단일
    assert params["b"].startswith("{유저이름}")  # 실명 저장 금지(placeholder 불변식)
    assert "승민" not in params["b"]
    assert params["s"] == time(14, 30) and params["a"] == TARGET


async def test_inner_barrier_set_during_generation_blocks_store(monkeypatch):
    """LLM 왕복(최대 ~90s) 중 삭제 장벽이 서면 저장하지 않는다 — privacy._REDACT가 지운
    row를 INSERT가 되살리면 삭제 계약 위반(리뷰 Major). 저장 직전 재확인으로 창을 닫는다."""
    deleted = []

    async def _gen(source_text, language, days_ago, hint=None, **kw):
        return "그 프로젝트 얘기 문득 생각났어."

    async def _verify(body, language, **kw):
        return True

    async def _delete(session, uid):
        deleted.append(uid)

    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_verify_body", _verify)
    monkeypatch.setattr(pp, "_delete_row", _delete)
    session = _GenSession(results=[], scalars=[True])  # 저장 직전 재확인에서 장벽 발견
    status = await pp._generate_inner(
        session, _profile(), TARGET, AD, [_msg()], [_msg()], True
    )
    assert status == "skipped" and deleted == [UID]
    assert not any(s.strip().startswith(("INSERT", "UPDATE")) for s, _ in session.sql)


async def test_inner_regen_update_zero_rows_is_failed(monkeypatch):
    """재생성 UPDATE가 0행(생성 중 row 삭제 경합)이면 ok로 계수하지 않는다 — row 부활
    금지 + push_gen_ok 정직성(리뷰 Minor)."""

    async def _gen(source_text, language, days_ago, hint=None, **kw):
        return "며칠 전 그 얘기 문득 생각났어."

    async def _verify(body, language, **kw):
        return True

    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_verify_body", _verify)

    class _ZeroRowSession(_GenSession):
        async def execute(self, stmt, params=None):
            res = await super().execute(stmt, params)
            res.rowcount = 0
            return res

    session = _ZeroRowSession(results=[[]], scalars=[])
    status = await pp._generate_inner(
        session, _profile(), AD - timedelta(days=2), AD, [_msg()], [_msg()], False
    )
    assert status == "failed"


async def test_inner_regen_update_preserves_stats(monkeypatch):
    """재생성(reset=False)은 UPDATE — anchor·sent_count·last_sent_on 무접촉(사이클 보존).
    리셋이 섞이면 §10 효과 측정이 오염되고 D+3 소진 판정이 어긋난다."""

    async def _gen(source_text, language, days_ago, hint=None, **kw):
        assert days_ago == 2  # D+2 재생성 → '며칠 전' 라벨 경로
        return "며칠 전 빙수 얘기하다 웃었던 거 생각났어."

    async def _verify(body, language, **kw):
        return True

    monkeypatch.setattr(pp, "_generate_body", _gen)
    monkeypatch.setattr(pp, "_verify_body", _verify)
    session = _GenSession(results=[[]], scalars=[])
    anchor = AD - timedelta(days=2)
    status = await pp._generate_inner(
        session, _profile(), anchor, AD, [_msg()], [_msg()], False
    )
    assert status == "ok"
    sql, params = session.sql[-1]
    assert sql.lstrip().startswith("UPDATE push_personalizations")
    assert "sent_count" not in sql and "last_sent_on" not in sql and "anchor_date" not in sql
    assert "generated_at = now()" in sql  # 오늘 몫 완료 멱등 마커
    assert "a" not in (params or {})


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
