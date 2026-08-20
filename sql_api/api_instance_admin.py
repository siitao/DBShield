"""
实例管理 DRF APIView 集 · 替代 sql/instance_account.py、sql/instance_database.py、
sql/instance.py 中的 param_* 函数，共 15 个端点。

路由：
  POST /api/v1/instance/accounts/            — 用户列表
  POST /api/v1/instance/accounts/create/     — 创建账号
  POST /api/v1/instance/accounts/edit/       — 修改/录入账号
  POST /api/v1/instance/accounts/grant/      — 授权/回收
  POST /api/v1/instance/accounts/reset_pwd/ — 重置密码
  POST /api/v1/instance/accounts/lock/       — 锁定/解锁
  POST /api/v1/instance/accounts/delete/     — 删除账号

  POST /api/v1/instance/databases/           — 数据库列表
  POST /api/v1/instance/databases/create/     — 创建数据库
  POST /api/v1/instance/databases/edit/       — 编辑/录入数据库

  POST /api/v1/instance/params/              — 参数列表
  POST /api/v1/instance/params/history/       — 参数修改历史
  POST /api/v1/instance/params/edit/          — 修改参数
  POST /api/v1/instance/params/compare/       — 参数对比
"""
import json as _json
import re
import time

import MySQLdb
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django_redis import get_redis_connection
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.views import APIView

from common.utils.extend_json_encoder import encode_json
from sql.engines import get_engine, ResultSet
from sql.models import (
    Instance,
    InstanceAccount,
    InstanceDatabase,
    ParamHistory,
    ParamTemplate,
    Users,
)
from sql.plugins.schemasync import SchemaSync
from sql.services.instance_service import resolve_instance
from sql.utils.instance_management import (
    SUPPORTED_MANAGEMENT_DB_TYPE,
    get_instanceaccount_unique_key,
    get_instanceaccount_unique_value,
)
from sql.utils.resource_group import user_instances

import logging

logger = logging.getLogger("default")


# ---------- shared helpers ----------

# GRANT/REVOKE 权限名白名单（MySQL 5.7/8.0 静态权限），防止权限名拼接注入
MYSQL_PRIVILEGE_WHITELIST = {
    "ALL", "ALL PRIVILEGES", "ALTER", "ALTER ROUTINE", "CREATE",
    "CREATE ROUTINE", "CREATE TABLESPACE", "CREATE TEMPORARY TABLES",
    "CREATE USER", "CREATE VIEW", "DELETE", "DROP", "EVENT", "EXECUTE",
    "FILE", "GRANT OPTION", "INDEX", "INSERT", "LOCK TABLES", "PROCESS",
    "PROXY", "REFERENCES", "RELOAD", "REPLICATION CLIENT",
    "REPLICATION SLAVE", "SELECT", "SHOW DATABASES", "SHOW VIEW",
    "SHUTDOWN", "SUPER", "TRIGGER", "UPDATE", "USAGE",
}


def _sanitize_privs(privs):
    """清洗权限名列表：统一大小写、GRANT→GRANT OPTION，白名单外直接拒绝"""
    cleaned = []
    for p in privs:
        p_up = str(p).strip().upper()
        if p_up == "GRANT":
            p_up = "GRANT OPTION"
        if p_up not in MYSQL_PRIVILEGE_WHITELIST:
            raise ValueError(f"不支持的权限名：{p}")
        cleaned.append(p_up)
    return cleaned


def _quote_ident(name):
    """GRANT/DDL 标识符（库/表/列名）白名单校验 + 反引号包裹，防标识符逃逸"""
    name = str(name).strip()
    if not re.fullmatch(r"[\w$]+", name):
        raise ValueError("库名/表名/列名仅允许字母、数字、下划线")
    return f"`{name}`"


def _get_instance(request):
    """从 request.data 获取 instance_id，鉴权后返回 Instance。"""
    instance_id = request.data.get("instance_id", 0)
    try:
        return resolve_instance(
            request.user, instance_id=instance_id, db_type=SUPPORTED_MANAGEMENT_DB_TYPE
        )
    except Instance.DoesNotExist:
        return None


def _get_instance_mysql_mongo(request):
    """获取 mysql/mongo 实例（数据库管理用）。"""
    instance_id = request.data.get("instance_id", 0)
    try:
        return resolve_instance(
            request.user, instance_id=instance_id, db_type=["mysql", "mongo"]
        )
    except Instance.DoesNotExist:
        return None


