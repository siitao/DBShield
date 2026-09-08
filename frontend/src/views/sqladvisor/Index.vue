<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { ElMessage } from "element-plus";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { useInstanceSelect } from "@/composables/useInstanceSelect";
import { fetchQueryResources } from "@/api/sqlquery";
import SqlEditor from "@/components/SqlEditor.vue";
import {
  optimizeSqlAdvisor,
  optimizeSoar,
  optimizeSqlTuning,
  explainSql,
  optimizeSqlByAIAsync,
  pollOptimizeTask,
  type AiOptimizeStep,
} from "@/api/phase2";
import TruncateCell from "@/components/TruncateCell.vue";

// gfm 表格渲染
marked.setOptions({ gfm: true, breaks: false });

const { instanceName, instanceGroups, currentInstance, currentDbType, loadInstances } =
  useInstanceSelect();

const dbName = ref("");
const dbOptions = ref<string[]>([]);
const sqlText = ref("");
type OptTool = "advisor" | "soar" | "tuning" | "explain" | "ai";
// 默认 AI 优化（P3 整合：AI Agent 取证能力覆盖传统工具的主要场景）
const tool = ref<OptTool>("ai");
const loading = ref(false);

const resultText = ref(""); // advisor/soar/ai 的文本结果（markdown 与否由 resultTool 决定）
const resultTool = ref<OptTool | "">(""); // 当前 resultText 由哪个工具产生，决定渲染方式（pre/markdown）
const explainCols = ref<string[]>([]);
const explainRows = ref<unknown[][]>([]);
// AI Agent 取证轨迹（仅 AI 优化）
const aiSteps = ref<AiOptimizeStep[]>([]);
// AI 优化异步任务轮询（独立于工具选择：切走工具/运行其他工具都不中断）
const aiPolling = ref(false);
let aiPollTimer: number | null = null;

function stopAiPolling() {
  if (aiPollTimer) {
    clearInterval(aiPollTimer);
    aiPollTimer = null;
  }
  aiPolling.value = false;
}

const AI_POLL_INTERVAL_MS = 3000;
const AI_POLL_TIMEOUT_MS = 300000; // 与后端 Agent 预算（240s）+ 余量对齐

/** 轮询异步优化任务直至成功/失败/超时（独立于当前工具选择） */
function pollAiTask(taskId: number) {
  aiPolling.value = true;
  const startedAt = Date.now();
  aiPollTimer = window.setInterval(async () => {
    try {
      const t = await pollOptimizeTask(taskId);
      if (t?.status === "success") {
        resultText.value = t.report || "";
        resultTool.value = "ai";
        aiSteps.value = t.steps || [];
        stopAiPolling();
        // 完成时若用户停留在其他工具上，主动提示报告已就绪
        if (tool.value !== "ai") {
          ElMessage.success("AI 优化报告已完成，切回「AI 优化」可查看完整报告与取证轨迹");
        }
      } else if (t?.status === "failed") {
        ElMessage.error(t.error || "AI 优化失败，请稍后重试");
        stopAiPolling();
      } else if (Date.now() - startedAt > AI_POLL_TIMEOUT_MS) {
        ElMessage.error("AI 优化任务超时，请稍后重试");
        stopAiPolling();
      }
    } catch {
      // 拦截器已提示
      stopAiPolling();
    }
  }, AI_POLL_INTERVAL_MS);
}

/** AI 优化入口：异步提交（缓存命中直接出报告）+ 轮询取报告 */
async function runAiOptimize() {
  // 重跑 AI 时先停掉在途轮询（后端同指纹去重会复用任务，避免双定时器）
  stopAiPolling();
  const submitted = await optimizeSqlByAIAsync({
    instance_name: instanceName.value,
    db_name: dbName.value,
    sql_content: sqlText.value,
  });
  if (submitted?.hit_cache) {
    resultText.value = submitted.report || "";
    resultTool.value = "ai";
    aiSteps.value = submitted.steps || [];
    loading.value = false;
    return;
  }
  const taskId = submitted?.task_id;
  if (!taskId) {
    loading.value = false;
    return;
  }
  resultText.value = "";
  aiSteps.value = [];
  loading.value = false;
  pollAiTask(taskId);
}

