# DBShield (Archery fork) 项目代码审查报告

| 项 | 内容 |
|---|---|
| 审查日期 | 2026-08-12 |
| 分支 | master (`2d549ea`) |
| 审查范围 | 全项目：`sql_api/`、`common/`、`sql/`、`frontend/`、`dbshield/`、部署与 CI |
| 审查方式 | 4 路并行只读审计（后端安全 / 后端正确性 / 前端 / 基础设施）+ 关键结论二次复核 |
| 总体结论 | 结构扎实，上轮 P0/P1 多数已修复；仍存在 **6 个 High 级问题**（4 个认证/越权类，集中在 `sql_api/` DRF 视图层）+ 1 个必然失败的后台任务 |

---

## 总体评估

项目为 hhyo/Archery 的 Vue3 SPA 现代化分支。DRF 默认权限基座（`IsApiSystemAdmin` 仅超管）设置正确，核心查询链路的参数化查询与 `query_check` 校验到位，commit 4259092 已修复上次审查的多数 P0/P1（轮询泄漏、fallback 缓存、EXPLAIN 校验、趋势单位）。

**当前最大风险面**：`sql_api/` 的 DRF 视图层认证/越权问题——工单冒名审核/执行、2FA 接管、任意文件下载，均可由普通登录用户直接利用（JWT 认证下无 CSRF 阻挡）。另有已确认的必然失败的慢日志清理任务（`cleanup_mysql_slow_log_task`）。

---

## 一、历史问题修复验证（对照 CODE-REVIEW-2026-08-06.md）

| 上次问题 | 现状 |
|---|---|
| H2 降级报告被当 success 缓存 | ✅ 已修（fallback 置 `status="failed"`，缓存只取 success） |
| H3 手动采集同步阻塞 | ✅ 已修（改为 `async_task` 入队） |
| H4 EXPLAIN 无 SELECT 校验 | ✅ 已修（剥离注释后 `re.match(^SELECT\|WITH)` + 拒绝 `INTO OUTFILE`） |
| `CLEANUP_BATCH_SLEEP` NameError | ⚠️ NameError 已修，但同函数暴露新的必崩 bug（见 H5） |
| 趋势时间单位不一致 | ✅ 已修（`TIME_UNIT_MS` 按 db_type 换算） |
| 前端轮询 H1/H2 | ✅ 已修（`pollGeneration` 守卫 + `MAX_POLL_ATTEMPTS=100` + visible 守卫） |
| 前端 XSS | ✅ 主链路均过 `marked + DOMPurify`（唯一例外见 Medium `SqlReviewTable.vue:180`） |
| `diagnose/feedback/` 路由 | ✅ 已修（`diagnose/<int:report_id>/feedback/`，仅模块 docstring 残留旧路径） |

---

## 二、🔴 High 级问题

### H1 · 工单审核/执行可冒名操作（认证绕过）
- 位置：`sql_api/api_workflow.py:255,263-270`（`AuditWorkflow`）、`:331-446`（`ExecuteWorkflow`）
- 操作主体取自请求参数 `engineer` 而非 `request.user`。攻击者传提交人用户名即可 **ABORT 任意工单**；传当前审核节点用户即可 **PASS/REJECT 任意工单并伪造审核记录**。`ExecuteWorkflow` 同理，可冒名触发工单执行（auto 模式直接入队 SQL）。JWT 认证下无 CSRF 阻挡。
- 建议：actor 一律取 `request.user`，`engineer` 仅超管可指定。

### H2 · 2FA 越权接管（认证绕过）
- 位置：`sql_api/api_user.py:349-396`（`TwoFA`）、`:462-513`（`TwoFAVerify`）
- 权限 `AllowAny`，且身份绑定检查（`request_user != engineer`）只作用于未登录分支（:369-374）。任意已认证用户可对任意用户名禁用 2FA、把 SMS OTP 发到攻击者手机（锁定受害者）、获取 TOTP secret。
- 建议：已登录分支强制 `engineer == request.user.username`，或仅超管可操作他人。

### H3 · My2sql 无资源组校验 + 返回真实回滚数据
- 位置：`sql_api/api_misc.py:272-380`（`My2sqlView`）
- 仅 `IsAuthenticated`，`Instance.objects.get(instance_name=...)` 无资源组校验；任意登录用户可对任意实例解析 binlog 返回 INSERT/UPDATE/DELETE 真实回滚数据，且 `stderr` 原样回显（:350）。
- 建议：改用 `user_instances(...)` + `menu_my2sql` 权限 + 通用错误文案。

