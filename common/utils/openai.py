import json
import logging
import re
import time

from openai import OpenAI
from common.config import SysConfig
from common.utils.ai_prompts import (
    build_diagnosis_prompt,
    build_nl2sql_guard,
    build_optimize_prompt,
    build_review_batch_prompt,
    build_review_prompt,
)
from common.utils.ai_risk import (
    AI_LEVEL_HIGH as AI_RISK_HIGH,
    AI_LEVEL_LOW as AI_RISK_LOW,
    AI_LEVEL_MEDIUM as AI_RISK_MEDIUM,
    AI_LEVEL_UNKNOWN as AI_RISK_UNKNOWN,
    BOTTLENECK_LABELS,
    normalize_level,
    score_band,
    severity_from_stats,
)
from django.template import Context, Template

logger = logging.getLogger("default")


# AI 审核：DDL 锁表风险等级（等级枚举复用 ai_risk，审核链路专有词汇）
AI_LOCK_NONE = "none"  # 非 DDL，无锁表风险
AI_LOCK_LOW = "low"  # DDL 但小表/在线变更，风险低
AI_LOCK_MEDIUM = "medium"  # 中等表，可能短暂锁
AI_LOCK_HIGH = "high"  # 大表 DDL，长时间锁表

# AI 审核默认占位（容错时返回，避免中断检测流程）
AI_REVIEW_FALLBACK = {
    "risk_level": AI_RISK_UNKNOWN,
    "risk_score": 0,
    "summary": "AI 审核跳过",
    "suggestion": "",
    "ddl_lock_risk": AI_LOCK_NONE,
    "affected_rows_estimate": "",
    "use_osc": False,
}

# AI 慢查诊断：瓶颈类型常量
DIAG_BOTTLENECK_FULL_SCAN = "full_scan"
DIAG_BOTTLENECK_MISSING_INDEX = "missing_index"
DIAG_BOTTLENECK_LOCK_WAIT = "lock_wait"
DIAG_BOTTLENECK_FILESORT = "filesort"
DIAG_BOTTLENECK_TMP_TABLE = "tmp_table"
DIAG_BOTTLENECK_TYPE_CAST = "type_cast"
DIAG_BOTTLENECK_OTHER = "other"

DIAG_VALID_BOTTLENECKS = set(BOTTLENECK_LABELS)

# AI 慢查诊断：降级占位（任何 AI 异常一律返回此值，绝不中断诊断流程）
DIAGNOSIS_FALLBACK = {
    "root_cause": "AI 诊断跳过",
    "severity": AI_RISK_UNKNOWN,
    "bottleneck_type": DIAG_BOTTLENECK_OTHER,
    "evidence": [],
    "suggestions": [],
    "confidence": 0.0,
    # 内部标记：调用方（diagnose_slowquery_task）据此把任务判为 failed，
    # 避免降级空报告以 success 落库并被 7 天缓存复用、用户永远无法重试
    "_is_fallback": True,
}


class AIScenario:
    """AI 调用场景默认参数：超时/重试/输出上限/思考开关按场景收敛配置。

    优先级：调用处显式入参 > 场景配置 > SDK 默认。
    """

    def __init__(self, timeout=60, max_retries=1, max_tokens=None, disable_thinking=False):
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self.disable_thinking = disable_thinking


# 场景登记：各能力链路的调用默认值单点维护。
# 超时上限需与调用方约束（前端超时/任务框架超时）对齐，改动前核对引用处注释。
AI_SCENARIOS = {
    # NL2SQL：交互式生成，常规超时
    "nl2sql": AIScenario(timeout=60, max_retries=1),
    # SQL 优化单轮模式：同步接口 90s/次（含 1 次重试最坏 ~180s），
    # 需 < 前端该接口的 300s 超时，避免响应送到时连接已被掐断
    "sql_optimize": AIScenario(timeout=90, max_retries=1),
    # Agent 模式的单轮调用（循环整体另有 240s 预算，见 ai_optimizer.AGENT_DEADLINE_SECONDS）
    "sql_optimize_agent": AIScenario(timeout=60, max_retries=1),
    # 工单 AI 审核：检测接口内逐条调用
    "sql_review": AIScenario(timeout=60, max_retries=1),
    # 慢查诊断的三项特例（不重试/限输出/关思考）在 diagnose_slowquery_by_openai
    # 内同时显式声明，防裸 client 调用时退化：推理模型长思考会耗尽 max_tokens、
    # 重试会把耗时翻倍逼近 django-q 任务超时（180s）
    "slowquery_diagnosis": AIScenario(
        timeout=60, max_retries=0, max_tokens=2000, disable_thinking=True
    ),
    # 配置页"测试连接"：最简请求快速失败
    "connection_test": AIScenario(timeout=15, max_retries=0, max_tokens=1),
}


