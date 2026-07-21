"""캐피 날짜별 자기일기 CSV → moly_life_ments 업서트(멱등).

CSV 열: diary_date(YYYY-MM-DD), weather(sunny|cloudy|rainy|windy), content.
  · content 빈 행은 스킵(빈 일기 방지 · content NOT NULL). 그날은 랜덤 폴백 풀이 대신 나감.
  · 같은 diary_date 재실행 시 in-place 갱신(ON CONFLICT). is_active는 건드리지 않음
    (운영자가 꺼둔 지정본이 재실행으로 되살아나지 않게).

기본 dry-run(ROLLBACK). --commit 주면 실제 반영.
사용:
  python scripts/seed_capi_diaries.py                       # db/capi_diaries.csv dry-run
  python scripts/seed_capi_diaries.py db/capi_diaries.csv --commit
"""
import asyncio
import csv
import re
import sys
from datetime import date, datetime

import asyncpg

WEATHERS = {"sunny", "cloudy", "rainy", "windy"}

# 부분 유니크 인덱스(WHERE diary_date IS NOT NULL) 대상이라 ON CONFLICT에 술어를 포함해야 한다.
_UPSERT = """
INSERT INTO public.moly_life_ments (content, weather, diary_date)
VALUES ($1, $2, $3)
ON CONFLICT (diary_date) WHERE diary_date IS NOT NULL
DO UPDATE SET content = EXCLUDED.content, weather = EXCLUDED.weather
"""


def load_conn() -> str:
    for line in open(".env"):
        line = line.strip()
        if line.startswith("SUPABASE_DB_CONNECTION_STRING"):
            v = line.split("=", 1)[1].strip().strip('"').strip("'")
            return re.sub(r"^postgresql\+asyncpg://", "postgresql://", v)
    raise SystemExit("no conn (.env의 SUPABASE_DB_CONNECTION_STRING 없음)")


def load_rows(path: str) -> list[tuple[str, str, date]]:
    """(content, weather, diary_date) 검증된 행. content 빈 행은 스킵.

    diary_date는 asyncpg의 date 컬럼 바인딩을 위해 `date` 객체로 변환한다(문자열이면 타입 에러).
    """
    rows: list[tuple[str, str, date]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):  # 2 = 헤더 다음 줄
            content = (r.get("content") or "").strip()
            if not content:
                continue  # 아직 안 쓴 날 — 스킵
            if "�" in content:  # 깨진 문자(U+FFFD �) = CSV 인코딩 사고 — 문 앞에서 막는다
                raise SystemExit(f"{i}행: 깨진 문자(U+FFFD �) 포함 — CSV 인코딩(UTF-8) 확인 필요")
            raw = (r.get("diary_date") or "").strip()
            try:
                d = datetime.strptime(raw, "%Y-%m-%d").date()  # ISO 형식 검증 + date 변환
            except ValueError:
                raise SystemExit(f"{i}행: diary_date 형식 오류 {raw!r} (YYYY-MM-DD)")
            weather = (r.get("weather") or "sunny").strip() or "sunny"
            if weather not in WEATHERS:
                raise SystemExit(f"{i}행: weather 값 오류 {weather!r} (허용: {sorted(WEATHERS)})")
            rows.append((content, weather, d))
    return rows


async def main(commit: bool, path: str) -> None:
    rows = load_rows(path)
    if not rows:
        print("반영할 행 없음(content 채운 행이 하나도 없음).")
        return
    c = await asyncpg.connect(load_conn(), statement_cache_size=0)
    tx = c.transaction()
    await tx.start()
    try:
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
    _args = [a for a in sys.argv[1:] if a != "--commit"]
    _path = _args[0] if _args else "db/capi_diaries.csv"
    asyncio.run(main("--commit" in sys.argv, _path))
