# -*- coding: UTF-8 -*-


# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
import os
from typing import List
from datetime import timedelta
import environ
import requests
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

environ.Env.read_env(os.path.join(BASE_DIR, ".env"))
env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["*"]),
    SECRET_KEY=(
        str,
        "",
    ),  # 参考 https://docs.djangoproject.com/zh-hans/4.0/ref/settings/#secret-key
    DATABASE_URL=(str, "mysql://root:@127.0.0.1:3306/archery"),
    CACHE_URL=(str, "redis://127.0.0.1:6379/0"),
    # 系统外部认证目前支持LDAP、OIDC、DINGDING三种，认证方式只能启用其中一种，如果启用多个，实际生效的只有一个，优先级LDAP > DINGDING > OIDC
    ENABLE_LDAP=(bool, False),
    ENABLE_OIDC=(bool, False),
    ENABLE_DINGDING=(
        bool,
        False,
    ),  # 钉钉认证方式参考文档：https://open.dingtalk.com/document/orgapp/tutorial-obtaining-user-personal-information
    AUTH_LDAP_ALWAYS_UPDATE_USER=(bool, True),
    AUTH_LDAP_USER_ATTR_MAP=(
        dict,
        {"username": "cn", "display": "displayname", "email": "mail"},
    ),
    OIDC_USER_ATTR_MAP=(
        dict,
        {"username": "preferred_username", "display": "name", "email": "email"},
    ),
    Q_CLUSTER_SYNC=(bool, False),  # qcluster 同步模式, debug 时可以调整为 True
    # CSRF_TRUSTED_ORIGINS=subdomain.example.com,subdomain.example2.com subdomain.example.com
    CSRF_TRUSTED_ORIGINS=(list, []),
    ENABLED_ENGINES=(
        list,
        [
            "mysql",
            "clickhouse",
            "goinception",
            "mssql",
            "redis",
            "pgsql",
            "oracle",
            "mongo",
            "phoenix",
            "odps",
            "cassandra",
            "doris",
            "elasticsearch",
            "opensearch",
            "memcached",
            "tdengine",
        ],
    ),
    ENABLED_NOTIFIERS=(
        list,
        [
            "sql.notify:DingdingWebhookNotifier",
            "sql.notify:DingdingPersonNotifier",
            "sql.notify:FeishuWebhookNotifier",
            "sql.notify:FeishuPersonNotifier",
            "sql.notify:QywxWebhookNotifier",
            "sql.notify:QywxToUserNotifier",
            "sql.notify:MailNotifier",
            "sql.notify:GenericWebhookNotifier",
        ],
    ),
    CURRENT_AUDITOR=(str, "sql.utils.workflow_audit:AuditV2"),
    PASSWORD_MIXIN_PATH=(str, "sql.plugins.password:DummyMixin"),
)

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = env("SECRET_KEY")

# 生产环境必须显式配置 SECRET_KEY：为空时 JWT SIGNING_KEY 为空串，
# 存在会话/令牌伪造隐患，启动即告警而不是静默上线
if not SECRET_KEY:
    logger.warning(
        "SECRET_KEY 未配置（.env 未设置）：JWT 签名密钥将为空串，"
        "生产环境务必配置随机密钥！"
    )

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env("DEBUG")

ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# https://docs.djangoproject.com/en/4.0/ref/settings/#csrf-trusted-origins
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

# 解决nginx部署跳转404
USE_X_FORWARDED_HOST = True

# 请求限制
DATA_UPLOAD_MAX_MEMORY_SIZE = 15728640

AVAILABLE_ENGINES = {
    "mysql": {"path": "sql.engines.mysql:MysqlEngine"},
    "cassandra": {"path": "sql.engines.cassandra:CassandraEngine"},
    "clickhouse": {"path": "sql.engines.clickhouse:ClickHouseEngine"},
    "goinception": {"path": "sql.engines.goinception:GoInceptionEngine"},
    "mssql": {"path": "sql.engines.mssql:MssqlEngine"},
    "redis": {"path": "sql.engines.redis:RedisEngine"},
    "pgsql": {"path": "sql.engines.pgsql:PgSQLEngine"},
    "oracle": {"path": "sql.engines.oracle:OracleEngine"},
    "mongo": {"path": "sql.engines.mongo:MongoEngine"},
    "phoenix": {"path": "sql.engines.phoenix:PhoenixEngine"},
    "odps": {"path": "sql.engines.odps:ODPSEngine"},
    "doris": {"path": "sql.engines.doris:DorisEngine"},
    "elasticsearch": {"path": "sql.engines.elasticsearch:ElasticsearchEngine"},
    "opensearch": {"path": "sql.engines.elasticsearch:OpenSearchEngine"},
    "memcached": {"path": "sql.engines.memcached:MemcachedEngine"},
    "tdengine": {"path": "sql.engines.tdengine:TDengineEngine"},
}

