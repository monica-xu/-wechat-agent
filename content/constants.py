"""Module-level constants: thresholds, limits, retry policies."""

# ---- Quality gates ----
CRITIC_PASS_THRESHOLD = 0.7
CRITIC_MIN_DIMENSION = 0.4
COMPLIANCE_HARD_GATE = 0.5
MAX_REWRITES = 3

# ---- Content constraints ----
MIN_CONTENT_LENGTH = 300
MAX_CONTENT_LENGTH = 5000
MAX_TITLE_LENGTH = 64
MAX_DIGEST_LENGTH = 120

# ---- Embedding ----
SIMILARITY_THRESHOLD = 0.85
EMBEDDING_DIMENSIONS = 1536

# ---- Publish limits ----
MAX_DAILY_PUBLISH = 1
MAX_WEEKLY_PUBLISH = 5
MIN_COOLDOWN_HOURS = 18

# ---- Topic pool ----
TOPIC_POOL_MIN_SIZE = 3
TOPIC_POOL_REFRESH_HOURS = 6

# ---- Scoring weights (default, can be overridden via DB) ----
DEFAULT_SCORING_WEIGHTS = {
    "time": 0.30,
    "content": 0.40,
    "risk": 0.30,
}
PUBLISH_THRESHOLD = 0.70

# ---- Bandit ----
BANDIT_EXPLORATION_WEIGHT = 0.40  # Weight of trend_score vs ucb_score in topic selection

# ---- Timezone ----
TZ_NAME = "Asia/Shanghai"

# ---- WeChat API ----
WECHAT_API_BASE = "https://api.weixin.qq.com"
ACCESS_TOKEN_TTL_SECONDS = 5400
