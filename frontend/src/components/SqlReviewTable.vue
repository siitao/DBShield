<script setup lang="ts">
import { ref, computed } from "vue";
import { marked } from "marked";
import DOMPurify from "dompurify";
import type { ReviewRow } from "@/api/sqlworkflow";
import TruncateCell from "@/components/TruncateCell.vue";

marked.setOptions({ gfm: true, breaks: false });

/** AI 建议 markdown → 消毒后的 HTML（XSS 防护，与 document/DiagnosisDrawer 一致） */
function aiSuggestionHtml(md?: string): string {
  if (!md) return "";
  const raw = marked.parse(md, { async: false }) as string;
  return DOMPurify.sanitize(raw);
}

const props = defineProps<{
  rows: ReviewRow[];
  /** review=提交检测阶段（无耗时/阶段列）；execute=执行结果阶段 */
  phase: "review" | "execute";
  /** 传入则对含 sqlsha1 的行显示「进度」操作（OSC 执行进度入口） */
  oscWorkflowId?: number;
}>();

const emit = defineEmits<{
  (e: "osc", row: ReviewRow): void;
}>();

const page = ref(1);
const pageSize = ref(500);

const pagedRows = computed(() => {
  const start = (page.value - 1) * pageSize.value;
  return props.rows.slice(start, start + pageSize.value);
});

/** 该行是否携带 AI 审核数据（用于决定「AI 建议」列显示标签还是占位） */
function hasAi(row: ReviewRow): boolean {
  return (
    row.ai_risk_level !== undefined &&
    row.ai_risk_level !== null &&
    row.ai_risk_level !== ""
  );
}

/** AI 风险等级 → el-tag type */
function aiTagType(
  level: ReviewRow["ai_risk_level"]
): "danger" | "warning" | "success" | "info" {
  switch (level) {
    case "high":
      return "danger";
    case "medium":
      return "warning";
    case "low":
      return "success";
    default:
      return "info";
  }
}

/** AI 风险等级 → 中文文案 */
function aiLevelText(level: ReviewRow["ai_risk_level"]): string {
  switch (level) {
    case "high":
      return "高风险";
    case "medium":
      return "中风险";
    case "low":
      return "低风险";
    default:
      return "AI跳过";
  }
}

/** DDL 锁表风险是否需要重点提示（medium/high 才显示标签） */
function hasLockRisk(row: ReviewRow): boolean {
  return row.ai_ddl_lock_risk === "medium" || row.ai_ddl_lock_risk === "high";
}

/** DDL 锁表风险 → el-tag type */
function lockTagType(
  level: ReviewRow["ai_ddl_lock_risk"]
): "danger" | "warning" | "info" {
  return level === "high" ? "danger" : "warning";
}

/** DDL 锁表风险 → 中文文案 */
function lockText(level: ReviewRow["ai_ddl_lock_risk"]): string {
  switch (level) {
    case "high":
      return "大表锁表";
    case "medium":
      return "锁表风险";
    default:
      return "";
  }
}

/** 按 errlevel 行变色：2 错误红 / 1 警告黄 */
function rowClass({ row }: { row: ReviewRow }): string {
  const lvl = Number((row as ReviewRow).errlevel ?? 0);
  if (lvl === 2) return "row-error";
  if (lvl === 1) return "row-warning";
  return "";
}

function toRecord(row: ReviewRow): Record<string, unknown> {
  return row as unknown as Record<string, unknown>;
}

function levelText(lvl: unknown): string {
  const n = Number(lvl ?? 0);
  return n === 0 ? "正常" : n === 1 ? "警告" : n === 2 ? "错误" : String(lvl);
}
</script>

