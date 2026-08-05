"""관계 event → state → locale render (7장).

**자유 서술 하나로 관리하지 않는다.** 결정적 상태(함께한 날 수·성공 turn 수·단계)는 event를
집계해서 만들고, 문장은 그 상태의 **투영**일 뿐이다. 모델이 만든 문서를 정본으로 쓰면
말이 조금씩 흘러 관계가 임의로 진전되거나 후퇴한다.

정본은 세 겹으로 나뉜다:
  event   append-only 사실 (성공 turn·활동일)
  state   event를 집계한 결정적 값 — 단조 증가, 되돌아가지 않는다
  render  state의 locale별 문장 — locale이 바뀌면 이것만 다시 만든다

관계 시작 시각의 정본은 `profiles`다. 여기에 복제하지 않는다(7.2절).
"""
from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import i18n, relationship

RENDERER_VERSION = "relationship-render-v1"

# event를 집계한다. `dedup_key`가 중복 집계를 막으므로 여기서는 세기만 한다.
#
# ⚠️ event_type은 반드시 `relationship.EVENT_*` 상수와 같아야 한다. 예전엔 여기서
# 'successful_turn'/'qualifying_turn'을 셌는데 **그런 값은 기록되지 않는다** —
# CHECK 제약이 'normal_turn_committed'/'active_day_started'만 허용한다. 그래서 두 카운터가
# 항상 0으로 나왔고, `compute_stage(0, 0)`이 'new'를 돌려줘 단계가 영원히 오르지 않았다.
#
# qualifying은 **하루 :cap개까지만** 센다(불변식 5). 안 그러면 하루에 몰아친 대화만으로
# 며칠 만에 close에 도달한다. 이 규칙은 `relationship.counters_from_events`와 같아야 한다.
_AGGREGATE = text("""
WITH per_day AS (
  SELECT activity_date, count(DISTINCT turn_seq) AS turns
  FROM relationship_events
  WHERE user_id = :user_id AND event_type = :turn_event
  GROUP BY activity_date
)
SELECT
  (SELECT count(DISTINCT activity_date) FROM relationship_events WHERE user_id = :user_id),
  (SELECT COALESCE(sum(turns), 0) FROM per_day),
  (SELECT COALESCE(sum(LEAST(turns, :cap)), 0) FROM per_day),
  (SELECT max(occurred_at) FROM relationship_events WHERE user_id = :user_id)
""")

# ⚠️ 단조 증가로만 갱신한다. event가 늦게 도착하거나 재처리돼도 단계가 **뒤로 가지 않는다** —
# 캐피가 어제보다 덜 친해지는 일은 없어야 한다.
_UPSERT_STATE = text("""
INSERT INTO user_relationship_states
  (user_id, active_days, successful_turns, qualifying_turns,
   last_interaction_at, relationship_stage)
VALUES (:user_id, :active_days, :successful_turns, :qualifying_turns,
        :last_interaction_at, :stage)
ON CONFLICT (user_id) DO UPDATE SET
  active_days = GREATEST(user_relationship_states.active_days, EXCLUDED.active_days),
  successful_turns = GREATEST(
    user_relationship_states.successful_turns, EXCLUDED.successful_turns),
  qualifying_turns = GREATEST(
    user_relationship_states.qualifying_turns, EXCLUDED.qualifying_turns),
  last_interaction_at = GREATEST(
    COALESCE(user_relationship_states.last_interaction_at, EXCLUDED.last_interaction_at),
    EXCLUDED.last_interaction_at),
  -- stage도 GREATEST여야 한다. 예전엔 EXCLUDED로 무조건 덮어써서, 집계가 0을 내는 동안
  -- 이미 acquainted였던 사용자가 'new'로 **내려갔다**(dev 실측: qualifying 20인데 stage new).
  -- 순서는 STAGE_ORDER = new < acquainted < familiar < close다.
  relationship_stage = CASE
    WHEN array_position(ARRAY['new','acquainted','familiar','close'],
                        EXCLUDED.relationship_stage)
       > array_position(ARRAY['new','acquainted','familiar','close'],
                        user_relationship_states.relationship_stage)
    THEN EXCLUDED.relationship_stage
    ELSE user_relationship_states.relationship_stage END
RETURNING active_days, qualifying_turns, relationship_stage
""")

_LOAD_STATE = text("""
SELECT active_days, qualifying_turns, relationship_stage
FROM user_relationship_states WHERE user_id = :user_id
""")

_PROFILE_REVISION = text("""
SELECT COALESCE(EXTRACT(EPOCH FROM relationship_started_at)::bigint, 0)
FROM profiles WHERE id = :user_id
""")

