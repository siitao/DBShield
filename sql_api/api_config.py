"""系统配置项管理。

提供：
- GET  /api/v1/config/              读取全部配置 key-value 字典（superuser 专用）
- POST /api/v1/config/change/       批量保存配置（替代旧 POST /config/change/）
- POST /api/v1/config/check_ai/     AI 服务连接测试
- POST /api/v1/config/check_inception/  goInception + 备份库连接测试（替代旧 /check/go_inception/）
"""

import logging
import traceback

import simplejson as json
from django.http import JsonResponse
from drf_spectacular.utils import extend_schema
from rest_framework import views, permissions
from rest_framework.response import Response

from common.config import SysConfig
from common.utils.ai_gateway import test_openai_connection

logger = logging.getLogger(__name__)


class IsSuperuser(permissions.BasePermission):
    """仅超管。"""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


class ConfigView(views.APIView):
    """系统全部配置（GET → {key:value} 字典）。"""

    permission_classes = [IsSuperuser]

    @extend_schema(
        summary="系统配置（全部）",
        description="返回全部配置项 key-value 字典（superuser 专用）。",
    )
    def get(self, request):
        sys_config = SysConfig()
        sys_config.get_all_config()
        return Response(sys_config.sys_config)


class ChangeConfigView(views.APIView):
    """批量保存系统配置（替代旧 POST /config/change/）。

    入参兼容三种格式（SysConfig.replace 内部固定用 json.loads 解析字符串）：
    - JSON body: {"configs": [{"key": "...", "value": "..."}, ...]}  ← SPA 新版
    - JSON body: {"configs": "[{\"key\":...}]" }                      ← 字符串化的 JSON
    - form-encoded: configs=<上述 JSON 字符串>                          ← 与旧接口逐字兼容
    """

    permission_classes = [IsSuperuser]

    @extend_schema(
        summary="保存系统配置",
        description="批量保存配置项（superuser 专用）。body: {configs: [{key,value}]}。"
        "也兼容旧 form-encoded 的 configs=<JSON 字符串>。",
    )
    def post(self, request):
        # 优先取 JSON body 的 configs；回退到 form 的 configs 字段（旧格式）
        configs = request.data.get("configs")
        if configs is None:
            configs = request.POST.get("configs")

        # SysConfig.replace 内部用 json.loads(configs)，要求 configs 是 JSON 字符串。
        # JSON body 提交时 configs 可能已经是 list/dict，这里统一序列化回字符串。
        if isinstance(configs, (list, dict)):
            configs = json.dumps(configs, ensure_ascii=False)

        result = SysConfig().replace(configs)
        return JsonResponse(result)


class CheckAIConnectionView(views.APIView):
    """AI 服务连接测试。

    接收表单参数（允许测试尚未保存的临时值），发送一个最简 chat 请求验证连通性。
    """

    permission_classes = [IsSuperuser]

    @extend_schema(
        summary="AI 服务连接测试",
        description="测试 openai_base_url / openai_api_key / default_chat_model 是否可用，"
        "发送一个最简 chat 请求验证连通性。",
    )
    def post(self, request):
        data = request.data
        base_url = data.get("openai_base_url")
        api_key = data.get("openai_api_key")
        model = data.get("default_chat_model")
        ok, msg = test_openai_connection(base_url=base_url, api_key=api_key, model=model)
        return JsonResponse(
            {"status": 0 if ok else 1, "msg": "连接成功" if ok else f"连接失败：{msg}"}
        )


class CheckInceptionView(views.APIView):
    """goInception + 远程备份库连接测试（替代旧 POST /check/go_inception/）。

    同时兼容 JSON body 与 form-encoded 两种提交方式（旧前端用 form）。
    """

    permission_classes = [IsSuperuser]

    @extend_schema(
        summary="goInception 连接测试",
        description="测试 goInception 主库 + 远程备份库连通性。"
        "body: go_inception_host/port/user/password + inception_remote_backup_*。",
    )
    def post(self, request):
        data = request.data
        result = {"status": 0, "msg": "ok", "data": []}

        try:
            import MySQLdb

            conn = MySQLdb.connect(
                host=data.get("go_inception_host", ""),
                port=int(data.get("go_inception_port", 0) or 0),
                user=data.get("go_inception_user", ""),
                password=data.get("go_inception_password", ""),
                charset="utf8mb4",
                connect_timeout=5,
            )
            conn.close()
        except Exception as e:
            logger.error(traceback.format_exc())
            result["status"] = 1
            result["msg"] = f"无法连接goInception\n{e}"
            return JsonResponse(result)

        try:
            conn = MySQLdb.connect(
                host=data.get("inception_remote_backup_host", ""),
                port=int(data.get("inception_remote_backup_port", 0) or 0),
                user=data.get("inception_remote_backup_user", ""),
                password=data.get("inception_remote_backup_password", ""),
                charset="utf8mb4",
                connect_timeout=5,
            )
            conn.close()
        except Exception as e:
            logger.error(traceback.format_exc())
            result["status"] = 1
            result["msg"] = f"无法连接goInception备份库\n{e}"
            return JsonResponse(result)

        return JsonResponse(result)
