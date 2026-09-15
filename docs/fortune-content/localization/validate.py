#!/usr/bin/env python3
"""Inspect localization sources and exact review proofs, without importing the app.

Writes validation.json by default. --check checks that artifact without writing.
This script does not assess language quality or create editorial review approvals.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import importlib.util
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location("localized_build", ROOT / "scripts/build_fortune_localized_copy.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
AXES = builder.AXES
DIMENSIONS = {"naturalness", "readability", "axis_specificity", "meaning_parity", "calibrated_confidence"}


def audit() -> dict:
    errors, pending, repetition = [], [], []
    counts, source_hashes = {}, {}

    def read(path):
        if not path.exists():
            pending.append({"file": str(path.relative_to(ROOT)), "reason": "missing_file"})
            return {}
        value = builder.read_json(path)
        source_hashes[str(path.relative_to(ROOT))] = builder.digest(value)
        return value

    ko = {axis: read(HERE.parent / "ko-rewrite" / f"{axis}.json") for axis in AXES}
    editorial = {"ko": {key: bundle for data in ko.values() for key, bundle in data.items()}}
    review = read(HERE / "review.json")
    for locale in ("en", "ja"):
        editorial[locale] = {}
        author = read(HERE / locale / "author-review.json")
        counts[locale] = {"by_axis": {}, "bodies": 0, "short_fields": 0,
                          "current_author_reads": 0, "current_independent_passes": 0}
        whole, sentences, short_fields = defaultdict(list), defaultdict(list), defaultdict(list)
        for axis in AXES:
            data = read(HERE / locale / f"{axis}.json")
            editorial[locale].update(data)
            counts[locale]["by_axis"][axis] = len(data)
            if set(data) != set(ko[axis]):
                pending.append({"locale": locale, "axis": axis, "reason": "coverage",
                                "missing": sorted(set(ko[axis]) - set(data)),
                                "extra": sorted(set(data) - set(ko[axis]))})
            for key, bundle in data.items():
                try:
                    builder.validate_bundle(key, bundle, locale)
                except (ValueError, KeyError, TypeError) as exc:
                    errors.append({"locale": locale, "key": key, "reason": str(exc)})
                    continue
                if key not in ko[axis]:
                    continue
                counts[locale]["bodies"] += 1
                counts[locale]["short_fields"] += 3 if axis == "overall" else 0
                if len(bundle["paragraphs"]) != len(ko[axis][key]["paragraphs"]):
                    errors.append({"locale": locale, "key": key, "reason": "paragraph_parity"})
                digest = builder.digest(bundle)
                if author.get("read_bundle_sha256", {}).get(key) == digest:
                    counts[locale]["current_author_reads"] += 1
                proof = review.get("locales", {}).get(locale, {}).get(key, {})
                scores = proof.get("scores", {})
                if (proof.get("status") == "pass" and proof.get("read_bundle_sha256") == digest
                        and proof.get("source_ko_sha256") == builder.digest(ko[axis][key])
                        and set(scores) == DIMENSIONS
                        and all(type(v) in (int, float) and 4 <= v <= 5 for v in scores.values())):
                    counts[locale]["current_independent_passes"] += 1
                else:
                    pending.append({"locale": locale, "key": key, "reason": "independent_read_not_current_pass"})
                body = "\n\n".join(bundle["paragraphs"])
                whole[body].append(key)
                for paragraph in bundle["paragraphs"]:
                    for sentence in re.split(r"(?<=[.!?。！？])\s*", paragraph):
                        if sentence:
                            sentences[sentence].append(key)
                for name in ("headline", "do", "pause"):
                    if name in bundle:
                        short_fields[(name, bundle[name])].append(key)
        for body, keys in whole.items():
            if len(keys) > 1:
                errors.append({"locale": locale, "reason": "duplicate_full_body", "keys": keys})
        for sentence, keys in sentences.items():
            if len(set(keys)) >= 3:
                repetition.append({"locale": locale, "kind": "sentence", "text": sentence, "keys": keys})
        for (name, value), keys in short_fields.items():
            if len(keys) >= 3:
                repetition.append({"locale": locale, "kind": name, "text": value, "keys": keys})
        if (author.get("status") != "complete" or author.get("actual_self_read_count") != 780
                or counts[locale]["current_author_reads"] != 780):
            pending.append({"locale": locale, "reason": "author_read_incomplete_or_stale"})
    for finding in review.get("findings", []):
        if finding.get("status") == "open":
            pending.append({"reason": "open_editorial_finding", **finding})
    short_review = read(HERE / "en-short-review.json")
    english_overall = read(HERE / "en/overall.json")
    if (short_review.get("status") != "complete"
            or short_review.get("actual_short_field_read_count") != 468):
        pending.append({"reason": "standalone_short_review_incomplete"})
    for key, bundle in english_overall.items():
        proof = short_review.get("entries", {}).get(key, {})
        fields = {name: bundle[name] for name in ("headline", "do", "pause")}
        if proof.get("status") != "pass" or proof.get("read_short_fields_sha256") != builder.digest(fields):
            pending.append({"key": key, "reason": "standalone_short_review_stale_or_failed"})
    ja_independent = read(HERE / "ja-independent-review.json")
    if ja_independent.get("status") != "complete" or ja_independent.get("actual_full_bundle_read_count") != 780:
        pending.append({"reason": "japanese_independent_review_incomplete"})
    ja_entries = ja_independent.get("entries", {})
    if set(ja_entries) != set(editorial["ko"]):
        pending.append({"reason": "japanese_independent_coverage_incomplete"})
    for key, bundle in editorial["ja"].items():
        proof = ja_entries.get(key, {})
        scores = proof.get("scores", {})
        if (key not in editorial["ko"] or proof.get("status") != "pass"
                or proof.get("read_bundle_sha256") != builder.digest(bundle)
                or proof.get("source_ko_sha256") != builder.digest(editorial["ko"].get(key))
                or set(scores) != DIMENSIONS
                or any(type(v) not in (int, float) or not 4 <= v <= 5 for v in scores.values())):
            pending.append({"locale": "ja", "key": key,
                            "reason": "japanese_independent_proof_stale_or_failed"})
    for supplemental in (short_review, ja_independent):
        if any(f.get("status") == "open" for f in supplemental.get("findings", [])):
            pending.append({"reason": "open_supplemental_editorial_finding", "locale": supplemental.get("locale")})
    corrections = read(HERE / "brief-corrections.json")
    corrected_briefs = {}
    for axis in AXES:
        if any(key.startswith(axis + ".") for key in corrections.get("corrections", {})):
            corrected_briefs.update(read(HERE.parent / "ko-rewrite" / f"{axis}.briefs.json"))
    corrected_entries = corrections.get("corrections", {})
    if (corrections.get("status") != "complete"
            or corrections.get("root_review", {}).get("actual_corrected_note_and_ko_read_count") != len(corrected_entries)
            or not corrected_entries):
        pending.append({"reason": "brief_correction_review_incomplete"})
    for key, proof in corrected_entries.items():
        note = corrected_briefs.get(key, {}).get("meaning_choice")
        if (proof.get("status") != "pass" or proof.get("after") != note
                or proof.get("root_read_note_sha256") != builder.digest(note)
                or proof.get("root_read_ko_sha256") != builder.digest(editorial["ko"].get(key))):
            pending.append({"key": key, "reason": "brief_correction_review_stale_or_failed"})
    repetition_review = review.get("repetition_review", {})
    if (repetition_review.get("status") != "pass"
            or repetition_review.get("read_repetition_sha256") != builder.digest(repetition)
            or not repetition_review.get("assessment")):
        pending.append({"reason": "repetition_review_incomplete_or_stale"})
    samples = read(HERE / "samples.inputs.json")
    cross_reviews = {item.get("set"): item for item in review.get("cross_card_reviews", [])}
    expected_sets = {"sun-upright", "tower-upright", "mixed-major", "mixed-suits"}
    if set(samples.get("sets", {})) != expected_sets:
        pending.append({"reason": "curated_samples_incomplete"})
    for name, locales in samples.get("sets", {}).items():
        proof = cross_reviews.get(name, {})
        if (proof.get("status") != "pass" or proof.get("read_bundle_sha256") != locales
                or not proof.get("assessment")):
            pending.append({"set": name, "reason": "cross_card_read_incomplete_or_stale"})
        for locale, entries in locales.items():
            for key, expected in entries.items():
                if key not in editorial.get(locale, {}) or builder.digest(editorial[locale][key]) != expected:
                    pending.append({"set": name, "locale": locale, "key": key,
                                    "reason": "curated_sample_source_stale"})
    if review.get("status") != "complete":
        pending.append({"reason": "root_review_incomplete"})
    try:
        builder.build_outputs(ROOT)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        pending.append({"reason": "generation_gate", "detail": str(exc)})
    return {"schema": "moly-fortune-localization-validation-v1",
            "scope": "Static baseline localization proofs; retained EN/JA refer to baseline Korean, not the later Korean override. No app or API execution",
            "copy_version": builder.BASELINE_VERSION, "target_catalog_version": builder.VERSION,
            "counts": counts, "source_hashes": source_hashes,
            "errors": errors, "pending": pending, "repetition_for_review": repetition,
            "repetition_sha256": builder.digest(repetition),
            "status": "pass" if not errors and not pending else "incomplete"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = audit()
    raw = builder.encode(result)
    path = HERE / "validation.json"
    if args.check:
        if not path.exists() or path.read_bytes() != raw:
            print("Localization validation artifact is stale")
            return 1
    else:
        path.write_bytes(raw)
    print(json.dumps({"status": result["status"], "counts": result["counts"],
                      "errors": len(result["errors"]), "pending": len(result["pending"]),
                      "repetition_groups": len(result["repetition_for_review"])}, ensure_ascii=False))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