# ---------- permissions ----------

class AccountPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (
            u.is_superuser or u.has_perm("sql.menu_instance_account")
        )


class AccountManagePermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (
            u.is_superuser or u.has_perm("sql.instance_account_manage")
        )


class DatabasePermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (
            u.is_superuser or u.has_perm("sql.menu_database")
        )


class ParamViewPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (
            u.is_superuser or u.has_perm("sql.param_view")
        )


class ParamEditPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (
            u.is_superuser or u.has_perm("sql.param_edit")
        )


# ========== 账号管理 ==========


class AccountListView(APIView):
    permission_classes = [IsAuthenticated, AccountPermission]

    def post(self, request):
        instance_id = request.data.get("instance_id")
        saved = request.data.get("saved") == "true"

        if not instance_id:
            return JsonResponse({"status": 0, "msg": "", "data": []})

        instance = _get_instance(request)
        if instance is None:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        cnf_users = {}
        for user in InstanceAccount.objects.filter(instance=instance).values(
            "id", "user", "host", "db_name", "remark"
        ):
            user["saved"] = True
            cnf_users[get_instanceaccount_unique_value(instance.db_type, user)] = user

        query_engine = get_engine(instance=instance)
        query_result = query_engine.get_instance_users_summary()
        if not query_result.error:
            rows = []
            key = get_instanceaccount_unique_key(db_type=instance.db_type)
            for row in query_result.rows:
                if row[key] in cnf_users:
                    row = dict(row, **cnf_users[row[key]])
                rows.append(row)
            if saved:
                rows = [row for row in rows if row["saved"]]
            result = {"status": 0, "msg": "ok", "rows": rows}
        else:
            result = {"status": 1, "msg": query_result.error}

        query_engine.close()
        return JsonResponse(encode_json(result), safe=False)


class AccountCreateView(APIView):
    permission_classes = [IsAuthenticated, AccountManagePermission]

    def post(self, request):
        instance = _get_instance(request)
        if instance is None:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        db_name = request.data.get("db_name")
        user = request.data.get("user")
        host = request.data.get("host")
        password1 = request.data.get("password1")
        password2 = request.data.get("password2")
        remark = request.data.get("remark", "")

        if (
            instance.db_type == "mysql" and not all([user, host, password1, password2])
        ) or (
            instance.db_type == "mongo"
            and not all([db_name, user, password1, password2])
        ):
            return JsonResponse({"status": 1, "msg": "参数不完整，请确认后提交", "data": []})

        if password1 != password2:
            return JsonResponse({"status": 1, "msg": "两次输入密码不一致", "data": []})

        try:
            validate_password(password1, user=None, password_validators=None)
        except ValidationError as msg:
            return JsonResponse({"status": 1, "msg": str(msg), "data": []})

        engine = get_engine(instance=instance)
        exec_result = engine.create_instance_user(
            db_name=db_name, user=user, host=host, password1=password1, remark=remark
        )
        engine.close()
        if exec_result.error:
            return JsonResponse({"status": 1, "msg": exec_result.error})

        accounts = [InstanceAccount(**row) for row in exec_result.rows]
        InstanceAccount.objects.bulk_create(accounts)
        return JsonResponse({"status": 0, "msg": "", "data": []})


class AccountEditView(APIView):
    permission_classes = [IsAuthenticated, AccountManagePermission]

    def post(self, request):
        instance = _get_instance(request)
        if instance is None:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        db_name = request.data.get("db_name", "")
        user = request.data.get("user")
        host = request.data.get("host", "")
        password = request.data.get("password")
        remark = request.data.get("remark", "")

        if (instance.db_type == "mysql" and not all([user, host])) or (
            instance.db_type == "mongo" and not all([db_name, user])
        ):
            return JsonResponse({"status": 1, "msg": "参数不完整，请确认后提交", "data": []})

        defaults = {"remark": remark}
        if password:
            defaults["password"] = password
        InstanceAccount.objects.update_or_create(
            instance=instance, user=user, host=host, db_name=db_name, defaults=defaults,
        )
        return JsonResponse({"status": 0, "msg": "", "data": []})


