"""Content memory query tools."""

from content.memory import ContentMemory


async def find_similar_articles(text: str, threshold: float = 0.85) -> list:
    """Find historically similar articles."""
    memory = ContentMemory("default")
    return await memory.find_similar(text, threshold=threshold)


async def get_cooldown(topic: str) -> dict:
    """Get topic cooldown in days."""
    days = ContentMemory.get_cooldown("default", topic)
    return {"topic": topic, "cooldown_days": days}
