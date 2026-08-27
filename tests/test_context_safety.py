"""과거 위기 에피소드 컨텍스트 압축 — 최신 위기는 보존하고 완료된 과거 반복만 제거한다."""
from datetime import date

import pytest

from app.services import checkpoint, context_safety as cs


D1 = date(2026, 8, 20)
D2 = date(2026, 8, 21)


def _e(role: str, content: str, day: date = D1) -> cs.ContextEntry:
    return cs.ContextEntry(role, content, day)


@pytest.mark.parametrize(
    ("language", "crisis", "safety", "shift", "current"),
    [
        (
            "ko-KR",
            "지금 죽고 싶어.",
            "지금 안전한 곳이야? 혼자 있지 말고 주변 사람에게 알려줘.",
            "다른 얘기하자. 오늘 영화 봤어.",
            "주인공이 정말 좋았어.",
        ),
        (
            "en-US",
            "I want to die.",
            "Are you safe right now? Please contact a trusted person nearby.",
            "By the way, I watched a movie today.",
            "I really liked the main character.",
        ),
        (
            "ja-JP",
            "死にたい。",
            "今は安全？一人でいないで近くの人に知らせて。",
            "ところで、今日は映画を観た。",
            "主人公がとても良かった。",
        ),
    ],
)
def test_completed_episode_is_compacted_after_clear_topic_shift(
    language, crisis, safety, shift, current
):
    repeated = safety + " "  # 거의 동일한 과거 assistant boilerplate
    entries = [
        _e("user", crisis),
        _e("assistant", safety),
        _e("user", "응" if language.startswith("ko") else ("okay" if language.startswith("en") else "うん")),
        _e("assistant", repeated),
        _e("user", shift),
        _e("assistant", "그 영화는 어땠어?"),
    ]

    result = cs.compact_historical_crises(
        entries, current_text=current, current_date=D1, language=language
    )

    prompt = "\n".join(e.content for e in result.entries)
    assert result.compacted_episodes == 1
    assert result.note
    assert crisis not in prompt
    assert safety not in prompt and repeated not in prompt
    assert shift in prompt


@pytest.mark.parametrize(
    ("language", "current"),
    [("ko", "지금 죽고 싶어."), ("en", "I want to kill myself."), ("ja", "今、死にたい。")],
)
def test_current_crisis_disables_all_compaction(language, current):
    old = [
        _e("user", current),
        _e("assistant", "지금 안전한 곳이야?"),
        _e("user", "다른 얘기하자. 오늘 영화 봤어."),
    ]

    result = cs.compact_historical_crises(
        old, current_text=current, current_date=D2, language=language
    )

    assert result.entries == tuple(old)
    assert result.note == ""
    assert result.compacted_episodes == 0


def test_acknowledgement_is_not_a_topic_transition():
    entries = [
        _e("user", "죽고 싶어."),
        _e("assistant", "지금 안전한 곳이야?"),
    ]
    result = cs.compact_historical_crises(
        entries, current_text="응, 고마워.", current_date=D1, language="ko"
    )
    assert result.entries == tuple(entries)
    assert result.note == ""


def test_one_substantive_topic_on_a_later_day_is_a_clear_transition():
    entries = [
        _e("user", "죽고 싶어."),
        _e("assistant", "지금 안전한 곳이야?"),
    ]
    result = cs.compact_historical_crises(
        entries, current_text="오늘 새 프로젝트를 시작했어.", current_date=D2, language="ko"
    )
    assert result.entries == ()
    assert result.compacted_episodes == 1


def test_same_day_requires_two_substantive_topic_messages_without_shift_marker():
    entries = [
        _e("user", "죽고 싶어."),
        _e("assistant", "지금 안전한 곳이야?"),
        _e("user", "오늘 새 프로젝트를 시작했어."),
        _e("assistant", "어떤 프로젝트인데?"),
    ]
    result = cs.compact_historical_crises(
        entries, current_text="친구와 만드는 작은 게임이야.", current_date=D1, language="ko"
    )
    assert [e.content for e in result.entries] == [
        "오늘 새 프로젝트를 시작했어.",
        "어떤 프로젝트인데?",
    ]


def test_no_assistant_reply_means_episode_is_not_completed():
    entries = [_e("user", "죽고 싶어."), _e("user", "다른 얘기하자. 영화 봤어.")]
    result = cs.compact_historical_crises(
        entries, current_text="주인공이 정말 좋았어.", current_date=D1, language="ko"
    )
    assert result.entries == tuple(entries)
    assert result.note == ""


def test_unrelated_assistant_reply_does_not_complete_crisis_episode():
    entries = [
        _e("user", "죽고 싶어."),
        _e("assistant", "오늘 날씨가 꽤 따뜻하네."),
        _e("user", "다른 얘기하자. 오늘 영화 봤어."),
        _e("assistant", "어떤 영화였어?"),
    ]

    result = cs.compact_historical_crises(
        entries, current_text="주인공이 정말 좋았어.", current_date=D1, language="ko"
    )

    assert result.entries == tuple(entries)
    assert result.note == ""
    assert result.compacted_episodes == 0


