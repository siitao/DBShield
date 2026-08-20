# -*- coding: UTF-8 -*-
"""
数据归档任务（django_q 异步侧）。
旧视图层已在 DRF 化重构中迁移至 sql_api/api_archiver.py，本模块仅保留任务函数，
历史版本曾随重构被误删导致归档执行链路断裂，现按 9758b51^ 恢复任务部分。

@author: hhyo
@license: Apache Licence
@file: archiver.py
"""

import logging
import os
import re
import time

from django.conf import settings
from django.db import connection, close_old_connections
from django_q.tasks import async_task

from common.utils.const import WorkflowStatus
from common.utils.timer import FuncTimer
from sql.engines import get_engine
from sql.plugins.pt_archiver import PtArchiver
from sql.models import ArchiveConfig, ArchiveLog

logger = logging.getLogger("default")
__author__ = "hhyo"


def add_archive_task(archive_ids=None):
    """
    添加数据归档异步任务，仅处理有效归档任务
    :param archive_ids: 归档任务id列表
    :return:
    """
    archive_ids = archive_ids or []
    if not isinstance(archive_ids, list):
        archive_ids = list(archive_ids)
    # 没有传archive_id代表全部归档任务统一调度
    if archive_ids:
        archive_cnf_list = ArchiveConfig.objects.filter(
            id__in=archive_ids,
            state=True,
            status=WorkflowStatus.PASSED,
        )
    else:
        archive_cnf_list = ArchiveConfig.objects.filter(
            state=True, status=WorkflowStatus.PASSED
        )

    # 添加task任务
    for archive_info in archive_cnf_list:
        archive_id = archive_info.id
        async_task(
            "sql.archiver.archive",
            archive_id,
            group=f'archive-{time.strftime("%Y-%m-%d %H:%M:%S ")}',
            timeout=-1,
            task_name=f"archive-{archive_id}",
        )


