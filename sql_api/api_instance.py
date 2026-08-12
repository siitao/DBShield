import logging

from rest_framework import views, generics, status, serializers, permissions
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema
from sql.utils.sql_utils import filter_db_list
from .serializers import (
    InstanceSerializer,
    InstanceDetailSerializer,
    TunnelSerializer,
    AliyunRdsSerializer,
    InstanceResourceSerializer,
    InstanceResourceListSerializer,
    TableInstanceLookupSerializer,
    TableInstanceLookupResponseSerializer,
)
from .pagination import CustomizedPagination
from .filters import InstanceFilter
from sql.models import Instance, Tunnel, AliyunRdsConfig, InstanceTag
from sql.engines import get_engine
from sql.utils.resource_group import user_instances
from .table_instance_locator import resolve_table_instances
from django.http import Http404
import MySQLdb

logger = logging.getLogger(__name__)


class InstanceTagList(views.APIView):
    """实例标签清单（供实例表单的 instance_tag M2M 选择器使用）。"""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(summary="实例标签清单", description="返回全部激活的实例标签。")
    def get(self, request):
        rows = InstanceTag.objects.filter(active=1).values("id", "tag_code", "tag_name")
        return Response(list(rows))


class InstanceList(generics.ListAPIView):
    """
    列出所有的instance或者创建一个新的instance配置
    """

    filterset_class = InstanceFilter
    pagination_class = CustomizedPagination
    serializer_class = InstanceSerializer
    queryset = Instance.objects.all().order_by("id")

    @extend_schema(
        summary="实例清单",
        request=InstanceSerializer,
        responses={200: InstanceSerializer},
        description="列出所有实例（过滤，分页）",
    )
    def get(self, request):
        instances = self.filter_queryset(self.queryset)
        page_ins = self.paginate_queryset(queryset=instances)
        serializer_obj = self.get_serializer(page_ins, many=True)
        data = {"data": serializer_obj.data}
        return self.get_paginated_response(data)

    @extend_schema(
        summary="创建实例",
        request=InstanceSerializer,
        responses={201: InstanceSerializer},
        description="创建一个实例配置",
    )
    def post(self, request):
        serializer = InstanceSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class InstanceDetail(views.APIView):
    """
    实例操作
    """

    serializer_class = InstanceDetailSerializer

    def get_object(self, pk):
        try:
            return Instance.objects.get(pk=pk)
        except Instance.DoesNotExist:
            raise Http404

    @extend_schema(
        summary="更新实例",
        request=InstanceDetailSerializer,
        responses={200: InstanceDetailSerializer},
        description="更新一个实例配置",
    )
    def put(self, request, pk):
        instance = self.get_object(pk)
        serializer = InstanceDetailSerializer(instance, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(summary="删除实例", description="删除一个实例配置")
    def delete(self, request, pk):
        instance = self.get_object(pk)
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TunnelList(generics.ListAPIView):
    """
    列出所有的tunnel或者创建一个新的tunnel配置
    """

    pagination_class = CustomizedPagination
    serializer_class = TunnelSerializer
    queryset = Tunnel.objects.all().order_by("id")

    @extend_schema(
        summary="隧道清单",
        request=TunnelSerializer,
        responses={200: TunnelSerializer},
        description="列出所有隧道（过滤，分页）",
    )
    def get(self, request):
        tunnels = self.filter_queryset(self.queryset)
        page_tunnels = self.paginate_queryset(queryset=tunnels)
        serializer_obj = self.get_serializer(page_tunnels, many=True)
        data = {"data": serializer_obj.data}
        return self.get_paginated_response(data)

    @extend_schema(
        summary="创建隧道",
        request=TunnelSerializer,
        responses={201: TunnelSerializer},
        description="创建一个隧道配置",
    )
    def post(self, request):
        serializer = TunnelSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AliyunRdsList(generics.ListAPIView):
    """
    列出所有的AliyunRDS或者创建一个新的AliyunRDS配置
    """

    pagination_class = CustomizedPagination
    serializer_class = AliyunRdsSerializer
    queryset = AliyunRdsConfig.objects.all().select_related("ak").order_by("id")

    @extend_schema(
        summary="AliyunRDS清单",
        request=AliyunRdsSerializer,
        responses={200: AliyunRdsSerializer},
        description="列出所有AliyunRDS（过滤，分页）",
    )
    def get(self, request):
        aliyunrds = self.filter_queryset(self.queryset)
        page_rds = self.paginate_queryset(queryset=aliyunrds)
        serializer_obj = self.get_serializer(page_rds, many=True)
        data = {"data": serializer_obj.data}
        return self.get_paginated_response(data)

    @extend_schema(
        summary="创建AliyunRDS",
        request=AliyunRdsSerializer,
        responses={201: AliyunRdsSerializer},
        description="创建一个AliyunRDS配置（包含一个CloudAccessKey）",
    )
    def post(self, request):
        serializer = AliyunRdsSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AliyunRdsConfigPermission(permissions.BasePermission):
    """AliyunRDS 配置含云账号密钥，仅超管可读写（H6）"""

    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and u.is_superuser


class AliyunRdsDetail(views.APIView):
    """单个 AliyunRDS 配置：按 pk 查 / 改 / 删。OneToOne 到 Instance。"""

    permission_classes = [AliyunRdsConfigPermission]

    def get_object(self, pk):
        try:
            return AliyunRdsConfig.objects.get(pk=pk)
        except AliyunRdsConfig.DoesNotExist:
            raise Http404

    @extend_schema(summary="AliyunRDS详情", description="按 id 查 RDS 配置。")
    def get(self, request, pk):
        obj = self.get_object(pk=pk)
        return Response(AliyunRdsSerializer(obj).data)

    @extend_schema(
        summary="更新AliyunRDS",
        request=AliyunRdsSerializer,
        description="更新 RDS 配置及其 AccessKey。",
    )
    def put(self, request, pk):
        obj = self.get_object(pk=pk)
        data = request.data
        obj.rds_dbinstanceid = data.get("rds_dbinstanceid", obj.rds_dbinstanceid)
        obj.is_enable = data.get("is_enable", obj.is_enable)
        ak_data = data.get("ak") or {}
        # H6：修复二次加密损坏——key_secret 不回显（write_only），只接受新值；
        # key_id 客户端回传的是明文（序列化器已解密），仅当显式提供时才落库，
        # 避免把库中密文再次加密
        ak_changed = False
        if ak_data.get("key_id"):
            obj.ak.key_id = ak_data["key_id"]
            ak_changed = True
        if ak_data.get("key_secret"):
            obj.ak.key_secret = ak_data["key_secret"]
            ak_changed = True
        if ak_data.get("remark") is not None:
            obj.ak.remark = ak_data["remark"]
            ak_changed = True
        if ak_changed:
            obj.ak.save()
        obj.save()
        return Response(AliyunRdsSerializer(obj).data)

    @extend_schema(summary="删除AliyunRDS", description="删除 RDS 配置（不含 ak，ak 留存）")
    def delete(self, request, pk):
        obj = self.get_object(pk=pk)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AliyunRdsByInstance(views.APIView):
    """按实例查其 AliyunRDS 配置（OneToOne）。
    GET /api/v1/instance/rds/by_instance/?instance=<id> → 配置或 404。供 SPA 实例表单回填。"""

    permission_classes = [AliyunRdsConfigPermission]

    @extend_schema(summary="按实例查AliyunRDS", description="按 instance_id 查 RDS 配置，无则 404。")
    def get(self, request):
        instance_id = request.GET.get("instance")
        try:
            obj = AliyunRdsConfig.objects.get(instance_id=instance_id)
        except AliyunRdsConfig.DoesNotExist:
            raise Http404
        return Response(AliyunRdsSerializer(obj).data)


class InstanceResource(views.APIView):
    """
    获取实例内的资源信息，database、schema、table、column
    """

    @extend_schema(
        summary="实例资源",
        request=InstanceResourceSerializer,
        responses={200: InstanceResourceListSerializer},
        description="获取实例内的资源信息",
    )
    def post(self, request):
        # 参数验证
        serializer = InstanceResourceSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        instance_id = request.data["instance_id"]
        resource_type = request.data["resource_type"]
        db_name = request.data["db_name"] if "db_name" in request.data.keys() else ""
        schema_name = (
            request.data["schema_name"] if "schema_name" in request.data.keys() else ""
        )
        tb_name = request.data["tb_name"] if "tb_name" in request.data.keys() else ""
        instance = Instance.objects.get(pk=instance_id)

        try:
            query_engine = get_engine(instance=instance)
            db_name = query_engine.escape_string(db_name)
            schema_name = query_engine.escape_string(schema_name)
            tb_name = query_engine.escape_string(tb_name)
            if resource_type == "database":
                resource = query_engine.get_all_databases()
                resource.rows = filter_db_list(
                    db_list=resource.rows,
                    db_name_regex=query_engine.instance.show_db_name_regex,
                    is_match_regex=True,
                )
                resource.rows = filter_db_list(
                    db_list=resource.rows,
                    db_name_regex=query_engine.instance.denied_db_name_regex,
                    is_match_regex=False,
                )
            elif resource_type == "schema" and db_name:
                resource = query_engine.get_all_schemas(db_name=db_name)
            elif resource_type == "table" and db_name:
                resource = query_engine.get_all_tables(
                    db_name=db_name, schema_name=schema_name
                )
            elif resource_type == "column" and db_name and tb_name:
                resource = query_engine.get_all_columns_by_tb(
                    db_name=db_name, tb_name=tb_name, schema_name=schema_name
                )
            else:
                raise serializers.ValidationError(
                    {"errors": "不支持的资源类型或者参数不完整！"}
                )
        except Exception as msg:
            raise serializers.ValidationError({"errors": msg})
        else:
            if resource.error:
                raise serializers.ValidationError({"errors": resource.error})
            else:
                resource = {"count": len(resource.rows), "result": resource.rows}
                serializer_obj = InstanceResourceListSerializer(resource)
                return Response(serializer_obj.data)


class TableInstanceLookup(views.APIView):
    """按表名查询所属实例列表。"""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="按表名查询所属实例",
        request=TableInstanceLookupSerializer,
        responses={200: TableInstanceLookupResponseSerializer},
        description="输入table名，返回包含该表的实例列表（固定返回status/msg/count/data）。",
    )
    def post(self, request):
        serializer = TableInstanceLookupSerializer(data=request.data)
        if not serializer.is_valid():
            errors = serializer.errors
            msg = "参数校验失败"
            if "table_name" in errors:
                msg = f"参数table_name错误: {errors['table_name'][0]}"
            return Response({"status": 1, "msg": msg, "count": 0, "data": []})

        table_name = serializer.validated_data["table_name"]
        instances = user_instances(request.user)

        try:
            data = resolve_table_instances(
                table_name=table_name,
                instances=instances,
                request=request,
            )
        except Exception as e:
            logger.exception(f"查询表所属实例失败: {e}")
            return Response(
                {
                    "status": 1,
                    "msg": "查询失败: 未知错误, 请联系管理员",
                    "count": 0,
                    "data": [],
                }
            )

        return Response(
            {
                "status": 0,
                "msg": "查询成功",
                "count": len(data),
                "data": data,
            }
        )
