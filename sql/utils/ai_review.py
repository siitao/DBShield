"""工单 AI 风险审核共享 runner（引擎无关）。

批量评审：一次 AI 调用评审一批语句（批内共享一次 DDL/行数收集，token 与
耗时都省），解析失败/个别缺失自动回退逐条；批级结果缓存 1h；用量按批记账。
mysql / pgsql / mongo 的 execute_check 通过 run_ai_review() 接入，行为口径
（脱敏、上限、降级、缓存、记账）由此单点保证。

上下文收集按 db_type 分支（best-effort，单表失败仅跳过该表）：
- mysql：SHOW CREATE TABLE + information_schema 行数；
- pgsql：information_schema 列信息转 DDL（pg_rows_to_ddl）+ 行数（尽力）；
- mongo：集合索引定义 + collStats（仅元数据，不含文档数据值）。

外发脱敏（H4）：待审核语句与 DDL 上下文均先做字面量脱敏再进 prompt。
"""

import hashlib
import logging

from common.utils.ai_prompts import (
    REVIEW_CONTRACT_VERSION,
    REVIEW_PROMPT_VERSION,
)
from sql.utils.sql_utils import (
    extract_mongo_collections,
    extract_tables,
    mask_sql_literals,
    pg_rows_to_ddl,
)

logger = logging.getLogger("default")

# 单张工单最多参与 AI 审核的语句数（超出标 unknown 跳过）
MAX_STATEMENTS = 20
# 批量评审：一次 AI 调用评审的语句数（检测通常 ≤10 条，一批完成）
BATCH_SIZE = 10
# 单批上下文最多收集的表/集合数（防 prompt 膨胀）
MAX_CONTEXT_OBJECTS = 5
# 审核结果批级缓存 TTL：短于优化建议的 24h，降低结构/行数漂移的陈旧风险
CACHE_TTL = 3600
# 检测结果快照的提交有效期：超过后必须重新检测，避免旧结论被长期复用
AI_REVIEW_TTL_HOURS = 2

_UNKNOWN_SUMMARY = "AI 审核失败"
_SKIP_SUMMARY = "AI 审核跳过（超过审核条数上限）"

# 挂到 ReviewResult 上、需要跨请求复用的 AI 字段（检测 → 提交 → 详情）
AI_REVIEW_FIELDS = (
    "ai_risk_level",
    "ai_risk_score",
    "ai_summary",
    "ai_suggestion",
    "ai_ddl_lock_risk",
    "ai_affected_rows_estimate",
    "ai_use_osc",
)


def _cache_get_quiet(key):
    try:
        from django.core.cache import cache

        return cache.get(key)
    except Exception:
        return None


def _cache_set_quiet(key, value, ttl):
    try:
        from django.core.cache import cache

        cache.set(key, value, ttl)
    except Exception:
        pass


