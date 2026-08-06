# PRD：慢查 AI 根因诊断（Slow Query AI Root-Cause Diagnosis）

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0（草稿） |
| 日期 | 2026-07-23 |
| 作者 | 产品组 |
| 状态 | 草稿 · 待评审 |
| 关联模块 | `sql_api/api_slowquery_v2.py`、`common/utils/openai.py`、`sql_api/api_dictionary.py`、`sql_api/api_workflow.py`、`sql/engines/*`、`frontend/src/views/slowquery/*` |

> 关联文档：本功能与 `docs/PRD-ai-copilot-sidebar.md`（AI Copilot 侧边栏）**互补不重叠**——Copilot 是「多轮对话式」助手，本功能是「针对单条慢查、带执行计划/统计/趋势上下文、自动生成结构化根因报告」的离线诊断能力；且本功能产出的报告可被 Copilot 慢查场景直接引用（见 P2）。

---

## 1. 问题陈述（Problem Statement）

### 1.1 现状

Archery 的慢查模块（`sql_api/api_slowquery_v2.py`）已具备完整的**数据采集与展示**能力：统计（p95、执行次数、扫描/返回行数）、明细（SQL 文本、锁时间、扫描行数）、趋势（按天聚合）、手动采集。但这些信息对使用者而言是**"裸数据"**——它告诉你"这条 SQL 很慢、扫描了 1200 万行"，却不告诉你**为什么慢、该先改哪里**。

要定位根因，DBA/研发需要手动做：拉执行计划（EXPLAIN）→ 读 `type=ALL`/`Using filesort`/`Using temporary` → 对照表结构判断缺索引 → 写出改写 SQL。这套动作**门槛高、耗时长、依赖少数资深 DBA 的经验**。

平台已有的 AI 能力（`common/utils/openai.py`）中，`analyze_sql_by_openai` / `optimize_sql_by_openai` 是**一次性、单条 SQL、无上下文**的按钮调用：它们拿不到这条慢查的统计指标、趋势、执行计划，因此无法回答"这条慢查相对它自身历史为何变慢、瓶颈卡在哪个算子"。

### 1.2 受影响人群与频率

- **DBA / 运维**：每天巡检慢查 Top 列表，对高耗时 SQL 逐条人工定位，是高频重复劳动。
- **研发**：收到"你的 SQL 慢"的工单后，需要自行 EXPLAIN + 请教 DBA，定位链路长（MTTR 高）。
- **架构 / TL**：需从慢查中识别共性根因（如团队普遍缺联合索引）推动治理，目前靠人工归纳。

### 1.3 不解决的代价

- **业务侧**：慢查无人及时跟进 → 线上性能持续退化，甚至引发故障。
- **效率侧**：根因定位平均耗时高、强依赖资深 DBA，成为性能治理的瓶颈。
- **竞争侧**：同类数据库平台已提供"慢查智能诊断"，本能力缺失削弱差异化。

### 1.4 证据（来自现有代码与数据模型）

- 慢查统计模型已含定位根因所需的关键信号：`query_time_p95`、`total_execution_counts`、`parse_total_row_counts`（扫描行）/ `return_total_row_counts`（返回行）——**扫描/返回比（rows_examined/rows_sent）是判断全表扫描最直接的证据**。
- 明细模型含 `query_time`、`lock_time`、`rows_sent`、`rows_examined`、`sql_text`。
- 引擎层已具备 EXPLAIN / 执行计划能力（`sql/engines/*` 的 `explain_check` / `query` 等），表结构可通过 `api_dictionary.table_info` 获取。
- 工单流已内置 AI 风险汇总（`sql_api/api_workflow.py:_calc_ai_risk_summary`），从诊断结论生成工单可平滑复用同一条管线。

---

## 2. 目标（Goals）

| 类型 | 目标 | 衡量方式 |
|---|---|---|
| 北极星 | 慢查 AI 诊断**覆盖率** ≥ 40%（90 天内，被诊断过的慢查指纹数 / 活跃慢查指纹总数） | 周维度统计 |
| 体验 | 单条诊断端到端 p95 ≤ 25s（异步生成，前端轮询/通知） | 后端任务耗时埋点 |
| 价值（闭环） | 诊断报告 → 生成优化工单草稿**转化率** ≥ 25% | 由诊断触发创建的工单数 / 诊断成功数 |
| 价值（质量） | 诊断报告"有帮助"（👍）率 ≥ 60% | 反馈埋点 |
| 成本 | 单次诊断 token 成本 ≤ 基线 × 1.2；可全局开关与按用户配额 | CopilotUsage 同类统计 |

