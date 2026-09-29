import request from "@/utils/request";
import { ElMessage } from "element-plus";

/**
 * My2SQL（binlog 解析）。全部接口通过 /api/v1/binlog/* 交互，form 提交 + {status,msg,data} 信封。
 *
 * 解析是同步长任务：binlog 越大耗时越长（实测约 1~10MB/s），超过后端同步阈值
 * （110s）会返回"解析超时"，此时应改用异步（save_sql=true）：提交后拿 task_id 轮询
 * 状态，完成后下载完整 SQL 文件。
 */

function checkStatus<T extends { status?: number; msg?: string }>(env: T): T {
  if (env.status !== 0) {
    ElMessage.error(env.msg || "操作失败");
    throw new Error(env.msg || "operation failed");
  }
  return env;
}

function form(obj: Record<string, unknown>) {
  const f = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) {
    if (v === undefined || v === null) continue;
    if (Array.isArray(v)) {
      for (const item of v) f.append(k, String(item));
    } else {
      f.append(k, String(v));
    }
  }
  return f;
}

const FORM_HEADERS = { "Content-Type": "application/x-www-form-urlencoded" };

/** 同步解析的单次请求超时（毫秒）：略大于后端 110s，保证先拿到后端的超时提示 */
const SYNC_TIMEOUT_MS = 120000;

/** binlog 文件行（show binary logs） */
export interface BinlogFile {
  Log_name: string;
  File_size: number | string;
  [key: string]: unknown;
}

/** 获取 binlog 列表（POST /binlog/list/） */
export function fetchBinlogList(instance_name: string) {
  return request
    .post<{ status: number; msg: string; data: BinlogFile[] }>(
      "/api/v1/binlog/list/",
      form({ instance_name }),
      { headers: FORM_HEADERS }
    )
    .then((res) => checkStatus(res.data).data || []);
}

/** 解析后的 SQL 行 */
export interface My2SqlRow {
  sql: string;
  extra_info?: string;
  [key: string]: unknown;
}

/** my2sql 解析参数（同步/异步共用） */
export interface My2SqlParams {
  instance_name: string;
  save_sql?: boolean;
  rollback?: boolean;
  extra_info?: boolean;
  ignore_primary_key?: boolean;
  full_columns?: boolean;
  no_db_prefix?: boolean;
  file_per_table?: boolean;
  threads?: number | string;
  num?: number | string;
  start_file: string;
  start_pos?: number | string;
  end_file?: string;
  end_pos?: number | string;
  stop_time?: string;
  start_time?: string;
  only_schemas?: string;
  only_tables?: string[];
  sql_type?: string[];
}

function buildMy2sqlForm(params: My2SqlParams) {
  const bool = (v?: boolean) => (v ? "true" : "false");
  return form({
    instance_name: params.instance_name,
    save_sql: bool(params.save_sql),
    rollback: bool(params.rollback),
    extra_info: bool(params.extra_info),
    ignore_primary_key: bool(params.ignore_primary_key),
    full_columns: bool(params.full_columns),
    no_db_prefix: bool(params.no_db_prefix),
    file_per_table: bool(params.file_per_table),
    threads: params.threads ?? "",
    num: params.num ?? "",
    start_file: params.start_file ?? "",
    start_pos: params.start_pos ?? "",
    end_file: params.end_file ?? "",
    end_pos: params.end_pos ?? "",
    stop_time: params.stop_time ?? "",
    start_time: params.start_time ?? "",
    only_schemas: params.only_schemas ?? "",
    "only_tables[]": params.only_tables ?? [],
    "sql_type[]": params.sql_type ?? [],
  });
}

/** 同步解析 binlog → SQL 行（POST /binlog/my2sql/） */
export function runMy2sql(params: My2SqlParams) {
  return request
    .post<{ status: number; msg: string; data: My2SqlRow[] }>(
      "/api/v1/binlog/my2sql/",
      buildMy2sqlForm({ ...params, save_sql: false }),
      { headers: FORM_HEADERS, timeout: SYNC_TIMEOUT_MS }
    )
    .then((res) => checkStatus(res.data).data || []);
}

/** 提交异步解析（POST /binlog/my2sql/，save_sql=true）→ 返回 task_id */
export function submitMy2sqlAsync(params: My2SqlParams) {
  return request
    .post<{ status: number; msg: string; task_id?: string }>(
      "/api/v1/binlog/my2sql/",
      buildMy2sqlForm({ ...params, save_sql: true }),
      { headers: FORM_HEADERS }
    )
    .then((res) => {
      const env = checkStatus(res.data);
      if (!env.task_id) throw new Error("未返回任务 ID");
      return env.task_id;
    });
}

/** 异步解析任务状态 */
export interface My2SqlTaskState {
  state: "queued" | "running" | "success" | "failure";
  /** 任务是否已被 django-q worker 接手（刚提交时可能还没落库） */
  registered: boolean;
  sql_count: number;
  has_file: boolean;
  error: string;
}

/**
 * 查询异步任务状态（GET /binlog/my2sql/task/）。
 * 轮询期间静默失败：任务刚提交时后端可能还查不到，交给调用方重试而不是弹错。
 */
export function fetchMy2sqlTask(task_id: string) {
  return request
    .get<{ status: number; msg: string; data: My2SqlTaskState }>(
      "/api/v1/binlog/my2sql/task/",
      { params: { task_id }, silent: true }
    )
    .then((res) => {
      const env = res.data;
      if (env.status !== 0) throw new Error(env.msg || "查询任务状态失败");
      return env.data;
    });
}

/** 下载异步解析结果（GET /binlog/my2sql/download/，服务端返回文件流） */
export function downloadMy2sqlResult(task_id: string) {
  const url = `/api/v1/binlog/my2sql/download/?task_id=${encodeURIComponent(task_id)}`;
  const a = document.createElement("a");
  a.href = url;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}
