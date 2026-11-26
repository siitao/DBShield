import json
from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers
from sql.models import (
    AIReviewRecord,
    Users,
    Instance,
    Tunnel,
    AliyunRdsConfig,
    CloudAccessKey,
    SqlWorkflow,
    SqlWorkflowContent,
    ResourceGroup,
    WorkflowAudit,
    WorkflowLog,
    QueryPrivilegesApply,
    ArchiveConfig,
)
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from sql.engines import get_engine
from sql.utils.ai_review import AI_REVIEW_FIELDS, AI_REVIEW_TTL_HOURS, sql_fingerprint
from sql.utils.sql_utils import validate_db_name
from sql.utils.workflow_audit import Audit, get_auditor
from sql.utils.resource_group import user_groups, user_instances
from common.utils.const import WorkflowType, WorkflowStatus
from common.config import SysConfig
import inspect
import traceback
import logging
from sql.offlinedownload import OffLineDownLoad

logger = logging.getLogger("default")


class UserSerializer(serializers.ModelSerializer):
    def create(self, validated_data):
        with transaction.atomic():
            extra_data = dict()
            for field in ("groups", "user_permissions", "resource_group"):
                if field in validated_data.keys():
                    extra_data[field] = validated_data.pop(field)
            user = Users(**validated_data)
            user.set_password(validated_data["password"])
            user.save()
            for field in extra_data.keys():
                getattr(user, field).set(extra_data[field])
            return user

    def validate_password(self, password):
        try:
            validate_password(password)
        except ValidationError as msg:
            raise serializers.ValidationError(msg)
        return password

    class Meta:
        model = Users
        fields = "__all__"
        extra_kwargs = {"password": {"write_only": True}, "display": {"required": True}}


class UserDetailSerializer(serializers.ModelSerializer):
    def update(self, instance, validated_data):
        for attr, value in validated_data.items():
            if attr == "password":
                instance.set_password(value)
            elif attr in ("groups", "user_permissions", "resource_group"):
                getattr(instance, attr).set(value)
            else:
                setattr(instance, attr, value)
        instance.save()
        return instance

    def validate_password(self, password):
        try:
            validate_password(password)
        except ValidationError as msg:
            raise serializers.ValidationError(msg)
        return password

    class Meta:
        model = Users
        fields = "__all__"
        extra_kwargs = {
            "password": {"write_only": True, "required": False},
            "username": {"required": False},
        }


class GroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = Group
        fields = "__all__"


class ResourceGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceGroup
        fields = "__all__"


class TwoFASerializer(serializers.Serializer):
    engineer = serializers.CharField(label="用户名")
    enable = serializers.ChoiceField(choices=["true", "false"], label="启用or禁用")
    phone = serializers.CharField(required=False, label="手机号码")
    auth_type = serializers.ChoiceField(
        choices=["totp", "sms"], label="验证类型：totp-Google身份验证器，sms-短信验证码"
    )

    def validate(self, attrs):
        engineer = attrs.get("engineer")

        try:
            Users.objects.get(username=engineer)
        except Users.DoesNotExist:
            raise serializers.ValidationError({"errors": "不存在该用户"})

        return attrs


class TwoFAStateSerializer(serializers.Serializer):
    engineer = serializers.CharField(label="用户名")

    def validate(self, attrs):
        engineer = attrs.get("engineer")

        try:
            Users.objects.get(username=engineer)
        except Users.DoesNotExist:
            raise serializers.ValidationError({"errors": "不存在该用户"})

        return attrs


class TwoFASaveSerializer(serializers.Serializer):
    engineer = serializers.CharField(label="用户名")
    key = serializers.CharField(required=False, label="密钥")
    phone = serializers.CharField(required=False, label="手机号码")
    auth_type = serializers.ChoiceField(
        choices=["disabled", "totp", "sms"],
        label="验证类型：disabled-关闭，totp-Google身份验证器，sms-短信验证码",
    )

    def validate(self, attrs):
        engineer = attrs.get("engineer")
        auth_type = attrs.get("auth_type")
        key = attrs.get("key")
        phone = attrs.get("phone")

        if auth_type == "sms":
            if not phone:
                raise serializers.ValidationError({"errors": "缺少 phone"})

        if auth_type == "totp":
            if not key:
                raise serializers.ValidationError({"errors": "缺少 key"})

        try:
            Users.objects.get(username=engineer)
        except Users.DoesNotExist:
            raise serializers.ValidationError({"errors": "不存在该用户"})

        return attrs


