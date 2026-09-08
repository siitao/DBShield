import {
  createRouter,
  createWebHistory,
  type RouteRecordRaw,
} from "vue-router";
import { useAuthStore } from "@/stores/auth";

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "login",
    component: () => import("@/views/Login.vue"),
    meta: { public: true },
  },
  {
    path: "/",
    component: () => import("@/layouts/BasicLayout.vue"),
    redirect: "/dashboard",
    children: [
      {
        path: "dashboard",
        name: "dashboard",
        component: () => import("@/views/dashboard/Index.vue"),
        meta: { title: "Dashboard" },
      },
      {
        path: "todoworkflow",
        name: "todoworkflow",
        component: () => import("@/views/todoworkflow/List.vue"),
        meta: { title: "待办工作流", perm: "sql.sql_review" },
      },
      {
        path: "instance",
        name: "instance-list",
        component: () => import("@/views/instance/List.vue"),
        meta: { title: "实例列表", perm: "sql.menu_instance_list", requireSuperuser: true },
      },
      {
        path: "sqlworkflow",
        name: "sqlworkflow-list",
        component: () => import("@/views/sqlworkflow/List.vue"),
        meta: { title: "SQL 上线", perm: "sql.menu_sqlworkflow" },
      },
      {
        path: "sqlworkflow/:id",
        name: "sqlworkflow-detail",
        component: () => import("@/views/sqlworkflow/Detail.vue"),
        meta: { title: "工单详情", perm: "sql.menu_sqlworkflow" },
      },
      {
        path: "sqlworkflow/submit",
        name: "sqlworkflow-submit",
        component: () => import("@/views/sqlworkflow/Submit.vue"),
        meta: { title: "提交 SQL", perm: "sql.sql_submit" },
      },
      {
        path: "sqlexport",
        name: "sqlexport",
        component: () => import("@/views/sqlexport/List.vue"),
        meta: { title: "数据导出", perm: "sql.menu_sqlexportworkflow" },
      },
      {
        path: "sqlexport/submit",
        name: "sqlexport-submit",
        component: () => import("@/views/sqlexport/Submit.vue"),
        meta: { title: "提交导出", perm: "sql.sqlexport_submit" },
      },
      {
        path: "queryapplylist",
        name: "queryapplylist",
        component: () => import("@/views/queryapplylist/Index.vue"),
        meta: { title: "权限管理", perm: "sql.menu_queryapplylist" },
      },
      {
        path: "queryapplydetail/:id",
        name: "queryapply-detail",
        component: () => import("@/views/queryapplydetail/Detail.vue"),
        meta: { title: "申请详情", perm: "sql.menu_queryapplylist" },
      },
      {
        path: "archive",
        name: "archive",
        component: () => import("@/views/archive/Index.vue"),
        meta: { title: "PTArchiver", perm: "sql.menu_archive" },
      },
      {
        path: "archive/:id",
        name: "archive-detail",
        component: () => import("@/views/archive/Detail.vue"),
        meta: { title: "归档详情", perm: "sql.menu_archive" },
      },
      {
        path: "my2sql",
        name: "my2sql",
        component: () => import("@/views/my2sql/Index.vue"),
        meta: { title: "My2SQL", perm: "sql.menu_my2sql" },
      },
      {
        path: "user",
        name: "user",
        component: () => import("@/views/user/Index.vue"),
        meta: { title: "用户管理" },
      },
      {
        path: "openapi",
        name: "openapi",
        component: () => import("@/views/openapi/Index.vue"),
        meta: { title: "OpenAPI", perm: "sql.menu_openapi" },
      },
      {
        path: "config",
        name: "config",
        component: () => import("@/views/config/Index.vue"),
        meta: { title: "配置项管理" },
      },
      {
        path: "authconfig",
        name: "authconfig",
        component: () => import("@/views/authconfig/Index.vue"),
        meta: { title: "认证配置", requireSuperuser: true },
      },
      {
        path: "aiusage",
        name: "aiusage",
        component: () => import("@/views/aiusage/Index.vue"),
        meta: { title: "AI 用量", requireSuperuser: true },
      },
      {
        path: "sqlquery",
        name: "sqlquery-index",
        component: () => import("@/views/sqlquery/Index.vue"),
        meta: { title: "在线查询", perm: "sql.menu_sqlquery" },
      },
      {
        path: "dbdiagnostic",
        name: "dbdiagnostic",
        component: () => import("@/views/dbdiagnostic/Index.vue"),
        meta: { title: "会话管理", perm: "sql.menu_dbdiagnostic" },
      },
      {
        path: "database",
        name: "database",
        component: () => import("@/views/database/Index.vue"),
        meta: { title: "数据库管理", perm: "sql.menu_database" },
      },
      {
        path: "instanceaccount",
        name: "instanceaccount",
        component: () => import("@/views/instanceaccount/Index.vue"),
        meta: { title: "账号管理", perm: "sql.menu_instance_account" },
      },
      {
        path: "instanceparam",
        name: "instanceparam",
        component: () => import("@/views/instanceparam/Index.vue"),
        meta: { title: "参数配置", perm: "sql.menu_param" },
      },
      {
        path: "paramcompare",
        name: "paramcompare",
        component: () => import("@/views/paramcompare/Index.vue"),
        meta: { title: "参数对比", perm: "sql.menu_param_compare" },
      },
      {
        path: "resourcegroup",
        name: "resourcegroup",
        component: () => import("@/views/resourcegroup/Index.vue"),
        meta: { title: "资源组管理" },
      },
      {
        path: "resourcegroup/:id",
        name: "resourcegroup-relations",
        component: () => import("@/views/resourcegroup/Relations.vue"),
        meta: { title: "关联管理" },
      },
      {
        path: "sqlanalyze",
        name: "sqlanalyze",
        component: () => import("@/views/sqlanalyze/Index.vue"),
        meta: { title: "SQL 分析", perm: "sql.menu_sqlanalyze" },
      },
      {
        path: "datadictionary",
        name: "datadictionary",
        component: () => import("@/views/datadictionary/Index.vue"),
        meta: { title: "数据字典", perm: "sql.menu_data_dictionary" },
      },
      {
        path: "sqladvisor",
        name: "sqladvisor",
        component: () => import("@/views/sqladvisor/Index.vue"),
        meta: { title: "优化工具", perm: "sql.menu_sqladvisor" },
      },
      {
        path: "slowquery",
        name: "slowquery",
        component: () => import("@/views/slowquery/IndexV2.vue"),
        meta: { title: "慢查日志", perm: "sql.menu_slowquery" },
      },
      {
        path: "schemasync",
        name: "schemasync",
        component: () => import("@/views/schemasync/Index.vue"),
        meta: { title: "SchemaSync", perm: "sql.menu_schemasync" },
      },
      {
        path: "audit",
        name: "audit",
        component: () => import("@/views/audit/Index.vue"),
        meta: { title: "系统审计", perm: "sql.audit_user" },
      },
      {
        path: "document",
        name: "document",
        component: () => import("@/views/document/Index.vue"),
        meta: { title: "相关文档", perm: "sql.menu_document" },
      },
    ],
  },
  {
    path: "/:pathMatch(.*)*",
    name: "not-found",
    component: () => import("@/views/NotFound.vue"),
    meta: { public: true },
  },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();

  if (to.meta.public) return true;

  // 首次进入或刷新：探测登录态
  if (!auth.isLoggedIn) {
    try {
      await auth.loadCurrentUser();
    } catch {
      return { name: "login", query: { redirect: to.fullPath } };
    }
  }

  // 权限位拦截
  const perm = to.meta.perm as string | undefined;
  if (perm && !auth.hasPerm(perm)) {
    return { name: "dashboard" };
  }

  // 超管专属页面拦截（如实例管理，其数据接口要求超管）
  if (to.meta.requireSuperuser && !auth.isSuperuser) {
    return { name: "dashboard" };
  }

  return true;
});

export default router;