class AccountGrantView(APIView):
    permission_classes = [IsAuthenticated, AccountManagePermission]

    def post(self, request):
        instance = _get_instance(request)
        if instance is None:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        grant_sql = ""
        engine = get_engine(instance=instance)

        if instance.db_type == "mysql":
            user_host = request.data.get("user_host")
            op_type = int(request.data.get("op_type"))
            priv_type = int(request.data.get("priv_type"))
            privs_raw = request.data.get("privs")
            if isinstance(privs_raw, str):
                privs = _json.loads(privs_raw)
            else:
                privs = privs_raw if isinstance(privs_raw, list) else []

            def _extract(key):
                if isinstance(privs, dict):
                    return privs.get(key, [])
                return privs

            def _getlist(key):
                # request.data 可能是 QueryDict（form 表单）或普通 dict（JSON）：
                # form 用 getlist 取重复键（db_name[]=a&db_name[]=b）；
                # JSON 下 get 到的值可能是 list 或 JSON 字符串。两者都要兼容，
                # 否则 JSON 请求时 dict 无 getlist 直接 AttributeError。
                data = request.data
                if hasattr(data, "getlist"):
                    val = data.getlist(key)
                    if val and val[0]:
                        return val
                key2 = key.rstrip("[]")
                raw = data.get(key)
                if raw is None or raw == "":
                    raw = data.get(key2)
                if raw is None or raw == "":
                    return []
                if isinstance(raw, list):
                    return [v for v in raw if v]
                if isinstance(raw, str):
                    try:
                        parsed = _json.loads(raw)
                        if isinstance(parsed, list):
                            return [str(v) for v in parsed if v]
                    except (_json.JSONDecodeError, TypeError):
                        pass
                    return [raw]
                return [str(raw)]

            def _get(key):
                val = request.data.get(key, "")
                # JSON 请求下值可能是 list（如 db_name: ["a"]），取首个
                if isinstance(val, list):
                    return val[0] if val else ""
                if isinstance(val, str) and val.startswith("["):
                    try:
                        parsed = _json.loads(val)
                        if isinstance(parsed, list) and parsed:
                            return parsed[0]
                    except (_json.JSONDecodeError, TypeError):
                        pass
                return val

            user_host = engine.escape_string(user_host)

            try:
                if priv_type == 0:
                    global_privs = _sanitize_privs(_extract("global_privs"))
                    if not global_privs:
                        return JsonResponse({"status": 1, "msg": "信息不完整，请确认后提交", "data": []})
                    if op_type == 0:
                        grant_sql = f"GRANT {','.join(global_privs)} ON *.* TO {user_host};"
                    elif op_type == 1:
                        grant_sql = f"REVOKE {','.join(global_privs)} ON *.* FROM {user_host};"

                elif priv_type == 1:
                    db_privs = _sanitize_privs(_extract("db_privs"))
                    db_names = _getlist("db_name[]")
                    if not db_privs or not db_names:
                        return JsonResponse({"status": 1, "msg": "信息不完整，请确认后提交", "data": []})
                    for db in db_names:
                        if op_type == 0:
                            grant_sql += f"GRANT {','.join(db_privs)} ON {_quote_ident(db)}.* TO {user_host};"
                        elif op_type == 1:
                            grant_sql += f"REVOKE {','.join(db_privs)} ON {_quote_ident(db)}.* FROM {user_host};"

                elif priv_type == 2:
                    tb_privs = _sanitize_privs(_extract("tb_privs"))
                    db_name = _get("db_name")
                    tb_names = _getlist("tb_name[]")
                    if not tb_privs or not db_name or not tb_names:
                        return JsonResponse({"status": 1, "msg": "信息不完整，请确认后提交", "data": []})
                    for tb in tb_names:
                        if op_type == 0:
                            grant_sql += (
                                f"GRANT {','.join(tb_privs)} ON "
                                f"{_quote_ident(db_name)}.{_quote_ident(tb)} TO {user_host};"
                            )
                        elif op_type == 1:
                            grant_sql += (
                                f"REVOKE {','.join(tb_privs)} ON "
                                f"{_quote_ident(db_name)}.{_quote_ident(tb)} FROM {user_host};"
                            )

                elif priv_type == 3:
                    col_privs = _sanitize_privs(_extract("col_privs"))
                    db_name = _get("db_name")
                    tb_name = _get("tb_name")
                    col_names = _getlist("col_name[]")
                    if not col_privs or not db_name or not tb_name or not col_names:
                        return JsonResponse({"status": 1, "msg": "信息不完整，请确认后提交", "data": []})
                    for priv in col_privs:
                        quoted_cols = ",".join(_quote_ident(c) for c in col_names)
                        if op_type == 0:
                            grant_sql += (
                                f"GRANT {priv}({quoted_cols}) "
                                f"ON {_quote_ident(db_name)}.{_quote_ident(tb_name)} TO {user_host};"
                            )
                        elif op_type == 1:
                            grant_sql += (
                                f"REVOKE {priv}({quoted_cols}) "
                                f"ON {_quote_ident(db_name)}.{_quote_ident(tb_name)} FROM {user_host};"
                            )
            except ValueError as e:
                return JsonResponse({"status": 1, "msg": str(e), "data": []})
            exec_result = engine.execute(db_name="mysql", sql=grant_sql)

        elif instance.db_type == "mongo":
            db_name_user = request.data.get("db_name_user")
            roles = request.data.getlist("roles[]")
            arr = db_name_user.split(".")
            db_name = arr[0]
            user = arr[1]
            exec_result = ResultSet()
            try:
                conn = engine.get_connection()
                conn[db_name].command("updateUser", user, roles=roles)
            except Exception as e:
                exec_result.error = str(e)

        engine.close()
        if exec_result.error:
            return JsonResponse({"status": 1, "msg": exec_result.error})
        return JsonResponse({"status": 0, "msg": "", "data": grant_sql})