> 目标区分"用户价值"（更快定位根因、少依赖 DBA）与"业务价值"（降低性能故障风险、提升平台差异化）。

---

## 3. 非目标（Non-Goals / YAGNI）

- **不直接执行任何 DDL/DML**：索引创建、SQL 改写落地一律生成工单草稿，走既有审核工作流 + goInception。原因：安全红线，且写操作须经人工确认。
- **不做实时流式逐字输出**：诊断报告是一次性异步生成的"离线报告"，非对话。原因：根因分析需要聚合多源上下文，流式无意义；与 Copilot 的流式定位互补。
- **不做 MongoDB / Redis 的深度根因（v1）**：这两类缺稳定可用的 EXPLAIN/执行计划，v1 仅给通用改写建议并标注"有限支持"。原因：避免给出不可靠的根因结论。
- **不负责慢查采集调度**：采集已有 `collect` 接口与定时任务，本功能只**消费**已采集数据。原因：职责单一，避免范围蔓延。
- **不做全局健康评分 / 跨库联合根因**：那是 `dbdiagnostic`（会话/锁/表空间诊断）与未来"实例健康"范畴。原因：本期聚焦单条慢查根因。

---

## 4. 用户故事（User Stories）

> **US-1（DBA 定位根因）**：作为 DBA，我在慢查统计页看到一条 p95 8s、扫描 1200 万行的 SQL，点击「AI 诊断」，得到根因结论"全表扫描 + 缺联合索引 `idx_status_created`"、严重度 `high`、建议索引 DDL，以及一键「生成工单草稿」入口；我确认后进入既有工单提交流程。

> **US-2（研发采纳改写）**：作为研发，我在诊断报告里看到改写前后对比（去掉 `SELECT *`、增加 `WHERE create_time` 索引命中），点击「复制改写 SQL」或「应用到我的查询编辑器」，直接拿到可用版本。

> **US-3（查看证据）**：作为任意有权限用户，我打开诊断报告，能看到"为什么这么判断"的证据卡（扫描/返回比、趋势恶化起始日、EXPLAIN 关键算子），而非只看到一句结论。

> **US-4（避免重复烧 token）**：作为高频使用者，我对同一条慢查（同 `sql_hash`）在 7 天内再次点击诊断时，系统直接返回已存报告而非重新调用 AI。

> **US-5（反馈闭环）**：作为用户，我对某条诊断报告点 👎 并填写"索引建议错误"，该反馈进入看板，用于迭代 prompt 与评估模型。

> **US-6（治理看板，P1）**：作为架构/TL，我查看"慢查根因健康度"看板，看到团队高频根因分布（缺索引 60% / 全表扫描 25% / 锁等待 15%），据此推动针对性治理。

---

## 5. 功能需求

### 5.1 诊断入口（P0）

- 慢查**统计页**与**明细页**均增加「AI 诊断」按钮，维度为 `sql_hash`（同指纹合并，避免重复）。
- 已诊断过的指纹显示「已诊断 · 查看报告」；未诊断显示「AI 诊断」。
- 未配置 OpenAI（`check_openai_config()` 为 False）或 `enable_ai_slowquery_diagnosis` 关闭或无 `sql.use_ai_diagnosis` 权限时，按钮**置灰并提示原因**。

### 5.2 异步生成与状态（P0）

- 点击诊断 → 提交 `django-q2` 异步任务（与 `collect_slowquery_task` 同构），前端轮询任务状态。
- 状态机：`pending → running → success | failed`。
- 生成中显示 loading + 进度文案（"正在采集执行计划…""正在分析根因…"）；失败显示错误原因 + 「重试」。

### 5.3 上下文采集（P0，核心）

后端在调用 AI 前自动聚合以下上下文（**不依赖用户手填**）：

