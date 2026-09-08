"""
AI 主动取证优化器（Agent 工具调用循环）

给 LLM 一组只读探查工具，由模型自主决定查什么、查几轮，取证完成后输出
markdown 优化报告。mysql 工具集：表清单 / DDL / 索引 / 行数与大小 /
EXPLAIN / EXPLAIN ANALYZE（8.0+，会实际执行白名单内的 SELECT）；
mongo 工具集：集合列表 / 索引定义 / 字段与类型（不含数据值）/ 统计 / explain。
（未提供 SHOW WARNINGS：它要求与 EXPLAIN 同一会话，引擎侧无会话连续性保证。）

安全与预算约束：
- 仅暴露只读探查工具；mysql EXPLAIN 走 sanitize_explain_sql 闸门（截首句、
  SELECT/WITH 白名单、拒 INTO OUTFILE/DUMPFILE），mongo explain 只做计划
  不执行语句本身
- 表名/集合名白名单校验后才可拼接；实例权限由调用方（视图）先行校验
- mongo 工具只返回元数据与字段名/类型，文档数据值不出内网（H4）
- 时间预算（AGENT_DEADLINE_SECONDS）+ 轮数上限（AGENT_MAX_ROUNDS）双保险，
  超限后强制模型基于已取证的信息收敛输出，避免循环失控
"""
import json
import logging
import re
import time

from sql.utils.sql_utils import (
    extract_mongo_collections,  # noqa: F401  再导出兼容旧引用
    sanitize_explain_sql,
    valid_collection_name,
)

logger = logging.getLogger("default")

# 总时间预算（秒）：需 < 前端该接口的 300s 超时
AGENT_DEADLINE_SECONDS = 240
# 最大工具调用轮数（每轮模型可并行发起多个工具调用）
AGENT_MAX_ROUNDS = 8
# 单个工具结果喂回模型的最大字符数，防 prompt 膨胀
TOOL_RESULT_MAX_CHARS = 6000

_TABLE_NAME_RE = re.compile(r"[\w$.]+")


def _mysql_tools_spec():
    """OpenAI function calling 工具清单（仅 mysql）。"""
    def _table_tool(name, desc):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": {"table": {"type": "string", "description": "表名"}},
                    "required": ["table"],
                },
            },
        }

    return [
        {
            "type": "function",
            "function": {
                "name": "list_tables",
                "description": "列出当前库的全部表名。语句解析不到表名、或需要确认表是否存在时使用。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        _table_tool(
            "get_table_ddl", "获取表的建表语句（全部列定义、主键、二级索引）。"
            "判断某列有没有索引、索引列顺序是否合适时使用。"
        ),
        _table_tool(
            "get_table_indexes", "获取表的索引清单（索引名、列、cardinality、是否唯一）。"
        ),
        _table_tool(
            "get_table_stats", "获取表的行数估计、数据与索引大小（MB）。"
        ),
        {
            "type": "function",
            "function": {
                "name": "run_explain",
                "description": "对 SELECT 查询执行 EXPLAIN 查看优化器预估执行计划"
                               "（不会执行语句本身）。仅支持 SELECT/WITH 开头的语句。",
                "parameters": {
                    "type": "object",
                    "properties": {"sql": {"type": "string", "description": "要分析的 SELECT 查询"}},
                    "required": ["sql"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_explain_analyze",
                "description": "对 SELECT 查询执行 EXPLAIN ANALYZE 获取真实执行行数与各步骤耗时"
                               "（MySQL 8.0+；会实际执行该查询，仅限 SELECT/WITH，大表慎用）。"
                               "预估计划与真实行数差异大、或需要确认走索引后的实际回表量时使用。",
                "parameters": {
                    "type": "object",
                    "properties": {"sql": {"type": "string", "description": "要分析的 SELECT 查询"}},
                    "required": ["sql"],
                },
            },
        },
    ]


def _mongo_tools_spec():
    """OpenAI function calling 工具清单（mongo）。"""
    def _coll_tool(name, desc, has_coll_arg=True):
        props = {}
        if has_coll_arg:
            props["collection"] = {"type": "string", "description": "集合名"}
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": ["collection"] if has_coll_arg else [],
                },
            },
        }

    return [
        _coll_tool(
            "get_collection_indexes", "获取集合的索引定义（索引名、键、是否唯一）。"
            "判断查询字段有没有索引、复合索引顺序是否合适时使用。"
        ),
        _coll_tool(
            "get_collection_fields", "获取集合的字段清单与类型（仅字段名和类型，不含数据值）。"
        ),
        _coll_tool(
            "get_collection_stats", "获取集合的文档数、数据与索引大小。"
        ),
        _coll_tool(
            "run_explain",
            "对 mongo 查询语句附加 explain 查看执行计划（不执行语句本身，只做计划）。"
            "语句为 db.collection.find(...) 等 shell 形式。",
            has_coll_arg=False,
        ),
        _coll_tool("list_collections", "列出当前库的全部集合名。", has_coll_arg=False),
    ]


