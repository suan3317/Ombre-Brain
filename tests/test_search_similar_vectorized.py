import json
import random
import sqlite3

import pytest

from embedding_engine import EmbeddingEngine


def test_cosine_similarity_batch_matches_pairwise():
    random.seed(1234)
    query = [random.uniform(-1, 1) for _ in range(64)]
    vectors = [[random.uniform(-1, 1) for _ in range(64)] for _ in range(20)]

    batch = EmbeddingEngine._cosine_similarity_batch(query, vectors)
    pairwise = [EmbeddingEngine._cosine_similarity(query, vector) for vector in vectors]

    assert list(batch) == pytest.approx(pairwise, abs=1e-12)


def test_cosine_similarity_batch_handles_zero_norms():
    batch = EmbeddingEngine._cosine_similarity_batch(
        [1.0, 0.0], [[0.0, 0.0], [1.0, 0.0]]
    )
    assert list(batch) == pytest.approx([0.0, 1.0])


@pytest.mark.asyncio
async def test_vectorized_search_preserves_content_meaning_and_tie_behavior(
    tmp_path, monkeypatch
):
    buckets_dir = tmp_path / "buckets"
    buckets_dir.mkdir()
    engine = EmbeddingEngine({
        "buckets_dir": str(buckets_dir),
        "embedding": {
            "enabled": True,
            "api_key": "test-key",
            "api_format": "openai_compat",
            "base_url": "https://example.invalid/v1",
            "model": "test-model",
            "dim": 3,
        },
    })

    async def generate(_text):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(engine, "_generate_async", generate)
    now = "2026-01-01T00:00:00Z"
    with sqlite3.connect(engine.db_path) as conn:
        rows = [
            ("meaning_wins", [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]),
            ("tie_first", [0.0, 1.0, 0.0], None),
            ("tie_second", [0.0, -1.0, 0.0], None),
            ("dim_mismatch", [1.0, 0.0], None),
        ]
        for bucket_id, content, meaning in rows:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings "
                "(bucket_id, embedding, meaning_embedding, updated_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    bucket_id,
                    json.dumps(content),
                    json.dumps(meaning) if meaning is not None else None,
                    now,
                    "",
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO embeddings "
            "(bucket_id, embedding, meaning_embedding, updated_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            ("malformed", "{bad json", None, now, ""),
        )

    results = await engine.search_similar_strict("query", top_k=10)

    assert results[0][0] == "meaning_wins"
    assert results[0][1] == pytest.approx(1.0)
    assert [bucket_id for bucket_id, _ in results[1:]] == [
        "tie_first",
        "tie_second",
        "dim_mismatch",
    ]
    assert "malformed" not in dict(results)
