<script setup lang="ts">
import { ref, computed } from "vue";
import { ElMessage } from "element-plus";
import { marked } from "marked";
import DOMPurify from "dompurify";
import SqlEditor from "@/components/SqlEditor.vue";
import { generateAnalyze, analyzeSql } from "@/api/phase2";
import TruncateCell from "@/components/TruncateCell.vue";

// gfm 表格渲染
marked.setOptions({ gfm: true, breaks: false });

const sqlText = ref("");
const loading = ref(false);

// generate 结果（行级）
const analyzeRows = ref<Record<string, unknown>[]>([]);
const analyzeColumns = ref<string[]>([]);

// 深度分析报告（markdown/html）
const report = ref("");
const reportLoading = ref(false);

/** SQL 长文本列名集合 */
const SQL_COLUMNS = new Set(["sql", "sqltext", "text", "errormessage", "message", "detail"]);

function isSqlColumn(col: string): boolean {
  return SQL_COLUMNS.has(col.toLowerCase());
}

async function onGenerate() {
  if (!sqlText.value.trim()) return ElMessage.warning("请输入 SQL");
  loading.value = true;
  analyzeRows.value = [];
  analyzeColumns.value = [];
  try {
    const r = await generateAnalyze(sqlText.value);
    analyzeRows.value = r.rows;
    if (r.rows.length) analyzeColumns.value = Object.keys(r.rows[0]);
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false;
  }
}

async function onAnalyze() {
  if (!sqlText.value.trim()) return ElMessage.warning("请输入 SQL");
  reportLoading.value = true;
  report.value = "";
  try {
    report.value = await analyzeSql({
      text: sqlText.value,
      instance_name: "",
      db_name: "",
    });
  } catch {
    // 拦截器已提示
  } finally {
    reportLoading.value = false;
  }
}

/** report（markdown）→ 安全 HTML */
const reportHtml = computed(() => {
  if (!report.value) return "";
  const raw = marked.parse(report.value, { async: false }) as string;
  return DOMPurify.sanitize(raw);
});
</script>

<template>
  <div v-loading="loading" class="analyze-page">
    <el-card shadow="never">
      <template #header>SQL 分析（语法/规范）</template>
      <SqlEditor v-model="sqlText" />
      <div class="actions">
        <el-button type="primary" @click="onGenerate">生成分析</el-button>
        <el-button @click="onAnalyze" :loading="reportLoading">深度分析（SOAR）</el-button>
      </div>
    </el-card>

    <el-card v-if="analyzeColumns.length" shadow="never">
      <template #header>分析结果（{{ analyzeRows.length }}）</template>
      <el-table :data="analyzeRows" stripe border max-height="420">
        <el-table-column
          v-for="col in analyzeColumns"
          :key="col"
          :prop="col"
          :label="col"
          min-width="160"
          :show-overflow-tooltip="!isSqlColumn(col)"
        >
          <template v-if="isSqlColumn(col)" #default="{ row }">
            <TruncateCell :value="(row as Record<string,unknown>)[col]" :row="row as Record<string,unknown>" :col="col" />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card v-if="report" shadow="never">
      <template #header>深度分析报告</template>
      <div class="report" v-html="reportHtml" />
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.analyze-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.actions {
  margin-top: 12px;
  display: flex;
  gap: 8px;
}

.report {
  :deep(table) {
    border-collapse: collapse;
    width: 100%;
  }
  :deep(th),
  :deep(td) {
    border: 1px solid var(--el-border-color);
    padding: 6px 10px;
  }
  :deep(pre) {
    white-space: pre-wrap;
    word-break: break-all;
  }
}
</style>
