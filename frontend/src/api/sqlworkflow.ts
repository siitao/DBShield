import request from "@/utils/request";
import { type Paginated } from "@/api/instance";

/** 工单内层对象（SqlWorkflow 模型，对齐后端 WorkflowSerializer fields="__all__"） */
export interface SqlWorkflowInfo {
  workflow_name: string;
  demand_url: string;
  group_id: number;
  group_name: string;
  instance: number; // Instance 外键 → id（序列化只有 id，无 instance_name）
  db_name: string;
  syntax_type: number;
  is_backup: boolean;
  engineer: string;
  engineer_display: string;
  status: string;
  audit_auth_groups: string;
  run_date_start: string | null;
  run_date_end: string | null;
  create_time: string;
  finish_time: string | null;
  is_manual: number;
  is_offline_export: number;
  file_name?: string;
  [key: string]: unknown;
}

/** 工单列表行（WorkflowContentSerializer） */
export interface SqlWorkflowRow {
  id: number;
  workflow_id: number;
  workflow: SqlWorkflowInfo;
  sql_content: string;
  review_content: string;
  execute_result: string;
  [key: string]: unknown;
}

/** 列表查询参数（对齐 WorkflowFilter + CustomizedPagination） */
export interface SqlWorkflowQuery {
  page?: number;
  size?: number;
  "workflow__workflow_name__icontains"?: string;
  "workflow__instance_id"?: number;
  "workflow__group_id"?: number;
  "workflow__status"?: string;
  "workflow__create_time__gte"?: string;
  "workflow__create_time__lt"?: string;
}

/** 工单状态 → 标签 + Element Plus tag type（对齐 common/static/dist/js/formatter.js:24 配色） */
export const WORKFLOW_STATUS: Record<
  string,
  { label: string; type: "success" | "info" | "warning" | "danger" | "primary" }
> = {
  workflow_manreviewing: { label: "审核中", type: "info" },
  workflow_review_pass: { label: "审核通过", type: "warning" },
  workflow_timingtask: { label: "定时执行", type: "warning" },
  workflow_queuing: { label: "排队中", type: "info" },
  workflow_executing: { label: "执行中", type: "primary" },
  workflow_finish: { label: "已执行", type: "success" },
  workflow_abort: { label: "人工终止", type: "info" },
  workflow_autoreviewwrong: { label: "自动审核不通过", type: "danger" },
  workflow_exception: { label: "执行异常", type: "danger" },
};

/** 语法类型（SqlWorkflow.syntax_type） */
export const SYNTAX_TYPE: Record<number, string> = {
  0: "其他",
  1: "DDL",
  2: "DML",
  3: "数据导出",
};

/** 工单列表（GET /api/v1/workflow/） */
export function fetchSqlWorkflows(params: SqlWorkflowQuery = {}) {
  return request.get<Paginated<SqlWorkflowRow>>("/api/v1/workflow/", { params });
}

/** 工单操作日志行（WorkflowLogListSerializer） */
export interface WorkflowLogRow {
  operation_type_desc: string;
  operation_info: string;
  operator_display: string;
  operation_time: string;
}

/** 工单操作日志（POST /api/v1/workflow/log/；workflow_type 默认 2=SQL 上线，1=查询权限申请） */
export function fetchWorkflowLogs(workflowId: number, workflowType: number = 2) {
  return request.post<Paginated<WorkflowLogRow>>("/api/v1/workflow/log/", {
    workflow_id: workflowId,
    workflow_type: workflowType,
  });
}

/** 审批流节点（WorkflowDetail 返回） */
export interface ReviewNodeInfo {
  group_name: string;
  is_current_node: boolean;
  is_passed_node: boolean;
  is_auto_pass: boolean;
}

/** ReviewSet 行（review_content / execute_result JSON 解析后的单条结果） */
export interface ReviewRow {
  id?: number;
  sql?: string;
  errlevel?: number; // 0 正常 / 1 警告 / 2 错误
  errormessage?: string;
  affected_rows?: number;
  execute_time?: number;
  backup_time?: number;
  stagestatus?: string;
  /** AI 审核结果（可选，仅开启 AI SQL 审核且调用成功时存在） */
  ai_risk_level?: "low" | "medium" | "high" | "unknown" | "";
  ai_risk_score?: number; // 0-100
  ai_summary?: string; // 一句话总结（表格内展示）
  ai_suggestion?: string; // 详细建议（markdown）
  /** 变更影响预测（DDL 锁表/影响行数/是否建议 OSC） */
  ai_ddl_lock_risk?: "none" | "low" | "medium" | "high" | "";
  ai_affected_rows_estimate?: string;
  ai_use_osc?: boolean;
  [key: string]: unknown;
}

/** 工单详情（GET /api/v1/workflow/<id>/） */
export interface SqlWorkflowDetail extends SqlWorkflowRow {
  review_info: ReviewNodeInfo[];
  current_reviewers: { username: string; display: string }[];
  last_operation_info: string;
  is_can_review: boolean;
  is_can_execute: boolean;
  is_can_timingtask: boolean;
  is_can_cancel: boolean;
  is_can_rollback: boolean;
  manual: boolean;
  run_date: string;
  /** AI 风险评估汇总（无 AI 数据时 ai_max_risk_level 为空串） */
  ai_max_risk_level?: "low" | "medium" | "high" | "";
  ai_max_risk_score?: number;
  ai_high_risk_count?: number;
  /** DDL 锁表高危 SQL 数（变更影响预测） */
  ai_lock_high_count?: number;
}

