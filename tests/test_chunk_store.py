"""分块存储测试。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.stores.chunk_store import ChunkStore


@pytest.mark.asyncio
async def test_replace_for_documents_removes_stale_chunks_atomically(
    test_engine: AsyncEngine,
) -> None:
    store = ChunkStore()
    first_document_id = str(uuid4())
    second_document_id = str(uuid4())
    old_first_id = str(uuid4())
    old_second_id = str(uuid4())
    await store.create_many(
        [
            {
                "id": old_first_id,
                "document_id": first_document_id,
                "chunk_index": 0,
                "text": "旧分块一",
                "token_count": 4,
            },
            {
                "id": old_second_id,
                "document_id": second_document_id,
                "chunk_index": 0,
                "text": "旧分块二",
                "token_count": 4,
            },
        ]
    )
    new_first_id = str(uuid4())

    await store.replace_for_documents(
        {
            first_document_id: [
                {
                    "id": new_first_id,
                    "document_id": first_document_id,
                    "chunk_index": 0,
                    "text": "新的 tokenizer 分块",
                    "token_count": 6,
                }
            ]
        }
    )

    first_rows = await store.list_for_document(first_document_id)
    second_rows = await store.list_for_document(second_document_id)
    assert [row.id for row in first_rows] == [new_first_id]
    assert first_rows[0].token_count == 6
    assert [row.id for row in second_rows] == [old_second_id]
