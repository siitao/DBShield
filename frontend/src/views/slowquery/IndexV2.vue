<script setup lang="ts">
import { ref, watch, onMounted, computed } from "vue";
import { ElMessage } from "element-plus";
import type { EChartsOption } from "echarts";
import { useInstanceSelect } from "@/composables/useInstanceSelect";
import { fetchQueryResources, checkOpenai } from "@/api/sqlquery";
import { useAuthStore } from "@/stores/auth";
import {
  fetchSlowSummaryV2,
  fetchSlowDetailV2,
  fetchSlowTrendV2,
  getDiagnosedHashes,
} from "@/api/phase2";
import EChart from "@/components/EChart.vue";
import TruncateCell from "@/components/TruncateCell.vue";
import DiagnosisDrawer from "./DiagnosisDrawer.vue";

const { instanceName, instanceGroups, currentInstance, loadInstances } =
  useInstanceSelect();

const dbName = ref("");
const dbOptions = ref<string[]>([]);
const dateRange = ref<[string, string] | null>(null);
const activeTab = ref("detail");

const summaryRows = ref<Record<string, unknown>[]>([]);
const summaryCols = ref<string[]>([]);
const detailRows = ref<Record<string, unknown>[]>([]);
const detailCols = ref<string[]>([]);
const loading = ref(false);

// 分页
const currentPage = ref(1);
const pageSize = ref(50);
const total = ref(0);

// 当前数据库类型
const currentDbType = computed(() => currentInstance.value?.db_type || "");

/** SQL 长文本列名集合（大小写不敏感，含中文列名） */
const SQL_COLUMNS = new Set([
  // 英文列名
  "sqltext", "sql_text", "sql", "info", "fingerprint",
  "commandtext", "command_text", "samplesql", "sample_sql",
  // 中文列名（阿里云/MongoDB 返回）
  "sql 文本", "sql 文档", "命令文本", "命令", "示例sql", "示例命令",
]);

function isSqlColumn(col: string): boolean {
  return SQL_COLUMNS.has(col.toLowerCase());
}

/**
 * 统一列配置
 *
 * label: 中文表头
 * summary: 是否在统计页显示
 * detail: 是否在明细页显示
 */
interface ColumnConfig {
  label: string;
  summary?: boolean;
  detail?: boolean;
}

/** 字段配置映射 */
const COLUMN_CONFIG: Record<string, ColumnConfig> = {
  // 通用字段
  SQLText: { label: "SQL 文本", summary: true, detail: true },
  SQLId: { label: "SQL ID" },
  FingerPrint: { label: "SQL 指纹" },
  DBName: { label: "数据库", summary: true, detail: true },
  CreateTime: { label: "最近出现时间", summary: true },
  ExecutionStartTime: { label: "执行开始时间", detail: true },
  HostAddress: { label: "客户端地址", detail: true },
  UserName: { label: "用户名", detail: true },

  // MySQL 统计字段
  MySQLTotalExecutionCounts: { label: "执行次数", summary: true },
  MySQLTotalExecutionTimes: { label: "总执行耗时(ms)", summary: true },

  // 通用统计字段
  TotalExecutionCounts: { label: "执行次数", summary: true },
  TotalExecutionTimes: { label: "总执行耗时(ms)", summary: true },
  QueryTimeAvg: { label: "平均耗时(ms)", summary: true },
  QueryTimePct95: { label: "95% 耗时(ms)", summary: true },
  ParseRowAvg: { label: "平均扫描行数", summary: true },
  ReturnRowAvg: { label: "平均返回行数", summary: true },

  // MySQL 明细字段
  QueryTimes: { label: "执行耗时(ms)", detail: true },
  LockTimes: { label: "锁等待时间(ms)", detail: true },
  ParseRowCounts: { label: "扫描行数", detail: true },
  ReturnRowCounts: { label: "返回行数", detail: true },
  ParseTotalRowCounts: { label: "总扫描行数", summary: true },
  ReturnTotalRowCounts: { label: "总返回行数", summary: true },

  // PgSQL 字段
  SharedBlksHit: { label: "缓存命中", summary: true, detail: true },
  SharedBlksRead: { label: "磁盘读取", summary: true, detail: true },

  // MongoDB 字段
  OperationType: { label: "操作类型", summary: true, detail: true },
  CollectionName: { label: "集合", summary: true, detail: true },
  DocsExaminedAvg: { label: "平均扫描文档数", summary: true },
  DocsReturnedAvg: { label: "平均返回文档数", summary: true },
  DocsExamined: { label: "扫描文档数", detail: true },
  DocsReturned: { label: "返回文档数", detail: true },
  NReturned: { label: "返回结果数", detail: true },
  HasSort: { label: "包含排序", summary: true, detail: true },
  PlanSummary: { label: "执行计划", detail: true },
  CommandText: { label: "命令文本", detail: true },

  // Redis 字段
  DurationPct95: { label: "95% 耗时(ms)", summary: true },
  HostName: { label: "主机", detail: true },
  Duration: { label: "执行耗时(ms)", detail: true },
};

