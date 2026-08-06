from django.urls import path, include
from sql_api import views
from rest_framework import routers
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from . import api_user, api_instance, api_workflow, api_sqlquery, api_document, api_query_priv, api_archiver, api_dashboard, api_config, api_auth_config, api_dictionary, api_instance_admin, api_diagnostic, api_slowquery, api_slowquery_v2, api_resource_group, api_misc, api_auth

router = routers.DefaultRouter()
router.register(
    "dictionary",
    api_dictionary.DataDictionaryViewSet,
    basename="dictionary",
)

urlpatterns = [
    path("v1/", include(router.urls)),
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("auth/token/verify/", TokenVerifyView.as_view(), name="token_verify"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "swagger/",
        SpectacularSwaggerView.as_view(url_name="sql_api:schema"),
        name="swagger",
    ),
    path(
        "redoc/", SpectacularRedocView.as_view(url_name="sql_api:schema"), name="redoc"
    ),
    path("v1/user/", api_user.UserList.as_view()),
    path("v1/user/<int:pk>/", api_user.UserDetail.as_view()),
    path("v1/user/me/", api_user.CurrentUser.as_view()),
    path("v1/user/group/", api_user.GroupList.as_view()),
    path("v1/user/permissions/", api_user.PermissionList.as_view()),
    path("v1/user/group/<int:pk>/", api_user.GroupDetail.as_view()),
    path("v1/user/resourcegroup/", api_user.ResourceGroupList.as_view()),
    path("v1/user/resourcegroup/<int:pk>/", api_user.ResourceGroupDetail.as_view()),
    path("v1/user/auth/", api_user.UserAuth.as_view()),
    path("v1/user/2fa/", api_user.TwoFA.as_view()),
    path("v1/user/2fa/context/", api_user.TwoFAVerifyContext.as_view()),
    path("v1/user/2fa/state/", api_user.TwoFAState.as_view()),
    path("v1/user/2fa/save/", api_user.TwoFASave.as_view()),
    path("v1/user/2fa/verify/", api_user.TwoFAVerify.as_view()),
    path("v1/instance/", api_instance.InstanceList.as_view()),
    path("v1/instance/tags/", api_instance.InstanceTagList.as_view()),
    path("v1/instance/<int:pk>/", api_instance.InstanceDetail.as_view()),
    path("v1/instance/resource/", api_instance.InstanceResource.as_view()),
    path("v1/instance/table-instances/", api_instance.TableInstanceLookup.as_view()),
    path("v1/sqlquery/instances/", api_sqlquery.SQLQueryInstancesView.as_view()),
    path("v1/sqlquery/resources/", api_sqlquery.SQLQueryResourcesView.as_view()),
    path(
        "v1/sqlquery/describetable/", api_sqlquery.SQLQueryDescribeTableView.as_view()
    ),
    path("v1/sqlquery/execute/", api_sqlquery.SQLQueryExecuteView.as_view()),
    path("v1/sqlquery/logs/", api_sqlquery.SQLQueryLogsView.as_view()),
    path("v1/sqlquery/favorites/", api_sqlquery.SQLQueryFavoritesView.as_view()),
    path("v1/instance/tunnel/", api_instance.TunnelList.as_view()),
    path("v1/instance/rds/", api_instance.AliyunRdsList.as_view()),
    path(
        "v1/instance/rds/by_instance/",
        api_instance.AliyunRdsByInstance.as_view(),
    ),
    path("v1/instance/rds/<int:pk>/", api_instance.AliyunRdsDetail.as_view()),
    path("v1/workflow/", api_workflow.WorkflowList.as_view()),
    path("v1/workflow/sqlcheck/", api_workflow.ExecuteCheck.as_view()),
    path("v1/workflow/audit/", api_workflow.AuditWorkflow.as_view()),
    path("v1/workflow/auditlist/", api_workflow.WorkflowAuditList.as_view()),
    path("v1/workflow/execute/", api_workflow.ExecuteWorkflow.as_view()),
    path("v1/workflow/log/", api_workflow.WorkflowLogList.as_view()),
    path("v1/workflow/timingtask/", api_workflow.TimingTask.as_view()),
    path("v1/workflow/alter_run_date/", api_workflow.AlterRunDate.as_view()),
    path("v1/workflow/<int:workflow_id>/", api_workflow.WorkflowDetail.as_view()),
    path(
        "v1/document/dbaprinciples/",
        api_document.DbAprinciplesDocument.as_view(),
    ),
    path(
        "v1/query_priv/<int:apply_id>/",
        api_query_priv.QueryPrivApplyDetail.as_view(),
    ),
    path("v1/query_priv/audit/", api_query_priv.QueryPrivAudit.as_view()),
    path("v1/archive/<int:pk>/", api_archiver.ArchiveDetail.as_view()),
    path("v1/archive/audit/", api_archiver.ArchiveAudit.as_view()),
    path(
        "v1/dashboard/charts/",
        api_dashboard.DashboardCharts.as_view(),
    ),
    path("v1/config/", api_config.ConfigView.as_view()),
    path("v1/config/change/", api_config.ChangeConfigView.as_view()),
    path("v1/config/check_ai/", api_config.CheckAIConnectionView.as_view()),
    path("v1/config/check_inception/", api_config.CheckInceptionView.as_view()),
    # ---- 认证方式配置 ----
    path("v1/auth_config/", api_auth_config.AuthConfigView.as_view()),
    path("v1/auth_config/reload/", api_auth_config.ReloadAuthConfigView.as_view()),
    path("v1/auth_config/test/", api_auth_config.TestAuthConnectionView.as_view()),
    path("v1/auth/login_options/", api_auth_config.LoginOptionsView.as_view()),
    # ---- 账号密码登录 / 退出 ----
    path("v1/authenticate/", api_auth.AuthenticateView.as_view()),
    path("v1/logout/", api_auth.LogoutView.as_view()),
    # ---- 实例管理 ----
    path("v1/instance/accounts/", api_instance_admin.AccountListView.as_view()),
    path("v1/instance/accounts/create/", api_instance_admin.AccountCreateView.as_view()),
    path("v1/instance/accounts/edit/", api_instance_admin.AccountEditView.as_view()),
    path("v1/instance/accounts/grant/", api_instance_admin.AccountGrantView.as_view()),
    path("v1/instance/accounts/reset_pwd/", api_instance_admin.AccountResetPwdView.as_view()),
    path("v1/instance/accounts/lock/", api_instance_admin.AccountLockView.as_view()),
    path("v1/instance/accounts/delete/", api_instance_admin.AccountDeleteView.as_view()),
    path("v1/instance/databases/", api_instance_admin.DatabaseListView.as_view()),
    path("v1/instance/databases/create/", api_instance_admin.DatabaseCreateView.as_view()),
    path("v1/instance/databases/edit/", api_instance_admin.DatabaseEditView.as_view()),
    path("v1/instance/params/", api_instance_admin.ParamListView.as_view()),
    path("v1/instance/params/history/", api_instance_admin.ParamHistoryView.as_view()),
    path("v1/instance/params/edit/", api_instance_admin.ParamEditView.as_view()),
    path("v1/instance/params/compare/", api_instance_admin.ParamCompareView.as_view()),
    # ---- 会话诊断 ----
    path("v1/diagnostic/process/", api_diagnostic.ProcessView.as_view()),
    path("v1/diagnostic/create_kill/", api_diagnostic.CreateKillSessionView.as_view()),
    path("v1/diagnostic/kill/", api_diagnostic.KillSessionView.as_view()),
    path("v1/diagnostic/tablespace/", api_diagnostic.TableSpaceView.as_view()),
    path("v1/diagnostic/trxandlocks/", api_diagnostic.TrxAndLocksView.as_view()),
    path("v1/diagnostic/innodb_trx/", api_diagnostic.InnodbTrxView.as_view()),
    # ---- 慢查日志（新版本） ----
    path("v1/slowquery/review/", api_slowquery_v2.SlowQuerySummaryView.as_view()),
    path("v1/slowquery/review_history/", api_slowquery_v2.SlowQueryDetailView.as_view()),
    path("v1/slowquery/summary/", api_slowquery_v2.SlowQuerySummaryView.as_view()),
    path("v1/slowquery/detail/", api_slowquery_v2.SlowQueryDetailView.as_view()),
    path("v1/slowquery/trend/", api_slowquery_v2.SlowQueryTrendView.as_view()),
    path("v1/slowquery/collect/", api_slowquery_v2.SlowQueryCollectView.as_view()),
    # ---- AI 慢查诊断 ----
    path("v1/slowquery/diagnose/", api_slowquery_v2.SlowQueryDiagnoseView.as_view()),
    path("v1/slowquery/diagnose/batch_status/", api_slowquery_v2.SlowQueryDiagnoseBatchStatusView.as_view()),
    path("v1/slowquery/diagnose/<int:task_id>/", api_slowquery_v2.SlowQueryDiagnoseTaskView.as_view()),
    # PRD §8.3：反馈路由为 diagnose/<report_id>/feedback/
    path("v1/slowquery/diagnose/<int:report_id>/feedback/", api_slowquery_v2.SlowQueryDiagnoseFeedbackView.as_view()),
    path("v1/slowquery/diagnose/workflow_draft/", api_slowquery_v2.SlowQueryDiagnoseWorkflowView.as_view()),
    # ---- SQL 分析 ----
    path("v1/sql_analyze/generate/", api_slowquery.SqlAnalyzeGenerateView.as_view()),
    path("v1/sql_analyze/analyze/", api_slowquery.SqlAnalyzeAnalyzeView.as_view()),
    path("v1/sql_analyze/ai/", api_slowquery.SqlAnalyzeAIView.as_view()),
    # ---- SQL 优化工具 ----
    path("v1/optimize/sqladvisor/", api_slowquery.OptimizeSqlAdvisorView.as_view()),
    path("v1/optimize/soar/", api_slowquery.OptimizeSoarView.as_view()),
    path("v1/optimize/sqltuning/", api_slowquery.OptimizeSqlTuningView.as_view()),
    path("v1/optimize/explain/", api_slowquery.ExplainSqlView.as_view()),
    path("v1/optimize/ai/", api_slowquery.OptimizeAIView.as_view()),
    # ---- 资源组管理 ----
    path("v1/group/list/", api_resource_group.GroupListView.as_view()),
    path("v1/group/relations/", api_resource_group.RelationsView.as_view()),
    path("v1/group/unassociated/", api_resource_group.UnassociatedView.as_view()),
    path("v1/group/instances/", api_resource_group.InstancesView.as_view()),
    path("v1/group/user_instances/", api_resource_group.UserInstancesView.as_view()),
    path("v1/group/addrelation/", api_resource_group.AddRelationView.as_view()),
    path("v1/group/removerelation/", api_resource_group.RemoveRelationView.as_view()),
    path("v1/group/auditors/", api_resource_group.AuditorsView.as_view()),
    path("v1/group/changeauditors/", api_resource_group.ChangeAuditorsView.as_view()),
    # ---- 审计 ----
    path("v1/audit/log/", api_misc.AuditLogView.as_view()),
    path("v1/audit/sqlworkflow/", api_misc.AuditSqlWorkflowView.as_view()),
    path("v1/audit/querylog/", api_misc.AuditQueryLogView.as_view()),
    # ---- binlog / my2sql ----
    path("v1/binlog/list/", api_misc.BinlogListView.as_view()),
    path("v1/binlog/my2sql/", api_misc.My2sqlView.as_view()),
    # ---- 查询 / AI ----
    path("v1/query/generate_sql/", api_misc.GenerateSqlView.as_view()),
    path("v1/query/check_openai/", api_misc.CheckOpenAIView.as_view()),
    # ---- 查询权限 ----
    path("v1/query/applylist/", api_misc.QueryApplyListView.as_view()),
    path("v1/query/userprivileges/", api_misc.UserPrivilegesView.as_view()),
    path("v1/query/applyforprivileges/", api_misc.ApplyForPrivilegesView.as_view()),
    path("v1/query/modifyprivileges/", api_misc.ModifyPrivilegesView.as_view()),
    # ---- SQL 上线辅助 ----
    path("v1/sqlworkflow/backup_sql/", api_misc.BackupSqlView.as_view()),
    path("v1/sqlworkflow/list_audit/", api_misc.AuditSqlWorkflowView.as_view()),
    path("v1/sqlworkflow/osc_control/", api_misc.OscControlView.as_view()),
    # ---- SchemaSync ----
    path("v1/schemasync/", api_misc.SchemaSyncView.as_view()),
    # ---- 回滚 / 导出（文件流 + 预检） ----
    path("v1/rollback/", api_misc.RollbackDownloadView.as_view()),
    path("v1/sqlexport/pre_check/", api_misc.SqlexportPreCheckView.as_view()),
    path("v1/downloadfile/", api_misc.DownloadFileView.as_view()),
    # ---- 归档 ----
    path("v1/archive/list/", api_archiver.ArchiveListView.as_view()),
    path("v1/archive/apply/", api_archiver.ArchiveApplyView.as_view()),
    path("v1/archive/log/", api_archiver.ArchiveLogView.as_view()),
    path("v1/archive/switch/", api_archiver.ArchiveSwitchView.as_view()),
    path("v1/archive/once/", api_archiver.ArchiveOnceView.as_view()),
    path("info", views.info),
    path("debug", views.debug),
    path("do_once/mirage", views.mirage),
]
