"""선발화 프리셋 — 시간대 버킷·풀 무결성·조사 처리."""
from app.services import greetings as g


def test_time_bucket_covers_all_24_hours():
    buckets = {h: g.time_bucket(h) for h in range(24)}
    assert buckets[4] == "dawn" and buckets[6] == "dawn"
    assert buckets[7] == "morning" and buckets[10] == "morning"
    assert buckets[11] == "day" and buckets[16] == "day"
    assert buckets[17] == "evening" and buckets[20] == "evening"
    # 21~03 = 밤(하루 경계 04:00 직전까지)
    assert all(buckets[h] == "night" for h in (21, 22, 23, 0, 1, 2, 3))
    assert set(buckets.values()) == set(g._HOME_BY_TIME)  # 빈 풀을 가리키는 버킷 없음


def _expected(pool, nickname):
    """이름 자리가 있는 문구는 치환된 형태로 비교(원문 풀엔 '{subj}'로 들어있음)."""
    return {g._personalize(t, nickname) if "{" in t else t for t in pool}


def test_home_enter_picks_from_the_hour_bucket():
    for hour, bucket in ((5, "dawn"), (9, "morning"), (14, "day"), (18, "evening"), (23, "night")):
        picked = {g.pick("home_enter", "지훈", hour) for _ in range(60)}
        assert picked <= _expected(g._HOME_BY_TIME[bucket], "지훈")  # 다른 시간대 인사 안 샘


def test_home_enter_without_hour_falls_back_to_day():
    assert g.pick("home_enter", "지훈") in _expected(g._HOME_BY_TIME["day"], "지훈")


def test_every_context_has_a_pool():
    for ctx in g.CONTEXTS:
        assert g.pick(ctx, "지훈", 12)  # KeyError·빈 풀 없이 문구가 나온다


def test_pools_nonempty_and_no_internal_dupes():
    for pool in (*g._HOME_BY_TIME.values(), *g._POOLS.values()):
        assert pool                                  # 빈 풀 없음
        assert len(set(pool)) == len(pool)           # 풀 내부 중복 없음


def test_morning_greeting_substitutes_nickname_subject_josa():
    # '{subj} 왔어? 아침은?' → 받침 유무에 맞는 주격 조사(승민이 / 지호가)
    picks = {g.pick("home_enter", "승민", 9) for _ in range(80)}
    assert "승민이 왔어? 아침은?" in picks
    picks_jh = {g.pick("home_enter", "지호", 9) for _ in range(80)}
    assert "지호가 왔어? 아침은?" in picks_jh


def test_with_wa_and_subject_josa():
    assert g.with_wa("승민") == "승민과" and g.with_wa("지호") == "지호와"
    assert g.subject("승민") == "승민이" and g.subject("지호") == "지호가"


def test_non_korean_name_no_josa():
    # 비한글 이름엔 한국어 조사를 붙이지 않는다 — 'Alex야' 방지(SOMA-347).
    assert g.copula("Alex") == "Alex"
    assert g.quote_ira("Alex") == "Alex"
    assert g.with_wa("Alex") == "Alex"
    assert g.vocative("Alex") == "Alex" and g.subject("Alex") == "Alex"
    # 한글 이름은 조사 유지(회귀 없음).
    assert g.copula("지훈") == "지훈이야" and g.copula("지호") == "지호야"


def test_onboarding_uses_nickname_with_correct_josa():
    assert "지훈이라고" in g.pick("onboarding", "지훈") or "지훈아" in g.pick("onboarding", "지훈")
    assert g.pick("onboarding", None) == "난 캐피야, 이 집에 살아. 편하게 얘기 걸어."  # 안전 폴백