class TwoFAVerifySerializer(serializers.Serializer):
    engineer = serializers.CharField(label="用户名")
    # 验证码按字符串接收，保留前导 0（TOTP/短信验证码都可能是 0 开头）
    otp = serializers.CharField(label="一次性密码/验证码", min_length=6, max_length=8)
    auth_type = serializers.ChoiceField(choices=["totp", "sms"], label="验证方式")

    def validate(self, attrs):
        try:
            Users.objects.get(username=attrs.get("engineer"))
        except Users.DoesNotExist:
            raise serializers.ValidationError({"errors": "不存在该用户"})
        return attrs


class InstanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Instance
        fields = "__all__"
        extra_kwargs = {"password": {"write_only": True}}


class InstanceDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Instance
        fields = "__all__"
        extra_kwargs = {
            "password": {"write_only": True},
            "instance_name": {"required": False},
            "type": {"required": False},
            "db_type": {"required": False},
            "host": {"required": False},
        }


class TunnelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tunnel
        fields = "__all__"
        # DRF 不支持 write_only_fields，必须用 extra_kwargs；否则密码/私钥会回显
        extra_kwargs = {
            "password": {"write_only": True},
            "pkey": {"write_only": True},
            "pkey_password": {"write_only": True},
        }


class CloudAccessKeySerializer(serializers.ModelSerializer):
    """key_secret 只写不回显（防密文/密钥泄漏）；key_id 展示时解密为明文（AK ID 非机密）。"""

    key_secret = serializers.CharField(
        write_only=True, required=False, allow_blank=True
    )

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data.pop("key_secret", None)
        try:
            data["key_id"] = instance.raw_key_id
        except Exception:
            # 解密失败（如历史脏数据）时原样返回，不阻断展示
            pass
        return data

    class Meta:
        model = CloudAccessKey
        fields = ("id", "type", "key_id", "key_secret", "remark")


class AliyunRdsSerializer(serializers.ModelSerializer):
    ak = CloudAccessKeySerializer()

    def create(self, validated_data):
        """创建包含accesskey的aliyunrds实例"""
        rds_data = validated_data.pop("ak")

        try:
            with transaction.atomic():
                ak = CloudAccessKey.objects.create(**rds_data)
                rds = AliyunRdsConfig.objects.create(ak=ak, **validated_data)
        except Exception as e:
            logger.error(f"创建AliyunRds报错，错误信息：{traceback.format_exc()}")
            raise serializers.ValidationError({"errors": str(e)})
        else:
            return rds

    class Meta:
        model = AliyunRdsConfig
        fields = ("id", "rds_dbinstanceid", "is_enable", "instance", "ak")


class QueryPrivilegesApplySerializer(serializers.ModelSerializer):
    class Meta:
        model = QueryPrivilegesApply
        fields = "__all__"


class ArchiveConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = ArchiveConfig
        fields = "__all__"


class InstanceResourceSerializer(serializers.Serializer):
    instance_id = serializers.IntegerField(label="实例id")
    resource_type = serializers.ChoiceField(
        choices=["database", "schema", "table", "column"], label="资源类型"
    )
    db_name = serializers.CharField(required=False, label="数据库名")
    schema_name = serializers.CharField(required=False, label="schema名")
    tb_name = serializers.CharField(required=False, label="表名")

    def validate(self, attrs):
        instance_id = attrs.get("instance_id")

        try:
            Instance.objects.get(id=instance_id)
        except Instance.DoesNotExist:
            raise serializers.ValidationError({"errors": "不存在该实例"})

        return attrs


class InstanceResourceListSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    result = serializers.ListField()


class TableInstanceLookupSerializer(serializers.Serializer):
    table_name = serializers.CharField(label="表名", max_length=256)


class TableInstanceSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    db_type = serializers.CharField()
    db_name = serializers.CharField()
    table_name = serializers.CharField(
        required=False, allow_null=True, allow_blank=True
    )


class LocatorFailureReasonSerializer(serializers.Serializer):
    instance_name = serializers.CharField()
    reason = serializers.CharField()


class LocatorExecutionSummarySerializer(serializers.Serializer):
    processed_instance_count = serializers.IntegerField()
    successful_instance_count = serializers.IntegerField()
    failed_instance_count = serializers.IntegerField()
    failure_reasons = LocatorFailureReasonSerializer(many=True)


