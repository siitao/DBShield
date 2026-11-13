import type { ReviewRow } from "@/api/sqlworkflow";

/**
 * 解析 ReviewSet JSON，兼容新格式（rows 为 dict 列表）与旧格式
 * （rows 为 [[...], ...] + column_list）。
 */
export function parseReviewRows(jsonStr?: string | null): ReviewRow[] {
  if (!jsonStr) return [];
  try {
    const obj = JSON.parse(jsonStr);
    // Direct array format (from ReviewSet.json() / DRF serializer)
    if (Array.isArray(obj)) {
      return obj as ReviewRow[];
    }
    const rows = obj?.rows ?? obj?.data ?? [];
    if (Array.isArray(rows) && rows.length && Array.isArray(rows[0])) {
      const cols: string[] = obj?.column_list ?? [];
      return rows.map((r: unknown[]) => {
        const o: Record<string, unknown> = {};
        cols.forEach((c, i) => (o[c] = r[i]));
        return o as ReviewRow;
      });
    }
    return Array.isArray(rows) ? (rows as ReviewRow[]) : [];
  } catch {
    return [];
  }
}