/** 获取列标签 */
function colLabel(col: string): string {
  return COLUMN_CONFIG[col]?.label || col;
}

/** 可见的统计列 */
const visibleSummaryCols = computed(() => {
  return summaryCols.value.filter((col) => COLUMN_CONFIG[col]?.summary);
});

/** 可见的明细列 */
const visibleDetailCols = computed(() => {
  // 阿里云 RDS 数据，直接返回所有字段
  const firstRow = detailRows.value[0];
  if (firstRow && !firstRow.hasOwnProperty("sql_hash")) {
    return detailCols.value;
  }

  // 本地数据库：根据配置过滤
  return detailCols.value.filter((col) => COLUMN_CONFIG[col]?.detail);
});

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

function dateStr(i: 0 | 1) {
  return dateRange.value?.[i] || undefined;
}

async function loadSummary() {
  if (!instanceName.value) return;
  loading.value = true;
  summaryRows.value = [];
  try {
    const r = await fetchSlowSummaryV2({
      instance_name: instanceName.value,
      db_name: dbName.value,
      StartTime: dateStr(0),
      EndTime: dateStr(1),
      limit: pageSize.value,
      offset: (currentPage.value - 1) * pageSize.value,
    });
    summaryRows.value = r.rows;
    total.value = r.total;
    if (r.rows.length) summaryCols.value = Object.keys(r.rows[0]);
    // 异步检查已诊断状态（不阻塞主流程）
    checkDiagnosedStatus(r.rows);
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false;
  }
}

async function loadDetail() {
  if (!instanceName.value) return;
  loading.value = true;
  detailRows.value = [];
  try {
    const r = await fetchSlowDetailV2({
      instance_name: instanceName.value,
      db_name: dbName.value,
      StartTime: dateStr(0),
      EndTime: dateStr(1),
      limit: pageSize.value,
      offset: (currentPage.value - 1) * pageSize.value,
    });
    detailRows.value = r.rows;
    total.value = r.total;
    if (r.rows.length) detailCols.value = Object.keys(r.rows[0]);
    // 异步检查已诊断状态（不阻塞主流程）
    checkDiagnosedStatus(r.rows);
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false;
  }
}

// 判断是否是阿里云 RDS（通过实例名称或后端返回的标识）
const isAliyunRds = computed(() => {
  // 可以通过实例名称特征判断，或者后端返回的字段
  // 这里简单判断：如果实例名称包含特定标识或后端返回了相关字段
  return currentInstance.value?.is_aliyun_rds || false;
});

// 判断是否是阿里云 Redis RDS
const isAliyunRedisRds = computed(() => {
  return isAliyunRds.value && currentDbType.value === "redis";
});

function onQuery() {
  // 阿里云 RDS 时间条件验证
  if (isAliyunRds.value) {
    if (!dateRange.value || !dateRange.value[0] || !dateRange.value[1]) {
      ElMessage.warning("阿里云 RDS 实例必须选择时间范围");
      return;
    }

    // Redis RDS 时间范围最大为一天
    if (isAliyunRedisRds.value) {
      const start = new Date(dateRange.value[0]);
      const end = new Date(dateRange.value[1]);
      const diffDays = (end.getTime() - start.getTime()) / (1000 * 60 * 60 * 24);
      if (diffDays > 1) {
        ElMessage.warning("阿里云 Redis RDS 时间范围最大为1天");
        return;
      }
    }
  }

  currentPage.value = 1;
  if (activeTab.value === "summary") loadSummary();
  else loadDetail();
}