| 上下文 | 来源 | 用途 |
|---|---|---|
| `sample_sql` / `fingerprint` | 慢查 Summary 模型 | 分析对象 |
| 统计指标 | Summary：`query_time_p95`、`total_execution_counts`、`parse_total_row_counts`、`return_total_row_counts` | 计算扫描/返回比、频率 |
| 近期趋势 | 复用 Trend 视图逻辑（最近 14 天 count/avg/max） | 判断"是否近期恶化" |
| 相关表 DDL | `api_dictionary.table_info`（或数据字典引擎） | 判断缺索引/字段类型 |
| 执行计划 EXPLAIN | 查询引擎 `explain`/`explain_check`（v1 限 MySQL/PgSQL 稳定支持） | 定位瓶颈算子（全表扫描/文件排序/临时表） |

> 上下文裁剪：EXPLAIN 输出超长时做关键字段摘要（保留 `type`/`key`/`rows`/`Extra`），避免 prompt 溢出。

### 5.4 AI 报告结构（P0）

新增 `common/utils/openai.py` 方法 `diagnose_slowquery_by_openai(...)`，返回**结构化 JSON**（复用 `review_sql_by_openai` 的 `_parse_review_json` 多层容错解析，避免 LLM 输出不规范导致解析失败）：

```jsonc
{
  "root_cause": "一句话根因（≤40字，中文）",
  "severity": "low | medium | high",          // 复用 AI_RISK_* 常量
  "bottleneck_type": "full_scan | missing_index | lock_wait | filesort | tmp_table | type_cast | other",
  "evidence": ["扫描/返回比 1200:1，疑似全表扫描", "趋势自 2026-07-10 起 p95 由 0.3s 升至 8s"],
  "suggestions": [
    {
      "type": "index_ddl",                    // index_ddl | rewrite | config
      "desc": "在 (status, created_at) 上建联合索引",
      "index_ddl": "ALTER TABLE orders ADD INDEX idx_status_created (status, created_at);",
      "before": "SELECT * FROM orders WHERE status=1 ORDER BY created_at DESC",
      "after": "SELECT id, ... FROM orders WHERE status=1 ORDER BY created_at DESC"
    }
  ],
  "confidence": 0.0,                           // 0-1，模型自评估（可选，按需计算）
  "report_markdown": "完整 markdown 报告（含问题清单+前后对比）"
}
```

**容错**：任何 AI 异常一律返回 `DIAGNOSIS_FALLBACK`（severity=unknown、空建议），**绝不抛异常中断诊断流程**，与现有 `AI_REVIEW_FALLBACK` 一致。

### 5.5 报告展示（P0）

- 报告以**抽屉/弹窗**呈现：顶部结构化卡片（根因 / 严重度标签 / 瓶颈类型 / 证据列表），下方 markdown 正文渲染（复用项目 markdown 渲染：marked + DOMPurify）。
- 建议卡片可操作：「复制改写 SQL」「复制索引 DDL」「生成工单草稿」。
- 嵌入该 `sql_hash` 的趋势迷你图（复用 `EChart` 组件）。

### 5.6 一键工单草稿（P0，安全红线）

- 点「生成工单草稿」→ 调既有 `sqlworkflow` 创建接口，把 `index_ddl` 或 `rewrite` 后的 SQL 填入工单 SQL 区，**标注来源为 AI 诊断**。
- 提交仍走既有审核工作流 + goInception 检测，**不直接执行**任何 DDL/DML。
- 复用 `api_workflow._calc_ai_risk_summary`，使工单带 AI 风险汇总，保持与手动提单一致的体验。

### 5.7 持久化与复用（P0）

- 诊断结果与任务落库（见 §7 数据模型），可复查、可对比（同指纹多次诊断保留历史）。
- 相同 `(instance, db_name, sql_hash, model)` 在 **7 天**内复用已有报告，避免重复烧 token（可在配置项调整窗口）。

### 5.8 权限与安全（P0）

| 维度 | 策略 |
|---|---|
| 功能开关 | 配置 `enable_ai_slowquery_diagnosis`（默认关）；关闭则隐藏入口 |
| 使用权限 | 新增 Django permission `sql.use_ai_diagnosis`；管理员可回收 |
| 查看权限 | 复用 `sql.menu_slowquery` |
| 写操作红线 | 仅生成工单草稿，提交走既有审核流 + goInception，零次直接执行 |
| 数据安全 | prompt 仅含 SQL 文本、表结构（DDL）、统计与执行计划，**不携带脱敏后的业务数据行** |
| 成本闸门 | 每用户/每日 token 配额，超限降级提示而非报错；复用 CopilotUsage 同类统计 |
| 审计 | 诊断动作、生成的工单均写入既有审计日志 |

