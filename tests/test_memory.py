from pathlib import Path
from unittest.mock import patch

import pytest

from kevin.memory import (
    MEM0_COLLECTION,
    OPENAI_EMBEDDING_DIMENSIONS,
    Mem0MemoryStore,
    Mem0StoreError,
)


class _FakeMemory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def add(self, *args, **kwargs):
        self.calls.append(("add", args, kwargs))
        return {"results": []}

    async def search(self, *args, **kwargs):
        self.calls.append(("search", args, kwargs))
        return {
            "results": [
                {"memory": "Likes cooperative games"},
                {"memory": " Likes   cooperative games "},
                {"memory": "Has a dog named Pepper"},
            ]
        }

    async def get_all(self, *args, **kwargs):
        self.calls.append(("get_all", args, kwargs))
        return {"results": [{"memory": "Prefers concise answers"}]}

    async def delete_all(self, *args, **kwargs):
        self.calls.append(("delete_all", args, kwargs))
        return {"message": "Memories deleted successfully!"}


def _store(tmp_path: Path) -> Mem0MemoryStore:
    return Mem0MemoryStore(
        "test-key",
        tmp_path / "mem0",
        "gpt-4.1-mini",
        "text-embedding-3-small",
    )


async def test_mem0_start_uses_local_qdrant_and_separate_openai_models(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    fake = _FakeMemory()
    with patch("kevin.memory.AsyncMemory.from_config", return_value=fake) as from_config:
        await store.start()

    config = from_config.call_args.args[0]
    assert store.ready is True
    assert config["vector_store"]["provider"] == "qdrant"
    assert config["vector_store"]["config"] == {
        "path": str(tmp_path / "mem0" / "qdrant"),
        "collection_name": MEM0_COLLECTION,
        "embedding_model_dims": OPENAI_EMBEDDING_DIMENSIONS,
        "on_disk": True,
    }
    assert config["history_db_path"] == ":memory:"
    assert config["llm"]["config"]["model"] == "gpt-4.1-mini"
    assert config["llm"]["config"]["store"] is False
    assert config["embedder"]["config"]["model"] == "text-embedding-3-small"


async def test_mem0_operations_are_scoped_to_discord_server_and_member(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    fake = _FakeMemory()
    store._memory = fake

    await store.add_member_messages(
        10, 20, "Alex", ["I like co-op games", "I have a dog named Pepper"]
    )
    notes = await store.search_member(10, 20, "what games do I like?", limit=6)
    listed = await store.list_member(10, 20)
    await store.forget_member(10, 20)
    await store.forget_guild(10)

    assert notes == ["Likes cooperative games", "Has a dog named Pepper"]
    assert listed == ["Prefers concise answers"]

    add_call = fake.calls[0]
    assert add_call[0] == "add"
    assert add_call[1][0] == [
        {"role": "user", "content": "I like co-op games"},
        {"role": "user", "content": "I have a dog named Pepper"},
    ]
    assert add_call[2]["user_id"] == "discord:10:20"
    assert add_call[2]["agent_id"] == "kevin-discord:10"
    assert add_call[2]["metadata"]["discord_user_id"] == "20"

    assert fake.calls[1] == (
        "search",
        ("what games do I like?",),
        {
            "top_k": 6,
            "filters": {
                "user_id": "discord:10:20",
                "agent_id": "kevin-discord:10",
            },
            "threshold": 0.1,
            "rerank": False,
        },
    )
    assert fake.calls[3][2] == {
        "user_id": "discord:10:20",
        "agent_id": "kevin-discord:10",
    }
    assert fake.calls[4][2] == {"agent_id": "kevin-discord:10"}


async def test_mem0_operations_fail_closed_before_start(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(Mem0StoreError, match="not available"):
        await store.search_member(10, 20, "hello")
