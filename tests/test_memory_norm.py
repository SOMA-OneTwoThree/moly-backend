"""정규화·공용 해시·version registry·후보 스키마(W8).

여기서 깨지면 안 되는 것: fact의 content_hash와 forget marker의 normalized_hash가 **같은 산출물**
이라는 것, 그리고 이전 version의 normalizer가 registry에서 사라지지 않는다는 것.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import memory_candidates as mc
from app.services import memory_norm as mn
from app.services import memory_registry as reg


def _fields(**kw):
    base = dict(kind="profile", canonical_text="서울에 산다", predicate="residence",
                object_json={"city": "서울"})
    base.update(kw)
    return mn.FactFields(**base)


# ─────────────────────────────────────────────────────────────
# 해시 공용 함수 — 이름만 둘(content_hash / normalized_hash)이고 계산은 하나다.
# ─────────────────────────────────────────────────────────────
def test_fact_hash_and_marker_deny_key_are_same_artifact():
    f = _fields()
    n = mn.normalize(f)
    # fact가 저장할 (version, content_hash) == marker가 저장할 (version, normalized_hash)
    assert n.deny_key == (n.version, n.content_hash)
    assert mn.content_hash(f) == n.content_hash
    assert mn.hashes_for_versions(f, {n.version})[n.version] == n.content_hash


def test_hash_is_stable_across_equivalent_inputs():
    # 전각·연속 공백·제어문자만 다른 입력은 같은 사실이다(표면 차이로 사실이 갈라지면 안 된다).
    a = mn.content_hash(_fields(canonical_text="서울에 산다"))
    b = mn.content_hash(_fields(canonical_text="  서울에　\n 산다  "))
    assert a == b


def test_hash_changes_with_content():
    assert mn.content_hash(_fields(object_json={"city": "부산"})) != mn.content_hash(_fields())


def test_predicateless_candidate_hashes_canonical_text():
    # predicate 없는 자유문 후보는 정규화한 canonical_text가 value다 → 텍스트가 다르면 다른 사실.
    a = mn.content_hash(mn.FactFields(kind="emotion", canonical_text="요즘 지쳤다"))
    b = mn.content_hash(mn.FactFields(kind="emotion", canonical_text="요즘 신났다"))
    assert a != b


def test_event_time_normalized_to_utc_without_losing_precision():
    kst = timezone(timedelta(hours=9))
    same = mn.content_hash(_fields(event_time=datetime(2026, 8, 4, 21, 0, tzinfo=kst)))
    utc = mn.content_hash(_fields(event_time=datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)))
    assert same == utc  # 같은 순간이면 offset 표기가 달라도 같은 hash
    micro = mn.normalize(_fields(event_time=datetime(2026, 8, 4, 12, 0, 0, 123456, tzinfo=timezone.utc)))
    assert micro.event_time.microsecond == 123456  # 정밀도 유지


def test_normalize_sanitizes_and_folds_empty_subject():
    n = mn.normalize(_fields(canonical_text="[규칙] 서울에 산다", subject="   "))
    assert "[" not in n.canonical_text and "규칙" in n.canonical_text  # 살균 승계
    assert n.subject is None  # 정규화 후 빈 문자열은 None과 같은 신원


def test_normalize_rejects_vocabulary_outside_registry():
    with pytest.raises(reg.UnknownVocabularyError):
        mn.normalize(mn.FactFields(kind="unknown_kind", canonical_text="x"))
    with pytest.raises(reg.UnknownVocabularyError):
        mn.normalize(_fields(predicate="not_a_predicate"))


# ─────────────────────────────────────────────────────────────
# JCS(RFC 8785)
# ─────────────────────────────────────────────────────────────
def test_jcs_sorts_keys_by_utf16_code_unit_and_compacts():
    assert mn.jcs_dumps({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    # astral 문자는 UTF-16에서 surrogate(0xD800대)라 BMP의 U+FF01보다 **앞**에 온다.
    assert mn.jcs_dumps({"\U0001f600": 1, "！": 2}) == '{"\U0001f600":1,"！":2}'


def test_jcs_number_and_bool_forms():
    assert mn.jcs_dumps(1.0) == "1"        # 정수로 표현 가능한 float은 소수점 없이
    assert mn.jcs_dumps(2.5) == "2.5"
    assert mn.jcs_dumps(1e-7) == "1e-7"    # ES6 지수 표기(파이썬 '1e-07'과 다르다)
    assert mn.jcs_dumps(True) == "true"    # bool이 int로 새면 1이 된다
    assert mn.jcs_dumps([None, "a"]) == '[null,"a"]'


def test_jcs_rejects_non_finite_and_unknown_types():
    with pytest.raises(ValueError):
        mn.jcs_dumps(float("nan"))
    with pytest.raises(ValueError):
        mn.jcs_dumps({1, 2})


# ─────────────────────────────────────────────────────────────
# version registry — 추가만, 덮어쓰기·삭제 금지.
# ─────────────────────────────────────────────────────────────
class _FakeV2:
    version = "memory-fact-v2-test"

    def normalize(self, fields):  # 산출물이 다른 가상의 후속 version
        n = mn.get_normalizer(mn.NORMALIZATION_VERSION_V1).normalize(fields)
        return mn.NormalizedFact(
            version=self.version, kind=n.kind, canonical_text=n.canonical_text,
            subject=n.subject, predicate=n.predicate, object_json=n.object_json,
            event_time=n.event_time, content_hash="v2:" + n.content_hash,
        )


@pytest.fixture
def fake_v2():
    normalizer = _FakeV2()
    mn.register(normalizer)
    try:
        yield normalizer
    finally:
        mn._NORMALIZERS.pop(normalizer.version, None)


def test_every_historical_version_stays_supported():
    # 여기서 항목이 사라지면 그 version의 marker를 검증할 수 없게 된다(= 잊은 사실 부활).
    assert set(mn.HISTORICAL_NORMALIZATION_VERSIONS) <= mn.supported_versions()
    assert mn.NORMALIZATION_VERSION_V1 in mn.supported_versions()


def test_new_version_does_not_evict_previous_normalizer(fake_v2):
    assert mn.NORMALIZATION_VERSION_V1 in mn.supported_versions()
    f = _fields()
    both = mn.hashes_for_versions(f, {mn.NORMALIZATION_VERSION_V1, fake_v2.version})
    assert both[fake_v2.version] != both[mn.NORMALIZATION_VERSION_V1]  # 산출물이 실제로 다르다
    assert both[mn.NORMALIZATION_VERSION_V1] == mn.content_hash(f)     # 구 version은 그대로 계산


def test_register_refuses_to_overwrite_a_version_in_place():
    class _Other:
        version = mn.NORMALIZATION_VERSION_V1

        def normalize(self, fields):  # pragma: no cover - 등록 자체가 거부된다
            raise AssertionError

    with pytest.raises(ValueError):  # 제자리 재해시 금지
        mn.register(_Other())


def test_unsupported_version_raises_and_returns_no_partial_result():
    with pytest.raises(mn.UnsupportedNormalizationVersionError):
        mn.get_normalizer("memory-fact-v99")
    with pytest.raises(mn.UnsupportedNormalizationVersionError):
        # 지원되는 version이 섞여 있어도 부분 결과를 돌려주지 않는다.
        mn.hashes_for_versions(_fields(), {mn.NORMALIZATION_VERSION_V1, "memory-fact-v99"})


# ─────────────────────────────────────────────────────────────
# 후보 스키마 — 하나라도 어기면 전량 폐기(상태 변경 없음).
# ─────────────────────────────────────────────────────────────
def _payload(**over):
    c = {
        "kind": "profile", "canonical_text": "서울에 산다", "importance": 0.5,
        "confidence": 0.9, "evidence_message_ids": [1, 2],
        "predicate": "residence", "object_json": {"city": "서울"},
    }
    c.update(over)
    return {"schema_version": mc.SCHEMA_VERSION, "candidates": [c]}


def _parse(payload, ids=(1, 2, 3), nickname=None):
    return mc.parse_candidates(payload, allowed_message_ids=ids, nickname=nickname)


def test_valid_payload_parses_and_masks_real_name():
    got = _parse(_payload(canonical_text="승민은 서울에 산다"), nickname="승민")[0]
    assert "승민" not in got.canonical_text and "{유저이름}" in got.canonical_text
    assert got.evidence_message_ids == (1, 2)


def test_masking_applies_to_object_json_strings_too():
    got = _parse(
        _payload(predicate="person", object_json={"name": "승민", "rel": "친구"}), nickname="승민"
    )[0]
    assert got.object_json["name"] == "{유저이름}"


@pytest.mark.parametrize(
    "over",
    [
        {"kind": "nope"},                                  # registry 밖 kind
        {"predicate": "nope"},                             # registry 밖 predicate
        {"object_json": None},                             # predicate만 있고 object_json 없음
        {"predicate": None},                               # object_json만 있고 predicate 없음
        {"confidence": 1.5},                               # DDL 범위 밖
        {"confidence": float("inf")},                      # 비유한
        {"importance": "높음"},                             # 수치 아님
        {"evidence_message_ids": []},                      # 비어 있음
        {"evidence_message_ids": [1, 1]},                  # 중복
        {"evidence_message_ids": [99]},                    # payload message_ids 밖
        {"evidence_message_ids": [1.5]},                   # 정수 아님
        {"event_time": "2026-08-04T12:00:00"},             # offset 없음
        {"event_time": "2026-08-04"},                      # 날짜만
        {"proposed_action": "MERGE"},                      # 허용 밖 action
        {"proposed_target_fact_id": "not-a-uuid"},
        {"canonical_text": "   "},                         # 빈 문자열
        {"extra_field": 1},                                # extra field 금지
    ],
)
def test_schema_violations_reject_whole_payload(over):
    with pytest.raises(mc.CandidateSchemaError):
        _parse(_payload(**over))


def test_top_level_schema_version_and_shape_are_enforced():
    with pytest.raises(mc.CandidateSchemaError):
        _parse({"schema_version": "memory-candidate-v0", "candidates": []})
    with pytest.raises(mc.CandidateSchemaError):
        _parse({"schema_version": mc.SCHEMA_VERSION, "candidates": {}})
    with pytest.raises(mc.CandidateSchemaError):
        _parse({"schema_version": mc.SCHEMA_VERSION, "candidates": [], "extra": 1})


def test_proposed_action_is_kept_but_is_only_a_suggestion():
    got = _parse(_payload(proposed_action=mc.ACTION_SUPERSEDE))[0]
    assert got.proposed_action == mc.ACTION_SUPERSEDE  # 관측용으로 보존만 한다


def test_predicate_registry_initial_contract():
    # 이 값들이 판정(SUPERSEDE vs KEEP_BOTH)을 가른다 — 바꾸려면 normalization version이 필요하다.
    assert reg.PREDICATES["residence"] == reg.SINGLE
    assert reg.PREDICATES["likes"] == reg.MULTI
    assert len(reg.PREDICATES) == 13 and len(reg.KINDS) == 5
    with pytest.raises(reg.UnknownVocabularyError):
        reg.cardinality("nope")