ENABLED_NOTIFIERS = env("ENABLED_NOTIFIERS")

ENABLED_ENGINES = env("ENABLED_ENGINES")

CURRENT_AUDITOR = env("CURRENT_AUDITOR")

PASSWORD_MIXIN_PATH = env("PASSWORD_MIXIN_PATH")

# Application definition
INSTALLED_APPS = (
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_q",
    "sql",
    "sql_api",
    "common",
    "rest_framework",
    "django_filters",
    "drf_spectacular",
)

MIDDLEWARE = (
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "common.middleware.check_login_middleware.CheckLoginMiddleware",
    "common.middleware.exception_logging_middleware.ExceptionLoggingMiddleware",
)

ROOT_URLCONF = "dbshield.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "common/templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "common.utils.global_info.global_info",
            ],
        },
    },
]

WSGI_APPLICATION = "dbshield.wsgi.application"

# Internationalization
LANGUAGE_CODE = "zh-hans"

TIME_ZONE = "Asia/Shanghai"

USE_I18N = True

USE_TZ = False

# 时间格式化
USE_L10N = False
DATETIME_FORMAT = "Y-m-d H:i:s"
DATE_FORMAT = "Y-m-d"

# Static files (CSS, JavaScript, Images)
STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "static")
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, "common/static"),
]
STATICFILES_STORAGE = "common.storage.ForgivingManifestStaticFilesStorage"

# 扩展django admin里users字段用到，指定了sql/models.py里的class users
AUTH_USER_MODEL = "sql.Users"

# 密码校验
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {
            "min_length": 9,
        },
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

############### 以下部分需要用户根据自己环境自行修改 ###################

# SESSION 设置
SESSION_COOKIE_AGE = 60 * 300  # 300分钟
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True  # 关闭浏览器，则COOKIE失效

# 该项目本身的mysql数据库地址
DATABASES = {
    "default": {
        **env.db(),
        **{
            "DEFAULT_CHARSET": "utf8mb4",
            "CONN_MAX_AGE": 50,
            "OPTIONS": {
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
                "charset": "utf8mb4",
            },
            "TEST": {
                "NAME": "test_archery",
                "CHARSET": "utf8mb4",
            },
        },
    }
}

# Django-Q
Q_CLUSTER = {
    "name": "dbshield",
    "workers": env("Q_CLUSTER_WORKERS", default=8),
    "recycle": 500,
    "timeout": env("Q_CLUSTER_TIMEOUT", default=60),
    # 定时任务追赶：False 表示不补跑错过的周期。若 next_run 因调度器异常卡在过去，
    # CATCH_UP=True 会每次调度都投一条任务（追赶风暴）把队列打爆，此处显式关闭。
    "catch_up": False,
    "compress": True,
    "cpu_affinity": 1,
    "save_limit": 0,
    "queue_limit": 50,
    "label": "Django Q",
    "django_redis": "default",
    "sync": env("Q_CLUSTER_SYNC"),  # 本地调试可以修改为True，使用同步模式
}

# 缓存配置
# 说明：显式配置连接池参数 + 失败降级，避免 Redis 抖动（如 10054 连接被对端重置）
# 导致整个请求 500。health_check_interval 会自动剔除僵尸连接并重建，
# IGNORE_EXCEPTIONS 在 Redis 真挂掉时静默降级（注意：限流会临时失效）。
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env("CACHE_URL", default="redis://127.0.0.1:6379/0"),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "CONNECTION_POOL_KWARGS": {
                "socket_timeout": 5,
                "socket_connect_timeout": 5,
                "health_check_interval": 30,
                "retry_on_timeout": True,
            },
        },
        "IGNORE_EXCEPTIONS": True,
    }
}

# https://docs.djangoproject.com/en/3.2/ref/settings/#std-setting-DEFAULT_AUTO_FIELD
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# API Framework
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    # 鉴权
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    # 权限
    "DEFAULT_PERMISSION_CLASSES": ("sql_api.permissions.IsApiSystemAdmin",),
    # 限速（anon：未认证用户  user：认证用户）
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {"anon": "120/min", "user": "600/min"},
    # 过滤
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    # 分页
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 5,
}