class AccountResetPwdView(APIView):
    permission_classes = [IsAuthenticated, AccountManagePermission]

    def post(self, request):
        instance = _get_instance(request)
        if instance is None:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        db_name_user = request.data.get("db_name_user")
        db_name = request.data.get("db_name", "")
        user_host = request.data.get("user_host")
        user = request.data.get("user")
        host = request.data.get("host", "")
        reset_pwd1 = request.data.get("reset_pwd1")
        reset_pwd2 = request.data.get("reset_pwd2")

        if (
            instance.db_type == "mysql"
            and not all([user, host, reset_pwd1, reset_pwd2])
        ) or (
            instance.db_type == "mongo"
            and not all([db_name, user, reset_pwd1, reset_pwd2])
        ):
            return JsonResponse({"status": 1, "msg": "参数不完整，请确认后提交", "data": []})

        if reset_pwd1 != reset_pwd2:
            return JsonResponse({"status": 1, "msg": "两次输入密码不一致", "data": []})

        try:
            validate_password(reset_pwd1, user=None, password_validators=None)
        except ValidationError as msg:
            return JsonResponse({"status": 1, "msg": str(msg), "data": []})

        engine = get_engine(instance=instance)
        exec_result = engine.reset_instance_user_pwd(
            user_host=user_host, db_name_user=db_name_user, reset_pwd=reset_pwd1
        )
        engine.close()
        if exec_result.error:
            return JsonResponse({"status": 1, "msg": exec_result.error})

        InstanceAccount.objects.update_or_create(
            instance=instance,
            user=user,
            host=host,
            db_name=db_name,
            defaults={"password": reset_pwd1},
        )
        return JsonResponse({"status": 0, "msg": "", "data": []})


class AccountLockView(APIView):
    permission_classes = [IsAuthenticated, AccountManagePermission]

    def post(self, request):
        user_host = request.data.get("user_host")
        is_locked = request.data.get("is_locked")

        if not user_host:
            return JsonResponse({"status": 1, "msg": "参数不完整，请确认后提交", "data": []})

        instance_id = request.data.get("instance_id", 0)
        try:
            instance = user_instances(request.user, db_type=["mysql"]).get(
                id=instance_id
            )
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        engine = get_engine(instance=instance)
        user_host = engine.escape_string(user_host)

        if is_locked == "N":
            lock_sql = f"ALTER USER {user_host} ACCOUNT LOCK;"
        elif is_locked == "Y":
            lock_sql = f"ALTER USER {user_host} ACCOUNT UNLOCK;"
        else:
            lock_sql = ""

        exec_result = engine.execute(db_name="mysql", sql=lock_sql)
        if exec_result.error:
            return JsonResponse({"status": 1, "msg": exec_result.error})
        return JsonResponse({"status": 0, "msg": "", "data": []})


