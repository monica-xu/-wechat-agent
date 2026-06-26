# WeChat AI Agent — Execution Architecture (v3)

## 1. Three-Plane Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           CONTROL PLANE                                  │
│                        "做不做" — Decision Layer                         │
│                                                                          │
│  ┌──────────────┐  ┌──────────────────┐  ┌──────────────────────────┐   │
│  │ APScheduler  │  │  /api/pipeline/* │  │  PIPELINE RUNNER         │   │
│  │ cron/manual  │──│  trigger/kill    │──│  loop.py state machine   │   │
│  │ 08:00 09:00  │  │  /api/system/*   │  │  5 macro-stages          │   │
│  └──────────────┘  └──────────────────┘  └──────────┬───────────────┘   │
│                                                     │                   │
│                    ┌────────────────────────────────┼──────┐            │
│                    │  policy_engine.py              │      │            │
│                    │  publish_score =               │      │            │
│                    │    0.30×time                   │      │            │
│                    │  + 0.40×content                │      │            │
│                    │  + 0.30×risk                   │      │            │
│                    │  → skip / normal / force       │      │            │
│                    └────────────────────────────────┘      │            │
│                                                            │            │
│  /api/config/*  ← runtime_config DB ← RuntimeConfig        │            │
└────────────────────────────────────────────────────────────┼────────────┘
                                                             │
                                      gate: skip / fail / force
                                                             │
┌────────────────────────────────────────────────────────────▼────────────┐
│                            DATA PLANE                                    │
│                     "怎么做" — Execution Layer                           │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                        GENERATE                                   │   │
│  │  ┌──────────┐    ┌──────────┐    ┌──────────────────────┐        │   │
│  │  │ Persona  │───→│  Topic   │───→│  Research            │        │   │
│  │  │ Select   │    │  Select  │    │  (LLM knowledge)     │        │   │
│  │  │ a/n/c    │    │  UCB +   │    └──────────┬───────────┘        │   │
│  │  │ rotate   │    │  trend   │               │                    │   │
│  │  └──────────┘    └──────────┘               ▼                    │   │
│  │                                    ┌──────────────────────┐      │   │
│  │                                    │  Writer              │      │   │
│  │                                    │  persona injection   │      │   │
│  │                                    │  + research notes    │      │   │
│  │                                    └──────────┬───────────┘      │   │
│  └───────────────────────────────────────────────┼──────────────────┘   │
│                                                   │ draft               │
│  ┌───────────────────────────────────────────────▼──────────────────┐   │
│  │                        REVIEW                                     │   │
│  │  ┌────────────────┐  ┌──────────────────┐  ┌────────────────┐    │   │
│  │  │ Primary Critic │  │ Compliance Critic│  │ Integrity Check│    │   │
│  │  │ 5 dimensions   │  │ regulatory gate  │  │ contradiction  │    │   │
│  │  │ score ≥ 0.7    │  │ score < 0.5→fail │  │ template detect│    │   │
│  │  └───────┬────────┘  └────────┬─────────┘  │ style drift    │    │   │
│  │          │                    │             └───────┬────────┘    │   │
│  │          │         concurrent │                     │             │   │
│  │          └────────────────────┼─────────────────────┘             │   │
│  │                               │                                   │   │
│  │                    ┌──────────▼──────────┐                        │   │
│  │                    │ Adversarial Critic  │  ← triggered on        │   │
│  │                    │ "devil's advocate"  │    borderline or       │   │
│  │                    │ factual/logic check │    post-rewrite        │   │
│  │                    └──────────┬──────────┘                        │   │
│  │                               │                                   │   │
│  │              pass ────────────┼──────── fail                      │   │
│  │              → PUBLISH        │        → rewrite (≤3) / FAIL      │   │
│  └───────────────────────────────┼───────────────────────────────────┘   │
│                                   │                                       │
│  ┌────────────────────────────────▼──────────────────────────────────┐   │
│  │                        PUBLISH                                     │   │
│  │  ┌──────────┐    ┌──────────────┐    ┌──────────────────────┐     │   │
│  │  │ Formatter│───→│ Create Draft │───→│ Publish (mode gated) │     │   │
│  │  │ MD→HTML  │    │ WeChat API   │    │ dry-run: skip        │     │   │
│  │  │ LLM      │    │ draft/add    │    │ semi-auto: await     │     │   │
│  │  └──────────┘    └──────────────┘    │ auto: publish+risk↓  │     │   │
│  │                                      └──────────────────────┘     │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ArticleState dataclass ── carries all mutable state across stages       │
│  AgentRuntime dataclass ── carries cross-stage context + LLM usage       │
└──────────────────────────────────────────────────────────────────────────┘
                                       │
                         metrics / embeddings / traces
                                       │
┌──────────────────────────────────────▼──────────────────────────────────┐
│                         FEEDBACK PLANE                                   │
│                  "下次怎么变好" — Learning Layer                         │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  feedback.py (daily 10:00)                                        │   │
│  │                                                                    │   │
│  │  WeChat API ──→ article metrics (read/share/like/complete)        │   │
│  │       │                                                            │   │
│  │       ├──→ reward shaping: 0.4×read + 0.3×share + 0.3×complete   │   │
│  │       │         (compliance-failed articles → reward = 0)          │   │
│  │       │                                                            │   │
│  │       ├──→ bandit UCB update:                                     │   │
│  │       │    ucb = avg_reward + √(2·ln(total) / times_published)    │   │
│  │       │    → updates content_memory.ucb_score                     │   │
│  │       │                                                            │   │
│  │       └──→ drift detection:                                       │   │
│  │            compute_system_state_vector()                           │   │
│  │            ├── style centroid (embedding cosine drift)            │   │
│  │            ├── topic entropy (Shannon)                            │   │
│  │            └── reward trend (linear regression slope)             │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  drift.py — auto-correction hierarchy                             │   │
│  │                                                                    │   │
│  │  Level 1: force_exploration     → prefer unexplored topics        │   │
│  │  Level 2: increase_threshold    → raise publish bar by 0.05       │   │
│  │  Level 3: escalate_to_semi_auto → downgrade auto→semi-auto        │   │
│  │                                                                    │   │
│  │  drift state persisted → RuntimeConfig.last_drift_state           │   │
│  │                        → exposed via GET /api/system/health       │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
```

## 1.1 Dual-Mode Topic Entrypoint

The topic selection has a **dual-mode entrypoint** — manual override or auto selection:

```
POST /api/pipeline/trigger  { mode, topic?, angle? }
              │
    ┌─────────┴─────────┐
    │                   │
  topic set          topic empty
  (manual)           (auto)
    │                   │
    ▼                   ▼
state.topic       run_topic()
→ direct use      → UCB + LLM selection
    │                   │
    └─────────┬─────────┘
              ▼
         GENERATE
```

- **manual**: User-provided topic bypasses LLM selection (`topic_source = "manual"`)
- **auto**: Existing behavior — UCB bandit + trend score → LLM picks topic (`topic_source = "auto"`)
- **candidate_topics**: Read-only list from topic pool, displayed in dashboard for visibility

## 2. Control Flow — Pipeline State Machine

```
[IDLE] ──(cron 09:00 / API trigger)──→ [DECIDE]
                                            │
                          ┌─────────────────┼─────────────────┐
                          │                 │                 │
                     score ≥ 0.7     0.5 ≤ score < 0.7   score < 0.5
                     (normal)        (low confidence)    (skip)
                          │                 │                 │
                          ▼                 ▼                 ▼
                     [GENERATE]       [GENERATE]          [SKIP]
                          │            + warning         → [DONE]
                          ▼                 │
                     [REVIEW]              │
                          │                 │
              ┌───────────┼───────────┐     │
              │           │           │     │
            pass      fail+<3     fail+≥3  │
              │      rewrites    rewrites  │
              ▼           │           │     │
         [PUBLISH]    [REWRITE]    [FAIL]   │
              │        → REVIEW   → [DONE] │
     ┌────┬───┴───┬────┐                   │
     │    │       │    │                   │
  dry-run semi  auto  api-fail             │
     │    │       │    │                   │
     ▼    ▼       ▼    ▼                   │
   save  draft  publish draft_only         │
     │   +wait    │    │                   │
     │    │       │    │                   │
     └────┴───────┴────┘                   │
            │                              │
            ▼                              │
       [FEEDBACK] ←────────────────────────┘
            │
            ▼
         [DONE]
```

## 3. Data Flow — Article Production

```
Topic Pool DB                    Persona DB
(UCB + trend scores)             (last_persona key)
       │                              │
       └──────────┬───────────────────┘
                  ▼
          ┌──────────────┐
          │   GENERATE   │
          │ writer +     │
          │ persona      │
          └──────┬───────┘
                 │ draft (title, content_md, embedding)
                 ▼
          ┌──────────────┐
          │ ARTICLE      │
          │ STATE        │──── embedding → Similarity Search (content_memory)
          │ dataclass    │
          └──────┬───────┘
                 │
                 ▼
          ┌──────────────┐
          │ CRITIC       │──── scores flowing to decision trace
          │ LAYER        │
          │ concurrent   │
          └──────┬───────┘
                 │
                 ▼
          ┌──────────────┐
          │ POLICY       │──── decision → decision_trace.jsonl
          │ ENGINE       │
          └──────┬───────┘
                 │
                 ▼
          ┌──────────────┐
          │ WECHAT API   │
          │ draft/add    │
          │ freepublish/ │
          │ submit       │
          └──────┬───────┘
                 │
                 ▼
          ┌──────────────┐
          │ METRICS      │──── article_metrics table
          │ INGESTION    │
          │ read/share/  │
          │ complete     │
          └──────┬───────┘
                 │
                 ▼
          ┌──────────────┐
          │ FEEDBACK /   │
          │ BANDIT       │──── updates content_memory + topic_pool
          │ UPDATE       │
          └──────────────┘
```

## 4. Four Feedback Loops

### 🔁 Loop 1: Topic Learning (Optimization)

```
topic_pool ──→ GENERATE ──→ publish ──→ metrics ──→ reward shape ──→ topic_pool update
                                                                    (UCB recalculation)

Mechanism:
  UCB = normalized_reward + √(2·ln(total) / times_published)
  shaped_reward = 0.4×read + 0.3×share + 0.3×complete
  compliance_fail → reward = 0

  Topic selection blends: 0.6×ucb_score + 0.4×trend_score
```

### 🔁 Loop 2: Quality Control (Optimization)

```
writer ──→ primary critic ──→ compliance gate ──→ integrity check
   ↑                                                    │
   └────────── rewrite (≤3) ←────────────────── fail ───┘

Mechanism:
  primary: 5-dimension scoring (info_density, originality, readability, engagement, structure)
  compliance: hard veto at < 0.5
  adversarial: triggered on borderline (0.7-0.8) or post-rewrite
  integrity: contradiction + template + style_drift (1 LLM call)
```

### 🔁 Loop 3: Style Stability (Optimization)

```
persona rotation ──→ writer ──→ style embedding ──→ drift monitor ──→ correction
                                                         │
                                              ┌──────────┼──────────┐
                                              │          │          │
                                        entropy < 0.2  centroid   reward
                                        → force        drift      slope < -0.30
                                        exploration    > 0.40     → escalate
                                                       → warn

Mechanism:
  3 personas (analytical / narrative / contrarian)
  round-robin rotation tracked in DB
  each persona has forbidden vocabulary list
  style centroid → cosine distance from historical mean
  topic entropy → Shannon entropy normalized by log2(N)
```

### 🔁 Loop 4: System Stability (Safety)

```
drift detector ──→ alerts ──→ auto-correction
                      │
          ┌───────────┼───────────┐
          │           │           │
      critical     critical     warning
      topic        reward       style
      collapse     decline      drift
          │           │           │
          ▼           ▼           ▼
      force        escalate     raise
      exploration  semi-auto    threshold

Safety loop corrections take precedence over optimization signals.
All corrections are logged to decision_trace.jsonl with [DRIFT: ...] tags.
```

## 5. Component Map

```
wechat-agent/
│
├── agent/                    CONTROL PLANE
│   ├── loop.py               Pipeline state machine (5 macro-stage handlers)
│   ├── policy_engine.py      Scoring model: should_publish decision
│   ├── state.py              ArticleState + AgentRuntime dataclasses
│   ├── prompts.py            All system prompts (topic, research, writer, critics, formatter)
│   └── helpers.py            Time, IDs, trace logging
│
├── content/                  DATA PLANE
│   ├── topic.py              Topic selection (UCB + bandit + LLM)
│   ├── research.py           Research notes generation
│   ├── writer.py             Draft generation + rewrite
│   ├── persona.py            Multi-persona rotation system
│   ├── formatter.py          Markdown → WeChat HTML
│   ├── publisher.py          WeChat draft + publish workflow
│   ├── integrity.py          Cross-article consistency + template detection
│   └── constants.py          Thresholds, weights, limits
│
├── critic/                   DATA PLANE (review sub-system)
│   ├── primary.py            5-dimension quality evaluation
│   ├── compliance.py         Regulatory risk check (hard veto)
│   └── adversarial.py        Devil's advocate fact/logic challenge
│
├── content/                  FEEDBACK PLANE
│   ├── feedback.py           Metrics fetch + reward shaping + bandit update
│   ├── drift.py              System state vector + auto-correction
│   └── memory.py             Embeddings, similarity search, topic cooldown
│
├── infra/                    INFRASTRUCTURE
│   ├── llm.py                Multi-provider LLM client (DeepSeek/GPT/Claude)
│   ├── scheduler.py          APScheduler + DB job locks
│   ├── wechat_api.py         WeChat Official Account API client
│   └── retry.py              Per-stage retry policies
│
├── db/                       STORAGE
│   ├── _base.py              Schema, migrations, backup, connection
│   ├── _articles.py          Article CRUD
│   ├── _content_memory.py    Topic memory + UCB bandit stats
│   ├── _publish_log.py       Pipeline audit trail
│   ├── _metrics.py           Article performance metrics
│   └── _config.py            RuntimeConfig typed getters/setters
│
├── api/                      CONTROL PLANE (external interface)
│   ├── app.py                FastAPI entry + lifespan hooks
│   └── routes/
│       ├── agent.py           Pipeline trigger / status / kill / health
│       ├── articles.py        Article CRUD + approve/reject
│       ├── config.py          Human mode / schedule / provider / scoring
│       └── webhook.py         Publish status callback placeholder
│
├── tools/                    TOOL REGISTRY
│   ├── registry.py           JSON Schema definitions + keyword selection
│   ├── research.py           Search tools
│   ├── memory.py             Content memory query tools
│   └── wechat.py             Publish statistics tools
│
└── data/                     RUNTIME DATA (gitignored)
    ├── wechat.db             SQLite database
    ├── decision_trace.jsonl  Pipeline decision audit trail
    ├── analysis_trace.jsonl  LLM call trace
    ├── previews/             dry-run HTML previews
    └── backups/              Daily JSON backups (7-day retention)
```

## 6. System Invariants — Runtime Contract

These are **hard constraints** that must hold at runtime regardless of configuration changes, LLM provider switches, or code evolution. They define the boundaries within which the system is guaranteed to be safe.

### I-1: Authority

```
1. Control Plane is the ONLY write authority to execution state.
   → Only policy_engine can set state.should_publish.
   → Only loop.py can transition state.stage.
   → API routes write to runtime_config, never to ArticleState directly.

2. Feedback Plane CANNOT trigger execution directly.
   → feedback.py writes to DB, never calls run_pipeline().
   → drift.py modifies runtime_config (threshold/mode), never touches state.
   → Bandit updates affect future DECIDE decisions, never current pipeline.

3. Data Plane NEVER calls back into Control Plane.
   → writer.py, critic/*.py, publisher.py receive state as input,
     return results as output. They never call policy_engine or loop handlers.
```

### I-2: Safety

```
4. Compliance failure = HARD STOP (no override, no retry, no force flag bypass).
   → critic_compliance_score < 0.5 → state.stage = "fail", pipeline ends.
   → Even mode_override="force" does NOT skip the compliance critic.
   → On LLM error during compliance check: fail-closed (score = 0).

5. At most 1 publish/day enforced at TWO independent layers:
   → Layer 1: policy_engine risk_score (daily_count_ok factor).
   → Layer 2: PUBLISH handler rate limiter (count_published_today check).
   → Both must pass. Failure of either blocks publish.

6. Kill switch is the highest-priority control.
   → KILL_SWITCH_ACTIVE → all pipeline runs return "skip" at DECIDE.
   → Persisted to runtime_config to survive restarts.
   → No API, no scheduler job, no manual trigger bypasses it.
```

### I-3: Decision

```
7. Every publish decision MUST pass policy_engine scoring.
   → No code path can set state.stage = "publish" without scoring.
   → Manual triggers with force=true bypass scoring but MUST still
     pass compliance (I-4) and rate limiter (I-5).
   → The force flag only skips the scoring threshold, not safety gates.

8. Dry-run mode NEVER calls WeChat API for draft creation or publish.
   → Guaranteed by early-return in PUBLISH handler, not by configuration.
   → Even if mode is changed mid-pipeline, the PUBLISH stage uses
     the mode value captured at pipeline start.
```

### I-4: Feedback

```
9. Feedback loop writes are idempotent and non-destructive.
   → UCB updates are INSERT OR REPLACE.
   → Metrics writes are INSERT OR REPLACE (article_id is PK).
   → Drift state is a single JSON value in runtime_config.
   → No feedback operation deletes data (only INSERT/UPDATE).

10. Safety corrections always escalate, never loosen.
    → raise_threshold: threshold can only increase (0.70 → 0.75, max 0.85).
    → escalate_to_semi_auto: auto → semi-auto only, never the reverse.
    → force_exploration: increases exploration weight, never decreases.
    → The system can become MORE conservative automatically,
      but never LESS conservative without human intervention.
```

### I-5: Observability

```
11. Every pipeline run produces exactly one trace entry.
    → decision_trace.jsonl: 1 line per pipeline (success, fail, or skip).
    → publish_log table: 1 row per pipeline.
    → analysis_trace.jsonl: 1 line per LLM call.
    → No pipeline code path exits without writing a trace.

12. Every state transition is logged.
    → state.trace list records [timestamp] [stage] message for every
      significant event (persona selection, critic scores, publish result).
    → Last 10 trace entries are returned in /api/pipeline/trigger response.
```

### Invariant Enforcement Map

| Invariant | Enforced By | Verified By |
|-----------|-------------|-------------|
| I-1: Control Plane sole authority | loop.py handler dispatch | Code review: no `from agent.loop import` in content/ or critic/ |
| I-2: Data Plane no callback | Function signatures (receive state, return result) | Grep: `policy_engine\|run_pipeline\|activate_kill` in content/ critic/ |
| I-3: Feedback no direct trigger | feedback.py only imports DB modules | Grep: `run_pipeline\|trigger` in content/feedback.py content/drift.py |
| I-4: Compliance hard stop | `handle_review()` gate logic | Test: force=true with non-compliant content → must fail |
| I-5: Rate limiter double check | policy_engine + publisher handler | Test: mock count_published_today return 1 → must block |
| I-6: Kill switch priority | First check in run_pipeline() | Test: activate kill → trigger → assert stage="skip" |
| I-7: Scoring required | handle_decide always called first | Code: loop.py stage order is hardcoded |
| I-8: Dry-run no API | Early return in handle_publish() | Test: dry-run with real WeChat creds → no draft created |
| I-9: Feedback idempotent | SQL INSERT OR REPLACE | DB constraint: article_id PK, (session_id,topic) UNIQUE |
| I-10: Only escalate | Correction functions are monotonic | Test: system should never auto-transition semi-auto→auto |
| I-11: Every run traced | _save_final_state in finally block | Grep: all return paths in loop.py must call _save_final_state |
| I-12: State transition log | _append_trace() calls in each handler | decision_trace.jsonl trace field |

## 7. System Verifiability

### Existing Verification

Every pipeline decision is **replayable** from its trace:

```
decision_trace.jsonl contains:
  - All factor scores (time, content, risk)
  - Final publish_score and threshold
  - All critic scores (primary, compliance, adversarial)
  - Integrity check results
  - Rewrite count and final stage

→ Given the same ArticleState and AgentRuntime, the same input
  should produce the same decision output (LLM calls are
  non-deterministic but the policy/critic scores are recorded).
```

### Verification Gaps (future work)

| Capability | Status | What's Missing |
|-----------|--------|----------------|
| Offline replay | Partial | decision_trace has all data; need a replay runner that re-feeds traces through policy_engine |
| Counterfactual simulation | Missing | "What if threshold were 0.80?" — requires running old states through new policy params |
| Multi-policy A/B | Missing | No support for concurrently running policy_v1 and policy_v2 against same topic pool |
| Drift bound proof | Missing | Can observe drift after it happens; cannot prove it won't exceed a bound given current parameters |

## 8. Document Map

| Document | Purpose | Audience |
|----------|---------|----------|
| **README.md** | User manual — setup, config, daily operations, FAQ | User / Operator |
| **CLAUDE.md** | Developer guide — code architecture, patterns, commands | Developer / Claude Code |
| **ARCHITECTURE.md** (this file) | System theory — control flow, data flow, invariants, loops | Architect / Reviewer |