# Swagger UI
SPECTACULAR_SETTINGS = {
    "TITLE": "DBShield API",
    "DESCRIPTION": "OpenAPI 3.0",
    "VERSION": "1.0.0",
}

# API Authentication
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=4),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=3),
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# OIDC
ENABLE_OIDC = env("ENABLE_OIDC", False)
if ENABLE_OIDC:
    try:
        INSTALLED_APPS += ("mozilla_django_oidc",)
        OIDC_USER_ATTR_MAP = env("OIDC_USER_ATTR_MAP")
        AUTHENTICATION_BACKENDS = (
            "common.authenticate.oidc_auth.OIDCAuthenticationBackend",
            "django.contrib.auth.backends.ModelBackend",
        )

        OIDC_RP_WELLKNOWN_URL = env(
            "OIDC_RP_WELLKNOWN_URL"
        )  # 例如 https://keycloak.example.com/realms/<your realm>/.well-known/openid-configuration
        OIDC_RP_CLIENT_ID = env("OIDC_RP_CLIENT_ID")
        OIDC_RP_CLIENT_SECRET = env("OIDC_RP_CLIENT_SECRET")

        # 拉取 well-known 端点：缺端点时 OIDC 流程会在实际使用时报错，不阻断启动/重载。
        OIDC_OP_AUTHORIZATION_ENDPOINT = None
        OIDC_OP_TOKEN_ENDPOINT = None
        OIDC_OP_USER_ENDPOINT = None
        OIDC_OP_JWKS_ENDPOINT = None
        OIDC_OP_LOGOUT_ENDPOINT = None
        try:
            response = requests.get(OIDC_RP_WELLKNOWN_URL, timeout=5)
            response.raise_for_status()
            _oidc_wellknown = response.json()
            OIDC_OP_AUTHORIZATION_ENDPOINT = _oidc_wellknown.get("authorization_endpoint")
            OIDC_OP_TOKEN_ENDPOINT = _oidc_wellknown.get("token_endpoint")
            OIDC_OP_USER_ENDPOINT = _oidc_wellknown.get("userinfo_endpoint")
            OIDC_OP_JWKS_ENDPOINT = _oidc_wellknown.get("jwks_uri")
            OIDC_OP_LOGOUT_ENDPOINT = _oidc_wellknown.get("end_session_endpoint")
        except Exception as _oidc_err:
            logger.warning(
                "OIDC well-known 端点拉取失败：%s。请在「认证配置」页面检查地址，"
                "或稍后点「重载认证配置」重试。",
                _oidc_err,
            )

        OIDC_RP_SCOPES = env("OIDC_RP_SCOPES", default="openid profile email")
        OIDC_RP_SIGN_ALGO = env("OIDC_RP_SIGN_ALGO", default="RS256")

        # OIDC 登录成功后落到 SPA（nginx 不代理 /dashboard，返回 index.html，SPA 接管）
        LOGIN_REDIRECT_URL = "/dashboard"
    except Exception as _oidc_init_err:
        # OIDC 配置不完整（缺必填项等）：不阻断启动，降级为仅本地登录
        ENABLE_OIDC = False
        logger.warning(
            "OIDC 初始化失败，已临时禁用 OIDC（本地登录不受影响）：%s。"
            "请在「认证配置」页面补全参数后重试。",
            _oidc_init_err,
        )

# Dingding
ENABLE_DINGDING = env("ENABLE_DINGDING", False)
if ENABLE_DINGDING:
    try:
        INSTALLED_APPS += ("django_auth_dingding",)
        AUTHENTICATION_BACKENDS = (
            "common.authenticate.dingding_auth.DingdingAuthenticationBackend",
            "django.contrib.auth.backends.ModelBackend",
        )
        AUTH_DINGDING_AUTHENTICATION_CALLBACK_URL = env(
            "AUTH_DINGDING_AUTHENTICATION_CALLBACK_URL"
        )
        AUTH_DINGDING_APP_KEY = env("AUTH_DINGDING_APP_KEY")
        AUTH_DINGDING_APP_SECRET = env("AUTH_DINGDING_APP_SECRET")
        # 钉钉登录成功后落到 SPA（django_auth_dingding 读此配置）
        AUTH_DINGDING_LOGIN_REDIRECT_URL = "/dashboard"
    except Exception as _dingding_init_err:
        ENABLE_DINGDING = False
        logger.warning(
            "钉钉初始化失败，已临时禁用（本地登录不受影响）：%s。"
            "请在「认证配置」页面补全参数后重试。",
            _dingding_init_err,
        )

