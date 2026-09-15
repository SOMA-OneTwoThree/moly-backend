#!/usr/bin/env python3
"""Render editorial JSON verbatim and check its structure and read-review hashes.

Uses only the Python standard library. This script never imports the application,
rewrites canonical copy, or updates review.json. Static success is not a language
quality certificate. Run normally to write generated/; use --check for a read-only
check of both current inputs and the generated artifacts.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
AXES = ("overall", "love", "money", "work", "energy")
LABELS = dict(zip(AXES, ("총운", "애정", "금전", "학업·직장", "건강")))
MAJORS = {
    "fool": "바보", "magician": "마법사", "high_priestess": "여사제",
    "empress": "여제", "emperor": "황제", "hierophant": "교황",
    "lovers": "연인", "chariot": "전차", "strength": "힘",
    "hermit": "은둔자", "wheel_of_fortune": "운명의 수레바퀴",
    "justice": "정의", "hanged_man": "매달린 사람", "death": "죽음",
    "temperance": "절제", "devil": "악마", "tower": "탑", "star": "별",
    "moon": "달", "sun": "태양", "judgement": "심판", "world": "세계",
}
SUITS = {"wands": "완드", "cups": "컵", "swords": "소드", "pentacles": "펜타클"}
CARDS = tuple("major." + card for card in MAJORS) + tuple(
    f"{suit}.{number:02d}" for suit in SUITS for number in range(1, 15)
)
ORIENTATIONS = {"upright": "정방향", "reversed": "역방향"}
EXPECTED = {
    axis: tuple(f"{axis}.{card}.{orientation}" for card in CARDS for orientation in ORIENTATIONS)
    for axis in AXES
}
PARAGRAPH_COUNTS = {"overall": {2}, "love": {3}, "money": {1, 2}, "work": {2}, "energy": {1, 2}}
BANNED = (
    "반가운 변화", "있겠네", "용기가 생길 수 있어", "해결할 여지",
    "가치를 알아볼수록", "적극적으로 나설수록 성과가 나는 날",
    "기대할 만한 수입", "이기려고 무리하기", "겠어", "흐름이야",
)
PRIOR_REJECTED = ("큰 약속", "큰약속", "안정되게", "생활의 일")
BRIEF_REQUIRED = {"card_id", "orientation", "keywords", "fortune_judgment", "writing_conditions", "source_ids", "application_note"}
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
AUTHOR_COMPLETE_STATUSES = {"self_reviewed", "complete"}


def digest(bundle: Any) -> str:
    data = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def string_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(nonempty(item) for item in value)


def valid_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def card_label(card: str) -> str:
    suit, rank = card.split(".")
    if suit == "major":
        return MAJORS[rank]
    ranks = {1: "에이스", 11: "시종", 12: "기사", 13: "여왕", 14: "왕"}
    return f"{SUITS[suit]} {ranks.get(int(rank), str(int(rank)))}"


class Audit:
    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.input_hashes: dict[str, str] = {}

    def issue(self, level: str, code: str, **details: Any) -> None:
        getattr(self, level).append({"code": code, **details})

    def read(self, filename: str) -> dict[str, Any]:
        path = ROOT / filename
        if not path.is_file():
            self.issue("pending", "missing_file", file=filename)
            return {}
        raw = path.read_bytes()
        self.input_hashes[filename] = hashlib.sha256(raw).hexdigest()
        duplicate_keys: list[str] = []

        def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    duplicate_keys.append(key)
                result[key] = value
            return result

        try:
            value = json.loads(raw, object_pairs_hook=pairs_hook)
        except (UnicodeError, json.JSONDecodeError) as exc:
            self.issue("errors", "invalid_json", file=filename, detail=str(exc))
            return {}
        if duplicate_keys:
            self.issue("errors", "duplicate_json_keys", file=filename, keys=duplicate_keys)
        if not isinstance(value, dict):
            self.issue("errors", "top_level_not_object", file=filename)
            return {}
        return value

    def keys(self, actual: dict[str, Any], expected: tuple[str, ...], file: str) -> None:
        missing, unknown = sorted(set(expected) - actual.keys()), sorted(actual.keys() - set(expected))
        if missing:
            self.issue("pending", "missing_keys", file=file, keys=missing)
        if unknown:
            self.issue("errors", "unknown_keys", file=file, keys=unknown)


def author_hashes(author: dict[str, Any], audit: Audit, axis: str) -> dict[str, Any]:
    """Normalize the three explicitly used author-review schemas without mutating them."""
    if not author:
        return {}
    if isinstance(author.get("read_bundle_sha256"), dict):
        hashes = author["read_bundle_sha256"]
    elif isinstance(author.get("bundle_hashes"), dict):
        hashes = author["bundle_hashes"]
        reviewed = author.get("reviewed_keys")
        if not isinstance(reviewed, list) or any(not isinstance(key, str) for key in reviewed):
            audit.issue("errors", "author_reviewed_keys_format", axis=axis)
        elif set(reviewed) != set(hashes) or len(reviewed) != len(set(reviewed)):
            audit.issue("errors", "author_reviewed_keys_mismatch", axis=axis)
    elif isinstance(author.get("reviewed_bundles"), dict):
        hashes = {}
        for key, record in author["reviewed_bundles"].items():
            if not isinstance(record, dict) or not string_list(record.get("read_scope")) or not nonempty(record.get("judgment")):
                audit.issue("errors", "author_bundle_record_format", key=key)
            hashes[key] = record.get("sha256") if isinstance(record, dict) else None
    else:
        audit.issue("errors", "unknown_author_review_schema", axis=axis)
        return {}
    status = author.get("status")
    if not nonempty(status):
        audit.issue("errors", "author_status_format", axis=axis)
    elif status not in AUTHOR_COMPLETE_STATUSES:
        audit.issue("pending", "author_review_incomplete", axis=axis, status=status)
    if author.get("independent_review") is True or author.get("human_review") is True:
        audit.issue("errors", "author_claims_independent_or_human_review", axis=axis)
    count = author.get("actual_self_read_count")
    if count is not None and (type(count) is not int or count != len(hashes)):
        audit.issue("errors", "author_read_count_mismatch", axis=axis, declared=count, hashes=len(hashes))
    return hashes


def sources_for(key: str, brief: dict[str, Any], source_briefs: dict[str, Any]) -> list[dict[str, Any]]:
    fallback = source_briefs.get(key, {})
    by_id: dict[str, dict[str, Any]] = {}
    for group in (fallback.get("sources", []) if isinstance(fallback, dict) else [], brief.get("sources", [])):
        if isinstance(group, list):
            for source in group:
                if isinstance(source, dict) and nonempty(source.get("id")):
                    by_id[source["id"]] = source
    ids = brief.get("source_ids", [])
    return [by_id.get(source_id, {"id": source_id}) for source_id in ids if isinstance(source_id, str)] if isinstance(ids, list) else []


def duplicate_rows(index: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [{"text": text, "occurrences": occurrences} for text, occurrences in sorted(index.items()) if len(occurrences) > 1]


def validate(audit: Audit, data: dict[str, Any], briefs: dict[str, Any], authors: dict[str, Any], review: dict[str, Any], source_briefs: dict[str, Any]) -> dict[str, Any]:
    bodies: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sentences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    short_fields = {field: defaultdict(list) for field in ("headline", "do", "pause")}
    counts = {}
    audit.keys(source_briefs, tuple(key for axis in AXES for key in EXPECTED[axis]), "source-briefs.json")
    root_axes = review.get("axes", {})
    if not isinstance(root_axes, dict):
        audit.issue("errors", "root_axes_format")
        root_axes = {}
    if set(root_axes) - set(AXES):
        audit.issue("errors", "unknown_root_axes", axes=sorted(set(root_axes) - set(AXES)))
    if review and (review.get("reviewer") != "root" or review.get("human_review") is not False):
        audit.issue("errors", "root_review_metadata")
    root_status = review.get("status")
    if review and not nonempty(root_status):
        audit.issue("errors", "root_status_format", status=root_status)
    elif root_status not in {"pass", "complete", "completed", "approved"}:
        audit.issue("pending", "root_review_not_final", status=root_status)
    findings = review.get("findings", [])
    if not isinstance(findings, list):
        audit.issue("errors", "root_findings_format")
    else:
        for number, finding in enumerate(findings):
            if not isinstance(finding, dict) or not nonempty(finding.get("status")):
                audit.issue("errors", "root_finding_status_format", finding=number)
            elif finding["status"] == "open":
                audit.issue("pending", "root_finding_open", finding=number, key=finding.get("key"), issue=finding.get("issue"))
            elif finding["status"] not in {"resolved", "closed", "accepted", "waived", "superseded"}:
                audit.issue("pending", "root_finding_unknown_status", finding=number, status=finding["status"])
    for axis in AXES:
        axis_data, axis_briefs = data[axis], briefs[axis]
        read_hashes = author_hashes(authors[axis], audit, axis)
        root_records = root_axes.get(axis, {})
        if not isinstance(root_records, dict):
            audit.issue("errors", "root_axis_records_format", axis=axis)
            root_records = {}
        for actual, filename in ((axis_data, f"{axis}.json"), (axis_briefs, f"{axis}.briefs.json"), (read_hashes, f"{axis}.author-review.json"), (root_records, f"review.json:{axis}")):
            audit.keys(actual, EXPECTED[axis], filename)
        current_pass = 0
        for key in EXPECTED[axis]:
            if key not in axis_data:
                continue
            bundle = axis_data[key]
            if not isinstance(bundle, dict):
                audit.issue("errors", "bundle_not_object", key=key)
                continue
            allowed = {"paragraphs", "headline", "do", "pause"} if axis == "overall" else {"paragraphs"}
            if set(bundle) != allowed:
                audit.issue("errors", "bundle_fields", key=key, actual=sorted(bundle), expected=sorted(allowed))
            paragraphs = bundle.get("paragraphs")
            if not isinstance(paragraphs, list) or len(paragraphs) not in PARAGRAPH_COUNTS[axis]:
                audit.issue("errors", "paragraph_count", key=key, expected=sorted(PARAGRAPH_COUNTS[axis]))
            if isinstance(paragraphs, list):
                if all(isinstance(paragraph, str) for paragraph in paragraphs):
                    bodies["\n\n".join(paragraphs)].append({"key": key})
                for number, paragraph in enumerate(paragraphs, 1):
                    if not nonempty(paragraph) or "\n" in paragraph or "\r" in paragraph or not paragraph.endswith("."):
                        audit.issue("errors", "paragraph_format", key=key, paragraph=number)
                        continue
                    if re.match(r"^(총운|애정|금전|학업|직장|건강|솔로|연애 중|이별 후)\s*[:：]", paragraph):
                        audit.issue("errors", "inline_category_heading", key=key, paragraph=number)
                    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
                        sentences[sentence].append({"key": key, "paragraph": number})
            if axis == "overall":
                for field, index in short_fields.items():
                    value = bundle.get(field)
                    if not nonempty(value) or "\n" in value or "\r" in value:
                        audit.issue("errors", "short_field_format", key=key, field=field)
                        continue
                    index[value].append({"key": key})
                    if len(value) > 60:
                        audit.issue("warnings", "long_short_field", key=key, field=field, length=len(value))
                    if field == "headline" and (not value.endswith(".") or len(re.split(r"(?<=[.!?])\s+", value)) != 1):
                        audit.issue("errors", "headline_sentence_format", key=key)
                    if field == "pause" and value.endswith(("않기", "말기")):
                        audit.issue("errors", "pause_negative_action", key=key, value=value)
            texts = [value for value in bundle.values() if isinstance(value, str)]
            if isinstance(paragraphs, list):
                texts += [value for value in paragraphs if isinstance(value, str)]
            for phrase in BANNED:
                if any(phrase in text for text in texts):
                    audit.issue("errors", "banned_phrase", key=key, phrase=phrase)
            for phrase in PRIOR_REJECTED:
                if any(phrase in text for text in texts):
                    audit.issue("warnings", "previously_rejected_phrase", key=key, phrase=phrase)
            brief = axis_briefs.get(key)
            if brief is not None:
                if not isinstance(brief, dict):
                    audit.issue("errors", "brief_not_object", key=key)
                else:
                    missing = sorted(BRIEF_REQUIRED - brief.keys())
                    if missing:
                        audit.issue("errors", "brief_missing_fields", key=key, fields=missing)
                    suffix = key[len(axis) + 1:]
                    card, orientation = suffix.rsplit(".", 1)
                    if brief.get("card_id") != card or brief.get("orientation") != orientation:
                        audit.issue("errors", "brief_card_orientation_mismatch", key=key)
                    for field in ("keywords", "writing_conditions", "source_ids"):
                        if not string_list(brief.get(field)):
                            audit.issue("errors", "brief_list_format", key=key, field=field)
                    for field in ("fortune_judgment", "application_note"):
                        if not nonempty(brief.get(field)):
                            audit.issue("errors", "brief_text_format", key=key, field=field)
                    for source in sources_for(key, brief, source_briefs):
                        url = source.get("url")
                        if not valid_url(url):
                            audit.issue("errors", "unresolved_source_id", key=key, source_id=source.get("id"))
            current_hash = digest(bundle)
            if key in read_hashes:
                actual_hash = read_hashes[key]
                if not isinstance(actual_hash, str) or not HEX64.fullmatch(actual_hash):
                    audit.issue("errors", "author_hash_format", key=key)
                elif actual_hash != current_hash:
                    audit.issue("pending", "author_hash_stale", key=key, read_sha256=actual_hash, current_sha256=current_hash)
            if key in root_records:
                record = root_records[key]
                if not isinstance(record, dict) or not nonempty(record.get("assessment")):
                    audit.issue("errors", "root_record_format", key=key)
                    continue
                if not nonempty(record.get("status")):
                    audit.issue("errors", "root_record_status_format", key=key, status=record.get("status"))
                    continue
                read_hash = record.get("read_bundle_sha256")
                if not isinstance(read_hash, str) or not HEX64.fullmatch(read_hash):
                    audit.issue("errors", "root_hash_format", key=key)
                elif read_hash != current_hash:
                    audit.issue("pending", "root_hash_stale", key=key, read_sha256=read_hash, current_sha256=current_hash)
                elif record.get("status") == "pass":
                    current_pass += 1
                if record.get("status") != "pass":
                    rejected_current = read_hash == current_hash and record.get("status") in {"fail", "rejected"}
                    audit.issue("errors" if rejected_current else "pending", "root_record_not_pass", key=key, status=record.get("status"))
        counts[axis] = {"expected": 156, "copy": len(axis_data), "briefs": len(axis_briefs), "author_hashes": len(read_hashes), "root_current_pass": current_pass}
    duplicate_bodies = duplicate_rows(bodies)
    if duplicate_bodies:
        audit.issue("errors", "identical_full_bodies", groups=len(duplicate_bodies))
    return {
        "schema": "moly-ko-editorial-static-validation-v1",
        "status": "fail" if audit.errors else "pending" if audit.pending else "pass",
        "scope": "Editorial files only; no app imports, API, DB, deployment, or language-quality certification.",
        "static_success_is_not_editorial_approval": True,
        "expected_total": 780, "actual_total": sum(len(data[axis]) for axis in AXES),
        "expected_overall_short_fields": 468,
        "actual_overall_short_fields": sum(sum(field in bundle for field in short_fields) for bundle in data["overall"].values() if isinstance(bundle, dict)),
        "expected_cards": 78, "expected_orientations_per_card": 2,
        "axes": counts,
        "errors": audit.errors, "pending": audit.pending, "warnings": audit.warnings,
        "duplicate_full_bodies": duplicate_bodies,
        "duplicate_sentences": duplicate_rows(sentences),
        "repeated_short_fields": {field: duplicate_rows(index) for field, index in short_fields.items()},
        "input_file_sha256": dict(sorted(audit.input_hashes.items())),
        "bundle_hash_convention": "sha256(UTF-8 JSON, ensure_ascii=False, sort_keys=True, separators=(',', ':'))",
    }


def render_entry(key: str, bundle: Any, brief: Any, sources: dict[str, Any]) -> str:
    lines = [f"키: `{key}`", ""]
    if isinstance(brief, dict):
        for field, label in (("keywords", "키워드"), ("fortune_judgment", "운의 판단"), ("writing_conditions", "작성 조건")):
            value = brief.get(field)
            text = " · ".join(value) if string_list(value) else value if isinstance(value, str) else "작성 대기"
            lines.extend([f"{label}: {text}", ""])
        lines.append("출처:")
        lines.append("")
        for source in sources_for(key, brief, sources):
            source_id, url = source.get("id", "?"), source.get("url")
            label = f"[{source_id}]({url})" if url else f"{source_id} (출처 주소 확인 필요)"
            section = source.get("section", "")
            lines.append(f"- {label}" + (f" — {section}" if section else ""))
        lines.extend(["", f"적용 근거: {brief.get('application_note', '작성 대기')}", ""])
    else:
        lines.extend(["의미표 작성 대기.", ""])
    if not isinstance(bundle, dict):
        lines.extend(["원고 작성 대기.", ""])
        return "\n".join(lines)
    if isinstance(bundle.get("headline"), str):
        lines.extend([bundle["headline"], ""])
    paragraphs = bundle.get("paragraphs", [])
    if isinstance(paragraphs, list):
        for paragraph in paragraphs:
            lines.extend([paragraph if isinstance(paragraph, str) else "유효한 단락이 아님.", ""])
    if key.startswith("overall."):
        for field, label in (("do", "해볼 것"), ("pause", "조심할 것")):
            value = bundle.get(field)
            lines.append(f"- {label}: {value if isinstance(value, str) else '작성 대기'}")
        lines.append("")
    return "\n".join(lines)


def render_artifacts(data: dict[str, Any], briefs: dict[str, Any], sources: dict[str, Any], report: dict[str, Any]) -> dict[str, str]:
    files = {}
    caveat = "편집 정본 JSON을 그대로 표시한 검토용 전문이야. 생성·정적 검사만으로 문체 검수 통과를 뜻하지 않아. 미작성 원고와 검수 상태는 validation.json에서 확인해."
    all_cards = ["# 같은 카드 한 장으로 읽는 다섯 분야", "", "156개 카드·정역 조합을 비교하기 위한 편집 자료야. 실제 서비스의 다섯 분야별 카드 추첨을 한 장 추첨으로 바꾸는 자료가 아니야.", "", caveat, ""]
    axis_lines = {axis: [f"# {LABELS[axis]} 전문", "", caveat, ""] for axis in AXES}
    for card in CARDS:
        for orientation, orientation_label in ORIENTATIONS.items():
            title = f"{card_label(card)} · {orientation_label}"
            all_cards.extend([f"## {title}", ""])
            for axis in AXES:
                key = f"{axis}.{card}.{orientation}"
                body = render_entry(key, data[axis].get(key), briefs[axis].get(key), sources)
                axis_lines[axis].extend([f"## {title}", "", body])
                all_cards.extend([f"### {LABELS[axis]}", "", body])
    for axis, lines in axis_lines.items():
        files[f"{axis}.md"] = "\n".join(lines).rstrip() + "\n"
    files["all-cards.md"] = "\n".join(all_cards).rstrip() + "\n"
    files["validation.json"] = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Do not write; fail if validation is incomplete/invalid or generated files are stale.")
    args = parser.parse_args()
    audit = Audit()
    data = {axis: audit.read(f"{axis}.json") for axis in AXES}
    briefs = {axis: audit.read(f"{axis}.briefs.json") for axis in AXES}
    authors = {axis: audit.read(f"{axis}.author-review.json") for axis in AXES}
    review, sources = audit.read("review.json"), audit.read("source-briefs.json")
    # Other writers may save during generation. Keep the exact input hashes and
    # explicitly flag an incoherent read instead of certifying a mixed snapshot.
    for filename, read_hash in list(audit.input_hashes.items()):
        path = ROOT / filename
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != read_hash:
            audit.issue("pending", "input_changed_during_read", file=filename)
    audit.input_hashes[Path(__file__).name] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report = validate(audit, data, briefs, authors, review, sources)
    artifacts = render_artifacts(data, briefs, sources, report)
    output = ROOT / "generated"
    stale = []
    if args.check:
        for name, content in artifacts.items():
            path = output / name
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                stale.append(name)
    else:
        output.mkdir(exist_ok=True)
        for name, content in artifacts.items():
            (output / name).write_text(content, encoding="utf-8")
    summary = {"status": report["status"], "copy": report["actual_total"], "expected": 780, "errors": len(audit.errors), "pending": len(audit.pending), "warnings": len(audit.warnings), "mode": "check" if args.check else "write", "stale_generated_files": stale}
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if report["status"] != "pass" or stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
