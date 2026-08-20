# DBShield (Archery fork) 项目代码审查报告

| 项 | 内容 |
|---|---|
| 审查日期 | 2026-08-20 |
| 分支 | master (`99af883`) |
| 审查范围 | 全项目第四轮：重点覆盖前三轮较浅的区域——`sql/` 引擎与采集任务层、`sql_api/` 剩余视图、慢查询/AI 模块 v1 端点、前端 sinks、部署基础设施；并复核 08-18 报告修复与历史遗留 |
| 审查方式 | 5 路并行只读审计（引擎任务层 / sql_api 视图 / 慢查 AI / 前端 / 基础设施）+ 4 项关键新发现逐行人工复核（归档链路、Doris kill、诊断 IDOR、v1 EXPLAIN） |
| 总体结论 | H6b/H6c 修复确认闭环；但本轮在此前覆盖较浅的引擎层和 v1 旧端点发现 **5 个新 High**（1 个功能已断裂、2 个注入、1 个越权、1 个静默丢数据），历史 H4b 仍未修 |

**基线事实**（影响越权类判定）：`dbshield/settings.py:313` DRF 默认权限为 `IsApiSystemAdmin`（仅超管），未显式声明 `permission_classes` 的视图默认超管专用；显式声明 `[IsAuthenticated]` 的视图才是普通登录用户可达的边界。本轮所有越权结论均已按此基线校准。

---

## 一、上轮修复与历史遗留核验

### 1.1 最新提交 99af883（H6 收尾）——✅ 闭环
- **H6c**：`AliyunRdsConfigPermission` 前移 + `AliyunRdsList.permission_classes` 收紧（api_instance.py:147-161），与 Detail/ByInstance 一致。
- **H6b**：前端 `saveRds` 区分新增/编辑（List.vue:237-243），编辑留空 Secret → 后端 `if ak_data.get("key_secret")` 空串为假跳过赋值（api_instance.py:228-230），链路自洽，人工复核通过。

### 1.2 08-18 报告遗留问题现状
| 问题 | 现状 |
|---|---|
| H4b 下载权限 OR 含 `sqlexport_submit`（api_misc.py:999-1005） | **未修**——已加工单归属+文件名匹配校验（:993-1008），但 OR 链仍放行持 `sqlexport_submit` 的普通用户下载**他人**工单导出文件；`workflow.engineer==user` 分支已覆盖本人，该分支应删除 |
| M2 mysql healthcheck（docker-compose.yml:31） | **未修，但影响需修正**：`mysqladmin ping` 在 Access denied 时退出码仍为 0，"永 unhealthy"不成立；真实风险相反——初始化阶段走 socket 的裸 ping 会在正式实例就绪前返回健康（假阳性），建议改 `mysqladmin ping -h 127.0.0.1` 走 TCP |
| M1 aliyun redis SDK 降版 | 未处理（可接受，建议补注释） |
| 08-12 Medium 清单 b/c/d/e/f/g/h/k/l | **全部未修**（行号核验见第三节，`ed9e43e` 修复未触及 api_archiver/api_query_priv/api_instance_admin/api_resource_group）；i) 明文配置读取已随权限收敛为超管（api_config.py:35） |

---

## 二、🔴 High 级问题（本轮新发现，均已人工逐行复核）

### H1 · 数据归档"立即执行"链路已断裂 + 无权限校验的越权口
- 位置：`sql/archiver.py`（**模块已不存在**）+ `sql_api/api_workflow.py:449-455` + `sql_api/api_archiver.py:326-329`
- **事实链**：`git log --follow sql/archiver.py` 显示该模块在 DRF 化重构提交 `9758b51`（"Phase 1 final, +1026,-2901"）中被整体删除；`importlib.util.find_spec("sql.archiver")` 当前返回 None（`__pycache__` 里的陈旧 .pyc 不会被 import）。但两处调用方仍在：
  - `ExecuteWorkflow`（仅 `IsAuthenticated`）`workflow_type==3` 分支直接 `async_task("sql.archiver.archive", workflow_id)`——**无 ArchiveMgtPermission、无工单状态校验**（对比 type==2 分支有 sql_execute + can_execute 双重校验；正规触发口 ArchiveOnceView 有 `ArchiveMgtPermission`）；
  - `ArchiveOnceView`（api_archiver.py:326-329）同样调用该不存在的任务。
