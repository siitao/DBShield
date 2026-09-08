import request from "@/utils/request";

/** AI 能力标识 → 展示名 */
export const AI_CAPABILITY_LABELS: Record<string, string> = {
  nl2sql: "自然语言生成SQL",
  sql_optimize: "SQL优化建议",
  sql_review: "SQL工单AI审核",
  slowquery_diagnosis: "AI慢查诊断",
};

export function capabilityLabel(key: string): string {
  return AI_CAPABILITY_LABELS[key] || key;
}

export interface AiUsageTotals {
  calls: number;
  cache_hits: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
  avg_latency_ms: number;
}

export type AiUsageGroupRow = {
  capability?: string;
  user_name?: string;
  day?: string | null;
  total_tokens?: number;
} & AiUsageTotals;

export interface AiUsageRow {
  id: number;
  created_at: string;
  capability: string;
  model: string;
  db_type: string;
  instance_name: string;
  db_name: string;
  user_name: string;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
  cache_hit: boolean;
  status: string;
  error: string;
}

/** 用量总览聚合（GET /api/v1/ai_usage/summary/，超管） */
export function fetchAiUsageSummary(params: { days?: number }) {
  return request
    .get<{
      days: number;
      totals: AiUsageTotals;
      by_capability: AiUsageGroupRow[];
      by_day: AiUsageGroupRow[];
      by_user: AiUsageGroupRow[];
    }>("/api/v1/ai_usage/summary/", { params })
    .then((res) => res.data);
}

/** 用量明细分页（GET /api/v1/ai_usage/list/，超管） */
export function fetchAiUsageList(params: {
  limit?: number;
  offset?: number;
  capability?: string;
  user_name?: string;
  status?: string;
  instance_name?: string;
}) {
  return request
    .get<{ total: number; rows: AiUsageRow[] }>("/api/v1/ai_usage/list/", { params })
    .then((res) => res.data);
}
