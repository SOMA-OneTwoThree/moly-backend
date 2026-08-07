"""기억 상태를 닫는 곳은 provider 벡터 정리도 같이 걸어야 한다.

`apply_transitions`가 기억을 duplicate/superseded로 닫으면 `provider_delete_state='pending'`이
된다. 그 pending을 집어가는 것은 `mem0_provider_delete` 잡뿐이고, **그 잡은 스스로 생기지
않는다** — 닫은 쪽이 같은 트랜잭션에서 걸어 줘야 한다.

실제로 `worker/reconsolidate_jobs.py`가 이걸 빠뜨렸다. 증상은 조용했다: 재판정은 "성공"으로
끝나고, 벡터만 provider에 영영 남았다. dev에서 pending 1건이 그렇게 방치돼 cutover 게이트의
`provider delete backlog` 항목에 걸려서야 드러났다.

이런 잡은 앞으로도 늘어난다(닫는 경로가 하나가 아니다). 그래서 개별 잡 테스트 대신 **소스에서
짝을 검사한다** — `apply_transitions`를 부르는 worker 모듈은 provider delete도 걸어야 한다.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

_WORKER = pathlib.Path(__file__).resolve().parents[1] / "worker"

# 이 짝을 면제받는 모듈. 면제한다면 **왜인지** 여기 적는다.
_EXEMPT: dict[str, str] = {}


def _modules_closing_memories() -> list[pathlib.Path]:
    out = []
    for p in sorted(_WORKER.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        if "apply_transitions(" in p.read_text(encoding="utf-8"):
            out.append(p)
    return out


def test_some_worker_closes_memories():
    """검사 대상이 0개면 이 파일은 아무것도 지키지 않는다 — 이름이 바뀌었는지 확인할 것."""
    assert _modules_closing_memories(), "apply_transitions를 부르는 worker 모듈을 못 찾았다"


@pytest.mark.parametrize(
    "path", _modules_closing_memories(), ids=lambda p: p.name
)
def test_closing_memories_also_enqueues_provider_delete(path):
    if path.name in _EXEMPT:
        pytest.skip(_EXEMPT[path.name])
    src = path.read_text(encoding="utf-8")
    assert "JOB_MEM0_PROVIDER_DELETE" in src or "enqueue_provider_delete" in src, (
        f"{path.name}이 기억을 닫으면서 provider 벡터 정리를 걸지 않는다. "
        "닫힌 기억의 벡터가 provider에 영영 남는다 — 증상이 조용해서 찾기 어렵다. "
        "정말 필요 없다면 _EXEMPT에 사유와 함께 적어라."
    )


# --- 없는 능력을 모델에 광고하지 않는다 ---

def test_final_response_schema_advertises_no_forget():
    """모델에 보내는 도구 스키마에 **적용 경로가 없는 기능**이 있으면 안 된다.

    `finish_response`는 오랫동안 `control_intents(kind: forget|pin)`을 스키마로 노출했다.
    대화형 망각은 제거됐는데(제품 판단) 필드만 남아서, 모델은 지울 수 있다고 믿고 의도를
    보냈고 서버는 그걸 버렸다. 게다가 chat이 그 의도를 보면 **"지울게"라고 답하는 문구를
    하드코딩**해 뒀다 — 아무것도 지우지 않으면서 캐피가 거짓말을 했다.

    스키마는 매 요청 프리픽스에 들어가므로 비용도 든다. 능력이 없으면 광고하지 않는다.
    """
    from app.services.agent.tools import final_response

    wire = json.dumps(final_response.wire_schema(), ensure_ascii=False)
    for banned in ("control_intents", "forget", "future_learning", "target_fact_ids"):
        assert banned not in wire, (
            f"finish_response 스키마에 {banned!r}이 있다. 적용 경로가 생기기 전에는 "
            "모델에 노출하지 않는다 — 모델이 없는 능력을 약속하게 된다."
        )


# --- 집계 SQL이 실제로 기록되는 event_type을 세는가 ---

def test_relationship_aggregate_uses_recorded_event_types():
    """집계가 **기록되지 않는 값**을 세면 조용히 0이 나오고 단계가 영원히 오르지 않는다.

    실제로 그랬다. projector는 'successful_turn'/'qualifying_turn'을 셌는데, 기록되는 값도
    CHECK 제약이 허용하는 값도 'normal_turn_committed'/'active_day_started'뿐이다. 두 카운터가
    항상 0이라 `compute_stage(0, 0)`이 'new'를 돌려줬고, dev 사용자는 111턴·3일을 대화하고도
    stage가 'new'였다. 에러는 나지 않는다 — 숫자만 0일 뿐이다.
    """
    from app.services import relationship, relationship_projector

    sql = str(relationship_projector._AGGREGATE)
    known = {relationship.EVENT_NORMAL_TURN, relationship.EVENT_ACTIVE_DAY}
    for literal in re.findall(r"'([a-z_]+_turn|[a-z_]+_started|[a-z_]+_committed)'", sql):
        assert literal in known, (
            f"집계 SQL이 {literal!r}을 세는데 그런 event_type은 기록되지 않는다. "
            f"기록되는 값: {sorted(known)}"
        )


def test_every_stage_has_prompt_text():
    """단계가 올라갔는데 문구가 없으면 렌더가 비고, **옛 문구가 계속 나간다**.

    'acquainted'가 실제로 빠져 있었다. project()는 빈 렌더면 새 행을 쓰지 않고 반환하므로
    (relationship_projector.project), 승급 순간부터 프롬프트에는 'new' 문장이 계속 붙는다.
    """
    from app.services import relationship
    from app.services.relationship_projector import _STAGE_TEXT

    missing = [s for s in relationship.STAGE_ORDER if s not in _STAGE_TEXT]
    assert not missing, f"프롬프트 문구가 없는 단계: {missing}"
    for stage, table in _STAGE_TEXT.items():
        assert {"ko", "en", "ja"} <= set(table), f"{stage}에 빠진 언어: {{'ko','en','ja'}} - {set(table)}"