class AccountDeleteView(APIView):
    permission_classes = [IsAuthenticated, AccountManagePermission]

    def post(self, request):
        instance = _get_instance(request)
        if instance is None:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        db_name_user = request.data.get("db_name_user")
        db_name = request.data.get("db_name")
        user_host = request.data.get("user_host")
        user = request.data.get("user")
        host = request.data.get("host")

        if (instance.db_type == "mysql" and not user_host) or (
            instance.db_type == "mongo" and not db_name_user
        ):
            return JsonResponse({"status": 1, "msg": "参数不完整，请确认后提交", "data": []})

        engine = get_engine(instance=instance)
        exec_result = engine.drop_instance_user(
            user_host=user_host, db_name_user=db_name_user
        )
        engine.close()
        if exec_result.error:
            return JsonResponse({"status": 1, "msg": exec_result.error})

        InstanceAccount.objects.filter(
            instance=instance, user=user, host=host, db_name=db_name
        ).delete()
        return JsonResponse({"status": 0, "msg": "", "data": []})


# ========== 数据库管理 ==========


class DatabaseListView(APIView):
    permission_classes = [IsAuthenticated, DatabasePermission]

    def post(self, request):
        instance_id = request.data.get("instance_id")
        saved = request.data.get("saved") == "true"

        if not instance_id:
            return JsonResponse({"status": 0, "msg": "", "data": []})

        instance = _get_instance_mysql_mongo(request)
        if instance is None:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        cnf_dbs = {}
        for db in InstanceDatabase.objects.filter(instance=instance).values(
            "id", "db_name", "owner", "owner_display", "remark"
        ):
            db["saved"] = True
            cnf_dbs[db["db_name"]] = db

        query_engine = get_engine(instance=instance)
        query_result = query_engine.get_all_databases_summary()
        if not query_result.error:
            rows = []
            for row in query_result.rows:
                if row["db_name"] in cnf_dbs:
                    row = dict(row, **cnf_dbs[row["db_name"]])
                rows.append(row)
            if saved:
                rows = [row for row in rows if row.get("saved")]
            result = {"status": 0, "msg": "ok", "rows": rows}
        else:
            result = {"status": 1, "msg": query_result.error}

        query_engine.close()
        return JsonResponse(encode_json(result), safe=False)


class DatabaseCreateView(APIView):
    permission_classes = [IsAuthenticated, DatabasePermission]

    def post(self, request):
        db_name = request.data.get("db_name")
        owner = request.data.get("owner", "")
        remark = request.data.get("remark", "")

        if not db_name:
            return JsonResponse({"status": 1, "msg": "参数不完整，请确认后提交", "data": []})

        instance = _get_instance_mysql_mongo(request)
        if instance is None:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        try:
            owner_display = Users.objects.get(username=owner).display
        except Users.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "负责人不存在", "data": []})

        engine = get_engine(instance=instance)
        if instance.db_type == "mysql":
            # 标识符白名单 + 反引号包裹（escape_string 不转义反引号，无法防标识符逃逸）
            try:
                quoted_db = _quote_ident(db_name)
            except ValueError as e:
                return JsonResponse({"status": 1, "msg": str(e), "data": []})
            exec_result = engine.execute(
                db_name="information_schema", sql=f"create database {quoted_db};"
            )
        elif instance.db_type == "mongo":
            exec_result = ResultSet()
            try:
                conn = engine.get_connection()
                db = conn[db_name]
                db.create_collection(name=f"dbshield-{db_name}")
            except Exception as e:
                exec_result.error = f"创建数据库失败, 错误信息：{str(e)}"

        engine.close()
        if exec_result.error:
            return JsonResponse({"status": 1, "msg": exec_result.error})

        InstanceDatabase.objects.create(
            instance=instance,
            db_name=db_name,
            owner=owner,
            owner_display=owner_display,
            remark=remark,
        )
        r = get_redis_connection("default")
        for key in r.scan_iter(match="*insRes*", count=2000):
            r.delete(key)

        return JsonResponse({"status": 0, "msg": "", "data": []})


