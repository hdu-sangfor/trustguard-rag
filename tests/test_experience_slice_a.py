"""Unit tests for Experience Slice A authority rules."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.domain.experience import ExperienceStatus, is_status_transition_allowed
from app.settings import get_settings


@pytest.fixture(autouse=True)
def experience_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_EXPERIENCE_ENABLED", "true")
    monkeypatch.setenv("RAG_EXPERIENCE_AUTH_ENABLED", "true")
    monkeypatch.setenv(
        "RAG_EXPERIENCE_TOKENS_JSON",
        json.dumps(
            {
                "exp-write-token": ["rag.experience.write"],
                "exp-feedback-token": ["rag.experience.feedback"],
                "exp-admin-token": [
                    "rag.experience.write",
                    "rag.experience.feedback",
                    "rag.experience.admin",
                ],
            }
        ),
    )
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
async def test_upsert_requires_write_token(client: httpx.AsyncClient) -> None:
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    missing = await client.put(
        f"/v1/experiences/{external_id}",
        json=_upsert_body(external_id),
    )
    assert missing.status_code == 401

    forbidden = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer exp-feedback-token"},
        json=_upsert_body(external_id),
    )
    assert forbidden.status_code == 403


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
async def test_write_cannot_patch_status_admin_can(
    client: httpx.AsyncClient,
) -> None:
    external_id = f"exp-{uuid.uuid4().hex[:8]}"
    created = await client.put(
        f"/v1/experiences/{external_id}",
        headers={"Authorization": "Bearer exp-write-token"},
        json=_upsert_body(external_id),
    )
    experience_id = created.json()["id"]

    denied = await client.patch(
        f"/v1/experiences/{experience_id}/status",
        headers={"Authorization": "Bearer exp-write-token"},
        json={"status": "proven", "reason": "self promote"},
    )
    assert denied.status_code == 403

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
