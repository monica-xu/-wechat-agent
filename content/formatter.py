"""Formatter — convert Markdown to WeChat-compatible HTML."""

import json
import logging
import re
from infra.llm import acall_llm
from agent.prompts import FORMATTER_PROMPT

logger = logging.getLogger(__name__)


async def run_formatter(state, runtime) -> dict:
    """Convert markdown to WeChat HTML. Returns {formatted_title, formatted_html, article_summary}."""
    user_msg = f"""文章标题：{state.draft_title}

文章正文（Markdown）：
{state.draft_content_markdown}"""

    try:
        resp = await acall_llm(
            FORMATTER_PROMPT, user_msg,
            provider=runtime.llm_provider,
            temperature=0.2,
            max_tokens=4096,
            json_mode=True,
            trace_stage="formatter",
        )
        result = json.loads(resp)
        return {
            "formatted_title": result.get("formatted_title", state.draft_title[:64]),
            "formatted_html": result.get("formatted_html", _basic_html(state)),
            "article_summary": result.get("article_summary", state.draft_content_markdown[:120]),
        }
    except Exception as e:
        logger.warning(f"Formatter LLM call failed, using basic HTML: {e}")
        return {
            "formatted_title": state.draft_title[:64],
            "formatted_html": _basic_html(state),
            "article_summary": state.draft_content_markdown[:120],
        }


def _basic_html(state) -> str:
    """Fallback: basic Markdown-to-HTML conversion."""
    content = state.draft_content_markdown
    # Simple conversions
    content = re.sub(r'^### (.+)$', r'<h3>\1</h3>', content, flags=re.MULTILINE)
    content = re.sub(r'^## (.+)$', r'<h2>\1</h2>', content, flags=re.MULTILINE)
    content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
    content = re.sub(r'\n\n', '</section>\n<section>', content)
    return f"<section>{content}</section>"