const AI_STEP_LABELS: Record<string, string> = {
  list_tables: "查看表清单",
  get_table_ddl: "查看建表语句",
  get_table_indexes: "查看索引",
  get_table_stats: "查看行数与大小",
  run_explain: "执行 EXPLAIN 验证执行计划",
  run_explain_analyze: "EXPLAIN ANALYZE 获取真实行数",
  list_collections: "查看集合列表",
  get_collection_indexes: "查看集合索引",
  get_collection_fields: "查看集合字段与类型",
  get_collection_stats: "查看集合行数与大小",
};

function aiStepLabel(s: AiOptimizeStep): string {
  const target = s.args?.table || s.args?.collection || "";
  return `${AI_STEP_LABELS[s.tool] || s.tool}${target ? " " + target : ""}`;
}

// tuning 维度复选框，对应后端 option
const tuningOptions = ref<string[]>(["sys_parm", "sql_plan", "obj_stat", "sql_profile"]);
const tuningOptionItems: { value: string; label: string }[] = [
  { value: "sys_parm", label: "系统参数" },
  { value: "sql_plan", label: "SQL 计划" },
  { value: "obj_stat", label: "对象统计" },
  { value: "sql_profile", label: "会话状态" },
];
// tuning 结构化结果
type TableData = { column_list: string[]; rows: unknown[][] };
type TuningResult = Record<string, unknown>;
const tuningData = ref<TuningResult>({});

// 高级工具依赖 MySQL 语法与工具链，非 mysql 实例禁用（AI 优化不限类型）
const advancedTools = computed(() => {
  const mysqlOnly = currentDbType.value !== "mysql";
  return [
    { key: "advisor", label: "SQLAdvisor", disabled: mysqlOnly },
    { key: "soar", label: "SOAR", disabled: mysqlOnly },
    { key: "tuning", label: "MySQL 调优", disabled: mysqlOnly },
    { key: "explain", label: "执行计划", disabled: mysqlOnly },
  ];
});

// 切到非 mysql 实例时，若当前工具是 mysql 专属则自动切回 AI 优化
watch(currentDbType, (t) => {
  if (t && t !== "mysql" && ["advisor", "soar", "tuning", "explain"].includes(tool.value)) {
    tool.value = "ai";
  }
});

/** tuning 各 section 的中文标题映射 */
const TUNING_SECTION_TITLE: Record<string, string> = {
  basic_information: "基本信息",
  sys_parameter: "系统参数",
  optimizer_switch: "优化器开关",
  optimizer_rewrite_sql: "优化器改写",
  plan: "执行计划",
  object_statistics: "对象统计",
  session_status: "会话状态",
  sqltext: "SQL 语句",
  structure: "表结构",
  table_info: "表信息",
  index_info: "索引信息",
  EXECUTE_TIME: "执行耗时",
  BEFORE_STATUS: "执行前状态",
  AFTER_STATUS: "执行后状态",
  "SESSION_STATUS(DIFFERENT)": "状态差异",
  PROFILING_DETAIL: "Profile 明细",
  PROFILING_SUMMARY: "Profile 汇总",
};

/** SQL 长文本列名集合 */
const SQL_COLUMNS = new Set(["sql", "query", "info", "detail", "plan", "suggestion"]);

/** 判断是否为 {column_list, rows} 表格数据 */
function isTableData(v: unknown): v is TableData {
  return (
    !!v &&
    typeof v === "object" &&
    Array.isArray((v as TableData).column_list) &&
    Array.isArray((v as TableData).rows)
  );
}

