"""Render the server's complete, parallel-language fortune copy as Markdown."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOCALES = ("ko", "en", "ja")
LABELS = {"love": "애정", "money": "금전", "work": "일·학업", "energy": "활력"}


def _cell(value: str) -> str:
    return value.replace("|", "&#124;").replace("\n", "<br>")


def _row(label: str, values: list[str]) -> str:
    return "| " + " | ".join(_cell(value) for value in [label, *values]) + " |"


def _header(title: str, version: str) -> list[str]:
    return [
        f"# {title}", "",
        f"> 카탈로그 버전: `{version}` · 서버 자산에서 생성한 전체 전문",
        "> 생성: `uv run python scripts/build_fortune_copy_docs.py` · 본문 수정은 서버 JSON에서 한다", "",
        "[운세 기준 문서로 돌아가기](../DAILY-FORTUNE.md)", "",
    ]


def _bundle_rows(bundles, *, overall):
    lines = ["| 구성 | 한국어 | English (US) | 日本語 |", "| --- | --- | --- | --- |"]
    if overall:
        lines.append(_row("총평", [b["headline"] for b in bundles]))
        for i in range(3):
            lines.append(_row(f"풀이 {i + 1}", [b["flow"][i] for b in bundles]))
        lines.append(_row("해볼 것", [b["do"] for b in bundles]))
        lines.append(_row("조심할 것", [b["pause"] for b in bundles]))
    else:
        for i in range(2):
            lines.append(_row(f"문장 {i + 1}", [b["text"][i] for b in bundles]))
    return lines + [""]


def render_documents() -> dict[Path, str]:
    from app.services.fortune_catalog import COPY_VERSION, load_catalog
    from app.services.fortune_copy_selection import CATEGORY_KEYS, DECILES, VARIANT_IDS

    catalog = load_catalog()
    briefs = json.loads((ROOT / "docs/fortune-content-v3/briefs.json").read_text())
    directory = ROOT / "docs/fortune-content-v3"
    documents = {}
    for decile in DECILES:
        number = int(decile[1:])
        upper = 100 if number == 90 else number + 9
        route = f"overall.{decile}.general"
        lines = _header(f"오늘의 총평 {number}–{upper}점 — 20묶음 × 3언어", COPY_VERSION)
        lines.extend([briefs["score_bands"][decile]["direction"], "",
                      "내부 계산 유형과 관계없이 이 점수 구간의 20묶음에서 선택한다", ""])
        for variant in VARIANT_IDS:
            lines.extend([f"## {variant}", ""])
            bundles = [catalog.overall_by_locale[locale][route]["variants"][variant] for locale in LOCALES]
            lines.extend(_bundle_rows(bundles, overall=True))
        documents[directory / f"overall-{decile}.md"] = "\n".join(lines)
    for category in CATEGORY_KEYS:
        lines = _header(f"{LABELS[category]} 운세 — 10점수 구간 × 20변형 × 3언어", COPY_VERSION)
        for decile in DECILES:
            number = int(decile[1:])
            upper = 100 if number == 90 else number + 9
            route = f"category.{category}.{decile}.general"
            lines.extend([f"## {number}–{upper}점 — `{route}`", ""])
            for variant in VARIANT_IDS:
                brief = briefs["categories"][category][variant]
                lines.extend([f"### {variant} · {brief['focus']}", "", f"의미 명세: {brief['context']}", ""])
                bundles = [catalog.categories_by_locale[locale][route]["variants"][variant] for locale in LOCALES]
                lines.extend(_bundle_rows(bundles, overall=False))
        documents[directory / f"category-{category}.md"] = "\n".join(lines)
    return documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if any generated document is stale")
    args = parser.parse_args()
    stale = []
    documents = render_documents()
    for path, content in documents.items():
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    obsolete = set((ROOT / "docs/fortune-content-v3").glob("overall-*.md")) - set(documents)
    stale.extend(str(p.relative_to(ROOT)) for p in sorted(obsolete))
    if stale:
        print("Fortune copy documents are stale or obsolete:\n" + "\n".join(stale))
        return 1
    print(f"Fortune copy documents {'verified' if args.check else 'written'}: {len(documents)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
