<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { useAuthStore } from "@/stores/auth";
import {
  fetchQueryInstances,
  fetchQueryResources,
  describeTable,
  executeQuery,
  fetchQueryLogs,
  toggleFavorite,
  locateTable,
  generateSql,
  checkOpenai,
  type QueryInstanceRow,
  type QueryResultSet,
  type QueryResultEnvelope,
  type QueryLogRow,
  type TableLocatorRow,
} from "@/api/sqlquery";
import SqlEditor from "@/components/SqlEditor.vue";
import SqlQueryResultTable from "@/components/SqlQueryResultTable.vue";
import type { SqlCompleters } from "@/components/SqlEditor.vue";
import TruncateCell from "@/components/TruncateCell.vue";

const auth = useAuthStore();

// 选择项
const form = reactive({
  instance_name: "",
  db_name: "",
  schema_name: "",
  table_name: "",
  limit_num: 1000,
});
const sqlContent = ref("");

const instanceOptions = ref<QueryInstanceRow[]>([]);
const dbOptions = ref<Array<string | { value: string; text: string }>>([]);
const schemaOptions = ref<string[]>([]);
const tableOptions = ref<string[]>([]);
const columnOptions = ref<string[]>([]);

const currentInstance = computed(() =>
  instanceOptions.value.find((i) => i.instance_name === form.instance_name)
);
const currentDbType = computed(() => currentInstance.value?.db_type || "");
const isPgsql = computed(() => currentDbType.value === "pgsql");

// 实例按 db_type 分组
const instanceGroups = computed(() => {
  const map = new Map<string, QueryInstanceRow[]>();
  for (const i of instanceOptions.value) {
    const k = i.db_type || "other";
    if (!map.has(k)) map.set(k, []);
    map.get(k)!.push(i);
  }
  return [...map.entries()].map(([label, items]) => ({ label, items }));
});

// 补全数据（传给 SqlEditor）
const completers = computed<SqlCompleters>(() => ({
  databases: dbOptions.value.map((d) => (typeof d === "object" ? d.text : d)),
  schemas: schemaOptions.value,
  tables: tableOptions.value,
  columns: columnOptions.value,
}));

// 动态参数 ${var}
const paramNames = ref<string[]>([]);
const paramValues = ref<Record<string, string>>({});
watch(sqlContent, (s) => {
  const set = new Set<string>();
  const re = /\$\{([^}]+)\}/g;
  let m: RegExpExecArray | null;
  while (s && (m = re.exec(s))) set.add(m[1]);
  const names = [...set];
  paramNames.value = names;
  const kept: Record<string, string> = {};
  for (const n of names) kept[n] = paramValues.value[n] ?? "";
  paramValues.value = kept;
});

// 结果 tabs
interface ResultTab {
  id: string;
  title: string;
  envelope: QueryResultEnvelope | null;
  loading: boolean;
  sql: string;
}
let tabSeq = 0;
const resultTabs = ref<ResultTab[]>([]);
const activeTabId = ref("history");
const activeResultTab = computed(() =>
  resultTabs.value.find((t) => t.id === activeTabId.value)
);

function addResultTab(): ResultTab {
  tabSeq += 1;
  const id = `result-${tabSeq}`;
  const tab = reactive<ResultTab>({
    id,
    title: `结果${tabSeq}`,
    envelope: null,
    loading: false,
    sql: "",
  });
  resultTabs.value.push(tab);
  activeTabId.value = id;
  return tab;
}

function onTabRemove(name: string | number) {
  const id = String(name);
  const i = resultTabs.value.findIndex((t) => t.id === id);
  if (i >= 0) {
    resultTabs.value.splice(i, 1);
    if (activeTabId.value === id) {
      activeTabId.value = resultTabs.value.length
        ? resultTabs.value[resultTabs.value.length - 1].id
        : "history";
    }
  }
}

function ensureResultTab(): ResultTab {
  return activeResultTab.value ?? addResultTab();
}

// 编辑器实例
const editorRef = ref<InstanceType<typeof SqlEditor>>();

// 历史
const logs = ref<QueryLogRow[]>([]);
const logTotal = ref(0);
const logLoading = ref(false);
const logQuery = reactive({
  search: "",
  star: "" as "" | "true" | "false",
  page: 1,
  size: 20,
});

async function loadLogs() {
  logLoading.value = true;
  try {
    const { data } = await fetchQueryLogs({
      search: logQuery.search || undefined,
      star: logQuery.star || undefined,
      limit: logQuery.size,
      offset: (logQuery.page - 1) * logQuery.size,
    });
    logs.value = data.rows || [];
    logTotal.value = data.total || 0;
  } catch {
    // 拦截器已提示
  } finally {
    logLoading.value = false;
  }
}

