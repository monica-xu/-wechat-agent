# 微信公众号 AI Agent 使用手册

一个自主内容生产系统：定时选题 → AI 撰稿 → 质量评审 → 格式化 → 发布到微信公众号。不是简单的"定时写发"脚本，而是具备**自主决策能力**的 Content Intelligence Agent。

## 快速开始

### 1. 环境准备

```bash
# Python 3.10+
pip install -r requirements.txt

# 复制配置文件
cp .env.example .env
```

### 2. 编辑 `.env`

```bash
# 必填：LLM 提供商（推荐 deepseek，性价比最高）
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-your-deepseek-key

# 必填：微信公众号凭证
WECHAT_APP_ID=wx_your_app_id
WECHAT_APP_SECRET=your_app_secret

# 可选：备选 LLM 提供商
OPENAI_API_KEY=sk-your-openai-key
CLAUDE_API_KEY=sk-ant-your-claude-key
```

### 3. 启动

```bash
python -m uvicorn api.app:app --host 0.0.0.0 --port 8000
```

启动后自动初始化数据库、注册定时任务。默认以 **dry-run** 模式运行（只生成不发布）。

### 4. 验证

```bash
# 手动触发一次 dry-run，确认流水线跑通
curl -X POST http://localhost:8000/api/pipeline/trigger \
  -H "Content-Type: application/json" \
  -d '{"mode": "dry-run"}'

# 查看运行结果
curl http://localhost:8000/api/pipeline/status
```

## 三种运行模式

| 模式 | 选题→写稿→评审 | 创建微信草稿 | 发布 | 适用场景 |
|------|:---:|:---:|:---:|------|
| `dry-run` | ✅ | ❌ | ❌ | 初次部署、调试、验证内容质量 |
| `semi-auto` | ✅ | ✅ | ⏸️ 等待人工审批 | 生产环境推荐模式 |
| `auto` | ✅ | ✅ | ✅ | 充分信任后可开启 |

**⚠️ 建议**：先用 `dry-run` 运行 1-2 周，确认文章质量稳定后再升级到 `semi-auto`。

### 切换模式

```bash
# 查看当前模式
curl http://localhost:8000/api/config/human-mode

# 切换为半自动
curl -X PUT http://localhost:8000/api/config/human-mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "semi-auto"}'

# 切换为全自动（谨慎）
curl -X PUT http://localhost:8000/api/config/human-mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "auto"}'
```

### 模式决策流程

```
定时到达（工作日 09:00）
    │
    ▼
[DECIDE] 策略引擎打分
    │
    ├─ 分数 < 0.5  → 跳过（不发）
    ├─ 分数 0.5-0.7 → 低信心发布（记录警告）
    ├─ 分数 ≥ 0.7  → 正常发布
    └─ 手动触发 + force=true → 跳过所有检查
    │
    ▼
[GENERATE] 人格选择 → 选题 → 调研 → 写作
    │
    ▼
[REVIEW] 质量评审 + 合规检查 + 一致性检查
    │
    ├─ 通过 → 继续
    └─ 不通过 → 重写（最多3次） → 仍不通过则放弃
    │
    ▼
[PUBLISH] 格式化 → 创建草稿 → 发布
    │
    ├─ dry-run  → 保存预览，结束
    ├─ semi-auto → 草稿已创建，等待 /api/articles/{id}/approve
    └─ auto → 直接发布
```

## 每日运行流程

定时任务由 APScheduler 自动执行（北京时间）：

```
08:00  刷新话题池      从 LLM 获取 5 个候选话题
09:00  执行发布流水线   完整 5 阶段流程（DECIDE→GENERATE→REVIEW→PUBLISH→FEEDBACK）
10:00  拉取反馈数据     从微信获取阅读量/分享数据，更新话题评分
20:00  数据库备份       JSON 格式备份到 data/backups/
```

## API 参考

### 流水线控制

```bash
# 手动触发（可选指定 topic/angle 接管选题）
POST   /api/pipeline/trigger     {"mode": "dry-run", "topic": "AI安全性", "angle": "从技术角度分析"}

# 查看当前状态（发布数、活跃话题数、候选话题列表、最近运行记录）
GET    /api/pipeline/status?session_id=default

# 查看历史运行记录
GET    /api/pipeline/history?session_id=default&limit=20

# 手动刷新候选话题池
POST   /api/pipeline/refresh-pool
```