class DatabaseEditView(APIView):
    permission_classes = [IsAuthenticated, DatabasePermission]

    def post(self, request):
        db_name = request.data.get("db_name")
        owner = request.data.get("owner", "")
        remark = request.data.get("remark", "")

        if not db_name:
            return JsonResponse({"status": 1, "msg": "参数不完整，请确认后提交", "data": []})

        instance = _get_instance_mysql_mongo(request)
        if instance is None:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        try:
            owner_display = Users.objects.get(username=owner).display
        except Users.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "负责人不存在", "data": []})

        InstanceDatabase.objects.update_or_create(
            instance=instance,
            db_name=db_name,
            defaults={"owner": owner, "owner_display": owner_display, "remark": remark},
        )
        return JsonResponse({"status": 0, "msg": "", "data": []})


# ========== 参数管理 ==========


class ParamListView(APIView):
    permission_classes = [IsAuthenticated, ParamViewPermission]

    def post(self, request):
        instance_id = request.data.get("instance_id")
        editable = bool(request.data.get("editable"))
        search = request.data.get("search", "")

        try:
            int(instance_id)
        except (TypeError, ValueError):
            return JsonResponse({"status": 1, "msg": "实例ID不合法", "data": []})

        # 资源组归属校验：防止跨组读取/修改任意实例参数
        try:
            ins = resolve_instance(request.user, instance_id=instance_id)
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "实例不存在或你所在组未关联该实例", "data": []})

        cnf_params = {}
        for param in ParamTemplate.objects.filter(
            db_type=ins.db_type, variable_name__contains=search
        ).values(
            "id", "variable_name", "default_value", "valid_values",
            "description", "editable",
        ):
            param["variable_name"] = param["variable_name"].lower()
            cnf_params[param["variable_name"]] = param

        engine = get_engine(instance=ins)
        ins_variables = engine.get_variables()
        rows = []
        for variable in ins_variables.rows:
            vname = variable[0].lower()
            row = {"variable_name": vname, "runtime_value": variable[1], "editable": False}
            if vname in cnf_params:
                row = dict(row, **cnf_params[vname])
            rows.append(row)

        if editable:
            rows = [r for r in rows if r["editable"]]
        else:
            rows = [r for r in rows if not r["editable"]]

        return JsonResponse(
            encode_json(rows),
            safe=False,
        )


class ParamHistoryView(APIView):
    permission_classes = [IsAuthenticated, ParamViewPermission]

    def post(self, request):
        try:
            limit = int(request.data.get("limit", 0))
            offset = int(request.data.get("offset", 0))
        except (TypeError, ValueError):
            return JsonResponse({"status": 1, "msg": "分页参数不合法", "data": []})
        limit = offset + limit
        instance_id = request.data.get("instance_id")
        search = request.data.get("search", "")

        # 资源组归属校验：仅可查本组实例的参数变更历史
        try:
            ins = resolve_instance(request.user, instance_id=instance_id)
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "实例不存在或你所在组未关联该实例", "data": []})

        phs = ParamHistory.objects.filter(instance__id=ins.id)
        if search:
            phs = phs.filter(variable_name__contains=search)

        count = phs.count()
        phs = phs[offset:limit].values(
            "instance__instance_name", "variable_name", "old_var", "new_var",
            "user_display", "create_time",
        )
        rows = [row for row in phs]

        return JsonResponse(
            encode_json(
                {"total": count, "rows": rows}
            ),
            safe=False,
        )


class ParamEditView(APIView):
    permission_classes = [IsAuthenticated, ParamEditPermission]

    def post(self, request):
        user = request.user
        instance_id = request.data.get("instance_id")
        variable_name = request.data.get("variable_name")
        variable_value = request.data.get("runtime_value")

        try:
            int(instance_id)
        except (TypeError, ValueError):
            return JsonResponse({"status": 1, "msg": "实例ID不合法", "data": []})

        # 资源组归属校验：SET GLOBAL 属实例级变更，仅限本组实例
        try:
            ins = resolve_instance(request.user, instance_id=instance_id)
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "实例不存在或你所在组未关联该实例", "data": []})

        engine = get_engine(instance=ins)
        variable_name = engine.escape_string(variable_name)
        variable_value = engine.escape_string(variable_value)

        if not ParamTemplate.objects.filter(variable_name=variable_name).exists():
            return JsonResponse({"status": 1, "msg": "请先在参数模板中配置该参数！", "data": []})

        runtime_value = engine.get_variables(variables=[variable_name]).rows[0][1]
        if variable_value == runtime_value:
            return JsonResponse({"status": 1, "msg": "参数值与实际运行值一致，未调整！", "data": []})

        set_result = engine.set_variable(
            variable_name=variable_name, variable_value=variable_value
        )
        if set_result.error:
            return JsonResponse({
                "status": 1,
                "msg": f"设置错误，错误信息：{set_result.error}",
                "data": [],
            })

        ParamHistory.objects.create(
            instance=ins,
            variable_name=variable_name,
            old_var=runtime_value,
            new_var=variable_value,
            set_sql=set_result.full_sql,
            user_name=user.username,
            user_display=user.display,
        )
        return JsonResponse({"status": 0, "msg": "修改成功，请手动持久化到配置文件！", "data": []})