<template>
  <el-table
    :data="pagedRows"
    stripe
    border
    :row-class-name="rowClass"
    style="width: 100%"
    max-height="520"
  >
    <el-table-column type="index" label="#" width="55" />
    <el-table-column label="SQL 内容" min-width="320">
      <template #default="{ row }">
        <TruncateCell :value="(row as ReviewRow).sql" :row="toRecord(row as ReviewRow)" col="sql" />
      </template>
    </el-table-column>
    <el-table-column label="状态" width="90">
      <template #default="{ row }">{{ levelText((row as ReviewRow).errlevel) }}</template>
    </el-table-column>
    <el-table-column label="信息" min-width="240" show-overflow-tooltip>
      <template #default="{ row }">{{ (row as ReviewRow).errormessage }}</template>
    </el-table-column>
    <el-table-column label="影响行数" width="100">
      <template #default="{ row }">{{ (row as ReviewRow).affected_rows }}</template>
    </el-table-column>
    <el-table-column label="AI 建议" min-width="200">
      <template #default="{ row }">
        <div class="ai-cell">
          <el-tag
            v-if="hasAi(row as ReviewRow)"
            :type="aiTagType((row as ReviewRow).ai_risk_level)"
            size="small"
            class="ai-tag"
          >
            {{ aiLevelText((row as ReviewRow).ai_risk_level) }}
            <template v-if="(row as ReviewRow).ai_risk_score">
              · {{ (row as ReviewRow).ai_risk_score }}
            </template>
          </el-tag>
          <el-tag
            v-if="hasLockRisk(row as ReviewRow)"
            :type="lockTagType((row as ReviewRow).ai_ddl_lock_risk)"
            size="small"
            effect="plain"
            class="ai-tag"
          >
            {{ lockText((row as ReviewRow).ai_ddl_lock_risk) }}
          </el-tag>
          <el-tag
            v-if="(row as ReviewRow).ai_use_osc"
            type="warning"
            size="small"
            effect="dark"
            class="ai-tag"
          >
            建议走 OSC
          </el-tag>
          <span
            v-if="(row as ReviewRow).ai_affected_rows_estimate"
            class="ai-affected"
          >影响：{{ (row as ReviewRow).ai_affected_rows_estimate }}</span>
          <span
            v-if="(row as ReviewRow).ai_summary"
            class="ai-summary"
          >{{ (row as ReviewRow).ai_summary }}</span>
          <el-popover
            v-if="(row as ReviewRow).ai_suggestion"
            trigger="click"
            placement="left"
            :width="420"
          >
            <template #reference>
              <el-button link type="primary" size="small">详情</el-button>
            </template>
            <div class="ai-suggestion" v-html="aiSuggestionHtml((row as ReviewRow).ai_suggestion)"></div>
          </el-popover>
        </div>
      </template>
    </el-table-column>
    <template v-if="phase === 'execute'">
      <el-table-column label="执行耗时" width="100">
        <template #default="{ row }">{{ (row as ReviewRow).execute_time }}</template>
      </el-table-column>
      <el-table-column label="阶段" width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ (row as ReviewRow).stagestatus }}</template>
      </el-table-column>
    </template>
    <el-table-column
      v-if="oscWorkflowId"
      label="操作"
      width="80"
      fixed="right"
    >
      <template #default="{ row }">
        <el-button
          v-if="(row as ReviewRow).sqlsha1"
          link
          type="primary"
          @click="emit('osc', row as ReviewRow)"
        >
          进度
        </el-button>
      </template>
    </el-table-column>
  </el-table>

  <div v-if="rows.length > pageSize" class="pager">
    <el-pagination
      :total="rows.length"
      :current-page="page"
      :page-size="pageSize"
      :page-sizes="[500, 1000, 5000]"
      layout="total, sizes, prev, pager, next"
      background
      @current-change="(p: number) => (page = p)"
      @size-change="(s: number) => (pageSize = s)"
    />
  </div>
</template>

<style scoped lang="scss">
.pager {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}

:deep(.row-error) {
  background: #fef0f0 !important;
}

:deep(.row-warning) {
  background: #fdf6ec !important;
}

.ai-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.ai-tag {
  flex-shrink: 0;
}

.ai-summary {
  color: var(--el-text-color-regular);
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 200px;
}

.ai-affected {
  color: var(--el-color-warning);
  font-size: 12px;
  flex-shrink: 0;
}

.ai-suggestion {
  max-height: 400px;
  overflow-y: auto;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
