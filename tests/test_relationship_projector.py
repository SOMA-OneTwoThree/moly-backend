"""관계 event → state → render (7장).

**자유 서술 하나로 관리하지 않는다.** 모델이 쓴 문서를 정본으로 삼으면 말이 조금씩 흘러
관계가 임의로 진전되거나 후퇴한다. 상태는 결정적으로 계산하고 문장은 그 투영일 뿐이다.
"""
from __future__ import annotations

import inspect

import pytest

from app.services import chat, relationship, relationship_projector as rp
from worker import consumer, relationship_jobs


def test_render_is_a_projection_of_state_not_model_output():
    """모델 호출이 있으면 그 문장이 정본이 되어 값이 흘러간다."""
    src = inspect.getsource(rp)
    assert "llm.generate" not in src and "await llm" not in src


@pytest.mark.parametrize("stage", ["new", "familiar", "close"])
def test_every_stage_has_text_in_every_language(stage):
    """빠진 언어가 있으면 그 사용자만 관계 블록 없이 대화한다."""
    for lang in ("ko", "en", "ja"):
        got = rp.render_state(stage=stage, active_days=10, language=lang)
        assert got and str(10) in got


def test_stage_names_match_the_deterministic_calculator():
    """계산기와 렌더 표가 갈라지면 특정 단계에서 관계 블록이 빈다."""
    computed = {
        relationship.compute_stage(0, 0),
        relationship.compute_stage(30, 100),
        relationship.compute_stage(400, 5000),
    }
    assert computed <= set(rp._STAGE_TEXT)


def test_unknown_stage_renders_empty_not_guessed():
    """모르는 단계에 문장을 지어내면 사실이 아닌 관계가 프롬프트에 들어간다."""
    assert rp.render_state(stage="soulmate", active_days=1, language="ko") == ""


def test_state_update_is_monotonic():
    """늦게 온 event나 재처리로 단계가 뒤로 가면 캐피가 어제보다 덜 친해진다."""
    src = inspect.getsource(rp)
    assert "GREATEST" in src, "단조 갱신이 아니다"


def test_relationship_start_is_not_duplicated_into_state():
    """관계 시작 시각의 정본은 profiles다(7.2절). 복제하면 두 값이 갈라진다."""
    src = inspect.getsource(rp)
    assert "relationship_started_at" not in src.split("_UPSERT_STATE")[1].split('"""')[0]


# ── 챗 연결 ──────────────────────────────────────────────────

def test_chat_reads_the_render_without_aggregating():
    """매 턴 전체 event를 세면 대화 지연이 이력 길이에 비례해 늘어난다."""
    src = inspect.getsource(chat.post_message)
    assert "relationship_projector.prompt_text(" in src
    assert "relationship_projector.project(" not in src


def test_v2_relationship_replaces_the_legacy_block():
    """둘 다 넣으면 같은 관계를 두 번 말한다."""
    out = "\n".join(chat._build_system(
        language="ko", nickname="승민", lead=None,
        relationship_text="레거시 관계", relationship_v2="[관계]\n함께한 날: 3일",
    ))
    assert "함께한 날" in out and "레거시 관계" not in out


def test_non_v2_user_is_unaffected():
    out = "\n".join(chat._build_system(
        language="ko", nickname="승민", lead=None, relationship_text="레거시 관계"))
    assert "레거시 관계" in out


def test_projector_job_is_registered():
    consumer._register_handlers()
    assert consumer._REGISTRY.get(relationship_jobs.JOB_RELATIONSHIP_PROJECT) is not None