- **后果**：① 归档"立即执行"功能全链路必失败（worker 端 ModuleNotFoundError，用户侧仍提示"开始执行"）；② `sql/test_archiver.py:12` `from sql.archiver import ...` 导致该测试文件收集即报错；③ 越权口是地雷——一旦按上游恢复 archiver 模块，任意登录用户即可对任意（含未审核通过的）归档配置触发执行，归档是删除/搬迁源表数据的破坏性操作。
- 建议：从 git 历史（`git show 9758b51^:sql/archiver.py`）或上游恢复模块并补测试；`ExecuteWorkflow` type==3 分支复用 `ArchiveMgtPermission` + 校验工单 `state/status` 已通过；恢复前先给两处调用口加权限，消除地雷。

### H2 · Doris 会话终止 SQL 注入 → 实例账号下任意 SQL 执行
- 位置：`sql/engines/doris.py:70-74`（覆盖默认值）+ `sql/engines/mysql.py:1159-1188`（拼接点）+ `sql_api/api_diagnostic.py:138-139`（调用口）
- `MysqlEngine.kill/get_kill_command` 默认 `thread_ids_check=True`（强制全 int 校验，mysql 实例安全）；**DorisEngine 把签名改为 `thread_ids_check=False`**，而 `KillSessionView` 调用 `engine.kill(thread_ids)` 不传该参数 → 校验被跳过 → `",".join(str(tid))` 直接拼入 `... where id in ({})`。该 SQL 先经 `query()` 执行，结果行拼成 `kill_sql` 再经 `execute()`（sqlparse.split 逐条执行）。
- 持 `sql.process_kill` 权限的用户提交 `ThreadIDs=["1) or 1=1 --"]` 可杀掉全部会话；用 `union select 'kill <任意SQL>;'` 型载荷可让第二阶段执行任意 SQL（实例账号权限内）。
- 建议：删除 DorisEngine 对默认值的覆盖（或在父类 kill/get_kill_command 内强制类型转换后再拼接），标识符外一律不拼用户输入。

### H3 · AI 诊断任务轮询 IDOR：慢查菜单权限即可读任意资源组的诊断报告
- 位置：`sql_api/api_slowquery_v2.py:1944`（`SlowQueryDiagnoseTaskView.get`）
- 授权条件为 `is_superuser or task.user_id == u.id or u.has_perm("sql.menu_slowquery")`——**第三个分支无实例归属校验**，`AIDiagnosisTask.id` 自增可枚举。任意仅持慢查菜单权限的用户 GET `/api/v1/slowquery/diagnose/<id>/` 即可读取其他资源组实例的诊断报告全文（root_cause、evidence、含改写 SQL/index DDL 的 suggestions、report_markdown、error）。
- 同文件 feedback（:1968）与 workflow_draft（:2034）均已做 `user_instances(...).filter(id=instance.id).exists()` 防护，唯独轮询口漏掉。注释"内部已有细粒度检查"（:1929）与实际不符。
- 建议：补同款 `user_instances` 归属校验。