**手动选题**：`topic` 和 `angle` 均为可选。如果传了 `topic`，系统直接用你指定的话题写作（`topic_source = "manual"`）；留空则自动选题（`topic_source = "auto"`）。

### 文章管理（semi-auto 模式用）

```bash
# 列出所有文章
GET    /api/articles?session_id=default&status=draft

# 查看单篇文章
GET    /api/articles/{article_id}

# 审批通过 → 发布到微信
POST   /api/articles/{article_id}/approve

# 拒绝
POST   /api/articles/{article_id}/reject

# 软删除
DELETE /api/articles/{article_id}
```

### 配置管理

```bash
# 查看/切换 运行模式
GET    /api/config/human-mode
PUT    /api/config/human-mode      {"mode": "semi-auto"}

# 查看/切换 LLM 提供商
GET    /api/config/provider
PUT    /api/config/provider        {"provider": "deepseek"}

# 查看/修改 发布阈值（0.3-0.95）
GET    /api/config/scoring
PUT    /api/config/scoring/threshold   {"threshold": 0.70}

# 查看/修改 评分权重（三项之和必须为 1.0）
PUT    /api/config/scoring/weights     {"time": 0.30, "content": 0.40, "risk": 0.30}

# 查看/修改 定时计划（需重启生效）
GET    /api/config/schedule
PUT    /api/config/schedule        {"publish_cron": "0 9 * * 1-5", "topic_cron": "0 8 * * 1-5"}
```

### 系统管理

```bash
# 系统健康检查（含漂移检测）
GET    /api/system/health

# 紧急停止（阻断所有流水线）
POST   /api/system/kill

# 恢复运行
POST   /api/system/resume
```

## 系统健康监控

`GET /api/system/health` 返回：

```json
{
  "kill_switch_active": false,
  "published_today": 1,
  "published_this_week": 3,
  "active_topics": 8,
  "topic_entropy": 0.62,         // < 0.3 表示话题过于集中
  "entropy_warning": false,
  "drift": {
    "alerts": [],                // 漂移告警列表
    "alert_count": 0
  }
}
```

**需要关注的信号**：
- `topic_entropy < 0.3` → 话题分布过于集中，系统会自动触发探索模式
- `drift.alert_count > 0` → 存在风格漂移、奖励下降等问题
- `drift.alerts[].level == "critical"` → 系统已自动降级为 semi-auto 模式

## 评分系统调参

策略引擎使用加权评分决定是否发布：

```
publish_score = 0.30 × time_score + 0.40 × content_score + 0.30 × risk_score
```

### time_score 因子
- 距上次发布时间（< 12h = 0, 12-18h = 0.5, > 18h = 1.0）
- 是否工作日（工作日 = 1.0, 周末 = 0.3）
- 是否在发布窗口 8:00-18:00（在窗口 = 1.0, 不在 = 0.3）

### content_score 因子
- 候选话题池大小（≥5 = 1.0, 3-4 = 0.7, <3 = 0.3）
- 话题池平均质量分（≥0.6 = 1.0）
- 是否有 6h 内新增话题（有 = 1.0）

### risk_score 因子
- 今日发布未超限（未超 = 1.0, 超限 = 0）
- 本周发布未超限（未超 = 1.0）
- 与近期文章相似度正常（< 0.85 = 1.0）
- 微信 API 健康（可达 = 1.0）

### 调参建议

| 场景 | 调整 | 命令 |
|------|------|------|
| 发文太频繁，质量不稳定 | 提高阈值 | `PUT /config/scoring/threshold {"threshold": 0.80}` |
| 总是不发，错过好时机 | 降低阈值 | `PUT /config/scoring/threshold {"threshold": 0.60}` |
| 内容质量好但不敢发 | 降低 risk 权重 | `PUT /config/scoring/weights {"time": 0.30, "content": 0.50, "risk": 0.20}` |
| 发文频率太高 | 提高 time 权重 | `PUT /config/scoring/weights {"time": 0.40, "content": 0.35, "risk": 0.25}` |

