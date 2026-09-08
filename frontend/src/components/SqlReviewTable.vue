<script setup lang="ts">
import { ref, computed } from "vue";
import { marked } from "marked";
import { ElMessage } from "element-plus";
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

/** AI 建议详情抽屉（替代原 420px popover：带风险标签、SQL 原文与排版后的建议正文） */
const aiDetailVisible = ref(false);
const aiDetailRow = ref<ReviewRow | null>(null);

function openAiDetail(row: ReviewRow) {
  aiDetailRow.value = row;
  aiDetailVisible.value = true;
}

async function copyText(text: string, label = "内容") {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    ElMessage.success(`${label}已复制到剪贴板`);
  } catch {
    ElMessage.warning("复制失败，请手动复制");
  }
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
          <el-button
            v-if="(row as ReviewRow).ai_suggestion"
            link
            type="primary"
            size="small"
            @click="openAiDetail(row as ReviewRow)"
          >
            详情
          </el-button>
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

  <!-- AI 建议详情抽屉 -->
  <el-drawer
    v-model="aiDetailVisible"
    direction="rtl"
    size="640px"
    :append-to-body="true"
  >
    <template #header>
      <div class="ai-drawer-header">
        <span class="ai-drawer-title">AI 审核建议</span>
        <template v-if="aiDetailRow">
          <el-tag :type="aiTagType(aiDetailRow.ai_risk_level)" effect="dark">
            {{ aiLevelText(aiDetailRow.ai_risk_level) }}
            <template v-if="aiDetailRow.ai_risk_score">
              · {{ aiDetailRow.ai_risk_score }} 分
            </template>
          </el-tag>
          <el-tag
            v-if="hasLockRisk(aiDetailRow)"
            :type="lockTagType(aiDetailRow.ai_ddl_lock_risk)"
            effect="plain"
          >
            {{ lockText(aiDetailRow.ai_ddl_lock_risk) }}
          </el-tag>
          <el-tag v-if="aiDetailRow.ai_use_osc" type="warning" effect="dark">
            建议走 OSC
          </el-tag>
        </template>
      </div>
    </template>
    <template #default>
      <div v-if="aiDetailRow" class="ai-drawer-body">
        <div v-if="aiDetailRow.ai_affected_rows_estimate" class="ai-meta">
          预估影响行数：<b>{{ aiDetailRow.ai_affected_rows_estimate }}</b>
        </div>
        <section class="ai-section">
          <div class="sec-head">
            <span class="sec-title">对应 SQL</span>
            <el-button
              link
              type="primary"
              size="small"
              @click="copyText(aiDetailRow.sql || '', 'SQL')"
            >
              复制
            </el-button>
          </div>
          <pre class="ai-sql-block"><code>{{ aiDetailRow.sql }}</code></pre>
        </section>
        <section class="ai-section">
          <div class="sec-head">
            <span class="sec-title">建议详情</span>
            <el-button
              link
              type="primary"
              size="small"
              @click="copyText(aiDetailRow.ai_suggestion || '', '建议')"
            >
              复制
            </el-button>
          </div>
          <div
            class="ai-suggestion"
            v-html="aiSuggestionHtml(aiDetailRow.ai_suggestion)"
          ></div>
        </section>
      </div>
    </template>
  </el-drawer>
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
  font-size: 13px;
  line-height: 1.7;
  word-break: break-word;
  color: var(--el-text-color-primary);

  /* markdown 排版：标题层级 */
  :deep(h1),
  :deep(h2),
  :deep(h3),
  :deep(h4) {
    margin: 14px 0 8px;
    font-size: 14px;
    font-weight: 600;
    color: var(--el-text-color-primary);

    &:first-child {
      margin-top: 0;
    }
  }

  :deep(h1),
  :deep(h2) {
    padding-bottom: 4px;
    border-bottom: 1px solid var(--el-border-color-lighter);
  }

  /* 列表 */
  :deep(ul),
  :deep(ol) {
    margin: 6px 0;
    padding-left: 20px;
  }

  :deep(li) {
    margin: 3px 0;
  }

  /* 行内代码 */
  :deep(code) {
    background: var(--el-fill-color-light);
    border: 1px solid var(--el-border-color-lighter);
    border-radius: 3px;
    padding: 1px 5px;
    font-family: var(--el-font-family-monospace, Menlo, Consolas, monospace);
    font-size: 12px;
    color: var(--el-color-danger);
  }

  /* 代码块（修改前后 SQL 对比）：不换行、横向滚动，保证 SQL 完整可读 */
  :deep(pre) {
    background: var(--el-fill-color-light);
    border: 1px solid var(--el-border-color-lighter);
    border-radius: 4px;
    padding: 10px 12px;
    margin: 8px 0;
    overflow-x: auto;

    code {
      background: none;
      border: none;
      padding: 0;
      color: var(--el-text-color-primary);
      font-size: 12px;
      line-height: 1.6;
      white-space: pre;
    }
  }

  /* markdown 表格（问题清单常用） */
  :deep(table) {
    border-collapse: collapse;
    margin: 8px 0;
    width: 100%;
    font-size: 12px;

    th,
    td {
      border: 1px solid var(--el-border-color-lighter);
      padding: 5px 8px;
      text-align: left;
    }

    th {
      background: var(--el-fill-color-light);
      font-weight: 600;
    }
  }

  :deep(blockquote) {
    margin: 8px 0;
    padding: 4px 12px;
    border-left: 3px solid var(--el-color-primary-light-5);
    color: var(--el-text-color-secondary);
  }

  :deep(p) {
    margin: 6px 0;
  }

  :deep(strong) {
    color: var(--el-color-danger);
  }
}

/* ---- AI 建议详情抽屉 ---- */
.ai-drawer-header {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ai-drawer-title {
  font-size: 16px;
  font-weight: 600;
  margin-right: 4px;
}

.ai-drawer-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ai-meta {
  font-size: 13px;
  color: var(--el-text-color-regular);
  background: var(--el-color-warning-light-9);
  border: 1px solid var(--el-color-warning-light-7);
  border-radius: 4px;
  padding: 8px 12px;
}

.ai-section {
  .sec-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
  }

  .sec-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--el-text-color-primary);
  }
}

.ai-sql-block {
  margin: 0;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  padding: 10px 12px;
  max-height: 200px;
  overflow: auto;
  font-family: Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre;
  color: var(--el-text-color-primary);
}
</style>
