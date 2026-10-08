"""루틴 기록 계산 — 요일 이력과 완료 날짜만으로 예정·인스턴스·스트릭·달성률을 낸다(DB 없음).

날짜는 모두 사용자 로컬 날짜다. 루틴은 시작일부터 삭제일 전날까지 활성이고, 그날의 요일은 그날 이하에서
가장 늦은 이력이 정한다. 인스턴스 = 그날 완료 기록이 있거나, 예정이었고 스킵하지 않은 (루틴, 날짜).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

Version = tuple[date, tuple[int, ...]]
Day = tuple[date, int, int]  # (날짜, 인스턴스 수, 완료 수)

_ONE_DAY = timedelta(days=1)


def dates(first: date, last: date) -> Iterator[date]:
    day = first
    while day <= last:
        yield day
        day += _ONE_DAY


def _days(days: Iterable[int]) -> tuple[int, ...]:
    return tuple(sorted(set(days)))


def schedule(
    stored: Iterable[tuple[date, Sequence[int]]],
    days: Sequence[int],
    created_on: date,
    updated_on: date | None,
) -> tuple[Version, ...]:
    """저장된 요일 이력. 이력이 없으면 생성일부터 현재 요일이다. 이전 서버가 이력 없이 요일만 바꿨으면
    마지막 수정일부터 현재 요일로 본다."""
    current = _days(days)
    versions = sorted((start, _days(values)) for start, values in stored)
    if not versions:
        return ((created_on, current),)
    last_from, last_days = versions[-1]
    if last_days != current:
        if updated_on is not None and updated_on > last_from:
            versions.append((updated_on, current))
        else:
            versions[-1] = (last_from, current)
    return tuple(versions)


def reschedule(versions: Sequence[Version], today: date, days: Sequence[int]) -> list[Version]:
    """오늘부터 새 요일. 어제까지의 이력은 그대로 둔다."""
    return [version for version in versions if version[0] < today] + [(today, _days(days))]


@dataclass(frozen=True)
class RoutineRecord:
    versions: tuple[Version, ...]
    end: date | None
    completed: frozenset[date]
    skips: frozenset[date] = frozenset()

    @property
    def start(self) -> date:
        return self.versions[0][0]

    def days_on(self, day: date) -> tuple[int, ...]:
        current = self.versions[0][1]
        for start, days in self.versions[1:]:
            if start > day:
                break
            current = days
        return current

    def planned(self, day: date) -> bool:
        return (
            self.start <= day
            and (self.end is None or day < self.end)
            and day.isoweekday() in self.days_on(day)
        )

    def skipped(self, day: date) -> bool:
        return day in self.skips and day not in self.completed and self.planned(day)

    def is_instance(self, day: date) -> bool:
        return day in self.completed or (day not in self.skips and self.planned(day))

    def instance_dates(self, today: date) -> frozenset[date]:
        last = today if self.end is None else min(today, self.end - _ONE_DAY)
        planned = {
            day for day in dates(self.start, last)
            if day not in self.skips and day.isoweekday() in self.days_on(day)
        }
        return frozenset(planned | {day for day in self.completed if day <= today})

    def days(self, today: date) -> list[Day]:
        instances = self.instance_dates(today)
        if not instances:
            return []
        return [
            (day, int(day in instances), int(day in self.completed))
            for day in dates(min(instances), today)
        ]


def streaks(days: Iterable[Day], today: date) -> tuple[int, int]:
    """(현재, 최장). 완료가 있으면 +1, 인스턴스가 없는 날과 오늘은 건너뛰고, 지난 미완료에서 끊는다."""
    run = best = 0
    for day, scheduled, completed in days:
        if completed:
            run += 1
            best = max(best, run)
        elif scheduled and day < today:
            run = 0
    return run, best


def rate(days: Iterable[Day], today: date) -> float | None:
    """완료 / (지난 인스턴스 + 오늘 완료). 분모가 0이면 None."""
    done = total = 0
    for day, scheduled, completed in days:
        done += completed
        total += scheduled if day < today else completed
    return done / total if total else None


@dataclass(frozen=True)
class Totals:
    scheduled: Counter[date]
    completed: Counter[date]

    def day(self, day: date) -> Day:
        return day, self.scheduled[day], self.completed[day]


def totals(records: Iterable[RoutineRecord], today: date) -> Totals:
    scheduled: Counter[date] = Counter()
    completed: Counter[date] = Counter()
    for record in records:
        scheduled.update(record.instance_dates(today))
        completed.update(day for day in record.completed if day <= today)
    return Totals(scheduled, completed)


def summary(totals: Totals, today: date) -> dict:
    days = [totals.day(day) for day in dates(min(totals.scheduled), today)] if totals.scheduled else []
    month = [day for day in days if day[0] >= today.replace(day=1)]
    current, best = streaks(days, today)
    return {
        "current_streak": current,
        "best_streak": best,
        "perfect_days": sum(1 for _, scheduled, completed in days if scheduled and scheduled == completed),
        "total_completed": sum(completed for _, _, completed in days),
        "completed_this_month": sum(completed for _, _, completed in month),
        "overall_rate": rate(days, today),
        "monthly_rate": rate(month, today),
    }


def routine_summary(record: RoutineRecord, today: date) -> dict:
    days = record.days(today)
    month_start = today.replace(day=1)
    current, best = streaks(days, today)
    return {
        "current_streak": current,
        "best_streak": best,
        "total_completed": sum(completed for _, _, completed in days),
        "completed_this_month": sum(completed for day, _, completed in days if day >= month_start),
        "overall_rate": rate(days, today),
    }
