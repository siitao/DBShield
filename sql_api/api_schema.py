# -*- coding: UTF-8 -*-
"""表级结构对比 API（POST /api/v1/schema/tablediff/）。

规则本体在 sql/services/schema_diff_service.py；本视图只做入参校验、
权限（SchemasyncPermission，实例归属在服务层双侧校验）与信封返回。
"""
import re

from django.http import JsonResponse
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from sql.services.schema_diff_service import table_diff
from sql_api.api_misc import SchemasyncPermission


class TableDiffSerializer(serializers.Serializer):
    instance_name = serializers.CharField()
    db_name = serializers.CharField()
    target_instance_name = serializers.CharField()
    target_db_name = serializers.CharField()
    tables = serializers.ListField(
        child=serializers.CharField(), required=False, default=list, allow_empty=True
    )
    sync_auto_inc = serializers.BooleanField(required=False, default=False)
    sync_comments = serializers.BooleanField(required=False, default=False)

    def validate_tables(self, value):
        # 引擎查询走参数化（IN %(tables)s），这里拦的是会破坏生成 SQL 的字符；
        # 表名拼进 ALTER 时另有反引号转义兜底
        for name in value:
            if re.search(r"[`'\";\\\r\n\s]", str(name)):
                raise serializers.ValidationError(f"表名包含非法字符: {name}")
        if len(value) > 200:
            raise serializers.ValidationError("对比表数量超过上限（200），请缩小选择范围")
        return value


class TableDiffView(APIView):
    """表级结构对比：对比列/索引/表选项差异，生成 patch/revert ALTER（不执行）"""

    permission_classes = [IsAuthenticated, SchemasyncPermission]

    def post(self, request):
        serializer = TableDiffSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        result = table_diff(
            user=request.user,
            instance_name=params["instance_name"],
            db_name=params["db_name"],
            target_instance_name=params["target_instance_name"],
            target_db_name=params["target_db_name"],
            tables=params.get("tables") or [],
            sync_auto_inc=params.get("sync_auto_inc", False),
            sync_comments=params.get("sync_comments", False),
        )
        return JsonResponse(result)
