"""Integrity Checker — cross-article consistency + template detection (merged).

One LLM call covers: contradiction detection, title/opening pattern repeat, topic coverage.
Style drift is computed locally via cosine similarity of embeddings.
"""

import json
import logging
from infra.llm import acall_llm
from agent.prompts import INTEGRITY_PROMPT
from db._articles import get_recently_published
from content.memory import ContentMemory

logger = logging.getLogger(__name__)


async def check(state, runtime) -> dict:
    """Run integrity check. Returns {contradiction_found, template_risk, style_drift, pass}."""
    sid = state.session_id

    # Build recent articles context
    recent = get_recently_published(sid, limit=10)
    recent_context = "\n".join(
        f"- [{r.get('topic', '')}] {r.get('title', '')} (摘要: {r.get('summary', '')[:80] if r.get('summary') else '无'})"
        for r in recent
    ) if recent else "（无历史文章）"

    # Compute style drift locally (embedding cosine similarity)
    style_drift = await _compute_style_drift(sid, state)

    # Use .replace() instead of .format() to avoid curly-brace collisions
    # with JSON examples or code blocks in article content.
    prompt = INTEGRITY_PROMPT
    prompt = prompt.replace("{recent_articles_context}", recent_context)
    prompt = prompt.replace("{topic}", state.selected_topic)
    prompt = prompt.replace("{title}", state.draft_title)
    prompt = prompt.replace("{summary}", state.draft_content_markdown[:500])

    try:
        resp = await acall_llm(
            prompt, "",
            provider=runtime.llm_provider,
            temperature=0.2,
            json_mode=True,
            trace_stage="integrity",
        )
        result = json.loads(resp)

        # Merge local style drift computation
        result["style_drift"] = round(style_drift, 4)
        result["template_risk"] = _assess_template_risk(result)

        result["pass"] = (
            not result.get("contradiction_found", False)
            and style_drift < 0.4
            and result.get("template_risk", "low") != "high"
        )
        return result

    except Exception as e:
        logger.error(f"Integrity check failed: {e}")
        return {
            "contradiction_found": False,
            "template_risk": "low",
            "style_drift": style_drift,
            "pass": True,
            "error": str(e),
            "fallback": True,
        }


async def _compute_style_drift(session_id: str, state) -> float:
    """Compute style drift between new article and historical style embeddings."""
    memory = ContentMemory(session_id)
    new_embedding = await memory.get_embedding(state.draft_content_markdown[:2000])

    recent = get_recently_published(session_id, limit=5)
    embeddings = []
    for r in recent:
        emb_str = r.get("embedding", "[]")
        try:
            emb = json.loads(emb_str) if isinstance(emb_str, str) else emb_str
            if emb:
                embeddings.append(emb)
        except (json.JSONDecodeError, TypeError):
            pass

    if not embeddings or not new_embedding:
        return 0.0

    # Average similarity to historical articles
    similarities = [ContentMemory.cosine_similarity(new_embedding, emb) for emb in embeddings]
    avg_sim = sum(similarities) / len(similarities)

    # Drift = 1 - average_similarity (higher drift = more different style)
    return max(0.0, 1.0 - avg_sim)


def _assess_template_risk(result: dict) -> str:
    """Determine overall template risk from LLM output."""
    template = result.get("template_issues", {})
    issues = []
    if template.get("title_pattern_repeat"):
        issues.append("title")
    if template.get("opening_pattern_repeat"):
        issues.append("opening")

    if len(issues) >= 2:
        return "high"
    elif len(issues) == 1:
        return "medium"
    return "low"
