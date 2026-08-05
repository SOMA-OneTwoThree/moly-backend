"""기억 추출 정책 — dev 실측에서 틀린 기억을 만든 사례들을 고정한다.

전부 감사가 실제 대화에서 잡은 것이다. 프롬프트 규칙으로 고친 것과 코드로 막은 것이
섞여 있는데, **프롬프트로 못 막은 건 코드로 막았다** — 모델이 표현만 바꿔 계속 빠져나갔다.
"""
from __future__ import annotations

import pytest

from app.services import mem0_ingest as mi


def _c(text, mid=1):
    return mi.Candidate(
        text=text,
        evidence=(mi.EvidenceSpan(message_id=mid, sender="user",
                                  start_utf8=0, end_utf8=3, content_hash="h"),),
        category="event",
    )


# ── 잊어달라는 요청 (코드로 막는다) ──────────────────────────

@pytest.mark.parametrize("utterance", [
    "그래 줄무늬 조끼는 없는거야. 잊어",
    "방금 그건 잊어줘",
    "없던 걸로 해줘",
    "그 얘기 지워줘",
    "그건 기억하지 마",
])
def test_forget_request_never_becomes_a_memory(utterance):
    """지워달라는 말을 기억으로 만들면 그 내용을 오히려 영원히 들고 있게 된다.

    프롬프트로 두 번 시도했으나 모델이 '정정한다'로 바꿔 계속 뽑았다(실측). 코드로 막는다.
    """
    passed, rejected = mi.filter_candidates(
        [_c("유저가 줄무늬 조끼가 없다고 말한다")], source_texts={1: utterance}
    )
    assert passed == []
    assert rejected[0][1] == "forget_request"


def test_ordinary_utterance_still_passes():
    """방어가 정상 기억까지 막으면 기능이 죽는다."""
    passed, _ = mi.filter_candidates(
        [_c("유저가 카피바라 그림을 그린다")], source_texts={1: "카피바라 그림 그리고 있어"}
    )
    assert len(passed) == 1


def test_forget_detection_does_not_fire_on_unrelated_words():
    """'잊지 못할' 같은 표현까지 막으면 정상 기억이 사라진다."""
    assert not mi.mentions_forget_request("어제 일은 잊지 못할 하루였어")
    assert mi.mentions_forget_request("그건 잊어줘")


def test_source_text_absent_does_not_crash():
    """근거 원문을 못 찾아도 추출이 통째로 실패하면 안 된다."""
    passed, _ = mi.filter_candidates([_c("유저가 산책을 갔다")], source_texts={})
    assert len(passed) == 1


# ── 프롬프트 규칙 (실제 모델로 검증했고, 여기선 규칙 존재를 고정한다) ──

def test_extractor_prompt_forbids_intent_as_completed():
    """'라면 먹을래'가 '라면을 먹었다'로 저장되면 캐피가 없던 일을 말한다."""
    from app.services import mem0_extractor as ex

    p = ex.build_system("ko")
    assert "하려는 것과 한 것" in p


def test_extractor_prompt_requires_third_person():
    """주어가 없으면 캐피가 자기 일로 착각한다('그림 그리고 있어')."""
    from app.services import mem0_extractor as ex

    assert "3인칭" in ex.build_system("ko")


def test_extractor_prompt_keeps_corrections():
    """부정을 안 뽑으면 옛 사실이 영원히 남는다."""
    from app.services import mem0_extractor as ex

    assert "정정과 부정도 새 사실" in ex.build_system("ko")


def test_versions_bumped_so_old_memories_are_distinguishable():
    """규칙이 바뀌었는데 버전이 그대로면 무엇을 재처리해야 하는지 알 수 없다."""
    from app.services import mem0_classifier as cl
    from app.services import mem0_extractor as ex

    assert ex.EXTRACTOR_VERSION.endswith("v2")
    assert cl.CLASSIFIER_VERSION.endswith("v2")
