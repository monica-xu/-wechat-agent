"""APScheduler setup with DB-based job lock for idempotency."""

import os
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from db._base import _db, daily_backup

load_dotenv()

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

DEFAULT_SESSION_ID = os.getenv("DEFAULT_SESSION_ID", "default")


def start_scheduler(session_id: str = ""):
    """Register cron jobs and start the scheduler."""
    sid = session_id or DEFAULT_SESSION_ID

    # 1. Topic pool refresh: weekday mornings at 08:00
    topic_cron = os.getenv("TOPIC_REFRESH_CRON", "0 8 * * 1-5")
    scheduler.add_job(
        _refresh_topic_pool_job,
        CronTrigger.from_crontab(topic_cron, timezone="Asia/Shanghai"),
        id="topic_refresh",
        args=[sid],
        replace_existing=True,
    )

    # 2. Publish pipeline: weekday mornings at 09:00
    publish_cron = os.getenv("PUBLISH_CRON", "0 9 * * 1-5")
    scheduler.add_job(
        _run_pipeline_job,
        CronTrigger.from_crontab(publish_cron, timezone="Asia/Shanghai"),
        id="publish_pipeline",
        args=[sid],
        replace_existing=True,
    )

    # 3. Daily backup at 20:00
    scheduler.add_job(
        daily_backup,
        CronTrigger(hour=20, minute=0, timezone="Asia/Shanghai"),
        id="daily_backup",
    )

    # 4. Feedback fetch: daily at 10:00 (fetch previous day's metrics)
    scheduler.add_job(
        _feedback_fetch_job,
        CronTrigger(hour=10, minute=0, timezone="Asia/Shanghai"),
        id="feedback_fetch",
        args=[sid],
    )

    # Clean stale locks on startup
    _clean_stale_locks()

    scheduler.start()
    logger.info(f"Scheduler started with {len(scheduler.get_jobs())} jobs")


def stop_scheduler():
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


# ---- Job Lock (DB-based) ----

def _acquire_lock(job_key: str) -> bool:
    """Try to acquire a job lock. Returns True if acquired, False if already held."""
    with _db() as conn:
        existing = conn.execute(
            "SELECT started_at, finished_at FROM job_locks WHERE job_key = ?",
            (job_key,),
        ).fetchone()

        if existing:
            if not existing["finished_at"]:
                # Still running — check if stale (>2h)
                started = datetime.fromisoformat(existing["started_at"])
                if datetime.now() - started.replace(tzinfo=None) < timedelta(hours=2):
                    return False
            # Lock exists but finished or stale — re-acquire
            conn.execute(
                "UPDATE job_locks SET started_at = datetime('now','localtime'), finished_at = '' WHERE job_key = ?",
                (job_key,),
            )
        else:
            conn.execute(
                "INSERT INTO job_locks (job_key, started_at) VALUES (?, datetime('now','localtime'))",
                (job_key,),
            )
    return True


def _release_lock(job_key: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE job_locks SET finished_at = datetime('now','localtime') WHERE job_key = ?",
            (job_key,),
        )


def _clean_stale_locks() -> None:
    """Remove locks that are >24h old (zombie locks)."""
    with _db() as conn:
        conn.execute(
            "DELETE FROM job_locks WHERE finished_at = '' AND started_at < datetime('now','localtime','-24 hours')"
        )


# ---- Cron Job Functions ----

async def _refresh_topic_pool_job(session_id: str):
    """Scheduled job: refresh topic pool."""
    from content.topic import refresh_topic_pool

    job_key = datetime.now().strftime("%Y-%m-%d") + "_topic_refresh"
    if not _acquire_lock(job_key):
        logger.info("Topic refresh already running for today, skipping")
        return
    try:
        logger.info(f"[Scheduler] Refreshing topic pool for {session_id}")
        await refresh_topic_pool(session_id)
    except Exception as e:
        logger.error(f"[Scheduler] Topic refresh failed: {e}")
    finally:
        _release_lock(job_key)


async def _run_pipeline_job(session_id: str):
    """Scheduled job: execute the full publish pipeline."""
    from agent.loop import run_pipeline
    from agent.state import ArticleState, AgentRuntime
    from agent.helpers import _generate_id, _now
    from db._config import RuntimeConfig

    job_key = datetime.now().strftime("%Y-%m-%d") + "_pipeline"
    if not _acquire_lock(job_key):
        logger.info("Pipeline already running for today, skipping")
        return
    try:
        mode = await RuntimeConfig.get_human_mode(session_id)
        article_id = _generate_id()

        state = ArticleState(
            article_id=article_id,
            session_id=session_id,
            human_mode=mode,
            created_at=_now(),
        )
        runtime = AgentRuntime(
            session_id=session_id,
            article_id=article_id,
            pipeline_start_time=_now(),
            llm_provider=os.getenv("LLM_PROVIDER", "deepseek"),
        )

        logger.info(f"[Scheduler] Starting pipeline: article={article_id}, mode={mode}")
        state = await run_pipeline(state, runtime)
        logger.info(f"[Scheduler] Pipeline complete: article={article_id}, stage={state.stage}")

    except Exception as e:
        logger.error(f"[Scheduler] Pipeline failed: {e}")
    finally:
        _release_lock(job_key)


async def _feedback_fetch_job(session_id: str):
    """Scheduled job: fetch WeChat metrics for published articles."""
    from content.feedback import run_feedback_loop

    job_key = datetime.now().strftime("%Y-%m-%d") + "_feedback"
    if not _acquire_lock(job_key):
        logger.info("Feedback fetch already running for today, skipping")
        return
    try:
        logger.info(f"[Scheduler] Running feedback loop for {session_id}")
        await run_feedback_loop(session_id)
    except Exception as e:
        logger.error(f"[Scheduler] Feedback loop failed: {e}")
    finally:
        _release_lock(job_key)
