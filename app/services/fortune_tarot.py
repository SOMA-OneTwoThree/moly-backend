"""Internal editorial provenance for complete, prewritten fortune paragraphs.

Cards select whole editorial paragraphs independently of scores. Card identities
and editorial provenance never enter the public response.
"""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping

FILENAME = "tarot-editorial.v2.json"
SCHEMA = "fortune-tarot-editorial-v2"
LOCALES = ("ko", "en", "ja")
AXES = ("overall", "love", "money", "work", "energy")
MAJORS = (
    "fool", "magician", "high_priestess", "empress", "emperor", "hierophant",
    "lovers", "chariot", "strength", "hermit", "wheel_of_fortune", "justice",
    "hanged_man", "death", "temperance", "devil", "tower", "star", "moon",
    "sun", "judgement", "world",
)
CARD_IDS = frozenset({f"major.{name}" for name in MAJORS} | {
    f"{suit}.{rank:02d}" for suit in ("cups", "swords", "wands", "pentacles")
    for rank in range(1, 15)
})



class TarotEditorialError(ValueError):
    """Incomplete or stale card-to-copy provenance must fail catalog validation."""


def copy_digest(bundle: Mapping[str, Any]) -> str:
    return sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def _keys(value: Any, expected: set | frozenset, label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise TarotEditorialError(f"{label}: unexpected fields or coverage")


def _prose(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise TarotEditorialError(f"{label}: nonempty editorial text required")


READING_KEYS = frozenset(
    f"{axis}.{card}.{orientation}" for axis in AXES for card in CARD_IDS
    for orientation in ("upright", "reversed")
)


def validate_editorial(asset: Any, copies: Mapping[str, Mapping[str, Any]], *, version: str) -> None:
    _keys(asset, {"schema", "copy_version", "source", "policy", "readings"}, "tarot editorial")
    if asset["schema"] != SCHEMA or asset["copy_version"] != version:
        raise TarotEditorialError("tarot editorial version mismatch")
    if asset["source"] != "https://sacred-texts.com/tarot/pkt/index.htm":
        raise TarotEditorialError("tarot editorial source mismatch")
    if asset["policy"] != "independent-card-draw-complete-paragraphs":
        raise TarotEditorialError("tarot editorial selection policy mismatch")
    _keys(asset["readings"], READING_KEYS, "tarot readings")
    _keys(dict(copies), set(LOCALES), "tarot locales")
    for key, entry in asset["readings"].items():
        _keys(entry, {"card_id", "orientation", "axis", "meaning", "angle", "observations", "copy_sha256"}, key)
        if f"{entry['axis']}.{entry['card_id']}.{entry['orientation']}" != key:
            raise TarotEditorialError(f"{key}: mismatched card identity")
        for field in ("meaning", "angle"):
            _prose(entry[field], key)
        observations = entry["observations"]
        if not isinstance(observations, list) or not 2 <= len(observations) <= 3:
            raise TarotEditorialError(f"{key}: two or three observations required")
        for observation in observations:
            _prose(observation, key)
        if len(set(observations)) != len(observations):
            raise TarotEditorialError(f"{key}: duplicate observations")
        _keys(entry["copy_sha256"], set(LOCALES), key)
        for locale in LOCALES:
            if entry["copy_sha256"][locale] != copy_digest(copies[locale]["readings"][key]):
                raise TarotEditorialError(f"{key}/{locale}: editorial/copy hash mismatch")
