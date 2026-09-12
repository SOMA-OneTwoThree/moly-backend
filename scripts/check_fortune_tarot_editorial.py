"""Audit complete independent-card copy; flags are not a naturalness certificate."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOCALES = ("ko", "en", "ja")
AXES = ("overall", "love", "money", "work", "energy")
INTERNAL = re.compile(r"타로|タロット|\btarot\b|major\.[a-z_]+|(?:cups|swords|wands|pentacles)\.\d", re.I)


def audit_readings(readings: dict, locale: str, *, complete: bool) -> dict:
    failures = []
    paragraphs: dict[str, list[str]] = defaultdict(list)
    sentences: dict[str, list[str]] = defaultdict(list)
    starts: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    lengths = []
    for key, bundle in sorted(readings.items()):
        axis = key.split(".", 1)[0]
        counts[axis] += 1
        chunks = bundle.get("flow" if axis == "overall" else "text", [])
        label = f"{locale}/{key}"
        expected_fields = {"headline", "flow", "do", "pause"} if axis == "overall" else {"text"}
        if set(bundle) != expected_fields or len(chunks) != (3 if axis == "overall" else 2):
            failures.append({"entry": label, "reason": "invalid complete bundle shape"})
        if not chunks or not all(isinstance(x, str) and x.strip() == x and x for x in chunks):
            failures.append({"entry": label, "reason": "empty or malformed paragraph"})
            continue
        paragraph = ("" if locale == "ja" else " ").join(chunks)
        parts = [s.strip() for s in re.split(r"[.!?。！？]", paragraph) if s.strip()]
        if len(parts) != 5:
            failures.append({"entry": label, "reason": "expected five complete sentences"})
        expected_chunk_sentences = [2, 2, 1] if axis == "overall" else [2, 3]
        if [len(re.findall(r"[.!?。！？]", chunk)) for chunk in chunks] != expected_chunk_sentences:
            failures.append({"entry": label, "reason": "sentence split differs from wire contract"})
        texts = [*chunks, *(bundle[k] for k in ("headline", "do", "pause") if k in bundle)]
        for value in texts:
            if INTERNAL.search(value):
                failures.append({"entry": label, "reason": "internal tarot information in public copy"})
            if any(term in value for term in ("\n", "\r", "겠어", "흐름이야")):
                failures.append({"entry": label, "reason": "retired tone or forced newline"})
            if locale != "ko" and re.search(r"[가-힣]", value):
                failures.append({"entry": label, "reason": "untranslated Korean"})
            if locale == "en" and re.search(r"[\u3040-\u30ff\u3400-\u9fff]", value):
                failures.append({"entry": label, "reason": "untranslated CJK text"})
        paragraphs[paragraph].append(key)
        for sentence in parts:
            sentences[sentence].append(key)
        starts[paragraph[:20]] += 1
        lengths.append(len(paragraph))
    if complete and (set(counts) != set(AXES) or any(counts[axis] != 156 for axis in AXES)):
        failures.append({"entry": locale, "reason": "expected 156 readings for each of five axes"})
    for keys in paragraphs.values():
        if len(keys) > 1:
            failures.append({"entry": locale, "reason": "duplicate complete paragraph", "keys": keys})
    return {
        "paragraphs": sum(counts.values()), "by_axis": dict(counts),
        "sentences": sum(map(len, sentences.values())),
        "min_characters": min(lengths, default=0), "max_characters": max(lengths, default=0),
        "repeated_sentences": [{"text": s, "keys": keys} for s, keys in sentences.items() if len(keys) >= 3],
        "repeated_starts": [{"text": s, "count": n} for s, n in starts.most_common() if n >= 5],
        "failures": failures,
    }


def audit() -> dict:
    from app.services import fortune_catalog
    fortune_catalog.load_catalog()
    reports = {}
    copies = {}
    for locale, filename in zip(LOCALES, ("copy.v3.json", "copy.v3.en.json", "copy.v3.ja.json")):
        asset = json.loads((ROOT / "app/resources/fortune" / filename).read_text())
        copies[locale] = asset["readings"]
        reports[locale] = audit_readings(asset["readings"], locale, complete=True)
    first_path = ROOT / "app/resources/fortune/first-visit.v1.json"
    first = json.loads(first_path.read_text())
    first_reports = {}
    for locale in LOCALES:
        readings = {}
        for entry in first["sets"]:
            copy = entry["copy_by_locale"][locale]
            readings[f"overall.{entry['id']}"] = copy["overall"]
            for axis, bundle in copy["categories"].items():
                readings[f"{axis}.{entry['id']}"] = bundle
        first_reports[locale] = audit_readings(readings, locale, complete=False)
    record = json.loads((ROOT / "docs/fortune-content/review.json").read_text())
    first_review = record.get("first_visit_review", {})
    first_failures = [f for r in first_reports.values() for f in r["failures"]]
    if first_review.get("status") != "pass" or first_review.get("sha256") != sha256(first_path.read_bytes()).hexdigest():
        first_failures.append({"entry": "first_visit_review", "reason": "first-visit review is missing or stale"})
    review_failures = audit_review(copies, fortune_catalog.COPY_VERSION) + first_failures
    return {"version": fortune_catalog.COPY_VERSION, "locales": reports, "first_visit": first_reports,
            "failures": [f for report in reports.values() for f in report["failures"]] + review_failures,
            "scope": "Structural checks and review flags only; full editorial review is separate."}


def audit_review(copies: dict, version: str) -> list[dict]:
    """Reject a document that still claims review of a subsequently edited bundle."""
    path = ROOT / "docs/fortune-content/review.json"
    if not path.exists():
        return [{"entry": "review", "reason": "final editorial review record is missing"}]
    record = json.loads(path.read_text())
    if (record.get("schema") != "fortune-editorial-review-v1"
            or record.get("copy_version") != version or record.get("status") != "pass"):
        return [{"entry": "review", "reason": "final review version or status mismatch"}]
    failures = []
    source_hashes = {
        key: sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode()).hexdigest()
        for key, bundle in copies["ko"].items()
    }
    for locale in LOCALES:
        expected = {
            key: sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()
            for key, bundle in copies[locale].items()
        }
        for role in ("language", "meaning"):
            reviewed = {}
            reviewed_sources = {}
            for evidence in record.get("evidence", {}).values():
                if evidence.get("locale") == locale and evidence.get("role") == role:
                    reviewed.update(evidence.get("read_bundle_sha256", {}))
                    reviewed_sources.update(evidence.get("source_ko_bundle_sha256", {}))
            if reviewed != expected:
                failures.append({"entry": f"review/{locale}/{role}",
                                 "reason": "reviewed bundle hashes differ from current copy"})
            if role == "meaning" and locale != "ko" and reviewed_sources != source_hashes:
                failures.append({"entry": f"review/{locale}/{role}",
                                 "reason": "reviewed Korean source hashes differ from current copy"})
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, help="audit a partial authored file without loading runtime assets")
    parser.add_argument("--locale", choices=LOCALES, default="ko")
    args = parser.parse_args()
    if args.staging:
        asset = json.loads(args.staging.read_text())
        report = audit_readings(asset["readings"], args.locale, complete=False)
    else:
        report = audit()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(bool(report["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
