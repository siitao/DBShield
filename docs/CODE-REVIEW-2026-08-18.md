# DBShield (Archery fork) 项目代码审查报告

| 项 | 内容 |
|---|---|
| 审查日期 | 2026-08-18 |
| 分支 | master (`e936e92`) |
| 审查基线 | `2d549ea`（08-12 全项目审查 HEAD）|
| 审查范围 | `2d549ea..e936e92` 共 4 提交：ed9e43e（H1-H6 安全修复）、eda60e8（镜像/主题）、6d9166f（两级构建）、e936e92（.env 脱敏+redis healthcheck） |
| 审查方式 | 逐提交 diff + 关键修复点全链路代码核实（视图→序列化器→模型→前端→存储） |
| 总体结论 | **H1/H2/H3/H5 修复有效**；**H4 修而不尽**（权限 OR 缺口）；**H6 已闭环**（H6a 复核为误报，H6b/H6c 本次修复并经测试验证） |

---

## 一、历史问题修复验证（对照 CODE-REVIEW-2026-08-12.md）

| 上次问题 | 现状 | 判定 |
|---|---|---|
| H1 工单审核/执行冒名 | `user=request.user` 为操作主体，`engineer` 仅超管可覆盖；`can_execute`/`can_review` 后续校验仍生效 | ✅ 已修 |
| H2 2FA 越权接管 | 已登录分支强制 `engineer==request.user.username`（超管除外）；未登录临时会话分支保留（session 由密码校验生成）；`TwoFASave/State` 仍 `IsOwner` | ✅ 已修 |
| H3 My2sql 越权+泄漏 | `resolve_instance`（资源组校验）+ `My2sqlPermission`（menu_my2sql）+ 通用错误文案（stderr 不再回显） | ✅ 已修 |
| H4 任意文件下载 | 增加 workflow_id 必填 + `file_name==workflow.file_name` 归属校验 + 权限 OR；**但 OR 里 `sqlexport_submit` 重新打开越权面** | ⚠️ 修而不尽 |
| H5 slow_log 清理必崩 | `sql=` 关键字 + `ResultSet.affected_rows` 回填贯通（MysqlEngine.execute 回填，唯一读取方 tasks.py:170） | ✅ 已修 |
| H6 Aliyun RDS 二次加密 | 复核结论：**误报**——mirage `Crypto.encrypt` 幂等，修 PUT 赋值路径 + `key_secret` write_only 后无二次加密；但引入前端无法编辑 RDS 的回归 + List 权限漏收（H6b/H6c 本次已修） | ⚠️→✅ 已闭环 |

---

## 二、🔴 High 级问题

### H6a · Aliyun RDS `key_secret` 二次加密 bug 原样存在（未修复）
- 位置：`sql/models.py:947-950`（`CloudAccessKey.save()`）+ `sql_api/api_instance.py:212-233`（`AliyunRdsDetail.put`）
- **根因**：`CloudAccessKey.save()` 对 `key_id`/`key_secret` **无条件** `self.c.encrypt()`。修复后的 PUT 只在显式传 `key_secret` 时才赋值，但**只要改任意字段触发 `obj.ak.save()`，库里既有的 key_secret 密文就被再加密一次**。
- **触发**：前端 `List.vue:244-248` 总是回传 `ak`（key_id/remark 非空 → `ak_changed=True` → save()）→ key_secret 二次加密。
- **后果**：密文每次编辑都叠加加密 → `raw_key_id`/`raw_key_secret` 解密失败 → 登录阿里云失败、`to_representation` 的 `except: pass` 返回乱码密文。
- 建议：`save()` 用 `prepare = key_id != self.key_id`（首次新建才加密），或 PUT 里改密字段时从库取明文再赋，或改用 `CloudAccessKey.objects.create` 专有流程 + 字段级加解密。

