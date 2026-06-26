"""Configuration routes — human mode, schedule, LLM provider."""

import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
DEFAULT_SESSION_ID = os.getenv("DEFAULT_SESSION_ID", "default")

VALID_MODES = ("dry-run", "semi-auto", "auto")
VALID_PROVIDERS = ("deepseek", "openai", "claude")


class ModeRequest(BaseModel):
    mode: str


class ScheduleRequest(BaseModel):
    publish_cron: str = "0 9 * * 1-5"
    topic_cron: str = "0 8 * * 1-5"


class ProviderRequest(BaseModel):
    provider: str


class ThresholdRequest(BaseModel):
    threshold: float = 0.70


class WeightsRequest(BaseModel):
    time: float = 0.30
    content: float = 0.40
    risk: float = 0.30


# ---- Human Mode ----

@router.get("/config/human-mode")
async def get_human_mode(session_id: str = ""):
    from db._config import RuntimeConfig
    sid = session_id or DEFAULT_SESSION_ID
    mode = await RuntimeConfig.get_human_mode(sid)
    return {"mode": mode}


@router.put("/config/human-mode")
async def set_human_mode(req: ModeRequest):
    if req.mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid mode. Valid: {VALID_MODES}")
    from db._config import RuntimeConfig
    from db._base import _db
    await RuntimeConfig.set_human_mode(DEFAULT_SESSION_ID, req.mode)
    return {"mode": req.mode, "status": "updated"}


# ---- Schedule ----

@router.get("/config/schedule")
async def get_schedule():
    from db._config import RuntimeConfig
    cron = await RuntimeConfig.get_publish_cron(DEFAULT_SESSION_ID)
    return {"publish_cron": cron}


@router.put("/config/schedule")
async def set_schedule(req: ScheduleRequest):
    from db._config import RuntimeConfig
    RuntimeConfig.set("publish_schedule_cron", req.publish_cron)
    RuntimeConfig.set("topic_refresh_cron", req.topic_cron)
    return {"publish_cron": req.publish_cron, "topic_cron": req.topic_cron, "status": "updated (restart to apply)"}


# ---- LLM Provider ----

@router.get("/config/provider")
async def get_provider():
    from db._config import RuntimeConfig
    provider = await RuntimeConfig.get_llm_provider(DEFAULT_SESSION_ID)
    return {"provider": provider}


@router.put("/config/provider")
async def set_provider(req: ProviderRequest):
    if req.provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Invalid provider. Valid: {VALID_PROVIDERS}")
    from db._config import RuntimeConfig
    RuntimeConfig.set("llm_provider", req.provider)
    return {"provider": req.provider, "status": "updated"}


# ---- Scoring Params ----

@router.get("/config/scoring")
async def get_scoring():
    from db._config import RuntimeConfig
    return {
        "threshold": await RuntimeConfig.get_publish_threshold(DEFAULT_SESSION_ID),
        "weights": await RuntimeConfig.get_scoring_weights(DEFAULT_SESSION_ID),
    }


@router.put("/config/scoring/threshold")
async def set_threshold(req: ThresholdRequest):
    if not 0.3 <= req.threshold <= 0.95:
        raise HTTPException(status_code=400, detail="Threshold must be between 0.3 and 0.95")
    from db._config import RuntimeConfig
    RuntimeConfig.set("publish_threshold", str(req.threshold))
    return {"threshold": req.threshold, "status": "updated"}


@router.put("/config/scoring/weights")
async def set_weights(req: WeightsRequest):
    total = req.time + req.content + req.risk
    if abs(total - 1.0) > 0.01:
        raise HTTPException(status_code=400, detail=f"Weights must sum to 1.0 (got {total})")
    from db._config import RuntimeConfig
    RuntimeConfig.set("scoring_weight_time", str(req.time))
    RuntimeConfig.set("scoring_weight_content", str(req.content))
    RuntimeConfig.set("scoring_weight_risk", str(req.risk))
    return {"weights": {"time": req.time, "content": req.content, "risk": req.risk}, "status": "updated"}