/** 工单详情（含审批流 + 操作权限标志） */
export function fetchWorkflowDetail(id: number) {
  return request.get<SqlWorkflowDetail>(`/api/v1/workflow/${id}/`);
}

/** 审核工单（workflow_type=2 SQL 上线；audit_type: pass 通过 / cancel 终止或驳回） */
export function auditWorkflow(params: {
  engineer: string;
  workflow_id: number;
  audit_type: "pass" | "cancel";
  audit_remark: string;
}) {
  return request.post<{ msg: string }>("/api/v1/workflow/audit/", {
    workflow_type: 2,
    ...params,
  });
}

/** 执行工单（workflow_type=2；mode: auto 线上执行 / manual 已手工执行） */
export function executeWorkflow(params: {
  engineer: string;
  workflow_id: number;
  mode: "auto" | "manual";
}) {
  return request.post<{ msg: string }>("/api/v1/workflow/execute/", {
    workflow_type: 2,
    ...params,
  });
}

/** 定时执行（run_date 格式 YYYY-MM-DD HH:mm，须晚于当前时间） */
export function timingTask(params: { workflow_id: number; run_date: string }) {
  return request.post<{ msg: string }>("/api/v1/workflow/timingtask/", params);
}

/** 修改可执行时间范围 */
export function alterRunDate(params: {
  workflow_id: number;
  run_date_start?: string;
  run_date_end?: string;
}) {
  return request.post<{ msg: string }>("/api/v1/workflow/alter_run_date/", params);
}

/** 回滚语句行（backup_sql 返回，get_rollback 列表，元素为 SQL 字符串或 {sql,...}） */
export type BackupSqlRow = string | ReviewRow;

/** 查看回滚 SQL（GET /api/v1/sqlworkflow/backup_sql/，{status,msg,rows}）
 *  rows 元素可能是：
 *   - 二维数组 [source_sql, rollback_sql]（goinception get_rollback 返回）
 *   - 字符串
 *   - ReviewRow 对象
 */
export async function fetchBackupSql(workflowId: number): Promise<ReviewRow[]> {
  const { data } = await request.get<{ status: number; msg: string; rows: BackupSqlRow[] }>(
    "/api/v1/sqlworkflow/backup_sql/",
    { params: { workflow_id: workflowId } }
  );
  if (data.status !== 0) {
    throw new Error(data.msg || "获取回滚语句失败");
  }
  return (data.rows || []).map((r) => {
    if (Array.isArray(r)) {
      // [source_sql, rollback_sql] 二维数组格式
      return { sql: r[0], errormessage: `回滚SQL: ${r[1] || ""}` } as ReviewRow;
    }
    if (typeof r === "string") {
      return { sql: r } as ReviewRow;
    }
    return r as ReviewRow;
  });
}

/** 下载回滚 SQL 文件（GET /api/v1/rollback/?workflow_id=，服务端打包文件流） */
export function downloadRollback(workflowId: number) {
  const url = `/api/v1/rollback/?workflow_id=${workflowId}`;
  const a = document.createElement("a");
  a.href = url;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

/** OSC 进度行（goInception pt-osc/gh-ost） */
export interface OscProgressRow {
  DBNAME?: string;
  TABLENAME?: string;
  PERCENT?: string | number;
  SQLSHA1?: string;
  REMAINTIME?: string;
  INFOMATION?: string;
  [key: string]: unknown;
}

/** OSC 控制（POST /inception/osc_control/，command get/pause/resume/kill） */
export async function oscControl(
  workflowId: number,
  sqlsha1: string,
  command: "get" | "pause" | "resume" | "kill"
): Promise<{ rows: OscProgressRow[]; msg: string | null }> {
  const form = new URLSearchParams();
  form.append("workflow_id", String(workflowId));
  form.append("sqlsha1", sqlsha1);
  form.append("command", command);
  const { data } = await request.post<{
    total: number;
    rows: OscProgressRow[];
    msg: string | null;
  }>("/api/v1/sqlworkflow/osc_control/", form, {
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  });
  return { rows: data.rows || [], msg: data.msg || null };
}

/** SQL 检测结果（ExecuteCheckResultSerializer） */
export interface SqlCheckResult {
  is_execute: boolean;
  warning_count: number;
  error_count: number;
  is_critical: boolean;
  syntax_type: number;
  rows: ReviewRow[] | unknown[];
  column_list?: string[];
  status?: string;
  affected_rows?: number;
  /** 服务端 AI 审核快照 token，提交时回传以复用 AI 结论 */
  ai_review_id?: string;
  [key: string]: unknown;
}

/** SQL 检测（POST /api/v1/workflow/sqlcheck/） */
export function sqlCheck(params: {
  instance_id: number;
  db_name: string;
  full_sql: string;
}) {
  return request.post<SqlCheckResult>("/api/v1/workflow/sqlcheck/", params);
}

/** 提交工单（POST /api/v1/workflow/） */
export function submitWorkflow(params: {
  workflow: {
    workflow_name: string;
    demand_url?: string;
    group_id: number;
    instance: number;
    db_name: string;
    is_backup: boolean;
    run_date_start?: string;
    run_date_end?: string;
    is_offline_export: number;
    export_format?: string;
  };
  sql_content: string;
  /** 检测接口返回的服务端 AI 审核快照 token；SQL 变更后必须重新检测 */
  ai_review_id?: string;
}) {
  return request.post<SqlWorkflowRow>("/api/v1/workflow/", params);
}