### H4 · v1 EXPLAIN 端点完全无语句校验（v2 的 H4 闸门未回灌）
- 位置：`sql_api/api_slowquery.py:297`
- `engine.query(db_name=db_name, sql=f"EXPLAIN {sql_content}")` 原样拼接用户 SQL。v2 `_collect_explain`（v2:1486-1494）已有的首句截断、注释剥离、SELECT/WITH 白名单、`INTO OUTFILE/DUMPFILE` 拒绝、超时控制，v1 端点一行都没有。
- 触发面：`EXPLAIN ANALYZE SELECT ... INTO OUTFILE '/tmp/x'`（MySQL 8.0.18+ 会**真实执行**查询并写文件——v2 修复注释里点名的正是该注入面）。同文件 v1 还有两处 AI 端点未套用 v2 的 `_mask_sql_literals` 脱敏（:150-155、:326-360，SHOW CREATE TABLE 拉取的真实 DDL 含 COMMENT/DEFAULT 业务数据原样外发外部 AI）。
- 建议：把 v2 校验逻辑抽成公共函数两处复用；v1 AI 端点复用脱敏 + 错误文案改通用（:155、271、307、360 异常原文回显）。

### H5 · MongoDB 慢查采集游标时区错位，每轮静默丢失最长 8 小时数据
- 位置：`sql/collectors/mongo_collector.py:230-234`（查询条件）、`:246-251`（游标存取）
- `system.profile` 查询条件用 naive 本地时间（游标在 :251 被转成 Asia/Shanghai naive 后存回），而 pymongo 将 naive datetime 按 UTC 编码、Mongo `ts` 本身是 UTC：游标之后 0~8 小时内的慢查询永远不满足条件被漏采，且无任何告警。国内时区部署必现，慢查平台对 Mongo 实例的数据完整性系统性受损。
- 建议：查询条件与游标统一用 UTC aware 时间比较。

### 遗留 High（08-18 已报，仍未修）
- **H4b**：`api_misc.py:999-1005` 下载权限 OR 含 `sqlexport_submit` 分支，持该普通业务权限的用户可下载他人工单导出文件（工单列表可见 workflow_id，file_name 恒等于 workflow.file_name 通过归属校验）。建议删除该分支，仅保留 `offline_download` + 提交人/超管。
- 另注意 `api_misc.py:1007-1017`：工单 `file_name` 为空时用户传入的文件名不做任何归一化直接进 `storage.exists/open`——本地 FileSystemStorage 有 safe_join 兜底，SFTP/S3 自定义后端（sql/storage.py DynamicStorage）未必防 `../`，建议空值拒绝下载。

---

## 三、🟠 Medium 级问题（按主题归组）

### 3.1 越权 / IDOR（均未修，行号为 HEAD 核验值）
| 位置 | 问题 |
|---|---|
| api_workflow.py:199-228 + serializers.py:669-689 | `WorkflowAuditList` 显式传任意 `engineer` 依然放行，可枚举任意用户待办工单（标题、审批组结构） |
| api_workflow.py:465-490 | `WorkflowLogList` 仅 `IsAuthenticated`，POST 任意 workflow_id 可读全部审批日志（含审批备注） |
| api_misc.py:765、:793-810 | `BackupSqlView`/`OscControlView` 仅菜单权限，无 can_view/归属校验：任意开工单页用户可读任意工单回滚 SQL（变更前数据镜像）、对任意工单 OSC kill/pause/resume，`osc_result.error` 原样回显 |
| api_archiver.py:62-65、:286-305 | 归档配置详情/日志 IDOR（源/目标实例、cmd 输出、审批人）；ArchiveLog 无资源组过滤（对比 ListView :187-193 有） |
| api_query_priv.py:44-51 | 查询权限申请详情 IDOR + DoesNotExist 未捕获 500 |
| api_instance_admin.py:642/687/721/773 | 参数管理 `Instance.objects.get` 直取无资源组校验，`param_edit` 可对任意实例 `SET GLOBAL`（对比同文件 :61-80 `_get_instance` 已用 resolve_instance） |
| api_user.py:203-283、api_resource_group.py:148-271 | 资源组 POST/PUT/DELETE 仅 `IsAuthenticated`（注释自认"靠前端按钮守卫"）；任意登录用户可创建资源组、按 group_name 枚举任意组实例与审批组 |
| serializers.py:417-427 | `ExecuteCheck` 的 `validate_instance_id` 仅校验存在不校验归属，持 sql_submit 者可对任意实例发起 goInception 检测探测 |

