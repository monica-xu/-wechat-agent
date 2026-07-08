"""Topic Agent — select the best topic + angle from candidate pool."""

import json
import re
import logging
from infra.llm import acall_llm
from agent.prompts import TOPIC_SYSTEM_PROMPT
from db._content_memory import get_active_topics, get_bandit_topics, get_topic_cooldown_days
from content.constants import BANDIT_EXPLORATION_WEIGHT

logger = logging.getLogger(__name__)


async def run_topic(state, runtime) -> dict:
    """Select topic and angle. Returns {topic, angle, source, reason, confidence}."""
    sid = state.session_id

    # Fetch candidates: bandit-scored topics + active topic pool
    bandit_topics = get_bandit_topics(sid, limit=5)
    active_topics = get_active_topics(sid, min_score=0.3)

    # Build context for LLM
    bandit_str = "\n".join(
        f"- [{t['topic']}] UCB={t.get('ucb_score', 0):.3f}, published={t.get('times_published', 0)}次, avg_read={t.get('avg_read_count', 0):.0f}"
        for t in bandit_topics
    ) if bandit_topics else "（暂无历史数据）"

    pool_str = "\n".join(
        f"- [{t['topic']}] trend={t.get('trend_score', 0):.2f}, source={t.get('source', 'auto')}"
        for t in active_topics[:10]
    ) if active_topics else "（候选池为空）"

    # Cooldown context
    cooldowns = []
    for t in active_topics[:5]:
        days = get_topic_cooldown_days(sid, t["topic"])
        cooldowns.append(f"- [{t['topic']}] 距上次发布: {days}天")
    cooldown_str = "\n".join(cooldowns) if cooldowns else "（无历史发布记录）"

    user_msg = f"""请从以下信息中选择今天发布的话题和角度：

## 历史表现（Bandit UCB 评分，越高越值得探索）
{bandit_str}

## 候选话题池
{pool_str}

## 话题冷却时间
{cooldown_str}

选择时请平衡：
- UCB高分话题（exploit，利用已验证的高表现话题）
- 长期未发布的话题（explore，探索新方向）
- 趋势热度高的新话题"""

    try:
        resp = await acall_llm(
            TOPIC_SYSTEM_PROMPT, user_msg,
            provider=runtime.llm_provider,
            temperature=0.7,
            json_mode=True,
            trace_stage="topic",
        )
        result = json.loads(resp)
        return {
            "topic": result.get("selected_topic", ""),
            "angle": result.get("selected_angle", ""),
            "source": "llm_selected",
            "reason": result.get("reason", ""),
            "confidence": result.get("confidence", 0.5),
            "alternatives": result.get("alternative_topics", []),
        }
    except Exception as e:
        logger.warning(f"Topic selection failed: {e}")
        # Fallback: pick highest UCB topic from bandit
        if bandit_topics:
            best = bandit_topics[0]
            return {
                "topic": best["topic"],
                "angle": f"深度解读：{best['topic']}",
                "source": "bandit_fallback",
                "reason": f"Fallback to highest UCB topic ({best['ucb_score']:.3f})",
                "confidence": 0.5,
            }
        return {"topic": "", "angle": "", "source": "fallback", "reason": str(e), "confidence": 0.0}


def _safe_json_parse(text: str):
    """Parse LLM JSON output, handling common formatting issues."""
    import re
    # Strip markdown code fences
    text = re.sub(r'^```(?:json)?\s*\n', '', text)
    text = re.sub(r'\n```\s*$', '', text)
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try extracting first JSON array or object
    for pattern in [r'\[[\s\S]*\]', r'\{[\s\S]*\}']:
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue
    return []