function onLogSearch() {
  logQuery.page = 1;
  loadLogs();
}

// AI
const openaiEnabled = ref(false);
const aiDesc = ref("");
const aiLoading = ref(false);

async function loadOpenai() {
  try {
    const { data } = await checkOpenai();
    // nl2sql_enabled 缺省视为开启（兼容旧后端响应）；开关关闭时隐藏 AI 入口
    openaiEnabled.value =
      data.status === 0 && !!data.data?.openai && data.data?.nl2sql_enabled !== false;
  } catch {
    openaiEnabled.value = false;
  }
}

async function onAiGenerate() {
  if (!aiDesc.value.trim()) return ElMessage.warning("请输入查询描述");
  if (!form.instance_name || !form.db_name)
    return ElMessage.warning("请先选择实例和库");
  if (!form.table_name)
    return ElMessage.warning("AI 生成 SQL 需要先选择表，以获取表结构上下文");
  aiLoading.value = true;
  try {
    const sql = await generateSql({
      query_desc: aiDesc.value,
      db_type: currentDbType.value,
      instance_name: form.instance_name,
      db_name: form.db_name,
      schema_name: form.schema_name || undefined,
      tb_name: form.table_name || undefined,
    });
    if (!sql) return ElMessage.warning("AI 未返回有效 SQL");
    editorRef.value?.insert(sql);
    ElMessage.success("已生成并插入 SQL");
  } catch {
    // 拦截器已提示
  } finally {
    aiLoading.value = false;
  }
}

// 加载实例
async function loadInstances() {
  try {
    instanceOptions.value = await fetchQueryInstances();
  } catch {
    // 拦截器已提示
  }
}

// 联动
async function onInstanceChange() {
  dbOptions.value = [];
  schemaOptions.value = [];
  tableOptions.value = [];
  columnOptions.value = [];
  form.db_name = "";
  form.schema_name = "";
  form.table_name = "";
  if (!form.instance_name || !currentInstance.value) return;
  try {
    dbOptions.value = await fetchQueryResources({
      instance_id: currentInstance.value.id,
      resource_type: "database",
    });
  } catch {
    // 拦截器已提示
  }
}

/** Redis 返回 db_name 为 {value, text} 对象；提取 value 保证后端收到字符串 */
function normalizeDbName(db: unknown): string {
  return db && typeof db === "object" ? (db as any).value ?? "" : (db as string);
}

async function onDbChange() {
  form.db_name = normalizeDbName(form.db_name);
  tableOptions.value = [];
  columnOptions.value = [];
  form.table_name = "";
  if (!form.db_name) return;
  try {
    if (isPgsql.value) {
      schemaOptions.value = await fetchQueryResources({
        instance_id: currentInstance.value?.id,
        resource_type: "schema",
        db_name: form.db_name,
      });
    }
    tableOptions.value = await fetchQueryResources({
      instance_id: currentInstance.value?.id,
      resource_type: "table",
      db_name: form.db_name,
      schema_name: form.schema_name || undefined,
    });
  } catch {
    // 拦截器已提示
  }
}

async function onSchemaChange() {
  tableOptions.value = [];
  columnOptions.value = [];
  form.table_name = "";
  if (!form.schema_name) return;
  try {
    tableOptions.value = await fetchQueryResources({
      instance_id: currentInstance.value?.id,
      resource_type: "table",
      db_name: form.db_name,
      schema_name: form.schema_name,
    });
  } catch {
    // 拦截器已提示
  }
}

async function onTableChange() {
  columnOptions.value = [];
  if (!form.table_name) return;
  try {
    columnOptions.value = await fetchQueryResources({
      instance_id: currentInstance.value?.id,
      resource_type: "column",
      db_name: form.db_name,
      schema_name: form.schema_name || undefined,
      tb_name: form.table_name,
    });
  } catch {
    // 拦截器已提示
  }
  // 选中表后自动解析表结构到结果 tab
  onViewTableStructure();
}

// 美化 SQL（调 SqlEditor expose 的 beautify）
function onBeautify() {
  editorRef.value?.beautify();
}

async function onViewTableStructure() {
  if (!form.table_name) return;
  const tab = ensureResultTab();
  tab.loading = true;
  tab.envelope = null;
  tab.title = `表结构: ${form.table_name}`;
  try {
    const r = await describeTable({
      instance_name: form.instance_name,
      db_name: form.db_name,
      tb_name: form.table_name,
      schema_name: form.schema_name || undefined,
    });
    tab.envelope = { status: 0, msg: "ok", data: r };
  } catch {
    // 拦截器已提示
  } finally {
    tab.loading = false;
  }
}