## 紧急操作

### 立即停止所有自动发布

```bash
curl -X POST http://localhost:8000/api/system/kill
```

效果：所有定时和手动触发的流水线在 DECIDE 阶段直接返回 `skip`。

### 恢复

```bash
curl -X POST http://localhost:8000/api/system/resume
```

### 手动干预话题池

（暂无 API，可直接操作 SQLite）

```bash
sqlite3 data/wechat.db "INSERT INTO topic_pool (session_id, topic, reason, source, trend_score) VALUES ('default', '你想写的话题', '人工指定', 'manual', 0.9);"
```

## 可视化管理面板

启动后在浏览器打开 `http://127.0.0.1:8080/dashboard/`：

- **总览** — 系统状态、发布统计、最近运行
- **控制** — 手动选题/自动选题、触发流水线、紧急停止
- **文章** — 查看、审批、删除文章
- **配置** — 切换运行模式、LLM 提供商、评分参数
- **健康** — 话题熵值仪表盘、漂移告警
- **追溯** — 流水线执行详情 DAG

## 数据与备份

- **数据库**：`data/wechat.db`（SQLite，WAL 模式）
- **决策追踪**：`data/decision_trace.jsonl`（每次流水线的完整决策过程）
- **LLM 调用追踪**：`data/analysis_trace.jsonl`（每次 LLM 调用的耗时、token 数、成功/失败）
- **每日备份**：`data/backups/YYYYMMDD.json`（自动保留 7 天）
- **文章预览**：`data/previews/`（dry-run 模式生成的 HTML 预览）

## 常见问题

### Q: dry-run 模式生成的文章在哪里看？

查看 `data/previews/` 目录下的 HTML 文件，或通过 API 查询：
```bash
curl http://localhost:8000/api/articles?status=draft | python -m json.tool
```

### Q: 为什么定时到了但没发文？

几种可能：
1. 策略引擎评分低于阈值（`publish_score < 0.7`）→ 查看 `data/decision_trace.jsonl`
2. kill switch 处于激活状态 → `GET /api/system/health`
3. 今日已发布数量达到上限（默认 1 篇/天）
4. 话题池没有足够的候选话题（需要 ≥3 个活跃话题）

### Q: 如何修改每天发文数量上限？

```bash
sqlite3 data/wechat.db "UPDATE runtime_config SET value = '3' WHERE key = 'daily_publish_limit';"
sqlite3 data/wechat.db "UPDATE runtime_config SET value = '15' WHERE key = 'weekly_publish_limit';"
```

### Q: 文章质量不稳定怎么办？

1. 保持在 `semi-auto` 模式，人工审批后再发布
2. 提高发布阈值：`PUT /api/config/scoring/threshold {"threshold": 0.80}`
3. 检查 `data/analysis_trace.jsonl` 中 critic 评分低的维度，针对性调整 prompt
4. 运行 1-2 周后检查 `/api/system/health` 的 drift 状态

### Q: LLM 调用失败怎么办？

系统有三级容错：
1. 同 provider 重试 3 次（指数退避）
2. 自动切换备选 provider（Claude→DeepSeek, OpenAI→DeepSeek）
3. 非关键环节降级（如研究失败 → 仅用话题文本写作）

### Q: 微信 API 报错？

- `errcode=40001/42001`：access_token 过期 → 系统自动刷新
- `errcode=48001`：API 权限不足 → 检查公众号是否认证
- 频繁 `errcode=-1`：微信风控 → 降低发布频率，检查内容是否模板化

## 进阶：切换到 Claude

如果你有 Claude API key 且偏好 Claude 的写作质量：

```bash
# 1. 配置 .env
LLM_PROVIDER=claude
CLAUDE_API_KEY=sk-ant-your-key

# 2. 运行时切换
curl -X PUT http://localhost:8000/api/config/provider \
  -H "Content-Type: application/json" \
  -d '{"provider": "claude"}'

# 3. 重启服务
```

注意：如果 Claude API 不可用，系统会自动 fallback 到 DeepSeek。