### H4 · 任意文件下载（IDOR）
- 位置：`sql_api/api_misc.py:959-1035`（`DownloadFileView`）
- 仅 `IsAuthenticated`，`file_name` 完全用户可控且无工单归属校验。本地存储靠 `FileSystemStorage.safe_join` 挡目录穿越，但可枚举下载任意用户的导出数据；SFTP/S3 后端无同保障。
- 建议：按 `workflow_id` 校验归属/资源组，文件名由服务端生成。

### H5 · slow_log 清理任务必然失败（静默）
- 位置：`sql/collectors/tasks.py:160-169`（`cleanup_mysql_slow_log_task`）
- `engine.execute(f"DELETE FROM mysql.slow_log...")` 第一位置参数被当作 `db_name`（SQL 应放 `sql=`），且 `result.rowcount` 在 `ResultSet` 上不存在（只有 `affected_rows`，mysql.execute 未赋值恒为 0）→ 每次运行抛 `AttributeError`，被外层 `except` 吞掉，任务**永远静默失败**，慢日志积累失控。
- 建议：`engine.execute(sql=sql, db_name="mysql")` + 读 `result.affected_rows`。

### H6 · Aliyun RDS 配置越权读写
- 位置：`sql_api/api_instance.py:183-238`（`AliyunRdsDetail`/`AliyunRdsByInstance`）
- 仅 `IsAuthenticated` 即可 GET/PUT/DELETE 任意 `AliyunRdsConfig`，可改写 `key_secret`；存在二次加密把密文再加密的功能性损坏 bug（:211-213）。GET 返回密文。
- 建议：改超管专用权限；`ak` 相关字段 `write_only`；PUT 从库取密文或走 `CloudAccessKey.objects.create` 专有流程。

---

## 三、🟠 Medium 级问题

### 后端越权 / 信息泄漏
| 位置 | 问题 |
|---|---|
| `api_misc.py:313-350`、`804-856` | My2sql/SchemaSync 子进程 argv 含明文库密码（本机可读）；SchemaSync 无资源组校验且 stdout+stderr 拼接回显 |
| `api_misc.py:747-799` | `BackupSqlView`/`OscControlView` 可读任意工单回滚 SQL / 对任意工单 OSC kill/pause/resume |
| `api_workflow.py:449-483` | 任意已认证用户可按 `workflow_id` 读任意工单审核日志 |
| `api_archiver.py:61-102,286-305` | 归档配置详情/日志（含源/目标实例、cmd 输出）IDOR |
| `api_query_priv.py:41-112` | 查询权限申请详情 IDOR |
| `api_instance_admin.py:628-867` | 参数管理直接 `Instance.objects.get(id=...)`，`param_edit` 可改任意实例 `SET GLOBAL` 参数 |
| `api_user.py:198-283` | 资源组 PUT/删除仅 `IsAuthenticated` |
| `api_resource_group.py:148-170` | 可枚举任意资源组实例 |
| `api_config.py:41-44` | 超管可读全部明文配置（`openai_api_key`/`go_inception_password`/`ding_app_secret`），与 `views.py:179-191` 掩码逻辑不一致 |
| `api_slowquery_v2.py:1443-1543` | 引擎异常文本拼入 prompt 外发外部 AI（内网信息泄漏面） |

### 批量赋值 / 序列化器
- `serializers.py:32-56,59-84,87-90,197-214,224-249,429-461`：`User/Group/Instance/CloudAccessKey/AliyunRds/Workflow` 等 `fields="__all__"`，可写 `is_superuser`/`groups`/`user_permissions`/`password` 等，无写入白名单（当前入口默认超管权限，一旦权限配置改动即成提权通道）。

### 错误处理 / SQL 拼接
| 位置 | 问题 |
|---|---|
| 多处 api_*.py（`api_misc.py:120-211`、`api_diagnostic.py:163-164`、`api_archiver.py:174-175`、`api_instance_admin.py:681-682`、`api_resource_group.py:50-51,74-75`） | `int(limit/offset)` 未捕获 `ValueError` → 非法入参 HTTP 500（`_safe_int` 未推广） |
| `api_misc.py:478`（`GenerateSqlView`） | `tb_name` 直接拼接进 `SELECT * FROM \`{tb_name}\`` 无标识符白名单 |
| `api_slowquery.py:297`（`ExplainSqlView`） | `EXPLAIN {sql_content}` 无 SELECT 类型校验（MySQL 8 EXPLAIN 支持 DML） |
| `api_dictionary.py:133`、`api_instance_admin.py:316-362,568` | `escape_string` 不转义反引号 |
| `api_misc.py:269,350`、`api_diagnostic.py:90,181,217,244` | `query_result.error`/`stderr` 原样回显（含引擎/连接细节） |