### H6b · `key_secret` write_only 与前端必填校验冲突 → 无法编辑 RDS 配置（功能回归）
- 位置：`serializers.py:228-230`（write_only）+ `frontend/src/views/instance/List.vue:237`
- **链路**：GET 回填 `rds.ak?.key_secret` 恒为 `""`（write_only 不回显）→ 前端 `!rdsForm.key_secret.trim()` 必填校验**直接拦截保存** → 编辑已有 RDS 配置 100% 失败（提示 "RDS 配置不完整"）。
- **后果**：实例编辑弹窗里 RDS 部分**永远保存失败**（新增不受影响，因为初始手填）。
- 建议：前端改"编辑时 key_secret 留空 = 保持不变"（仿照 instance 密码的处理）；后端 PUT 兼容空 key_secret（已兼容，见 H6a）。

### H6c · `AliyunRdsList` 权限未收（H6 漏网）
- 位置：`sql_api/api_instance.py:147-180`
- `AliyunRdsList`（GET 列出全部 + POST 创建）仍 `permissions.IsAuthenticated`，H6 只改了 `AliyunRdsDetail`/`AliyunRdsByInstance`。
- 任意已登录用户可 **GET 全部 RDS 配置**（`to_representation` 返回解密后的 `key_id` 明文）+ **POST 创建**指向任意实例的 RDS 配置。
- 建议：与 H6 一致改为 `AliyunRdsConfigPermission`（超管），或至少 POST 走 `IsApiSystemAdmin`。

### H4b · 下载权限 OR 里 `sqlexport_submit` 重新打开越权面
- 位置：`sql_api/api_misc.py:999-1004`（`DownloadFileView.get`）
- 权限条件为 **OR**：`is_superuser or has_perm(sqlexport_submit) or has_perm(offline_download) or workflow.engineer==user.username`。
- 拥有 `sqlexport_submit`（提交数据导出）的普通用户可下载**任意用户**的导出文件——只要知道 `workflow_id`（工单列表可见）+ `file_name`（工单字段可见，且恒等于 `workflow.file_name` 校验通过）。
- `workflow.engineer==username` 分支已覆盖"自己提交的文件"，故 `sqlexport_submit` 分支纯属画蛇添足。
- 建议：删掉 `sqlexport_submit` 分支，仅保留 `offline_download` 权限 + 提交人/超管。

---

## 三、🟠 Medium 级问题

### M1 · requirements.txt 阿里云 Redis SDK 降版未说明原因
- `aliyun-python-sdk-r-kvstore 3.5.0 → 2.20.17`。`aliyun_redis.py:10,153` 用的 `DescribeSlowLogRecordsRequest`（v20150101）在 2.x 仍存在，兼容性大概率 OK，但**降版原因无 commit message 说明**（可能是构建失败规避）。建议补注释说明。

### M2 · docker-compose mysql healthcheck 仍可能失败
- `docker-compose.yml:30-34`：`mysqladmin ping` 未带 `-uroot -p123456`。`MYSQL_ROOT_PASSWORD` 设置后 root 有密码，`mysqladmin ping` 不带密码在部分 MySQL 5.7 配置下认证失败 → mysql 永远 unhealthy → dbshield 起不来。
- 本次只修了 redis healthcheck（正确），mysql 的属**既有问题未覆盖**，顺带提示。

---

## 四、🟡 Low / Info

- `Dockerfile:31-36` 新增阿里云镜像源 `sed` 替换 deb 源 + `PIP_INDEX_URL`：无锁版本漂移风险（apt 源替换 deb.debian.org→aliyun 若 build 环境不连通阿里云会失败），但属国内部署常规做法，可接受。
- `docker-base-image.yml` 推 `ghcr.io/${{ github.repository }}-base` + `docker-image.yml` 本地 build base 现产现用：两级构建在 CI 里**未先 build+push base 再 pull**，而是 `docker build -f Dockerfile-base -t dbshield-base:v1 .`（本地 tag），buildx 多平台下可能不可用（仅 linux/amd64）。Low。
- `SqlReviewTable.vue:188-192`：`ai_suggestion` 改用 `marked + DOMPurify.sanitize` ✅，`marked.setOptions({gfm:true, breaks:false})` 配置合理，与 `DiagnosisDrawer.vue` 一致。
- `theme.scss` `--el-color-primary-rgb` 补丁：✅ 正确（Element Plus 半透明依赖 rgb 分量）。
- redis healthcheck `redis-cli -a 123456 ping`：✅ 正确修复（NOAUTH 原因到位）。
- `.env` → `.env.example` 重命名 + .gitignore + README 警告：✅ 合理，消除了「运行时写回真实密钥被 git add」风险面。

