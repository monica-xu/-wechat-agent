"""Agent pipeline routes — manual trigger and status queries."""

import os
from fastapi import APIRouter, Query
from pydantic import BaseModel

router = APIRouter()
DEFAULT_SESSION_ID = os.getenv("DEFAULT_SESSION_ID", "default")


class TriggerRequest(BaseModel):
    mode: str = "dry-run"  # dry-run | semi-auto | auto
    session_id: str = ""
    force: bool = False
    topic: str = ""   # optional manual topic override
    angle: str = ""   # optional manual angle
    seed_text: str = ""  # optional user draft for co-writing mode
    co_writing_mode: str = "polish"  # polish | expand


@router.post("/pipeline/trigger")
async def trigger_pipeline(req: TriggerRequest):
    """Manually trigger the publish pipeline."""
    from agent.loop import run_pipeline
    from agent.state import ArticleState, AgentRuntime
    from agent.helpers import _generate_id, _now

    sid = req.session_id or DEFAULT_SESSION_ID
    article_id = _generate_id()

    state = ArticleState(
        article_id=article_id,
        session_id=sid,
        human_mode=req.mode if req.mode in ("dry-run", "semi-auto", "auto") else "dry-run",
        mode_override="force" if req.force else "normal",
        created_at=_now(),
    )
    # Manual topic override (optional) — set on state before pipeline runs
    if req.topic and req.topic.strip():
        state.topic = req.topic.strip()
        state.angle = req.angle.strip() if req.angle else ""
    # Seed text for co-writing mode (optional) — bypasses research
    if req.seed_text and req.seed_text.strip():
        state.seed_text = req.seed_text.strip()
        state.co_writing_mode = req.co_writing_mode if req.co_writing_mode in ("polish", "expand") else "polish"
    runtime = AgentRuntime(
        session_id=sid,
        article_id=article_id,
        pipeline_start_time=_now(),
        llm_provider=os.getenv("LLM_PROVIDER", "deepseek"),
    )

    state = await run_pipeline(state, runtime)

    return {
        "article_id": article_id,
        "final_stage": state.stage,
        "selected_topic": state.selected_topic,
        "draft_title": state.draft_title,
        "critic_score": state.critic_primary_score,
        "wechat_draft_id": state.wechat_draft_id,
        "wechat_publish_id": state.wechat_publish_id,
        "co_writing": bool(state.seed_text and state.seed_text.strip()),
        "skipped": state.co_writing_skipped,
        "trace": state.trace[-10:],  # Last 10 trace entries
    }


@router.get("/pipeline/status")
async def pipeline_status(session_id: str = Query(default="")):
    """Get current pipeline status and last run info."""
    from db._publish_log import list_publish_logs
    from db._articles import count_published_today, count_published_this_week
    from db._content_memory import get_active_topics

    sid = session_id or DEFAULT_SESSION_ID
    logs = list_publish_logs(sid, limit=5)
    topics = get_active_topics(sid)

    return {
        "session_id": sid,
        "published_today": count_published_today(sid),
        "published_this_week": count_published_this_week(sid),
        "active_topics": len(topics),
        "candidate_topics": [
            {"topic": t.get("topic", ""), "trend_score": t.get("trend_score", 0)}
            for t in topics[:10]
        ],
        "last_runs": [
            {
                "article_id": log.get("article_id"),
                "final_stage": log.get("final_stage"),
                "critic_score": log.get("critic_overall_score"),
                "created_at": log.get("created_at"),
            }
            for log in logs
        ],
    }


@router.get("/pipeline/history")
async def pipeline_history(session_id: str = Query(default=""), limit: int = Query(default=20)):
    """Get pipeline run history."""
    from db._publish_log import list_publish_logs
    sid = session_id or DEFAULT_SESSION_ID
    logs = list_publish_logs(sid, limit=limit)
    return {"history": [dict(log) for log in logs]}


# ---- Kill Switch ----

@router.post("/system/kill")
async def kill_switch_activate():
    """Activate kill switch — immediately blocks all pipeline runs."""
    from agent.loop import activate_kill_switch
    activate_kill_switch()
    return {"status": "kill_switch_active", "message": "All pipeline runs blocked. Use /api/system/resume to restore."}


@router.post("/system/resume")
async def kill_switch_deactivate():
    """Deactivate kill switch — resume normal operations."""
    from agent.loop import deactivate_kill_switch
    deactivate_kill_switch()
    return {"status": "operations_resumed", "message": "Pipeline runs restored."}


@router.post("/pipeline/refresh-pool")
async def refresh_pool(session_id: str = ""):
    """Manually refresh the topic candidate pool."""
    from content.topic import refresh_topic_pool
    sid = session_id or DEFAULT_SESSION_ID
    await refresh_topic_pool(sid)
    return {"status": "refreshed", "session_id": sid}


@router.get("/system/health")
async def system_health():
    """Full system health check including drift detection."""
    from agent.loop import KILL_SWITCH_ACTIVE
    from db._articles import count_published_today, count_published_this_week
    from db._content_memory import get_active_topics
    from content.memory import ContentMemory
    from db._config import RuntimeConfig
    import json

    sid = "default"
    entropy = ContentMemory.compute_topic_entropy(sid)

    # Load last drift state
    drift_raw = RuntimeConfig.get("last_drift_state", "{}")
    try:
        drift_state = json.loads(drift_raw) if isinstance(drift_raw, str) else drift_raw
    except (json.JSONDecodeError, TypeError):
        drift_state = {}

    return {
        "kill_switch_active": KILL_SWITCH_ACTIVE,
        "published_today": count_published_today(sid),
        "published_this_week": count_published_this_week(sid),
        "active_topics": len(get_active_topics(sid)),
        "topic_entropy": round(entropy, 4),
        "entropy_warning": entropy < 0.3,
        "drift": {
            "state": drift_state,
            "alerts": drift_state.get("alerts", []),
            "alert_count": len(drift_state.get("alerts", [])),
        },
    }