class TableInstanceLookupResponseSerializer(serializers.Serializer):
    status = serializers.IntegerField()
    msg = serializers.CharField()
    count = serializers.IntegerField()
    data = TableInstanceSerializer(many=True)
    summary = LocatorExecutionSummarySerializer(required=False, allow_null=True)


class SqlQueryInstancesQuerySerializer(serializers.Serializer):
    type = serializers.CharField(required=False, allow_blank=True)
    db_type = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True
    )
    tag_codes = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True
    )


class SqlQueryResourceQuerySerializer(serializers.Serializer):
    instance_id = serializers.IntegerField(required=False)
    instance_name = serializers.CharField(required=False, allow_blank=True)
    resource_type = serializers.ChoiceField(
        choices=["database", "schema", "table", "column"]
    )
    db_name = serializers.CharField(required=False, allow_blank=True)
    schema_name = serializers.CharField(required=False, allow_blank=True)
    tb_name = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get("instance_id") and not attrs.get("instance_name"):
            raise serializers.ValidationError(
                "instance_id 或 instance_name 必须提供一个"
            )
        resource_type = attrs.get("resource_type")
        if resource_type in ("table", "schema") and not attrs.get("db_name"):
            raise serializers.ValidationError("db_name 不能为空")
        if resource_type == "column" and (
            not attrs.get("db_name") or not attrs.get("tb_name")
        ):
            raise serializers.ValidationError("column 查询需提供 db_name 和 tb_name")
        return attrs


class SqlQueryDescribeTableSerializer(serializers.Serializer):
    instance_name = serializers.CharField()
    db_name = serializers.CharField()
    tb_name = serializers.CharField()
    schema_name = serializers.CharField(required=False, allow_blank=True, default="")


class SqlQueryExecuteSerializer(serializers.Serializer):
    instance_name = serializers.CharField()
    db_name = serializers.CharField()
    schema_name = serializers.CharField(required=False, allow_blank=True)
    tb_name = serializers.CharField(required=False, allow_blank=True)
    sql_content = serializers.CharField()
    limit_num = serializers.IntegerField(min_value=0)


class SqlQueryLogsQuerySerializer(serializers.Serializer):
    limit = serializers.IntegerField(
        required=False, default=20, min_value=1, max_value=500
    )
    offset = serializers.IntegerField(required=False, default=0, min_value=0)
    search = serializers.CharField(required=False, allow_blank=True, default="")
    star = serializers.CharField(required=False, allow_blank=True, default="")
    query_log_id = serializers.IntegerField(required=False)
    start_date = serializers.CharField(required=False, allow_blank=True, default="")
    end_date = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_star(self, value):
        return str(value).lower() == "true"


class SqlQueryFavoriteSerializer(serializers.Serializer):
    query_log_id = serializers.IntegerField(min_value=1)
    star = serializers.CharField()
    alias = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_star(self, value):
        return str(value).lower() == "true"


class ExecuteCheckSerializer(serializers.Serializer):
    instance_id = serializers.IntegerField(label="实例id")
    db_name = serializers.CharField(label="数据库名")
    full_sql = serializers.CharField(label="SQL内容")

    def validate_db_name(self, db_name):
        # 库名会被拼进 goInception 的 `use` 语句，必须在此收口（防标识符逃逸）。
        # 仅 mysql 走 goInception，严格黑名单也只对 mysql 生效；其他库
        # （如 PostgreSQL 合法库名可含空格/引号）不套用该校验，
        # 与工单入口（WorkflowSerializer.validate）保持同口径。
        instance = getattr(self, "_instance", None)
        if instance is not None and instance.db_type != "mysql":
            return db_name
        try:
            return validate_db_name(db_name)
        except ValueError as e:
            raise serializers.ValidationError(str(e))

    def validate_instance_id(self, instance_id):
        try:
            instance = Instance.objects.get(pk=instance_id)
        except Instance.DoesNotExist:
            raise serializers.ValidationError(
                {"errors": f"不存在该实例：{instance_id}"}
            )
        # 缓存实例供 validate_db_name 判断校验强度
        # （字段按声明顺序校验，instance_id 在 db_name 之前）
        self._instance = instance
        return instance_id

    def get_instance(self):
        return Instance.objects.get(pk=self.validated_data["instance_id"])


