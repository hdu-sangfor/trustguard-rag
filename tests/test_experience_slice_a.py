"""Unit tests for Experience Slice A authority rules."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from app.domain.experience import ExperienceStatus, is_status_transition_allowed
from app.application.scopes import ScopeRegistry, resolve_scope_definition
from app.stores.experience_store import PENETRATION_EXPERIENCE_KB_ID
from app.settings import get_settings


@pytest.fixture(autouse=True)
def experience_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_EXPERIENCE_ENABLED", "true")
    get_settings.cache_clear()


def test_status_transition_allows_skip_forward() -> None:
    assert is_status_transition_allowed(
        ExperienceStatus.CANDIDATE, ExperienceStatus.PROVEN
    )
    assert is_status_transition_allowed(
        ExperienceStatus.CANDIDATE, ExperienceStatus.PENDING
    )
    assert is_status_transition_allowed(
        ExperienceStatus.PROVEN, ExperienceStatus.DEPRECATED
    )
    assert is_status_transition_allowed(
        ExperienceStatus.DEPRECATED, ExperienceStatus.ARCHIVED
    )


def test_status_transition_rejects_rollback() -> None:
    assert not is_status_transition_allowed(
        ExperienceStatus.PROVEN, ExperienceStatus.PENDING
    )
    assert not is_status_transition_allowed(
        ExperienceStatus.ARCHIVED, ExperienceStatus.PROVEN
    )
    assert not is_status_transition_allowed(
        ExperienceStatus.CANDIDATE, ExperienceStatus.CANDIDATE
    )


def _upsert_body(external_id: str, revision: int = 1, **overrides):
    body = {
        "schema_version": "trustguard-experience-upsert-v1",
        "external_id": external_id,
        "source_system": "trustguard-agent",
        "source_revision": revision,
        "knowledge_scope": "penetration",
        "workflow_type": "penetration",
        "experience_type": "skill_outcome",
        "workspace_id": None,
        "visibility": "global",
        "conditions": {"skill_id": "http-fingerprint"},
        "action_summary": "Confirm RememberMe then validate safely.",
        "outcome_summary": "Reduced false positives in lab.",
        "skill_id": "http-fingerprint",
        "phase": "recon",
        "source_task_id": "task-1",
        "evidence_refs": [],
        "expires_at": None,
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_upsert_uses_gateway_service_auth(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_GATEWAY_AUTH_ENABLED", "true")
    monkeypatch.setenv("RAG_GATEWAY_SERVICE_TOKEN", "gateway-token")
    get_settings.cache_clear()
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    missing = await client.put(
        f"/v1/experiences/{external_id}",
        json=_upsert_body(external_id),
    )
    assert missing.status_code == 401

    forbidden = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer wrong-token"},
        json=_upsert_body(external_id),
    )
    assert forbidden.status_code == 401
    accepted = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer gateway-token"},
        json=_upsert_body(external_id),
    )
    assert accepted.status_code == 200
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_upsert_candidate_and_reject_alert_triage(
    client: httpx.AsyncClient,
) -> None:
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    created = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer exp-write-token"},
        json=_upsert_body(external_id),
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["status"] == "candidate"
    assert payload["index_status"] == "not_indexed"
    experience_id = payload["id"]

    got = await client.get(
        f"/v1/experiences/{experience_id}",
        headers={"Authorization": "Bearer exp-write-token"},
    )
    assert got.status_code == 200
    assert got.json()["status"] == "candidate"

    rejected = await client.put(
        f"/v1/experiences/{external_id}-at",
        headers={"Authorization": "Bearer exp-write-token"},
        json=_upsert_body(
            f"{external_id}-at",
            knowledge_scope="alert-triage",
            workflow_type="alert-triage",
        ),
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "EXPERIENCE_SCOPE_NOT_ENABLED"


@pytest.mark.asyncio
async def test_gateway_service_can_patch_review_status(
    client: httpx.AsyncClient,
) -> None:
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    created = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer exp-write-token"},
        json=_upsert_body(external_id),
    )
    experience_id = created.json()["id"]

    promoted = await client.patch(
        f"/v1/experiences/{experience_id}/status",
        headers={"Authorization": "Bearer exp-admin-token"},
        json={"status": "proven", "reason": "reviewed"},
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["status"] == "proven"
    assert promoted.json()["index_status"] == "indexed"


@pytest.mark.asyncio
async def test_revision_rules(client: httpx.AsyncClient) -> None:
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    headers = {"Authorization": "Bearer exp-write-token"}
    first = await client.put(
        f"/v1/experiences/{external_id}",
        headers=headers,
        json=_upsert_body(external_id, revision=2),
    )
    assert first.status_code == 200

    stale = await client.put(
        f"/v1/experiences/{external_id}",
        headers=headers,
        json=_upsert_body(external_id, revision=1),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "EXPERIENCE_STALE_REVISION"

    conflict = await client.put(
        f"/v1/experiences/{external_id}",
        headers=headers,
        json=_upsert_body(
            external_id,
            revision=2,
            action_summary="Different action summary text.",
        ),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "EXPERIENCE_CONFLICT"

    idempotent = await client.put(
        f"/v1/experiences/{external_id}",
        headers=headers,
        json=_upsert_body(external_id, revision=2),
    )
    assert idempotent.status_code == 200

    newer = await client.put(
        f"/v1/experiences/{external_id}",
        headers=headers,
        json=_upsert_body(
            external_id,
            revision=3,
            action_summary="Updated reusable action.",
        ),
    )
    assert newer.status_code == 200
    assert newer.json()["source_revision"] == 3


@pytest.mark.asyncio
async def test_http_rollback_rejected(client: httpx.AsyncClient) -> None:
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    created = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer exp-write-token"},
        json=_upsert_body(external_id),
    )
    experience_id = created.json()["id"]
    promoted = await client.patch(
        f"/v1/experiences/{experience_id}/status",
        headers={"Authorization": "Bearer exp-admin-token"},
        json={"status": "proven", "reason": "ok"},
    )
    assert promoted.status_code == 200
    rollback = await client.patch(
        f"/v1/experiences/{experience_id}/status",
        headers={"Authorization": "Bearer exp-admin-token"},
        json={"status": "pending", "reason": "nope"},
    )
    assert rollback.status_code == 409
    assert rollback.json()["code"] == "EXPERIENCE_CONFLICT"


@pytest.mark.asyncio
async def test_feedback_idempotent_and_no_status_change(
    client: httpx.AsyncClient,
) -> None:
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    created = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer exp-write-token"},
        json=_upsert_body(external_id),
    )
    experience_id = created.json()["id"]
    event_id = f"evt-{uuid.uuid4().hex[:8]}"
    body = {
        "schema_version": "trustguard-experience-feedback-v1",
        "event_id": event_id,
        "experience_id": experience_id,
        "task_id": "task-9",
        "workflow_type": "penetration",
        "outcome": "success",
        "evidence_level": "observed",
        "notes": "worked",
        "occurred_at": None,
    }
    first = await client.post(
        f"/v1/experiences/{experience_id}/feedback",
        headers={"Authorization": "Bearer exp-feedback-token"},
        json=body,
    )
    assert first.status_code == 200, first.text
    assert first.json()["duplicated"] is False
    assert first.json()["experience_status"] == "candidate"

    second = await client.post(
        f"/v1/experiences/{experience_id}/feedback",
        headers={"Authorization": "Bearer exp-feedback-token"},
        json=body,
    )
    assert second.status_code == 200
    assert second.json()["duplicated"] is True

    got = await client.get(
        f"/v1/experiences/{experience_id}",
        headers={"Authorization": "Bearer exp-admin-token"},
    )
    assert got.json()["status"] == "candidate"
    assert got.json()["success_count"] == 1


@pytest.mark.asyncio
async def test_experience_payload_cannot_escape_gateway_workspace(
    client: httpx.AsyncClient,
) -> None:
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    created = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer exp-admin-token"},
        json=_upsert_body(
            external_id,
            visibility="workspace",
            workspace_id="default",
        ),
    )
    assert created.status_code == 200, created.text
    denied = await client.get(
        f"/v1/experiences/{created.json()['id']}",
    )
    assert denied.status_code == 200

    other_id = f"exp-{uuid.uuid4().hex[:8]}"
    rejected = await client.put(
        f"/v1/experiences/{other_id}",
        json=_upsert_body(
            other_id,
            visibility="workspace",
            workspace_id="other",
        ),
    )
    assert rejected.status_code == 403


@pytest.mark.asyncio
async def test_list_total_is_count_before_pagination(client: httpx.AsyncClient) -> None:
    headers = {"Authorization": "Bearer exp-write-token"}
    for _ in range(2):
        external_id = f"exp-{uuid.uuid4().hex[:8]}"
        response = await client.put(
            f"/v1/experiences/{external_id}",
            headers=headers,
            json=_upsert_body(external_id),
        )
        assert response.status_code == 200
    listed = await client.get(
        "/v1/experiences?limit=1",
        headers=headers,
    )
    assert len(listed.json()["items"]) == 1
    assert listed.json()["total"] == 2


@pytest.mark.asyncio
async def test_idempotency_key_is_bound_to_request(client: httpx.AsyncClient) -> None:
    headers = {
        "Authorization": "Bearer exp-write-token",
        "Idempotency-Key": f"idem-{uuid.uuid4().hex}",
    }
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    first = await client.put(
        f"/v1/experiences/{external_id}",
        headers=headers,
        json=_upsert_body(external_id),
    )
    assert first.status_code == 200
    repeated = await client.put(
        f"/v1/experiences/{external_id}",
        headers=headers,
        json=_upsert_body(external_id),
    )
    assert repeated.status_code == 200
    assert repeated.json()["id"] == first.json()["id"]

    other_id = f"exp-{uuid.uuid4().hex[:8]}"
    conflict = await client.put(
        f"/v1/experiences/{other_id}",
        headers=headers,
        json=_upsert_body(other_id),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_concurrent_revisions_cannot_overwrite_newer_revision(
    client: httpx.AsyncClient,
) -> None:
    headers = {"Authorization": "Bearer exp-write-token"}
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    first = await client.put(
        f"/v1/experiences/{external_id}",
        headers=headers,
        json=_upsert_body(external_id),
    )
    experience_id = first.json()["id"]

    responses = await asyncio.gather(
        client.put(
            f"/v1/experiences/{external_id}",
            headers=headers,
            json=_upsert_body(external_id, revision=2, action_summary="revision two"),
        ),
        client.put(
            f"/v1/experiences/{external_id}",
            headers=headers,
            json=_upsert_body(external_id, revision=3, action_summary="revision three"),
        ),
    )
    assert all(response.status_code in {200, 409} for response in responses)
    got = await client.get(
        f"/v1/experiences/{experience_id}",
        headers=headers,
    )
    assert got.json()["source_revision"] == 3
    assert got.json()["action_summary"] == "revision three"


@pytest.mark.asyncio
async def test_duplicate_feedback_event_with_different_content_conflicts(
    client: httpx.AsyncClient,
) -> None:
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    created = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer exp-write-token"},
        json=_upsert_body(external_id),
    )
    experience_id = created.json()["id"]
    event_id = f"evt-{uuid.uuid4().hex[:8]}"
    body = {
        "schema_version": "trustguard-experience-feedback-v1",
        "event_id": event_id,
        "experience_id": experience_id,
        "task_id": "task-1",
        "workflow_type": "penetration",
        "outcome": "success",
        "evidence_level": "observed",
        "notes": None,
        "occurred_at": None,
    }
    headers = {"Authorization": "Bearer exp-feedback-token"}
    first = await client.post(
        f"/v1/experiences/{experience_id}/feedback",
        headers=headers,
        json=body,
    )
    assert first.status_code == 200
    conflict = await client.post(
        f"/v1/experiences/{experience_id}/feedback",
        headers=headers,
        json={**body, "outcome": "failure"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "EXPERIENCE_CONFLICT"


@pytest.mark.asyncio
async def test_empty_scope_workflow_allowlist_remains_unrestricted(test_engine) -> None:
    registry = ScopeRegistry.from_definitions(
        {"penetration": {"knowledge_base_ids": ["kb-existing"]}}
    )
    definition = await resolve_scope_definition("penetration", registry=registry)
    assert "kb-existing" in definition.knowledge_base_ids
    assert PENETRATION_EXPERIENCE_KB_ID in definition.knowledge_base_ids
    assert definition.allowed_workflow_types == []


@pytest.mark.asyncio
async def test_physical_search_cannot_bypass_experience_scope(
    client: httpx.AsyncClient,
) -> None:
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    created = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer exp-write-token"},
        json=_upsert_body(external_id),
    )
    response = await client.post(
        "/v1/search",
        json={
            "query": "experience",
            "knowledge_base_id": created.json()["knowledge_base_id"],
        },
    )
    assert response.status_code == 403

    answered = await client.post(
        "/v1/answer",
        json={
            "query": "experience",
            "knowledge_base_id": created.json()["knowledge_base_id"],
        },
    )
    assert answered.status_code == 403

    experience_id = created.json()["id"]
    promoted = await client.patch(
        f"/v1/experiences/{experience_id}/status",
        headers={"Authorization": "Bearer exp-admin-token"},
        json={"status": "proven", "reason": "reviewed"},
    )
    assert promoted.status_code == 200
    document = await client.get(f"/v1/documents/{experience_id}")
    assert document.status_code == 403


@pytest.mark.asyncio
async def test_failed_index_deletion_stays_pending_until_recovery(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Indexer:
        delete_success = False

        async def upsert_proven(self, row) -> None:
            return None

        async def delete_proven(self, experience_id: str) -> bool:
            return self.delete_success

    indexer = Indexer()
    monkeypatch.setattr(
        "app.application.experience.get_experience_indexer",
        lambda: indexer,
    )
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    created = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer exp-write-token"},
        json=_upsert_body(external_id),
    )
    experience_id = created.json()["id"]
    promoted = await client.patch(
        f"/v1/experiences/{experience_id}/status",
        headers={"Authorization": "Bearer exp-admin-token"},
        json={"status": "proven", "reason": "reviewed"},
    )
    assert promoted.json()["index_status"] == "indexed"
    deprecated = await client.patch(
        f"/v1/experiences/{experience_id}/status",
        headers={"Authorization": "Bearer exp-admin-token"},
        json={"status": "deprecated", "reason": "outdated"},
    )
    assert deprecated.json()["index_status"] == "index_pending"

    indexer.delete_success = True
    from app.application.experience import ExperienceService

    assert await ExperienceService().recover_pending_indexes() == 1
    recovered = await client.get(
        f"/v1/experiences/{experience_id}",
        headers={"Authorization": "Bearer exp-admin-token"},
    )
    assert recovered.json()["index_status"] == "not_indexed"


@pytest.mark.asyncio
async def test_expired_proven_experience_is_deprecated_by_recovery(
    client: httpx.AsyncClient,
) -> None:
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    created = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer exp-write-token"},
        json=_upsert_body(external_id, expires_at="2000-01-01T00:00:00Z"),
    )
    experience_id = created.json()["id"]
    promoted = await client.patch(
        f"/v1/experiences/{experience_id}/status",
        headers={"Authorization": "Bearer exp-admin-token"},
        json={"status": "proven", "reason": "reviewed"},
    )
    assert promoted.json()["status"] == "proven"

    from app.application.experience import ExperienceService

    assert await ExperienceService().recover_pending_indexes() >= 1
    got = await client.get(
        f"/v1/experiences/{experience_id}",
        headers={"Authorization": "Bearer exp-admin-token"},
    )
    assert got.json()["status"] == "deprecated"
    assert got.json()["index_status"] == "not_indexed"


def test_production_experience_requires_gateway_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_APP_ENV", "prod")
    monkeypatch.setenv("RAG_QDRANT_MOCK", "false")
    monkeypatch.setenv("RAG_SEARCH_OPENSEARCH_MOCK", "false")
    monkeypatch.setenv("RAG_EXPERIENCE_ENABLED", "true")
    monkeypatch.setenv("RAG_INTERNAL_SERVICE_TOKEN", "internal-token")
    monkeypatch.setenv("RAG_GATEWAY_AUTH_ENABLED", "false")
    monkeypatch.setenv("RAG_GATEWAY_SERVICE_TOKEN", "gateway-token")
    monkeypatch.setenv(
        "RAG_RESOURCE_REF_SECRET",
        "production-resource-ref-secret-with-at-least-32-characters",
    )
    get_settings.cache_clear()

    from app.main import create_app

    with pytest.raises(ValueError, match="RAG_GATEWAY_AUTH_ENABLED"):
        create_app()
    get_settings.cache_clear()
