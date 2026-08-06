import request, { unwrapLegacy, type LegacyEnvelope } from "@/utils/request";
import { type Paginated } from "@/api/instance";

/** 资源组（对齐 ResourceGroupSerializer） */
export interface ResourceGroupRow {
  group_id: number;
  group_name: string;
  [key: string]: unknown;
}

/** 资源组列表（GET /api/v1/user/resourcegroup/） */
export function fetchResourceGroups(params: { size?: number } = {}) {
  return request.get<Paginated<ResourceGroupRow>>(
    "/api/v1/user/resourcegroup/",
    { params }
  );
}

/** 资源组关联实例行（旧版 /group/instances/ data 元素） */
export interface GroupInstanceRow {
  id: number;
  type: number;
  db_type: string;
  instance_name: string;
  /** 后端 resource_service 附带的字段：实例是否已启用阿里云 RDS */
  is_aliyun_rds?: boolean;
}

/** 用户可访问实例（GET /api/v1/group/user_instances/，旧信封 {status,msg,data}） */
export function fetchUserInstances(
  params: { db_type?: string[]; tag_codes?: string[] } = {}
) {
  return request
    .get<LegacyEnvelope<GroupInstanceRow[]>>("/api/v1/group/user_instances/", {
      params,
    })
    .then((res) => unwrapLegacy(res.data));
}

/** 资源组关联实例（POST /group/instances/，旧信封；用 group_name + tag_code） */
export function fetchGroupInstances(groupName: string, tagCode = "can_write") {
  const form = new URLSearchParams();
  form.append("group_name", groupName);
  form.append("tag_code", tagCode);
  return request
    .post<LegacyEnvelope<GroupInstanceRow[]>>("/api/v1/group/instances/", form, {
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    })
    .then((res) => unwrapLegacy(res.data));
}

/** 资源组审批流（旧版 /group/auditors/ data） */
export interface GroupAuditors {
  auditors: string;
  auditors_display: string;
}

/** 资源组审批流（POST /group/auditors/，旧信封；workflow_type=2 SQL 上线） */
export function fetchGroupAuditors(groupName: string, workflowType = 2) {
  const form = new URLSearchParams();
  form.append("group_name", groupName);
  form.append("workflow_type", String(workflowType));
  return request
    .post<LegacyEnvelope<GroupAuditors>>("/api/v1/group/auditors/", form, {
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    })
    .then((res) => unwrapLegacy(res.data));
}
