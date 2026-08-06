import json
import logging
import re

from openai import OpenAI
from common.config import SysConfig
from django.template import Context, Template

logger = logging.getLogger("default")


# AI 审核：风险等级常量
AI_RISK_LOW = "low"
AI_RISK_MEDIUM = "medium"
AI_RISK_HIGH = "high"
AI_RISK_UNKNOWN = "unknown"

# AI 审核：DDL 锁表风险等级
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

DIAG_VALID_BOTTLENECKS = {
    DIAG_BOTTLENECK_FULL_SCAN, DIAG_BOTTLENECK_MISSING_INDEX,
    DIAG_BOTTLENECK_LOCK_WAIT, DIAG_BOTTLENECK_FILESORT,
    DIAG_BOTTLENECK_TMP_TABLE, DIAG_BOTTLENECK_TYPE_CAST,
    DIAG_BOTTLENECK_OTHER,
}

# AI 慢查诊断：降级占位（任何 AI 异常一律返回此值，绝不中断诊断流程）
DIAGNOSIS_FALLBACK = {
    "root_cause": "AI 诊断跳过",
    "severity": AI_RISK_UNKNOWN,
    "bottleneck_type": DIAG_BOTTLENECK_OTHER,
    "evidence": [],
    "suggestions": [],
    "confidence": 0.0,
    "report_markdown": "AI 诊断因服务异常暂不可用，请稍后重试。",
}


