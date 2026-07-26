"""不可解析 Resource Ref 的保密性、完整性和版本绑定。"""

from __future__ import annotations

import pytest

from app.application.resource_refs import (
    InvalidResourceRef,
    ResourceRefClaims,
    ResourceRefCodec,
)


def test_resource_ref_round_trip_hides_physical_identity() -> None:
    codec = ResourceRefCodec("resource-ref-test-secret-with-more-than-32-characters")
    claims = ResourceRefClaims(
        scope="compliance",
        knowledge_base_id="kb-sensitive",
        chunk_id="chunk-sensitive",
        source_revision=7,
        content_hash="a" * 64,
    )

    resource_ref = codec.issue(claims)

    assert resource_ref.startswith("krf1.")
    assert "kb-sensitive" not in resource_ref
    assert "chunk-sensitive" not in resource_ref
    assert codec.parse(resource_ref) == claims


def test_resource_ref_rejects_tampering_and_wrong_secret() -> None:
    codec = ResourceRefCodec("resource-ref-test-secret-with-more-than-32-characters")
    resource_ref = codec.issue(
        ResourceRefClaims(
            scope="compliance",
            knowledge_base_id="kb-a",
            chunk_id="chunk-a",
            source_revision=1,
            content_hash="b" * 64,
        )
    )
    position = len(resource_ref) // 2
    replacement = "A" if resource_ref[position] != "A" else "B"
    tampered = resource_ref[:position] + replacement + resource_ref[position + 1 :]

    with pytest.raises(InvalidResourceRef):
        codec.parse(tampered)
    with pytest.raises(InvalidResourceRef):
        ResourceRefCodec(
            "another-resource-ref-secret-with-more-than-32-characters"
        ).parse(resource_ref)
