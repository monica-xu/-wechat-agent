"""Narrative Module — Shape selection + prompt assembly.

Entry point for the Narrative layer. Selects a Shape for each article
and generates the prompt injection that guides the writer on structural
unfolding, camera flow, and metaphor.

Persona and Narrative are orthogonal:
  - Persona = who is speaking (tone, vocabulary, rhetorical preference)
  - Narrative = how the article unfolds (structure, perspective, rhythm)

Both are selected independently and injected as separate blocks.
"""

import random
from content.narrative.shapes import SHAPES

_SHAPE_KEYS = list(SHAPES.keys())


def get_next_shape(session_id: str) -> dict:
    """Uniform random selection from all 8 Shapes.

    Uses simple random selection — no distribution weighting in Sprint A.
    Author-specific distributions will be learned from data in Sprint C/D.
    """
    from db._config import RuntimeConfig

    # Avoid immediate repeat for variety (not for author-matching)
    last_key = RuntimeConfig.get("last_narrative_shape", "")
    available = [k for k in _SHAPE_KEYS if k != last_key]
    if not available:
        available = _SHAPE_KEYS

    selected = random.choice(available)
    RuntimeConfig.set("last_narrative_shape", selected)
    return {"key": selected, **SHAPES[selected]}


def assemble_narrative_prompt(shape: dict) -> str:
    """Generate the Narrative injection block for the writer system prompt.

    Contains the Shape definition (opening→turning→expansion→ending),
    camera_flow, and metaphor_hint. Distance is NOT injected separately —
    it flows naturally from the Shape.
    """
    if not shape:
        return ""

    camera = " → ".join(shape.get("camera_flow", []))
    if not camera:
        camera = "自然流动，不强制"

    return f"""
【叙事方式：{shape['name']}】

{shape['description']}

全文推进方式：
- 开头：{shape['opening']}
- 转场：{shape['turning_point']}
- 展开：{shape['expansion']}
- 收束：{shape['ending']}

视角流：{camera}
（在不同阶段自然切换视角。close = 第一人称/近距感受，medium = 具体人物/场景，far = 时代/结构/抽象俯瞰。不强制每段切换——在叙事需要时流动。）

{shape['metaphor_hint']}
"""