def _format_rows(column_list, rows, max_rows=30):
    """结果集 → 紧凑文本表格，方便模型阅读。"""
    if not rows:
        return "（无结果）"
    cols = list(column_list or [])
    lines = [" | ".join(str(c) for c in cols)] if cols else []
    for row in rows[:max_rows]:
        lines.append(" | ".join(str(v) for v in row))
    if len(rows) > max_rows:
        lines.append(f"...（共 {len(rows)} 行，已截断）")
    return "\n".join(lines)


def _execute_tool(engine, db_name, name, args, db_type="mysql"):
    """执行单个工具调用，返回 (ok, 文本结果)。任何异常由调用方兜底。"""
    if db_type == "mongo":
        return _execute_mongo_tool(engine, db_name, name, args)
    return _execute_mysql_tool(engine, db_name, name, args)


def _execute_mysql_tool(engine, db_name, name, args):
    if name in ("run_explain", "run_explain_analyze"):
        sql_text = str(args.get("sql", ""))
        clean, reason = sanitize_explain_sql(sql_text)
        if clean is None:
            return False, f"该语句不能 EXPLAIN：{reason}"
        # 参数化模板 SQL 的 '?' 占位符替换为字面量 1（EXPLAIN 只做计划不执行；
        # explain analyze 仅实际执行白名单内的 SELECT/WITH，engine 侧已限流 30s）
        clean = clean.replace("?", "1")
        prefix = "EXPLAIN ANALYZE " if name == "run_explain_analyze" else "EXPLAIN "
        rs = engine.query(
            db_name=db_name, sql=f"{prefix}{clean}", max_execution_time=30000
        )
        return True, _format_rows(rs.column_list, rs.rows)

    if name == "list_tables":
        rs = engine.query(
            db_name=db_name,
            sql=(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() ORDER BY table_name"
            ),
        )
        names = [str(row[0]) for row in (rs.rows or [])]
        return True, "、".join(names) if names else "（空库）"

    if name in ("get_table_ddl", "get_table_indexes", "get_table_stats"):
        tb = str(args.get("table", "")).strip("`").strip()
        if not tb or not _TABLE_NAME_RE.fullmatch(tb):
            return False, f"非法表名：{tb!r}"

        if name == "get_table_ddl":
            rs = engine.query(db_name=db_name, sql=f"SHOW CREATE TABLE `{tb}`")
            if not rs.rows:
                return False, f"表 {tb} 不存在或无权查看"
            # 剥掉 AUTO_INCREMENT 计数器噪音（列定义里的自增标记不受影响）
            return True, re.sub(r"\sAUTO_INCREMENT=\d+", "", str(rs.rows[0][1]))

        if name == "get_table_indexes":
            rs = engine.query(db_name=db_name, sql=f"SHOW INDEX FROM `{tb}`")
            return True, _format_rows(rs.column_list, rs.rows)

        rs = engine.query(
            db_name=db_name,
            sql=(
                "SELECT table_name, table_rows, "
                "ROUND(data_length/1024/1024, 1) AS data_mb, "
                "ROUND(index_length/1024/1024, 1) AS index_mb "
                "FROM information_schema.tables "
                f"WHERE table_schema = DATABASE() AND table_name = '{tb}'"
            ),
        )
        return True, _format_rows(rs.column_list, rs.rows)

    return False, f"未知工具：{name}"


def _execute_mongo_tool(engine, db_name, name, args):
    """mongo 工具：经 pymongo 只读探查（集合元数据，不含文档数据值）。"""
    tb = str(args.get("collection", "")).strip()
    if name in ("get_collection_indexes", "get_collection_fields", "get_collection_stats"):
        if not valid_collection_name(tb):
            return False, f"非法集合名：{tb!r}"

    conn = engine.get_connection()
    db = conn[db_name]

    if name == "list_collections":
        return True, "、".join(db.list_collection_names()) or "（空库）"

    if name == "get_collection_indexes":
        if not db[tb].count_documents({}, limit=1) and tb not in db.list_collection_names():
            return False, f"集合 {tb} 不存在"
        specs = []
        for idx in db[tb].list_indexes():
            key = ", ".join(
                f"{f}:{d}" for f, d in idx.get("key", {}).items()
            )
            unique = "，唯一" if idx.get("unique") else ""
            specs.append(f"{idx.get('name')}（{key}{unique}）")
        return True, "\n".join(specs) or "（无索引，仅有默认 _id）"

    if name == "get_collection_fields":
        # 只采样字段名与 BSON 类型，不含数据值（H4：真实业务数据不出内网）
        fields = {}
        for doc in db[tb].find().sort([("_id", 1)]).limit(2):
            for k, v in doc.items():
                fields.setdefault(k, type(v).__name__)
        for doc in db[tb].find().sort([("_id", -1)]).limit(2):
            for k, v in doc.items():
                fields.setdefault(k, type(v).__name__)
        if not fields:
            return False, f"集合 {tb} 为空或不存在，无法采样字段"
        return True, "、".join(f"{k}({t})" for k, t in fields.items())

    if name == "get_collection_stats":
        stats = db.command({"collStats": tb})
        return True, (
            f"文档数约 {stats.get('count', 0)}，"
            f"数据大小 {round(stats.get('size', 0) / 1024 / 1024, 1)}MB，"
            f"索引大小 {round(stats.get('totalIndexSize', 0) / 1024 / 1024, 1)}MB，"
            f"索引个数 {stats.get('nindexes', '?')}"
        )

    if name == "run_explain":
        statement = str(args.get("sql", "")).strip()
        if not statement:
            return False, "语句为空"
        # 引擎的语句解析支持 explain 前缀（附加 .explain()，只做计划不执行）
        rs = engine.query(db_name=db_name, sql=f"explain {statement}")
        return True, _format_rows(rs.column_list, rs.rows) or "（已生成执行计划，详见返回列）"

    return False, f"未知工具：{name}"


