"""Runtime configuration CRUD (DB-backed, persists across restarts)."""

import json
from db._base import _db


class RuntimeConfig:
    """DB-backed mutable config with typed getters/setters."""

    @staticmethod
    def get(key: str, default=None):
        with _db() as conn:
            row = conn.execute(
                "SELECT value FROM runtime_config WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    @staticmethod
    def set(key: str, value) -> None:
        with _db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runtime_config (key, value, updated_at) VALUES (?, ?, datetime('now','localtime'))",
                (key, json.dumps(value) if not isinstance(value, str) else value),
            )

    @staticmethod
    async def get_human_mode(session_id: str) -> str:
        return RuntimeConfig.get("human_mode", "dry-run")

    @staticmethod
    async def set_human_mode(session_id: str, mode: str) -> None:
        if mode not in ("dry-run", "semi-auto", "auto"):
            raise ValueError(f"Invalid human mode: {mode}")
        RuntimeConfig.set("human_mode", mode)

    @staticmethod
    async def get_publish_cron(session_id: str) -> str:
        return RuntimeConfig.get("publish_schedule_cron", "0 9 * * 1-5")

    @staticmethod
    async def get_llm_provider(session_id: str) -> str:
        return RuntimeConfig.get("llm_provider", "deepseek")

    @staticmethod
    async def get_publish_threshold(session_id: str) -> float:
        return float(RuntimeConfig.get("publish_threshold", "0.70"))

    @staticmethod
    async def get_scoring_weights(session_id: str) -> dict:
        return {
            "time": float(RuntimeConfig.get("scoring_weight_time", "0.30")),
            "content": float(RuntimeConfig.get("scoring_weight_content", "0.40")),
            "risk": float(RuntimeConfig.get("scoring_weight_risk", "0.30")),
        }