### 3.2 SQL/标识符注入
| 位置 | 问题 |
|---|---|
| api_misc.py:478-490 | `GenerateSqlView` 的 `tb_name` 直接拼 `SELECT * FROM \`{tb_name}\``，且 `engine.query` 直连执行**绕过 query_priv_check 与数据脱敏**；mysql.py:731-732 `show create table` 同点 |
| api_instance_admin.py:308-361 | GRANT/REVOKE 的 db/tb/col 名反引号包裹未转义（`` ` `` 不处理），`db_name="a\`.\`b"` 可改写授权对象（MySQL 层横向提权）；权限名未做白名单 |
| api_dictionary.py:126-133、api_instance_admin.py:566-568 | `escape_string` 不转义反引号族（同根问题） |
| sql/engines/mssql.py:66-87 | ODBC 连接串 `.format` 拼接 `PWD={4}` 且 `DATABASE={db_name}` 未用花括号包裹：密码/库名含 `;`/`}` 可注入连接参数，含特殊字符的合法密码直接连接失败 |
| sql/engines/oracle.py:1144-1167 | 工单执行备份块 object_name（从 SQL 文本正则解析）未转义拼入 `upper('{...}')`，建议绑定变量 |

### 3.3 引擎层必然崩溃族（except/finally 引用未定义变量）
| 位置 | 触发 |
|---|---|
| sql/engines/pgsql.py:252-257、:441-445 | `get_connection()` 失败 → except 里 `conn.rollback()` NameError，掩盖原始连接错误 |
| sql/engines/oracle.py:1238-1254 | 备份块 finally 用 try 内赋值的 cursor/begin_time，连接失败时 NameError 使执行任务崩溃、状态不落库 |
| sql/engines/oracle.py:1416-1463 | `task_begin=1` 在 CREATE_TUNING_TASK 之前设置，创建失败时清理动作再抛 ORA-13605 覆盖真实错误 |
| sql/engines/elasticsearch.py:806-823 | 连接失败 → except 里 `doc.sql` NameError |
| sql/engines/clickhouse.py:60-68 | 表名含多个 `.` 时 `split(".")` ValueError → 检测 500；system.build_options 空结果 IndexError |
| sql/engines/mongo.py:1030-1067 | `insert ({...})` 带空格即 `re.search(...).group(1)` AttributeError；count 异常时 rows[0] IndexError |
| sql/engines/phoenix.py:1154 | `sql.format(...)` 应为 `sqlparse.format`，SQL 含 `{}` 抛 KeyError 使 query_check 崩溃 |

### 3.4 采集/统计正确性
| 位置 | 问题 |
|---|---|
| sql/collectors/mysql_collector.py:155-251 | 明细分批 `start_time >` 游标严格大于 + 无 tiebreaker：同秒超 1000 条时剩余记录永久跳过 |
| sql/collectors/pgsql_collector.py:195-211 + aggregator.py:183-267 | detail 被 update_or_create 唯一化（每指纹 1 行），定时链路只走 detail+aggregate → 统计 execution_counts 恒 1、total_time=单次均值，数据完全失真；能写真实 calls 的 collect_summary 在 "all" 类型下不会被调用 |
| sql/collectors/mongo_collector.py:67-80、:261 | 指纹把集合名也替换成 `?`：不同库/不同集合同形状查询互相覆盖合并 |
| sql/collectors/mongo_collector.py:30-34 | URI 凭据未 URL 编码（特殊字符密码连错主机）、忽略 is_ssl 与 authSource，SSL 实例采集必失败仅记 warning |
| mysql/mongo/redis_collector 的 bulk_create | Detail 表无唯一约束 + 游标最后才提交：任务中途失败重跑重复插入，与清理任务可能形成循环膨胀 |
| sql/engines/mssql.py:683-696 | `execute_check` 连接清理条件恒 False，每次检测泄漏一个连接 |

### 3.5 v1/相邻模块未同步 v2 修复（"新不修旧"断层）
- `api_diagnostic.py:12` `import _json as json`——C 模块无 `loads`，`json.loads(thread_ids)`（:109、:132）遇 JSON 字符串直接 AttributeError 500（**kill 会话功能被此打断**）；`:163-164` 裸 `int()`。
- v1 AI 端点无脱敏 + 异常原文回显（见 H4）；`_safe_int` 未推广到 api_misc/api_archiver/api_resource_group/api_diagnostic 等十余处（08-12 清单 l 项，全部未修）。

### 3.6 基础设施 / 部署
| 位置 | 问题 |
|---|---|
| docker-compose.yml:19、:5 | **EOL 基础镜像**：mysql:5.7（2023-10 停止维护）承载平台元数据库、redis:5 同样过期，无安全补丁 |
| docker-compose.yml:37、:51 | goinception 镜像无 tag（=latest 供应链漂移）；dbshield 镜像无 build 段需手工预构建 |
| src/docker/setup.sh:4-29 + Dockerfile:36 | 二进制（sqladvisor/soar/my2sql/oracle client）curl 下载无 checksum；`PIP_TRUSTED_HOST=mirrors.aliyun.com` 明文信任第三方镜像 |
| settings.py:488、:367 | `CAS_VERIFY_SSL_CERTIFICATE` 默认 False（认证流可 MITM）；OIDC well-known 在 settings import 期同步外呼（可配 URL，启动阻塞 + SSRF 面） |
| .env.example:26-28 | `Q_CLUISTER_*` 拼写错误与 settings 读取的 `Q_CLUSTER_*` 不一致，照抄部署 worker 数静默失效（.env.list:21 同 typo，:24-25 还提交了一份指向 localhost 的无效 CAS 配置） |
| docker-compose.yml:66 | 挂载不存在的 `./dbshield/sql/migrations` → Docker 自动建空目录**遮蔽镜像内迁移文件**，容器内 migrate 看不到任何内置迁移 |
| src/docker/startup.sh:10-14 + nginx.conf:40 | `NGINX_PORT` sed 占位符在 nginx.conf 中不存在，配置完全无效 |
| settings.py:87-95、:339 | SECRET_KEY 空串仅告警放行（JWT SIGNING_KEY 直接受影响）；ALLOWED_HOSTS 默认 `*`；全库无任何 SESSION/CSRF cookie Secure 配置（.env.list 的 SECURE_SSL_REDIRECT 根本不被读取） |
| requirements.txt | cassandra-driver/httpx/OpenAI/boto3/parameterized 未 pin；dev-requirements 六包全部未 pin |
| masking.sh:36-54 | `while read` 3 变量对 4 列 → 连接参数整体错位；`$instance_id` 未定义；脚本逻辑已断裂 |
| 前端 config/authconfig 页 | 后端 GET 返回明文密钥（api_config.py:41-44、common/config.py:162-173），前端回填并整体回传；authconfig "留空表示不修改"机制因 GET 不脱敏而失效 |

---

## 四、🟡 Low / Info（简列）

- **api_misc.py SchemaSync**（:833-867）：无资源组校验 + `mysql://user:pass@host` 明文进 argv + stdout/stderr 拼接回显（08-12 b 项，My2sql 部分已修、SchemaSync 未修）。
- **api_slowquery_v2**：`SlowQueryV2Permission` 定义后无引用成死代码（:93-98）；limit/days 无 clamp（负数反向切片拖全表）；stale 判定以 created_at 起算，排队后执行的任务会被误判失败（:911-934）；PgSQL DDL 采集表名拼接（:1300-1328）；`_mask_sql_literals` 漏 `''` 双写转义与 0x 十六进制字面量，explain_text/table_schemas 未脱敏；诊断异常原文入库回显（:1680+984）。
- **前端**：OSC 进度弹窗重复打开 interval 泄漏叠加（Detail.vue:277-284）；`demand_url` 无协议校验可提交 `javascript:`（Submit.vue 自由文本 + Detail.vue:447 直出，存储型 XSS 向量）；user/config/resourcegroup 路由缺 meta 守卫（后端有兜底，非漏洞）；audit 三个子菜单 routeName 相同不带 query.type，入口全部落到通用审计；utils/auth.ts setCookie 无 SameSite；Login redirect 未校验；sqlquery locatorTimer 与短信倒计时未清理。
- **CI**：black@stable 滚动、codecov fail_ci_if_error、codeql v1 仅 python 不含前端、actions 全面过旧、docker-base-image.yml paths 漏 setup.sh 且产物无消费者、admin.sh kill 大小写杀不中、pyproject 无 testpaths。
- **容器**：全程 root 运行（无 USER 指令）；gunicorn 非 exec、PID1 为 bash 无法优雅停机；nginx 无 server_tokens off 且 SPA 回退把未知 API 路径返 200 index.html；inception config.toml 备份 root/123456 硬编码入库。
- **其他**：mysql.py SHOW CREATE 系列反引号未转义；goinception.py:220 f-string 缺 `{}`；采集任务每 5 分钟对全部 db_type 派发注定跳过的任务；redis 采集忽略集群模式；tasks.py:327 schedule 无同名去重。
- **本机卫生（不入库）**：本地 `.env` 含真实凭据（MySQL root、LDAP bind 密码及公网地址、生产 SECRET_KEY）及 `.env~`/`..env.un~` 编辑器备份——虽被 .gitignore/.dockerignore 排除，建议尽快轮换上述凭据并清理备份文件。