# LDAP
ENABLE_LDAP = env("ENABLE_LDAP", False)
if ENABLE_LDAP:
    try:
        import ldap
        from django_auth_ldap.config import LDAPSearch

        AUTHENTICATION_BACKENDS = (
            "django_auth_ldap.backend.LDAPBackend",  # 配置为先使用LDAP认证，如通过认证则不再使用后面的认证方式
            "django.contrib.auth.backends.ModelBackend",  # django系统中手动创建的用户也可使用，优先级靠后。注意这2行的顺序
        )

        AUTH_LDAP_SERVER_URI = env("AUTH_LDAP_SERVER_URI", default="ldap://xxx")
        AUTH_LDAP_USER_DN_TEMPLATE = env("AUTH_LDAP_USER_DN_TEMPLATE", default=None)
        if not AUTH_LDAP_USER_DN_TEMPLATE:
            del AUTH_LDAP_USER_DN_TEMPLATE
            AUTH_LDAP_BIND_DN = env(
                "AUTH_LDAP_BIND_DN", default="cn=xxx,ou=xxx,dc=xxx,dc=xxx"
            )
            AUTH_LDAP_BIND_PASSWORD = env("AUTH_LDAP_BIND_PASSWORD", default="***********")
            AUTH_LDAP_USER_SEARCH_BASE = env(
                "AUTH_LDAP_USER_SEARCH_BASE", default="ou=xxx,dc=xxx,dc=xxx"
            )
            AUTH_LDAP_USER_SEARCH_FILTER = env(
                "AUTH_LDAP_USER_SEARCH_FILTER", default="(cn=%(user)s)"
            )
            AUTH_LDAP_USER_SEARCH = LDAPSearch(
                AUTH_LDAP_USER_SEARCH_BASE, ldap.SCOPE_SUBTREE, AUTH_LDAP_USER_SEARCH_FILTER
            )
        AUTH_LDAP_ALWAYS_UPDATE_USER = env(
            "AUTH_LDAP_ALWAYS_UPDATE_USER", default=True
        )  # 每次登录从ldap同步用户信息
        AUTH_LDAP_USER_ATTR_MAP = env("AUTH_LDAP_USER_ATTR_MAP")
    except ImportError:
        # python-ldap 未安装（Windows 常见）：不阻断启动，降级为仅本地登录
        ENABLE_LDAP = False
        logger.warning(
            "未安装 python-ldap（django-auth-ldap 依赖），已临时禁用 LDAP（本地登录不受影响）。"
            "Linux 安装：pip install django-auth-ldap；Windows 可在 WSL/Docker 中运行。"
        )
    except Exception as _ldap_init_err:
        ENABLE_LDAP = False
        logger.warning(
            "LDAP 初始化失败，已临时禁用 LDAP（本地登录不受影响）：%s。"
            "请在「认证配置」页面补全参数后重试。",
            _ldap_init_err,
        )

# CAS认证
ENABLE_CAS = env("ENABLE_CAS", default=False)
if ENABLE_CAS:
    try:
        INSTALLED_APPS += ("django_cas_ng",)
        MIDDLEWARE += ("django_cas_ng.middleware.CASMiddleware",)
        AUTHENTICATION_BACKENDS = (
            "django.contrib.auth.backends.ModelBackend",
            "django_cas_ng.backends.CASBackend",
        )

        # CAS 的地址
        CAS_SERVER_URL = env("CAS_SERVER_URL")
        # CAS 版本
        CAS_VERSION = env("CAS_VERSION")
        # 存入所有 CAS 服务端返回的 User 数据。
        CAS_APPLY_ATTRIBUTES_TO_USER = True
        # 关闭浏览器退出登录
        SESSION_EXPIRE_AT_BROWSER_CLOSE = True
        #  忽略  SSL  证书校验
        CAS_VERIFY_SSL_CERTIFICATE = env("CAS_VERIFY_SSL_CERTIFICATE", default=False)
        #  忽略来源验证
        CAS_IGNORE_REFERER = True
        # https请求问题
        CAS_FORCE_SSL_SERVICE_URL = env("CAS_FORCE_SSL_SERVICE_URL", default=False)
        CAS_RETRY_TIMEOUT = 1
        CAS_RETRY_LOGIN = True
        CAS_EXTRA_LOGIN_PARAMS = {"renew": True}
        CAS_LOGOUT_COMPLETELY = True
        # CAS 登录成功后落到 SPA（django_cas_ng 在 CAS_IGNORE_REFERER=True 时用此配置）
        CAS_REDIRECT_URL = "/dashboard"
    except Exception as _cas_init_err:
        ENABLE_CAS = False
        logger.warning(
            "CAS 初始化失败，已临时禁用 CAS（本地登录不受影响）：%s。"
            "请在「认证配置」页面补全参数后重试。",
            _cas_init_err,
        )