# ⚠️ unique 키에 **revision이 들어간다**. 즉 revision마다 새 행이 남는 구조다(이력 보존).
# 덮어쓰기로 가정하고 (user_id, locale, renderer_version)로 ON CONFLICT를 걸면 맞는 제약이
# 없어서 터진다 — 그 실패는 예외가 아니라 `lease_expired`로만 보인다(실측).
_UPSERT_RENDER = text("""
INSERT INTO relationship_profile_renders
  (user_id, prompt_revision, profile_relationship_revision, locale,
   renderer_version, rendered_text, render_hash)
VALUES (:user_id, :prompt_revision, :profile_revision, :locale,
        :renderer_version, :rendered_text, :render_hash)
ON CONFLICT (user_id, prompt_revision, profile_relationship_revision, locale,
             renderer_version)
DO UPDATE SET rendered_text = EXCLUDED.rendered_text,
              render_hash = EXCLUDED.render_hash
""")

# 여러 revision이 쌓이므로 **최신 하나**를 집는다. 정렬이 없으면 아무거나 나온다.
_LOAD_RENDER = text("""
SELECT rendered_text FROM relationship_profile_renders
WHERE user_id = :user_id AND locale = :locale AND renderer_version = :renderer_version
ORDER BY prompt_revision DESC, profile_relationship_revision DESC
LIMIT 1
""")

# 단계별 문장. **상태의 투영이지 모델이 쓴 글이 아니다.**
_STAGE_TEXT: dict[str, dict[str, str]] = {
    "new": {
        "ko": "아직 알아가는 중이야. 조심스럽게 다가가.",
        "en": "You are still getting to know them. Approach gently.",
        "ja": "まだ知り合ったばかり。そっと近づいて。",
    },
    "acquainted": {
        "ko": "이제 서로 조금은 아는 사이야. 너무 어색해하지 않아도 돼.",
        "en": "You know each other a little now. No need to be too formal.",
        "ja": "少しずつ分かってきた間柄。そんなに気を張らなくていい。",
    },
    "familiar": {
        "ko": "이제 제법 익숙한 사이야. 편하게 말해도 돼.",
        "en": "You are familiar with each other now. You can speak comfortably.",
        "ja": "もうすっかり馴染んだ間柄。気楽に話していい。",
    },
    "close": {
        "ko": "오래 함께한 사이야. 굳이 설명하지 않아도 통하는 게 있어.",
        "en": "You have been together a long time. Some things go without saying.",
        "ja": "長く一緒にいる間柄。言わなくても通じるものがある。",
    },
}


def render_state(*, stage: str, active_days: int, language: str | None) -> str:
    """state → 문장. 모르는 단계면 빈 문자열이다(억지로 만들지 않는다)."""
    table = _STAGE_TEXT.get(stage)
    if table is None:
        return ""
    lang = i18n.resolve(language)
    line = table.get(lang) or table["ko"]
    label = {"en": "[relationship]", "ja": "[関係]"}.get(lang, "[관계]")
    days = {
        "en": f"Days together: {active_days}",
        "ja": f"一緒に過ごした日: {active_days}日",
    }.get(lang, f"함께한 날: {active_days}일")
    return f"{label}\n{days}\n{line}"


async def project(
    session: AsyncSession, user_id: uuid.UUID, *, language: str | None
) -> str:
    """event를 집계해 state를 갱신하고 locale render를 만든다. 반환 = 렌더 문자열."""
    agg = (await session.execute(_AGGREGATE, {
        "user_id": user_id,
        "turn_event": relationship.EVENT_NORMAL_TURN,
        "cap": relationship.MAX_QUALIFYING_TURNS_PER_DAY,
    })).first()
    if agg is None:
        return ""
    active_days = int(agg[0] or 0)
    stage = relationship.compute_stage(active_days, int(agg[2] or 0))

    row = (await session.execute(_UPSERT_STATE, {
        "user_id": user_id, "active_days": active_days,
        "successful_turns": int(agg[1] or 0), "qualifying_turns": int(agg[2] or 0),
        "last_interaction_at": agg[3], "stage": stage,
    })).first()
    if row is not None:
        # 단조 갱신 결과를 다시 읽는다 — 늦게 온 event가 값을 낮췄을 수 있다.
        active_days, stage = int(row[0]), str(row[2])

    rendered = render_state(stage=stage, active_days=active_days, language=language)
    if not rendered:
        return ""

    revision = int(await session.scalar(_PROFILE_REVISION, {"user_id": user_id}) or 0)
    await session.execute(_UPSERT_RENDER, {
        "user_id": user_id, "prompt_revision": revision,
        "profile_revision": revision, "locale": i18n.resolve(language),
        "renderer_version": RENDERER_VERSION, "rendered_text": rendered,
        "render_hash": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    })
    return rendered


async def prompt_text(
    session: AsyncSession, user_id: uuid.UUID, *, language: str | None
) -> str:
    """프롬프트용 관계 블록. **읽기만 한다** — 챗 경로에서 집계를 돌리지 않는다.

    없으면 빈 문자열이다. projector가 아직 안 돈 사용자는 관계 블록 없이 대화한다.
    """
    got = await session.scalar(_LOAD_RENDER, {
        "user_id": user_id, "locale": i18n.resolve(language),
        "renderer_version": RENDERER_VERSION,
    })
    return str(got or "")
