#!/usr/bin/env python3
"""Render the Korean manuscript; verify coverage and actual readers' records.

Standard library only. Does not import the app, modify runtime assets, infer a
language review, or refresh review hashes. --check checks generated files too.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "ko-rewrite"
AXES = ("overall", "love", "money", "work", "energy")
LABELS = dict(zip(AXES, ("총운", "애정", "금전", "학업·직장", "건강")))
FIELDS = ("headline", "do", "pause")
CRITERIA = "ko-plain-voice.1"
BANNED = ("경험자", "흐름이야", "반가운 변화", "있겠네", "겠어", "해결할 여지", "기대할 만한", "이기려고 무리하기")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{path}: duplicate key {key}")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def validate_review(axis, bundles, kind, errors):
    path = ROOT / f"{axis}.{kind}-review.json"
    if not path.exists():
        errors.append(f"{axis}: missing {kind} review")
        return None
    proof = read(path)
    if (proof.get("status") != "complete" or proof.get("criteria") != CRITERIA
            or proof.get("review_kind") != kind or not proof.get("reviewer")
            or proof.get("actual_read_count") != len(bundles)):
        errors.append(f"{axis}: incomplete {kind} review metadata")
    records = proof.get("readings", {})
    if records.keys() != bundles.keys():
        errors.append(f"{axis}: incomplete {kind} review keys")
    for key, bundle in bundles.items():
        record = records.get(key, {})
        if record.get("read_bundle_sha256") != digest(bundle):
            errors.append(f"{key}: stale/missing {kind} read")
        if not str(record.get("judgment", "")).strip():
            errors.append(f"{key}: missing {kind} judgment")
        if kind == "independent" and record.get("status") != "pass":
            errors.append(f"{key}: {kind} review did not pass")
    return proof.get("reviewer")


def bundle_lines(bundle):
    lines = []
    if "headline" in bundle:
        lines.extend([f"**{bundle['headline']}**", ""])
    for paragraph in bundle["paragraphs"]:
        lines.extend([paragraph, ""])
    if "do" in bundle:
        lines.extend([f"- 해볼 것: {bundle['do']}", f"- 조심할 것: {bundle['pause']}", ""])
    return lines


def build():
    errors, flags, summary, data, outputs = [], [], {}, {}, {}
    for axis in AXES:
        source = read(SOURCE / f"{axis}.json")
        briefs = read(SOURCE / f"{axis}.briefs.json")
        path = ROOT / f"{axis}.json"
        if not path.exists():
            errors.append(f"{axis}: missing manuscript")
            continue
        bundles = read(path)
        data[axis] = bundles
        if bundles.keys() != source.keys():
            errors.append(f"{axis}: key coverage differs from source")
        field_changes = Counter()
        body_lengths = []
        seen_fields = {field: Counter() for field in FIELDS}
        changed = 0
        lines = [f"# {LABELS[axis]} 전문", "", "현재 한국어 원고 전문. 서비스 자산은 이 원고와 검수 기록에서 생성한다.", ""]
        for key, bundle in bundles.items():
            if key not in source:
                continue
            old = source[key]
            body_length = len(" ".join(bundle.get("paragraphs", [])))
            body_lengths.append(body_length)
            if not 110 <= body_length <= 140:
                flags.append({"key": key, "field": "paragraphs", "kind": "body_length_reference", "characters": body_length})
            if bundle.keys() != old.keys():
                errors.append(f"{key}: field shape changed")
            if len(bundle.get("paragraphs", [])) != len(old["paragraphs"]):
                errors.append(f"{key}: paragraph count changed")
            strings = list(bundle.get("paragraphs", [])) + [bundle[f] for f in FIELDS if f in bundle]
            for text in strings:
                if not isinstance(text, str) or not text.strip() or text != text.strip() or "\n" in text:
                    errors.append(f"{key}: invalid text")
                    continue
                for phrase in BANNED:
                    if phrase in text:
                        errors.append(f"{key}: rejected phrase {phrase}")
            changed += bundle != old
            for field in bundle:
                field_changes[field] += bundle[field] != old.get(field)
            for field in FIELDS:
                if field not in bundle:
                    continue
                text = bundle[field]
                seen_fields[field][text] += 1
                if len(text) > (42 if field == "headline" else 34):
                    flags.append({"key": key, "field": field, "kind": "length_reference", "text": text})
            if "pause" in bundle and "하지 않기" in bundle["pause"]:
                errors.append(f"{key}: pause reverses the action")
            keywords = ", ".join(briefs[key]["keywords"])
            lines.extend([f"## {key}", "", f"카드 키워드: {keywords}", "",
                          "작성 조건: 카드의 뜻을 유지하고 쉬운 일상어로 운과 행동을 바로 말한다. "
                          "불필요한 상황·이유를 덧붙이지 않는다. [전체 기준](../CRITERIA.md)", "",
                          *bundle_lines(bundle)])
        reviewers = {kind: validate_review(axis, bundles, kind, errors) for kind in ("author", "independent")}
        if reviewers["author"] and reviewers["author"] == reviewers["independent"]:
            errors.append(f"{axis}: author also claimed independent review")
        summary[axis] = {"bundles": len(bundles), "changed_bundles": changed,
                         "changed_fields": dict(field_changes), "source_sha256": digest(source),
                         "manuscript_sha256": digest(bundles), "reviewers": reviewers}
        if body_lengths:
            summary[axis]["body_characters_including_spaces"] = {
                "min": min(body_lengths), "median": statistics.median(body_lengths), "max": max(body_lengths)}
        for field, counts in seen_fields.items():
            for text, count in counts.items():
                if count > 1:
                    flags.append({"axis": axis, "field": field, "kind": "duplicate_reference", "count": count, "text": text})
        outputs[ROOT / "generated" / f"{axis}.md"] = "\n".join(lines).rstrip() + "\n"
    if "overall" in data:
        old = read(SOURCE / "overall.json")
        lines = ["# 총평·행동 문구 전체 비교", "", "v8-localized.2 한국어 원고와 후속 교정 원고의 비교다.", "",
                 "| 카드 | 필드 | 기존 | 새 원고 |", "| --- | --- | --- | --- |"]
        for key, bundle in data["overall"].items():
            if key not in old:
                continue
            for field in FIELDS:
                before, after = old[key][field].replace("|", "\\|"), bundle[field].replace("|", "\\|")
                lines.append(f"| {key} | {field} | {before} | {after} |")
        outputs[ROOT / "generated" / "short-comparison.md"] = "\n".join(lines).rstrip() + "\n"
    lines = ["# 같은 카드 한 장의 전체 예시", "", "한국어 문구 검토용이다. 점수나 실제 서비스 추첨 결과를 뜻하지 않는다.", ""]
    for card, label in (("major.sun.upright", "태양 정방향"),
                        ("pentacles.03.reversed", "펜타클 3 역방향"),
                        ("major.tower.upright", "탑 정방향")):
        lines.extend([f"## {label}", "", f"내부 카드: `{card}`", "",
                      "작성 조건: 카드 뜻을 유지하며 쉬운 말로 운을 직접 설명한다. "
                      "각 분야 전체 4~5줄을 목표로 하되 글자 수를 채우기 위한 상황은 덧붙이지 않는다.", ""])
        for axis in AXES:
            bundle = data.get(axis, {}).get(f"{axis}.{card}")
            if bundle:
                brief = read(SOURCE / f"{axis}.briefs.json")[f"{axis}.{card}"]
                lines.extend([f"### {LABELS[axis]}", "", f"카드 키워드: {', '.join(brief['keywords'])}", "",
                              *bundle_lines(bundle)])
    outputs[ROOT / "generated" / "examples.md"] = "\n".join(lines).rstrip() + "\n"
    report = {"criteria": CRITERIA, "scope": "korean-manuscript-only", "static_errors": errors,
              "reference_flags": flags, "axes": summary,
              "human_review": False,
              "note": "Hash checks verify recorded reads, not language quality. Reference flags require editorial judgment."}
    outputs[ROOT / "generated" / "validation.json"] = encode(report)
    return outputs, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    outputs, report = build()
    stale = []
    for path, expected in outputs.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != expected:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")
    print(encode({"bundles": sum(a["bundles"] for a in report["axes"].values()),
                  "errors": len(report["static_errors"]), "reference_flags": len(report["reference_flags"]),
                  "stale_outputs": stale}).strip())
    return 1 if report["static_errors"] or stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
