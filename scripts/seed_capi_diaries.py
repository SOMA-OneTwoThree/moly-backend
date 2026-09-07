"""캐피 CSV 등록: 날짜별 업서트 또는 주간 원고의 불변·append 등록.

legacy: diary_date,weather,content (빈 본문 스킵, 날짜별 내용 갱신).
weekly: week_start_date,sequence_no,weather,content (빈 본문 거부).
주간 동일 슬롯 재입력은 no-op이며 회수된 원고도 재활성화하지 않는다.
기본 dry-run(전체 ROLLBACK). --commit과 기존 --env 선택으로 실반영.
"""
import asyncio
import csv
import sys
from dataclasses import dataclass
from datetime import date

import asyncpg

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from app.services.text_clean import strip_symbols
from db.envfile import announce, is_prod, load_conn, split_env_arg

WEATHERS = {"sunny", "cloudy", "rainy", "windy"}
LEGACY_FIELDS = {"diary_date", "weather", "content"}
WEEKLY_FIELDS = {"week_start_date", "sequence_no", "weather", "content"}
_UPSERT = """
INSERT INTO public.moly_life_ments (content, weather, diary_date)
VALUES ($1, $2, $3)
ON CONFLICT (diary_date) WHERE diary_date IS NOT NULL
DO UPDATE SET content = EXCLUDED.content, weather = EXCLUDED.weather
"""
# Two-int advisory locks have a separate key space from the user UUID bigint locks.
_WEEK_LOCK = "SELECT pg_advisory_xact_lock(1297042521, $1::integer)"
_WEEK_ROWS = """
SELECT sequence_no, content, weather FROM public.moly_life_ments
WHERE week_start_date = $1
"""  # Includes inactive rows: withdrawal must never reopen an earlier slot.
_WEEK_INSERT = """
INSERT INTO public.moly_life_ments (content, weather, week_start_date, sequence_no)
VALUES ($1, $2, $3, $4)
"""


@dataclass(frozen=True)
class WeeklyRow:
    content: str
    weather: str
    week_start_date: date
    sequence_no: int


def _date(raw: str, field: str, line: int) -> date:
    try:
        value = date.fromisoformat(raw)
        if value.isoformat() != raw:
            raise ValueError
        return value
    except ValueError:
        raise SystemExit(f"{line}행: {field} 형식 오류 {raw!r} (YYYY-MM-DD)") from None


def load_rows(path: str) -> list[tuple[str, str, date]] | list[WeeklyRow]:
    """헤더로 형식을 결정하고 파일 전체를 검증. legacy 반환 형식은 유지한다."""
    rows = []
    seen = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or set(fields) not in (LEGACY_FIELDS, WEEKLY_FIELDS):
            raise SystemExit("CSV 헤더 오류: 날짜형 또는 주간형 열을 정확히 사용하세요(혼합 불가).")
        weekly = set(fields) == WEEKLY_FIELDS
        for i, r in enumerate(reader, start=2):
            if None in r or any(v is None for v in r.values()):
                raise SystemExit(f"{i}행: CSV 열 수 오류")
            content = r["content"].strip()
            if not content and not weekly:
                continue
            if "�" in content:
                raise SystemExit(f"{i}행: 깨진 문자(U+FFFD �) 포함 — CSV 인코딩(UTF-8) 확인 필요")
            if weekly and not strip_symbols(content).strip():
                raise SystemExit(f"{i}행: content가 비어 있거나 정제 후 빈 본문입니다.")
            weather = r["weather"].strip() or "sunny"
            if weather not in WEATHERS:
                raise SystemExit(f"{i}행: weather 값 오류 {weather!r} (허용: {sorted(WEATHERS)})")
            if not weekly:
                rows.append((content, weather, _date(r["diary_date"].strip(), "diary_date", i)))
                continue
            week = _date(r["week_start_date"].strip(), "week_start_date", i)
            if week.weekday() != 0:
                raise SystemExit(f"{i}행: week_start_date는 월요일이어야 합니다.")
            raw_sequence = r["sequence_no"].strip()
            if not raw_sequence.isascii() or not raw_sequence.isdecimal():
                raise SystemExit(f"{i}행: sequence_no는 양의 정수여야 합니다.")
            sequence = int(raw_sequence)
            if not 1 <= sequence <= 2147483647:
                raise SystemExit(f"{i}행: sequence_no는 양의 PostgreSQL integer여야 합니다.")
            slot = (week, sequence)
            if slot in seen:
                raise SystemExit(f"{i}행: 주간 순번 중복 {week}/{sequence}")
            seen.add(slot)
            rows.append(WeeklyRow(content, weather, week, sequence))
    return rows


async def insert_weekly_rows(c, rows: list[WeeklyRow]) -> tuple[int, int]:
    """호출자의 단일 transaction 안에서 모든 주 잠금→검증→삽입. (추가, no-op)."""
    weeks = sorted({r.week_start_date for r in rows})
    for week in weeks:
        await c.execute(_WEEK_LOCK, week.toordinal())
    inserts = []
    unchanged = 0
    for week in weeks:
        existing = {r["sequence_no"]: r for r in await c.fetch(_WEEK_ROWS, week)}
        maximum = max(existing, default=0)
        for row in sorted((r for r in rows if r.week_start_date == week), key=lambda r: r.sequence_no):
            old = existing.get(row.sequence_no)
            if old is not None:
                if old["content"] != row.content or old["weather"] != row.weather:
                    raise ValueError(f"주간 원고 수정 불가: {week}/{row.sequence_no}")
                unchanged += 1
                continue
            if row.sequence_no <= maximum:
                raise ValueError(f"주간 원고는 기존 최대 순번 {maximum} 뒤에만 추가 가능: {week}/{row.sequence_no}")
            inserts.append((row.content, row.weather, week, row.sequence_no))
    if inserts:
        await c.executemany(_WEEK_INSERT, inserts)
    return len(inserts), unchanged


async def main(commit: bool, path: str, env: str | None = None) -> None:
    dsn = load_conn(env)
    announce(env, dsn, commit=commit)
    rows = load_rows(path)
    if not rows:
        print("반영할 행 없음(content 채운 행이 하나도 없음).")
        return
    if commit and is_prod(env):
        print(">>> PROD 실반영을 시작합니다.", file=sys.stderr)
    c = await asyncpg.connect(dsn, statement_cache_size=0)
    tx = c.transaction()
    try:
        await tx.start()
        if isinstance(rows[0], WeeklyRow):
            added, unchanged = await insert_weekly_rows(c, rows)
            print(f"주간 원고 추가 {added}건, 동일 원고 no-op {unchanged}건")
        else:
            await c.executemany(_UPSERT, rows)
            print(f"업서트 {len(rows)}건: {', '.join(d.isoformat() for *_, d in rows)}")
        if commit:
            await tx.commit()
            print(">>> COMMIT 완료 — 실 DB 반영됨.")
        else:
            await tx.rollback()
            print(">>> DRY-RUN — ROLLBACK 완료(반영 안 됨). --commit 주면 실제 적용.")
    except Exception as e:
        await tx.rollback()
        print(f"!!! 실패 — ROLLBACK: {type(e).__name__}: {e}")
        raise
    finally:
        await c.close()


if __name__ == "__main__":
    _env, _rest = split_env_arg(sys.argv[1:])
    _args = [a for a in _rest if a != "--commit"]
    _path = _args[0] if _args else "db/capi_diaries.csv"
    asyncio.run(main("--commit" in _rest, _path, _env))
