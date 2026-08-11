# -*- coding: UTF-8 -*-
"""认证配置的运行时重载。

外部认证（LDAP/OIDC/钉钉/CAS）深度耦合 Django 启动期配置：AUTHENTICATION_BACKENDS、
INSTALLED_APPS、MIDDLEWARE、URL 路由，以及 OIDC 启动时拉取 well-known、LDAP 构造
LDAPSearch 等。这些都在 settings 模块首次 import 时固化，纯改数据库不会生效。

本模块是项目中唯一的「运行时改 settings」入口，集中处理：

1. 把数据库里的认证配置（SysConfig）写回 .env 的受管标记块
   （# === auth config (managed by DBShield) === ... # === end managed ===），
   只覆盖这一段，不动用户其它手写内容。
2. 用 importlib.reload 重新执行 settings 模块，使 AUTHENTICATION_BACKENDS /
   INSTALLED_APPS / MIDDLEWARE 重新求值。
3. 清 Django 的 URL resolver 缓存，使 /oidc/、/dingding/、/cas/authenticate/
   按 settings.ENABLE_* 重新 include。
4. 任何步骤失败则回滚（还原 .env 备份、还原 settings 模块），保证已登录会话不受影响。

并发保护：模块级 threading.Lock 串行化 reload，避免并发请求同时触发。
"""

import importlib
import logging
import os
import threading
from io import StringIO

from django.conf import settings

from common.config import SysConfig

logger = logging.getLogger("default")

# .env 受管标记块边界
_ENV_MANAGED_BEGIN = "# === auth config (managed by DBShield) ==="
_ENV_MANAGED_END = "# === end managed ==="

# os.environ 快照的「key 原本不存在」哨兵
_ENV_ABSENT = object()

# DB key -> .env 变量名 的映射（key 与 SysConfig.AUTH_PROVIDER_KEYS 对齐）
_DBKEY_TO_ENV = {
    # LDAP
    "auth_ldap_server_uri": "AUTH_LDAP_SERVER_URI",
    "auth_ldap_bind_dn": "AUTH_LDAP_BIND_DN",
    "auth_ldap_bind_password": "AUTH_LDAP_BIND_PASSWORD",
    "auth_ldap_user_dn_template": "AUTH_LDAP_USER_DN_TEMPLATE",
    "auth_ldap_user_search_base": "AUTH_LDAP_USER_SEARCH_BASE",
    "auth_ldap_user_search_filter": "AUTH_LDAP_USER_SEARCH_FILTER",
    "auth_ldap_user_attr_map": "AUTH_LDAP_USER_ATTR_MAP",
    # OIDC
    "oidc_rp_wellknown_url": "OIDC_RP_WELLKNOWN_URL",
    "oidc_rp_client_id": "OIDC_RP_CLIENT_ID",
    "oidc_rp_client_secret": "OIDC_RP_CLIENT_SECRET",
    "oidc_rp_scopes": "OIDC_RP_SCOPES",
    "oidc_rp_sign_algo": "OIDC_RP_SIGN_ALGO",
    "oidc_user_attr_map": "OIDC_USER_ATTR_MAP",
    # 钉钉
    "ding_app_key": "AUTH_DINGDING_APP_KEY",
    "ding_app_secret": "AUTH_DINGDING_APP_SECRET",
    "ding_callback_url": "AUTH_DINGDING_AUTHENTICATION_CALLBACK_URL",
    # CAS
    "cas_server_url": "CAS_SERVER_URL",
    "cas_version": "CAS_VERSION",
    "cas_verify_ssl_certificate": "CAS_VERIFY_SSL_CERTIFICATE",
}

# 并发锁：整个进程同一时刻只允许一次 reload
_RELOAD_LOCK = threading.Lock()


def _env_path():
    """返回 .env 绝对路径（与 settings.py 里 read_env 一致）。"""
    return os.path.join(settings.BASE_DIR, ".env")


def _provider_to_env_flags(provider):
    """auth_provider 值 -> {ENABLE_LDAP/ENABLE_OIDC/...} 的 bool 字典。"""
    return {
        "ENABLE_LDAP": provider == "ldap",
        "ENABLE_OIDC": provider == "oidc",
        "ENABLE_DINGDING": provider == "dingding",
        "ENABLE_CAS": provider == "cas",
    }