function onPageChange(page: number) {
  currentPage.value = page;
  if (activeTab.value === "summary") loadSummary();
  else loadDetail();
}

function onSizeChange(size: number) {
  pageSize.value = size;
  currentPage.value = 1;
  if (activeTab.value === "summary") loadSummary();
  else loadDetail();
}

watch(instanceName, () => {
  dbName.value = "";
  if (currentInstance.value) loadDbs();
});

function onTabChange(name: string | number) {
  currentPage.value = 1;
  total.value = 0;
  if (String(name) === "summary") loadSummary();
  else loadDetail();
}

// 趋势弹窗
const trendVisible = ref(false);
const trendLoading = ref(false);
const trendOption = ref<Record<string, unknown>>({});
const trendTitle = ref("");

async function openTrend(row: Record<string, unknown>) {
  const sqlHash = String(row.SQLId || row.sql_hash || "");
  if (!sqlHash) return ElMessage.warning("该行无 SQL ID，无法查看趋势");

  trendTitle.value = String(row.SQLText || row.fingerprint || sqlHash).slice(0, 80);
  trendVisible.value = true;
  trendLoading.value = true;
  trendOption.value = {};

  try {
    const r = await fetchSlowTrendV2({
      instance_name: instanceName.value,
      sql_hash: sqlHash,
      days: 7,
    });

    if (r.status !== 0) {
      ElMessage.error(r.msg || "获取趋势数据失败");
      return;
    }

    const data = r.data || [];
    const dates = data.map((d) => d.date);
    const counts = data.map((d) => d.count);
    const avgTimes = data.map((d) => d.avg_time);

    trendOption.value = {
      title: {
        text: "SQL 历史趋势（近7天）",
        left: "center",
        textStyle: { fontSize: 14 },
      },
      tooltip: { trigger: "axis" },
      legend: { top: 24 },
      grid: {
        left: 56,
        right: 24,
        top: 56,
        bottom: 48,
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: dates,
        boundaryGap: false,
        axisLabel: { interval: 0, rotate: dates.length > 7 ? 45 : 0 },
      },
      yAxis: [
        { type: "value", name: "执行次数" },
        { type: "value", name: "平均耗时(ms)" },
      ],
      series: [
        {
          name: "执行次数",
          type: "line",
          smooth: true,
          areaStyle: { opacity: 0.3 },
          data: counts,
        },
        {
          name: "平均耗时",
          type: "line",
          smooth: true,
          yAxisIndex: 1,
          showSymbol: false,
          areaStyle: { opacity: 0.3 },
          data: avgTimes,
        },
      ],
    } as EChartsOption;
  } catch {
    // 拦截器已提示
  } finally {
    trendLoading.value = false;
  }
}

// ---- AI 诊断 ----

const diagnosisVisible = ref(false);
const diagnosisSqlHash = ref("");
const diagnosisSqlText = ref("");
const diagnosisDbName = ref("");
// 已诊断的 sql_hash 集合（用于显示"已诊断"状态）
const diagnosedHashes = ref<Set<string>>(new Set());

// ---- AI 诊断入口门禁（M1）----
// 后端 POST 已有 use_ai_diagnosis 权限 + OpenAI 配置双重校验，这里做前端探测
// 提前置灰，避免无权限/未配置用户进入抽屉（对照 sqlanalyze/Index.vue 的 checkOpenai 模式）
const authStore = useAuthStore();
const openaiEnabled = ref(false);
const canUseAIDiagnosis = computed(
  () => openaiEnabled.value && authStore.hasPerm("sql.use_ai_diagnosis")
);
const diagGateTip = computed(() => {
  if (!openaiEnabled.value) return "AI 服务未配置（需在系统配置中填写 API Key）";
  if (!authStore.hasPerm("sql.use_ai_diagnosis")) return "无 AI 慢查诊断权限（sql.use_ai_diagnosis）";
  return "";
});

