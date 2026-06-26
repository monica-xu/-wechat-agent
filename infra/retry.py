"""Generic retry wrapper with backoff and fallback strategies."""

import asyncio
import logging
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)


class RetryableError(Exception):
    """Error that should trigger a retry."""
    pass


class NonRetryableError(Exception):
    """Error that should NOT be retried."""
    pass


RETRY_POLICY = {
    "topic":      {"max_retries": 1, "backoff_seconds": [2],    "fallback": "default_topic"},
    "research":   {"max_retries": 1, "backoff_seconds": [2],    "fallback": "topic_only"},
    "writer":     {"max_retries": 2, "backoff_seconds": [2, 5], "fallback": "fail"},
    "critic":     {"max_retries": 0, "backoff_seconds": [],     "fallback": "pass_warning"},
    "publisher":  {"max_retries": 1, "backoff_seconds": [5],    "fallback": "draft_only"},
    "integrity":  {"max_retries": 1, "backoff_seconds": [2],    "fallback": "pass_warning"},
    "feedback":   {"max_retries": 1, "backoff_seconds": [3],    "fallback": "skip"},
}


async def run_with_retry(
    stage_name: str,
    fn: Callable[..., Awaitable],
    *args,
    **kwargs,
):
    """Wrap an async function with retry and fallback logic.

    Uses RETRY_POLICY[stage_name] for max_retries, backoff, and fallback strategy.
    RetryableError triggers retry; NonRetryableError skips directly to fallback.
    """
    policy = RETRY_POLICY.get(stage_name, {"max_retries": 1, "backoff_seconds": [2], "fallback": "fail"})
    max_retries = policy["max_retries"]
    backoffs = policy["backoff_seconds"]
    fallback = policy["fallback"]

    last_error = None

    for attempt in range(max_retries + 1):
        try:
            result = await fn(*args, **kwargs)
            return result
        except NonRetryableError as e:
            logger.warning(f"[{stage_name}] Non-retryable error, applying fallback '{fallback}': {e}")
            return _apply_fallback(fallback, str(e))
        except RetryableError as e:
            last_error = e
            if attempt < max_retries:
                delay = backoffs[min(attempt, len(backoffs) - 1)]
                logger.info(f"[{stage_name}] Retry {attempt + 1}/{max_retries} after {delay}s: {e}")
                await asyncio.sleep(delay)
            else:
                logger.error(f"[{stage_name}] All {max_retries + 1} attempts exhausted: {e}")
                return _apply_fallback(fallback, str(e))
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = backoffs[min(attempt, len(backoffs) - 1)]
                logger.info(f"[{stage_name}] Unexpected error retry {attempt + 1}/{max_retries} after {delay}s: {e}")
                await asyncio.sleep(delay)
            else:
                logger.error(f"[{stage_name}] All attempts exhausted on unexpected error: {e}")
                return _apply_fallback(fallback, str(e))

    return _apply_fallback(fallback, str(last_error))


def _apply_fallback(fallback: str, error_msg: str):
    """Return a fallback result dict."""
    return {
        "status": "fallback",
        "fallback": fallback,
        "error": error_msg,
    }
