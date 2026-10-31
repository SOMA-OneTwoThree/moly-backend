"""오늘의 작은 실천 시드 — 생성(LLM)과 검토된 원고의 SQL 빌드.

    generate  카테고리별로 부족한 만큼 생성해 seed.json에 더한다. 다시 돌리면 이어서 채운다.
    reject    사람이 읽고 버린 원고를 seed.json에서 빼고 rejected.json에 사유와 함께 남긴다.
    sql       seed.json을 db/changes/daily_practice_seed.sql로 렌더링한다(--check면 일치만 검사).

원고의 원본은 content/daily_practice/seed.json이고, 운영 적용은 렌더링된 SQL을 db.apply로 한다.
LLM 호출은 사용자 데이터를 쓰지 않으며 원장 기록은 DB가 연결되어 있을 때만 남는다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402
from app.services import daily_practice_content as content  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "content/daily_practice/seed.json"
REJECTED = ROOT / "content/daily_practice/rejected.json"
SQL = ROOT / "db/changes/daily_practice_seed.sql"
_COLUMNS = ("category", "slots", "icon", "title_ko", "title_en", "title_ja",
            "speech_ko", "speech_en", "speech_ja", "source", "prompt_version", "model")
_MAX_TOKENS = 16000
_TIMEOUT_S = 240.0


def _load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


async def generate(categories: list[str], per_category: int, batch: int, rounds: int) -> None:
    seed = _load(SEED, [])
    rejected_titles = [row["title_ko"] for row in _load(REJECTED, [])]
    totals = {"input": 0, "output": 0}
    for category in categories:
        for _ in range(rounds):
            have = [row["title"]["ko"] for row in seed if row["category"] == category]
            if len(have) >= per_category:
                break
            result = await content.generate(
                category,
                batch,
                existing_titles=[row["title"]["ko"] for row in seed] + rejected_titles,
                avoid_in_prompt=have + rejected_titles,
                purpose="daily_practice_seed",
                max_tokens=_MAX_TOKENS,
                timeout=_TIMEOUT_S,
            )
            totals["input"] += result.input_tokens
            totals["output"] += result.output_tokens
            seed += [
                {
                    "category": action.category,
                    "slots": list(action.slots),
                    "icon": action.icon,
                    "title": action.title,
                    "speech": action.speech,
                    "source": "seed",
                    "prompt_version": content.PROMPT_VERSION,
                    "model": settings.model_daily_practice,
                }
                for action in result.accepted
            ]
            _write(SEED, seed)
            print(f"{category}: +{len(result.accepted)} rejected {len(result.rejected)} "
                  f"total {sum(1 for row in seed if row['category'] == category)}")
            for title, reasons in result.rejected:
                print(f"  rejected {title!r}: {','.join(reasons)}")
    print(f"tokens input={totals['input']} output={totals['output']}")


def reject(reviews_path: Path) -> None:
    """reviews_path: [{"title_ko": ..., "reason": ...}]"""
    reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
    reasons = {row["title_ko"]: row["reason"] for row in reviews}
    seed = _load(SEED, [])
    missing = set(reasons) - {row["title"]["ko"] for row in seed}
    if missing:
        raise SystemExit(f"not in seed: {sorted(missing)}")
    log = _load(REJECTED, [])
    log += [{"title_ko": row["title"]["ko"], "category": row["category"],
             "reason": reasons[row["title"]["ko"]], "action": row}
            for row in seed if row["title"]["ko"] in reasons]
    _write(REJECTED, log)
    _write(SEED, [row for row in seed if row["title"]["ko"] not in reasons])


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_sql(seed: list[dict]) -> str:
    rows = []
    for row in sorted(seed, key=lambda r: (content.CATEGORIES.index(r["category"]), r["title"]["ko"])):
        slots = "ARRAY[" + ", ".join(_quote(s) for s in row["slots"]) + "]::text[]"
        values = [_quote(row["category"]), slots, _quote(row["icon"])]
        values += [_quote(row["title"][k]) for k in content.LANGUAGES]
        values += [_quote(row["speech"][k]) for k in content.LANGUAGES]
        values += [_quote(row["source"]), _quote(row["prompt_version"]), _quote(row["model"])]
        rows.append("  (" + ", ".join(values) + ")")
    return (
        "-- 오늘의 작은 실천 시드. content/daily_practice/seed.json에서 scripts/daily_practice_seed.py sql로\n"
        "-- 만든 파일이다. 직접 고치지 않는다. db/changes/daily_practice.sql을 적용한 뒤 db.apply로 적용한다.\n"
        f"-- 멱등이다. 이미 있는 제목은 건드리지 않으므로 운영에서 retired로 바꾼 행도 되살아나지 않는다. 원고 {len(rows)}개.\n"
        "SET LOCAL lock_timeout = '2s';\n"
        "SET LOCAL statement_timeout = '60s';\n"
        f"INSERT INTO public.daily_practice_actions ({', '.join(_COLUMNS)})\nVALUES\n"
        + ",\n".join(rows)
        + "\nON CONFLICT (title_ko) DO NOTHING;\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate")
    gen.add_argument("--category", action="append", choices=content.CATEGORIES)
    gen.add_argument("--per-category", type=int, default=30)
    gen.add_argument("--batch", type=int, default=14)
    gen.add_argument("--rounds", type=int, default=6)
    rej = commands.add_parser("reject")
    rej.add_argument("reviews", type=Path)
    sql = commands.add_parser("sql")
    sql.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.command == "generate":
        logging.getLogger("moly-backend").setLevel(logging.ERROR)
        asyncio.run(generate(args.category or list(content.CATEGORIES), args.per_category,
                             args.batch, args.rounds))
    elif args.command == "reject":
        reject(args.reviews)
    else:
        seed = _load(SEED, [])
        if not seed:
            raise SystemExit("seed.json is empty: run the generate command first")
        rendered = render_sql(seed)
        if args.check:
            if SQL.read_text(encoding="utf-8") != rendered:
                raise SystemExit("daily_practice_seed.sql is stale: run this script with sql")
            print("daily_practice_seed.sql is current")
        else:
            SQL.write_text(rendered, encoding="utf-8")
            print(f"written: {SQL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
