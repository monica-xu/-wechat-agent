"""ArticleState and AgentRuntime dataclasses — the single source of truth for pipeline state."""

from dataclasses import dataclass, field


@dataclass
class ArticleState:
    """Mutable state for one article pipeline run. 5 macro-stages."""

    # -- Identity --
    article_id: str = ""
    session_id: str = ""

    # -- Macro-stage control --
    stage: str = "decide"
    # Valid: decide | generate | review | publish | feedback | done | skip | fail

    # -- Sub-stage trace (for GENERATE observability) --
    generate_trace: dict = field(default_factory=lambda: {
        "topic": {},
        "research": {},
        "writer": {},
    })

    # -- Review trace --
    review_trace: dict = field(default_factory=lambda: {
        "primary": {},
        "compliance": {},
        "adversarial": {},
        "integrity": {},
    })

    # -- POLICY_DECIDE output --
    should_publish: bool = True
    publish_score: float = 0.0
    time_score: float = 0.0
    content_score: float = 0.0
    risk_score: float = 0.0
    mode_override: str = "normal"  # normal | skip | force
    decide_reason: str = ""

    # -- GENERATE: Persona --
    persona_key: str = ""  # analytical | narrative | contrarian
    persona_config: dict = field(default_factory=dict)

    # -- GENERATE: Topic --
    topic: str = ""          # user input override (optional)
    angle: str = ""          # user input angle (optional)
    selected_topic: str = "" # final execution topic (always set)
    selected_angle: str = ""
    topic_source: str = ""   # auto | pool | trending | manual

    # -- GENERATE: Research --
    research_data: str = ""
    research_sources: list = field(default_factory=list)

    # -- GENERATE: Writer --
    draft_title: str = ""
    draft_content_markdown: str = ""
    rewrite_count: int = 0
    max_rewrites: int = 3

    # -- REVIEW: Critic scores --
    critic_primary_score: float = 0.0
    critic_primary_dimensions: dict = field(default_factory=dict)
    critic_compliance_score: float = 1.0
    critic_adversarial_score: float = 0.0
    critic_adversarial_issues: list = field(default_factory=list)
    critic_feedback: str = ""
    critic_rewrite_instructions: str = ""

    # -- REVIEW: Integrity --
    integrity_style_drift: float = 0.0
    integrity_contradiction: bool = False
    integrity_template_risk: str = "low"
    integrity_pass: bool = True

    # -- PUBLISH: Format --
    formatted_title: str = ""
    formatted_html: str = ""
    article_summary: str = ""
    cover_image_url: str = ""

    # -- PUBLISH: WeChat --
    wechat_draft_id: str = ""
    wechat_publish_id: str = ""
    publish_error: str = ""

    # -- Human mode --
    human_mode: str = "dry-run"  # dry-run | semi-auto | auto
    requires_approval: bool = False
    human_approved: bool = False
    human_rejected: bool = False

    # -- Timing --
    created_at: str = ""
    updated_at: str = ""

    # -- Trace --
    trace: list = field(default_factory=list)
    llm_call_count: int = 0
    macro_stages_completed: list = field(default_factory=list)


@dataclass
class AgentRuntime:
    """Carries cross-stage context, tool call history, and LLM usage stats.

    Pattern: mirrors ai-invest-agent's AgentRuntime — passed through the pipeline
    as a mutable reference, populated by async fetchers, read by context builders.
    """

    session_id: str = ""
    article_id: str = ""
    called_tools: set = field(default_factory=set)
    tool_failures: int = 0
    llm_provider: str = "deepseek"
    llm_total_tokens: int = 0
    llm_call_count: int = 0
    pipeline_start_time: str = ""
    pipeline_elapsed_ms: int = 0
