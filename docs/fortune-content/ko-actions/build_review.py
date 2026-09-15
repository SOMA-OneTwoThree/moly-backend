"""Build Korean action-copy review artifacts without changing runtime assets."""
from __future__ import annotations

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
import re
import statistics
import unicodedata

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "ko-rewrite"
PARTS = ("major-cups", "minor-actions")
REVIEW_STANDARD = "ko-actions.r2"
DIMENSIONS = {"standalone_clarity", "naturalness", "cohesion", "action_specificity", "source_fit", "pair_complementarity"}
EXAMPLES = (
    ("overall.major.sun.upright", "태양 · 정방향"),
    ("overall.major.tower.upright", "탑 · 정방향"),
    ("overall.cups.02.upright", "컵 2 · 정방향"),
    ("overall.swords.09.reversed", "소드 9 · 역방향"),
    ("overall.wands.08.reversed", "완드 8 · 역방향"),
    ("overall.pentacles.09.upright", "펜타클 9 · 정방향"),
)


def read(path):
    return json.loads(path.read_text())


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def actions(bundle):
    return {field: bundle[field] for field in ("do", "pause")}


def build():
    original = read(HERE / "source-overall.json")
    briefs = read(SOURCE / "overall.briefs.json")
    previous = read(HERE / "round-1.candidates.json")
    values, errors, pending, warnings = {}, [], [], []
    for part in PARTS:
        path = HERE / f"{part}.json"
        if not path.exists():
            pending.append(f"missing {part}")
            continue
        section = read(path)
        for key, bundle in section.items():
            if key in values:
                errors.append(f"duplicate key: {key}")
            values[key] = bundle
        author_path = HERE / f"{part}.author-review.json"
        author = read(author_path) if author_path.exists() else {}
        if (author.get("status") != "complete" or author.get("actual_read_count") != len(section)
                or author.get("criteria_version") != REVIEW_STANDARD):
            pending.append(f"incomplete author review: {part}")
        for key, bundle in section.items():
            if author.get("read_bundle_sha256", {}).get(key) != digest(actions(bundle)):
                pending.append(f"author has not read current actions: {key}")
    if set(values) != set(original):
        pending.append(f"coverage: {len(values)}/{len(original)}")
        if set(values) - set(original):
            errors.append("unknown reading keys")
    review_path = HERE / "review.json"
    review = read(review_path) if review_path.exists() else {}
    approved, repeated = 0, defaultdict(list)
    for key, bundle in values.items():
        if key not in original:
            continue
        if bundle.get("source_ko_sha256") != digest(original[key]) or bundle.get("source_brief_sha256") != digest(briefs[key]):
            errors.append(f"source changed: {key}")
        if not bundle.get("rationale"):
            errors.append(f"missing contextual rationale: {key}")
        for field, text in actions(bundle).items():
            if not isinstance(text, str) or not text or text != text.strip() or unicodedata.normalize("NFC", text) != text:
                errors.append(f"invalid text: {key}/{field}")
                continue
            if "\n" in text or "\r" in text or not text.endswith("."):
                errors.append(f"incomplete paragraph: {key}/{field}")
            if re.search(r"TODO|TBD|\{\{|타로|흐름이야|겠어|이기려고 무리하기", text):
                errors.append(f"placeholder or rejected wording: {key}/{field}")
            sentences = [s.strip() for s in re.split(r"[.!?]", text) if s.strip()]
            if len(sentences) not in (1, 2):
                warnings.append({"key": key, "field": field, "reason": "outside one or two sentences"})
            if not 35 <= len(text) <= 80:
                warnings.append({"key": key, "field": field, "reason": "outside reference length", "characters": len(text)})
            repeated[text].append(f"{key}/{field}")
        proof = review.get("entries", {}).get(key, {})
        scores = proof.get("scores", {})
        readback = proof.get("logic_readback", {})
        has_readback = all(
            isinstance(readback.get(field, {}).get(item), str) and readback[field][item].strip()
            for field in ("do", "pause") for item in ("action", "connection")
        )
        if (proof.get("status") == "pass" and proof.get("criteria_version") == REVIEW_STANDARD
                and proof.get("read_action_sha256") == digest(actions(bundle))
                and proof.get("source_ko_sha256") == digest(original[key])
                and proof.get("source_brief_sha256") == digest(briefs[key])
                and has_readback
                and set(scores) == DIMENSIONS
                and all(type(v) in (int, float) and 4 <= v <= 5 for v in scores.values())):
            approved += 1
        else:
            pending.append(f"independent review incomplete: {key}")
    duplicates = [{"text": text, "keys": keys} for text, keys in repeated.items() if len(keys) > 1]
    if duplicates:
        errors.append("identical action paragraphs")
    if (review.get("status") != "complete" or review.get("human_review") is not False
            or review.get("criteria_version") != REVIEW_STANDARD
            or review.get("actual_read_pair_count") != len(original)):
        pending.append("independent review not complete")
    if any(f.get("status") == "open" for f in review.get("findings", [])):
        pending.append("open editorial findings")
    stats = {}
    for field in ("do", "pause"):
        before = [len(b[field]) for b in original.values()]
        after = [len(b[field]) for b in values.values()]
        stats[field] = {"before_mean": round(statistics.mean(before), 1),
                        "after_mean": round(statistics.mean(after), 1) if after else None,
                        "after_min": min(after) if after else None, "after_max": max(after) if after else None}
    changed = {key: [field for field in ("do", "pause") if bundle[field] != previous[key][field]]
               for key, bundle in values.items() if key in previous}
    report = {"status": "fail" if errors else "incomplete" if pending else "pass",
              "criteria_version": REVIEW_STANDARD,
              "scope": "Korean action editorial and static proof checks; API verification is separate",
              "pairs": len(values), "fields": len(values) * 2, "independent_current_passes": approved,
              "revised_pairs": sum(bool(fields) for fields in changed.values()),
              "revised_fields": sum(len(fields) for fields in changed.values()),
              "lengths": stats, "errors": errors, "pending": pending, "warnings": warnings, "duplicates": duplicates}
    lines = ["# 한국어 해볼 것·조심할 것 전체 개선 비교", "",
             "개선 전 총운 본문과 확정한 한국어 행동 문구의 비교다. 카드 정보는 내부 검수용이다.", "",
             "[기준과 검수 결과](README.md) · [문장 기준](CRITERIA.md)", ""]
    for key, bundle in sorted(values.items()):
        if key not in original:
            continue
        old, brief = original[key], briefs[key]
        lines.extend([f"## {key}", "", f"키워드: {', '.join(brief['keywords'])}", "",
                      f"**총평:** {old['headline']}", "", *[p + "\n" for p in old["paragraphs"]],
                      "| 항목 | 기존 | 개선안 |", "| --- | --- | --- |"])
        for field, label in (("do", "해볼 것"), ("pause", "조심할 것")):
            cells = [old[field], bundle[field]]
            lines.append(f"| {label} | " + " | ".join(s.replace("|", "\\|") for s in cells) + " |")
        lines.extend(["", f"연결 근거: {bundle['rationale']}", ""])
    revisions = ["# 사용자 지적 후 한국어 행동 문구 재다듬기", "",
                 "첫 개선안과 이번 수정본이 달라진 항목을 비교한다. 수정하지 않은 항목도 새 기준으로 재독한다.", "",
                 "[판정 기준](CRITERIA.md) · [전체 본문과 현재 행동](comparison.md)", ""]
    for key, fields in sorted(changed.items()):
        if not fields:
            continue
        revisions.extend([f"## {key}", "", f"총평: {original[key]['headline']}", ""])
        for field in fields:
            label = "해볼 것" if field == "do" else "조심할 것"
            revisions.extend([f"**{label}**", "", f"첫 개선안: {previous[key][field]}", "",
                              f"재다듬은 문구: {values[key][field]}", ""])
    examples = ["# 한국어 행동 문구 비교 예시", "",
                "카드와 키워드는 내부 검토 정보다. 아래 문구는 검수한 확정 원고에서 그대로 가져왔다.", "",
                "[첫 개선안 이후 수정 내역](revision-comparison.md) · [전체 156쌍 비교](comparison.md) · [기준과 검수 결과](README.md)", ""]
    for key, label in EXAMPLES:
        if key not in values:
            continue
        old, bundle, brief = original[key], values[key], briefs[key]
        examples.extend([f"## {label}", "", f"키워드: {', '.join(brief['keywords'])}", "",
                         f"기존 총평: {old['headline']}", ""])
        for field, name in (("do", "해볼 것"), ("pause", "조심할 것")):
            examples.extend([f"**{name}**", "", f"첫 개선안: {previous[key][field]}", "", f"현재 문구: {bundle[field]}", ""])
    return {HERE / "validation.json": encode(report),
            HERE / "candidates.json": encode({k: actions(v) for k, v in sorted(values.items())}),
            HERE / "comparison.md": "\n".join(lines).rstrip() + "\n",
            HERE / "revision-comparison.md": "\n".join(revisions).rstrip() + "\n",
            HERE / "examples.md": "\n".join(examples).rstrip() + "\n"}, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    outputs, report = build()
    if args.check:
        stale = [p.name for p, data in outputs.items() if not p.exists() or p.read_text() != data]
    else:
        for path, data in outputs.items():
            path.write_text(data)
        stale = []
    print(encode({k: report[k] for k in ("status", "pairs", "fields", "independent_current_passes", "lengths")}))
    if stale:
        print("Stale artifacts: " + ", ".join(stale))
    return 0 if report["status"] == "pass" and not stale else 1


if __name__ == "__main__":
    raise SystemExit(main())