class ParamCompareView(APIView):
    permission_classes = [IsAuthenticated, ParamViewPermission]

    def post(self, request):
        instance_id1 = request.data.get("instance_id1")
        instance_id2 = request.data.get("instance_id2")
        diff_only = request.data.get("diff_only", "true") == "true"

        for iid, label in [(instance_id1, "源实例ID"), (instance_id2, "目标实例ID")]:
            try:
                int(iid)
            except (TypeError, ValueError):
                return JsonResponse({"status": 1, "msg": f"{label}不合法", "data": []})

        # 资源组归属校验：两侧实例均须在当前用户可访问组内
        try:
            ins1 = resolve_instance(request.user, instance_id=instance_id1)
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "源实例不存在或你所在组未关联该实例", "data": []})
        try:
            ins2 = resolve_instance(request.user, instance_id=instance_id2)
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "目标实例不存在或你所在组未关联该实例", "data": []})

        if ins1.db_type != ins2.db_type:
            return JsonResponse({"status": 1, "msg": "两个实例的数据库类型不一致，无法对比", "data": []})

        if ins1.db_type not in {"mysql", "goinception", "pgsql"}:
            return JsonResponse({
                "status": 1,
                "msg": f"{ins1.db_type} 引擎不支持参数对比功能",
                "data": [],
            })

        engine1 = get_engine(instance=ins1)
        variables1 = engine1.get_variables()
        if variables1.error:
            return JsonResponse({
                "status": 1,
                "msg": "获取源实例参数失败，请联系管理员",
                "data": [],
            })

        engine2 = get_engine(instance=ins2)
        variables2 = engine2.get_variables()
        if variables2.error:
            return JsonResponse({
                "status": 1,
                "msg": "获取目标实例参数失败，请联系管理员",
                "data": [],
            })

        vars1 = {str(v[0]).lower(): str(v[1]) for v in variables1.rows}
        vars2 = {str(v[0]).lower(): str(v[1]) for v in variables2.rows}

        cnf_params = {}
        for param in ParamTemplate.objects.filter(db_type=ins1.db_type).values(
            "variable_name", "default_value", "description"
        ):
            cnf_params[param["variable_name"].lower()] = param

        all_keys = sorted(set(vars1.keys()) | set(vars2.keys()))
        rows = []
        same_count = 0
        diff_count = 0

        for key in all_keys:
            val1 = vars1.get(key)
            val2 = vars2.get(key)
            if val1 is not None and val2 is not None:
                if val1 == val2:
                    diff_type = "一致"
                    same_count += 1
                else:
                    diff_type = "值不同"
                    diff_count += 1
            elif val1 is not None:
                diff_type = "仅源实例存在"
                diff_count += 1
            else:
                diff_type = "仅目标实例存在"
                diff_count += 1

            if diff_only and diff_type == "一致":
                continue

            row = {
                "variable_name": key,
                "instance1_value": val1 if val1 is not None else "-",
                "instance2_value": val2 if val2 is not None else "-",
                "diff_type": diff_type,
                "description": "",
                "default_value": "",
            }
            if key in cnf_params:
                row["description"] = cnf_params[key].get("description", "")
                row["default_value"] = cnf_params[key].get("default_value", "")
            rows.append(row)

        return JsonResponse({
            "status": 0,
            "msg": "ok",
            "data": {
                "rows": rows,
                "total": len(all_keys),
                "same_count": same_count,
                "diff_count": diff_count,
                "instance1_name": ins1.instance_name,
                "instance2_name": ins2.instance_name,
            },
        })