// 表名定位器
const locatorKeyword = ref("");
const locatorResults = ref<TableLocatorRow[]>([]);
const locatorLoading = ref(false);
let locatorTimer: number | null = null;

function onLocatorInput() {
  if (locatorTimer) clearTimeout(locatorTimer);
  locatorTimer = window.setTimeout(async () => {
    const kw = locatorKeyword.value.trim();
    if (!kw) {
      locatorResults.value = [];
      return;
    }
    locatorLoading.value = true;
    try {
      locatorResults.value = await locateTable(kw);
    } catch {
      locatorResults.value = [];
    } finally {
      locatorLoading.value = false;
    }
  }, 500);
}

async function onLocatorPick(row: TableLocatorRow) {
  form.instance_name = row.name;
  await onInstanceChange();
  form.db_name = row.db_name;
  await onDbChange();
  form.table_name = row.table_name;
  await onTableChange();
  locatorResults.value = [];
  locatorKeyword.value = "";
}

// explain 按引擎加前缀
function withExplain(sql: string): string {
  switch (currentDbType.value) {
    case "oracle":
      return `explain plan for ${sql}`;
    case "mssql":
      return `SET SHOWPLAN_ALL ON;\n${sql};\nSET SHOWPLAN_ALL OFF;`;
    default:
      return `explain ${sql}`;
  }
}

function getSqlToRun(): string {
  const sel = editorRef.value?.getSelection()?.trim();
  return sel || sqlContent.value;
}

async function doExecute(isExplain = false) {
  if (!form.instance_name) return ElMessage.warning("请选择实例");
  if (!form.db_name) return ElMessage.warning("请选择库");
  let sql = getSqlToRun();
  if (!sql.trim()) return ElMessage.warning("请输入 SQL");
  sql = sql.replace(/\$\{([^}]+)\}/g, (_s, k) => paramValues.value[k] ?? "");
  if (isExplain) sql = withExplain(sql);

  const tab = ensureResultTab();
  tab.loading = true;
  tab.envelope = null;
  tab.sql = sql;
  try {
    const { data } = await executeQuery({
      instance_name: form.instance_name,
      db_name: form.db_name,
      sql_content: sql,
      limit_num: form.limit_num,
      schema_name: form.schema_name || undefined,
      tb_name: form.table_name || undefined,
    });
    tab.envelope = data;
  } catch {
    tab.envelope = {
      status: 1,
      msg: "查询请求失败（网络或超时，后端上限 60s）",
      data: {} as QueryResultSet,
    };
  } finally {
    tab.loading = false;
  }
}

// 历史重查
function onRequery(row: QueryLogRow) {
  sqlContent.value = row.sqllog;
  form.instance_name = row.instance_name;
  onInstanceChange().then(() => {
    form.db_name = row.db_name;
    onDbChange();
  });
  ElMessage.success("已回填，点击执行");
}

// 收藏
async function onToggleFav(row: QueryLogRow) {
  if (row.favorite) {
    try {
      await toggleFavorite({ query_log_id: row.id, star: "false" });
      ElMessage.success("已取消收藏");
      loadLogs();
    } catch {
      // 业务错误已提示
    }
    return;
  }
  try {
    const { value } = await ElMessageBox.prompt("收藏别名", "收藏查询", {
      inputPlaceholder: "别名（可空）",
      confirmButtonText: "收藏",
      cancelButtonText: "取消",
    });
    await toggleFavorite({
      query_log_id: row.id,
      star: "true",
      alias: value || undefined,
    });
    ElMessage.success("已收藏");
    loadLogs();
  } catch (e) {
    if (e !== "cancel") {
      // 业务错误已提示
    }
  }
}

const canExecute = computed(() => auth.hasPerm("sql.query_submit"));

onMounted(() => {
  loadInstances();
  loadLogs();
  loadOpenai();
});
</script>