### 凭据 / 密钥处理
| 位置 | 问题 |
|---|---|
| `common/utils/aes_decryptor.py:7,13` | 硬编码 AES 密钥 `eCcGFZQj6PNoSSma31LR39rTzTbLkU8E` + 固定 IV `0000000000000000` |
| 前端 `authconfig/Index.vue:58-62` | LDAP 密码/OIDC secret/钉钉 secret 明文回填表单并整体回传 |
| 前端 `instance/List.vue:225` | RDS `key_secret` 明文回填 |
| 前端 `config/Index.vue:21` | `openai_api_key` 等明文回显并整体 re-save |
| 前端 `utils/auth.ts:16` | `setCookie` 无 Secure/HttpOnly/SameSite（2FA 临时 sessionid） |

### 前端功能 / 路由
| 位置 | 问题 |
|---|---|
| `components/SqlReviewTable.vue:180` | `ai_suggestion`（markdown）直接 `v-html` **未消毒** → 存储型 XSS（全库唯一漏网点） |
| `router/index.ts` | `user`/`config`/`resourcegroup`/`audit`/`authconfig` 等管理路由缺 `meta.perm`/`requireSuperuser` |
| `config/menu.ts` + `views/audit/Index.vue:14-16` | 「SQL 上线审计/查询审计」两个菜单都 `routeName:"audit"` 不带 `query.type` → 入口全部指向通用审计 |
| `sqlworkflow/Detail.vue:181,283` | 3s/10s 轮询无在途守卫，慢响应并发叠加 |
| `sqlquery/Index.vue:336` | `locatorTimer` 未在 onUnmounted 清理 |
| `Login.vue:210/217/224` | SSO 登录 URL 服务端配置直出 `:href`，未校验协议 |
| `sqlworkflow/Detail.vue:447` + `Submit.vue:23` | `demand_url` 无协议校验，`javascript:` 可触发 |
| `api/sqlexport.ts:86` | 后端返回 URL 直接 `window.location.href` 跳转 |

### 基础设施 / 部署 / CI
| 位置 | 问题 |
|---|---|
| `src/docker-compose/.env`（被 git 跟踪）+ `common/auth_settings_reload.py:136-160` + `README.md:100` | 运行时会把真实 LDAP/OIDC/钉钉密钥写回该文件，README 还引导用户修改 → `git add -A` 即提交生产凭据 |
| `docker-compose.yml:8,12` | redis `--requirepass 123456` 与 `redis-cli ping` healthcheck 矛盾 → 容器永远 unhealthy → compose 无法启动 |
| `docker-compose.yml:21-28` | MySQL 3306 暴露宿主机 + root/123456 弱密码；goinception 4000 暴露宿主（无认证） |
| `inception/config.toml:66-69` | backup root/123456 硬编码 |
| `src/docker/Dockerfile:22-40` | 容器全程以 root 运行 gunicorn/supervisord/nginx |
| `masking.sh:54,28` | `$instance_id` 未定义 → 脱敏记录 instance_id 全为 NULL；`truncate table` 无备份 |
| `requirements.txt:48-57` | `cassandra-driver`/`httpx`/`OpenAI`/`boto3`/`parameterized` 完全未 pin |
| `settings.py:23-26,91-95,339` | `SECRET_KEY` 空串仅告警不阻断；`ALLOWED_HOSTS=["*"]`；无 SESSION/CSRF cookie Secure 标志；nginx 无 TLS |
| `settings.py:196` | `USE_TZ=False` + `TIME_ZONE=Asia/Shanghai` 与时区感知系统互操作隐患 |
| `settings.py:542-551` | LOGGING `maxBytes` 100MB 但注释写 5MB；`ExceptionLoggingMiddleware` 记录完整 traceback 可能含 SQL/凭据 |
| `black.yml:10` + `pyproject.toml` | `psf/black@stable` 滚动版无 `[tool.black]` 配置，CI 随机漂移 |
| `django.yml:96-98` | codecov `fail_ci_if_error: true`，未配 CODECOV_TOKEN 则 CI 必败 |
| `codeql-analysis.yml:42,53,67` | codeql-action v1 过旧，language 仅 python 不含前端 JS/TS |
| `pyproject.toml:1-3` | 无 `testpaths`/`addopts`，pytest 会递归收集 venv 下测试文件 |
| `admin.sh:41` | `kill -9 $(ps -ef | grep "DBShield")` 大小写敏感且进程名实际为 gunicorn/qcluster，杀不中 |