class OpenaiClient:
    def __init__(self, timeout: int = None, scenario: str = ""):
        all_config = SysConfig()
        self.base_url = all_config.get("openai_base_url", "")
        self.api_key = all_config.get("openai_api_key", "")
        self.default_chat_model = all_config.get("default_chat_model", "gpt-3.5-turbo")
        self.default_query_template = all_config.get(
            "default_query_template",
            "你是一个熟悉 {{db_type}} 的资深工程师。\n"
            "请严格根据以下【表结构 DDL】中给出的表名和字段名，结合用户描述，生成一条可直接执行的查询 SQL。\n"
            "重要：只能使用 DDL 中出现的表名和字段名，禁止编造或推测不存在的表和字段。\n"
            "请参考【样本数据】中字段值的实际格式（如命名风格、编码方式），"
            "在 WHERE 条件中使用正确的值，而不是凭常识猜测。\n"
            "要求：仅返回 SQL 语句本身，不要返回注释、序号或 markdown 代码块。\n\n"
            "【表结构 DDL】\n{{table_schema}}\n\n"
            "【样本数据】\n{{sample_data}}\n\n"
            "【查询需求】\n{{user_input}}",
        )
        sc = AI_SCENARIOS.get(scenario)
        self.scenario = scenario
        self.scenario_max_retries = sc.max_retries if sc else None
        self.scenario_max_tokens = sc.max_tokens if sc else None
        self.scenario_disable_thinking = sc.disable_thinking if sc else False
        if timeout is None:
            timeout = sc.timeout if sc else 60
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=timeout,   # 单次请求上限，避免 AI 服务无响应时任务无限挂起
            max_retries=1,
        )
        # 遥测捕获（统一记账 record_ai_usage 的数据来源）：
        # last_* 为最近一次调用，total_* 为本 client 实例生命周期内累计
        # （client 均按单次操作创建，累计值即该次操作的总量，Agent 多轮循环复用）
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.last_latency_ms = 0
        self.total_latency_ms = 0

    def _create_chat_completion(self, *, model, messages, max_retries=None, **kwargs):
        """发起 chat 补全，套用场景默认值并尽力捕获 token 用量与耗时。

        usage/延迟采集是尽力而为：部分 OpenAI 兼容网关可能不返回 usage
        （测试中亦可能喂入非 SDK 响应对象），缺失时保持 0，不影响补全结果。
        """
        if max_retries is None:
            max_retries = self.scenario_max_retries
        if "max_tokens" not in kwargs and self.scenario_max_tokens:
            kwargs["max_tokens"] = self.scenario_max_tokens
        if "extra_body" not in kwargs and self.scenario_disable_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        t0 = time.monotonic()
        if max_retries is not None:
            completion = self.client.with_options(max_retries=max_retries).chat.completions.create(
                model=model, messages=messages, **kwargs
            )
        else:
            completion = self.client.chat.completions.create(
                model=model, messages=messages, **kwargs
            )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        self.last_latency_ms = elapsed_ms
        self.total_latency_ms += elapsed_ms
        usage = getattr(completion, "usage", None)
        prompt_tokens = completion_tokens = 0
        if usage is not None:
            try:
                prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            except (TypeError, ValueError):
                prompt_tokens = 0
            try:
                completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            except (TypeError, ValueError):
                completion_tokens = 0
        self.last_usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        self.total_usage["prompt_tokens"] += prompt_tokens
        self.total_usage["completion_tokens"] += completion_tokens
        return completion

    def _create_structured(self, *, model, messages, **kwargs):
        """结构化输出请求：优先 json_object 模式（强制模型只产合法 JSON，
        OpenAI 及主流兼容网关广泛支持），网关不支持该参数时（通常快速 400）
        降级为普通补全，由 _extract_json_object 的容错解析链兜底。

        仅用于无严格时限约束的同步链路（工单审核）；诊断任务受 django-q
        180s 超时约束，降级重试会让最坏耗时翻倍，故不启用。
        """
        try:
            return self._create_chat_completion(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                **kwargs,
            )
        except Exception as e:
            logger.warning(f"json_object 模式不可用，降级普通补全: {e}")
            return self._create_chat_completion(model=model, messages=messages, **kwargs)

    def request_chat_completion(self, messages, **kwargs):
        """chat_completion"""
        return self._create_chat_completion(
            model=self.default_chat_model, messages=messages, **kwargs
        )

    def generate_sql_by_openai(self, db_type: str, table_schema: str, user_input: str, sample_data: str = ""):
        """根据传入的基本信息生成查询语句"""
        template = Template(self.default_query_template)
        current_context = Context(
            dict(db_type=db_type, table_schema=table_schema, user_input=user_input, sample_data=sample_data)
        )
        # 注入防护：查询模板允许用户自定义，数据边界声明附在渲染结果之后，
        # 确保 DDL/样本数据不被当作指令（不受模板内容影响）
        prompt = template.render(current_context) + build_nl2sql_guard()
        messages = [dict(role="user", content=prompt)]
        logger.info(messages)
        try:
            res = self.request_chat_completion(messages)
            return res.choices[0].message.content
        except Exception as e:
            raise ValueError(f"请求openai生成查询语句失败: {e}")

    def optimize_sql_by_openai(
        self,
        db_type: str,
        db_name: str,
        sql_text: str,
        table_schemas: str,
    ):
        """结合表结构上下文，对 SQL 给出优化建议，返回 markdown 报告"""
        prompt = build_optimize_prompt(
            db_type=db_type,
            db_name=db_name,
            sql_text=sql_text,
            table_schemas=table_schemas,
        )
        messages = [dict(role="user", content=prompt)]
        logger.info(messages)
        try:
            res = self.request_chat_completion(messages)
            return res.choices[0].message.content
        except Exception as e:
            raise ValueError(f"请求openai优化SQL失败: {e}")

    def review_sql_by_openai(
        self,
        db_type: str,
        db_name: str,
        sql_text: str,
        table_schemas: str,
        table_rows: str,
    ):
        """对单条 SQL 做风险审核 + 变更影响预测，返回结构化结果。

        输出 dict：
            {
                "risk_level": "low" | "medium" | "high",
                "risk_score": int (0-100),
                "summary": str,                 # 一句话总结，供表格内展示
                "suggestion": str,              # 详细建议（markdown）
                "ddl_lock_risk": "none"|"low"|"medium"|"high",  # DDL 锁表风险
                "affected_rows_estimate": str,  # 影响行数预估（如 "约132万行"）
                "use_osc": bool                 # 是否建议走 gh-ost/pt-osc 在线变更
            }

        纯参考、不阻断：任何异常都返回 AI_REVIEW_FALLBACK（risk_level=unknown），
        绝不抛异常中断外层检测流程。
        """
        prompt = build_review_prompt(
            db_type=db_type,
            db_name=db_name,
            sql_text=sql_text,
            table_schemas=table_schemas,
            table_rows=table_rows,
        )
        messages = [dict(role="user", content=prompt)]
        try:
            # json_object 模式优先（网关不支持时自动降级普通补全）
            res = self._create_structured(model=self.default_chat_model, messages=messages)
            content = res.choices[0].message.content
            return self._parse_review_json(content)
        except Exception as e:
            logger.warning(f"AI 审核 SQL 失败，降级返回 unknown: {e}")
            return dict(AI_REVIEW_FALLBACK)

    def review_sql_batch_by_openai(
        self, db_type: str, db_name: str, statements: list, table_schemas: str, table_rows: str
    ):
        """批量审核：一次评审多条 SQL，返回与 statements 等长的归一结果 list。

        位置用每项的 index 字段对齐；整体解析失败返回 None（调用方回退逐条），
        个别项缺失时对应位置为 None（调用方可对该项单独回退）。
        """
        prompt = build_review_batch_prompt(
            db_type=db_type,
            db_name=db_name,
            statements=statements,
            table_schemas=table_schemas,
            table_rows=table_rows,
        )
        messages = [dict(role="user", content=prompt)]
        logger.info(f"AI 批量审核 prompt 长度: {len(prompt)} 字符，语句数: {len(statements)}")
        try:
            res = self._create_structured(model=self.default_chat_model, messages=messages)
            content = res.choices[0].message.content
            return self._parse_review_batch_json(content, len(statements))
        except Exception as e:
            logger.warning(f"AI 批量审核失败: {e}")
            return None

    def diagnose_slowquery_by_openai(
        self,
        db_type: str,
        db_name: str,
        sample_sql: str,
        stats: dict,
        trend_summary: str,
        table_schemas: str,
        explain_text: str,
    ):
        """聚合统计/趋势/表结构/执行计划，输出结构化根因 JSON（容错解析）。

        Args:
            db_type: 数据库类型（mysql / pgsql / mongo / redis）
            db_name: 数据库名
            sample_sql: 慢查示例 SQL（或指纹）
            stats: 统计指标 dict，含 query_time_p95 / total_execution_counts /
                   parse_total_row_counts / return_total_row_counts 等
            trend_summary: 近期趋势摘要文本（如"近 14 天 p95 由 0.3s 升至 8s"）
            table_schemas: 相关表 DDL 文本
            explain_text: 执行计划摘要文本

        Returns:
            结构化 dict，字段见 DIAGNOSIS_FALLBACK。任何 AI 异常一律返回
            DIAGNOSIS_FALLBACK，绝不抛异常中断诊断流程。
        """
        # 构建统计指标摘要
        p95 = stats.get("query_time_p95", 0)
        exec_count = stats.get("total_execution_counts", 0)
        rows_examined = stats.get("parse_total_row_counts", 0)
        rows_returned = stats.get("return_total_row_counts", 0)
        scan_return_ratio = (
            f"{rows_examined / rows_returned:g}:1"
            if rows_returned and rows_returned > 0
            else "N/A"
        )

        # MongoDB 的行级统计实为文档级统计，措辞用"文档"
        row_unit = "文档" if db_type == "mongo" else "行"
        stats_text = (
            f"- p95 执行耗时: {p95} ms\n"
            f"- 总执行次数: {exec_count}\n"
            f"- 总扫描{row_unit}数: {rows_examined}\n"
            f"- 总返回{row_unit}数: {rows_returned}\n"
            f"- 扫描/返回比: {scan_return_ratio}\n"
        )
        # MongoDB 特有上下文：集合/操作类型/是否排序
        if db_type == "mongo":
            mongo_ctx = []
            coll = stats.get("collection_name", "")
            op = stats.get("operation_type", "")
            if coll:
                mongo_ctx.append(f"集合: {coll}")
            if op:
                mongo_ctx.append(f"操作类型: {op}")
            if stats.get("has_sort") is not None:
                mongo_ctx.append(f"包含排序: {'是' if stats.get('has_sort') else '否'}")
            if mongo_ctx:
                stats_text += "- " + ", ".join(mongo_ctx) + "\n"
        # 无行级统计的数据库类型（如 Redis）标注说明，避免 AI 误读
        if not rows_examined and not rows_returned:
            stats_text += "- 说明: 该数据库类型未采集行级统计，扫描/返回比不可用\n"

        # MongoDB 专属语料：bottleneck 语义映射 + 索引/改写产出格式
        mongo_guide = ""
        if db_type == "mongo":
            mongo_guide = (
                "\nMongoDB 专属说明：\n"
                "- bottleneck_type 映射：full_scan≈COLLSCAN 集合扫描、"
                "filesort≈内存 SORT 排序（可能触发 32MB 排序内存限制）、"
                "tmp_table≈$group/$lookup 内存聚合、"
                "type_cast≈字段类型不匹配导致索引失效；\n"
                "- 索引建议的 index_ddl 字段请给 createIndex 命令，"
                "如 db.collection.createIndex({field: 1}, {background: true})；\n"
                "- 改写建议（before/after）给 mongo shell 命令或聚合管道对比。\n"
            )

        # 章节标签按数据库类型切换（mongo 用"集合索引/示例命令"措辞）
        schema_label = "集合索引" if db_type == "mongo" else "相关表结构 DDL"
        sample_label = "慢查示例命令" if db_type == "mongo" else "慢查示例 SQL"

        prompt = build_diagnosis_prompt(
            db_type=db_type,
            db_name=db_name,
            stats_text=stats_text,
            trend_summary=trend_summary,
            table_schemas=table_schemas,
            explain_text=explain_text,
            sample_sql=sample_sql,
            schema_label=schema_label,
            sample_label=sample_label,
            extra_guide=mongo_guide,
        )
        messages = [dict(role="user", content=prompt)]
        logger.info(f"AI 慢查诊断 prompt 长度: {len(prompt)} 字符")
        try:
            # max_tokens 限制输出长度：输出为紧凑的结构化 JSON（report_markdown 留空，
            # 前端直接渲染结构化字段），无需超长输出，避免模型生成冗长内容导致耗时成倍增加。
            # max_retries=0：诊断对失败容忍（降级 DIAGNOSIS_FALLBACK），
            # 重试只会把最长耗时从 60s 翻倍到 120s，逼近 django-q 任务超时（180s）导致
            # 任务被硬杀、状态永久卡 running。故诊断路径强制单次尝试。
            # extra_body thinking=disabled：DeepSeek 推理类模型（如 deepseek-v4-flash）
            # 对复杂诊断 prompt 会陷入长思考，把 max_tokens 预算全耗在 reasoning_tokens 上，
            # 导致 content 为空、finish=length、JSON 解析失败降级 fallback。显式关闭思考
            # 让其直接输出结构化结果（实测耗时 42.8s→9s，JSON 完整）。
            res = self._create_chat_completion(
                model=self.default_chat_model,
                messages=messages,
                max_retries=0,
                max_tokens=2000,
                extra_body={"thinking": {"type": "disabled"}},
            )
            content = res.choices[0].message.content
            result = self._parse_diagnosis_json(content)
            # 记录 token 使用量
            if hasattr(res, "usage") and res.usage:
                result["_prompt_tokens"] = res.usage.prompt_tokens or 0
                result["_completion_tokens"] = res.usage.completion_tokens or 0
            # 统计指标兜底严重度：模型可能忽略规则判低，以统计数据为准覆写
            self._apply_stat_severity(result, stats)
            return result
        except Exception as e:
            logger.warning(f"AI 慢查诊断失败，降级返回 fallback: {e}")
            return dict(DIAGNOSIS_FALLBACK)

    @staticmethod
    def _apply_stat_severity(result: dict, stats: dict) -> None:
        """用统计指标对严重度做规则兜底（阈值见 ai_risk.severity_from_stats，
        与诊断 prompt 判定说明同源）。统计缺失时不覆写，保留模型判定。
        """
        if not stats:
            return
        severity = severity_from_stats(
            stats.get("query_time_p95"),
            stats.get("parse_total_row_counts"),
            stats.get("return_total_row_counts"),
        )
        if severity:
            result["severity"] = severity

    @staticmethod
    def _parse_diagnosis_json(content: str):
        """解析 AI 返回的慢查诊断结果。

        JSON 提取复用 _extract_json_object 的多层容错，此处仅做诊断字段
        校验与归一。report_markdown 仅保留模型主动编写的叙述（prompt 已要求
        留空）——前端直接渲染结构化字段，服务端不再拼装重复的"完整报告"。
        """
        data = OpenaiClient._extract_json_object(content)
        if data is None:
            logger.warning(f"AI 诊断结果解析失败，原始内容: {(content or '')[:200]}")
            return dict(DIAGNOSIS_FALLBACK)

        # 字段校验与归一
        root_cause = OpenaiClient._strip_emoji(
            str(data.get("root_cause", ""))[:200]
        ) or "AI 诊断完成"

        severity = normalize_level(data.get("severity"))

        bottleneck = str(data.get("bottleneck_type", "")).lower()
        if bottleneck not in DIAG_VALID_BOTTLENECKS:
            bottleneck = DIAG_BOTTLENECK_OTHER

        evidence_raw = data.get("evidence", [])
        if not isinstance(evidence_raw, list):
            evidence_raw = [str(evidence_raw)]
        evidence = [
            OpenaiClient._strip_emoji(str(e)) for e in evidence_raw if e
        ]

        suggestions_raw = data.get("suggestions", [])
        if not isinstance(suggestions_raw, list):
            suggestions_raw = []
        suggestions = []
        for s in suggestions_raw:
            if not isinstance(s, dict):
                continue
            suggestions.append({
                "type": str(s.get("type", "other")),
                "desc": OpenaiClient._strip_emoji(str(s.get("desc", ""))),
                "index_ddl": str(s.get("index_ddl", "") or ""),
                "before": str(s.get("before", "") or ""),
                "after": str(s.get("after", "") or ""),
            })

        try:
            confidence = float(data.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0

        result = {
            "root_cause": root_cause,
            "severity": severity,
            "bottleneck_type": bottleneck,
            "evidence": evidence,
            "suggestions": suggestions,
            "confidence": confidence,
        }
        return result

    @staticmethod
    def _normalize_review_fields(data: dict) -> dict:
        """单条审核结果的字段校验与归一（单条/批量两条链路共用）。"""
        level = normalize_level(data.get("risk_level"))
        try:
            score = int(data.get("risk_score", 0))
            score = max(0, min(100, score))
        except (TypeError, ValueError):
            score = 0
        # DDL 锁表风险归一
        lock = str(data.get("ddl_lock_risk", AI_LOCK_NONE)).lower()
        if lock not in (AI_LOCK_NONE, AI_LOCK_LOW, AI_LOCK_MEDIUM, AI_LOCK_HIGH):
            lock = AI_LOCK_NONE
        # 影响行数预估（字符串，直接取）
        affected = str(data.get("affected_rows_estimate", "") or "")
        # use_osc 归一为 bool
        osc_raw = data.get("use_osc", False)
        if isinstance(osc_raw, str):
            use_osc = osc_raw.strip().lower() in ("true", "1", "yes")
        else:
            use_osc = bool(osc_raw)
        return {
            "risk_level": level,
            "risk_score": score,
            "summary": OpenaiClient._strip_emoji(
                str(data.get("summary", ""))[:200]
            )
            or "AI 审核完成",
            "suggestion": OpenaiClient._strip_emoji(
                str(data.get("suggestion", ""))
            ),
            "ddl_lock_risk": lock,
            "affected_rows_estimate": OpenaiClient._strip_emoji(affected),
            "use_osc": use_osc,
        }

    @staticmethod
    def _parse_review_json(content: str):
        """解析单条 AI 审核结果（json_object 优先 + 容错提取 + 字段归一）。"""
        data = OpenaiClient._extract_json_object(content)
        if data is None:
            logger.warning(f"AI 审核结果解析失败，原始内容: {(content or '')[:200]}")
            return dict(AI_REVIEW_FALLBACK)
        return OpenaiClient._normalize_review_fields(data)

    @staticmethod
    def _parse_review_batch_json(content: str, expected_count: int):
        """解析批量审核结果（JSON 数组，按 index 对齐为等长 list）。

        兼容模型把数组包成 {"reviews": [...]} 的对象形态。整体不可解析
        返回 None（调用方回退逐条审核）；个别项缺失/越界时对应位置为
        None（调用方可对该项单独回退）。
        """
        data = OpenaiClient._extract_json_object(content)
        if isinstance(data, dict):
            for key in ("reviews", "results", "items", "statements"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            return None
        results = [None] * expected_count
        for item in data:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            # bool 是 int 子类，显式排除
            if isinstance(idx, bool) or not isinstance(idx, int):
                continue
            if not (0 <= idx < expected_count):
                continue
            if results[idx] is None:
                results[idx] = OpenaiClient._normalize_review_fields(item)
        return results

    @staticmethod
    def _strip_emoji(text: str) -> str:
        """移除 emoji 表情及杂项符号，保持输出专业。

        覆盖常见 emoji 区块：杂项符号与象形文字、表情符号、补充符号、
        交通符号、旗帜等。同时压缩 emoji 移除后可能残留的多余空白。
        """
        if not text:
            return text
        cleaned = re.sub(
            "["
            "\U0001F300-\U0001FAFF"  # 符号与象形文字 / 表情符号 / 补充
            "\U00002600-\U000027BF"  # 杂项符号 / 装饰符号
            "\U0001F1E6-\U0001F1FF"  # 旗帜区域指示符
            "\U0001F900-\U0001F9FF"  # 补充符号与象形文字
            "]+",
            "",
            text,
        )
        # 压缩因 emoji 删除产生的连续空白（但保留换行）
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        return cleaned.strip()

    @staticmethod
    def _try_load_json(text: str):
        """尝试解析 JSON，对 LLM 常见不规范输出做多层容错修复。

        依次尝试：
        1. 直接 json.loads；
        2. 字符串值内裸露换行符/制表符转义；
        3. 去除尾部逗号（],} 前的 ,）；
        4. 单引号 → 双引号（仅键名/标量，逐字符扫描避免误伤字符串内容）；
        5. 中文标点（“”‘’，：）替换为 ASCII 标点。

        返回解析后的 dict，或 None（解析失败）。
        """
        # 第1层：直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 第2层：字符串值内裸露控制字符转义（逐字符扫描，跟踪是否在字符串内）
        escaped = []
        in_str = False
        escape = False
        for ch in text:
            if escape:
                escaped.append(ch)
                escape = False
                continue
            if ch == "\\":
                escaped.append(ch)
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                escaped.append(ch)
                continue
            if in_str and ch == "\n":
                escaped.append("\\n")
            elif in_str and ch == "\r":
                escaped.append("\\r")
            elif in_str and ch == "\t":
                escaped.append("\\t")
            else:
                escaped.append(ch)
        text2 = "".join(escaped)
        try:
            return json.loads(text2)
        except json.JSONDecodeError:
            pass

        # 第3层：去除尾部逗号（}, ] 前的逗号，可能带空白）
        text3 = re.sub(r",\s*([}\]])", r"\1", text2)
        if text3 != text2:
            try:
                return json.loads(text3)
            except json.JSONDecodeError:
                pass

        # 第4层：中文全角标点 → ASCII（LLM 中文输出常带全角逗号/冒号/引号）
        text4 = text3.translate(
            str.maketrans(
                {
                    "\u201c": '"',  # “
                    "\u201d": '"',  # ”
                    "\u2018": "'",  # ‘
                    "\u2019": "'",  # ’
                    "\uff0c": ",",  # ，
                    "\uff1a": ":",  # ：
                    "\uff1b": ";",  # ；
                }
            )
        )
        if text4 != text3:
            try:
                return json.loads(text4)
            except json.JSONDecodeError:
                pass

        # 第5层：单引号 → 双引号。
        # 仅替换"结构位置"的单引号（键名 + 非字符串标量），逐字符扫描避免
        # 误伤字符串内部的撇号（如英文 it's）。
        text5 = OpenaiClient._single_to_double_quote(text4)
        if text5 != text4:
            try:
                return json.loads(text5)
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def _single_to_double_quote(text: str):
        """把 JSON 结构位置的单引号转成双引号，保留字符串内部的单引号。

        用状态机：区分"在字符串内"和"在结构位置"。结构位置的单引号
        （紧跟 key 或作为字符串边界）转双引号；字符串内的撇号保留。
        """
        out = []
        in_dq = False  # 是否在双引号字符串内
        in_sq = False  # 是否在单引号字符串内
        escape = False
        for ch in text:
            if escape:
                out.append(ch)
                escape = False
                continue
            if ch == "\\":
                out.append(ch)
                escape = True
                continue
            if in_dq:
                out.append(ch)
                if ch == '"':
                    in_dq = False
                continue
            if in_sq:
                if ch == "'":
                    out.append('"')
                    in_sq = False
                else:
                    out.append(ch)
                continue
            # 结构位置
            if ch == '"':
                in_dq = True
                out.append(ch)
            elif ch == "'":
                in_sq = True
                out.append('"')
            else:
                out.append(ch)
        return "".join(out)

    @staticmethod
    def _strip_codefence(text: str) -> str:
        """剥掉 markdown 代码块包裹（```json ... ```），非代码块文本原样返回。"""
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        return text

    @staticmethod
    def _extract_json_object(content: str):
        """从 LLM 输出中容错提取首个 JSON 对象，失败返回 None。

        三层候选依次尝试：整段 → 剥代码块包裹 → 抽取首个 {...}（DOTALL 跨行，
        兼容 JSON 前后有解释性文字）；每层候选复用 _try_load_json 的修复链
        （字符串内裸换行转义、尾逗号、全角标点、结构位单引号）。
        审核/诊断两条结构化链路共用，避免容错逻辑分叉。
        """
        if not content:
            return None
        text = content.strip()
        candidates = [text, OpenaiClient._strip_codefence(text)]
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            candidates.append(match.group(0))
        for candidate in candidates:
            if candidate:
                data = OpenaiClient._try_load_json(candidate)
                if data is not None:
                    return data
        return None


def record_ai_usage(
    capability,
    client=None,
    usage=None,
    model="",
    db_type="",
    instance_name="",
    db_name="",
    user_name="",
    latency_ms=None,
    status="success",
    cache_hit=False,
    error="",
):
    """统一 AI 用量记账（AIUsageLog）：NL2SQL / SQL优化 / 工单审核 / 慢查诊断共用。

    tokens 来源优先级：显式传入的 usage dict > client.last_usage（单次调用口径）。
    Agent 多轮调用请传 client.total_usage（client 实例生命周期内累计）。
    latency_ms 同理：显式传入 > client.last_latency_ms（客户端在传输层采集，
    口径统一为纯 AI 调用耗时，不含上下文收集）。

    任何异常只写告警日志、绝不抛出——记账是旁路增强，不能影响业务主流程。
    """
    try:
        from sql.models import AIUsageLog

        if usage is None and client is not None:
            usage = getattr(client, "last_usage", None)
        if latency_ms is None and client is not None:
            latency_ms = getattr(client, "last_latency_ms", 0)
        prompt_tokens = completion_tokens = 0
        if isinstance(usage, dict):
            for key, target in (("prompt_tokens", "p"), ("completion_tokens", "c")):
                try:
                    value = int(usage.get(key) or 0)
                except (TypeError, ValueError):
                    value = 0
                if target == "p":
                    prompt_tokens = value
                else:
                    completion_tokens = value

        AIUsageLog.objects.create(
            capability=capability,
            model=str(model or getattr(client, "default_chat_model", "") or "")[:64],
            db_type=str(db_type or "")[:32],
            instance_name=str(instance_name or "")[:128],
            db_name=str(db_name or "")[:128],
            user_name=str(user_name or "")[:128],
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=int(latency_ms or 0),
            cache_hit=bool(cache_hit),
            status=str(status or "success")[:16],
            error=str(error or "")[:500],
        )
    except Exception as e:
        logger.warning(f"AI 用量记账失败（忽略）: {e}")


def check_openai_config():
    """校验openai必需配置openai_api_key是否存在"""
    all_config = SysConfig()
    api_key = all_config.get("openai_api_key")
    if api_key:
        return True
    return False


def test_openai_connection(base_url=None, api_key=None, model=None):
    """测试 AI 服务连通性。

    可显式传入临时参数（用于配置页"测试连接"，此时尚未保存）；
    不传则读取 SysConfig 已保存的配置。发送一个最简 chat 请求验证。
    成功返回 (True, 模型名)，失败返回 (False, 错误信息)。
    """
    all_config = SysConfig()
    base_url = base_url if base_url is not None else all_config.get("openai_base_url", "")
    api_key = api_key if api_key is not None else all_config.get("openai_api_key", "")
    model = model if model is not None else all_config.get(
        "default_chat_model", "gpt-3.5-turbo"
    )
    if not api_key:
        return False, "AI API Key 未配置"
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=15, max_retries=0)
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        client.close()
        return True, model
    except Exception as e:
        return False, str(e)
