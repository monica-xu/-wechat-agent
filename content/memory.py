"""ContentMemory — embeddings, dedup, topic cooldown, and cross-article search."""

import json
import math
import logging
from infra.llm import aget_embedding
from db._articles import get_all_embeddings as _get_article_embeddings
from db._content_memory import (
    save_topic_memory, get_topic_memory, get_topic_cooldown_days,
    get_bandit_topics, get_active_topics,
)
from content.constants import SIMILARITY_THRESHOLD

logger = logging.getLogger(__name__)


class ContentMemory:
    """Manages cross-article content memory: embeddings, dedup, topic tracking."""

    def __init__(self, session_id: str):
        self.session_id = session_id

    async def get_embedding(self, text: str) -> list[float]:
        """Get embedding for a text snippet."""
        try:
            return await aget_embedding(text[:8000])
        except Exception as e:
            logger.warning(f"Embedding failed: {e}")
            return []

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def find_similar(self, text: str, threshold: float = 0.85, limit: int = 5) -> list[dict]:
        """Find articles with cosine similarity above threshold."""
        query_emb = await self.get_embedding(text)
        if not query_emb:
            return []

        rows = _get_article_embeddings(self.session_id)
        results = []
        for row in rows:
            stored_emb_str = row.get("embedding", "[]")
            try:
                stored_emb = json.loads(stored_emb_str) if isinstance(stored_emb_str, str) else stored_emb_str
            except (json.JSONDecodeError, TypeError):
                continue
            if not stored_emb:
                continue

            sim = self.cosine_similarity(query_emb, stored_emb)
            if sim >= threshold:
                results.append({
                    "article_id": row.get("id", ""),
                    "topic": row.get("topic", ""),
                    "summary": row.get("summary", ""),
                    "similarity": round(sim, 4),
                })

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]

    async def is_duplicate(self, text: str) -> bool:
        """Check if text is too similar to any existing article."""
        similar = await self.find_similar(text, threshold=SIMILARITY_THRESHOLD, limit=1)
        return len(similar) > 0

    async def record_article(self, state) -> None:
        """Store article in content memory with embedding."""
        summary = state.article_summary or state.draft_content_markdown[:500]
        embedding = await self.get_embedding(summary)

        key_points = await self._extract_key_points(state)

        save_topic_memory(
            session_id=state.session_id,
            topic=state.selected_topic,
            embedding=embedding,
            summary=summary,
            key_points=key_points,
            category="",
        )
        logger.info(f"Article recorded in memory: topic='{state.selected_topic}'")

    async def _extract_key_points(self, state) -> list[str]:
        """Extract key points from article for memory storage."""
        content = state.draft_content_markdown
        # Simple extraction: first sentence of each paragraph
        points = []
        for line in content.split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and len(line) > 20:
                points.append(line[:200])
        return points[:5]

    @staticmethod
    def get_cooldown(session_id: str, topic: str) -> int:
        """Days since last publish on this topic."""
        return get_topic_cooldown_days(session_id, topic)

    @staticmethod
    def get_top_topics(session_id: str, limit: int = 5) -> list[dict]:
        """Get top topics by bandit UCB score."""
        return get_bandit_topics(session_id, limit)

    @staticmethod
    def compute_topic_entropy(session_id: str) -> float:
        """Compute Shannon entropy of topic distribution. Low entropy = topic collapse risk.

        Returns 0-1 normalized entropy. < 0.3 means topics are too concentrated.
        """
        import math
        from db._content_memory import list_topic_memories
        topics = list_topic_memories(session_id)
        if len(topics) < 2:
            return 0.0

        total_published = sum(t.get("times_published", 0) for t in topics)
        if total_published == 0:
            return 1.0

        entropy = 0.0
        for t in topics:
            p = t.get("times_published", 0) / total_published
            if p > 0:
                entropy -= p * math.log2(p)

        # Normalize by max possible entropy (log2(N))
        max_entropy = math.log2(len(topics))
        if max_entropy == 0:
            return 0.0
        return entropy / max_entropy

    @staticmethod
    def should_force_exploration(session_id: str, threshold: float = 0.3) -> bool:
        """Check if topic distribution entropy is too low — forcing exploration."""
        entropy = ContentMemory.compute_topic_entropy(session_id)
        return entropy < threshold
