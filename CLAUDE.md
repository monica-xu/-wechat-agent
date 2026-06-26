# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

WeChat Article Agent — a **Content Intelligence Agent System** (not merely a "write-and-post" script). It autonomously decides *whether* to publish, *what* topic to write about, *how* to write it (via rotating personas), and *when* to skip low-quality output. The system is designed to self-stabilize over multi-week operation through global drift control and bandit-based topic learning.

Key capabilities beyond basic automation:
- **Autonomous gating**: The policy engine can decide "don't publish today" if content/risk scores are low — it's not a blind timer→write→post pipeline.
- **Style diversity**: Three rotating writing personas prevent convergence to a single "average AI voice."
- **Self-correction**: Global drift detection auto-escalates to semi-auto mode or forces topic exploration when quality degrades.
- **Quality-weighted learning**: Bandit topic scoring uses shaped rewards (read×0.4 + share×0.3 + completion×0.3), not raw click counts, to prevent clickbait optimization.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server (FastAPI + uvicorn)
python -m uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload

# Or run directly (app is importable from main.py)
python -c "import uvicorn; uvicorn.run('api.app:app', host='0.0.0.0', port=8000, reload=True)"

# Run a single test/manual pipeline trigger
curl -X POST http://localhost:8000/api/pipeline/trigger \
  -H "Content-Type: application/json" \
  -d '{"mode": "dry-run"}'

# Check pipeline status
curl http://localhost:8000/api/pipeline/status

# Activate kill switch (blocks all pipeline runs)
curl -X POST http://localhost:8000/api/system/kill

# Resume operations
curl -X POST http://localhost:8000/api/system/resume

# System health with drift detection
curl http://localhost:8000/api/system/health
```

**Environment**: Copy `.env.example` to `.env` and fill in `DEEPSEEK_API_KEY`, `WECHAT_APP_ID`, `WECHAT_APP_SECRET`. Set `LLM_PROVIDER` to `deepseek`, `openai`, or `claude`.

## Architecture

### Three-Plane Architecture

The system follows a **control plane / data plane / feedback plane** separation, analogous to network device architecture and Kubernetes design patterns:

```
┌─────────────────────────────────────────────────────┐
│                 CONTROL PLANE                        │
│  "做不做" — decides whether to act                   │
│  policy_engine.py / scheduler.py / loop.py           │
│  API config routes / kill switch                     │
└────────────────────────┬────────────────────────────┘
                         │ gate / skip / force
┌────────────────────────▼────────────────────────────┐
│                 DATA PLANE                           │
│  "怎么做" — executes content production              │
│  topic.py / research.py / writer.py / persona.py     │
│  critic/ (primary, compliance, adversarial)          │
│  formatter.py / publisher.py / integrity.py          │
└────────────────────────┬────────────────────────────┘
                         │ metrics / embeddings / traces
┌────────────────────────▼────────────────────────────┐
│               FEEDBACK PLANE                         │
│  "下次怎么变好" — learns and self-corrects           │
│  feedback.py / drift.py / memory.py                  │
│  bandit updater (db/_content_memory.py)              │
│  reward shaping / entropy monitor                    │
└─────────────────────────────────────────────────────┘
```

Key design constraint: the control plane can block the data plane (skip/fail/kill) but the data plane never calls back into the control plane. The feedback plane reads from both but only writes to DB — it never directly triggers pipeline actions.

### Four Feedback Loops

The system's "intelligence" comes from four interacting closed loops:

| Loop | Type | Path | Mechanism |
|------|------|------|-----------|
| **Topic Learning** | Optimization | pool → GENERATE → publish → metrics → reward → pool update | UCB bandit + shaped reward (read×0.4 + share×0.3 + completion×0.3) |
| **Quality Control** | Optimization | writer → critic+integrity → policy → rewrite | Adversarial critic + compliance hard-gate + integrity drift check |
| **Style Stability** | Optimization | persona rotation → style embedding → drift monitor → correction | 3-persona round-robin + centroid tracking + entropy control |
| **System Stability** | **Safety** | feedback → drift detector → system adjustment | raise_threshold / force_exploration / escalate to semi-auto / reduce frequency |

The first three are **optimization loops** (make the system better, can be gradual). The fourth is a **safety loop** (prevent the system from degrading, must respond fast). Safety loop corrections take precedence over optimization signals.

### Design Evolution & Rationale

The architecture converged through three iterations driven by production-readiness concerns:

| Version | Stages | Critics | Policy | Problem |
|---------|--------|---------|--------|---------|
| v1 | 8 explicit stages | Single critic | Rule-chain | Script-like, no decision intelligence |
| v2 | 12 explicit stages | 4 critics (A/B/C/D) + aggregator | 3-layer rules | State explosion, ~7-12 LLM calls/article, rule explosion |
| **v3 (current)** | **5 macro-stages** with internal sub-stages | **2+1** (primary + compliance always; adversarial on-demand) | **Scoring model** (weighted sum) | Converged: ~4-6 calls/article, linear failure paths, tunable weights |

Key convergence decisions and why:

- **12→5 stages**: Each macro-stage (`GENERATE`, `REVIEW`, `PUBLISH`) encapsulates internal sub-stages that don't expose to the top-level state machine. This prevents O(n²) failure paths from stage×retry×condition interactions.
- **4→2+1 critics**: Primary (quality) and Compliance (hard gate) run every time. Adversarial ("devil's advocate") only triggers on borderline scores (0.7-0.8) or post-rewrite — ~60% cost reduction while preserving the adversarial safeguard where it matters.
- **Rules→scoring model**: `publish_score = 0.30×time + 0.40×content + 0.30×risk`. Prevents rule explosion (new edge cases require new rules → unmaintainable). Each factor is independently testable and weights are DB-configurable.
- **Consistency+fingerprint→integrity**: Two overlapping modules merged into one LLM call. Style drift computed locally via cosine similarity (no LLM needed).

### System Identity

This is a **Content Intelligence Agent**, not a publishing script. Its core loop is a decision system: every cycle it asks "should I publish today?" and can answer "no." The bottleneck is content quality stability over weeks, not API integration.

### 5-Stage Pipeline (`agent/loop.py`)

The central orchestrator is a state machine running `ArticleState` through five macro-stages:

```
DECIDE → GENERATE → REVIEW → PUBLISH → FEEDBACK
  │                    │         │
  └─ skip → DONE       └─ fail → DONE/FEEDBACK
