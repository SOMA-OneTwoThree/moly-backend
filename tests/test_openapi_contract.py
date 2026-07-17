"""Canonical OpenAPI structure, bundle freshness, and FastAPI surface conformance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.openapi.models import OpenAPI

from app.main import app
from scripts.openapi_contract import (
    DEFAULT_OUTPUT,
    DEFAULT_SOURCE,
    build_bundle,
    dump_yaml,
    load_yaml,
    resolve_pointer,
    validate_internal_refs,
)


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
PUBLIC_SECURITY = {
    ("/health", "get"): [],
    ("/webhooks/revenuecat", "post"): [{"RevenueCatAuthorization": []}],
    ("/webhooks/ad-ssv", "get"): [{"AdMobSsvSignature": []}],
}
MANUAL_PARAMETERS = {
    ("/webhooks/ad-ssv", "get"): {
        ("key_id", "query"),
        ("custom_data", "query"),
        ("transaction_id", "query"),
    },
}


def _operations(document: dict[str, Any], *, skip_dev: bool = False) -> dict[tuple[str, str], dict]:
    return {
        (path, method): operation
        for path, path_item in document["paths"].items()
        if not (skip_dev and path.startswith("/dev/"))
        for method, operation in path_item.items()
        if method in HTTP_METHODS
    }


def _resolve_ref(document: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    while isinstance(value, dict) and isinstance(value.get("$ref"), str):
        ref = value["$ref"]
        assert ref.startswith("#/")
        assert ref not in seen, f"circular reference: {ref}"
        seen.add(ref)
        value = resolve_pointer(document, tuple(
            token.replace("~1", "/").replace("~0", "~")
            for token in ref[2:].split("/")
        ))
    assert isinstance(value, dict)
    return value


def _json_schema(document: dict[str, Any], response: dict[str, Any]) -> dict | None:
    response = _resolve_ref(document, response)
    return response.get("content", {}).get("application/json", {}).get("schema")


def _request_schema(operation: dict[str, Any]) -> dict | None:
    return (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )


def test_split_sources_and_committed_bundle_are_valid_and_current():
    for source in DEFAULT_SOURCE.parent.rglob("*.yaml"):
        load_yaml(source)
    generated = build_bundle(DEFAULT_SOURCE)
    assert DEFAULT_OUTPUT.read_text(encoding="utf-8") == dump_yaml(generated)
    committed = load_yaml(DEFAULT_OUTPUT)
    assert committed == generated
    validate_internal_refs(committed)
    OpenAPI.model_validate(committed)


def test_contract_metadata_marks_a_stable_normative_release():
    document = load_yaml(DEFAULT_OUTPUT)
    assert document["info"]["version"] == "1.0.0"
    assert document["x-contract-status"] == "stable"
    assert document["x-contract-versioning"]["current"] == document["info"]["version"]
    assert document["x-source-of-truth"]["contract"] == "openapi/openapi.yaml"
    assert document["x-source-of-truth"]["generated_bundle"] == "openapi/openapi.bundle.yaml"
    assert document["x-generated-by"].startswith("scripts/openapi_contract.py")


def test_route_methods_success_statuses_and_top_level_models_match_fastapi():
    contract = load_yaml(DEFAULT_OUTPUT)
    runtime = app.openapi()
    documented = _operations(contract)
    generated = _operations(runtime, skip_dev=True)
    assert documented.keys() == generated.keys()

    for key, runtime_operation in generated.items():
        contract_operation = documented[key]
        runtime_request = _request_schema(runtime_operation)
        contract_request = _request_schema(contract_operation)
        if key == ("/webhooks/revenuecat", "post"):
            assert runtime_request is None and contract_request is not None
        elif runtime_request is None:
            assert contract_request is None, key
        else:
            assert contract_request is not None, key
            assert contract_request.get("$ref") == runtime_request.get("$ref"), key

        runtime_success = {
            status: response
            for status, response in runtime_operation["responses"].items()
            if status.startswith("2")
        }
        contract_success = {
            status: response
            for status, response in contract_operation["responses"].items()
            if status.startswith("2")
        }
        assert contract_success.keys() == runtime_success.keys(), key
        for status, runtime_response in runtime_success.items():
            runtime_schema = _json_schema(runtime, runtime_response)
            contract_schema = _json_schema(contract, contract_success[status])
            if runtime_schema is None:
                assert contract_schema is None, (*key, status)
            else:
                assert contract_schema is not None, (*key, status)
                assert contract_schema.get("$ref") == runtime_schema.get("$ref"), (*key, status)


def test_parameters_operation_ids_and_security_are_explicit():
    contract = load_yaml(DEFAULT_OUTPUT)
    runtime = app.openapi()
    documented = _operations(contract)
    generated = _operations(runtime, skip_dev=True)
    operation_ids: list[str] = []

    for key, operation in documented.items():
        operation_id = operation.get("operationId")
        assert operation_id, key
        operation_ids.append(operation_id)
        expected_security = PUBLIC_SECURITY.get(key, [{"SupabaseBearer": []}])
        assert operation.get("security") == expected_security, key

        documented_parameters = {
            (parameter["name"].lower(), parameter["in"])
            for parameter in operation.get("parameters", [])
        }
        runtime_parameters = {
            (parameter["name"].lower(), parameter["in"])
            for parameter in generated[key].get("parameters", [])
            if not (key == ("/webhooks/revenuecat", "post") and parameter["in"] == "header")
        }
        expected_parameters = runtime_parameters | MANUAL_PARAMETERS.get(key, set())
        assert documented_parameters == expected_parameters, key

    assert len(operation_ids) == len(set(operation_ids))


def test_error_examples_match_the_error_envelope():
    contract = load_yaml(DEFAULT_OUTPUT)
    for operation in _operations(contract).values():
        for response in operation["responses"].values():
            response = _resolve_ref(contract, response)
            media = response.get("content", {}).get("application/json", {})
            schema = media.get("schema", {})
            if schema.get("$ref") != "#/components/schemas/ErrorEnvelope":
                continue
            examples = media.get("examples", {})
            assert examples
            for example in examples.values():
                value = example["value"]
                assert set(value) == {"error"}
                assert set(value["error"]) == {"code", "message", "details"}
                assert isinstance(value["error"]["details"], dict)


def test_contract_files_stay_in_declared_directory():
    root = DEFAULT_SOURCE.parent.resolve()
    for source in root.rglob("*.yaml"):
        assert source.resolve().is_relative_to(root)
    assert Path(DEFAULT_OUTPUT).parent == root
