"""Build and validate the canonical, self-contained OpenAPI contract."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml
from fastapi.openapi.models import OpenAPI
from yaml.resolver import BaseResolver


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPOSITORY_ROOT / "openapi/openapi.yaml"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "openapi/openapi.bundle.yaml"


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(
                f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        document = yaml.load(stream, Loader=UniqueKeyLoader)
    if not isinstance(document, dict):
        raise ValueError(f"{path}: document root must be a mapping")
    return document


def _pointer_tokens(fragment: str) -> tuple[str, ...]:
    fragment = unquote(fragment)
    if not fragment:
        return ()
    if not fragment.startswith("/"):
        raise ValueError(f"unsupported reference fragment: {fragment}")
    return tuple(
        token.replace("~1", "/").replace("~0", "~")
        for token in fragment[1:].split("/")
    )


def resolve_pointer(document: Any, tokens: tuple[str, ...]) -> Any:
    value = document
    for token in tokens:
        if isinstance(value, dict):
            if token not in value:
                raise ValueError(f"unresolved JSON pointer token {token!r}")
            value = value[token]
        elif isinstance(value, list):
            try:
                value = value[int(token)]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"unresolved JSON pointer index {token!r}") from exc
        else:
            raise ValueError(f"JSON pointer crosses non-container at {token!r}")
    return value


def iter_refs(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str):
            yield ref
        for child in value.values():
            yield from iter_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_refs(child)


def validate_internal_refs(document: dict[str, Any]) -> None:
    for ref in iter_refs(document):
        if not ref.startswith("#/"):
            raise ValueError(f"bundle contains external reference: {ref}")
        resolve_pointer(document, _pointer_tokens(ref[1:]))


CanonicalRef = tuple[Path, tuple[str, ...]]


class ContractBundler:
    def __init__(self, source: Path) -> None:
        self.source = source.resolve()
        self.contract_dir = self.source.parent
        self._documents: dict[Path, dict[str, Any]] = {}
        self.root = self._load(self.source)
        self.exports: dict[CanonicalRef, str] = {}
        components = self.root.get("components", {})
        for section, entries in components.items():
            if not isinstance(entries, dict):
                raise ValueError(f"component {section!r} is not a mapping")
            for name, entry in entries.items():
                if isinstance(entry, dict) and isinstance(entry.get("$ref"), str):
                    canonical = self._canonical_ref(entry["$ref"], self.source)
                    exported = f"#/components/{section}/{name}"
                    previous = self.exports.setdefault(canonical, exported)
                    if previous != exported:
                        raise ValueError(
                            f"component target exported twice: {previous} and {exported}"
                        )

    def _load(self, path: Path) -> dict[str, Any]:
        path = path.resolve()
        if not path.is_relative_to(self.contract_dir):
            raise ValueError(f"reference escapes contract directory: {path}")
        if path not in self._documents:
            self._documents[path] = load_yaml(path)
        return self._documents[path]

    def _canonical_ref(self, ref: str, current_file: Path) -> CanonicalRef:
        target_name, separator, fragment = ref.partition("#")
        if not separator:
            fragment = ""
        target_file = (
            (current_file.parent / unquote(target_name)).resolve()
            if target_name
            else current_file.resolve()
        )
        if not target_file.is_relative_to(self.contract_dir):
            raise ValueError(f"reference escapes contract directory: {ref}")
        return target_file, _pointer_tokens(fragment)

    def _resolve(self, canonical: CanonicalRef) -> Any:
        target_file, tokens = canonical
        return resolve_pointer(self._load(target_file), tokens)

    def _walk(
        self,
        value: Any,
        current_file: Path,
        stack: tuple[CanonicalRef, ...] = (),
    ) -> Any:
        if isinstance(value, list):
            return [self._walk(item, current_file, stack) for item in value]
        if not isinstance(value, dict):
            return deepcopy(value)

        ref = value.get("$ref")
        if isinstance(ref, str):
            canonical = self._canonical_ref(ref, current_file)
            siblings = {
                key: self._walk(item, current_file, stack)
                for key, item in value.items()
                if key != "$ref"
            }
            exported = self.exports.get(canonical)
            if exported is not None:
                return {"$ref": exported, **siblings}
            if canonical in stack:
                chain = " -> ".join(f"{path}#{'/'.join(tokens)}" for path, tokens in stack)
                raise ValueError(f"unexported circular reference: {chain} -> {ref}")
            target_file, _ = canonical
            materialized = self._walk(
                self._resolve(canonical),
                target_file,
                (*stack, canonical),
            )
            if siblings:
                if not isinstance(materialized, dict):
                    raise ValueError(f"cannot merge reference siblings into scalar target: {ref}")
                materialized.update(siblings)
            return materialized

        return {
            key: self._walk(item, current_file, stack)
            for key, item in value.items()
        }

    def build(self) -> dict[str, Any]:
        bundle: dict[str, Any] = {}
        for key, value in self.root.items():
            if key != "components":
                bundle[key] = self._walk(value, self.source)
                continue

            bundled_components: dict[str, Any] = {}
            for section, entries in value.items():
                bundled_entries: dict[str, Any] = {}
                for name, entry in entries.items():
                    if isinstance(entry, dict) and isinstance(entry.get("$ref"), str):
                        canonical = self._canonical_ref(entry["$ref"], self.source)
                        target_file, _ = canonical
                        materialized = self._walk(
                            self._resolve(canonical),
                            target_file,
                            (canonical,),
                        )
                        siblings = {
                            sibling_key: self._walk(sibling, self.source)
                            for sibling_key, sibling in entry.items()
                            if sibling_key != "$ref"
                        }
                        if siblings:
                            if not isinstance(materialized, dict):
                                raise ValueError(
                                    f"component {section}/{name} is not a mapping"
                                )
                            materialized.update(siblings)
                        bundled_entries[name] = materialized
                    else:
                        bundled_entries[name] = self._walk(entry, self.source)
                bundled_components[section] = bundled_entries
            bundle[key] = bundled_components

        bundle["x-generated-by"] = "scripts/openapi_contract.py; do not edit directly"
        validate_internal_refs(bundle)
        OpenAPI.model_validate(bundle)
        return bundle


def build_bundle(source: Path = DEFAULT_SOURCE) -> dict[str, Any]:
    return ContractBundler(source).build()


def dump_yaml(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=100)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="regenerate the bundled artifact")
    mode.add_argument("--check", action="store_true", help="check the committed bundle (default)")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    generated = dump_yaml(build_bundle(args.source))
    output = args.output.resolve()
    if args.write:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(generated, encoding="utf-8")
        print(f"OpenAPI bundle written: {output}")
        return
    if not output.exists():
        raise SystemExit(f"OpenAPI bundle is missing: run {Path(__file__).name} --write")
    if output.read_text(encoding="utf-8") != generated:
        raise SystemExit(f"OpenAPI bundle is stale: run {Path(__file__).name} --write")
    print(f"OpenAPI contract valid and bundle current: {output}")


if __name__ == "__main__":
    main()