def run_agent_optimize(engine, db_name, masked_sql, table_names, db_type="mysql", client=None):
    """Agent 主循环：模型自主调用只读工具取证，最后输出 markdown 报告。

    :param engine: 已按实例构造的引擎（工具经它只读访问目标库）
    :param db_name: 目标库名
    :param masked_sql: 已做字面量脱敏的待优化查询
    :param table_names: 从语句解析出的表名（供参考，模型可自行增查）
    :param client: 可选的 OpenaiClient（调用方注入以便读取 total_usage 记账）；
        不传则内部创建
    :return: (report_markdown, steps)；steps 为取证轨迹 [{tool,args,ok,elapsed}]
    """
    from common.utils.openai import OpenaiClient

    if client is None:
        client = OpenaiClient(scenario="sql_optimize_agent")
    deadline = time.monotonic() + AGENT_DEADLINE_SECONDS
    steps = []

    is_mongo = db_type == "mongo"
    obj_name = "集合" if is_mongo else "表"
    tools_spec = _mongo_tools_spec() if is_mongo else _mysql_tools_spec()
    sys_prompt = (
        f"你是一位资深的 {db_type} DBA 和性能优化专家，正在诊断一条查询语句的性能问题。"
        f"你可以调用工具主动获取需要的信息（{obj_name}结构/字段、索引、行数、执行计划），"
        "请用尽量少的调用取到足够的信息（涉及索引判断时优先用执行计划验证），"
        "然后输出精炼的中文 markdown 优化报告：只保留最重要的建议（最多 3 条）、"
        "全文 500 字以内，索引建议给出创建语句，改写建议给出修改前后对比。"
    )
    messages = [
        {"role": "system", "content": sys_prompt},
        {
            "role": "user",
            "content": (
                f"数据库：{db_name}\n"
                f"涉及{obj_name}：{', '.join(table_names) if table_names else '（未能自动解析，请先用工具查看）'}\n"
                f"待优化查询：\n{masked_sql}"
            ),
        },
    ]

    report = ""
    rounds = 0
    while rounds < AGENT_MAX_ROUNDS and time.monotonic() < deadline:
        rounds += 1
        resp = client.request_chat_completion(messages, tools=tools_spec)
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            report = msg.content or ""
            break

        assistant_msg = {"role": "assistant", "tool_calls": []}
        if msg.content:
            assistant_msg["content"] = msg.content
        for tc in tool_calls:
            assistant_msg["tool_calls"].append(
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
            )
        messages.append(assistant_msg)

        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            t0 = time.monotonic()
            try:
                ok, text = _execute_tool(
                    engine, db_name, tc.function.name, args, db_type=db_type
                )
            except Exception as e:
                ok, text = False, f"工具执行失败：{e}"
            elapsed = round(time.monotonic() - t0, 1)
            steps.append(
                {"tool": tc.function.name, "args": args, "ok": ok, "elapsed": elapsed}
            )
            logger.info(
                f"AI Agent 工具调用 {tc.function.name}({args}) ok={ok} 耗时 {elapsed}s"
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(text)[:TOOL_RESULT_MAX_CHARS],
                }
            )

    if not report:
        # 时间/轮数预算用尽：不允许再调工具，强制基于已取证信息收敛
        logger.warning("AI Agent 达到轮数/时间预算，强制收敛输出")
        messages.append(
            {
                "role": "user",
                "content": "诊断预算已用完，请立即基于已获取的信息输出最终报告，不要再调用工具。",
            }
        )
        resp = client.request_chat_completion(messages)
        report = resp.choices[0].message.content or ""

    return report, steps