### 5.9 P1 / P2 需求

**P1（快速跟进）**
- 批量诊断：对慢查 Top N 一键批量诊断，列表展示严重度/根因摘要。
- 反馈 👍/👎 + 原因；报告可复制全文。
- 根因健康度看板：高频 `bottleneck_type` 分布、待治理清单（按严重度排序）。

**P2（架构保险，本期不做）**
- MongoDB / Redis 深度根因（待 EXPLAIN 类能力稳定）。
- 与 Copilot 联动：Copilot 慢查场景直接引用本诊断报告，避免重复分析。
- 规则触发的周期治理工单（如"high 且持续 7 天自动建工单"）。
- 多模型 / 自调参（按 db_type 选不同模型）。

---

## 6. 关键流程

### 6.1 单条诊断主流程

```
用户点「AI 诊断」(sql_hash)
   │
   ▼
POST /api/v1/slowquery/diagnose/  {instance_name, db_name, sql_hash, force?}
   │
   ├─ 命中 7 天内同指纹报告且 force=false ──▶ 直接返回已有报告 id
   │
   ▼ 否则：创建 AIDiagnosisTask(pending) → 投递 django-q2 任务
   │
   ▼ 异步 diagnose_slowquery_task：
   1. 采集上下文（stats + trend + DDL + EXPLAIN）
   2. diagnose_slowquery_by_openai(上下文) → 结构化 JSON（容错解析）
   3. 落 AIDiagnosisReport；更新 Task → success
   │
   ▼ 前端轮询 GET /diagnose/<task_id>/ → 渲染报告卡片 + markdown
```

### 6.2 诊断 → 工单草稿闭环

```
报告建议卡片「生成工单草稿」
   │
   ▼
复用 sqlworkflow 创建接口（SQL 区填入 index_ddl / rewrite）
   │  标注来源=AI诊断，触发 _calc_ai_risk_summary
   ▼
用户确认 → 进入既有审核工作流 → goInception 检测 → 执行（不绕过）
```

---

## 7. 数据模型

新增到 `sql/models.py`（建议归入独立 `ai_diagnosis` 相关模型，便于解耦）：

```python
class AIDiagnosisTask(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    instance = models.ForeignKey(Instance, on_delete=models.CASCADE)
    db_name = models.CharField(max_length=128, blank=True, default="")
    sql_hash = models.CharField(max_length=128, db_index=True)
    status = models.CharField(max_length=16, default="pending")  # pending/running/success/failed
    model = models.CharField(max_length=64, blank=True, default="")
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        index_together = [("instance", "db_name", "sql_hash")]


class AIDiagnosisReport(models.Model):
    task = models.OneToOneField(AIDiagnosisTask, related_name="report", on_delete=models.CASCADE)
    sql_hash = models.CharField(max_length=128, db_index=True)
    root_cause = models.CharField(max_length=200, blank=True, default="")
    severity = models.CharField(max_length=16, default="unknown")   # low/medium/high/unknown
    bottleneck_type = models.CharField(max_length=32, blank=True, default="other")
    evidence = models.JSONField(default=list, blank=True)
    suggestions = models.JSONField(default=list, blank=True)        # [{type, desc, index_ddl, before, after}]
    report_markdown = models.TextField(blank=True, default="")
    confidence = models.FloatField(default=0.0)
    model = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)


class AIDiagnosisFeedback(models.Model):   # P1
    report = models.ForeignKey(AIDiagnosisReport, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    helpful = models.BooleanField(default=True)
    reason = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
```

> 原始 EXPLAIN / DDL 全文**不入库**（仅存 digest 摘要），避免库表膨胀与敏感结构外泄风险；`suggestions` 仅存结构化建议。

---

## 8. 接口契约

### 8.1 触发诊断（核心）

`POST /api/v1/slowquery/diagnose/`

```jsonc
{ "instance_name": "mysql-prod-01", "db_name": "orders", "sql_hash": "a1b2c3...", "force": false }
```

响应：