---

## 五、亮点

- H1 补丁 `user=request.user` + 超管显式覆盖，写得很克制：没有破坏 `can_execute`（sql_review.py:13）/`can_review`（workflow_audit.py:720）的既有校验链。
- H2 补丁保留未登录临时会话分支（session 由 `api_auth.py:77-81` 密码校验建立，5 分钟过期），登录流程语义未被破坏。
- H3 补丁用 `resolve_instance`（instance_service.py:15）统一资源组校验，与全项目模式一致；错误文案脱敏到位。
- H5 补丁把 `sql=` 关键字、`ResultSet.affected_rows` 回填（mysql.py:1098）、唯一读取方（tasks.py:170）三点串成闭环，改动最小化。
- `SqlReviewTable.vue` XSS 修复用 DOMPurify 而非信任 marked 输出，正确。

---

## 六、修复优先级 Top-5

| # | 级别 | 位置 | 问题 |
|---|---|---|---|
| 1 | ~~High~~ | ~~`models.py:947-950` + `api_instance.py:212-233`~~ | ~~H6a：二次加密 bug~~ **（2026-08-18 复核确认：误报，mirage `Crypto.encrypt` 幂等，改 key_id 后 key_secret 密文不损坏，实测测试通过，无需修复）** |
| 2 | High | `List.vue:237` + `serializers.py:228-230` | H6b：write_only 与前端必填校验冲突 → RDS 配置无法编辑（**已修**：编辑留空=保持原值） |
| 3 | High | `api_instance.py:147-180` | H6c：`AliyunRdsList` 仍 `IsAuthenticated`（**已修**：权限类前移 + 改超管，普通用户 GET/POST 403 实测通过） |
| 4 | High | `api_misc.py:999-1004` | H4b：下载权限 OR 含 `sqlexport_submit`，越权下载他人导出文件（未修，不在本次范围） |
| 5 | Medium | `docker-compose.yml:30-34` | mysql healthcheck 未带密码，可能永 unhealthy（未修） |

---

## 七、H6 修复记录（2026-08-18）

按用户要求仅修复 H6。三个子项复核后的处置：

### H6a · 二次加密 —— ❌ 误报，未改代码
- 实测 mirage `Crypto.encrypt`（`venv/Lib/site-packages/mirage/crypto.py:79-86`）是幂等的：先 `decrypt(text)` 试探，若已是密文则原样返回。`CloudAccessKey.save()` 对密文再次 encrypt 不会损坏。
- 回归保护测试 `test_h6a_cloudaccesskey_no_double_encrypt` **PASSED**：`ak.key_id = "k2"; ak.save()` 后 `ak.raw_key_secret == "secret1"` 仍成立。
- 连带结论：08-12 报告 H6 中的"二次加密 bug"判断本身也不成立。

### H6b · 前端无法编辑 RDS —— ✅ 已修
- 位置：`frontend/src/views/instance/List.vue:237`（saveRds 校验）
- 改动：新增必须填 key_secret；**编辑已有 RDS 时允许留空＝保持原值**（后端 PUT 已对空/缺省 key_secret 跳过赋值，链路自洽）。

### H6c · AliyunRdsList 越权 —— ✅ 已修
- 位置：`sql_api/api_instance.py`
- 改动：`AliyunRdsConfigPermission` 前移至 `AliyunRdsList` 之前（消除类体引用 NameError），`AliyunRdsList.permission_classes` 改为 `[AliyunRdsConfigPermission]`（与 Detail/ByInstance 一致）。
- 验证：pytest 通过（普通用户 GET/POST rds 列表 403，超管 200/201）；`manage.py check` 0 错误；前端 `vue-tsc --noEmit` 通过。
- 说明：前端实例操作列已 `v-if="auth.isSuperuser"` 门控（`List.vue:352`），非超管原本看不到编辑入口，权限收紧无功能回退。

### 测试环境注记
- `sql_api/tests.py` 中 RDS 相关用例在本地测试库报 `mysql_slow_query_summary doesn't exist`——该表 `managed=False`（由采集任务建表），测试库迁移不建它，属既有环境问题（clean 基线同样失败），与本次改动无关。
