"""Render the complete, parallel-language fortune catalog as reviewable Markdown."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOCALES = ("ko", "en", "ja")
LABELS = {
    "start": "시작", "advance": "전진", "focus": "집중", "coordinate": "조율",
    "change": "변화", "organize": "정리", "recover": "회복", "balance": "균형",
    "love": "애정", "money": "금전", "work": "일·학업", "energy": "활력",
}


def _cell(value: str) -> str:
    return value.replace("|", "&#124;").replace("\n", "<br>")


def _row(label: str, values: list[str]) -> str:
    return "| " + " | ".join(_cell(value) for value in [label, *values]) + " |"


def render_documents() -> dict[Path, str]:
    from app.services.fortune_catalog import COPY_VERSION, load_catalog
    from app.services.fortune_copy_selection import CATEGORY_KEYS, FLOW_KEYS, VARIANT_IDS

    catalog = load_catalog()
    briefs = json.loads((ROOT / "docs/fortune-content-v3/briefs.json").read_text())
    documents = {}
    for section, domains, catalogs in (
        ("overall", FLOW_KEYS, catalog.overall_by_locale),
        ("category", CATEGORY_KEYS, catalog.categories_by_locale),
    ):
        for domain in domains:
            lines = [
                f"# {LABELS[domain]} 운세 — 10점수 구간 × 20변형 × 3언어",
                "",
                f"> 카탈로그 버전: `{COPY_VERSION}`. 서버 자산에서 생성한 전체 전문이다.",
                "> 생성: `uv run python scripts/build_fortune_copy_docs.py` · 본문 수정은 서버 JSON에서 한다.",
                "",
                "[운세 기준 문서로 돌아가기](../DAILY-FORTUNE.md)",
                "",
            ]
            for number in range(0, 100, 10):
                upper = 100 if number == 90 else number + 9
                route = (
                    f"overall.d{number:02d}.{domain}.default" if section == "overall"
                    else f"category.{domain}.d{number:02d}.general"
                )
                lines.extend([f"## {number}–{upper}점 — `{route}`", ""])
                for variant in VARIANT_IDS:
                    brief_section = "overall" if section == "overall" else "categories"
                    brief = briefs[brief_section][domain][variant]
                    lines.extend([
                        f"### {variant} · {brief['focus']}", "",
                        f"의미 명세: {brief['context']}", "",
                        "| 구성 | 한국어 | English (US) | 日本語 |",
                        "| --- | --- | --- | --- |",
                    ])
                    bundles = [catalogs[locale][route]["variants"][variant] for locale in LOCALES]
                    if section == "overall":
                        lines.append(_row("총평", [b["headline"] for b in bundles]))
                        for index in range(3):
                            lines.append(_row(f"흐름 {index + 1}", [b["flow"][index] for b in bundles]))
                        lines.append(_row("해볼 것", [b["do"] for b in bundles]))
                        lines.append(_row("조심할 것", [b["pause"] for b in bundles]))
                    else:
                        for index in range(2):
                            lines.append(_row(f"문장 {index + 1}", [b["text"][index] for b in bundles]))
                    lines.append("")
            documents[ROOT / f"docs/fortune-content-v3/{section}-{domain}.md"] = "\n".join(lines)
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
    if stale:
        print("Fortune copy documents are stale:\n" + "\n".join(stale))
        return 1
    print(f"Fortune copy documents {'verified' if args.check else 'written'}: {len(documents)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