```jsonc
{ "status": 0, "msg": "ok", "data": { "task_id": 123, "hit_cache": false } }
// 命中缓存时 hit_cache=true 且直接可用 report_id
```

### 8.2 查询任务/报告

`GET /api/v1/slowquery/diagnose/<task_id>/` → 轮询；返回 `{status, report?}`。
`GET /api/v1/slowquery/diagnose/?instance_name=&db_name=&sql_hash=` → 返回该指纹已有报告（供"已诊断·查看"）。

### 8.3 反馈（P1）

`POST /api/v1/slowquery/diagnose/<report_id>/feedback/` → `{helpful: bool, reason?}`。

### 8.4 AI 客户端扩展

`common/utils/openai.py` 新增：

```python
def diagnose_slowquery_by_openai(self, db_type, db_name, sample_sql, stats, trend_summary, table_schemas, explain_text):
    """聚合统计/趋势/表结构/执行计划，输出结构化根因 JSON（容错解析）。"""
    # prompt 明确：基于给定证据推断 bottleneck_type 与 root_cause，
    # 索引建议必须给出可执行 DDL，改写建议给出前后对比；仅输出 JSON+markdown。
    ...
    return self._parse_diagnosis_json(content)  # 复用 _parse_review_json 的容错思路
```

### 8.5 复用现有能力（不重复造轮子）

- `check_openai_config()` / `test_openai_connection()`：配置探测。
- `api_dictionary.table_info`：取相关表 DDL。
- 查询引擎 `explain` / `explain_check`：取执行计划（v1 限 MySQL/PgSQL）。
- `sql_api/api_workflow.py` 创建接口 + `_calc_ai_risk_summary`：生成工单草稿。
- `collect_slowquery_task` 模式：异步任务投递。
- `AI_RISK_*` / `AI_LOCK_*` 常量与 `_parse_review_json` 容错：severity 归一与解析。

---

## 9. 技术方案

### 9.1 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│ Vue3 SPA  (slowquery 统计/明细页 + 报告抽屉)                  │
│   ├ 诊断按钮 / 轮询 composable                                │
│   └ 报告渲染（markdown + 结构化卡片 + EChart 趋势）           │
└───────────────────────────────┬─────────────────────────────┘
                          REST (polling)
┌───────────────────────────────▼─────────────────────────────┐
│ sql_api/api_slowquery_v2.py (新增 diagnose 视图)              │
│   上下文采集 → 投递 django-q2 任务 → 轮询返回                  │
└───────────┬───────────────────────┬──────────────────────────┘
            │                       │
   ┌────────▼─────────┐    ┌────────▼──────────────┐
   │ common/utils/    │    │ sql/ 既有能力          │
   │ openai.py        │    │ dictionary(DDL)        │
   │ diagnose_slow... │    │ engines(explain)       │
   └──────────────────┘    │ workflow(工单草稿)     │
                           └─────────────────────────┘
