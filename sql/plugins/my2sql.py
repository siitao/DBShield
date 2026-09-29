# -*- coding: UTF-8 -*-
"""
my2sql 插件封装。

职责：
1. build_args —— 把 Web 端选项翻译成 my2sql 命令行参数；
2. run —— 带超时执行，避免解析卡住时长时间占用请求线程 / 队列 worker；
3. collect_sql_rows / count_sql_rows —— 读取输出目录里的 *.sql；
4. is_failure / describe_failure —— 判定并翻译失败原因。

参数名一律以 my2sql 的 flag 定义为准（其源码 base/context.go 中 flag.XxxVar 的
第一个入参），不要臆造：Go 的 flag 包遇到未注册的 flag 会打印 usage 并以 exit 2
退出，整次解析直接失败；写成别的名字（例如把 -ignore-primaryKey-forInsert 写成
-ignore-primary-key-for-rollback）就是"点了选项必然失败"。
"""

import logging
import os
import re
import subprocess

from common.config import SysConfig
from sql.plugins.plugin import Plugin

logger = logging.getLogger("default")

# -work-type 取值
WORK_TYPE_2SQL = "2sql"
WORK_TYPE_ROLLBACK = "rollback"
# -sql 取值：仅支持这三类，逗号分隔
SQL_TYPES = ("insert", "update", "delete")

# 同步解析（HTTP 请求内）最长等待秒数：超过则提示改用异步解析，
# 不能让请求一直挂着（前端 axios 超时会直接放弃，用户只看到一直转圈）。
SYNC_TIMEOUT = 110
# 异步解析（django-q 任务）最长等待秒数：仅作兜底，避免异常进程长期占用 worker
TASK_TIMEOUT = 3600

# my2sql 日志打到 stdout（[fatal] xxx），只有 flag 解析错误才走 stderr，
# 因此失败判定必须同时看 stdout / stderr / 退出码，否则解析中断会被当成成功。
_FATAL_RE = re.compile(r"\[fatal\]")

# 失败原因 → 用户可读提示（my2sql 原文 → 处理建议）
_FAILURE_HINTS = (
    (
        r"column count \d+ in binlog > in table structure",
        "解析中断：该时间段的表结构与当前线上不一致（区间内发生过 DDL 变更）。"
        "请缩小解析范围后重试，回滚 SQL 也应在表结构未变更的区间内生成。",
    ),
    (
        r"no table struct found",
        "解析中断：binlog 中的表已被删除或改名，工具取不到表结构。",
    ),
    (
        r"Access denied|access denied",
        "解析失败：实例账号无权限。解析 binlog 需要 SELECT、REPLICATION SLAVE、"
        "REPLICATION CLIENT 权限。",
    ),
    (
        r"flag provided but not defined",
        "解析失败：传入的选项不被当前 my2sql 版本支持，请联系管理员升级工具。",
    ),
    (
        r"must less than stop position|must be ealier than",
        "解析失败：起始位置/时间必须早于终止位置/时间。",
    ),
    (
        r"invalid sqltypes|invalid arg for",
        "解析失败：选项取值不合法，请检查参数配置。",
    ),
    (
        r"Could not find first log file name|1236",
        "解析失败：起始 binlog 文件在实例上已不存在（可能已被清理），请重新选择。",
    ),
    (
        r"unsupported database name|unsupported table name",
        "解析失败：binlog 中的库名/表名含工具不支持的特殊字符。",
    ),
    (
        r"error replication from master|error to get binlog event|error to get binlog",
        "解析失败：从实例拉取 binlog 中断（连接被断开、或并发解析占用了相同 server-id）。"
        "请稍后重试；持续失败请检查实例与网络。",
    ),
)


