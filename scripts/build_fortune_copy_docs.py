"""Render the server's complete, parallel-language fortune copy as Markdown."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOCALES = ("ko", "en", "ja")
LABELS = {"love": "애정", "money": "금전", "work": "일·학업", "energy": "건강"}


def _cell(value: str) -> str:
    return value.replace("|", "&#124;").replace("\n", "<br>")


def _row(label: str, values: list[str]) -> str:
    return "| " + " | ".join(_cell(value) for value in [label, *values]) + " |"


def _header(title: str, version: str) -> list[str]:
    from app.services.fortune_catalog import COPY_VERSIONS
    return [
        f"# {title}", "",
        f"> 카탈로그 버전: `{version}` · 서버 자산에서 생성한 전체 전문",
        f"> 한국어: `{COPY_VERSIONS['ko']}` · 영어: `{COPY_VERSIONS['en']}` · 일본어: `{COPY_VERSIONS['ja']}`",
        "> 영어·일본어는 검수한 한국어 원고의 해석·조건·제안을 각 언어에 맞게 현지화했다. 같은 분야·카드·정역방향을 대조해 읽는다.",
        "> 생성: `uv run python scripts/build_fortune_copy_docs.py` · 본문 수정은 서버 JSON에서 한다", "",
        "[운세 기준 문서로 돌아가기](../DAILY-FORTUNE.md)", "",
    ]


def _bundle_rows(bundles, *, overall):
    lines = ["| 구성 | 한국어 | English (US) | 日本語 |", "| --- | --- | --- | --- |"]
    if overall:
        lines.append(_row("총평", [b["headline"] for b in bundles]))
        lines.append(_row("전체 풀이", [("" if locale == "ja" else " ").join(b["flow"]) for locale, b in zip(LOCALES, bundles)]))
        lines.append(_row("해볼 것", [b["do"] for b in bundles]))
        lines.append(_row("조심할 것", [b["pause"] for b in bundles]))
    else:
        lines.append(_row("분야 풀이", [("" if locale == "ja" else " ").join(b["text"]) for locale, b in zip(LOCALES, bundles)]))
    return lines + [""]


def render_documents() -> dict[Path, str]:
    from app.services.fortune_catalog import COPY_VERSION, load_catalog
    from app.services.fortune_tarot import FILENAME

    load_catalog()
    resource_dir = ROOT / "app/resources/fortune"
    copies = {
        locale: json.loads((resource_dir / filename).read_text())["readings"]
        for locale, filename in zip(LOCALES, ("copy.v3.json", "copy.v3.en.json", "copy.v3.ja.json"))
    }
    editorial = json.loads((resource_dir / FILENAME).read_text())["readings"]
    directory = ROOT / "docs/fortune-content"
    documents = {}
    for axis in ("overall", "love", "money", "work", "energy"):
        title = "오늘의 총평" if axis == "overall" else f"{LABELS[axis]} 운세"
        lines = _header(f"{title} — 78장 × 정·역방향 × 3언어", COPY_VERSION)
        lines.extend([
            "점수와 무관하게 추첨한 카드에 해당하는 완성 문단을 사용한다. 점수대별 변형이나 요청 중 문장 조합은 없다.",
            "", "아래 카드·방향·해석 근거는 내부 편집 자료이며 앱에는 운세 문구만 표시한다.", "",
        ])
        for key in sorted(k for k in editorial if k.startswith(axis + ".")):
            source = editorial[key]
            orientation = "정방향" if source["orientation"] == "upright" else "역방향"
            lines.extend([
                f"## {source['card_id']} · {orientation}", "",
                f"내부 ID: `{key}`", "",
                f"카드 의미: {source['meaning']}", "",
                f"분야 해석: {source['angle']}", "",
                "관찰: " + " / ".join(source["observations"]), "",
            ])
            lines.extend(_bundle_rows([copies[locale][key] for locale in LOCALES], overall=axis == "overall"))
        name = "overall.md" if axis == "overall" else f"category-{axis}.md"
        documents[directory / name] = "\n".join(lines)
    first = json.loads((resource_dir / "first-visit.v1.json").read_text())
    lines = _header("첫 방문 운세 — 6세트 × 3언어", first["version"])
    for entry in first["sets"]:
        lines.extend([f"## {entry['id']}", "", "내부 카드: " + json.dumps(entry["cards"], ensure_ascii=False), ""])
        bundles = [entry["copy_by_locale"][locale] for locale in LOCALES]
        lines.extend(_bundle_rows([b["overall"] for b in bundles], overall=True))
        for axis in LABELS:
            lines.extend([f"### {LABELS[axis]}", ""])
            lines.extend(_bundle_rows([b["categories"][axis] for b in bundles], overall=False))
    documents[directory / "first-visit.md"] = "\n".join(lines)
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
    obsolete = set((ROOT / "docs/fortune-content").glob("overall-*.md")) - set(documents)
    if args.check:
        stale.extend(str(p.relative_to(ROOT)) for p in sorted(obsolete))
    else:
        # Only generated score-band documents are retired; review records stay intact.
        for path in obsolete:
            if path.stem in {f"overall-d{n:02d}" for n in range(0, 100, 10)}:
                path.unlink()
            else:
                stale.append(str(path.relative_to(ROOT)))
    if stale:
        print("Fortune copy documents are stale or obsolete:\n" + "\n".join(stale))
        return 1
    print(f"Fortune copy documents {'verified' if args.check else 'written'}: {len(documents)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
