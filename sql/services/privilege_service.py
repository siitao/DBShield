# -*- coding: UTF-8 -*-
"""查询权限申请授权服务。

查询权限申请审核通过后的授权落库逻辑单点收口：
- sql_api/api_misc._query_apply_audit_call_back（api_workflow / api_query_priv 引用）
- sql/query_privileges._query_apply_audit_call_back（旧视图残留，已删）

统一走本模块的 update_or_create 幂等实现（旧 bulk_create 版重复审核会撞唯一键）。
"""

import logging

from common.utils.const import WorkflowStatus
from sql.models import QueryPrivilegesApply, QueryPrivileges

logger = logging.getLogger("default")


def query_apply_audit_call_back(apply_id, workflow_status):
    """查询权限申请审核回调：更新申请状态；通过后按类型落库授权。

    :param apply_id: QueryPrivilegesApply.apply_id
    :param workflow_status: WorkflowStatus（PASSED / REJECTED）
    """
    apply_info = QueryPrivilegesApply.objects.get(apply_id=apply_id)
    if workflow_status == WorkflowStatus.PASSED:
        apply_info.status = WorkflowStatus.PASSED
        apply_info.save()
        # 通过后写入权限表
        ins = apply_info.instance
        if apply_info.priv_type == 1:
            for db in apply_info.db_list.split(","):
                QueryPrivileges.objects.update_or_create(
                    user_name=apply_info.user_name,
                    user_display=apply_info.user_display,
                    instance=ins,
                    db_name=db,
                    priv_type=1,
                    defaults={
                        "table_name": "",
                        "limit_num": apply_info.limit_num,
                        "valid_date": apply_info.valid_date,
                        "is_deleted": 0,
                    },
                )
        elif apply_info.priv_type == 2:
            for tb in apply_info.table_list.split(","):
                # lookup 必须包含 table_name：否则多表申请的各行 lookup 相同，
                # 互相覆盖只保留最后一张表（历史 bug）
                QueryPrivileges.objects.update_or_create(
                    user_name=apply_info.user_name,
                    user_display=apply_info.user_display,
                    instance=ins,
                    db_name=apply_info.db_list,
                    priv_type=2,
                    table_name=tb,
                    defaults={
                        "limit_num": apply_info.limit_num,
                        "valid_date": apply_info.valid_date,
                        "is_deleted": 0,
                    },
                )
    elif workflow_status == WorkflowStatus.REJECTED:
        apply_info.status = WorkflowStatus.REJECTED
        apply_info.save()
