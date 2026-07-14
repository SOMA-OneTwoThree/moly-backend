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


def test_home_enter_picks_from_the_hour_bucket():
    for hour, bucket in ((5, "dawn"), (9, "morning"), (14, "day"), (18, "evening"), (23, "night")):
        picked = {g.pick("home_enter", "지훈", hour) for _ in range(40)}
        assert picked <= set(g._HOME_BY_TIME[bucket])  # 다른 시간대 인사가 새지 않는다


def test_home_enter_without_hour_falls_back_to_day():
    assert g.pick("home_enter", "지훈") in g._HOME_BY_TIME["day"]


def test_every_context_has_a_pool():
    for ctx in g.CONTEXTS:
        assert g.pick(ctx, "지훈", 12)  # KeyError·빈 풀 없이 문구가 나온다


def test_pools_are_diverse_and_not_all_questions():
    """첫마디가 전부 질문이면 캐묻는 인상이 된다 — 절반 이상은 물음표 없이 끝난다."""
    for pool in (*g._HOME_BY_TIME.values(), *g._POOLS.values()):
        assert len(pool) >= 5
        assert len(set(pool)) == len(pool)  # 중복 없음
        assert sum(not line.rstrip().endswith("?") for line in pool) >= len(pool) / 2


def test_onboarding_uses_nickname_with_correct_josa():
    assert "지훈이라고" in g.pick("onboarding", "지훈") or "지훈아" in g.pick("onboarding", "지훈")
    assert g.pick("onboarding", None) in g._ONBOARDING_NONAME  # 닉네임 없으면 폴백