class OpenaiClient:
    def __init__(self):
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
        self.client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=60,        # 单次请求上限，避免 AI 服务无响应时任务无限挂起
            max_retries=1,
        )

    def request_chat_completion(self, messages, **kwargs):
        """chat_completion"""
        completion = self.client.chat.completions.create(
            model=self.default_chat_model, messages=messages, **kwargs
        )
        return completion

    def generate_sql_by_openai(self, db_type: str, table_schema: str, user_input: str, sample_data: str = ""):
        """根据传入的基本信息生成查询语句"""
        template = Template(self.default_query_template)
        current_context = Context(
            dict(db_type=db_type, table_schema=table_schema, user_input=user_input, sample_data=sample_data)
        )
        messages = [dict(role="user", content=template.render(current_context))]
        logger.info(messages)
        try:
            res = self.request_chat_completion(messages)
            return res.choices[0].message.content
        except Exception as e:
            raise ValueError(f"请求openai生成查询语句失败: {e}")

    def analyze_sql_by_openai(self, sql_text: str):
        """对一段 SQL 进行语法/规范/潜在问题的评审，返回 markdown 报告"""
        prompt = (
            "你是一位资深的 DBA 和 SQL 审核专家。请对下面的 SQL 语句进行审核分析，"
            "从语法正确性、书写规范（关键字大小写/表别名/字段显式列出等）、"
            "潜在性能问题（如 SELECT *、缺少 WHERE、隐式类型转换、OR 条件、LIKE 前缀通配等）、"
            "安全风险（SQL 注入、危险操作）等方面给出评审意见。\n"
            "请用 Markdown 格式输出，结构清晰，包含「问题清单」和「改进建议」两部分，"
            "每条建议尽量给出修改前后的对比示例。不要输出与 SQL 无关的内容。\n\n"
            f"待审核的 SQL：\n{sql_text}"
        )
        messages = [dict(role="user", content=prompt)]
        logger.info(messages)
        try:
            res = self.request_chat_completion(messages)
            return res.choices[0].message.content
        except Exception as e:
            raise ValueError(f"请求openai分析SQL失败: {e}")

    def optimize_sql_by_openai(
        self,
        db_type: str,
        db_name: str,
        sql_text: str,
        table_schemas: str,
    ):
        """结合表结构上下文，对 SQL 给出优化建议，返回 markdown 报告"""
        prompt = (
            f"你是一位资深的 {db_type} DBA 和性能优化专家。"
            "请结合下面提供的表结构信息，对目标 SQL 给出优化建议，"
            "包括但不限于：索引建议（是否缺少索引、是否有更优索引）、"
            "SQL 改写建议、潜在的全表扫描/临时表/文件排序风险、"
            "以及执行计划的解读要点。\n"
            "请用 Markdown 格式输出结构清晰的优化报告，"
            "索引建议请给出对应的 DDL 语句，改写建议请给出修改前后的 SQL 对比。"
            "不要输出与优化无关的内容。\n\n"
            f"数据库：{db_name}\n"
            f"相关表结构：\n{table_schemas}\n\n"
            f"目标 SQL：\n{sql_text}"
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
        prompt = (
            f"你是一位资深的 {db_type} DBA 和 SQL 审核专家。请对下面这条待上线的 SQL 进行风险审核和变更影响预测。\n"
            "审核维度：\n"
            "1. 语法与规范：关键字大小写、表别名、SELECT *、缺显式字段等；\n"
            "2. 性能风险：是否有全表扫描、缺索引、LIKE 前缀通配、隐式类型转换、OR 条件、临时表/文件排序等；\n"
            "3. 数据量与锁：结合提供的表行数，判断 DDL 是否会长时间锁表（大表加索引/改字段）、"
            "DML 是否会扫描过多行；\n"
            "4. 安全风险：是否为危险操作（无 WHERE 的 UPDATE/DELETE、TRUNCATE、DROP）。\n\n"
            "变更影响预测（务必结合提供的表行数）：\n"
            "- ddl_lock_risk：DDL 语句的锁表风险等级。非 DDL 填 none；小表(<1万行)填 low；"
            "中等表(1万-100万)填 medium；大表(>100万)的加索引/改字段/改类型填 high。\n"
            "- affected_rows_estimate：预估影响的行数，用中文描述（如「约132万行」「全表约5000行」），非数据变更填空串。\n"
            "- use_osc：当 ddl_lock_risk 为 high 时填 true（建议走 gh-ost/pt-online-schema-change 在线变更），否则 false。\n\n"
            "评分标准（0-100，越高风险越大）：\n"
            "- 0-39：low（低风险，可放心执行）\n"
            "- 40-70：medium（中风险，需关注，建议在低峰执行或加限流）\n"
            "- 71-100：high（高风险，强烈建议改写、分批或走在线变更）\n\n"
            "请严格按如下 JSON 格式输出（仅输出 JSON，不要任何额外文字、不要 markdown 代码块）：\n"
            "输出要求：使用专业、严谨的技术措辞，不要使用任何 emoji 表情符号，不要使用口语化表达。\n"
            '{"risk_level": "low|medium|high", '
            '"risk_score": 整数, '
            '"summary": "一句话总结（≤40字，中文）", '
            '"suggestion": "详细建议（markdown，包含问题清单和修改前后的 SQL 对比）", '
            '"ddl_lock_risk": "none|low|medium|high", '
            '"affected_rows_estimate": "影响行数预估", '
            '"use_osc": true或false}\n\n'
            f"数据库：{db_name}\n"
            f"相关表行数：\n{table_rows}\n\n"
            f"相关表结构：\n{table_schemas}\n\n"
            f"待审核 SQL：\n{sql_text}"
        )
        messages = [dict(role="user", content=prompt)]
        try:
            res = self.request_chat_completion(messages)
            content = res.choices[0].message.content
            return self._parse_review_json(content)
        except Exception as e:
            logger.warning(f"AI 审核 SQL 失败，降级返回 unknown: {e}")
            return dict(AI_REVIEW_FALLBACK)

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

        prompt = (
            f"你是一位资深的 {db_type} DBA 和性能优化专家。"
            "请基于以下慢查询的统计指标、近期趋势、集合/表结构信息和执行计划，"
            "进行根因诊断并给出优化建议。\n\n"
            "诊断要求：\n"
            "1. root_cause：用一句话（≤40字，中文）概括最可能的根因；\n"
            "2. severity：根据 p95 耗时和扫描/返回比判断严重度——"
            "p95>5000ms 或扫描/返回比>1000 判为 high；p95 1000-5000ms 或比 100-1000 判为 medium；其余 low；\n"
            "3. bottleneck_type：从 full_scan / missing_index / lock_wait / filesort / "
            "tmp_table / type_cast / other 中选择最匹配的瓶颈类型；\n"
            "4. evidence：列出支撑你判断的证据（2-4 条），如扫描/返回比异常、"
            "执行计划中 COLLSCAN/type=ALL、趋势恶化起始日等；\n"
            "5. suggestions：给出优化建议列表，每条含 type（index_ddl / rewrite / config）、"
            "desc（描述）、index_ddl（如适用，给出可执行 DDL）、before（改写前 SQL）、"
            "after（改写后 SQL）；before/after 仅在 type=rewrite 时提供；\n"
            "6. report_markdown：可留空字符串（服务端会基于以上结构化字段自动生成完整报告），"
            "不要额外编写；\n\n"
            f"{mongo_guide}\n"
            "请严格按如下 JSON 格式输出（仅输出 JSON，不要任何额外文字、不要 markdown 代码块）：\n"
            "输出要求：使用专业、严谨的技术措辞，不要使用任何 emoji 表情符号，不要使用口语化表达。\n"
            '{"root_cause": "一句话根因（≤40字，中文）", '
            '"severity": "low|medium|high", '
            '"bottleneck_type": "full_scan|missing_index|lock_wait|filesort|tmp_table|type_cast|other", '
            '"evidence": ["证据1", "证据2"], '
            '"suggestions": [{"type": "index_ddl|rewrite|config", '
            '"desc": "建议描述", "index_ddl": "DDL语句或空串", '
            '"before": "改写前SQL或空串", "after": "改写后SQL或空串"}], '
            '"confidence": 0.0到1.0的数字, '
            '"report_markdown": "可留空字符串"}\n\n'
            f"数据库：{db_name}（{db_type}）\n\n"
            f"【统计指标】\n{stats_text}\n"
            f"【近期趋势】\n{trend_summary}\n\n"
            f"【{schema_label}】\n{table_schemas}\n\n"
            f"【执行计划摘要】\n{explain_text}\n\n"
            f"【{sample_label}】\n{sample_sql}"
        )
        messages = [dict(role="user", content=prompt)]
        logger.info(f"AI 慢查诊断 prompt 长度: {len(prompt)} 字符")
        try:
            # max_tokens 限制输出长度：报告结构固定（report_markdown 由服务端拼装），
            # 无需超长输出，避免模型生成冗长内容导致耗时成倍增加。
            # with_options(max_retries=0)：诊断对失败容忍（降级 DIAGNOSIS_FALLBACK），
            # 重试只会把最长耗时从 60s 翻倍到 120s，逼近 django-q 任务超时（180s）导致
            # 任务被硬杀、状态永久卡 running。故诊断路径强制单次尝试。
            # extra_body thinking=disabled：DeepSeek 推理类模型（如 deepseek-v4-flash）
            # 对复杂诊断 prompt 会陷入长思考，把 max_tokens 预算全耗在 reasoning_tokens 上，
            # 导致 content 为空、finish=length、JSON 解析失败降级 fallback。显式关闭思考
            # 让其直接输出结构化结果（实测耗时 42.8s→9s，JSON 完整）。
            res = self.client.with_options(max_retries=0).chat.completions.create(
                model=self.default_chat_model,
                messages=messages,
                max_tokens=2000,
                extra_body={"thinking": {"type": "disabled"}},
            )
            content = res.choices[0].message.content
            result = self._parse_diagnosis_json(content, db_type=db_type)
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
        """用统计指标对严重度做规则兜底（与 prompt 判定规则一致）。

        模型可能忽略统计规则判低严重度，此处以统计数据为准覆写：
        p95>5000ms 或扫描/返回比>1000 → high；p95 1000-5000ms 或比 100-1000 → medium。
        统计缺失时不覆写，保留模型判定。
        """
        if not stats:
            return
        try:
            p95 = float(stats.get("query_time_p95") or 0)
            examined = float(stats.get("parse_total_row_counts") or 0)
            returned = float(stats.get("return_total_row_counts") or 0)
        except (TypeError, ValueError):
            return
        ratio = examined / returned if returned and returned > 0 else 0
        if p95 > 5000 or ratio > 1000:
            result["severity"] = AI_RISK_HIGH
        elif p95 > 1000 or ratio > 100:
            result["severity"] = AI_RISK_MEDIUM

    @staticmethod
    def _parse_diagnosis_json(content: str, db_type: str = ""):
        """解析 AI 返回的慢查诊断结果。

        复用 _try_load_json 的多层容错（代码块去除、裸换行转义、尾逗号清理、
        全角标点归一、单引号转双引号），额外做诊断字段校验与归一。
        """
        if not content:
            return dict(DIAGNOSIS_FALLBACK)
        text = content.strip()

        data = OpenaiClient._try_load_json(text)
        if data is None:
            # 去掉代码块包裹后重试
            stripped = text
            if stripped.startswith("```"):
                stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
                stripped = re.sub(r"\s*```$", "", stripped)
            data = OpenaiClient._try_load_json(stripped)
        if data is None:
            # 抽取首个 {...} 片段（DOTALL 跨行）
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                data = OpenaiClient._try_load_json(match.group(0))
        if data is None:
            logger.warning(f"AI 诊断结果解析失败，原始内容: {content[:200]}")
            return dict(DIAGNOSIS_FALLBACK)

        # 字段校验与归一
        root_cause = OpenaiClient._strip_emoji(
            str(data.get("root_cause", ""))[:200]
        ) or "AI 诊断完成"

        severity = str(data.get("severity", "")).lower()
        if severity not in (AI_RISK_LOW, AI_RISK_MEDIUM, AI_RISK_HIGH):
            severity = AI_RISK_UNKNOWN

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

        report_md = OpenaiClient._strip_emoji(
            str(data.get("report_markdown") or "")
        )

        result = {
            "root_cause": root_cause,
            "severity": severity,
            "bottleneck_type": bottleneck,
            "evidence": evidence,
            "suggestions": suggestions,
            "confidence": confidence,
            "report_markdown": report_md,
        }
        # 模型未编写 report_markdown 时，由服务端从结构化字段确定性拼装，
        # 省去模型额外生成叙述性 markdown 的 ~300-600 输出 token。
        # 注意：DIAGNOSIS_FALLBACK 的固定文案非空，不会走到拼装分支。
        if not result["report_markdown"]:
            result["report_markdown"] = OpenaiClient._build_diagnosis_markdown(
                result, db_type
            )
        return result

    @staticmethod
    def _build_diagnosis_markdown(result: dict, db_type: str = "") -> str:
        """基于结构化诊断字段确定性组装 markdown 完整报告。

        替代让模型额外写一段叙述性 report_markdown：输出 token 更省、
        解析失败率更低、格式稳定。仅当前端"完整报告"区无模型原文时使用。
        """
        severity_map = {
            AI_RISK_LOW: "低危",
            AI_RISK_MEDIUM: "中危",
            AI_RISK_HIGH: "高危",
            AI_RISK_UNKNOWN: "未知",
        }
        bottleneck_map = {
            DIAG_BOTTLENECK_FULL_SCAN: "全表扫描",
            DIAG_BOTTLENECK_MISSING_INDEX: "缺索引",
            DIAG_BOTTLENECK_LOCK_WAIT: "锁等待",
            DIAG_BOTTLENECK_FILESORT: "文件排序",
            DIAG_BOTTLENECK_TMP_TABLE: "临时表",
            DIAG_BOTTLENECK_TYPE_CAST: "类型转换",
            DIAG_BOTTLENECK_OTHER: "其他",
        }
        suggestion_type_map = {
            "index_ddl": "索引建议",
            "rewrite": "SQL 改写",
            "config": "配置建议",
        }

        lines = ["## 慢查根因诊断", ""]
        root_cause = str(result.get("root_cause", "") or "").strip()
        severity = severity_map.get(result.get("severity", ""), "未知")
        bottleneck = bottleneck_map.get(result.get("bottleneck_type", ""), "其他")

        lines.append(f"- **根因**：{root_cause or '未识别'}")
        lines.append(f"- **严重度**：{severity}")
        lines.append(f"- **瓶颈类型**：{bottleneck}")

        evidence = result.get("evidence") or []
        if evidence:
            lines += ["", "### 证据", ""]
            lines += [f"- {e}" for e in evidence]

        suggestions = result.get("suggestions") or []
        if suggestions:
            lines += ["", "### 优化建议", ""]
            for i, s in enumerate(suggestions, 1):
                stype = suggestion_type_map.get(str(s.get("type", "")), "建议")
                desc = str(s.get("desc", "") or "").strip()
                title = f"**{i}. [{stype}] {desc}**" if desc else f"**{i}. [{stype}]**"
                lines.append(title)
                # MongoDB 的 createIndex/聚合管道是 JS shell 语法，代码块用 js 高亮
                code_lang = "js" if db_type == "mongo" else "sql"
                index_ddl = str(s.get("index_ddl", "") or "").strip()
                if index_ddl:
                    lines += ["", f"```{code_lang}", index_ddl, "```"]
                before = str(s.get("before", "") or "").strip()
                after = str(s.get("after", "") or "").strip()
                if before and after:
                    lines += [
                        "", "**改写前**：", f"```{code_lang}", before, "```",
                        "**改写后**：", f"```{code_lang}", after, "```",
                    ]

        lines += ["", "> 本报告由 AI 辅助生成，建议人工确认后再执行任何变更。"]
        return "\n".join(lines)

    @staticmethod
    def _parse_review_json(content: str):
        """解析 AI 返回的审核结果。

        兼容 LLM 常见的不规范输出：
        1. markdown 代码块包裹（```json ... ```）；
        2. JSON 前后有解释性文字（抽取首个 {...}）；
        3. 字符串值内含裸露换行符（违反 JSON 规范，需转义为 \\n）——这是 LLM
           在 JSON 里写多行 markdown 时的典型行为，最易导致解析失败。
        """
        if not content:
            return dict(AI_REVIEW_FALLBACK)
        text = content.strip()

        data = OpenaiClient._try_load_json(text)
        if data is None:
            # 去掉代码块包裹后重试
            stripped = text
            if stripped.startswith("```"):
                stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
                stripped = re.sub(r"\s*```$", "", stripped)
            data = OpenaiClient._try_load_json(stripped)
        if data is None:
            # 抽取首个 {...} 片段（DOTALL 跨行），再做换行容错
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                data = OpenaiClient._try_load_json(match.group(0))
        if data is None:
            logger.warning(f"AI 审核结果解析失败，原始内容: {content[:200]}")
            return dict(AI_REVIEW_FALLBACK)

        # 字段校验与归一
        level = str(data.get("risk_level", "")).lower()
        if level not in (AI_RISK_LOW, AI_RISK_MEDIUM, AI_RISK_HIGH):
            level = AI_RISK_UNKNOWN
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
