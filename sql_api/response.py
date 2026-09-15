# -*- coding:utf-8 -*-
"""统一 JSON 响应信封。

信封约定（与前端各 api/*.ts 的 checkStatus 约定一致）：
- 业务成功：HTTP 200 + {"status": 0, "msg": "success", "data"/"total"/"rows": ...}
- 业务失败：HTTP 200 + {"status": 非0, "msg": 原因}
- 协议/权限/资源不存在类错误：标准 HTTP 状态码（401/403/404/405），
  由前端 axios 拦截器统一提取 msg/detail 弹错。

新代码请直接使用本模块，勿再复制本地实现。
"""

from common.utils.extend_json_encoder import encode_json as _encode
from django.http import JsonResponse


def success_response(data=None, msg="success"):
    """成功响应"""
    return JsonResponse(_encode({
        "status": 0,
        "msg": msg,
        "data": data
    }))


def error_response(msg="操作失败", status=1):
    """业务失败响应（HTTP 200，前端 checkStatus 按 status 非 0 处理）"""
    return JsonResponse({
        "status": status,
        "msg": msg,
        "data": None
    })


def list_response(rows, total):
    """列表响应"""
    return JsonResponse(_encode({
        "status": 0,
        "msg": "success",
        "total": total,
        "rows": rows
    }))