---

## 五、亮点

- `sql/engines/pgsql.py` 全面参数化 + pglast AST 级工单审核（区分 DROP/TRUNCATE 高危与无 WHERE 告警），是各引擎中质量最高的实现。
- `sql/plugins/plugin.py` 的 subprocess 参数列表 + `shell=False` 机制性杜绝 shell 注入，pt-archiver/schemasync/soar 均继承。
- 慢查 v2 主链路的修复质量好：实例归属校验（除 H3 轮询口）、H4 EXPLAIN 闸门、H1 外发脱敏、DOMPurify 渲染、诊断降级不落缓存均到位——问题集中在"未回灌 v1"而非 v2 本身。
- `sql/collectors/base.py` CursorManager 游标语义（仅新数据推进、有数据才提交）与定时任务显式 timeout=600 的有界设计意识良好。
- 前端全库 v-html 仅 6 处且全部过 marked+DOMPurify，无 localStorage 存敏感数据，会话依赖 HttpOnly cookie + CSRF token。

---

## 六、修复优先级 Top-10

| # | 级别 | 位置 | 问题 |
|---|---|---|---|
| 1 | High | api_workflow.py:449-455 + api_archiver.py:326-329 | H1：归档执行链路断裂（sql.archiver 模块已删）+ ExecuteWorkflow type==3 无权限校验（先加权限消雷，再恢复模块） |
| 2 | High | doris.py:70-74 + mysql.py:1159-1188 | H2：Doris kill SQL 注入 → 任意 SQL 执行 |
| 3 | High | api_slowquery_v2.py:1944 | H3：诊断任务轮询 IDOR（补 user_instances 校验，一行级修复） |
| 4 | High | api_slowquery.py:297 等 | H4：v1 EXPLAIN 无校验 + v1 AI 端点无脱敏（回灌 v2 闸门） |
| 5 | High | mongo_collector.py:230-251 | H5：Mongo 采集时区错位静默丢 8 小时数据 |
| 6 | High | api_misc.py:999-1005 | H4b（遗留）：下载权限 OR 删 `sqlexport_submit` 分支 |
| 7 | Medium | api_diagnostic.py:12/109/163 | `import _json` 使 kill 会话功能 500 + 裸 int()（一行 import 修复，收益高） |
| 8 | Medium | 3.1 芊 IDOR 组 | BackupSql/OscControl、WorkflowLogList、归档/查询权限申请详情、参数管理、资源组写接口（可一次提交统一补 resolve_instance/权限类） |
| 9 | Medium | api_misc.py:478-490 + api_instance_admin.py:308-361 | GenerateSqlView 绕过脱敏直查 + GRANT 标识符逃逸 |
| 10 | Medium | docker-compose.yml:19/66 + .env.example | EOL 镜像、migrations 挂载遮蔽、Q_CLUISTER typo（部署正确性三连） |

