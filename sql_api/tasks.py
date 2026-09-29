# -*- coding: UTF-8 -*-
"""django-q 异步任务：后台执行 my2sql 解析 binlog。

大 binlog（几百 MB 级）解析耗时以分钟计，放在 HTTP 请求里必然超时，
因此「保存到文件（异步）」走这里：请求只负责提交任务，解析完成后由
hook=sql.notify.notify_for_my2sql 通过站内信/钉钉等通知提交人。

任务返回值约定（被 sql/notify.py 的 notify_for_my2sql 依赖）：
- 成功：返回 [SQL 条数, 输出目录]，hook 用 result[1] 作为文件目录播报
- 失败：抛异常，hook 用 task.result 作为失败原因播报
- kwargs 必须带 user（提交人用户名），供 hook 定位通知接收人
"""

import logging

from sql.plugins.my2sql import TASK_TIMEOUT, My2SQL, describe_failure, is_failure

logger = logging.getLogger("default")


def my2sql_execute(user, args, output_dir):
    """执行 my2sql，SQL 文件落在 output_dir。

    :param user: 提交人用户名（通知接收人）
    :param args: sql.plugins.my2sql.My2SQL.build_args 生成的参数
    :param output_dir: 输出目录
    :return: [SQL 条数, 输出目录]
    """
    my2sql = My2SQL()
    args_check = my2sql.check_args(args)
    if args_check["status"] == 1:
        raise RuntimeError(args_check["msg"])

    cmd_args = my2sql.generate_args2cmd(args)
    logger.info(
        "my2sql 异步解析开始 提交人=%s 输出目录=%s", user, output_dir
    )
    stdout, stderr, returncode, timed_out = my2sql.run(cmd_args, TASK_TIMEOUT)

    if timed_out:
        my2sql.remove_sql_files(output_dir)
        raise RuntimeError(
            "解析超时（超过 %s 秒），请缩小解析范围后重试" % TASK_TIMEOUT
        )
    if is_failure(stdout, stderr, returncode):
        # 细节只进后端日志：my2sql 的日志在 stdout，flag 报错在 stderr
        logger.error(
            "my2sql 异步解析失败 rc=%s output_dir=%s\nstdout:\n%s\nstderr:\n%s",
            returncode,
            output_dir,
            (stdout or "")[-4000:],
            (stderr or "")[-4000:],
        )
        my2sql.remove_sql_files(output_dir)
        raise RuntimeError(describe_failure(stdout, stderr, returncode))

    sql_count = my2sql.count_sql_rows(output_dir)
    logger.info(
        "my2sql 异步解析完成 提交人=%s 输出目录=%s SQL条数=%s",
        user,
        output_dir,
        sql_count,
    )
    return [sql_count, output_dir]