async def _extract_tensions(headlines: list[dict], provider: str) -> list[dict]:
    """Pass 1: extract cognitive tensions from news headlines.

    Returns list of {shift, conflict, statement} dicts.
    The anti-news constraint is critical: tensions must describe structural
    changes, not events. If a tension can be phrased as "what happened",
    it has failed.
    """
    if not headlines:
        return []

    news_text = "\n".join(
        f"- [{h.get('source','')}] {h.get('title','')}" for h in headlines[:10]
    )

    prompt = f"""你是认知结构分析器。从以下新闻中抽象出5条tension。

输入新闻：
{news_text}

每条tension必须包含三个字段：
- shift：底层结构发生了什么变化（禁止描述事件）
- conflict：核心冲突（旧认知 vs 新现实）
- statement：一句话判断句（可作文章开头）

❗ 反新闻约束（必须遵守）：
- tension 必须是"结构变化"，不能是事件描述
- 如果可以用"发生了什么"来说明，说明失败
- 不能包含任何时间点、动作主体、发布行为
- 不能使用以下词汇：发布、上线、财报、涨跌、宣布、突破、达成

❌ bad tension（仍是新闻改写）：
  shift: AI芯片需求上升
  conflict: 供应不足 vs 需求增长
  statement: AI芯片需求激增推动行业变革

✅ good tension（真正的结构变化）：
  shift: 计算能力从"稀缺资源"变为"默认假设"
  conflict: 硬件公司的稀缺性溢价逻辑正在瓦解
  statement: 当算力不再稀缺，芯片行业的定价权正在从硬件转向软件

输出JSON：[{{"shift":"...","conflict":"...","statement":"..."}}]"""

    try:
        from infra.llm import acall_llm
        resp = await acall_llm(prompt, "从新闻中提取结构性认知冲突。", provider=provider,
                               temperature=0.7, max_tokens=2048, json_mode=True,
                               trace_stage="extract_tensions")
        # Handle common JSON formatting issues from LLM output
        results = _safe_json_parse(resp)
        if isinstance(results, dict):
            results = results.get("tensions", results.get("items", []))
        if isinstance(results, list):
            logger.info(f"Extracted {len(results)} tensions from {len(headlines)} headlines")
            return results[:5]
        return []
    except Exception as e:
        logger.warning(f"Tension extraction failed: {e}")
        return []


async def refresh_topic_pool(session_id: str) -> None:
    """Refresh the topic pool. Branches by topic_source config."""
    import os
    from db._config import RuntimeConfig
    from db._content_memory import clear_topic_pool
    clear_topic_pool(session_id)

    source = RuntimeConfig.get("topic_source", "")
    if source == "books":
        await _generate_from_books(session_id)
    elif source == "news":
        await _generate_from_news(session_id)
    else:
        # Auto: use NewsAPI if key configured, else books mode
        if os.getenv("NEWSAPI_KEY", "").startswith("sk-") or len(os.getenv("NEWSAPI_KEY", "")) > 20:
            await _generate_from_news(session_id)
        else:
            await _generate_from_books(session_id)


async def _generate_from_books(session_id: str) -> None:
    """Book review / reading notes topic generation."""
    import os
    from infra.llm import acall_llm
    from db._content_memory import save_topic_pool
    from db._config import RuntimeConfig

    provider = os.getenv("LLM_PROVIDER", "deepseek")
    focus = RuntimeConfig.get("topic_focus", "")
    reading_list = RuntimeConfig.get("reading_list", "")
    focus_line = f"聚焦方向：{focus}。" if focus else ""

    book_hint = ""
    if reading_list and reading_list.strip():
        book_hint = f"""从以下书单中选题：
{reading_list.strip()}"""
    else:
        book_hint = "从你的知识库中推荐值得写书评/读后感的书籍。"

    prompt = f"""你是书评/读后感公众号编辑。{focus_line}

{book_hint}

步骤1：为每本书先选一个"阅读透镜"（Lens），决定今天为什么值得写：
- 现代映射：这本书解释了今天正在发生的什么？
- 认知反转：这本书的哪个观点和常识相反？
- 现实冲突：这本书的哪个判断在现实中碰壁了？
- 被误读：这本书被大众误解最深的是什么？
- 为什么今天重读：这本书在当下为什么突然重要了？

步骤2：基于选定的 Lens，生成公众号选题。

要求：
- 标题必须同时包含书名/作者 + Lens + 可争议观点
- 禁止："X读后感""从X看Y"等模板
- 禁止新闻语气、学术结构句

例（基于 Lens）：
- 现代映射：《规训与惩罚》写于1975年，描述的监视社会今天不是监狱而是社交媒体的点赞
- 认知反转：《思考快与慢》真正难学的不是理论，而是承认自己会错
- 现实冲突：为什么《原则》在硅谷被奉为圣经，在中国企业却水土不服？
- 被误读：《乌合之众》不是群众非理性的证明，而是精英引导的狂欢
- 为什么今天重读：当AI开始替你思考，《娱乐至死》的预言进入了下一阶段

输出JSON：[{{"topic":"...","reason":"基于什么Lens（20字）","category":"书评/思想","trend_score":0.8}}]"""

    try:
        resp = await acall_llm(prompt, "推荐5个书评/读后感选题。", provider=provider,
                               temperature=0.7, json_mode=True, trace_stage="topic_books")
        topics = json.loads(resp)
        if isinstance(topics, dict) and "topics" in topics:
            topics = topics["topics"]
        if isinstance(topics, list):
            save_topic_pool(session_id, topics)
            logger.info(f"Topic pool refreshed (books): {len(topics)} topics added")
    except Exception as e:
        logger.error(f"Book topic refresh failed: {e}")