```

- **DECIDE** (`agent/policy_engine.py`): 3-layer scoring model (time + content + risk) with DB-configurable weights. Scores below the publish threshold (default 0.70) skip the pipeline.
- **GENERATE**: Sub-stages run sequentially — persona selection → topic selection → research → writer. Topic selection supports **manual override**: if `state.topic` is set (via API `POST /api/pipeline/trigger { topic, angle }`), the LLM selection is bypassed and the user-provided topic is used directly (`topic_source = "manual"`). If no topic provided, the existing UCB bandit + LLM auto-selection runs (`topic_source = "auto"`). The writer gets persona tone/style injected into its system prompt. Retry with backoff on short/empty output.
- **REVIEW**: Three critics run concurrently (`critic/primary.py` quality evaluation, `critic/compliance.py` regulatory check, `content/integrity.py` contradiction/template/style-drift check). Compliance < 0.5 is a hard-fail gate. Primary < 0.7 triggers rewrite (max 3). Borderline scores also run an adversarial critic. Critics fail-closed on error (compliance → score 0, primary → score 0.5).
- **PUBLISH**: Formats markdown to WeChat HTML via LLM, then calls WeChat API. `dry-run` mode skips API. `auto` mode with risk_score < 0.30 auto-downgrades to `semi-auto`. Daily rate limiter at 1 article.
- **FEEDBACK**: Records article to content memory with embeddings, marks topic as used, prepares for metrics fetch (async, non-blocking).

**Pipeline idempotency**: A DB-backed lock (`pipeline_runs` table) prevents duplicate runs — each trigger gets a unique `article_id` UUID, and the lock key is `{session_id}:{article_id}`.

### State Objects (`agent/state.py`)

Two dataclasses carry all pipeline context:
- `ArticleState` — per-article mutable state (topic, draft, critic scores, publish results, trace). Fields are named for the stage that produces/consumes them.
- `AgentRuntime` — cross-stage context (LLM provider, call counts, timing). Mirrors the pattern from ai-invest-agent.

### LLM Infrastructure (`infra/llm.py`)

Unified async client supporting DeepSeek, OpenAI, and Claude via the OpenAI-compatible SDK. Key behaviors:
- 3-attempt retry with exponential backoff per call
- Fallback routing: Claude → DeepSeek, OpenAI → DeepSeek. DeepSeek has no fallback (last line).
- `json_mode=True` sets `response_format: json_object` (skipped for Claude which doesn't support it).
- All calls traced to `data/analysis_trace.jsonl`.
- Separate embedding API (`aget_embedding`) with its own provider config.

### Database (`db/_base.py` + per-table modules)

SQLite with WAL mode, stored at `data/wechat.db`. Tables:
- `articles` — full article storage with soft delete (`status_flag`)
- `content_memory` — per-topic bandit stats (UCB scores, times published, avg read count)
- `topic_pool` — candidate topics with trend scores
- `publish_log` — immutable audit trail of every pipeline run
- `article_metrics` — WeChat analytics (read/share/like counts)
- `runtime_config` — mutable key-value config persisted across restarts
- `job_locks` / `pipeline_runs` — idempotency and deduplication

`RuntimeConfig` (`db/_config.py`) wraps the `runtime_config` table with typed getters for scoring weights, thresholds, human mode, and LLM provider. JSON values are auto-serialized.

### Persona Layer (`content/persona.py`)

Without persona separation, the system converges to a single "average voice" over time — each article's style gravitates toward the mean of all previous articles, producing bland, samey content. This is a **style convergence problem** that kills reader engagement over weeks of operation.

Three writing personas rotate round-robin each publish cycle to prevent style convergence:
- **analytical** (深度分析师): data-driven, logical, cold-objective tone. Forbids sensational words ("震惊", "必须看").
- **narrative** (叙事者): story-driven, scene-based, emotional resonance. Forbids template endings ("综上所述", "在当今社会").
- **contrarian** (反常识思考者): challenges assumptions, alternative perspectives, sharp but not mean. Forbids appeal-to-authority ("权威专家指出", "众所周知").

Each persona injects tone rules, opening style, structure preference, and **forbidden vocabulary** into the writer's system prompt. Rotation is tracked via `last_persona` in `runtime_config`. The persona is selected **before** topic selection in the GENERATE stage so the topic angle aligns with the persona's voice.

### Critic Ensemble (`critic/`)

Three independent critics evaluate drafts:
1. **Primary** — 5 dimensions (information_density, originality, readability, engagement, structure). Score ≥ 0.7 passes.
2. **Compliance** — regulatory risk check (sensitive topics, false info, platform rules). Score < 0.5 = hard veto.
3. **Adversarial** — "devil's advocate" finding factual issues, logic gaps, overclaims, bias. Triggered only for borderline scores or post-rewrite. `overall_issue_score ≥ 0.7` is fatal.

### Integrity Check (`content/integrity.py`)

Single LLM call checking: contradiction with historical articles, title/opening template pattern repeat, topic coverage. Style drift is computed locally via cosine similarity of embeddings against the historical centroid. Fails-open on error (non-critical path).

### Topic Scoring & Bandit (`db/_content_memory.py`)

UCB (Upper Confidence Bound) scoring balances exploitation (high read-count topics) vs exploration (under-published topics):
- `ucb = normalized_reward + sqrt(2 * ln(total_published) / times_published)`
- Rewards are **shape-adjusted** in the feedback loop: `reward = 0.4×normalized_reads + 0.3×share_rate + 0.3×completion_rate`, not raw read count. This prevents the bandit from optimizing for clickbait (high clicks, low quality). Articles that failed compliance review get reward = 0 regardless of metrics.
- Topic selection blends UCB score (exploit/explore) with trend score: `topic_score = ucb_score×0.6 + trend_score×0.4`.

### Global Drift Control (`content/drift.py`)

Without drift control, an autonomous agent degrades over weeks: style shifts toward bland or sensational, topic distribution collapses to a few "safe" themes, reward signal trends down. Individual checks (entropy, similarity) catch local issues but miss systemic degradation.

Monitors three axes of long-term degradation via a **system state vector** computed each feedback cycle:
1. **Style drift** — embedding cosine distance from historical centroid (warn > 0.25, critical > 0.40)
2. **Topic collapse** — Shannon entropy of topic distribution (critical < 0.20)
3. **Reward trend** — linear regression slope over recent articles (critical slope < -0.30)

**Auto-correction hierarchy** (least→most severe):
- `force_exploration` → overrides topic selection to prefer unexplored topics
- `increase_critic_threshold` → temporarily raises publish threshold by 0.05 (max 0.85)
- `escalate_to_semi_auto` → downgrades auto→semi-auto, requiring human approval

Drift state is persisted to `runtime_config.last_drift_state` and exposed via `/api/system/health`.

### Scheduling (`infra/scheduler.py`)

APScheduler with Asia/Shanghai timezone, cron-configurable via env:
- Topic pool refresh: weekday 08:00
- Publish pipeline: weekday 09:00
- Feedback fetch: daily 10:00
- Daily DB backup: 20:00

All jobs use DB-backed locks (`job_locks` table) to prevent duplicate execution. Stale locks (>2h) are auto-cleaned.

### WeChat API (`infra/wechat_api.py`)

Async httpx client with automatic access_token refresh (double-checked locking, 5-min safety buffer). Supports draft CRUD, publish/submit, article listing, and image upload. Token expiry mid-request triggers a single retry.

### API Routes (`api/routes/`)

- `/api/pipeline/*` — manual trigger, status, history, kill switch
- `/api/articles/*` — CRUD, approve/reject (semi-auto mode)
- `/api/config/*` — human mode, schedule, LLM provider, scoring params
- `/api/webhook/*` — WeChat message webhook receiver

### Tools (`tools/`)

Tool registry with JSON Schema definitions for LLM function-calling. Keyword-based selection via Chinese regex matching (mirrors ai-invest-agent pattern). Categories: research, memory, wechat.

### Retry Policy (`infra/retry.py`)

Per-stage retry with configurable backoff and fallback strategies:
- writer: 2 retries → fail
- publisher: 1 retry → draft_only
- critic/integrity: 0 retries → pass_warning
- topic/research: 1 retry → default_topic/topic_only

## Risk Profile & Known Failure Modes

From the system's engineering review, ranked by severity:

| Risk Area | Level | Primary Failure Mode | Mitigation |
|-----------|-------|---------------------|------------|
| **Long-term consistency** | **High** | Style drift, topic collapse, logical contradictions across articles | Persona rotation + drift control + integrity check |
| **Auto-publish strategy** | Medium-High | Policy engine over-fit, publishing low-quality content to "meet schedule" | Scoring model with skip threshold; risk-based auto→semi-auto downgrade |
| **WeChat anti-automation** | Medium | Behavioral pattern detection (similar titles, structure repetition) | Integrity check template detection; persona diversity |
| **Critic reliability** | Medium | LLM "self-rationalization" (critic is same model as writer) | Adversarial critic on borderline; compliance fail-closed |
| **Scheduler reliability** | Medium | Duplicate runs, stale locks, process restart | DB job locks + idempotency keys + stale lock cleanup |
| **Writing pipeline** | Low | Empty/short output, malformed JSON | Writer retry with validation; fallback to topic-only text |
| **LLM API** | Low | Provider outage, rate limits | 3-retry + provider fallback chain (Claude→DeepSeek, OpenAI→DeepSeek) |

**Critical failure modes to watch in production:**
- **Hollow content**: Structurally correct but information-sparse articles pass critics with mediocre scores
- **Clickbait drift**: Bandit optimizes for read count → titles get more sensational → quality degrades → reward shaping should catch this
- **Template feel**: Articles diverge in content but converge in structure (same opening pattern, same paragraph rhythm) → integrity check detects this
- **Contradiction chains**: Article N contradicts article N-3 on the same topic → integrity check catches this but only within 10-article window

## Key Patterns

- **Fail-closed on compliance, fail-open on integrity**: Compliance check failure blocks publish (conservative — better to miss a publish than publish a violation); integrity check failure allows it through (non-critical, false positives would block the pipeline).
- **Fail-closed on LLM errors in critical paths**: Compliance critic errors → score 0 (blocks publish). Primary critic errors → score 0.5 (triggers rewrite). Non-critical paths (integrity, feedback) fail-open.
- **LLM traceability**: Every LLM call logs to `data/analysis_trace.jsonl` with stage, provider, model, token lengths, and latency. Pipeline decisions log to `data/decision_trace.jsonl`.
- **DB as config store**: All tunable parameters (weights, thresholds, modes) live in `runtime_config` and can be changed at runtime via API — no restart needed.
- **Soft delete**: Articles use `status_flag = 'deleted'` rather than hard deletes. Daily JSON backups to `data/backups/` with 7-day retention.
- **Human modes**: `dry-run` (preview only), `semi-auto` (draft created, awaits approval), `auto` (full autonomous publish with risk-based downgrade guard).

## System Invariants (Runtime Contract)

These are hard constraints that must never be violated during development. Full details in `ARCHITECTURE.md` §6.

| # | Invariant | Rationale |
|---|-----------|-----------|
| I-1 | Control Plane is the only write authority to execution state | Prevents self-modifying execution loops |
| I-2 | Data Plane never calls back into Control Plane | Writer/critic/publisher are pure functions of state |
| I-3 | Feedback Plane cannot trigger execution directly | Feedback writes to DB → affects *future* decisions only |
| I-4 | Compliance failure = hard stop, no override | Even force=true does not skip compliance critic |
| I-5 | At most 1 publish/day enforced at two independent layers | policy_engine + publisher handler both check |
| I-6 | Kill switch is highest-priority control | Checked first in run_pipeline(), persists across restarts |
| I-7 | Every publish decision must pass policy_engine scoring | No code path bypasses DECIDE stage |
| I-8 | Dry-run mode never calls WeChat API | Guaranteed by early-return, not configuration |
| I-9 | Feedback writes are idempotent and non-destructive | INSERT OR REPLACE only, never DELETE |
| I-10 | Safety corrections only escalate, never loosen | Auto can → semi-auto, never semi-auto → auto |
| I-11 | Every pipeline run produces exactly one trace entry | decision_trace.jsonl + publish_log + analysis_trace.jsonl |
| I-12 | Every state transition is logged | state.trace list records all significant events |