async function checkDiagGate() {
  try {
    const { data } = await checkOpenai();
    openaiEnabled.value = data.status === 0 && !!data.data?.openai;
  } catch {
    openaiEnabled.value = false;
  }
}

/** 打开诊断抽屉 */
function openDiagnosis(row: Record<string, unknown>) {
  const sqlHash = String(row.SQLId || row.sql_hash || "");
  if (!sqlHash) {
    ElMessage.warning("该数据源无 SQL 指纹，不支持 AI 诊断（仅支持按指纹聚合的本地采集数据）");
    return;
  }
  const sqlText = String(row.SQLText || row.fingerprint || row.sample_sql || sqlHash);

  diagnosisSqlHash.value = sqlHash;
  diagnosisSqlText.value = sqlText;
  diagnosisDbName.value = String(row.DBName || row.db_name || dbName.value || "");
  diagnosisVisible.value = true;
}

/** 行是否有可诊断的 SQL 指纹 */
function hasDiagnosableId(row: Record<string, unknown>): boolean {
  return Boolean(row.SQLId || row.sql_hash);
}

/** 诊断入口按钮是否禁用：无指纹，或（未诊断 且 无 AI 权限/未配置 OpenAI） */
function diagEntryDisabled(row: Record<string, unknown>): boolean {
  if (!hasDiagnosableId(row)) return true;
  return !isDiagnosed(row) && !canUseAIDiagnosis.value;
}

/** 诊断入口 tooltip：按原因给出可读提示（空串则禁用 tooltip） */
function diagEntryTip(row: Record<string, unknown>): string {
  if (!hasDiagnosableId(row)) return "该数据源无 SQL 指纹，不支持 AI 诊断";
  if (!isDiagnosed(row)) return diagGateTip.value;
  return "";
}

/** 批量检查已诊断状态（统计数据加载后调用，单次请求避免 N+1） */
async function checkDiagnosedStatus(rows: Record<string, unknown>[]) {
  if (!rows.length || !instanceName.value) return;
  const hashes = rows
    .map((r) => String(r.SQLId || r.sql_hash || ""))
    .filter(Boolean);
  if (!hashes.length) return;

  // 后端 batch_status 单次上限 100，整页（最大 200）分批查询（M5），
  // 避免 200 行页内超出 50 条的行被误标为"AI诊断"
  const BATCH = 100;
  const diagnosed = new Set<string>();
  try {
    for (let i = 0; i < hashes.length; i += BATCH) {
      const chunk = hashes.slice(i, i + BATCH);
      const data = await getDiagnosedHashes({
        instance_name: instanceName.value,
        db_name: dbName.value,
        hashes: chunk,
      });
      (data?.diagnosed || []).forEach((h) => diagnosed.add(h));
    }
    diagnosedHashes.value = diagnosed;
  } catch {
    // 接口失败时标记为空，不阻断列表展示
    diagnosedHashes.value = new Set();
  }
}

/** 判断行是否已诊断 */
function isDiagnosed(row: Record<string, unknown>): boolean {
  const hash = String(row.SQLId || row.sql_hash || "");
  return diagnosedHashes.value.has(hash);
}

/** 诊断完成：把该 SQL 指纹加入已诊断集合，行内按钮立即变"已诊断" */
function onDiagnosed(sqlHash: string) {
  if (sqlHash) diagnosedHashes.value.add(sqlHash);
}

onMounted(() => {
  loadInstances();
  checkDiagGate();
});
</script>