---

## 七、审查方法与可信度说明

- 5 路并行只读审计覆盖 sql/、sql_api/、慢查 AI 模块、frontend/、基础设施，所有结论要求 file:line 证据；历史问题逐条与 `ed9e43e`/`99af883` diff 比对判定修复状态。
- 本报告 4 个 headline 新发现（H1-H4）经人工逐行复核：H1 额外做了 git 历史（9758b51 删除）与运行时 import 验证；H2/H3/H4 直接核对调用链与拼接点。
- 引擎层崩溃族、采集统计类问题（3.3/3.4 节）来自单路审计，行号已抽查未见偏差，但建议修复时以实际代码为准逐条确认触发条件。

---

## 八、修复记录（2026-08-20，按 Top-10 顺序）

| # | 修复内容 | 涉及文件 |
|---|---|---|
| 1 | **归档链路断裂**：`sql/archiver.py` 按 `9758b51^` 恢复任务部分（`add_archive_task`/`archive`，删掉已 DRF 化的 6 个旧 view 与无用 import）；`ExecuteWorkflow` type==3 分支补 `archive_mgt` 权限 + 工单 state/status 校验；连带修复 `sql/tests.py` 对已删模块 `sql.binlog`/`sql.query` 的死 import（删除 `test_my2sql_file`/`test_kill_query_conn` 两个死用例），恢复 pytest 收集 | 新增 `sql/archiver.py`；`sql_api/api_workflow.py`；`sql/tests.py` |
| 2 | **Doris kill 注入**：删除 `doris.py` 覆盖的 `thread_ids_check=False`；父类 `mysql.py` 校验放宽为"int 或纯数字字符串"（`_is_valid_thread_id`），其余载荷一律拒绝 | `sql/engines/doris.py`、`sql/engines/mysql.py` |
| 3 | **诊断轮询 IDOR**：`SlowQueryDiagnoseTaskView` 对非超管/非发起人补 `user_instances` 实例归属校验（对齐 feedback/workflow 两个口子） | `sql_api/api_slowquery_v2.py` |
| 4 | **v1 回灌**：v2 的 `_mask_sql_literals` 更名公共 `mask_sql_literals`、EXPLAIN 闸门抽为公共 `sanitize_explain_sql`；v1 `ExplainSqlView` 复用闸门（+占位符替换、max_execution_time、异常脱敏），`SqlAnalyzeAIView`/`OptimizeAIView` 复用脱敏 + 表名白名单 + 异常文案改通用；`SqlTuning` 错误文案脱敏 | `sql_api/api_slowquery_v2.py`、`sql_api/api_slowquery.py` |
| 5 | **Mongo 采集时区**：新增 `_local_naive_to_utc`/`_utc_naive_to_local_naive`，detail/summary 查询条件统一换算为 UTC aware；入库/游标仍存本地 naive；硬编码 Asia/Shanghai 改为读 `settings.TIME_ZONE` | `sql/collectors/mongo_collector.py` |
| 6 | **下载权限**：`DownloadFileView` 删除 `sqlexport_submit` 分支（仅保留超管/offline_download/提交人）；工单 `file_name` 为空拒绝下载 | `sql_api/api_misc.py` |
| 7 | **api_diagnostic**：`import _json` → `import json`（修复 kill 会话 500）；新增 `_safe_int`/`_parse_thread_ids` 并应用 | `sql_api/api_diagnostic.py` |
| 8 | **IDOR 组**：`WorkflowAuditList` 非超管强制 `engineer=request.user`；`WorkflowLogList` 按三类工单补归属校验（SQL 走 `can_view`，归档/查询申请按资源组/提交人）；`BackupSqlView` 走 `can_view`、`OscControlView` 走 `can_execute`；`ArchiveDetail`/`ArchiveLogView` 按资源组收口 + `_safe_int`/404；`QueryPrivApplyDetail` 提交人/同组校验 + 404；参数管理 4 视图改 `resolve_instance`（含 ParamHistory 丢实例过滤的既有 bug）；资源组创建/更新/删除(api_user)仅超管；`InstancesView`/`AuditorsView` 收口为本人所在组（工单提交常规流程不受影响）；`ExecuteCheck` 补 `user_instances(can_write)` 校验 | `sql_api/api_workflow.py`、`sql_api/api_misc.py`、`sql_api/api_archiver.py`、`sql_api/api_query_priv.py`、`sql_api/api_instance_admin.py`、`sql_api/api_user.py`、`sql_api/api_resource_group.py` |
| 9 | **标识符/权限注入**：`GenerateSqlView` 表名白名单 `[\w$.]{1,128}` + 样本查询走平台 `query_masking`；`AccountGrantView` 库/表/列名 `_quote_ident`（白名单+反引号）、权限名 `_sanitize_privs`（MySQL 静态权限白名单）；`DatabaseCreateView` 库名同样转义 | `sql_api/api_misc.py`、`sql_api/api_instance_admin.py` |
| 10 | **部署三连**：mysql:5.7→8.0（`--default-authentication-plugin=mysql_native_password`，注明既有数据卷需原地升级）、redis:5→7；移除遮蔽镜像迁移文件的 migrations 挂载；`.env.example`/`.env.list` 的 `Q_CLUISTER_*` →`Q_CLUSTER_*`，`.env.list` 禁用指向 127.0.0.1 的无效 CAS 配置、删除无效 `SECURE_SSL_REDIRECT`；mysql healthcheck 改 `-h 127.0.0.1` 走 TCP（消除初始化期假阳性） | `src/docker-compose/docker-compose.yml`、`.env.example`、`.env.list` |

### 验证情况
- `manage.py check`：0 错误（改后复跑通过）。
- 31 项纯逻辑冒烟测试全部通过：kill 线程 ID 校验、EXPLAIN 闸门（SELECT/WITH 放行、DELETE/ANALYZE/OUTFILE/注释剥离/多语句截断拒绝、字面量脱敏）、Mongo 时区换算（本地↔UTC 差 8 小时往返一致）、`_safe_int`/`_parse_thread_ids`、GRANT 权限白名单与标识符转义、v1 与 v2 循环导入。
- pytest 与 `sql/tests.py`/`test_archiver.py` 收集已恢复（此前因 `sql.archiver`/`sql.binlog`/`sql.query` 缺失无法收集）；**DB 集成用例在本机因远程测试库（192.168.0.251:13306）连接中途断开（MySQLdb 2006）全部失败，属既有环境问题，非本次改动引起**（失败均发生在 setUp 阶段数据库层，未触及本次改动的任何逻辑）。
- docker-compose.yml 的 mysql 8.0 升级与镜像变更**未在本机实际启动验证**（无 Docker 环境），需在部署机确认既有 5.7 数据卷升级路径（8.0 首次启动会自动执行升级检查）。