---

## 四、🟡 Low / Info（简列）

- `sql/collectors/aggregator.py:89-91`：`fingerprint=Max("sql_text")` 实为原始 SQL 文本；`MySQLSlowQuerySummary.fingerprint` 2048 字符限制下超长 SQL 在 `STRICT_TRANS_TABLES` 下抛 DataError（settings.py:248）。
- `aggregator.py:45-64`：`_batch_calculate_p95` 每 (instance_id, sql_hash) 全量物化再 Python 算分位数，非批量 SQL。
- `api_slowquery_v2.py:1794-1810`：诊断去重是纯 check-then-act，无锁/唯一约束，并发双击可重复建任务。
- `api_slowquery_v2.py:823-838`：诊断用 web 进程内 `ThreadPoolExecutor`（有意设计，需部署文档注明）。
- `api_slowquery_v2.py:11`：模块 docstring 仍写旧路径 `diagnose/feedback/`。
- `sql/engines/mssql.py:590`：`USE [{db_name}]` 未转义 `]`。
- 前端：全库 `@ts-ignore` 仅 1 处（auto-imports.d.ts）；`any` 15 处（多为 ace 适配，可接受）；`console.log` 0 处；package.json `build` 未跑 `vue-tsc`。
- 配置：`.env.example` 缺失；`Q_CLUISTER_SYNC` 拼写错误（settings 恒读默认值，静默失效）；LOGGING 路径为相对 CWD；supervisord 无 stdout_logfile。
- CI/Docker：actions 多为 v1/v2 旧版；`docker-image.yml` fork 向 hhyo/archery 上游命名空间推镜像；`setup.sh` 下载二进制无 checksum 校验。

---

## 五、亮点（保持现状）

- 本地 `.env`/`.env~`/`..env.un~` 忽略规则完善，git 历史未发现真实密钥泄漏。
- `common/utils/django_q_win_patch.py` 与 django-q2 1.9.0 源码逐项核实正确（无 LongTimeoutHandler 漏 patch、TimeoutException 继承 SystemExit、token 防误杀）。
- `IsApiSystemAdmin` 默认超管权限基座、`_safe_int` 模式、`resolve_instance` 组校验、慢查诊断"零直接执行"红线与 AI 输出容错均实现规范。
- Q_CLUSTER `catch_up=False` 显式防护、`.dockerignore` 排除 `.env*` 入镜像、CSRF/中间件/OIDC/LDAP/DingTalk 降级容错处理规范。

---

## 六、修复优先级 Top-10

| # | 级别 | 位置 | 问题 |
|---|---|---|---|
| 1 | High | `api_workflow.py:231-446` | 工单审核/执行以请求参数 `engineer` 为操作主体，可冒名 |
| 2 | High | `api_user.py:349-513` | 2FA 越权接管/禁用他人 |
| 3 | High | `collectors/tasks.py:160-169` | slow_log 清理必崩（`sql=` 参数错位 + `ResultSet.rowcount` AttributeError） |
| 4 | High | `api_misc.py:272-380` | My2sql 无资源组校验 + 真实回滚数据/错误外泄 |
| 5 | High | `api_misc.py:959-1035` | 任意文件下载 IDOR |
| 6 | High | `api_instance.py:183-238` | Aliyun RDS 越权读写 + 二次加密损坏 bug |
| 7 | High | `src/docker-compose/.env`（跟踪）+ `auth_settings_reload.py:136-160` | 容器运行时真实密钥可被 git 提交 |
| 8 | High | `frontend/.../SqlReviewTable.vue:180` | markdown 未消毒 `v-html` 存储型 XSS |
| 9 | Medium | `docker-compose.yml:8-28,39-40` | redis healthcheck 矛盾 / MySQL·GoInception 暴露宿主机弱密码 |
| 10 | Medium | `serializers.py` + `api_config.py:41-44` | 批量赋值 + 明文密钥读取 |