<template>
  <div class="sqlquery-page">
    <el-row :gutter="12" class="work-row">
      <!-- 左：编辑器 -->
      <el-col :span="18" class="editor-col">
        <SqlEditor
          ref="editorRef"
          v-model="sqlContent"
          :completers="completers"
          :db-type="currentDbType"
          :show-beautify="false"
          fill-height
        />
        <div v-if="paramNames.length" class="dynamic-params">
          <span class="label">动态参数：</span>
          <el-input
            v-for="p in paramNames"
            :key="p"
            v-model="paramValues[p]"
            :placeholder="p"
            class="param-input"
          />
        </div>
      </el-col>

      <!-- 右：控制栏 -->
      <el-col :span="6">
        <el-card shadow="never" body-class="control-body" class="control-card">
          <el-form label-width="72px">
            <el-form-item label="表名定位">
              <el-input
                v-model="locatorKeyword"
                placeholder="输入表名反查"
                clearable
                :prefix-icon="undefined"
                @input="onLocatorInput"
              />
            </el-form-item>
            <div
              v-if="locatorResults.length"
              v-loading="locatorLoading"
              class="locator-list"
            >
              <div
                v-for="r in locatorResults"
                :key="r.id"
                class="locator-item"
                @click="onLocatorPick(r)"
              >
                {{ r.name }} / {{ r.db_name }} / {{ r.table_name }}
              </div>
            </div>

            <el-form-item label="实例">
              <el-select
                v-model="form.instance_name"
                filterable
                placeholder="选择实例"
                style="width: 100%"
                @change="onInstanceChange"
              >
                <el-option-group
                  v-for="grp in instanceGroups"
                  :key="grp.label"
                  :label="grp.label"
                >
                  <el-option
                    v-for="i in grp.items"
                    :key="i.id"
                    :label="i.instance_name"
                    :value="i.instance_name"
                  />
                </el-option-group>
              </el-select>
            </el-form-item>

            <el-form-item label="数据库">
              <el-select
                v-model="form.db_name"
                filterable
                value-key="value"
                placeholder="选择库"
                style="width: 100%"
                @change="onDbChange"
              >
                <el-option
                  v-for="d in dbOptions"
                  :key="typeof d === 'object' ? d.value : d"
                  :label="typeof d === 'object' ? d.text : d"
                  :value="typeof d === 'object' ? d.value : d"
                />
              </el-select>
            </el-form-item>

            <el-form-item v-if="isPgsql" label="Schema">
              <el-select
                v-model="form.schema_name"
                filterable
                placeholder="选择 schema"
                style="width: 100%"
                @change="onSchemaChange"
              >
                <el-option
                  v-for="s in schemaOptions"
                  :key="s"
                  :label="s"
                  :value="s"
                />
              </el-select>
            </el-form-item>

            <el-form-item label="表">
              <el-select
                v-model="form.table_name"
                filterable
                placeholder="选择表（自动解析结构）"
                style="width: 100%"
                @change="onTableChange"
              >
                <el-option
                  v-for="t in tableOptions"
                  :key="t"
                  :label="t"
                  :value="t"
                />
              </el-select>
            </el-form-item>

            <el-form-item label="行数限制">
              <el-select v-model="form.limit_num" style="width: 100%">
                <el-option :value="100" label="100" />
                <el-option :value="500" label="500" />
                <el-option :value="1000" label="1000" />
                <el-option :value="0" label="不限（最大）" />
              </el-select>
            </el-form-item>

            <el-form-item label="操作">
              <div class="action-btns">
                <el-button
                  type="success"
                  @click="onBeautify"
                >美化</el-button>
                <el-button v-if="canExecute" type="primary" @click="doExecute(false)">
                  执行
                </el-button>
                <el-button v-if="canExecute" @click="doExecute(true)">
                  执行计划
                </el-button>
              </div>
            </el-form-item>
          </el-form>

          <div v-if="openaiEnabled && canExecute" class="ai-block">
            <el-divider content-position="left">AI 生成 SQL</el-divider>
            <el-input
              v-model="aiDesc"
              type="textarea"
              :rows="2"
              placeholder="用自然语言描述查询需求"
            />
            <el-button
              type="primary"
              plain
              class="ai-btn"
              :loading="aiLoading"
              @click="onAiGenerate"
            >
              生成 SQL
            </el-button>
          </div>

          <el-alert
            v-if="!canExecute"
            type="info"
            :closable="false"
            show-icon
            title="无 sql.query_submit 权限，仅可查看历史/收藏"
          />
        </el-card>
      </el-col>
    </el-row>

    <!-- 结果 + 历史 tabs -->
    <el-card shadow="never" class="result-card">
      <el-tabs v-model="activeTabId" type="card" @tab-remove="onTabRemove">
        <el-tab-pane label="查询历史" name="history">
          <div class="log-filter">
            <el-input
              v-model="logQuery.search"
              placeholder="搜索 SQL / 用户 / 别名"
              clearable
              class="log-search"
              @keyup.enter="onLogSearch"
            />
            <el-select
              v-model="logQuery.star"
              class="log-star"
              @change="onLogSearch"
            >
              <el-option label="全部" value="" />
              <el-option label="已收藏" value="true" />
              <el-option label="未收藏" value="false" />
            </el-select>
            <el-button type="primary" @click="onLogSearch">查询</el-button>
          </div>
          <el-table
            v-loading="logLoading"
            :data="logs"
            stripe
            border
            max-height="440"
            class="log-table"
          >
            <el-table-column
              label="SQL"
              prop="sqllog"
              min-width="300"
            >
              <template #default="{ row }">
                <TruncateCell :value="(row as QueryLogRow).sqllog" :row="row as unknown as Record<string,unknown>" col="SQLLog" />
              </template>
            </el-table-column>
            <el-table-column
              label="实例"
              prop="instance_name"
              width="130"
              show-overflow-tooltip
            />
            <el-table-column
              label="库"
              prop="db_name"
              width="110"
              show-overflow-tooltip
            />
            <el-table-column label="行数" prop="effect_row" width="70" />
            <el-table-column label="耗时" width="80">
              <template #default="{ row }">{{ row.cost_time }}s</template>
            </el-table-column>
            <el-table-column
              label="用户"
              prop="user_display"
              width="100"
              show-overflow-tooltip
            />
            <el-table-column label="时间" prop="create_time" width="150" />
            <el-table-column label="操作" width="140" fixed="right">
              <template #default="{ row }">
                <el-button
                  link
                  type="primary"
                  @click="onRequery(row as QueryLogRow)"
                >
                  重查
                </el-button>
                <el-button
                  link
                  :type="row.favorite ? 'warning' : 'primary'"
                  @click="onToggleFav(row as QueryLogRow)"
                >
                  {{ row.favorite ? "取消收藏" : "收藏" }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="pager">
            <el-pagination
              :total="logTotal"
              :current-page="logQuery.page"
              :page-size="logQuery.size"
              :page-sizes="[20, 50, 100]"
              layout="total, sizes, prev, pager, next"
              background
              @current-change="
                (p: number) => {
                  logQuery.page = p;
                  loadLogs();
                }
              "
              @size-change="
                (s: number) => {
                  logQuery.size = s;
                  logQuery.page = 1;
                  loadLogs();
                }
              "
            />
          </div>
        </el-tab-pane>

        <el-tab-pane
          v-for="t in resultTabs"
          :key="t.id"
          :label="t.title"
          :name="t.id"
          closable
        >
          <SqlQueryResultTable :envelope="t.envelope" :loading="t.loading" />
          <div v-if="t.sql" class="executed-sql">
            <span class="lbl">执行 SQL：</span>
            <code>{{ t.sql }}</code>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.sqlquery-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* 上半区左右等高，编辑器随右栏伸缩 */
.work-row {
  align-items: stretch;
}
.work-row :deep(.el-col) {
  display: flex;
  flex-direction: column;
}
.editor-col :deep(.sql-editor-wrap) {
  flex: 1;
  min-height: 0;
}
.control-card {
  height: 100%;
}
.control-card :deep(.el-card__body) {
  height: 100%;
  overflow-y: auto;
}
/* 控制栏表单紧凑、输入框对齐 */
.control-body :deep(.el-form-item) {
  margin-bottom: 10px;
}

.dynamic-params {
  margin-top: 8px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;

  .label {
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }

  .param-input {
    width: 160px;
  }
}

:deep(.control-body) {
  padding: 12px;
}

.locator-list {
  max-height: 160px;
  overflow-y: auto;
  margin: -4px 0 8px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;

  .locator-item {
    padding: 4px 8px;
    font-size: 12px;
    cursor: pointer;

    &:hover {
      background: var(--el-fill-color-light);
    }
  }
}

.action-btns {
  display: flex;
  flex-wrap: nowrap;
  gap: 6px;
  width: 100%;
}
/* 执行 / 执行计划：等宽拉伸 */
.action-btns :deep(.el-button) {
  flex: 0 0 auto;
  margin: 0;
}

.ai-block {
  margin-top: 8px;

  .ai-btn {
    margin-top: 6px;
    width: 100%;
  }
}

.result-card {
  margin-top: 4px;
}

.log-filter {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;

  .log-search {
    width: 260px;
  }

  .log-star {
    width: 120px;
  }
}

.pager {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}

.executed-sql {
  margin-top: 10px;
  padding: 8px 10px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  font-size: 12px;
  word-break: break-all;

  .lbl {
    color: var(--el-text-color-secondary);
  }

  code {
    font-family: monospace;
  }
}
</style>