class ExecuteCheckResultSerializer(serializers.Serializer):
    is_execute = serializers.BooleanField(read_only=True, default=False)
    checked = serializers.CharField(read_only=True)
    warning = serializers.CharField(read_only=True)
    error = serializers.CharField(read_only=True)
    warning_count = serializers.IntegerField(read_only=True)
    error_count = serializers.IntegerField(read_only=True)
    is_critical = serializers.BooleanField(read_only=True, default=False)
    syntax_type = serializers.IntegerField(read_only=True)
    rows = serializers.JSONField(read_only=True)
    column_list = serializers.JSONField(read_only=True)
    status = serializers.CharField(read_only=True)
    affected_rows = serializers.IntegerField(read_only=True)
    ai_review_id = serializers.SerializerMethodField()

    def get_ai_review_id(self, obj):
        """服务端 AI 审核快照 token；AI 未启用/失败时为空串。"""
        return getattr(obj, "ai_review_id", "")


class WorkflowSerializer(serializers.ModelSerializer):
    def to_internal_value(self, data):
        if data.get("run_date_start") == "":
            data["run_date_start"] = None
        if data.get("run_date_end") == "":
            data["run_date_end"] = None
        return super().to_internal_value(data)

    @staticmethod
    def validate_group_id(group_id):
        try:
            ResourceGroup.objects.get(pk=group_id)
        except ResourceGroup.DoesNotExist:
            raise serializers.ValidationError({"errors": f"不存在该资源组：{group_id}"})
        return group_id

    def validate(self, attrs):
        # MySQL 工单的库名会被拼进 goInception 的 `use \`db\``（见
        # GoInceptionEngine.execute_check/execute），提交人可控且执行时用的是
        # 平台托管账号，因此必须在入口拦住反引号/分号等逃逸字符，
        # 否则可把未经审核的语句并入执行批次（H5：审核绕过）。
        instance = attrs.get("instance")
        if instance is not None and instance.db_type == "mysql":
            try:
                attrs["db_name"] = validate_db_name(attrs.get("db_name"))
            except ValueError as e:
                raise serializers.ValidationError({"db_name": str(e)})
        return attrs

    class Meta:
        model = SqlWorkflow
        fields = "__all__"
        read_only_fields = [
            "status",
            "syntax_type",
            "audit_auth_groups",
            "engineer_display",
            "group_name",
            "finish_time",
            "is_manual",
        ]
        extra_kwargs = {
            "demand_url": {"required": False},
            "is_backup": {"required": False},
            "engineer": {"required": False},
        }


