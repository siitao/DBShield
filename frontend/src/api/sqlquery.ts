import request, { unwrapLegacy, type LegacyEnvelope } from "@/utils/request";

/**
 * Redis 引擎返回 db_name 为 {value, text} 对象而非纯字符串。
 * 统一提取为 string，防止 axios 将对象序列化为 db_name[value]=…。
 */
function dbName(v: unknown): string {
  return v && typeof v === "object" ? (v as any).value ?? "" : (v as string) ?? "";
}

/** 可访问实例（resource_service:14） */
export interface QueryInstanceRow {
  id: number;
  type: number;
  db_type: string;
  instance_name: string;
  [key: string]: unknown;
}

/** 查询结果集（ResultSet.__dict__，对齐 sql/engines/models.py:124-167） */
export interface QueryResultSet {
  full_sql: string;
  is_execute: boolean;
  is_masked: boolean;
  query_time: string;
  mask_time: string;
  mask_rule_hit: boolean;
  error: string | null;
  is_critical: boolean;
  /** 行数据为「值数组」的二维数组（非对象），渲染需按 column_list zip */
  rows: unknown[][];
  column_list: string[];
  column_type: unknown[];
  status: string | null;
  affected_rows: number;
  seconds_behind_master?: number;
  [key: string]: unknown;
}

/** 查询历史行（querylog_service:46-59） */
export interface QueryLogRow {
  id: number;
  instance_name: string;
  db_name: string;
  sqllog: string;
  effect_row: number;
  cost_time: string;
  user_display: string;
  favorite: boolean;
  alias: string;
  create_time: string;
  [key: string]: unknown;
}

/** 表名定位结果（table_instance_locator） */
export interface TableLocatorRow {
  id: number;
  name: string;
  db_type: string;
  db_name: string;
  table_name: string;
  [key: string]: unknown;
}

/** 执行查询的完整信封（status 三态：0 成功 / 1 错误 / 2 权限不足） */
export interface QueryResultEnvelope extends LegacyEnvelope<QueryResultSet> {}

/** 可访问实例（GET /api/v1/sqlquery/instances/） */
export function fetchQueryInstances(
  params: { type?: string; db_type?: string[]; tag_codes?: string[] } = {}
) {
  return request
    .get<LegacyEnvelope<QueryInstanceRow[]>>("/api/v1/sqlquery/instances/", {
      params,
    })
    .then((res) => unwrapLegacy(res.data));
}

/** 库/schema/表/列资源（GET /api/v1/sqlquery/resources/） */
export function fetchQueryResources(params: {
  instance_id?: number;
  instance_name?: string;
  resource_type: "database" | "schema" | "table" | "column";
  db_name?: string;
  schema_name?: string;
  tb_name?: string;
}) {
  return request
    .get<LegacyEnvelope<string[]>>("/api/v1/sqlquery/resources/", {
      params: { ...params, db_name: dbName(params.db_name) },
    })
    .then((res) => unwrapLegacy(res.data));
}

/** 表结构（POST /api/v1/sqlquery/describetable/） */
export function describeTable(params: {
  instance_name: string;
  db_name: string;
  tb_name: string;
  schema_name?: string;
}) {
  return request
    .post<LegacyEnvelope<QueryResultSet>>("/api/v1/sqlquery/describetable/", {
      ...params,
      db_name: dbName(params.db_name),
    })
    .then((res) => unwrapLegacy(res.data));
}

/**
 * 执行查询（POST /api/v1/sqlquery/execute/）。
 * 不 unwrap：status 三态（0 成功 / 1 错误 / 2 权限不足）由页面分支处理。
 * timeout 90s（后端 max_execution_time 默认 60s + 余量）。
 */
export function executeQuery(params: {
  instance_name: string;
  db_name: string;
  sql_content: string;
  limit_num: number;
  schema_name?: string;
  tb_name?: string;
}) {
  return request.post<QueryResultEnvelope>(
    "/api/v1/sqlquery/execute/",
    { ...params, db_name: dbName(params.db_name) },
    { timeout: 90000 }
  );
}

/** 查询历史（GET /api/v1/sqlquery/logs/，返回 {total,rows} 非 LegacyEnvelope） */
export function fetchQueryLogs(
  params: {
    limit?: number;
    offset?: number;
    search?: string;
    star?: "" | "true" | "false";
    query_log_id?: number;
    start_date?: string;
    end_date?: string;
  } = {}
) {
  return request.get<{ total: number; rows: QueryLogRow[] }>(
    "/api/v1/sqlquery/logs/",
    { params }
  );
}

/** 收藏/取消（POST /api/v1/sqlquery/favorites/） */
export function toggleFavorite(params: {
  query_log_id: number;
  star: "true" | "false";
  alias?: string;
}) {
  return request
    .post<LegacyEnvelope<unknown>>("/api/v1/sqlquery/favorites/", params)
    .then((res) => unwrapLegacy(res.data));
}

/** 表名定位（POST /api/v1/instance/table-instances/） */
export function locateTable(tableName: string) {
  return request
    .post<LegacyEnvelope<TableLocatorRow[]>>(
      "/api/v1/instance/table-instances/",
      { table_name: tableName }
    )
    .then((res) => unwrapLegacy(res.data));
}

/** AI 生成 SQL（POST /query/generate_sql/）。
 * 后端结合所选表 DDL 作为上下文，调用 OpenAI 生成查询语句。
 * unwrapLegacy：status 非 0 时自动弹错并抛出。 */
export function generateSql(params: {
  query_desc: string;
  db_type: string;
  instance_name: string;
  db_name: string;
  schema_name?: string;
  tb_name?: string;
}) {
  return request
    .post<LegacyEnvelope<string>>("/api/v1/query/generate_sql/", {
      query_desc: params.query_desc,
      db_type: params.db_type,
      instance_name: params.instance_name,
      db_name: params.db_name,
      schema_name: params.schema_name || undefined,
      tb_name: params.tb_name || undefined,
    })
    .then((res) => unwrapLegacy(res.data));
}

/**
 * 探测 OpenAI 是否配置（GET /api/v1/query/check_openai/）。
 * 不 unwrap：未配置时 status 可能非 0，但不是「错误」（不弹提示），由页面判 status。
 */
export function checkOpenai() {
  return request.get<LegacyEnvelope<{ openai?: boolean; nl2sql_enabled?: boolean }>>("/api/v1/query/check_openai/");
}