/** 把 {column_list, rows} 的行数组 zip 成对象数组，便于 el-table 渲染 */
function zipRows(table: TableData): Record<string, unknown>[] {
  return table.rows.map((row) => {
    const o: Record<string, unknown> = {};
    table.column_list.forEach((_col, idx) => {
      o[String(idx)] = (row as unknown[])[idx];
    });
    return o;
  });
}

function sectionTitle(key: string): string {
  return TUNING_SECTION_TITLE[key] || key;
}

/** resultText（markdown，soar / ai）→ 安全 HTML */
const resultHtml = computed(() => {
  if (!resultText.value) return "";
  const raw = marked.parse(resultText.value, { async: false }) as string;
  return DOMPurify.sanitize(raw);
});

function isSqlColumn(col: string): boolean {
  return SQL_COLUMNS.has(col.toLowerCase());
}

async function loadDbs() {
  if (!currentInstance.value) return;
  try {
    dbOptions.value = await fetchQueryResources({
      instance_id: currentInstance.value.id,
      resource_type: "database",
    });
  } catch {
    // 拦截器已提示
  }
}

watch(instanceName, () => {
  dbName.value = "";
  if (currentInstance.value) loadDbs();
});

async function onRun() {
  if (!instanceName.value || !dbName.value)
    return ElMessage.warning("请选择实例和库");
  if (!sqlText.value.trim()) return ElMessage.warning("请输入 SQL");
  // 注意：不在这里停 AI 轮询——轮询独立于工具选择，运行其他工具不影响
  // 在途的 AI 任务，报告完成后照常回填展示（runAiOptimize 内部自行管理轮询）
  loading.value = true;
  resultText.value = "";
  resultTool.value = "";
  explainCols.value = [];
  explainRows.value = [];
  tuningData.value = {};
  aiSteps.value = [];
  try {
    if (tool.value === "advisor") {
      resultText.value = await optimizeSqlAdvisor({
        instance_name: instanceName.value,
        db_name: dbName.value,
        sql_content: sqlText.value,
      });
      resultTool.value = "advisor";
    } else if (tool.value === "soar") {
      resultText.value = await optimizeSoar({
        instance_name: instanceName.value,
        db_name: dbName.value,
        sql: sqlText.value,
      });
      resultTool.value = "soar";
    } else if (tool.value === "tuning") {
      tuningData.value = await optimizeSqlTuning({
        instance_name: instanceName.value,
        db_name: dbName.value,
        sql_content: sqlText.value,
        option: tuningOptions.value,
      });
    } else if (tool.value === "ai") {
      await runAiOptimize();
    } else {
      const r = await explainSql({
        instance_name: instanceName.value,
        db_name: dbName.value,
        sql_content: sqlText.value,
      });
      explainCols.value = r.column_list || [];
      explainRows.value = r.rows || [];
    }
  } catch {
    // 拦截器已提示（AI 轮询不受其他工具失败影响，保持运行）
    loading.value = false;
  }
}

onMounted(loadInstances);
onUnmounted(stopAiPolling);
</script>

