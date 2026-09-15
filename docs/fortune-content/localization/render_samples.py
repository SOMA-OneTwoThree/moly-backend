#!/usr/bin/env python3
"""Render curated editorial comparisons verbatim; this never draws a fortune."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
AXES = ("overall", "love", "money", "work", "energy")
LABELS = dict(zip(AXES, ("총운", "애정", "금전", "학업·직장", "건강")))
SETS = {
    "sun-upright": dict.fromkeys(AXES, "major.sun.upright"),
    "tower-upright": dict.fromkeys(AXES, "major.tower.upright"),
    "mixed-major": dict(zip(AXES, ("major.moon.reversed", "major.sun.upright", "major.tower.reversed",
                                   "major.chariot.reversed", "major.temperance.reversed"))),
    "mixed-suits": dict(zip(AXES, ("cups.05.upright", "wands.04.reversed", "pentacles.09.upright",
                                   "swords.02.reversed", "major.strength.upright"))),
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def render():
    data, briefs = {}, {}
    for locale in ("ko", "en", "ja"):
        directory = HERE.parent / "ko-rewrite" if locale == "ko" else HERE / locale
        data[locale] = {}
        for axis in AXES:
            data[locale].update(json.loads((directory / f"{axis}.json").read_text()))
            if locale == "ko":
                briefs.update(json.loads((directory / f"{axis}.briefs.json").read_text()))
    lines = ["# 현지화 조합 검수용 전문", "",
             "정본에서 그대로 생성한 편집 비교 자료다. 날짜·생일로 추첨한 API 결과가 아니며 점수도 붙이지 않는다.",
             "앞의 두 세트는 같은 카드의 분야별 차이를 보기 위한 예시다. 실제 일반 추첨은 서로 다른 다섯 장을 사용한다.",
             "뒤의 두 세트는 서로 다른 카드 다섯 장을 직접 고른 조합이다. 카드·키워드·작성 조건은 검수용이며 앱 본문에 노출하지 않는다.", ""]
    proof = {"schema": "moly-localization-curated-samples-v1", "scope": "verbatim editorial rendering, not an API or runtime draw", "sets": {}}
    for name, cards in SETS.items():
        proof["sets"][name] = {}
        lines += [f"## {name}", ""]
        for locale in ("ko", "en", "ja"):
            lines += [f"### {locale}", ""]
            proof["sets"][name][locale] = {}
            for axis, card in cards.items():
                key = f"{axis}.{card}"
                bundle, brief = data[locale][key], briefs[key]
                proof["sets"][name][locale][key] = digest(bundle)
                conditions = brief["writing_conditions"]
                condition = " / ".join(conditions) if isinstance(conditions, list) else str(conditions)
                lines += [f"**{LABELS[axis]} · {card}**", "", "키워드: " + ", ".join(brief["keywords"]), "",
                          "작성 조건: " + condition, ""]
                if axis == "overall":
                    lines += [bundle["headline"], ""]
                for paragraph in bundle["paragraphs"]:
                    lines += [paragraph, ""]
                if axis == "overall":
                    lines += ["해볼 것: " + bundle["do"], "", "조심할 것: " + bundle["pause"], ""]
    return {HERE / "samples.md": "\n".join(lines),
            HERE / "samples.inputs.json": json.dumps(proof, ensure_ascii=False, indent=2) + "\n"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        outputs = render()
    except (KeyError, OSError) as exc:
        print(f"Samples not ready: {exc}")
        return 2
    for path, raw in outputs.items():
        if args.check:
            if not path.exists() or path.read_text() != raw:
                print(f"Stale sample artifact: {path.name}")
                return 1
        else:
            path.write_text(raw)
    print("Curated samples checked" if args.check else "Curated samples rendered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