class WorkflowContentSerializer(serializers.ModelSerializer):
    workflow = WorkflowSerializer()

    def create(self, validated_data):
        """使用原工单submit流程创建工单"""
        workflow_data = validated_data.pop("workflow")
        instance = workflow_data["instance"]
        sql_content = validated_data["sql_content"].strip()
        group = ResourceGroup.objects.get(pk=workflow_data["group_id"])
        engineer = workflow_data.get("engineer")

        # 管理员可以指定提交人信息
        if self.context["request"].user.is_superuser and engineer:
            try:
                user = Users.objects.get(username=engineer)
            except Users.DoesNotExist:
                raise serializers.ValidationError({"errors": f"不存在用户：{engineer}"})
        # 提交人只能是自己
        else:
            user = self.context["request"].user

        # 资源组归属校验：非超管只能选择自己所在的资源组；实例必须属于
        # 所选资源组，避免把工单挂到别的组导致审批流错配。
        if not self.context["request"].user.is_superuser:
            user_group_ids = {g.group_id for g in user_groups(user)}
            if group.group_id not in user_group_ids:
                raise serializers.ValidationError({"errors": "你不在所选资源组内！"})
        if not instance.resource_group.filter(group_id=group.group_id).exists():
            raise serializers.ValidationError({"errors": "所选实例不属于该资源组！"})

        # 验证提交用户的组权限（用户是否在该组、该组是否有指定实例）
        tag_codes = (
            ["can_read"] if workflow_data["is_offline_export"] else ["can_write"]
        )
        try:
            user_instances(user, tag_codes=tag_codes).get(id=instance.id)
        except instance.DoesNotExist:
            raise serializers.ValidationError({"errors": "你所在组未关联该实例！"})

        # 再次交给engine进行检测，防止绕过
        try:
            check_engine = get_engine(instance=instance)
            sql_export = OffLineDownLoad()
            if workflow_data["is_offline_export"]:
                instance.sql_content = sql_content
                instance.selected_db_name = workflow_data["db_name"]
                check_result = sql_export.pre_count_check(workflow=instance)
            else:
                # AI 风险审核支持 mysql/pgsql/mongo（execute_check 带 run_ai_review 参数）；
                # 其他引擎传了会 TypeError，按签名动态传入
                check_kwargs = {
                    "db_name": workflow_data["db_name"],
                    "sql": sql_content,
                }
                if (
                    "run_ai_review"
                    in inspect.signature(check_engine.execute_check).parameters
                ):
                    check_kwargs["run_ai_review"] = False
                check_result = check_engine.execute_check(**check_kwargs)
        except Exception as e:
            raise serializers.ValidationError({"errors": str(e)})

        # 复用检测阶段的服务端 AI 审核快照：只信任 review_id，并校验发起人/
        # 实例/库/SQL 指纹，避免客户端篡改 AI 字段或把旧结论套到新 SQL 上。
        self._apply_ai_review_record(check_result, workflow_data, instance, sql_content)

        # 未开启备份选项，并且engine支持备份，强制设置备份
        is_backup = (
            False
            if workflow_data["is_offline_export"]
            else workflow_data.get("is_backup", False)
        )
        sys_config = SysConfig()
        if not sys_config.get("enable_backup_switch") and check_engine.auto_backup:
            if not workflow_data["is_offline_export"]:
                is_backup = True

        workflow_data.update(
            status="workflow_manreviewing",
            is_backup=is_backup,
            is_manual=0,
            syntax_type=check_result.syntax_type,
            engineer=user.username,
            engineer_display=user.display,
            group_name=group.group_name,
            audit_auth_groups="",
        )
        try:
            with transaction.atomic():
                workflow = SqlWorkflow(**workflow_data)
                validated_data["review_content"] = check_result.json()
                workflow.save()
                workflow_content = SqlWorkflowContent.objects.create(
                    workflow=workflow, **validated_data
                )
                # 自动创建工作流
                auditor = get_auditor(workflow=workflow)
                auditor.create_audit()
        except Exception as e:
            logger.error(f"提交工单报错，错误信息：{traceback.format_exc()}")
            raise serializers.ValidationError({"errors": str(e)})
        # 有时候提交后自动审批通过, 在这里改写一下 workflow 状态
        if auditor.audit.current_status == WorkflowStatus.REJECTED:
            auditor.workflow.status = "workflow_autoreviewwrong"
        elif auditor.audit.current_status == WorkflowStatus.PASSED:
            auditor.workflow.status = "workflow_review_pass"
        auditor.workflow.save()
        return workflow_content

    def _apply_ai_review_record(
        self, check_result, workflow_data, instance, sql_content
    ):
        """用服务端 AI 审核快照回填本次检测结果。

        检测接口已把 AI 结果落到 AIReviewRecord；提交时只信任 review_id，
        并校验发起人、实例、库、SQL 指纹与有效期。客户端回传的 AI 字段
        不再采信，避免篡改风险等级或把旧结论套到变更后的 SQL 上。
        """
        review_id = self.context["request"].data.get("ai_review_id")
        if not review_id:
            return
        request_user = self.context["request"].user
        try:
            record = AIReviewRecord.objects.get(token=review_id)
        except (AIReviewRecord.DoesNotExist, ValidationError, ValueError, TypeError):
            raise serializers.ValidationError(
                {"errors": "AI 审核结果无效，请重新检测后再提交"}
            )
        if record.user_id != request_user.id and not request_user.is_superuser:
            raise serializers.ValidationError({"errors": "无权使用该 AI 审核结果"})
        if record.instance_id != instance.id or (record.db_name or "") != (
            workflow_data.get("db_name") or ""
        ):
            raise serializers.ValidationError(
                {"errors": "AI 审核结果与当前实例/库不一致，请重新检测"}
            )
        if record.sql_hash != sql_fingerprint(sql_content):
            raise serializers.ValidationError(
                {"errors": "SQL 内容已变更，请重新检测后再提交"}
            )
        if record.created_at < timezone.now() - timedelta(hours=AI_REVIEW_TTL_HOURS):
            raise serializers.ValidationError(
                {"errors": "AI 审核结果已过期，请重新检测后再提交"}
            )

        try:
            ai_rows = json.loads(record.result_json or "[]")
        except (ValueError, TypeError):
            ai_rows = []
        ai_by_hash = {}
        for item in ai_rows:
            if isinstance(item, dict) and item.get("sql_hash"):
                ai_by_hash[item["sql_hash"]] = item
        if not ai_by_hash:
            return

        for row in check_result.rows:
            if isinstance(row, dict):
                sql = row.get("sql") or ""
            else:
                sql = getattr(row, "sql", "") or ""
            item = ai_by_hash.get(sql_fingerprint(sql))
            if not item:
                continue
            for field in AI_REVIEW_FIELDS:
                if field not in item:
                    continue
                if isinstance(row, dict):
                    row[field] = item[field]
                else:
                    setattr(row, field, item[field])

    class Meta:
        model = SqlWorkflowContent
        fields = (
            "id",
            "workflow_id",
            "workflow",
            "sql_content",
            "review_content",
            "execute_result",
        )
        read_only_fields = ["review_content", "execute_result"]


