"""
SQL 分析 / SQL 优化 DRF APIView 集

路由：
  POST /api/v1/sql_analyze/generate/      — 解析上传文件为 SQL 列表
  POST /api/v1/sql_analyze/analyze/       — SOAR 分析 SQL
  POST /api/v1/optimize/sqladvisor/       — SQLAdvisor 建议
  POST /api/v1/optimize/soar/             — SOAR 建议（markdown）
  POST /api/v1/optimize/sqltuning/        — MySQL 调优
  POST /api/v1/optimize/explain/          — 执行计划
  POST /api/v1/optimize/ai/               — AI 优化建议

注意：慢查询相关 API 已移至 api_slowquery_v2.py
"""
import hashlib
import logging
import re
import time
import threading
import datetime as _dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sqlparse
from common.config import SysConfig
from common.utils.extend_json_encoder import encode_json as _encode
from common.utils.openai import OpenaiClient, check_openai_config, record_ai_usage
from django.core.cache import cache
from django.http import JsonResponse
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.views import APIView
from sql.engines import get_engine
from sql.models import Instance
from sql.plugins.soar import Soar
from sql.plugins.sqladvisor import SQLAdvisor
from sql.services.instance_service import resolve_instance
from sql.sql_tuning import SqlTuning
from sql.utils.resource_group import user_instances
from sql.utils.sql_utils import (
    extract_tables,
    generate_sql,
    mask_sql_literals,
    sanitize_explain_sql,
)
from sql_api.ai_optimizer import extract_mongo_collections, run_agent_optimize

logger = logging.getLogger("default")

# AI 报告缓存：同一（脱敏后）SQL + 表结构 24h 内直接复用；Redis 不可用时静默降级为直连 AI
AI_REPORT_CACHE_TTL = 24 * 3600
# 单条 SQL 最多带入的表 DDL 数量，防 prompt 膨胀拖慢推理（与引擎侧 AI_REVIEW_MAX_TABLES 对齐）
AI_OPTIMIZE_MAX_TABLES = 5


