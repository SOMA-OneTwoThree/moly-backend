"""오늘의 작은 실천 선택 — 공용 풀에서 하나를 고르는 결정적 점수 함수.

DB도 시계도 읽지 않는다. 무작위는 주입받으므로 같은 입력·같은 seed면 같은 답이 나온다.
"""
from __future__ import annotations

import random
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

CATEGORIES = ("rest", "body", "calm", "mind", "self_kindness", "connect", "order")

SHOWN_EXCLUSION_DAYS = 30
LEAST_RECENT_KEEP = 5  # 30일 제외를 풀 때 점수 경쟁에 남기는 수

FOCUS_MAX = 4.0  # 기록이 없을 때 focus 첫 순위의 가중치
FOCUS_FADE_EVENTS = 8  # 완료·건너뛰기가 이만큼 쌓이면 focus 가중치가 절반이 된다
FOCUS_RANK = (1.0, 0.6, 0.35)
COMPLETED_WEIGHT = 0.6
SKIPPED_WEIGHT = -0.4
HISTORY_HALF_LIFE_DAYS = 14
HISTORY_CLAMP = 1.5
MOOD_LEAN = 1.5
LOW_MOOD_KINDS = frozenset({"annoyed", "tired"})
KNOWN_MOOD_KINDS = LOW_MOOD_KINDS | {"neutral", "content", "excited"}
LOW_MOOD_CATEGORIES = frozenset({"rest", "calm", "self_kindness"})
NOISE = 0.6


class Candidate(Protocol):
    id: uuid.UUID
    category: str
    slots: Sequence[str]


@dataclass(frozen=True, slots=True)
class Shown:
    action_id: uuid.UUID
    category: str
    status: str  # offered | skipped | accepted | completed
    local_date: date


@dataclass(frozen=True, slots=True)
class Selection:
    action: Candidate | None
    short_categories: tuple[str, ...] = ()  # 보충을 요청할 카테고리


def slot_of(hour: int) -> str:
    """현지 시각의 시간대. morning 05-11, day 11-17, evening 17-22, night 22-05."""
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "day"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def focus_strength(events: int) -> float:
    return FOCUS_MAX / (1 + events / FOCUS_FADE_EVENTS)


def category_scores(
    history: Iterable[Shown], focus: Sequence[str], moods: Iterable[str], today: date
) -> dict[str, float]:
    history = list(history)
    events = sum(1 for row in history if row.status in {"completed", "skipped"})
    strength = focus_strength(events)
    scores = dict.fromkeys(CATEGORIES, 0.0)

    for rank, category in reversed(list(enumerate(focus[: len(FOCUS_RANK)]))):
        scores[category] = strength * FOCUS_RANK[rank]

    learned = dict.fromkeys(CATEGORIES, 0.0)
    for row in history:
        weight = {"completed": COMPLETED_WEIGHT, "skipped": SKIPPED_WEIGHT}.get(row.status)
        if weight is not None and row.category in learned:
            age = max(0, (today - row.local_date).days)
            learned[row.category] += weight * 0.5 ** (age / HISTORY_HALF_LIFE_DAYS)
    for category, value in learned.items():
        scores[category] += max(-HISTORY_CLAMP, min(HISTORY_CLAMP, value))

    known = [kind for kind in moods if kind in KNOWN_MOOD_KINDS]
    if known:
        low = sum(1 for kind in known if kind in LOW_MOOD_KINDS) / len(known)
        for category in LOW_MOOD_CATEGORIES:
            scores[category] += MOOD_LEAN * low
    return scores


def choose(
    actions: Iterable[Candidate],
    *,
    slot: str,
    today: date,
    history: Iterable[Shown],
    focus: Sequence[str] = (),
    moods: Iterable[str] = (),
    rng: random.Random,
) -> Selection:
    history = list(history)
    avoided = {row.category for row in history if row.local_date == today and row.status == "skipped"}
    shown_today = {row.action_id for row in history if row.local_date == today}
    last_shown: dict[uuid.UUID, date] = {}
    for row in history:
        if row.action_id not in last_shown or row.local_date > last_shown[row.action_id]:
            last_shown[row.action_id] = row.local_date
    cutoff = today - timedelta(days=SHOWN_EXCLUSION_DAYS)

    # id 정렬로 입력 순서가 결과와 noise 소비에 영향을 주지 않게 한다.
    fitting = sorted(
        (a for a in actions if slot in a.slots and a.id not in shown_today),
        key=lambda a: a.id,
    )
    if not fitting:
        return Selection(None, tuple(dict.fromkeys(focus)) or CATEGORIES)

    def is_fresh(action: Candidate) -> bool:
        return action.id not in last_shown or last_shown[action.id] < cutoff

    def least_recent(pool: list[Candidate]) -> list[Candidate]:
        return sorted(pool, key=lambda a: (last_shown[a.id], a.id))[:LEAST_RECENT_KEEP]

    wanted = [a for a in fitting if a.category not in avoided]
    fresh = [a for a in wanted if is_fresh(a)]
    short = not fresh
    if fresh:
        pool = fresh
    elif wanted:
        pool = least_recent(wanted)
    else:
        fresh_avoided = [a for a in fitting if is_fresh(a)]
        pool = fresh_avoided or least_recent(fitting)

    scores = category_scores(history, focus, moods, today)
    picked = max(pool, key=lambda a: scores[a.category] + rng.uniform(0, NOISE))
    if not short:
        return Selection(picked)
    return Selection(picked, tuple(dict.fromkeys([*focus[:1], picked.category])))