SUPPORTED_AUTHENTICATION = [
    ("LDAP", ENABLE_LDAP),
    ("DINGDING", ENABLE_DINGDING),
    ("OIDC", ENABLE_OIDC),
    ("CAS", ENABLE_CAS),
]
# 计算当前启用的外部认证方式数量
ENABLE_AUTHENTICATION_COUNT = len(
    [enabled for (name, enabled) in SUPPORTED_AUTHENTICATION if enabled]
)
if ENABLE_AUTHENTICATION_COUNT > 0:
    if ENABLE_AUTHENTICATION_COUNT > 1:
        logger.warning(
            "系统外部认证目前支持LDAP、DINGDING、OIDC、CAS四种，认证方式只能启用其中一种，如果启用多个，实际生效的只有一个，优先级LDAP > DINGDING > OIDC > CAS"
        )
    authentication = ""  # 默认为空
    for name, enabled in SUPPORTED_AUTHENTICATION:
        if enabled:
            authentication = name
            break
    logger.info("当前生效的外部认证方式：" + authentication)
    logger.info("认证后端：" + AUTHENTICATION_BACKENDS.__str__())

# LOG配置
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[%(asctime)s][%(threadName)s:%(thread)d][task_id:%(name)s][%(filename)s:%(lineno)d][%(levelname)s]- %(message)s"
        },
    },
    "handlers": {
        "default": {
            "level": "DEBUG",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "logs/dbshield.log",
            "maxBytes": 1024 * 1024 * 100,  # 5 MB
            "backupCount": 5,
            "formatter": "verbose",
        },
        "django-q": {
            "level": "DEBUG",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "logs/qcluster.log",
            "maxBytes": 1024 * 1024 * 100,  # 5 MB
            "backupCount": 5,
            "formatter": "verbose",
        },
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        "default": {  # default日志
            "handlers": ["console", "default"],
            "level": "WARNING",
        },
        "django-q": {  # django_q模块相关日志
            "handlers": ["console", "django-q"],
            "level": "WARNING",
            "propagate": False,
        },
        "django_auth_ldap": {  # django_auth_ldap模块相关日志
            "handlers": ["console", "default"],
            "level": "WARNING",
            "propagate": False,
        },
        "mozilla_django_oidc": {
            "handlers": ["console", "default"],
            "level": "WARNING",
            "propagate": False,
        },
        # 'django.db': {  # 打印SQL语句，方便开发
        #     'handlers': ['console', 'default'],
        #     'level': 'DEBUG',
        #     'propagate': False
        # },
        # 'django.request': {  # 打印请求错误堆栈信息，方便开发
        #     'handlers': ['console', 'default'],
        #     'level': 'DEBUG',
        #     'propagate': False
        # },
    },
}

# 在网站标题及登录页面追加此内容, 可用于多dbshield实例的区分。DBShield后台也有相同配置，如都做了配置，以后台配置为准
CUSTOM_TITLE_SUFFIX = env("CUSTOM_TITLE_SUFFIX", default="")

MEDIA_ROOT = os.path.join(BASE_DIR, "media")
if not os.path.exists(MEDIA_ROOT):
    os.mkdir(MEDIA_ROOT)

PKEY_ROOT = os.path.join(MEDIA_ROOT, "keys")
if not os.path.exists(PKEY_ROOT):
    os.mkdir(PKEY_ROOT)

try:
    from local_settings import *
except ImportError:
    print("import local settings failed, ignored")

# Windows 下 django-q2 的 SIGALRM 任务超时静默失效（timeout.py 内被 except 吞掉），
# 卡死的任务无法优雅超时，配合 timeout=-1 会永久占住 worker、队列只进不出。
# 用守护线程看门狗在 Windows 上复刻超时行为（详见 common.utils.django_q_win_patch）。
if os.name == "nt":
    try:
        from common.utils import django_q_win_patch

        django_q_win_patch.install()
    except Exception as _q_win_patch_err:
        logger.warning(
            "django-q Windows 超时补丁加载失败（不影响启动）：%s", _q_win_patch_err
        )
