"""Persona Layer — multi-perspective writing to prevent style convergence.

Three personas rotate each publish cycle. Each persona injects distinct
tone, structure, and rhetorical preferences into the writer prompt.

Without this, the system converges to a single "average voice" over time.
"""

import random

PERSONAS = {
    "analytical": {
        "name": "深度分析师",
        "description": "理性、数据驱动、逻辑严密",
        "tone": "冷静客观，用事实和数据说话，避免情绪化表达",
        "opening_style": "从一组数据或一个反常识的事实切入",
        "structure": "问题→分析→证据→结论",
        "voice_sample": "让我们先看一组数据...",
        "forbidden": ["震惊", "必须看", "彻底改变", "颠覆你的认知"],
    },
    "narrative": {
        "name": "叙事者",
        "description": "故事驱动、场景化、情感共鸣",
        "tone": "用故事和场景让抽象概念变得可感知，可以适当感性",
        "opening_style": "从一个具体的人物故事或生活场景切入",
        "structure": "故事→问题→洞察→回归故事",
        "voice_sample": "小王早上打开电脑，发现...",
        "forbidden": ["综上所述", "总而言之", "在当今社会"],
    },
    "contrarian": {
        "name": "反常识思考者",
        "description": "挑战常规认知、提供对立视角",
        "tone": "犀利但不刻薄，挑战读者的惯性思维，提供被忽略的视角",
        "opening_style": "从一个被普遍接受但可能是错误的观点切入",
        "structure": "常识→质疑→证据→新视角",
        "voice_sample": "你可能一直以为X是对的，但...",
        "forbidden": ["权威专家指出", "众所周知", "不言而喻"],
    },
}

# Rotation order ensures even distribution
_PERSONA_KEYS = list(PERSONAS.keys())


def get_next_persona(session_id: str) -> dict:
    """Select the next persona in rotation. Returns persona config dict.

    Uses DB to track which persona was last used, ensuring round-robin rotation.
    Falls back to random if DB unavailable.
    """
    from db._config import RuntimeConfig
    last_key = RuntimeConfig.get("last_persona", "")
    try:
        idx = _PERSONA_KEYS.index(last_key) if last_key in _PERSONA_KEYS else -1
    except ValueError:
        idx = -1
    next_idx = (idx + 1) % len(_PERSONA_KEYS)
    next_key = _PERSONA_KEYS[next_idx]
    RuntimeConfig.set("last_persona", next_key)
    return {"key": next_key, **PERSONAS[next_key]}


def get_persona_prompt_injection(persona: dict) -> str:
    """Generate the tone/style injection block for the writer system prompt."""
    return f"""
【本次写作人格：{persona['name']}】
- 风格定位：{persona['description']}
- 语气要求：{persona['tone']}
- 开头方式：{persona['opening_style']}
- 结构偏好：{persona['structure']}
- 语感参考："{persona['voice_sample']}"
- 禁用词汇：{', '.join(persona['forbidden'])}
"""
