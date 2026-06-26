"""Unified LLM client: supports DeepSeek, OpenAI, Claude. Includes embedding API."""

import os
import json
import time
import logging
import asyncio
from typing import AsyncGenerator

from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Provider configs
PROVIDER_CONFIG = {
    "deepseek": {
        "api_key": os.getenv("DEEPSEEK_API_KEY", ""),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": "deepseek-v4-pro",
    },
    "openai": {
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "base_url": None,
        "model": "gpt-4o",
    },
    "claude": {
        "api_key": os.getenv("CLAUDE_API_KEY", ""),
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-20250514",
    },
}

EMBEDDING_CONFIG = {
    "openai": {
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "base_url": None,
        "model": os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
    },
    "deepseek": {
        "api_key": os.getenv("DEEPSEEK_API_KEY", ""),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": "deepseek-v4-pro",
    },
}

TRACE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "analysis_trace.jsonl")


def _get_client(provider: str) -> AsyncOpenAI:
    cfg = PROVIDER_CONFIG.get(provider, PROVIDER_CONFIG["deepseek"])
    kwargs = {"api_key": cfg["api_key"]}
    if cfg["base_url"]:
        kwargs["base_url"] = cfg["base_url"]
    return AsyncOpenAI(**kwargs)


def _get_model(provider: str) -> str:
    return PROVIDER_CONFIG.get(provider, PROVIDER_CONFIG["deepseek"])["model"]


async def acall_llm(
    system_prompt: str,
    user_message: str,
    provider: str = "deepseek",
    temperature: float = 0.7,
    max_tokens: int = 4096,
    json_mode: bool = False,
    trace_stage: str = "",
) -> str:
    """Async LLM call with retry (exponential backoff). Returns response text."""
    client = _get_client(provider)
    model = _get_model(provider)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if json_mode and provider != "claude":
        kwargs["response_format"] = {"type": "json_object"}

    t_start = time.time()
    last_error = None

    for attempt in range(3):
        try:
            resp = await client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            elapsed = int((time.time() - t_start) * 1000)
            _log_trace(trace_stage, provider, model, len(system_prompt) + len(user_message),
                       len(content), elapsed, success=True)
            return content

        except Exception as e:
            last_error = e
            logger.warning(f"LLM call attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
            continue

    elapsed = int((time.time() - t_start) * 1000)
    _log_trace(trace_stage, provider, model, len(system_prompt) + len(user_message),
               0, elapsed, success=False, error=str(last_error))

    # ---- Fallback routing ----
    # If primary provider fails, try fallback provider (cheaper/more available model)
    fallback_provider = _get_fallback_provider(provider)
    if fallback_provider and fallback_provider != provider:
        logger.warning(f"Primary provider '{provider}' failed, falling back to '{fallback_provider}'")
        try:
            fallback_client = _get_client(fallback_provider)
            fallback_model = _get_model(fallback_provider)
            kwargs["model"] = fallback_model
            resp = await fallback_client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            elapsed = int((time.time() - t_start) * 1000)
            _log_trace(trace_stage, fallback_provider, fallback_model,
                       len(system_prompt) + len(user_message), len(content), elapsed,
                       success=True, error=f"Fallback from {provider}")
            return content
        except Exception as fb_e:
            logger.error(f"Fallback provider '{fallback_provider}' also failed: {fb_e}")

    raise RuntimeError(f"LLM call failed after 3 attempts + fallback: {last_error}")


async def acall_llm_stream(
    system_prompt: str,
    user_message: str,
    provider: str = "deepseek",
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> AsyncGenerator[str, None]:
    """Async streaming LLM call."""
    client = _get_client(provider)
    model = _get_model(provider)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )

    async for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            yield delta.content


async def aget_embedding(text: str, provider: str = "") -> list[float]:
    """Get text embedding. Uses EMBEDDING_PROVIDER from env, falls back to LLM provider."""
    emb_provider = provider or os.getenv("EMBEDDING_PROVIDER", "openai")
    cfg = EMBEDDING_CONFIG.get(emb_provider, EMBEDDING_CONFIG["openai"])

    kwargs = {"api_key": cfg["api_key"]}
    if cfg.get("base_url"):
        kwargs["base_url"] = cfg["base_url"]

    client = AsyncOpenAI(**kwargs)
    model = cfg["model"]

    for attempt in range(3):
        try:
            resp = await client.embeddings.create(model=model, input=text)
            return resp.data[0].embedding
        except Exception as e:
            logger.warning(f"Embedding attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)

    logger.error("Embedding failed after 3 attempts")
    return []


def _get_fallback_provider(primary: str) -> str:
    """Return a fallback provider if primary fails.

    Priority order: configured LLM_PROVIDER → deepseek (cheapest/most available)
    Falls back to any provider with a configured API key.
    """
    # If primary is already the cheapest, try openai if configured
    fallback_chain = {
        "claude": "deepseek",
        "openai": "deepseek",
        "deepseek": "",  # No fallback — DeepSeek is the last line
    }
    fb = fallback_chain.get(primary, "")
    if fb:
        cfg = PROVIDER_CONFIG.get(fb, {})
        if cfg.get("api_key"):
            return fb
    return ""


def _log_trace(stage: str, provider: str, model: str, prompt_len: int,
               response_len: int, elapsed_ms: int, success: bool, error: str = ""):
    """Append one line to analysis_trace.jsonl."""
    try:
        entry = {
            "stage": stage,
            "provider": provider,
            "model": model,
            "prompt_len": prompt_len,
            "response_len": response_len,
            "elapsed_ms": elapsed_ms,
            "success": success,
            "error": error,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        os.makedirs(os.path.dirname(TRACE_PATH), exist_ok=True)
        with open(TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