class My2SQL(Plugin):
    def __init__(self):
        self.path = SysConfig().get("my2sql")
        self.required_args = []
        self.disable_args = []
        super(Plugin, self).__init__()

    @staticmethod
    def build_args(
        host,
        port,
        user,
        password,
        output_dir,
        work_type=WORK_TYPE_2SQL,
        threads=4,
        start_file=None,
        start_pos=None,
        stop_file=None,
        stop_pos=None,
        start_datetime=None,
        stop_datetime=None,
        databases=None,
        tables=None,
        sql_types=None,
        add_extra_info=False,
        ignore_primary_key=False,
        full_columns=False,
        no_db_prefix=False,
        file_per_table=False,
    ):
        """Web 端选项 → my2sql 命令行参数（键名即 my2sql 的 flag 名）。

        列表型参数（-databases/-tables/-sql）在 my2sql 中是"逗号分隔的字符串"，
        不能直接喂 Python 列表字面量，否则过滤条件静默失效（-sql 还会因取值非法直接
        退出）。空值交由 generate_args2cmd 跳过。
        """
        args = {
            # 连接与模式：默认 repl（伪装成从库向实例拉 binlog），file 模式要求本地有 binlog 文件
            "mode": "repl",
            "work-type": work_type,
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            # 解析控制
            "threads": threads,
            "start-file": start_file,
            "start-pos": start_pos,
            "stop-file": stop_file,
            "stop-pos": stop_pos,
            "start-datetime": start_datetime,
            "stop-datetime": stop_datetime,
            "output-dir": output_dir,
            # 过滤
            "databases": _join(databases),
            "tables": _join(tables),
            "sql": _join(sql_types),
            # 输出形态
            "add-extraInfo": add_extra_info,
            "ignore-primaryKey-forInsert": ignore_primary_key,
            "full-columns": full_columns,
            "do-not-add-prifixDb": no_db_prefix,
            "file-per-table": file_per_table,
        }
        return args

    def run(self, cmd_args, timeout):
        """执行 my2sql。

        :return: (stdout, stderr, returncode, timed_out)
        """
        process = self.execute_cmd(cmd_args)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return stdout, stderr, process.returncode, False
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            return stdout, stderr, process.returncode, True

    @staticmethod
    def collect_sql_rows(output_dir, limit=None):
        """读取输出目录下的 SQL 语句。

        :param limit: 最多返回多少条，None 表示不限
        :return: [{"sql": "...", "extra_info": "..."}, ...]
        """
        rows = []
        for extra_info, sql in _iter_sql_lines(output_dir):
            row = {"sql": sql}
            if extra_info:
                row["extra_info"] = extra_info
            rows.append(row)
            if limit and len(rows) >= limit:
                break
        return rows

    @staticmethod
    def count_sql_rows(output_dir):
        """统计输出目录下的 SQL 语句条数（不把内容读进内存）。"""
        return sum(1 for _ in _iter_sql_lines(output_dir))

    @staticmethod
    def remove_sql_files(output_dir):
        """清理输出目录里的 *.sql（保留 binlog_status.txt 等统计文件便于排查）。"""
        for root, _dirs, files in os.walk(output_dir):
            for fn in files:
                if fn.endswith(".sql"):
                    try:
                        os.remove(os.path.join(root, fn))
                    except OSError:
                        logger.warning("清理 my2sql 输出文件失败: %s", os.path.join(root, fn))


def _join(value):
    """列表/字符串 → my2sql 需要的逗号分隔字符串。"""
    if not value:
        return None
    if isinstance(value, str):
        value = [v for v in value.split(",") if v.strip()]
    return ",".join(str(v).strip() for v in value if str(v).strip()) or None


def _iter_sql_lines(output_dir):
    """按文件名顺序遍历输出目录下的 *.sql，产出 (extra_info, sql)。

    - 跳过隐藏文件：rollback 模式的中间文件是 .<db>.<tb>.rollback.<n>.sql
    - -add-extraInfo 的注释行（# datetime=... database=...）作为下一条 SQL 的附加信息
    """
    extra_info = ""
    for root, _dirs, files in os.walk(output_dir):
        for fn in sorted(files):
            if not fn.endswith(".sql") or fn.startswith("."):
                continue
            with open(os.path.join(root, fn), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    content = line.strip()
                    if not content:
                        continue
                    if content.startswith("#"):
                        extra_info = content
                        continue
                    if content[:6].upper() in ("INSERT", "DELETE", "UPDATE"):
                        yield extra_info, content if content.endswith(";") else content + ";"


def is_failure(stdout, stderr, returncode):
    """my2sql 是否失败。"""
    if returncode:
        return True
    if stderr and stderr.strip():
        return True
    return bool(stdout and _FATAL_RE.search(stdout))


def describe_failure(stdout, stderr, returncode):
    """把 my2sql 的输出翻译成用户可读的失败原因，细节由调用方写日志。"""
    output = "%s\n%s" % (stdout or "", stderr or "")
    for pattern, hint in _FAILURE_HINTS:
        if re.search(pattern, output, re.IGNORECASE):
            return hint
    return "my2sql 解析失败，请检查实例连接与参数配置（详情见后端日志）"