<template>
  <div v-loading="loading" class="advisor-page">
    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent>
        <el-form-item label="实例">
          <el-select v-model="instanceName" filterable placeholder="选择实例" style="width: 220px">
            <el-option-group v-for="g in instanceGroups" :key="g.label" :label="g.label">
              <el-option v-for="i in g.items" :key="i.id" :label="i.instance_name" :value="i.instance_name" />
            </el-option-group>
          </el-select>
        </el-form-item>
        <el-form-item label="库">
          <el-select v-model="dbName" filterable placeholder="选择库" style="width: 200px">
            <el-option v-for="d in dbOptions" :key="d" :label="d" :value="d" />
          </el-select>
        </el-form-item>
        <el-form-item label="工具">
          <el-select v-model="tool" style="width: 180px">
            <el-option-group label="AI 智能">
              <el-option value="ai" label="AI 优化（推荐）" />
            </el-option-group>
            <el-option-group label="高级工具">
              <el-option
                v-for="t in advancedTools"
                :key="t.key"
                :label="t.label"
                :value="t.key"
                :disabled="t.disabled"
              />
            </el-option-group>
          </el-select>
        </el-form-item>
        <el-form-item v-if="tool === 'tuning'" label="维度">
          <el-checkbox-group v-model="tuningOptions">
            <el-checkbox v-for="o in tuningOptionItems" :key="o.value" :value="o.value">{{ o.label }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <SqlEditor v-model="sqlText" />
      <div class="actions">
        <el-button type="primary" @click="onRun">执行</el-button>
      </div>
    </el-card>

    <el-card v-if="resultText" shadow="never">
      <template #header>建议</template>
      <!-- 渲染方式按"产生该结果的工具"决定，而非当前选中的工具：
           AI/soar 报告轮询完成时用户可能停留在其他工具上 -->
      <pre v-if="resultTool !== 'soar' && resultTool !== 'ai'" class="result-text">{{ resultText }}</pre>
      <div v-else class="markdown" v-html="resultHtml" />
    </el-card>

    <el-alert
      v-if="aiPolling"
      type="info"
      :closable="false"
      show-icon
      class="polling-hint"
      title="AI 正在主动取证分析（查看表结构 / 索引 / 执行计划）——与当前选择的工具无关，完成后会自动展示报告"
    />
    <el-card v-if="tool === 'ai' && aiSteps.length" shadow="never">
      <template #header>AI 诊断过程（{{ aiSteps.length }} 次取证）</template>
      <div class="ai-steps">
        <div v-for="(s, i) in aiSteps" :key="i" class="ai-step">
          <el-tag :type="s.ok ? 'info' : 'danger'" size="small" class="ai-step-index">
            {{ i + 1 }}
          </el-tag>
          <span class="ai-step-text">{{ aiStepLabel(s) }}</span>
          <span class="ai-step-elapsed">{{ s.elapsed }}s</span>
        </div>
      </div>
    </el-card>

    <el-card v-if="explainCols.length" shadow="never">
      <template #header>执行计划</template>
      <el-table :data="explainRows" stripe border max-height="420">
        <el-table-column
          v-for="(col, idx) in explainCols"
          :key="col"
          :prop="String(idx)"
          :label="col"
          min-width="140"
          :show-overflow-tooltip="!isSqlColumn(col)"
        >
          <template v-if="isSqlColumn(col)" #default="{ row }">
            <TruncateCell :value="String((row as unknown[])[idx])" :row="row as unknown as Record<string,unknown>" :col="col" />
          </template>
          <template v-else #default="{ row }">{{ (row as unknown[])[idx] }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- MySQL 调优：结构化分区展示 -->
    <template v-if="tool === 'tuning' && Object.keys(tuningData).length">
      <!-- 一级 section：表格 / 列表 / 字符串 -->
      <el-card
        v-for="(value, key) in tuningData"
        :key="key"
        shadow="never"
        class="tuning-card"
      >
        <template #header>{{ sectionTitle(String(key)) }}</template>
        <!-- 1) 单表格 -->
        <el-table
          v-if="isTableData(value)"
          :data="zipRows(value)"
          stripe
          border
          max-height="420"
        >
          <el-table-column
            v-for="(col, idx) in value.column_list"
            :key="col"
            :prop="String(idx)"
            :label="col"
            min-width="140"
            :show-overflow-tooltip="!isSqlColumn(col)"
          >
            <template v-if="isSqlColumn(col)" #default="{ row }">
              <TruncateCell :value="String(row[String(idx)])" :row="row" :col="col" />
            </template>
            <template v-else #default="{ row }">{{ row[String(idx)] }}</template>
          </el-table-column>
        </el-table>
        <!-- 2) 字符串（sqltext / EXECUTE_TIME） -->
        <pre v-else-if="typeof value === 'string'" class="result-text">{{ value }}</pre>
        <!-- 3) 对象统计：表数组，每张表含 structure/table_info/index_info -->
        <div v-else-if="Array.isArray(value)" class="obj-stat">
          <div v-for="(tbl, ti) in value" :key="ti" class="obj-stat-table">
            <div class="obj-stat-title">表 #{{ ti + 1 }}</div>
            <el-card
              v-for="(subVal, subKey) in tbl as Record<string, unknown>"
              :key="subKey"
              shadow="never"
              class="tuning-sub-card"
            >
              <template #header>{{ sectionTitle(String(subKey)) }}</template>
              <el-table
                v-if="isTableData(subVal)"
                :data="zipRows(subVal)"
                stripe
                border
                max-height="360"
              >
                <el-table-column
                  v-for="(col, idx) in subVal.column_list"
                  :key="col"
                  :prop="String(idx)"
                  :label="col"
                  min-width="140"
                  :show-overflow-tooltip="!isSqlColumn(col)"
                >
                  <template v-if="isSqlColumn(col)" #default="{ row }">
                    <TruncateCell :value="String(row[String(idx)])" :row="row" :col="col" />
                  </template>
                  <template v-else #default="{ row }">{{ row[String(idx)] }}</template>
                </el-table-column>
              </el-table>
              <pre v-else-if="typeof subVal === 'string'" class="result-text">{{ subVal }}</pre>
            </el-card>
          </div>
        </div>
        <!-- 4) 会话状态：嵌套字典（EXECUTE_TIME 字符串 + 多个表格） -->
        <div v-else-if="value && typeof value === 'object'" class="session-status">
          <pre v-if="'EXECUTE_TIME' in (value as Record<string, unknown>)" class="result-text">执行耗时：{{ (value as Record<string, unknown>).EXECUTE_TIME }} s</pre>
          <el-card
            v-for="(subVal, subKey) in value as Record<string, unknown>"
            :key="subKey"
            shadow="never"
            class="tuning-sub-card"
          >
            <template #header>{{ sectionTitle(String(subKey)) }}</template>
            <el-table
              v-if="isTableData(subVal)"
              :data="zipRows(subVal)"
              stripe
              border
              max-height="360"
            >
              <el-table-column
                v-for="(col, idx) in subVal.column_list"
                :key="col"
                :prop="String(idx)"
                :label="col"
                min-width="140"
                :show-overflow-tooltip="!isSqlColumn(col)"
              >
                <template v-if="isSqlColumn(col)" #default="{ row }">
                  <TruncateCell :value="String(row[String(idx)])" :row="row" :col="col" />
                </template>
                <template v-else #default="{ row }">{{ row[String(idx)] }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </div>
      </el-card>
    </template>
  </div>
</template>

<style scoped lang="scss">
.advisor-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.ai-steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ai-step {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.ai-step-index {
  flex-shrink: 0;
}

.ai-step-text {
  color: var(--el-text-color-primary);
}

.ai-step-elapsed {
  margin-left: auto;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.filter-card :deep(.el-form-item) {
  margin-bottom: 0;
}

.actions {
  margin-top: 12px;
}

.result-text {
  margin: 0;
  padding: 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  font-family: monospace;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-all;
}

.markdown {
  :deep(table) {
    border-collapse: collapse;
  }
  :deep(th),
  :deep(td) {
    border: 1px solid var(--el-border-color);
    padding: 4px 8px;
  }
}

.tuning-card {
  margin-bottom: 0;
}

.obj-stat {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.obj-stat-title {
  font-weight: 600;
  margin-bottom: 8px;
}

.tuning-sub-card {
  margin-bottom: 8px;
  :deep(.el-card__header) {
    padding: 8px 12px;
    font-size: 13px;
  }
}
</style>
