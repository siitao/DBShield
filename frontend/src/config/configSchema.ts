/**
 * 配置项管理 schema —— 103 个配置项的元数据定义。
 * 从 common/templates/config.html 提取并整理，由 config/Index.vue 动态渲染表单。
 *
 * 每个字段定义：key, label(中文), type, section, subsection?, desc(placeholder),
 * options?(静态), dynamic?(运行时数据源 key), showWhen?(条件显隐).
 */

export type FieldType =
  | "text"
  | "number"
  | "boolean"
  | "password"
  | "select"
  | "multiselect";

export interface ConfigField {
  key: string;
  label: string;
  type: FieldType;
  section: string;
  subsection?: string;
  desc?: string;
  options?: { value: string; label: string }[];
  /** 运行时数据源 key（form 组件根据 key 加载选项并传入） */
  dynamic?: "tags" | "groups" | "resourceGroups" | "users" | "dbTypes";
  /** 条件显隐：当该 key 的值等于 value 时才显示此字段 */
  showWhen?: { key: string; value: string | boolean };
}

// ============ 配置分区顺序 ============
export const CONFIG_SECTIONS = [
  "goInception 配置",
  "功能模块配置",
  "通知配置",
  "AI 配置",
  "其他配置",
] as const;

export const CONFIG_SUBSECTIONS: Record<string, string[]> = {
  "功能模块配置": ["SQL上线", "SQL查询", "数据导出", "存储配置", "SQL优化"],
  "通知配置": ["基础通知", "钉钉通知", "企业微信通知", "短信发送"],
  "其他配置": ["基础配置", "慢日志配置"],
};

// 静态选项
const STORAGE_TYPES = [
  { value: "local", label: "本地存储" },
  { value: "sftp", label: "SFTP" },
  { value: "s3c", label: "S3兼容存储" },
  { value: "azure", label: "Azure Blob" },
];

const SMS_PROVIDERS = [
  { value: "aliyun", label: "阿里云短信" },
  { value: "tencent", label: "腾讯云短信" },
];

const DB_TYPES = [
  "mysql", "mariadb", "mssql", "redis", "pgsql", "oracle", "mongo",
  "clickhouse", "phoenix", "odps", "cassandra", "doris", "elasticsearch",
  "opensearch", "memcached", "tdengine",
];

