import request from "@/utils/request";

export interface InstanceRow {
  id: number;
  instance_name: string;
  type: string;
  db_type: string;
  mode?: string;
  host: string;
  port: number;
  user: string;
  db_name?: string;
  charset?: string;
  [key: string]: unknown;
}

export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface InstanceQuery {
  page?: number;
  size?: number;
  /** InstanceFilter: instance_name__icontains */
  instance_name__icontains?: string;
  db_type?: string;
  host?: string;
}

/** 实例列表（DRF InstanceList，CustomizedPagination） */
export function fetchInstances(params: InstanceQuery = {}) {
  return request.get<Paginated<InstanceRow>>("/api/v1/instance/", { params });
}

/** 可选数据库类型（GET /api/v1/instance/engines/，仅返回已启用的引擎） */
export interface InstanceEngineOption {
  value: string;
  label: string;
}
export function fetchInstanceEngines() {
  return request
    .get<InstanceEngineOption[]>("/api/v1/instance/engines/")
    .then((r) => r.data);
}

/** 测试实例连接结果（POST /api/v1/instance/test_connection/） */
export interface InstanceTestResult {
  status: number;
  msg: string;
}
export function testInstanceConnection(data: InstanceForm) {
  return request.post<InstanceTestResult>("/api/v1/instance/test_connection/", data);
}

/** 实例标签（GET /api/v1/instance/tags/） */
export interface InstanceTagRow {
  id: number;
  tag_code: string;
  tag_name: string;
}
export function fetchInstanceTags() {
  return request.get<InstanceTagRow[]>("/api/v1/instance/tags/").then((r) => r.data);
}

/** 隧道（GET /api/v1/instance/tunnel/） */
export interface TunnelRow {
  id: number;
  tunnel_name: string;
  host: string;
  port: number;
}
export function fetchTunnels() {
  return request.get<Paginated<TunnelRow>>("/api/v1/instance/tunnel/", {
    params: { size: 1000 },
  });
}

/** 实例表单字段（对齐 Instance 模型，fields="__all__"） */
export interface InstanceForm {
  id?: number;
  instance_name: string;
  type: string;
  db_type: string;
  mode?: string;
  host: string;
  port: number;
  user?: string;
  password?: string;
  is_ssl?: boolean;
  verify_ssl?: boolean;
  db_name?: string;
  show_db_name_regex?: string;
  denied_db_name_regex?: string;
  charset?: string;
  service_name?: string;
  sid?: string;
  resource_group?: number[];
  instance_tag?: number[];
  tunnel?: number | null;
  [key: string]: unknown;
}

/** 新增实例（POST /api/v1/instance/） */
export function createInstance(data: InstanceForm) {
  return request.post<InstanceRow>("/api/v1/instance/", data);
}

/** 更新实例（PUT /api/v1/instance/<id>/） */
export function updateInstance(id: number, data: Partial<InstanceForm>) {
  return request.put<InstanceRow>(`/api/v1/instance/${id}/`, data);
}

/** 删除实例（DELETE /api/v1/instance/<id>/） */
export function deleteInstance(id: number) {
  return request.delete(`/api/v1/instance/${id}/`);
}

// ============ Aliyun RDS（OneToOne 到 Instance） ============

export interface AliyunRdsConfig {
  id?: number;
  rds_dbinstanceid: string;
  is_enable: boolean;
  instance: number;
  ak: { id?: number; key_id: string; key_secret: string; remark?: string };
}

/** 查某实例的 RDS 配置（GET /api/v1/instance/rds/by_instance/?instance=<id>） */
export function fetchInstanceRds(instanceId: number) {
  return request
    .get<AliyunRdsConfig | null>("/api/v1/instance/rds/by_instance/", {
      params: { instance: instanceId },
      validateStatus: (s) => s < 500,
    })
    .then((r) => (r.status === 404 ? null : r.data))
    .catch(() => null);
}

/** 创建 RDS 配置（POST /api/v1/instance/rds/） */
export function createRds(data: AliyunRdsConfig) {
  return request.post("/api/v1/instance/rds/", {
    ...data,
    ak: { type: "aliyun", ...data.ak },
  });
}

/** 更新 RDS 配置（PUT /api/v1/instance/rds/<id>/） */
export function updateRds(id: number, data: Partial<AliyunRdsConfig>) {
  return request.put(`/api/v1/instance/rds/${id}/`, data);
}

/** 删除 RDS 配置（DELETE /api/v1/instance/rds/<id>/） */
export function deleteRds(id: number) {
  return request.delete(`/api/v1/instance/rds/${id}/`);
}
