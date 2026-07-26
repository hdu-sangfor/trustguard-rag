"""跨仓库 v1 契约的可执行冻结测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = ROOT / "contracts" / "v1"
SCHEMA_ROOT = CONTRACT_ROOT / "schemas"
FIXTURE_ROOT = ROOT / "tests" / "contracts" / "v1"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _contracts() -> list[dict[str, str]]:
    return _load(CONTRACT_ROOT / "manifest.json")["contracts"]


def _registry() -> Registry:
    resources = []
    for path in SCHEMA_ROOT.glob("*.schema.json"):
        schema = _load(path)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


@pytest.mark.parametrize("contract", _contracts(), ids=lambda item: item["name"])
def test_valid_contract_fixtures(contract: dict[str, str]) -> None:
    schema = _load(CONTRACT_ROOT / contract["schema"])
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=FormatChecker(),
    )
    validator.validate(_load(FIXTURE_ROOT / "valid" / f"{contract['name']}.json"))


@pytest.mark.parametrize("contract", _contracts(), ids=lambda item: item["name"])
def test_invalid_contract_fixtures_are_rejected(contract: dict[str, str]) -> None:
    schema = _load(CONTRACT_ROOT / contract["schema"])
    validator = Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=FormatChecker(),
    )
    with pytest.raises(ValidationError):
        validator.validate(_load(FIXTURE_ROOT / "invalid" / f"{contract['name']}.json"))


def test_manifest_covers_all_schemas_and_fixtures() -> None:
    contracts = _contracts()
    names = {item["name"] for item in contracts}
    schema_paths = {item["schema"] for item in contracts}

    assert len(names) == len(contracts)
    assert schema_paths == {
        f"schemas/{path.name}" for path in SCHEMA_ROOT.glob("*.schema.json")
    }
    for fixture_kind in ("valid", "invalid"):
        assert names == {
            path.stem
            for path in (FIXTURE_ROOT / fixture_kind).glob("*.json")
        }


def test_manifest_freezes_reviewed_baselines() -> None:
    manifest = _load(CONTRACT_ROOT / "manifest.json")

    assert manifest["contract_version"] == "v1"
    assert manifest["rag_baseline"] == "origin/main@93d08d0"
    assert manifest["agent_baseline"] == "trustguard-agent/main@c8f3796"
