/** 侧边栏菜单定义，结构对齐旧版 base.html，按 perms.sql.menu_* 权限位显隐。
 * routeName：已迁移到 SPA 的页面（内部路由跳转）。
 * legacyPath：尚未迁移的页面，在新标签打开旧版（迁移期共存）。
 * superuserOnly：仅超管可见（与路由 meta.requireSuperuser 同口径）。 */
export interface MenuItem {
  title: string;
  icon?: string;
  perm?: string;
  routeName?: string;
  legacyPath?: string;
  superuserOnly?: boolean;
  children?: MenuItem[];
}

export const menuItems: MenuItem[] = [
  {
    title: "Dashboard",
    icon: "Odometer",
    perm: "sql.menu_dashboard",
    routeName: "dashboard",
  },
  {
    title: "待办工作流",
    icon: "Bell",
    perm: "sql.sql_review",
    routeName: "todoworkflow",
  },
  {
    title: "SQL 审核",
    icon: "CircleCheck",
    perm: "sql.menu_sqlcheck",
    children: [
      { title: "SQL 上线", perm: "sql.menu_sqlworkflow", routeName: "sqlworkflow-list" },
      { title: "SQL 分析", perm: "sql.menu_sqlanalyze", routeName: "sqlanalyze" },
    ],
  },
  {
    title: "SQL 查询",
    icon: "Search",
    perm: "sql.menu_query",
    children: [
      { title: "在线查询", perm: "sql.menu_sqlquery", routeName: "sqlquery-index" },
      { title: "数据导出", perm: "sql.menu_sqlexportworkflow", routeName: "sqlexport" },
      { title: "数据字典", perm: "sql.menu_data_dictionary", routeName: "datadictionary" },
      { title: "权限管理", perm: "sql.menu_queryapplylist", routeName: "queryapplylist" },
    ],
  },
  {
    title: "SQL 优化",
    icon: "Tools",
    perm: "sql.menu_sqloptimize",
    children: [
      { title: "优化工具", perm: "sql.menu_sqladvisor", routeName: "sqladvisor" },
      { title: "慢查日志", perm: "sql.menu_slowquery", routeName: "slowquery" },
    ],
  },
  {
    title: "实例管理",
    icon: "Coin",
    perm: "sql.menu_instance",
    children: [
      { title: "实例列表", perm: "sql.menu_instance_list", routeName: "instance-list" },
      { title: "会话管理", perm: "sql.menu_dbdiagnostic", routeName: "dbdiagnostic" },
      { title: "数据库管理", perm: "sql.menu_database", routeName: "database" },
      { title: "账号管理", perm: "sql.menu_instance_account", routeName: "instanceaccount" },
      { title: "参数配置", perm: "sql.menu_param", routeName: "instanceparam" },
      { title: "参数对比", perm: "sql.menu_param_compare", routeName: "paramcompare" },
    ],
  },
  {
    title: "工具插件",
    icon: "Connection",
    perm: "sql.menu_tools",
    children: [
      { title: "PTArchiver", perm: "sql.menu_archive", routeName: "archive" },
      { title: "My2SQL", perm: "sql.menu_my2sql", routeName: "my2sql" },
      { title: "SchemaSync", perm: "sql.menu_schemasync", routeName: "schemasync" },
    ],
  },
  {
    title: "系统管理",
    icon: "Setting",
    perm: "sql.menu_system",
    children: [
      { title: "配置项管理", routeName: "config" },
      { title: "认证配置", routeName: "authconfig" },
      { title: "AI 用量", routeName: "aiusage", superuserOnly: true },
      { title: "资源组管理", routeName: "resourcegroup" },
    ],
  },
  {
    title: "系统审计",
    icon: "View",
    perm: "sql.audit_user",
    children: [
      { title: "通用审计", routeName: "audit" },
      { title: "SQL 上线审计", routeName: "audit" },
      { title: "查询审计", routeName: "audit" },
    ],
  },
  {
    title: "相关文档",
    icon: "Document",
    perm: "sql.menu_document",
    routeName: "document",
  },
  {
    title: "OpenAPI",
    icon: "Promotion",
    perm: "sql.menu_openapi",
    routeName: "openapi",
  },
];