<template>
  <div class="slow-page">
    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent>
        <el-form-item label="实例">
          <el-select
            v-model="instanceName"
            filterable
            placeholder="选择实例"
            style="width: 220px"
          >
            <el-option-group
              v-for="g in instanceGroups"
              :key="g.label"
              :label="g.label"
            >
              <el-option
                v-for="i in g.items"
                :key="i.id"
                :label="i.instance_name"
                :value="i.instance_name"
              />
            </el-option-group>
          </el-select>
        </el-form-item>
        <el-form-item label="库">
          <el-select
            v-model="dbName"
            filterable
            placeholder="全部"
            clearable
            style="width: 180px"
          >
            <el-option v-for="d in dbOptions" :key="d" :label="d" :value="d" />
          </el-select>
        </el-form-item>
        <el-form-item label="时间">
          <el-date-picker
            v-model="dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            range-separator="-"
            start-placeholder="开始"
            end-placeholder="结束"
            style="width: 240px"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="onQuery">查询</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <el-tabs v-model="activeTab" @tab-change="onTabChange">
        <el-tab-pane label="慢查明细" name="detail">
          <el-table
            v-loading="loading"
            :data="detailRows"
            stripe
            border
            max-height="560"
          >
            <el-table-column
              v-for="col in visibleDetailCols"
              :key="col"
              :prop="col"
              :label="colLabel(col)"
              min-width="140"
              :show-overflow-tooltip="!isSqlColumn(col)"
            >
              <template v-if="isSqlColumn(col)" #default="{ row }">
                <TruncateCell
                  :value="(row as Record<string, unknown>)[col]"
                  :row="row as Record<string, unknown>"
                  :col="col"
                />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100" fixed="right">
              <template #default="{ row }">
                <el-tooltip
                  :disabled="!diagEntryTip(row as Record<string, unknown>)"
                  :content="diagEntryTip(row as Record<string, unknown>)"
                  placement="top"
                >
                  <el-button
                    link
                    :disabled="diagEntryDisabled(row as Record<string, unknown>)"
                    :type="isDiagnosed(row as Record<string, unknown>) ? 'success' : 'primary'"
                    @click="openDiagnosis(row as Record<string, unknown>)"
                  >
                    {{ isDiagnosed(row as Record<string, unknown>) ? "已诊断" : "AI诊断" }}
                  </el-button>
                </el-tooltip>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="慢查统计" name="summary">
          <el-table
            v-loading="loading"
            :data="summaryRows"
            stripe
            border
            max-height="560"
          >
            <el-table-column
              v-for="col in visibleSummaryCols"
              :key="col"
              :prop="col"
              :label="colLabel(col)"
              min-width="140"
              :show-overflow-tooltip="!isSqlColumn(col)"
            >
              <template v-if="isSqlColumn(col)" #default="{ row }">
                <TruncateCell
                  :value="(row as Record<string, unknown>)[col]"
                  :row="row as Record<string, unknown>"
                  :col="col"
                />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="140" fixed="right">
              <template #default="{ row }">
                <el-button
                  link
                  type="primary"
                  @click="openTrend(row as Record<string, unknown>)"
                >
                  趋势
                </el-button>
                <el-tooltip
                  :disabled="!diagEntryTip(row as Record<string, unknown>)"
                  :content="diagEntryTip(row as Record<string, unknown>)"
                  placement="top"
                >
                  <el-button
                    link
                    :disabled="diagEntryDisabled(row as Record<string, unknown>)"
                    :type="isDiagnosed(row as Record<string, unknown>) ? 'success' : 'primary'"
                    @click="openDiagnosis(row as Record<string, unknown>)"
                  >
                    {{ isDiagnosed(row as Record<string, unknown>) ? "已诊断" : "AI诊断" }}
                  </el-button>
                </el-tooltip>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>

      <!-- 分页 -->
      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[20, 50, 100, 200]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </div>
    </el-card>

    <!-- 趋势弹窗 -->
    <el-dialog v-model="trendVisible" title="慢查历史趋势" width="900px">
      <div class="trend-title" :title="trendTitle">{{ trendTitle }}</div>
      <EChart
        v-loading="trendLoading"
        :option="trendOption"
        height="420px"
      />
    </el-dialog>

    <!-- AI 诊断抽屉 -->
    <DiagnosisDrawer
      v-model:visible="diagnosisVisible"
      :instance-name="instanceName"
      :db-name="diagnosisDbName"
      :sql-hash="diagnosisSqlHash"
      :sql-text="diagnosisSqlText"
      @diagnosed="onDiagnosed"
    />
  </div>
</template>

<style scoped lang="scss">
.slow-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.filter-card :deep(.el-form-item) {
  margin-bottom: 0;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.trend-title {
  margin-bottom: 12px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
