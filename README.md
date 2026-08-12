<div align="center">

# DBShield
<h4>SQL 审核查询平台 · Vue3 SPA 现代化分支</h4>

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](./LICENSE)
[![python](https://img.shields.io/pypi/pyversions/django)](https://www.python.org/)
[![django](https://img.shields.io/badge/django-4.2-brightgreen.svg)](https://docs.djangoproject.com/zh-hans/4.2/)
[![vue](https://img.shields.io/badge/vue-3.5-42b883.svg)](https://vuejs.org/)
[![code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

</div>

## 项目简介

本项目是基于开源项目 [hhyo/Archery](https://github.com/hhyo/Archery) 的二次开发分支，在保留其核心多数据库引擎、SQL 审核工作流的基础上，对**前端与接口层进行了大规模现代化重构**：

- **前端**：从 Django 服务端渲染页面（Bootstrap + jQuery + metisMenu / sb-admin-2）整体迁移到 **Vue 3 + TypeScript + Vite + Element Plus** 单页应用（SPA），位于 [`frontend/`](./frontend)。
- **后端**：新增 Django REST Framework 应用 [`sql_api/`](./sql_api)，将原有服务端渲染页面逐步改造为纯 JSON API，前端不再依赖 Django 模板。
- **AI 集成**：基于 OpenAI，提供自然语言生成 SQL、SQL 智能分析、SQL 智能优化等能力。
- **认证**：Django Session 认证 + 双因素（TOTP / 短信），登录全流程在 SPA 内完成；支持 OIDC / CAS / 钉钉 / LDAP 等多种企业认证。
- **可视化**：仪表盘、慢查趋势等图表由 pyecharts 改为 ECharts 重写。

> 该分支为独立演进，不保证与上游 `hhyo/Archery` 可合并。核心 `sql/engines/` 多数据库引擎与审核工作流逻辑保持不变。

## 功能清单

| 数据库        | 查询 | 审核 | 执行 | 备份 | 数据字典 | 慢日志 | 会话管理 | 账号管理 | 参数管理 | 数据归档 |
|------------| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MySQL      | √ | √ | √ | √ | √ | √ | √ | √ | √ | √ |
| MariaDB    | √ | √ | √ | √ | √ | √ | √ | √ | √ | √ |
| MsSQL      | √ | × | √ | × | √ | × | × | × | × | × |
| Redis      | √ | × | √ | × | × | × | × | × | × | × |
| PgSQL      | √ | × | √ | × | × | × | × | × | × | × |
| Oracle     | √ | √ | √ | √ | √ | × | √  | × | × | × |
| MongoDB    | √ | √  | √  | × | × | × | √  | √ | × | × |
| Phoenix    | √ | ×  | √  | × | × | × | × | × | × | × |
| ODPS       | √ | ×  | ×  | × | × | × | × | × | × | × |
| ClickHouse | √ | √  | √  | × | × | × | × | × | × | × |
| Cassandra  | √ | ×  | √  | × | × | × | × | × | × | × |
| Doris      | √ | ×  | √  | × | × | × | √ | × | × | × |
| Elasticsearch | √ | × | √ | × | × | × | × | × | × | × |
| OpenSearch | √ | ×  | √  | × | × | × | × | × | × | × |
| Memcached  | √ | ×  | √  | × | × | × | × | × | × | × |
| TDengine   | √ | √  | √  | × | × | × | √ | × | × | × |

## 系统架构

```
┌──────────────────────────────────────────────────────────┐
│  浏览器  →  Vue 3 SPA (Element Plus / ECharts / Pinia)     │
│            frontend/  (Vite 构建，nginx 托管 dist/)        │
└───────────────────────────┬──────────────────────────────┘
                            │  /api/v1/*  (DRF JSON)
┌───────────────────────────▼──────────────────────────────┐
│  Django 4.2 后端                                          │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐  │
│  │  sql_api/    │   │   sql/       │   │  common/     │  │
│  │  DRF 接口层   │←─→│  核心业务+引擎 │←─→│  通用工具/认证 │  │
│  └──────────────┘   └──────────────┘   └──────────────┘  │
│         │                   │                  │          │
│         └────── OpenAI ─────┴─── django-q 异步任务 ───────┘
└───────────────────────────┬──────────────────────────────┘
                            │
        MySQL / Redis / Oracle / MongoDB / PgSQL / ...
```

- **`frontend/`** — Vue 3 SPA。25 个菜单页全部迁入：仪表盘、SQL 上线、在线查询、数据导出、实例管理（会话/库/账号/参数）、权限管理、数据归档、My2SQL、慢查、SQL 分析 / 优化 / 数据字典 / SchemaSync、系统审计、配置项管理、资源组 / 用户 / 权限组管理、认证配置、相关文档等。
- **`sql_api/`** — DRF 接口层。`api_workflow.py` / `api_sqlquery.py` / `api_instance.py` / `api_query_priv.py` / `api_archiver.py` / `api_config.py` / `api_dashboard.py` / `api_slowlog.py` / `api_misc.py` 等模块，配合 [drf-spectacular](https://github.com/tfranzel/drf-spectacular) 自动生成 OpenAPI 文档。
- **`sql/`** — 上游核心：`engines/` 多数据库引擎、审核工作流、查询/优化/分析逻辑，本分支保持不动。
- **`common/utils/openai.py`** — OpenAI 客户端封装，供 NL→SQL 生成、AI 分析、AI 优化复用。

### AI 能力

| 能力 | 接口 | 说明 |
| --- | --- | --- |
| 自然语言生成 SQL | `POST /api/v1/query/generate_sql/` | 结合所选表的 DDL 作为上下文，调用 OpenAI 生成查询语句 |
| SQL 智能分析 | `POST /api/v1/sql_analyze/ai/` | AI 解读 SQL 执行计划与统计信息 |
| SQL 智能优化 | `POST /api/v1/optimize/ai/` | AI 给出优化建议 |
| OpenAI 配置探测 | `GET /api/v1/query/check_openai/` | 检查后端是否已配置可用 OpenAI |

> 在「配置项管理」页配置 OPENAI 相关参数（API Key / 模型 / Base URL）后即可启用。

## 快速开始

### Docker

两级镜像构建：`Dockerfile-base`（基础镜像：创建 `/opt/venv4dbshield` 并安装 SQLAdvisor / SOAR / my2sql 等外部工具）→ `Dockerfile`（应用镜像：`node:22-alpine` 阶段构建 SPA `dist/`，与基础镜像合并，由 nginx 托管前端、反代后端）。

**1. 构建镜像（先 base，再应用镜像）**

```bash
# 1) 基础镜像（含 venv4dbshield 与外部工具，耗时较长，只依赖 Dockerfile-base/setup.sh）
docker build -f src/docker/Dockerfile-base -t dbshield-base:v1 .

# 2) 应用镜像（默认引用 dbshield-base:v1，也可 --build-arg BASE_IMAGE=<其他镜像> 覆盖）
docker build -f src/docker/Dockerfile -t dbshield-vue3:v1 .
```

**2. 准备 `.env` 与挂载目录**

```bash
cd src/docker-compose
cp .env.example .env   # 按需修改数据库密码、NGINX_PORT、CSRF_TRUSTED_ORIGINS
```

> ⚠️ `.env` 会被容器内「认证配置重载」写入真实 LDAP/OIDC/钉钉密钥，已被 `.gitignore` 排除，**切勿 `git add -f` 提交**。

`.env` 同时承担两个职责（见下文「认证配置机制」），**不要遗漏**：

- 供 `startup.sh` 读取 `NGINX_PORT` 等 shell 变量（通过 docker-compose 的 `env_file` 注入）；
- 供 `settings.py` 的 `environ.read_env()` 与「认证配置」重载逻辑读写（通过 `./.env:/opt/dbshield/.env` 文件挂载）。

**3. 启动**

```bash
docker compose up -d
```

初始化账号：`docker exec -it dbshield /opt/venv4dbshield/bin/python3 /opt/dbshield/manage.py createsuperuser`

> 更多细节可参考上游 [docker 部署文档](https://github.com/hhyo/archery/wiki/docker)。

#### 认证配置机制（重要）

本分支对认证（LDAP / OIDC / 钉钉 / CAS）做了改造，**不再通过 `local_settings.py` 配置**，而是改为「数据库 + 运行时重载」：

```
后台「认证配置」页面 ──写入──▶ DB（SysConfig / sql_config 表）
                                   │  点击「重载认证配置」
                                   ▼
        common/auth_settings_reload.py
          1. 把 DB 中的认证配置写回 .env 的「受管标记块」
             (# === auth config (managed by DBShield) === … # === end managed ===)
          2. 同步 os.environ
          3. importlib.reload(settings 模块) → AUTHENTICATION_BACKENDS /
             AUTH_LDAP_* / ENABLE_LDAP 等重新求值
          4. 清 URL resolver 缓存，使 /oidc/ 等路由按新配置 include
```

因此 **docker-compose 不再挂载 `local_settings.py`**——它在 `settings.py` 末尾以 `from local_settings import *` 加载，优先级最高，会覆盖上述所有认证配置，导致后台修改 + 重载完全不生效（典型表现：LDAP/SSO 登录始终提示「用户名或密码不正确」）。

如需自定义非认证相关的 settings，请改 `.env`（推荐）或直接进容器修改，不要重新挂载 `local_settings.py`。

### 本地开发

后端与前端可分别独立运行，前端通过 Vite proxy 转发 `/api`、`/authenticate` 等到后端。

**1. 后端（Django + DRF）**

```bash
# 创建虚拟环境（Python 3.11）
python -m venv venv
source venv/Scripts/activate    # Windows (Git Bash)
# source venv/bin/activate      # Linux / macOS

# 安装依赖（Windows 本地开发用 requirements-local.txt，去掉了难编译的原生驱动）
pip install "setuptools<82" wheel
pip install -r requirements-local.txt --no-build-isolation

# 配置环境变量（复制 .env.list 为 .env 并按需修改）
#   DATABASE_URL    数据库连接（默认 MySQL）
#   CACHE_URL       Redis 缓存连接
#   ENABLED_ENGINES 启用的数据库引擎，逗号分隔，如 mysql,pgsql
#   CSRF_TRUSTED_ORIGINS  信任的源（需包含前端 dev 地址，如 http://localhost:5175）

# 初始化数据库
python manage.py makemigrations sql
python manage.py migrate

# 启动开发服务器
python manage.py runserver 0.0.0.0:8000
```

**2. 前端（Vue 3 SPA）**

```bash
cd frontend
npm ci
npm run dev          # 默认 http://localhost:5175，proxy → http://localhost:8000
```

常用脚本：

| 命令 | 说明 |
| --- | --- |
| `npm run dev` | 启动 Vite 开发服务器 |
| `npm run build` | 类型检查 + 生产构建到 `dist/` |
| `npm run type-check` | `vue-tsc` 类型检查 |
| `npm run preview` | 本地预览构建产物 |

**3. 初始数据与账号**

导入默认权限组与慢查表结构（也可直接执行 `bash admin.sh migration`）：

```bash
python manage.py dbshell < sql/fixtures/auth_group.sql                 # Default / RD / DBA / PM / QA 权限组
python manage.py dbshell < src/init_sql/mysql_slow_query_review.sql    # 慢查询评审表结构
python manage.py createsuperuser                                       # 创建管理员账号
```

## 运行测试

```bash
# 后端
pytest -q
# 按模块执行
pytest -q sql common sql_api

# 前端
cd frontend
npm run type-check
```

## 技术栈与依赖

### 前端（`frontend/`）

- 框架：[Vue 3](https://github.com/vuejs/core) · [TypeScript](https://github.com/microsoft/TypeScript) · [Vite](https://github.com/vitejs/vite)
- UI：[Element Plus](https://github.com/element-plus/element-plus) · [@element-plus/icons-vue](https://github.com/element-plus/element-plus-icons)
- 状态 / 路由：[Pinia](https://github.com/vuejs/pinia) · [Vue Router](https://github.com/vuejs/router)
- 图表：[ECharts](https://github.com/apache/echarts)
- SQL 编辑器 / 格式化：[ace-builds](https://github.com/ajaxorg/ace) · [sql-formatter](https://github.com/sql-formatter-org/sql-formatter)
- 数据导出：[xlsx](https://github.com/SheetJS/sheetjs) · openpyxl
- Markdown：[marked](https://github.com/markedjs/marked) · [DOMPurify](https://github.com/cure53/DOMPurify)
- HTTP：[axios](https://github.com/axios/axios)

### 后端框架

- [Django](https://github.com/django/django) 4.2 · [Django REST Framework](https://github.com/encode/django-rest-framework)
- [drf-spectacular](https://github.com/tfranzel/drf-spectacular)（OpenAPI）· [django-filter](https://github.com/carltongibson/django-filter)
- 队列任务 [django-q2](https://github.com/GDay/django-q2) · 配置 [django-environ](https://github.com/joke2k/django-environ)
- 数据加密 [django-mirage-field](https://github.com/luojilab/django-mirage-field) · 对象存储 [django-storages](https://github.com/jschneier/django-storages)

### 认证

- Session 认证 · 双因素 [pyotp](https://github.com/pyauth/pyotp) / [qrcode](https://github.com/lincolnloop/python-qrcode)
- 短信：阿里云 dysmsapi · 腾讯云 SMS
- 企业 SSO：[mozilla-django-oidc](https://github.com/mozilla/mozilla-django-oidc) · [django-cas-ng](https://github.com/django-cas-ng/django-cas-ng) · [django-auth-dingding](https://github.com/JiangRRUU/django-auth-dingding) · LDAP（python-ldap / ldap3）

### AI

- [OpenAI Python SDK](https://github.com/openai/openai-python) — NL→SQL 生成、SQL 智能分析与优化

### 数据库连接器

- MySQL [mysqlclient](https://github.com/PyMySQL/mysqlclient-python) / [PyMySQL](https://github.com/PyMySQL/PyMySQL)
- MsSQL [pyodbc](https://github.com/mkleehammer/pyodbc)
- PostgreSQL [psycopg2](https://github.com/psycopg/psycopg2)
- Oracle [cx_Oracle](https://github.com/oracle/python-cx_Oracle)
- MongoDB [pymongo](https://github.com/mongodb/mongo-python-driver)
- Redis [redis-py](https://github.com/redis/redis-py) / Memcached [pymemcache](https://github.com/pinterest/pymemcache)
- ClickHouse [clickhouse-driver](https://github.com/mymarilyn/clickhouse-driver)
- Cassandra [cassandra-driver](https://github.com/datastax/python-driver)
- Phoenix [phoenixdb](https://github.com/lalinsky/python-phoenixdb) · ODPS [pyodps](https://github.com/aliyun/aliyun-odps-python-sdk)
- Elasticsearch [elasticsearch-py](https://github.com/elastic/elasticsearch-py) · OpenSearch [opensearch-py](https://github.com/opensearch-project/opensearch-py)
- TDengine [taos-ws-py](https://github.com/taosdata/taos-connector-python)

### 功能依赖

- MySQL 审核 / 执行 / 备份 [goInception](https://github.com/hanchuanchuan/goInception) · [inception](https://github.com/hhyo/inception)
- MySQL 索引优化 [SQLAdvisor](https://github.com/Meituan-Dianping/SQLAdvisor)
- SQL 优化 / 压缩 [SOAR](https://github.com/XiaoMi/soar)
- Binlog 解析 / 回滚 [my2sql](https://github.com/liuhr/my2sql) · [python-mysql-replication](https://github.com/noplay/python-mysql-replication)
- 表结构同步 [SchemaSync](https://github.com/hhyo/SchemaSync)
- 慢日志解析 [pt-query-digest](https://www.percona.com/doc/percona-toolkit/3.0/pt-query-digest.html)
- 大表 DDL [gh-ost](https://github.com/github/gh-ost) · [pt-online-schema-change](https://www.percona.com/doc/percona-toolkit/3.0/pt-online-schema-change.html)
- MyBatis XML 解析 [mybatis-mapper2sql](https://github.com/hhyo/mybatis-mapper2sql)
- SQL 解析 / 切分 / 类型判断 [sqlparse](https://github.com/andialbrecht/sqlparse)
- RDS 管理 [aliyun-openapi-python-sdk](https://github.com/aliyun/aliyun-openapi-python-sdk)

## 目录结构

```
.
├── dbshield/         # Django 项目配置（settings / urls / wsgi）
├── sql/              # 核心业务：多数据库引擎、审核工作流、查询/优化/分析
├── sql_api/          # DRF 接口层（本分支新增）
├── common/           # 通用工具、认证、OpenAI 客户端封装
├── frontend/         # Vue 3 SPA（本分支新增）
│   └── src/{api,views,components,stores,router,layouts,composables,...}
├── src/
│   ├── docker/       # Dockerfile / nginx.conf / supervisord.conf / 启动脚本
│   ├── charts/       # Helm Chart
│   └── plugins/      # SQLAdvisor / SOAR / my2sql 等外部工具
├── docs/             # 文档（含 PHASE4-CLEANUP.md 迁移收尾清单）
├── requirements.txt          # 生产依赖
└── requirements-local.txt    # Windows 本地开发依赖（去掉难编译的原生驱动）
```

## 贡献代码

欢迎通过以下方式贡献：

- Bug 修复、新功能、代码优化、测试用例完善
- [Wiki 文档](https://github.com/hhyo/Archery/wiki)（开放编辑）
- 提交 Issue 或 Pull Request

## 致谢

- [archer](https://github.com/jly8866/archer) — Archery 项目基于 archer 二次开发而来
- [goInception](https://github.com/hanchuanchuan/goInception) — 集审核、执行、备份及生成回滚语句于一身的 MySQL 运维工具
- [JetBrains Open Source](https://www.jetbrains.com/zh-cn/opensource/?from=archery) — 为项目提供免费的 IDE 授权
  [<img src="https://resources.jetbrains.com/storage/products/company/brand/logos/jb_beam.png" width="200"/>](https://www.jetbrains.com/opensource/)

## License

[Apache License 2.0](./LICENSE)