def _build_managed_block(auth_config):
    """根据 DB 中的认证配置，生成要写入 .env 的受管块文本（含边界标记）。"""
    buf = StringIO()
    buf.write(f"{_ENV_MANAGED_BEGIN}\n")
    buf.write("# 本段由「认证配置」页面自动维护，请勿手动编辑；手动配置请写在本段之外。\n")

    provider = auth_config.get("auth_provider", "local")
    flags = _provider_to_env_flags(provider)
    for flag, on in flags.items():
        buf.write(f"{flag}={'true' if on else 'false'}\n")

    # 写当前方式的参数（其它方式的参数不写，保持 .env 干净）
    keys_for_provider = SysConfig.AUTH_PROVIDER_KEYS.get(provider, ())
    for dbkey in keys_for_provider:
        envkey = _DBKEY_TO_ENV.get(dbkey)
        value = auth_config.get(dbkey, "")
        if not envkey or value == "":
            continue
        buf.write(f"{envkey}={value}\n")

    buf.write(f"{_ENV_MANAGED_END}\n")
    return buf.getvalue()


def _apply_to_environ(auth_config):
    """把认证相关配置写入 os.environ。

    必要性：django-environ 的 read_env(overwrite=False) 默认不覆盖已存在的
    os.environ 值。进程首次启动时 .env 的旧值已进入 os.environ，仅改 .env 文件、
    再 reload settings 模块，settings.py 里的 env("ENABLE_LDAP") 仍会读到旧值。
    因此这里显式更新 os.environ，确保 reload 后 settings 能拿到最新值。
    """
    import os as _os

    provider = auth_config.get("auth_provider", "local")
    for flag, on in _provider_to_env_flags(provider).items():
        _os.environ[flag] = "true" if on else "false"

    keys_for_provider = SysConfig.AUTH_PROVIDER_KEYS.get(provider, ())
    for dbkey in keys_for_provider:
        envkey = _DBKEY_TO_ENV.get(dbkey)
        value = auth_config.get(dbkey, "")
        if not envkey or value == "":
            # 清空：设为空串让 settings 里的 default 生效
            _os.environ.pop(envkey, None)
        else:
            _os.environ[envkey] = str(value)


def _rewrite_env(new_block):
    """把 .env 里的受管块替换为 new_block；若不存在则追加。

    返回原 .env 完整内容（用于回滚）。
    """
    path = _env_path()
    original = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            original = f.read()

    if _ENV_MANAGED_BEGIN in original and _ENV_MANAGED_END in original:
        # 替换既有受管块
        before = original.split(_ENV_MANAGED_BEGIN)[0]
        after = original.split(_ENV_MANAGED_END, 1)[1]
        new_content = before + new_block + after.lstrip("\n")
    else:
        # 追加（保证前面有空行分隔）
        sep = "\n\n" if original and not original.endswith("\n") else (
            "\n" if original else ""
        )
        new_content = original + sep + new_block

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return original


def _restore_env(original):
    """把 .env 还原为 original 内容。"""
    path = _env_path()
    with open(path, "w", encoding="utf-8") as f:
        f.write(original)


def _reload_settings_module():
    """重新执行 settings 模块，让 AUTHENTICATION_BACKENDS 等重新求值。

    Django 的 django.conf.settings 是个 lazy 包装，指向 SETTINGS_MODULE 指定的模块对象。
    reload 该模块对象后，settings 上已访问过的属性是缓存的旧值，需要清除 lazy 缓存。
    """
    settings_module_name = getattr(settings, "SETTINGS_MODULE", None) or os.environ.get(
        "DJANGO_SETTINGS_MODULE"
    )
    if not settings_module_name:
        raise RuntimeError("无法确定 DJANGO_SETTINGS_MODULE")

    module = importlib.import_module(settings_module_name)
    importlib.reload(module)

    # 清 django.conf.settings 的 lazy 属性缓存，使其重新取模块属性
    # LazyObject 内部用 _wrapped；这里走公共 API 显式 setup
    from django.conf import settings as dj_settings

    try:
        dj_settings._setup()  # 重新绑定 _wrapped 到 reload 后的模块
    except Exception:
        # 某些 Django 版本无 _setup 公开，退而求其次清 _wrapped
        dj_settings._wrapped = None