def _ai_report_cache_key(*parts) -> str:
    digest = hashlib.md5("||".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return f"ai_sql_report:{digest}"


def _cache_get_quiet(key: str):
    try:
        return cache.get(key)
    except Exception:
        return None


def _cache_set_quiet(key: str, value, ttl: int) -> None:
    try:
        cache.set(key, value, ttl)
    except Exception:
        pass


def _split_cached_report(cached):
    """兼容两种缓存形态：Agent 模式存 {report,steps}，单轮模式存纯字符串。"""
    if isinstance(cached, dict) and "report" in cached:
        return cached.get("report") or "", cached.get("steps") or []
    return cached or "", []


# ---------- permissions ----------


class SqlAnalyzePermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.sql_analyze"))


class SqlOptimizePermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.menu_sqladvisor"))


# ---------- helpers ----------


def _get_and_check_instance(user, instance_name, db_type=None):
    """获取实例并做权限校验。"""
    if not instance_name:
        raise Instance.DoesNotExist
    i = Instance.objects.get(instance_name=instance_name)
    if db_type:
        user_instances(user, db_type=[db_type]).get(instance_name=instance_name)
    else:
        user_instances(user, db_type=[i.db_type]).get(instance_name=instance_name)
    return i


# ========== SQL 分析 ==========


class SqlAnalyzeGenerateView(APIView):
    """解析 SQL 文本为列表"""

    permission_classes = [IsAuthenticated, SqlAnalyzePermission]

    def post(self, request):
        text = request.data.get("text")
        if text is None:
            result = {"total": 0, "rows": []}
        else:
            rows = generate_sql(text)
            result = {"total": len(rows), "rows": rows}
        return JsonResponse(_encode(result), safe=False)


class SqlAnalyzeAnalyzeView(APIView):
    """SOAR 分析 SQL"""

    permission_classes = [IsAuthenticated, SqlAnalyzePermission]

    def post(self, request):
        text = request.data.get("text")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")
        if not text:
            result = {"total": 0, "rows": []}
            return JsonResponse(_encode(result), safe=False)

        soar = Soar()
        online_dsn = ""
        soar_test_dsn = ""
        if instance_name and db_name:
            try:
                instance = _get_and_check_instance(request.user, instance_name, db_type="mysql")
            except Instance.DoesNotExist:
                return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！", "data": []})
            soar_test_dsn = SysConfig().get("soar_test_dsn") or ""
            user, password = instance.get_username_password()
            online_dsn = f"{user}:{password}@{instance.host}:{instance.port}/{db_name}"

        args = {
            "report-type": "markdown",
            "query": "",
            "online-dsn": online_dsn,
            "test-dsn": soar_test_dsn,
            "allow-online-as-test": False,
        }
        rows = generate_sql(text)
        for row in rows:
            try:
                p = Path(row["sql"].strip())
                if p.exists():
                    return JsonResponse({"status": 1, "msg": "SQL 语句不合法", "data": []})
            except OSError:
                pass
            args["query"] = row["sql"]
            cmd_args = soar.generate_args2cmd(args=args)
            stdout, stderr = soar.execute_cmd(cmd_args).communicate()
            row["report"] = stdout if stdout else stderr

        result = {"total": len(rows), "rows": rows}
        return JsonResponse(_encode(result), safe=False)


# ========== SQL 优化 ==========


class OptimizeSqlAdvisorView(APIView):
    """SQLAdvisor 索引优化建议"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def post(self, request):
        sql_content = request.data.get("sql_content")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")
        verbose = request.data.get("verbose", 1)

        # 参数验证
        if not sql_content or not instance_name:
            return JsonResponse({"status": 1, "msg": "页面提交参数可能为空"})

        # 实例权限校验（限 mysql）
        try:
            instance = _get_and_check_instance(request.user, instance_name, db_type="mysql")
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！"})

        # 检查 sqladvisor 程序路径
        sqladvisor_path = SysConfig().get("sqladvisor") or "sqladvisor"
        sqladvisor = SQLAdvisor(sqladvisor_path)

        # 获取连接信息
        user, password = instance.get_username_password()
        online_dsn = f"{user}:{password}@{instance.host}:{instance.port}/{db_name}"

        # 执行 SQLAdvisor
        args = {
            "query": sql_content,
            "online-dsn": online_dsn,
            "verbose": verbose,
        }
        cmd_args = sqladvisor.generate_args2cmd(args=args)
        stdout, stderr = sqladvisor.execute_cmd(cmd_args).communicate()

        result = stdout if stdout else stderr
        return JsonResponse({"status": 0, "msg": "success", "data": result})


class OptimizeSoarView(APIView):
    """SOAR 建议（markdown）"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def post(self, request):
        sql_content = request.data.get("sql")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")

        if not sql_content:
            return JsonResponse({"status": 1, "msg": "SQL 语句不能为空"})

        soar = Soar()
        online_dsn = ""
        soar_test_dsn = ""

        if instance_name and db_name:
            try:
                instance = _get_and_check_instance(request.user, instance_name, db_type="mysql")
            except Instance.DoesNotExist:
                return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！"})
            soar_test_dsn = SysConfig().get("soar_test_dsn") or ""
            user, password = instance.get_username_password()
            online_dsn = f"{user}:{password}@{instance.host}:{instance.port}/{db_name}"

        args = {
            "report-type": "markdown",
            "query": sql_content,
            "online-dsn": online_dsn,
            "test-dsn": soar_test_dsn,
            "allow-online-as-test": False,
        }
        cmd_args = soar.generate_args2cmd(args=args)
        stdout, stderr = soar.execute_cmd(cmd_args).communicate()

        result = stdout if stdout else stderr
        return JsonResponse({"status": 0, "msg": "success", "data": result})


class OptimizeSqlTuningView(APIView):
    """MySQL 调优"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def post(self, request):
        sql_content = request.data.get("sql_content")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")
        option = request.data.get("option", [])

        # 参数验证
        if not sql_content or not instance_name or not db_name:
            return JsonResponse({"status": 1, "msg": "页面提交参数可能为空"})

        # 实例权限校验（限 mysql）
        try:
            instance = _get_and_check_instance(request.user, instance_name, db_type="mysql")
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！"})

        # 执行调优
        try:
            tuning = SqlTuning(instance, db_name)
            result = tuning.tuning(sql_content, option)
            return JsonResponse({"status": 0, "msg": "success", "data": result})
        except Exception as e:
            logger.error(f"SQL 调优失败: {e}", exc_info=True)
            return JsonResponse({"status": 1, "msg": "SQL 调优失败，请查看服务端日志"})


class ExplainSqlView(APIView):
    """执行计划"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def post(self, request):
        sql_content = request.data.get("sql_content")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")

        # 参数验证
        if not sql_content or not instance_name or not db_name:
            return JsonResponse({"status": 1, "msg": "页面提交参数可能为空"})

        # 实例权限校验
        try:
            instance = _get_and_check_instance(request.user, instance_name, db_type="mysql")
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！"})

        # 执行 EXPLAIN（H4 回灌：与 v2 共用 sanitize_explain_sql 闸门——
        # 截首句、剥注释、SELECT/WITH 白名单、拒 INTO OUTFILE/DUMPFILE，
        # 防 EXPLAIN ANALYZE 真实执行写文件等注入面）
        clean_sql, reject_reason = sanitize_explain_sql(sql_content)
        if clean_sql is None:
            return JsonResponse({"status": 1, "msg": reject_reason})
        # 参数化模板 SQL 的 '?' 占位符替换为字面量 1（EXPLAIN 只做计划不执行）
        clean_sql = clean_sql.replace("?", "1")
        try:
            engine = get_engine(instance=instance)
            result = engine.query(
                db_name=db_name,
                sql=f"EXPLAIN {clean_sql}",
                max_execution_time=30000,
            )
            column_list = result.column_list
            rows = result.rows
            return JsonResponse({
                "status": 0,
                "msg": "success",
                "data": {"column_list": column_list, "rows": rows}
            })
        except Exception as e:
            logger.error(f"获取执行计划失败: {e}", exc_info=True)
            return JsonResponse({"status": 1, "msg": "获取执行计划失败，请查看服务端日志"})


class OptimizeAIView(APIView):
    """AI 优化建议"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def post(self, request):
        sql_content = request.data.get("sql_content")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")

        if not sql_content:
            return JsonResponse({"status": 1, "msg": "SQL 语句不能为空"})

        if not check_openai_config():
            return JsonResponse({"status": 1, "msg": "未配置 OpenAI API"})

        # 按实例真实 db_type 区分口径：mysql/mongo 走 Agent 主动取证（DDL/索引/行数/
        # 执行计划由模型自主决定查什么），其他类型暂走单轮建议
        instance = None
        db_type = "mysql"
        if instance_name and db_name:
            try:
                instance = _get_and_check_instance(request.user, instance_name)
            except Instance.DoesNotExist:
                return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！"})
            db_type = instance.db_type

        # 缓存键取脱敏后的输入 + 模型名：报告由（脱敏 SQL × 模型）共同决定，
        # 切换 default_chat_model 后旧模型报告不应继续命中（与诊断缓存口径一致）
        masked_sql = mask_sql_literals(sql_content)
        model_name = SysConfig().get("default_chat_model", "gpt-3.5-turbo")
        cache_key = _ai_report_cache_key(
            "optimize", db_type, db_name or "", masked_sql, model_name
        )
        cached = _cache_get_quiet(cache_key)
        if cached is not None:
            report, steps = _split_cached_report(cached)
            logger.info("AI 优化建议命中缓存")
            record_ai_usage(
                capability="sql_optimize",
                model=model_name,
                db_type=db_type,
                instance_name=instance.instance_name if instance else "",
                db_name=db_name or "",
                user_name=request.user.username,
                cache_hit=True,
            )
            return JsonResponse({"status": 0, "msg": "success", "data": report, "steps": steps})

        started = time.monotonic()
        if instance and db_type in ("mysql", "mongo"):
            # Agent 模式：模型自主调用只读探查工具取证后出报告
            if db_type == "mongo":
                table_names = extract_mongo_collections(masked_sql)
            else:
                table_names = []
                for table in extract_tables(sql_content):
                    tb_name = str(table.get("name", "")).strip("`").strip()
                    if tb_name and tb_name not in table_names and re.fullmatch(r"[\w$.]+", tb_name):
                        table_names.append(tb_name)
            # client 由视图创建后传入：Agent 多轮的累计 token 用量记在 total_usage 上
            agent_client = OpenaiClient(scenario="sql_optimize_agent")
            try:
                engine = get_engine(instance=instance)
                report, steps = run_agent_optimize(
                    engine,
                    db_name or "",
                    masked_sql,
                    table_names[:AI_OPTIMIZE_MAX_TABLES],
                    db_type=db_type,
                    client=agent_client,
                )
                elapsed = time.monotonic() - started
                logger.info(f"AI 优化建议(Agent) 耗时 {elapsed:.1f}s，取证 {len(steps)} 次")
                record_ai_usage(
                    capability="sql_optimize",
                    client=agent_client,
                    usage=agent_client.total_usage,
                    latency_ms=agent_client.total_latency_ms,
                    db_type=db_type,
                    instance_name=instance.instance_name,
                    db_name=db_name or "",
                    user_name=request.user.username,
                )
                _cache_set_quiet(
                    cache_key, {"report": report, "steps": steps}, AI_REPORT_CACHE_TTL
                )
                return JsonResponse(
                    {"status": 0, "msg": "success", "data": report, "steps": steps}
                )
            except Exception as e:
                logger.error(f"AI 优化建议失败: {e}", exc_info=True)
                record_ai_usage(
                    capability="sql_optimize",
                    client=agent_client,
                    usage=agent_client.total_usage,
                    db_type=db_type,
                    instance_name=instance.instance_name,
                    db_name=db_name or "",
                    user_name=request.user.username,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    status="failed",
                    error=str(e)[:500],
                )
                return JsonResponse({"status": 1, "msg": "AI 优化建议失败，请查看服务端日志"})

        # 非 mysql：单轮模式（无 DDL 上下文，人设按实际类型）
        try:
            # 场景化配置：90s/次（含 1 次重试最坏 ~180s），
            # 需 < 前端该接口的 300s 超时，避免响应送到时连接已被掐断
            client = OpenaiClient(scenario="sql_optimize")
            # H4 回灌：SQL 与 DDL（COMMENT/DEFAULT 常含真实业务数据）外发前统一字面量脱敏
            result = client.optimize_sql_by_openai(
                db_type=db_type,
                db_name=db_name or "",
                sql_text=masked_sql,
                table_schemas="",
            )
            elapsed = time.monotonic() - started
            logger.info(f"AI 优化建议耗时 {elapsed:.1f}s")
            record_ai_usage(
                capability="sql_optimize",
                client=client,
                db_type=db_type,
                instance_name=instance.instance_name if instance else "",
                db_name=db_name or "",
                user_name=request.user.username,
            )
            _cache_set_quiet(cache_key, result, AI_REPORT_CACHE_TTL)
            return JsonResponse({"status": 0, "msg": "success", "data": result})
        except Exception as e:
            logger.error(f"AI 优化建议失败: {e}", exc_info=True)
            return JsonResponse({"status": 1, "msg": "AI 优化建议失败，请查看服务端日志"})


# ---------- AI 优化异步任务（P2：Agent 取证后台执行，前端轮询取报告） ----------
#
# 同步 OptimizeAIView 的 Agent 模式会把 web worker 挂住最长 240s，且依赖前端
# 300s 超时硬扛。异步模式复用慢查诊断的线程池骨架：提交即返回 task_id，
# 前端轮询状态接口取报告。同步接口保留兼容（非 mysql 单轮模式仍同步）。

_OPTIMIZE_MAX_WORKERS = 2    # 同时最多 2 个 Agent 取证执行
_OPTIMIZE_MAX_PENDING = 4    # 允许排队数量（超过直接拒绝）
_OPTIMIZE_EXECUTOR = None
_OPTIMIZE_SLOTS = None
_OPTIMIZE_EXECUTOR_LOCK = threading.Lock()
# 任务 stale 判定（分钟）：Agent 预算 240s + 采集余量，10 分钟足够兜底
_OPTIMIZE_STALE_MINUTES = 10


def _get_optimize_executor():
    """懒加载优化线程池（模块级单例），并发上限 = workers + pending。"""
    global _OPTIMIZE_EXECUTOR, _OPTIMIZE_SLOTS
    if _OPTIMIZE_EXECUTOR is None:
        with _OPTIMIZE_EXECUTOR_LOCK:
            if _OPTIMIZE_EXECUTOR is None:
                _OPTIMIZE_SLOTS = threading.BoundedSemaphore(
                    _OPTIMIZE_MAX_WORKERS + _OPTIMIZE_MAX_PENDING
                )
                _OPTIMIZE_EXECUTOR = ThreadPoolExecutor(
                    max_workers=_OPTIMIZE_MAX_WORKERS, thread_name_prefix="ai-optimize"
                )
    return _OPTIMIZE_EXECUTOR, _OPTIMIZE_SLOTS


def _mark_stale_optimize_task_failed(task):
    """将超时停留在 pending/running 的优化任务标记 failed。

    防止进程重启遗留的僵尸任务永久阻塞同指纹去重、前端无限轮询。
    返回是否执行了标记。
    """
    if task.status in ("pending", "running") and task.created_at:
        if _dt.datetime.now() - task.created_at > _dt.timedelta(
            minutes=_OPTIMIZE_STALE_MINUTES
        ):
            task.status = "failed"
            task.error = f"任务超时（{_OPTIMIZE_STALE_MINUTES} 分钟内未完成），已自动判失败"
            task.finished_at = _dt.datetime.now()
            task.save(update_fields=["status", "error", "finished_at"])
            logger.warning(f"AI 优化任务 {task.id} stale，已标记 failed")
            return True
    return False


def run_optimize_task(task_id):
    """执行 AI 优化任务（web 进程线程池中运行）。

    状态机: pending → running → success | failed；成功后回填 24h 报告缓存。
    """
    from sql.models import AIOptimizeTask

    try:
        task = AIOptimizeTask.objects.get(id=task_id)
    except AIOptimizeTask.DoesNotExist:
        logger.error(f"AI 优化任务 {task_id} 不存在")
        return
    task.status = "running"
    task.save(update_fields=["status"])
    try:
        instance = task.instance
        engine = get_engine(instance=instance)
        client = OpenaiClient(scenario="sql_optimize_agent")
        report, steps = run_agent_optimize(
            engine,
            task.db_name,
            task.sql_text,
            task.table_names or [],
            db_type=task.db_type,
            client=client,
        )
        task.report_markdown = report
        task.steps = steps
        task.prompt_tokens = client.total_usage["prompt_tokens"]
        task.completion_tokens = client.total_usage["completion_tokens"]
        task.model = client.default_chat_model
        task.status = "success"
        task.finished_at = _dt.datetime.now()
        task.save(update_fields=[
            "report_markdown", "steps", "prompt_tokens",
            "completion_tokens", "model", "status", "finished_at",
        ])
        # 回填 24h 报告缓存（与同步视图同键口径：脱敏SQL + 模型）
        _cache_set_quiet(
            _ai_report_cache_key(
                "optimize", task.db_type, task.db_name, task.sql_text, task.model
            ),
            {"report": report, "steps": steps},
            AI_REPORT_CACHE_TTL,
        )
        record_ai_usage(
            capability="sql_optimize",
            client=client,
            usage=client.total_usage,
            latency_ms=client.total_latency_ms,
            db_type=task.db_type,
            instance_name=instance.instance_name,
            db_name=task.db_name,
            user_name=task.user.username,
        )
        logger.info(f"AI 优化任务 {task_id} 完成，取证 {len(steps)} 次")
    except Exception as e:
        logger.error(f"AI 优化任务 {task_id} 失败: {e}", exc_info=True)
        task.status = "failed"
        task.error = str(e)[:500]
        task.finished_at = _dt.datetime.now()
        task.save(update_fields=["status", "error", "finished_at"])


class OptimizeAIAsyncSubmitView(APIView):
    """异步提交 AI 优化任务（Agent 模式，mysql/mongo）"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def post(self, request):
        sql_content = request.data.get("sql_content")
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")

        if not sql_content:
            return JsonResponse({"status": 1, "msg": "SQL 语句不能为空"})
        if not instance_name or not db_name:
            return JsonResponse({"status": 1, "msg": "异步模式需要选择实例和库"})
        if not check_openai_config():
            return JsonResponse({"status": 1, "msg": "未配置 OpenAI API"})

        try:
            instance = _get_and_check_instance(request.user, instance_name)
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！"})
        db_type = instance.db_type
        if db_type not in ("mysql", "mongo"):
            return JsonResponse({
                "status": 1, "msg": f"{db_type} 暂不支持 AI Agent 优化，请使用同步接口",
            })

        masked_sql = mask_sql_literals(sql_content)
        model_name = SysConfig().get("default_chat_model", "gpt-3.5-turbo")

        # 24h 报告缓存命中直接返回，不建任务
        cached = _cache_get_quiet(
            _ai_report_cache_key("optimize", db_type, db_name or "", masked_sql, model_name)
        )
        if cached is not None:
            report, steps = _split_cached_report(cached)
            record_ai_usage(
                capability="sql_optimize",
                model=model_name,
                db_type=db_type,
                instance_name=instance.instance_name,
                db_name=db_name or "",
                user_name=request.user.username,
                cache_hit=True,
            )
            return JsonResponse({
                "status": 0, "msg": "success",
                "data": {"hit_cache": True, "report": report, "steps": steps},
            })

        # 表名/集合名解析在提交时做一次（脱敏不改结构，解析结果一致），任务线程直接使用
        if db_type == "mongo":
            table_names = extract_mongo_collections(masked_sql)
        else:
            table_names = []
            for table in extract_tables(sql_content):
                tb_name = str(table.get("name", "")).strip("`").strip()
                if tb_name and tb_name not in table_names and re.fullmatch(r"[\w$.]+", tb_name):
                    table_names.append(tb_name)
        table_names = table_names[:AI_OPTIMIZE_MAX_TABLES]

        sql_hash = hashlib.md5(
            "||".join([db_type, db_name or "", masked_sql]).encode("utf-8")
        ).hexdigest()

        # 运行中去重（防双击重复建任务烧 token）；僵尸 stale 任务先判失败放行
        from sql.models import AIOptimizeTask

        running_task = (
            AIOptimizeTask.objects
            .filter(
                instance=instance, db_name=db_name or "", sql_hash=sql_hash,
                status__in=["pending", "running"],
            )
            .order_by("-created_at")
            .first()
        )
        if running_task and not _mark_stale_optimize_task_failed(running_task):
            return JsonResponse({
                "status": 0, "msg": "已有进行中的任务",
                "data": {"task_id": running_task.id, "reused": True},
            })

        task = AIOptimizeTask.objects.create(
            user=request.user,
            instance=instance,
            db_name=db_name or "",
            db_type=db_type,
            sql_hash=sql_hash,
            sql_text=masked_sql,
            table_names=table_names,
            model=model_name,
        )

        executor, slots = _get_optimize_executor()
        if not slots.acquire(blocking=False):
            task.status = "failed"
            task.error = f"AI 优化并发已满（同时最多 {_OPTIMIZE_MAX_WORKERS} 个执行 + {_OPTIMIZE_MAX_PENDING} 个排队），请稍后再试"
            task.finished_at = _dt.datetime.now()
            task.save(update_fields=["status", "error", "finished_at"])
            return JsonResponse({"status": 1, "msg": "AI 优化并发已满，请稍后再试"})

        def _run(_tid=task.id, _slots=slots):
            try:
                run_optimize_task(_tid)
            except Exception:
                logger.exception(f"AI 优化任务 {_tid} 执行异常")
            finally:
                _slots.release()
                # 线程池线程不是 Django 请求线程，任务结束主动关闭 DB 连接防泄漏
                try:
                    from django.db import connections
                    connections.close_all()
                except Exception:
                    pass

        try:
            executor.submit(_run)
        except Exception as e:
            slots.release()
            logger.error(f"提交 AI 优化任务失败: {e}", exc_info=True)
            task.status = "failed"
            task.error = f"提交任务失败: {e}"
            task.save(update_fields=["status", "error"])
            return JsonResponse({"status": 1, "msg": "提交 AI 优化任务失败"})

        return JsonResponse({"status": 0, "msg": "任务已提交", "data": {"task_id": task.id}})


class OptimizeAIAsyncStatusView(APIView):
    """轮询 AI 优化任务状态/报告"""

    permission_classes = [IsAuthenticated, SqlOptimizePermission]

    def get(self, request, task_id):
        from sql.models import AIOptimizeTask

        try:
            task = AIOptimizeTask.objects.select_related("instance").get(id=task_id)
        except AIOptimizeTask.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "任务不存在"})

        # 防枚举：仅任务发起人或超管可查看
        u = request.user
        if not (u.is_superuser or task.user_id == u.id):
            return JsonResponse({"status": 1, "msg": "无权查看该任务"})

        _mark_stale_optimize_task_failed(task)
        return JsonResponse(
            _encode({
                "status": 0,
                "msg": "success",
                "data": {
                    "task_id": task.id,
                    "status": task.status,
                    "error": task.error,
                    "report": task.report_markdown,
                    "steps": task.steps or [],
                    "model": task.model,
                    "prompt_tokens": task.prompt_tokens,
                    "completion_tokens": task.completion_tokens,
                    "created_at": task.created_at,
                    "finished_at": task.finished_at,
                },
            }),
            safe=False,
        )