```

### 9.2 为什么用异步任务而非同步请求

- 根因分析需聚合多源上下文 + 一次（可能多次）AI 调用，端到端常 10–30s，远超 HTTP 同步期望。
- 与现有 `collect_slowquery_task` 同构，复用 django-q2 基建与失败重试。
- 前端轮询任务状态，体验与"采集"一致，无需引入 SSE（区别于 Copilot 流式）。

### 9.3 上下文与 token 治理

- 执行计划超长时仅保留 `type`/`key`/`rows`/`Extra` 关键字段做摘要。
- 相同指纹 7 天缓存复用，避免重复烧 token。
- 每用户/每日配额，超限降级提示（复用 CopilotUsage 同类统计）。
- 单请求 token 上限可配置。

### 9.4 解析容错（关键）

直接复用 `openai.py` 中已验证的多层容错：`_parse_review_json`（去代码块、抽首个 `{}`、裸换行转义、去尾逗号、全角标点归一、单引号转双引号）。新增 `diagnose_slowquery_by_openai` 输出 JSON 同样适用，降低解析失败率。任何异常返回 `DIAGNOSIS_FALLBACK`，不中断流程。

---

## 10. 埋点指标

| 事件名 | 触发时机 | 关键属性 |
|---|---|---|
| `slowquery.diagnose_start` | 提交诊断 | instance、db_name、sql_hash、hit_cache |
| `slowquery.diagnose_success` | 任务成功 | severity、bottleneck_type、tokens、latency_ms |
| `slowquery.diagnose_failed` | 任务失败 | error_code |
| `slowquery.report_view` | 查看报告 | report_id、severity |
| `slowquery.suggestion_adopt` | 生成工单草稿 | suggestion_type、bottleneck_type |
| `slowquery.feedback` | 👍/👎 | report_id、helpful、reason |
| `slowquery.diagnose_quota_limited` | 触达配额 | user_id、day |

**核心看板**：诊断覆盖率、p95 端到端时延、诊断成功率、报告👍率、诊断→工单转化率、人均 token 成本、各 `bottleneck_type` 分布（P1 看板）。

---

## 11. 分期计划

| 阶段 | 范围 | 验收要点 |
|---|---|---|
| **P0 MVP** | 单条诊断入口 + 异步生成 + 上下文采集（stats/trend/DDL/EXPLAIN）+ 结构化报告 + 报告展示 + 一键工单草稿 + 持久化/7天复用 + 开关/权限/审计 | 点「AI 诊断」→ 异步出报告；建议可一键生成工单草稿且走 goInception；同指纹 7 天内复用 |
| **P1** | 批量诊断 Top N、👍/👎 反馈、根因健康度看板 | 批量诊断可用；看板展示高频根因分布 |
| **P2** | Mongo/Redis 深度根因、Copilot 联动、规则触发治理工单、多模型 | 多引擎覆盖；Copilot 可引用报告 |

---

## 12. 风险与对策

| 风险 | 对策 |
|---|---|
| AI 索引建议错误/不适用 | 只生成工单草稿，不直接执行；工单走 goInception + 人工审核；报告明确"需人工确认" |
| EXPLAIN 大结果灌爆 prompt | 仅保留关键字段摘要；单请求 token 上限 |
| 非 MySQL/PgSQL 引擎 EXPLAIN 不稳定 | v1 限 MySQL/PgSQL 稳定支持；其他引擎标注"有限支持"，仅给通用改写 |
| token 成本失控 | 7 天缓存复用、趋势/EXPLAIN 摘要、每日配额、全局开关 |
| 上游 OpenAI 超时/抖动 | 失败重试 + `DIAGNOSIS_FALLBACK` 降级提示，不中断主流程 |
| 安全：敏感数据外泄 | prompt 仅含 SQL/结构/统计/执行计划，不携带脱敏后业务数据行 |
| 解析失败 | 复用 `openai.py` 多层容错解析 |

---

## 13. 验收标准（Acceptance）

- [ ] 慢查统计/明细页出现「AI 诊断」入口；无 `sql.use_ai_diagnosis` 或无 OpenAI 配置时置灰并提示。
- [ ] 点「AI 诊断」触发异步任务，前端可轮询状态；生成中/失败态有清晰提示与重试。
- [ ] 报告含结构化字段（root_cause / severity / bottleneck_type / evidence / suggestions）且 markdown 正常渲染。
- [ ] 建议可一键「生成工单草稿」，进入既有工单提交流程且**不绕过** goInception 检测。
- [ ] 同 `(instance, db_name, sql_hash, model)` 7 天内重复点击直接复用报告，不重复调用 AI。
- [ ] 诊断结果落库可复查；诊断动作与生成工单入审计日志；写操作零次直接执行。
- [ ] 触达每日 token 配额时降级提示而非崩溃。
- [ ] 仅 MySQL/PgSQL 给出深度根因；其他引擎标注"有限支持"不误导。

---

## 14. 开放问题（Open Questions）

| 问题 | 需回答方 | 阻塞级 |
|---|---|---|
| v1 是否默认开启 `enable_ai_slowquery_diagnosis`？还是仅超管手动开？ | 产品 / 运维 | 阻塞（影响上线策略） |
| 7 天复用窗口是否合适？是否按 db_type / 数据量动态？ | 产品 / DBA | 非阻塞 |
| `severity` 与现有 `review_sql_by_openai` 的风险分是否统一一套评分标准？ | 工程 | 非阻塞 |
| P1 看板是否需要对接现有 dashboard（ECharts）还是独立页？ | 产品 / 前端 | 非阻塞（P1） |
| Mongo/Redis 深度根因的 EXPLAIN 等价数据如何获取？ | 工程（引擎组） | 非阻塞（P2） |