class AuditWorkflowSerializer(serializers.Serializer):
    engineer = serializers.CharField(label="操作用户")
    workflow_id = serializers.IntegerField(label="工单id")
    audit_remark = serializers.CharField(label="审批备注")
    workflow_type = serializers.ChoiceField(
        choices=WorkflowType.choices,
        label="工单类型：1-查询权限申请，2-SQL上线申请，3-数据归档申请",
    )
    audit_type = serializers.ChoiceField(choices=["pass", "cancel"], label="审核类型")

    def validate(self, attrs):
        engineer = attrs.get("engineer")
        workflow_id = attrs.get("workflow_id")
        workflow_type = attrs.get("workflow_type")

        try:
            Users.objects.get(username=engineer)
        except Users.DoesNotExist:
            raise serializers.ValidationError({"errors": f"不存在该用户：{engineer}"})

        try:
            WorkflowAudit.objects.get(
                workflow_id=workflow_id, workflow_type=workflow_type
            )
        except WorkflowAudit.DoesNotExist:
            raise serializers.ValidationError({"errors": "不存在该工单"})

        return attrs


class WorkflowAuditSerializer(serializers.Serializer):
    engineer = serializers.CharField(label="操作用户")

    def validate_engineer(self, engineer):
        try:
            Users.objects.get(username=engineer)
        except Users.DoesNotExist:
            raise serializers.ValidationError({"errors": f"不存在该用户：{engineer}"})
        return engineer


class WorkflowAuditListSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowAudit
        exclude = [
            "group_id",
            "workflow_remark",
            "next_audit",
            "create_user",
            "sys_time",
        ]


class WorkflowLogSerializer(serializers.Serializer):
    workflow_id = serializers.IntegerField(label="工单id")
    workflow_type = serializers.ChoiceField(
        choices=[1, 2, 3],
        label="工单类型：1-查询权限申请，2-SQL上线申请，3-数据归档申请",
    )

    def validate(self, attrs):
        workflow_id = attrs.get("workflow_id")
        workflow_type = attrs.get("workflow_type")

        try:
            WorkflowAudit.objects.get(
                workflow_id=workflow_id, workflow_type=workflow_type
            )
        except WorkflowAudit.DoesNotExist:
            raise serializers.ValidationError({"errors": "不存在该工单"})

        return attrs


class WorkflowLogListSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowLog
        fields = [
            "operation_type_desc",
            "operation_info",
            "operator_display",
            "operation_time",
        ]


class ExecuteWorkflowSerializer(serializers.Serializer):
    engineer = serializers.CharField(required=False, label="操作用户")
    workflow_id = serializers.IntegerField(label="工单id")
    workflow_type = serializers.ChoiceField(
        choices=[2, 3], label="工单类型：1-查询权限申请，2-SQL上线申请，3-数据归档申请"
    )
    mode = serializers.ChoiceField(
        choices=["auto", "manual"],
        label="执行模式：auto-线上执行，manual-已手动执行",
        required=False,
    )

    def validate(self, attrs):
        engineer = attrs.get("engineer")
        workflow_id = attrs.get("workflow_id")
        workflow_type = attrs.get("workflow_type")
        mode = attrs.get("mode")

        # SQL上线工单的mode和engineer为必需字段
        if workflow_type == 2:
            if not mode:
                raise serializers.ValidationError({"errors": "缺少 mode"})
            if not engineer:
                raise serializers.ValidationError({"errors": "缺少 engineer"})

            try:
                Users.objects.get(username=engineer)
            except Users.DoesNotExist:
                raise serializers.ValidationError(
                    {"errors": f"不存在该用户：{engineer}"}
                )

        try:
            WorkflowAudit.objects.get(
                workflow_id=workflow_id, workflow_type=workflow_type
            )
        except WorkflowAudit.DoesNotExist:
            raise serializers.ValidationError({"errors": "不存在该工单"})

        return attrs