def _clear_url_caches():
    """清 URL resolver 缓存，使 dbshield/urls.py 里的 if settings.ENABLE_* 重新求值。

    Django 4.2+/5.x 用 functools.cache 包装 _get_cached_resolver，调用其
    cache_clear() 即可丢弃缓存的 URLResolver，下一次请求会重新 import urlconf，
    从而重新执行 dbshield/urls.py 顶部的 if settings.ENABLE_* 块。
    """
    from django.urls import resolvers as _resolvers

    _resolvers._get_cached_resolver.cache_clear()


def _effective_provider():
    """读 reload 后的 settings，返回实际生效的外部认证方式。

    settings.py 对配置不全/缺库的方式会降级为 ENABLE_*=False，所以以 settings 为准。
    """
    if getattr(settings, "ENABLE_LDAP", False):
        return "ldap"
    if getattr(settings, "ENABLE_OIDC", False):
        return "oidc"
    if getattr(settings, "ENABLE_DINGDING", False):
        return "dingding"
    if getattr(settings, "ENABLE_CAS", False):
        return "cas"
    return "local"


def _snapshot_environ():
    """快照所有认证相关 + 启用标志的 os.environ 值（用于回滚）。

    只快照我们关心的 key，避免复制整个 environ；key 不存在记为 sentinel。
    """
    import os as _os

    keys = set()
    for v in _DBKEY_TO_ENV.values():
        keys.add(v)
    keys.update({"ENABLE_LDAP", "ENABLE_OIDC", "ENABLE_DINGDING", "ENABLE_CAS"})
    snap = {}
    for k in keys:
        if k in _os.environ:
            snap[k] = _os.environ[k]
        else:
            snap[k] = _ENV_ABSENT
    return snap


def _restore_environ(snap):
    """按快照还原 os.environ（_ENV_ABSENT 表示原本不存在，要删除）。"""
    import os as _os

    for k, v in snap.items():
        if v is _ENV_ABSENT:
            _os.environ.pop(k, None)
        else:
            _os.environ[k] = v


def reload_auth_config():
    """把数据库认证配置应用到 settings 并即时生效。

    成功返回 (True, "...")。
    失败返回 (False, "错误描述")，并保证 settings / .env / os.environ 被还原，
    让进程回到重载前的可用状态（本地登录始终可用）。
    """
    if not _RELOAD_LOCK.acquire(blocking=False):
        return False, "另一个重载操作正在进行，请稍后重试"

    try:
        auth_config = SysConfig().get_auth_config()
        new_block = _build_managed_block(auth_config)

        # 1. 备份（.env 文件 + os.environ 快照）
        original_env = _rewrite_env(new_block)
        original_environ = _snapshot_environ()

        # 2. 同步 os.environ（environ.read_env 默认不覆盖，需显式写入）
        _apply_to_environ(auth_config)

        # 3. reload settings + 清 URL 缓存
        try:
            _reload_settings_module()
            _clear_url_caches()
        except Exception:
            logger.error("重载 settings 失败，开始回滚 .env 和 os.environ")
            logger.exception("reload settings error")
            # 必须先还原 os.environ，否则再 reload 还是读到被污染的变量
            _restore_environ(original_environ)
            _restore_env(original_env)
            try:
                _reload_settings_module()
                _clear_url_caches()
                logger.info("回滚后 settings 已恢复到重载前状态")
            except Exception:
                logger.exception(
                    "回滚 settings 也失败——进程 settings 可能处于异常状态，"
                    "本地登录仍可用；请检查 .env 和日志后手动重启服务。"
                )
            return False, "重载 settings 失败，已回滚 .env 和环境变量，请检查日志"

        provider = auth_config.get("auth_provider", "local")
        logger.info(f"认证配置已重载，数据库配置的外部认证方式: {provider}")

        # 校验：数据库选的 provider 是否真的在 settings 生效了。
        # settings.py 对配置不全/缺库的方式会降级为 ENABLE_*=False（不阻断 reload），
        # 这里把降级情况如实反馈给管理员，避免「以为启用了实际没生效」。
        effective = _effective_provider()
        if provider in ("ldap", "oidc", "dingding", "cas") and effective != provider:
            return True, (
                f"认证配置已重载，但「{provider}」未真正生效（可能缺少依赖或参数不全），"
                f"已降级为仅本地登录。请检查日志中的 WARNING 并补全后重试。"
            )
        return True, f"认证配置已重载，当前外部认证方式: {provider or '本地登录'}"
    except Exception as e:
        logger.exception("reload_auth_config 未知错误")
        return False, f"重载失败: {e}"
    finally:
        _RELOAD_LOCK.release()