def test_matching_safety_reply_allows_completed_episode_compaction():
    entries = [
        _e("user", "죽고 싶어."),
        _e("assistant", "지금 안전한 곳이야? 혼자 있지 말고 주변 사람에게 알려줘."),
        _e("user", "다른 얘기하자. 오늘 영화 봤어."),
    ]

    result = cs.compact_historical_crises(
        entries, current_text="주인공이 정말 좋았어.", current_date=D1, language="ko"
    )

    assert [entry.content for entry in result.entries] == ["다른 얘기하자. 오늘 영화 봤어."]
    assert result.note
    assert result.compacted_episodes == 1


def test_multiple_completed_crisis_episodes_are_compacted_independently():
    entries = [
        _e("user", "죽고 싶어."),
        _e("assistant", "지금 안전한 곳이야? 주변 사람에게 알려줘."),
        _e("user", "다른 얘기하자. 오늘 영화 봤어."),
        _e("assistant", "어떤 영화였어?"),
        _e("user", "다시 자해하고 싶어."),
        _e("assistant", "지금 안전한 곳이야? 다칠 수 있는 것은 멀리 두자."),
        _e("user", "그건 그렇고 저녁에는 산책했어."),
        _e("assistant", "산책은 어땠어?"),
    ]

    result = cs.compact_historical_crises(
        entries, current_text="공원이 조용해서 좋았어.", current_date=D1, language="ko"
    )

    prompt = "\n".join(entry.content for entry in result.entries)
    assert result.compacted_episodes == 2
    assert "죽고 싶" not in prompt
    assert "자해하고 싶" not in prompt
    assert "오늘 영화 봤어" in prompt
    assert "저녁에는 산책했어" in prompt


@pytest.mark.parametrize(
    ("language", "crisis", "safety", "continuing"),
    [
        ("ko", "죽고 싶어.", "지금 안전한 곳이야?", "아직 너무 힘들어서 못 버티겠어."),
        ("en", "I want to die.", "Are you safe right now?", "I still feel unsafe."),
        ("ja", "死にたい。", "今は安全？", "まだつらくて耐えられない。"),
    ],
)
def test_current_continuing_distress_preserves_all_historical_crisis_context(
    language, crisis, safety, continuing
):
    entries = [
        _e("user", crisis),
        _e("assistant", safety),
        _e("user", "다른 얘기하자. 오늘 영화 봤어."),
    ]

    result = cs.compact_historical_crises(
        entries, current_text=continuing, current_date=D2, language=language
    )

    assert result.entries == tuple(entries)
    assert result.note == ""
    assert result.compacted_episodes == 0


def test_existing_checkpoint_is_neutralized_only_with_clear_later_topic():
    summary = (
        "유저는 새 프로젝트를 시작했다. 죽고 싶다고 말했다. "
        "캐피는 지금 안전한 곳인지 확인하고 주변 사람에게 연락하라고 했다."
    )
    recent = [
        _e("user", "오늘 영화 봤어."),
        _e("assistant", "어땠어?"),
    ]
    compacted = cs.compact_checkpoint_summary(
        summary,
        recent_entries=recent,
        current_text="주인공이 정말 좋았어.",
        language="ko",
    )
    assert "새 프로젝트" in compacted
    assert "죽고 싶" not in compacted
    assert "안전한 곳" not in compacted
    assert "힘든 순간" in compacted


def test_existing_checkpoint_stays_raw_during_current_crisis():
    summary = "이전에 죽고 싶다고 말했고 캐피가 안전을 확인했다."
    out = cs.compact_checkpoint_summary(
        summary,
        recent_entries=[_e("user", "오늘 영화 봤어.")],
        current_text="지금 죽고 싶어.",
        language="ko",
    )
    assert out == summary


@pytest.mark.parametrize(
    ("language", "summary", "continuing"),
    [
        ("ko", "이전에 죽고 싶다고 말했고 캐피가 안전을 확인했다.", "아직 너무 힘들어서 못 버티겠어."),
        ("en", "They wanted to die and Cappy checked whether they were safe.", "I still feel unsafe."),
        ("ja", "以前、死にたいと話し、キャッピーが安全を確認した。", "まだつらくて耐えられない。"),
    ],
)
def test_checkpoint_stays_raw_during_current_continuing_distress(
    language, summary, continuing
):
    out = cs.compact_checkpoint_summary(
        summary,
        recent_entries=[_e("user", "오늘 영화 봤어.")],
        current_text=continuing,
        language=language,
    )

    assert out == summary


def test_checkpoint_prompt_encodes_completed_vs_active_crisis_rule():
    prompt = checkpoint.build_system("ko")
    assert "다른 화제로 명확히 넘어갔다면" in prompt
    assert "최신 위기 발화" in prompt
    assert checkpoint.SUMMARIZER_VERSION.endswith("v2")