def archive(archive_id):
    """
    执行数据库归档
    :return:
    """
    archive_info = ArchiveConfig.objects.get(id=archive_id)
    s_ins = archive_info.src_instance
    src_db_name = archive_info.src_db_name
    src_table_name = archive_info.src_table_name
    condition = archive_info.condition
    no_delete = archive_info.no_delete
    sleep = archive_info.sleep
    mode = archive_info.mode

    # 获取归档表的字符集信息
    s_engine = get_engine(s_ins)
    s_db = s_engine.schema_object.databases[src_db_name]
    s_tb = s_db.tables[src_table_name]
    s_charset = s_tb.options["charset"].value
    if s_charset is None:
        s_charset = s_db.options["charset"].value

    pt_archiver = PtArchiver()
    # 准备参数
    source = (
        rf"h={s_ins.host},u={s_ins.user},p={s_ins.password},"
        rf"P={s_ins.port},D={src_db_name},t={src_table_name},A={s_charset}"
    )
    args = {
        "no-version-check": True,
        "source": source,
        "where": condition,
        "progress": 5000,
        "statistics": True,
        "charset": "utf8",
        "limit": 10000,
        "txn-size": 1000,
        "sleep": sleep,
    }

    # 归档到目标实例
    if mode == "dest":
        d_ins = archive_info.dest_instance
        dest_db_name = archive_info.dest_db_name
        dest_table_name = archive_info.dest_table_name
        # 目标表的字符集信息
        schema_object = get_engine(d_ins).schema_object
        d_db = schema_object.databases[dest_db_name]
        d_tb = d_db.tables[dest_table_name]
        d_charset = d_tb.options["charset"].value
        if d_charset is None:
            d_charset = d_db.options["charset"].value
        schema_object.connection.close()
        # dest
        dest = (
            rf"h={d_ins.host},u={d_ins.user},p={d_ins.password},P={d_ins.port},"
            rf"D={dest_db_name},t={dest_table_name},A={d_charset}"
        )
        args["dest"] = dest
        if no_delete:
            args["no-delete"] = True
    elif mode == "file":
        output_directory = os.path.join(settings.BASE_DIR, "downloads/archiver")
        os.makedirs(output_directory, exist_ok=True)
        args["file"] = (
            f"{output_directory}/{s_ins.instance_name}-{src_db_name}-{src_table_name}.txt"
        )
        if no_delete:
            args["no-delete"] = True
    elif mode == "purge":
        args["purge"] = True

    # 参数检查
    args_check_result = pt_archiver.check_args(args)
    if args_check_result["status"] == 1:
        # 任务上下文无 HttpResponse 可返回，记录日志后原样返回结果由调用方处理
        logger.error(f"归档任务 {archive_id} 参数检查未通过: {args_check_result}")
        return args_check_result
    # 参数转换
    cmd_args = pt_archiver.generate_args2cmd(args)
    # 执行命令，获取结果
    select_cnt = 0
    insert_cnt = 0
    delete_cnt = 0
    with FuncTimer() as t:
        p = pt_archiver.execute_cmd(cmd_args)
        stdout = ""
        for line in iter(p.stdout.readline, ""):
            if re.match(r"^SELECT\s(\d+)$", line, re.I):
                select_cnt = re.findall(r"^SELECT\s(\d+)$", line)
            elif re.match(r"^INSERT\s(\d+)$", line, re.I):
                insert_cnt = re.findall(r"^INSERT\s(\d+)$", line)
            elif re.match(r"^DELETE\s(\d+)$", line, re.I):
                delete_cnt = re.findall(r"^DELETE\s(\d+)$", line)
            stdout += f"{line}\n"
    statistics = stdout
    # 获取异常信息
    stderr = p.stderr.read()
    if stderr:
        statistics = stdout + stderr

    # 判断归档结果
    select_cnt = int(select_cnt[0]) if select_cnt else 0
    insert_cnt = int(insert_cnt[0]) if insert_cnt else 0
    delete_cnt = int(delete_cnt[0]) if delete_cnt else 0
    error_info = ""
    success = True
    if stderr:
        error_info = f"命令执行报错:{stderr}"
        success = False
    if mode == "dest":
        # 删除源数据，判断删除数量和写入数量
        if not no_delete and (insert_cnt != delete_cnt):
            error_info = f"删除和写入数量不一致:{insert_cnt}!={delete_cnt}"
            success = False
    elif mode == "file":
        # 删除源数据，判断查询数量和删除数量
        if not no_delete and (select_cnt != delete_cnt):
            error_info = f"查询和删除数量不一致:{select_cnt}!={delete_cnt}"
            success = False
    elif mode == "purge":
        # 直接删除。判断查询数量和删除数量
        if select_cnt != delete_cnt:
            error_info = f"查询和删除数量不一致:{select_cnt}!={delete_cnt}"
            success = False

    # 执行信息保存到数据库
    if connection.connection and not connection.is_usable():
        close_old_connections()
    # 更新最后归档时间
    ArchiveConfig(id=archive_id, last_archive_time=t.end).save(
        update_fields=["last_archive_time"]
    )
    # 替换密码信息后保存
    shell_cmd = " ".join(cmd_args)
    ArchiveLog.objects.create(
        archive=archive_info,
        cmd=(
            shell_cmd.replace(s_ins.password, "***").replace(d_ins.password, "***")
            if mode == "dest"
            else shell_cmd.replace(s_ins.password, "***")
        ),
        condition=condition,
        mode=mode,
        no_delete=no_delete,
        sleep=sleep,
        select_cnt=select_cnt,
        insert_cnt=insert_cnt,
        delete_cnt=delete_cnt,
        statistics=statistics,
        success=success,
        error_info=error_info,
        start_time=t.start,
        end_time=t.end,
    )
    if not success:
        raise Exception(f"{error_info}\n{statistics}")