def sql_fingerprint(sql: str) -> str:
    """SQL 文本指纹（统一换行、去除首尾空白后的 SHA-256）。

    用于 AI 审核结果与提交内容绑定：SQL 任意变更都会导致指纹不同；
    仅归一 CRLF/CR，避免同一 SQL 在不同传输层被误判为变更。
    """
    normalized = (sql or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def batch_cache_key(
    model,
    db_name,
    masked_sqls,
    schema_fingerprint="",
    prompt_version="",
    contract_version="",
    db_type="",
):
    """批级缓存 key：模型、库类型/库、SQL、表结构指纹与 prompt/契约版本共同决定。"""
    digest = hashlib.md5(
        "||".join(
            [
                model,
                db_type or "",
                db_name or "",
                prompt_version or "",
                contract_version or "",
                schema_fingerprint or "",
                *masked_sqls,
            ]
        ).encode("utf-8")
    ).hexdigest()
    return f"ai_sql_review_batch:{digest}"


def _extract_obj_names(masked_sqls, db_type):
    """从一批（已脱敏）语句解析涉及的表/集合名，去重保序。"""
    names = []
    if db_type == "mongo":
        for s in masked_sqls:
            for name in extract_mongo_collections(s):
                if name not in names:
                    names.append(name)
    else:
        for s in masked_sqls:
            for tb in extract_tables(s):
                name = str(tb.get("name", "")).strip("`").strip()
                if name and name not in names:
                    names.append(name)
    return names


def _collect_mysql_context(engine, db_name, names):
    escaped = engine.escape_string(db_name)
    schema_parts, rows_parts = [], []
    for name in names[:MAX_CONTEXT_OBJECTS]:
        try:
            rs = engine.describe_table(escaped, name)
            rows = getattr(rs, "rows", None) or []
            if rows:
                ddl = rows[0][1] if len(rows[0]) > 1 else ""
                schema_parts.append(f"-- 表 {name}\n{ddl};")
        except Exception as e:
            logger.debug(f"AI 审核拉取 {name} DDL 失败: {e}")
        try:
            meta = engine.get_table_meta_data(escaped, name)
            col_idx = meta["column_list"].index("table_rows")
            rows_parts.append(f"{name}: 约 {meta['rows'][col_idx]} 行")
        except Exception as e:
            logger.debug(f"AI 审核拉取 {name} 行数失败: {e}")
    return "\n".join(schema_parts), "\n".join(rows_parts)


def _collect_pgsql_context(engine, db_name, names):
    schema_parts, rows_parts = [], []
    for name in names[:MAX_CONTEXT_OBJECTS]:
        try:
            rs = engine.describe_table(db_name, name)
            rows = getattr(rs, "rows", None) or []
            if rows:
                schema_parts.append(f"-- 表 {name}\n{pg_rows_to_ddl(name, rows)}")
        except Exception as e:
            logger.debug(f"AI 审核拉取 {name} DDL 失败: {e}")
        # pgsql 行数尽力而为：元数据接口不可用/字段缺失仅跳过
        try:
            meta = engine.get_table_meta_data(db_name, name)
            if "table_rows" in meta.get("column_list", []):
                col_idx = meta["column_list"].index("table_rows")
                rows_parts.append(f"{name}: 约 {meta['rows'][col_idx]} 行")
        except Exception as e:
            logger.debug(f"AI 审核拉取 {name} 行数失败: {e}")
    return "\n".join(schema_parts), "\n".join(rows_parts)


def _collect_mongo_context(engine, db_name, names):
    """mongo 上下文：索引定义 + collStats（仅元数据，不含文档数据值）。"""
    conn = engine.get_connection()
    db = conn[db_name]
    schema_parts, rows_parts = [], []
    for name in names[:MAX_CONTEXT_OBJECTS]:
        try:
            specs = []
            for idx in db[name].list_indexes():
                key = ", ".join(f"{f}:{d}" for f, d in idx.get("key", {}).items())
                unique = "，唯一" if idx.get("unique") else ""
                specs.append(f"{idx.get('name')}（{key}{unique}）")
            schema_parts.append(
                f"-- 集合 {name} 索引\n"
                + ("\n".join(specs) or "（无索引，仅默认 _id）")
            )
        except Exception as e:
            logger.debug(f"AI 审核拉取集合 {name} 索引失败: {e}")
        try:
            stats = db.command({"collStats": name})
            rows_parts.append(
                f"{name}: 约 {stats.get('count', 0)} 文档，"
                f"数据 {round(stats.get('size', 0) / 1024 / 1024, 1)}MB"
            )
        except Exception as e:
            logger.debug(f"AI 审核拉取集合 {name} 统计失败: {e}")
    return "\n".join(schema_parts), "\n".join(rows_parts)


def _collect_context(engine, db_type, db_name, names):
    if not names:
        return "(无法解析出表名/集合名，仅依据 SQL 文本给出建议)", "（未解析到表名）"
    if db_type == "mysql":
        return _collect_mysql_context(engine, db_name, names)
    if db_type == "pgsql":
        return _collect_pgsql_context(engine, db_name, names)
    if db_type == "mongo":
        return _collect_mongo_context(engine, db_name, names)
    return (
        "(该数据库类型暂不支持结构上下文收集，仅依据 SQL 文本给出建议)",
        "（无行数信息）",
    )


def _prepare_context(engine, db_type, db_name, names):
    """收集上下文并生成指纹/截断元数据。

    返回 (schema_ctx, rows_ctx, meta)。meta 中的 schema_fingerprint 参与
    缓存 key，context_truncated 暴露给 prompt 与工单审核记录。
    """
    included = names[:MAX_CONTEXT_OBJECTS]
    schema_ctx, rows_ctx = _collect_context(engine, db_type, db_name, names)
    schema_fingerprint = hashlib.sha256(
        f"{schema_ctx}\n--rows--\n{rows_ctx}".encode("utf-8")
    ).hexdigest()
    meta = {
        "context_truncated": len(names) > MAX_CONTEXT_OBJECTS,
        "total_objects": len(names),
        "included_objects": list(included),
        "schema_fingerprint": schema_fingerprint,
    }
    return schema_ctx, rows_ctx, meta


def _attach(row, result):
    """把归一后的审核结果挂到 ReviewResult 行的自定义属性上。"""
    row.ai_risk_level = result["risk_level"]
    row.ai_risk_score = result["risk_score"]
    row.ai_summary = result["summary"]
    row.ai_suggestion = result["suggestion"]
    row.ai_ddl_lock_risk = result["ddl_lock_risk"]
    row.ai_affected_rows_estimate = result["affected_rows_estimate"]
    row.ai_use_osc = result["use_osc"]


def _attach_unknown(row, summary=_UNKNOWN_SUMMARY):
    """挂载 unknown 占位字段（失败/跳过/缺失统一形态，保证前端字段齐全）。"""
    from common.utils.ai_gateway import AI_LOCK_NONE, AI_RISK_UNKNOWN

    row.ai_risk_level = AI_RISK_UNKNOWN
    row.ai_risk_score = 0
    row.ai_summary = summary
    row.ai_suggestion = ""
    row.ai_ddl_lock_risk = AI_LOCK_NONE
    row.ai_affected_rows_estimate = ""
    row.ai_use_osc = False


def _review_one(engine, client, db_type, db_name, masked_sql):
    """单条回退审核（批量不可用/个别缺失时）。失败返回 {"error": ...}。"""
    names = _extract_obj_names([masked_sql], db_type)
    schema_ctx, rows_ctx, context_meta = _prepare_context(
        engine, db_type, db_name, names
    )
    return client.review_sql_by_openai(
        db_type=db_type,
        db_name=db_name,
        sql_text=masked_sql,
        table_schemas=mask_sql_literals(schema_ctx, db_type),
        table_rows=rows_ctx,
        context_truncated=context_meta["context_truncated"],
    )


def review_statements(
    engine, client, db_type, db_name, statements, instance_name="", user_name=""
):
    """批量评审一批语句，返回 (与 statements 等长的结果 list, 上下文元数据)。

    每项为归一结果 dict；该项 AI 完全不可用时为 {"error": ...} 占位
    （调用方按 "risk_level" 不存在判 unknown）。批级异常会抛出。
    上下文元数据包含 schema 指纹、是否截断、模型/prompt/契约版本。
    """
    from common.utils.ai_gateway import record_ai_usage

    masked = [mask_sql_literals(s or "", db_type) for s in statements]
    model = str(client.default_chat_model)

    # 先收集上下文：schema 指纹参与缓存 key，表结构变化后旧缓存自然失效
    names = _extract_obj_names(masked, db_type)
    schema_ctx, rows_ctx, context_meta = _prepare_context(
        engine, db_type, db_name, names
    )
    cache_key = batch_cache_key(
        model,
        db_name or "",
        masked,
        schema_fingerprint=context_meta["schema_fingerprint"],
        prompt_version=REVIEW_PROMPT_VERSION,
        contract_version=REVIEW_CONTRACT_VERSION,
        db_type=db_type,
    )
    meta = {
        **context_meta,
        "model": model,
        "prompt_version": REVIEW_PROMPT_VERSION,
        "contract_version": REVIEW_CONTRACT_VERSION,
    }

    results = None
    cache_hit = False
    cached = _cache_get_quiet(cache_key)
    if isinstance(cached, list) and len(cached) == len(masked):
        results = cached
        cache_hit = True

    try:
        if results is None:
            results = client.review_sql_batch_by_openai(
                db_type=db_type,
                db_name=db_name,
                statements=masked,
                table_schemas=mask_sql_literals(schema_ctx, db_type),
                table_rows=rows_ctx,
                context_truncated=context_meta["context_truncated"],
            )
        if results is None:
            # 整批解析失败 → 全部逐条回退
            results = [None] * len(masked)
        elif len(results) != len(masked):
            # 条数不匹配（模型漏评/多评）：缺失位补 None 逐条回退
            logger.warning(
                f"批量审核返回条数不匹配（期望 {len(masked)} 实得 {len(results)}），缺失项回退逐条"
            )
            results = list(results) + [None] * (len(masked) - len(results))
        # 批级记账紧跟批级调用（时序在回退之前）：缓存命中记 0 token，
        # 真实调用记 client 捕获的 tokens；回退单条的成败在循环内各自记账
        if cache_hit:
            record_ai_usage(
                capability="sql_review",
                model=model,
                instance_name=instance_name,
                db_name=db_name or "",
                db_type=db_type,
                user_name=user_name,
                latency_ms=0,
                cache_hit=True,
            )
        else:
            record_ai_usage(
                capability="sql_review",
                client=client,
                instance_name=instance_name,
                db_name=db_name or "",
                db_type=db_type,
                user_name=user_name,
            )
        for i, item in enumerate(results):
            if item is not None:
                continue
            try:
                results[i] = _review_one(engine, client, db_type, db_name, masked[i])
            except Exception as e:
                logger.warning(f"AI 审核单条回退失败，降级 unknown: {e}")
                record_ai_usage(
                    capability="sql_review",
                    client=client,
                    instance_name=instance_name,
                    db_name=db_name or "",
                    db_type=db_type,
                    user_name=user_name,
                    status="failed",
                    error=str(e)[:500],
                )
                results[i] = {"error": str(e)[:500]}
        # 仅当整批结果有效时回填缓存（含 error 占位的批次不缓存，避免坏结果常驻）
        if not cache_hit and all(
            isinstance(r, dict) and "risk_level" in r for r in results
        ):
            _cache_set_quiet(cache_key, results, CACHE_TTL)
        return results, meta
    except Exception as e:
        # 批级异常（如上下文收集失败）：整批判 failed，由调用方逐行挂 unknown
        logger.warning(f"AI 批量审核失败，整批降级 unknown: {e}")
        record_ai_usage(
            capability="sql_review",
            client=client,
            instance_name=instance_name,
            db_name=db_name or "",
            db_type=db_type,
            user_name=user_name,
            status="failed",
            error=str(e)[:500],
        )
        raise


def run_ai_review(engine, check_result, db_type, db_name, user_name=""):
    """工单 AI 风险审核入口（各引擎 execute_check 调用）。

    开关（ai_review_enabled）/ OpenAI 配置不满足时静默跳过；任何异常
    均降级为 unknown 占位，绝不影响检测主流程（errlevel 等不变）。

    审核完成后把模型/prompt/契约版本、schema 指纹、上下文截断标记写入
    check_result.ai_review_meta，供检测接口落 AIReviewRecord。
    """
    # 仅 mysql/clickhouse/tdengine 在 __init__ 里设了 self.config，
    # mongo/pgsql 等引擎没有该属性——缺失时视为未开启，而非 AttributeError
    engine_config = getattr(engine, "config", None) or {}
    if not engine_config.get("ai_review_enabled", False):
        return
    from common.utils.ai_gateway import OpenaiClient, check_openai_config

    if not check_openai_config():
        return
    try:
        client = OpenaiClient(scenario="sql_review")
    except Exception as e:
        logger.warning(f"AI 审核客户端初始化失败，整体跳过: {e}")
        return

    instance_name = (
        getattr(getattr(engine, "instance", None), "instance_name", "") or ""
    )
    # 上限裁剪：超出 MAX_STATEMENTS 的行标 unknown 跳过
    eligible = []
    for idx, row in enumerate(check_result.rows):
        if isinstance(row, dict):
            continue
        if idx >= MAX_STATEMENTS:
            _attach_unknown(row, _SKIP_SUMMARY)
            continue
        eligible.append(row)

    if not eligible:
        return None

    aggregate_meta = {
        "model": str(client.default_chat_model),
        "prompt_version": REVIEW_PROMPT_VERSION,
        "contract_version": REVIEW_CONTRACT_VERSION,
        "context_truncated": False,
        "total_objects": 0,
        "included_objects": [],
        "schema_fingerprint": "",
        "statement_count": len(eligible),
        "partial": False,
    }
    fingerprints = []

    for i in range(0, len(eligible), BATCH_SIZE):
        batch_rows = eligible[i : i + BATCH_SIZE]
        statements = [row.sql or "" for row in batch_rows]
        try:
            results, batch_meta = review_statements(
                engine,
                client,
                db_type,
                db_name,
                statements,
                instance_name=instance_name,
                user_name=user_name,
            )
            aggregate_meta["context_truncated"] = bool(
                aggregate_meta["context_truncated"]
                or batch_meta.get("context_truncated")
            )
            aggregate_meta["total_objects"] = max(
                aggregate_meta["total_objects"],
                int(batch_meta.get("total_objects") or 0),
            )
            for obj in batch_meta.get("included_objects") or []:
                if obj not in aggregate_meta["included_objects"]:
                    aggregate_meta["included_objects"].append(obj)
            if batch_meta.get("schema_fingerprint"):
                fingerprints.append(batch_meta["schema_fingerprint"])
        except Exception:
            # 批级异常（上下文收集失败等）：整批挂 unknown，不中断检测
            aggregate_meta["partial"] = True
            for row in batch_rows:
                _attach_unknown(row)
            continue
        for row, result in zip(batch_rows, results):
            if isinstance(result, dict) and "risk_level" in result:
                _attach(row, result)
            else:
                _attach_unknown(row)

    if fingerprints:
        aggregate_meta["schema_fingerprint"] = hashlib.sha256(
            "|".join(fingerprints).encode("utf-8")
        ).hexdigest()
    check_result.ai_review_meta = aggregate_meta
    return aggregate_meta