// ============ 103 个字段定义 ============
export const CONFIG_FIELDS: ConfigField[] = [
  // ── goInception 配置 ──────────────────────────────────
  { key: "go_inception_host", label: "goInception 地址", type: "text", section: "goInception 配置", desc: "goInception 服务地址" },
  { key: "go_inception_port", label: "goInception 端口", type: "number", section: "goInception 配置", desc: "goInception 服务端口" },
  { key: "go_inception_user", label: "goInception 用户名", type: "text", section: "goInception 配置" },
  { key: "go_inception_password", label: "goInception 密码", type: "password", section: "goInception 配置" },
  { key: "inception_remote_backup_host", label: "备份库地址", type: "text", section: "goInception 配置" },
  { key: "inception_remote_backup_port", label: "备份库端口", type: "number", section: "goInception 配置" },
  { key: "inception_remote_backup_user", label: "备份库用户", type: "text", section: "goInception 配置" },
  { key: "inception_remote_backup_password", label: "备份库密码", type: "password", section: "goInception 配置" },

  // ── 功能模块 / SQL上线 ──────────────────────────────────
  { key: "critical_ddl_regex", label: "禁止提交 DDL 正则", type: "text", section: "功能模块配置", subsection: "SQL上线", desc: "正则条件，匹配的语句会禁止提交" },
  { key: "auto_review_wrong", label: "自动驳回等级", type: "number", section: "功能模块配置", subsection: "SQL上线", desc: "1=警告驳回，2/空=错误驳回，其他不驳回" },
  { key: "enable_backup_switch", label: "是否开启备份选项", type: "boolean", section: "功能模块配置", subsection: "SQL上线", desc: "默认关闭，强制要求备份" },
  { key: "auto_review", label: "自动审批开关", type: "boolean", section: "功能模块配置", subsection: "SQL上线", desc: "是否开启SQL上线自动审批" },
  { key: "auto_review_tag", label: "自动审批实例标签", type: "multiselect", section: "功能模块配置", subsection: "SQL上线", dynamic: "tags", showWhen: { key: "auto_review", value: true } },
  { key: "auto_review_db_type", label: "自动审批数据库类型", type: "multiselect", section: "功能模块配置", subsection: "SQL上线", options: DB_TYPES.map((t) => ({ value: t, label: t })), showWhen: { key: "auto_review", value: true } },
  { key: "auto_review_regex", label: "自动审批过滤正则", type: "text", section: "功能模块配置", subsection: "SQL上线", desc: "匹配的语句需要人工审批", showWhen: { key: "auto_review", value: true } },
  { key: "auto_review_max_update_rows", label: "最大更新行数", type: "number", section: "功能模块配置", subsection: "SQL上线", desc: "自动审批允许的最大更新行数", showWhen: { key: "auto_review", value: true } },
  { key: "manual", label: "手工执行确认", type: "boolean", section: "功能模块配置", subsection: "SQL上线", desc: "是否开启SQL上线手工执行确认" },
  { key: "ddl_dml_separation", label: "DDL/DML 禁止同时提交", type: "boolean", section: "功能模块配置", subsection: "SQL上线", desc: "是否禁止DDL和DML在SQL上线同时提交(MySQL)" },
  { key: "ban_self_audit", label: "禁止自审工单", type: "boolean", section: "功能模块配置", subsection: "SQL上线", desc: "ON=禁止自审（管理员除外）" },
  { key: "real_row_count", label: "获取DML真实影响行数", type: "boolean", section: "功能模块配置", subsection: "SQL上线", desc: "MySQL、MongoDB" },

  // ── 功能模块 / SQL查询 ──────────────────────────────────
  { key: "data_masking", label: "开启动态脱敏", type: "boolean", section: "功能模块配置", subsection: "SQL查询" },
  { key: "query_check", label: "开启脱敏校验", type: "boolean", section: "功能模块配置", subsection: "SQL查询", desc: "无法脱敏的语句会抛错" },
  { key: "disable_star", label: "禁止查询使用 *", type: "boolean", section: "功能模块配置", subsection: "SQL查询" },
  { key: "max_execution_time", label: "查询超时时间(秒)", type: "number", section: "功能模块配置", subsection: "SQL查询", desc: "默认60" },
  { key: "admin_query_limit", label: "管理员查询结果限制", type: "number", section: "功能模块配置", subsection: "SQL查询" },

  // ── 功能模块 / 数据导出 ──────────────────────────────────
  { key: "max_export_rows", label: "导出数据量阈值", type: "number", section: "功能模块配置", subsection: "数据导出", desc: "默认10000行" },

  // ── 功能模块 / 存储配置（按 storage_type 条件显隐） ──────────────────────────────────
  { key: "storage_type", label: "存储类型", type: "select", section: "功能模块配置", subsection: "存储配置", options: STORAGE_TYPES },
  // SFTP
  { key: "sftp_host", label: "SFTP 主机", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "sftp" } },
  { key: "sftp_port", label: "SFTP 端口", type: "number", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "sftp" } },
  { key: "sftp_user", label: "SFTP 用户名", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "sftp" } },
  { key: "sftp_password", label: "SFTP 密码", type: "password", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "sftp" } },
  { key: "sftp_path", label: "SFTP 路径", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "sftp" } },
  { key: "sftp_custom_params", label: "SFTP 自定义参数", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "sftp" } },
  // S3
  { key: "s3c_access_key_id", label: "S3 Access Key", type: "password", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "s3c" } },
  { key: "s3c_access_key_secret", label: "S3 Secret Key", type: "password", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "s3c" } },
  { key: "s3c_endpoint", label: "S3 Endpoint URL", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "s3c" } },
  { key: "s3c_region", label: "S3 Region", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "s3c" } },
  { key: "s3c_bucket_name", label: "S3 Bucket", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "s3c" } },
  { key: "s3c_path", label: "S3 Path", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "s3c" } },
  { key: "s3c_custom_params", label: "S3 自定义参数", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "s3c" } },
  // Azure
  { key: "azure_container", label: "Azure 容器", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "azure" } },
  { key: "azure_account_name", label: "Azure 账号名", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "azure" } },
  { key: "azure_account_key", label: "Azure 账号密钥", type: "password", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "azure" } },
  { key: "azure_path", label: "Azure Path", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "azure" } },
  { key: "azure_custom_params", label: "Azure 自定义参数", type: "text", section: "功能模块配置", subsection: "存储配置", showWhen: { key: "storage_type", value: "azure" } },

  // ── 功能模块 / SQL优化 ──────────────────────────────────
  { key: "sqladvisor", label: "SQLAdvisor 路径", type: "text", section: "功能模块配置", subsection: "SQL优化" },
  { key: "soar", label: "SOAR 路径", type: "text", section: "功能模块配置", subsection: "SQL优化" },
  { key: "soar_test_dsn", label: "SOAR 测试DSN", type: "text", section: "功能模块配置", subsection: "SQL优化" },

  // ── 通知配置 / 基础通知 ──────────────────────────────────
  { key: "archery_base_url", label: "Archery 访问地址", type: "text", section: "通知配置", subsection: "基础通知" },
  { key: "ddl_notify_auth_group", label: "DDL通知权限组", type: "select", section: "通知配置", subsection: "基础通知", dynamic: "groups" },
  { key: "notify_phase_control", label: "通知阶段控制", type: "select", section: "通知配置", subsection: "基础通知", options: [
    { value: "true", label: "全流程通知" },
    { value: "false", label: "不通知" },
  ]},
  { key: "mail", label: "邮件通知开关", type: "boolean", section: "通知配置", subsection: "基础通知" },
  { key: "mail_ssl", label: "邮件 SSL", type: "boolean", section: "通知配置", subsection: "基础通知", showWhen: { key: "mail", value: true } },
  { key: "mail_smtp_server", label: "SMTP 服务器", type: "text", section: "通知配置", subsection: "基础通知", showWhen: { key: "mail", value: true } },
  { key: "mail_smtp_port", label: "SMTP 端口", type: "number", section: "通知配置", subsection: "基础通知", showWhen: { key: "mail", value: true } },
  { key: "mail_smtp_user", label: "SMTP 用户名", type: "text", section: "通知配置", subsection: "基础通知", showWhen: { key: "mail", value: true } },
  { key: "mail_smtp_password", label: "SMTP 密码", type: "password", section: "通知配置", subsection: "基础通知", showWhen: { key: "mail", value: true } },

  // ── 通知配置 / 钉钉 ──────────────────────────────────
  { key: "ding", label: "钉钉通知开关", type: "boolean", section: "通知配置", subsection: "钉钉通知" },
  { key: "ding_to_person", label: "钉钉通知到个人", type: "boolean", section: "通知配置", subsection: "钉钉通知", showWhen: { key: "ding", value: true } },
  { key: "ding_agent_id", label: "Agent ID", type: "text", section: "通知配置", subsection: "钉钉通知", showWhen: { key: "ding", value: true } },
  { key: "ding_app_key", label: "App Key", type: "text", section: "通知配置", subsection: "钉钉通知", showWhen: { key: "ding", value: true } },
  { key: "ding_app_secret", label: "App Secret", type: "password", section: "通知配置", subsection: "钉钉通知", showWhen: { key: "ding", value: true } },
  { key: "ding_archery_username", label: "钉钉用户名字段", type: "text", section: "通知配置", subsection: "钉钉通知", showWhen: { key: "ding_to_person", value: true } },
  { key: "ding_dept_ids", label: "部门ID", type: "text", section: "通知配置", subsection: "钉钉通知", desc: "多个用逗号分隔", showWhen: { key: "ding", value: true } },

  // ── 通知配置 / 企业微信 ──────────────────────────────────
  { key: "wx", label: "企业微信通知开关", type: "boolean", section: "通知配置", subsection: "企业微信通知" },
  { key: "wx_corpid", label: "Corp ID", type: "text", section: "通知配置", subsection: "企业微信通知", showWhen: { key: "wx", value: true } },
  { key: "wx_agent_id", label: "Agent ID", type: "text", section: "通知配置", subsection: "企业微信通知", showWhen: { key: "wx", value: true } },
  { key: "wx_app_secret", label: "App Secret", type: "password", section: "通知配置", subsection: "企业微信通知", showWhen: { key: "wx", value: true } },
  { key: "qywx_webhook", label: "企业微信 Webhook", type: "boolean", section: "通知配置", subsection: "企业微信通知" },
  { key: "feishu_webhook", label: "飞书 Webhook", type: "boolean", section: "通知配置", subsection: "企业微信通知" },
  { key: "feishu", label: "飞书应用通知", type: "boolean", section: "通知配置", subsection: "企业微信通知" },
  { key: "feishu_appid", label: "飞书 App ID", type: "text", section: "通知配置", subsection: "企业微信通知", showWhen: { key: "feishu", value: true } },
  { key: "feishu_app_secret", label: "飞书 App Secret", type: "text", section: "通知配置", subsection: "企业微信通知", showWhen: { key: "feishu", value: true } },
  { key: "generic_webhook_url", label: "通用 Webhook URL", type: "text", section: "通知配置", subsection: "企业微信通知" },

  // ── 通知配置 / 短信 ──────────────────────────────────
  { key: "sms_provider", label: "短信服务商", type: "select", section: "通知配置", subsection: "短信发送", options: SMS_PROVIDERS },
  // 阿里云短信
  { key: "aliyun_access_key_id", label: "阿里云 Access Key ID", type: "password", section: "通知配置", subsection: "短信发送", showWhen: { key: "sms_provider", value: "aliyun" } },
  { key: "aliyun_access_key_secret", label: "阿里云 Access Key Secret", type: "password", section: "通知配置", subsection: "短信发送", showWhen: { key: "sms_provider", value: "aliyun" } },
  { key: "aliyun_sign_name", label: "阿里云短信签名", type: "text", section: "通知配置", subsection: "短信发送", showWhen: { key: "sms_provider", value: "aliyun" } },
  { key: "aliyun_template_code", label: "阿里云模板代码", type: "text", section: "通知配置", subsection: "短信发送", showWhen: { key: "sms_provider", value: "aliyun" } },
  { key: "aliyun_variable_name", label: "阿里云变量名", type: "text", section: "通知配置", subsection: "短信发送", showWhen: { key: "sms_provider", value: "aliyun" } },
  // 腾讯云短信
  { key: "tencent_secret_id", label: "腾讯云 Secret ID", type: "password", section: "通知配置", subsection: "短信发送", showWhen: { key: "sms_provider", value: "tencent" } },
  { key: "tencent_secret_key", label: "腾讯云 Secret Key", type: "password", section: "通知配置", subsection: "短信发送", showWhen: { key: "sms_provider", value: "tencent" } },
  { key: "tencent_sign_name", label: "腾讯云短信签名", type: "text", section: "通知配置", subsection: "短信发送", showWhen: { key: "sms_provider", value: "tencent" } },
  { key: "tencent_template_id", label: "腾讯云模板ID", type: "text", section: "通知配置", subsection: "短信发送", showWhen: { key: "sms_provider", value: "tencent" } },
  { key: "tencent_sdk_appid", label: "腾讯云 SDKAppID", type: "text", section: "通知配置", subsection: "短信发送", showWhen: { key: "sms_provider", value: "tencent" } },

  // OIDC 配置已迁移到「系统管理 → 认证配置」页面（含 oidc_btn_name 等）

  // ── AI 配置 ──────────────────────────────────
  { key: "ai_review_enabled", label: "开启 AI SQL 审核", type: "boolean", section: "AI 配置", desc: "开启后工单 SQL 检测会调用 AI 给出风险评分与建议（仅参考，不阻断提交）" },
  { key: "enable_ai_slowquery_diagnosis", label: "开启 AI 慢查诊断", type: "boolean", section: "AI 配置", desc: "开启后慢查页面可使用 AI 根因诊断（需配合 sql.use_ai_diagnosis 权限）" },
  { key: "openai_base_url", label: "AI 服务商地址", type: "text", section: "AI 配置", desc: "OpenAI 兼容的 API 地址，如 https://api.openai.com/v1" },
  { key: "openai_api_key", label: "API Key", type: "password", section: "AI 配置" },
  { key: "default_chat_model", label: "默认模型", type: "text", section: "AI 配置", desc: "如 gpt-3.5-turbo、gpt-4o、deepseek-chat 等" },
  { key: "default_query_template", label: "查询模板", type: "text", section: "AI 配置", desc: "生成SQL的提示词模板" },

  // ── 其他配置 / 基础配置 ──────────────────────────────────
  { key: "index_path_url", label: "首页地址", type: "text", section: "其他配置", subsection: "基础配置" },
  { key: "my2sql", label: "my2sql 路径", type: "text", section: "其他配置", subsection: "基础配置" },
  { key: "default_auth_group", label: "默认权限组", type: "select", section: "其他配置", subsection: "基础配置", dynamic: "groups" },
  { key: "default_resource_group", label: "默认资源组", type: "select", section: "其他配置", subsection: "基础配置", dynamic: "resourceGroups" },
  { key: "api_user_whitelist", label: "API 用户白名单", type: "select", section: "其他配置", subsection: "基础配置", dynamic: "users" },
  { key: "lock_time_threshold", label: "锁定时间阈值", type: "number", section: "其他配置", subsection: "基础配置" },
  { key: "lock_cnt_threshold", label: "锁定次数阈值", type: "number", section: "其他配置", subsection: "基础配置" },
  { key: "watermark_enabled", label: "开启水印", type: "boolean", section: "其他配置", subsection: "基础配置" },
  { key: "enforce_2fa", label: "强制 2FA", type: "boolean", section: "其他配置", subsection: "基础配置" },
  { key: "announcement_content_enabled", label: "公告开关", type: "boolean", section: "其他配置", subsection: "基础配置" },
  { key: "announcement_content", label: "公告内容", type: "text", section: "其他配置", subsection: "基础配置", showWhen: { key: "announcement_content_enabled", value: true } },
  { key: "custom_title_suffix", label: "自定义标题后缀", type: "text", section: "其他配置", subsection: "基础配置" },

  // ── 其他配置 / 慢日志配置 ──────────────────────────────────
  { key: "slow_query_retention_days", label: "数据保留天数", type: "number", section: "其他配置", subsection: "慢日志配置", desc: "超过此天数的慢查询明细数据将被自动清理，默认30天" },
  { key: "slow_query_cleanup_batch_size", label: "每批删除数量", type: "number", section: "其他配置", subsection: "慢日志配置", desc: "批量删除时每批处理的记录数，默认5000" },
  { key: "slow_query_cleanup_batch_sleep", label: "批次间隔(秒)", type: "number", section: "其他配置", subsection: "慢日志配置", desc: "每批删除后的等待秒数，避免数据库压力过大，默认1秒" },
];