async def _generate_from_news(session_id: str) -> None:
    """News-driven topic generation (existing flow)."""
    import os
    from infra.llm import acall_llm
    from db._content_memory import save_topic_pool, clear_topic_pool
    from db._config import RuntimeConfig

    provider = os.getenv("LLM_PROVIDER", "deepseek")
    focus = RuntimeConfig.get("topic_focus", "")
    if focus:
        focus_instruction = f"请聚焦在以下方向：{focus}。"
    else:
        focus_instruction = "请结合近期具体事件/产品/数据选题。"

    from tools.research import fetch_headlines
    headlines = await fetch_headlines(count=10)
    if headlines:
        headline_text = "\n".join(f"- [{h.get('source','')}] {h.get('title','')}" for h in headlines[:10])
        news_hint = f"""参考以下今日真实新闻头条，从中挑选最有话题价值的转化为中文公众号选题：

{headline_text}"""
    else:
        news_hint = "（今日新闻头条暂不可用，请基于你的知识库选题）"

    # ---- Pass 1: Extract cognitive tensions from news ----
    tensions = await _extract_tensions(headlines, provider)

    # ---- Pass 2: Convert tensions to topics ----
    focus_txt = f"聚焦方向：{focus}。" if focus else ""
    tension_text = "\n".join(
        f"{i+1}. shift: {t['shift']} | conflict: {t['conflict']} | statement: {t['statement']}"
        for i, t in enumerate(tensions)
    ) if tensions else "（无tension数据，自由选题）"

    topic_prompt = f"""你是微信公众号选题编辑。将以下认知冲突转化为可传播的中文公众号选题。{focus_txt}

{tension_text}

选题优先级（从高到低）：
1. 认知冲突（人们原本相信X vs 现实正在发生Y）
2. 生活/职业可代入场景（普通人能感同身受）
3. 轻度结构对比（A vs B）
4. 最后才允许抽象结构描述

推荐表达模板（必须含具体人/行为/场景）：
- 当{{具体角色}}开始{{具体行为}}，{{旧认知}}正在被推翻
- 为什么{{具体角色}}越{{行为}}，反而越{{反常识结果}}？
- {{具体场景}}里，{{旧规则}}正在被{{新现实}}取代
- {{具体角色}}正在用{{新行为}}重新定义{{旧概念}}

锚点分布（5个标题必须按此比例）：
- 2个「角色锚点」：以具体角色为核心（用户/打工人/管理者/投资者/父母/学生）
- 2个「场景锚点」：以具体场景为核心（面试/会议/工作/投资/购物/教育/社交）
- 1个「行为/机制锚点」：以具体行为或机制变化为核心

人类视角：至少3/5必须包含"人/你/企业/管理者/决策者"。
隐喻限制：隐喻（毒丸/强心针/游戏/战争）只能作为修饰词，不能作为主锚点。

不合格：纯抽象标题、无具体锚点的评论句、隐喻主导的标题
合格示例：
- "当35岁程序员开始教AI写代码，经验贬值还有多远？"（角色：程序员）
- "面试不再考算法了，技术招聘正在经历一场静默革命"（场景：面试）
- "联席总裁制扩张，但为什么决策反而更慢了？"（机制：决策速度）

禁止：
- 新闻标题语气（发布/上线/财报/增长/宣布/突破）
- 学术结构句（"X从A转向B结构"作为主表达）
- 纯理论表达（无场景、无主体、无人感）

落地锚点（每个标题至少包含以下之一）：
- 一个具体角色（用户/打工人/管理者/年轻人/公司/投资者/父母/学生）
- 一个具体行为（选择/放弃/裁员/跳槽/转向/学习/使用/信任）
- 一个具体场景（面试/会议/工作/投资/购物/教育/社交/家庭）

输出要求：
- 必须像"公众号标题"，而不是分析报告
- 冲突必须落地到具体的人或行为，不能漂浮在抽象层
- 必须可引发点击阅读欲望

输出JSON：[{{"topic":"...","reason":"基于哪个tension","category":"科技/商业/生活","trend_score":0.8}}]"""

    try:
        resp = await acall_llm(topic_prompt, "基于tension生成公众号选题。", provider=provider,
                               temperature=0.6, json_mode=True, trace_stage="topic_from_tension")
        topics = json.loads(resp)
        if isinstance(topics, dict) and "topics" in topics:
            topics = topics["topics"]
        if isinstance(topics, list):
            save_topic_pool(session_id, topics)
            logger.info(f"Topic pool refreshed: {len(topics)} topics added")
        else:
            logger.warning(f"Unexpected topic refresh response format: {type(topics)}")
    except Exception as e:
        logger.error(f"Topic pool refresh failed: {e}")
