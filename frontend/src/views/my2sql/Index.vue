<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { format as sqlFormat } from "sql-formatter";
import TruncateCell from "@/components/TruncateCell.vue";
import { fetchQueryInstances, fetchQueryResources } from "@/api/sqlquery";
import {
  downloadMy2sqlResult,
  fetchBinlogList,
  fetchMy2sqlTask,
  runMy2sql,
  submitMy2sqlAsync,
  type BinlogFile,
  type My2SqlParams,
  type My2SqlRow,
} from "@/api/binlog";
import { useBinlogHandoffStore } from "@/stores/binlogHandoff";

const router = useRouter();
const handoff = useBinlogHandoffStore();

// 实例（仅 MySQL）
const instanceOptions = ref<{ instance_name: string }[]>([]);
const binlogFiles = ref<BinlogFile[]>([]);
const dbOptions = ref<string[]>([]);
const tableOptions = ref<string[]>([]);
const loading = ref(false);
const result = ref<My2SqlRow[]>([]);

// 异步解析（save_sql）状态
const asyncTaskId = ref("");
const asyncState = ref<"" | "queued" | "running" | "success" | "failure">("");
const asyncRegistered = ref(false);
const asyncSqlCount = ref(0);
const asyncHasFile = ref(false);
const asyncError = ref("");
/** 连续多少轮还没被 worker 接手（超过 8 轮≈1 分钟提示检查 qcluster） */
const asyncUnregisteredPolls = ref(0);
let pollTimer: number | undefined;

const form = reactive({
  instance_name: "",
  save_sql: false,
  rollback: false,
  extra_info: false,
  ignore_primary_key: false,
  full_columns: false,
  no_db_prefix: false,
  file_per_table: false,
  threads: 4,
  num: 30,
  start_file: "",
  start_pos: "" as number | string,
  end_file: "",
  end_pos: "" as number | string,
  start_time: "",
  stop_time: "",
  only_schemas: "",
  only_tables: [] as string[],
  sql_type: [] as string[],
});

async function loadInstances() {
  try {
    instanceOptions.value = await fetchQueryInstances({ db_type: ["mysql"] });
  } catch {
    // 拦截器已提示
  }
}

// 实例变更 → 拉库 + binlog
watch(
  () => form.instance_name,
  async () => {
    dbOptions.value = [];
    tableOptions.value = [];
    binlogFiles.value = [];
    form.only_schemas = "";
    form.only_tables = [];
    form.start_file = "";
    form.end_file = "";
    result.value = [];
    resetAsyncTask();
    if (!form.instance_name) return;
    try {
      dbOptions.value = await fetchQueryResources({
        instance_name: form.instance_name,
        resource_type: "database",
      });
    } catch {
      // 拦截器已提示
    }
    try {
      binlogFiles.value = await fetchBinlogList(form.instance_name);
    } catch {
      // 拦截器已提示
    }
  }
);

// 库变更 → 拉表
watch(
  () => form.only_schemas,
  async () => {
    form.only_tables = [];
    tableOptions.value = [];
    result.value = [];
    if (!form.only_schemas) return;
    try {
      tableOptions.value = await fetchQueryResources({
        instance_name: form.instance_name,
        resource_type: "table",
        db_name: form.only_schemas,
      });
    } catch {
      // 拦截器已提示
    }
  }
);

// 起始文件变更 → 默认终止文件与其一致
watch(
  () => form.start_file,
  () => {
    if (form.start_file && !form.end_file) form.end_file = form.start_file;
    if (form.start_file && form.end_file === form.start_file) {
      form.end_pos = binlogSizeByName(form.start_file);
    }
  }
);

// 终止文件变更 → 默认终止位置为该文件大小
watch(
  () => form.end_file,
  () => {
    form.end_pos = binlogSizeByName(form.end_file);
  }
);

function binlogLabel(f: BinlogFile): string {
  return `${f.Log_name}   Size:${f.File_size}`;
}

function binlogSize(f: BinlogFile): string {
  return String(f.File_size);
}

function binlogSizeByName(name: string): string {
  const f = binlogFiles.value.find((i) => i.Log_name === name);
  return f ? binlogSize(f) : "";
}

function formatSql(sql: string): string {
  try {
    return sqlFormat(sql, { language: "mysql" });
  } catch {
    return sql;
  }
}

/** 选中区间涉及的文件大小合计（首尾 pos 未裁剪，偏保守） */
const rangeBytes = computed(() => {
  const files = binlogFiles.value;
  if (!files.length || !form.start_file) return 0;
  const startIdx = files.findIndex((f) => f.Log_name === form.start_file);
  const endName = form.end_file || form.start_file;
  const endIdx = files.findIndex((f) => f.Log_name === endName);
  if (startIdx < 0 || endIdx < 0 || endIdx < startIdx) return 0;
  let total = 0;
  for (let i = startIdx; i <= endIdx; i += 1) {
    total += Number(files[i].File_size) || 0;
  }
  return total;
});

/** 粗估同步解析耗时：实测 1~10MB/s，这里取 2MB/s 保守估算 */
const estimatedSeconds = computed(() => Math.round(rangeBytes.value / (2 * 1024 * 1024)));

const rangeHint = computed(() => {
  if (!rangeBytes.value) return "";
  const mb = (rangeBytes.value / 1024 / 1024).toFixed(0);
  const mins = Math.max(1, Math.round(estimatedSeconds.value / 60));
  if (estimatedSeconds.value <= 60) return `解析范围约 ${mb}MB，预计 ${estimatedSeconds.value} 秒左右`;
  return `解析范围约 ${mb}MB，预计需要 ${mins} 分钟以上，同步请求会超时，请勾选「保存到文件（异步）」`;
});

function resetAsyncTask() {
  stopPolling();
  asyncTaskId.value = "";
  asyncState.value = "";
  asyncRegistered.value = false;
  asyncUnregisteredPolls.value = 0;
  asyncSqlCount.value = 0;
  asyncHasFile.value = false;
  asyncError.value = "";
}

function stopPolling() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

async function pollOnce() {
  if (!asyncTaskId.value) return;
  try {
    const state = await fetchMy2sqlTask(asyncTaskId.value);
    asyncState.value = state.state;
    asyncRegistered.value = state.registered ?? true;
    asyncUnregisteredPolls.value = asyncRegistered.value
      ? 0
      : asyncUnregisteredPolls.value + 1;
    asyncSqlCount.value = state.sql_count || 0;
    asyncHasFile.value = !!state.has_file;
    asyncError.value = state.error || "";
    if (state.state === "success") {
      stopPolling();
      ElMessage.success(`解析完成，共 ${asyncSqlCount.value} 条 SQL，可下载结果文件`);
    } else if (state.state === "failure") {
      stopPolling();
    }
  } catch {
    // 轮询失败（含任务刚提交还没落库）不打扰用户，下一轮继续
  }
}

function buildParams(): My2SqlParams {
  return {
    instance_name: form.instance_name,
    save_sql: form.save_sql,
    rollback: form.rollback,
    extra_info: form.extra_info,
    ignore_primary_key: form.ignore_primary_key,
    full_columns: form.full_columns,
    no_db_prefix: form.no_db_prefix,
    file_per_table: form.file_per_table,
    threads: form.threads,
    num: form.num,
    start_file: form.start_file,
    start_pos: form.start_pos,
    end_file: form.end_file,
    end_pos: form.end_pos,
    stop_time: form.stop_time,
    start_time: form.start_time,
    only_schemas: form.only_schemas,
    only_tables: form.only_tables,
    sql_type: form.sql_type,
  };
}

async function onRun() {
  if (!form.instance_name) return ElMessage.warning("请选择实例");
  if (!form.start_file && !form.start_time) {
    return ElMessage.warning("请选择起始解析文件，或填写起始解析时间");
  }
  // 只按时间解析时，终止范围交给终止时间控制
  if (form.start_file && !form.end_file) form.end_file = form.start_file;
  if (form.end_file && !form.end_pos) form.end_pos = binlogSizeByName(form.end_file);
  if (form.end_file && !form.end_pos) {
    return ElMessage.warning("请填写终止解析位置（默认取所选 binlog 文件大小）");
  }
  result.value = [];
  resetAsyncTask();
  loading.value = true;
  try {
    if (form.save_sql) {
      const taskId = await submitMy2sqlAsync(buildParams());
      asyncTaskId.value = taskId;
      asyncState.value = "queued";
      ElMessage.success("已提交后台解析，完成后会通知你，可离开本页面");
      pollTimer = window.setInterval(pollOnce, 8000);
      void pollOnce();
    } else {
      result.value = await runMy2sql(buildParams());
      if (result.value.length === 0) {
        ElMessage.info("无解析结果：该区间没有匹配的 DML 语句，或过滤条件过严");
      }
    }
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false;
  }
}

function onRefreshTask() {
  void pollOnce();
}

function onDownload() {
  if (!asyncTaskId.value) return;
  downloadMy2sqlResult(asyncTaskId.value);
}

function onSubmitWorkflow() {
  if (result.value.length === 0) return;
  if (result.value.length > 2000) {
    return ElMessage.warning("SQL 语句超过 2000 行，不支持提交工单，请使用异步导出文件方式");
  }
  const sqlContent = result.value.map((r) => r.sql).filter(Boolean).join("\n");
  const wfName = `My2SQL回滚-${form.instance_name}-${form.only_schemas}`.slice(0, 49);
  handoff.set({
    workflow_name: wfName,
    sql_content: sqlContent,
    instance_name: form.instance_name,
    db_name: form.only_schemas,
  });
  router.push({ name: "sqlworkflow-submit" });
}

function onCopyResult() {
  if (result.value.length === 0) return;
  const content = result.value.map((r) => r.sql).filter(Boolean).join("\n");
  void navigator.clipboard.writeText(content).then(
    () => ElMessage.success(`已复制 ${result.value.length} 条 SQL`),
    () => ElMessage.error("复制失败，请手动选择复制")
  );
}

function confirmRollbackModeTip(): void {
  void ElMessageBox.confirm(
    "回滚模式生成的是反向 SQL（原 insert → delete，原 delete → insert），执行前务必人工核对；" +
      "生成回滚 SQL 要求 binlog_format=ROW 且 binlog_row_image=FULL，且解析区间内表结构未变更。",
    "回滚模式提示",
    { confirmButtonText: "知道了", showCancelButton: false, type: "warning" }
  ).catch(() => undefined);
}

onUnmounted(stopPolling);
loadInstances();
</script>

<template>
  <div class="my2sql-page">
    <el-row :gutter="16">
      <!-- 左：选项 -->
      <el-col :span="7">
        <el-card shadow="never" v-loading="loading">
          <template #header>操作选项</template>
          <el-form :model="form" label-width="0" label-position="top">
            <el-form-item label="选择实例" required>
              <el-select v-model="form.instance_name" filterable placeholder="请选择实例" style="width: 100%">
                <el-option
                  v-for="i in instanceOptions"
                  :key="i.instance_name"
                  :label="i.instance_name"
                  :value="i.instance_name"
                />
              </el-select>
            </el-form-item>

            <el-form-item>
              <el-tooltip
                content="大 binlog（几十上百 MB）解析需要几分钟，同步请求会超时；勾选后提交后台任务，解析完成会通知你，并可在本页下载完整 SQL 文件"
                placement="right"
              >
                <el-checkbox v-model="form.save_sql">保存到文件（异步，推荐大范围使用）</el-checkbox>
              </el-tooltip>
            </el-form-item>

            <div class="section-title">解析模式</div>
            <el-checkbox v-model="form.rollback" @change="form.rollback && confirmRollbackModeTip()">
              -work-type rollback（生成回滚 SQL）
            </el-checkbox>
            <el-tooltip content="生成的 insert 语句去掉主键，便于重新导入" placement="right">
              <el-checkbox v-model="form.ignore_primary_key">-ignore-primaryKey-forInsert</el-checkbox>
            </el-tooltip>
            <el-tooltip
              content="update 语句带全部列；delete/update 的 where 条件用全部列而非仅主键（结果更精确、也更慢）"
              placement="right"
            >
              <el-checkbox v-model="form.full_columns">-full-columns</el-checkbox>
            </el-tooltip>
            <el-tooltip content="SQL 中的表名不带库名前缀" placement="right">
              <el-checkbox v-model="form.no_db_prefix">-do-not-add-prifixDb（表名不带库名）</el-checkbox>
            </el-tooltip>
            <el-tooltip content="按表拆分文件，便于只取某张表的 SQL；配合异步下载会打包成 zip" placement="right">
              <el-checkbox v-model="form.file_per_table">-file-per-table（按表拆分文件）</el-checkbox>
            </el-tooltip>
            <el-tooltip content="在每条 SQL 前加 # 注释：库表、datetime、binlog 位点" placement="right">
              <el-checkbox v-model="form.extra_info">-add-extraInfo（附带库表/位点注释）</el-checkbox>
            </el-tooltip>

            <div class="section-title">解析线程数</div>
            <el-input-number v-model="form.threads" :min="1" :max="64" controls-position="right" style="width: 100%" />

            <div class="section-title">解析范围控制</div>
            <el-tooltip
              content="仅控制页面展示多少条，不减少解析耗时：解析始终覆盖你选择的整个范围"
              placement="right"
            >
              <el-input-number
                v-model="form.num"
                :min="1"
                controls-position="right"
                placeholder="展示行数，默认 30"
                style="width: 100%"
              />
            </el-tooltip>
            <el-select
              v-model="form.start_file"
              filterable
              clearable
              placeholder="起始解析文件（按时间解析时可留空）"
              style="width: 100%"
            >
              <el-option v-for="f in binlogFiles" :key="f.Log_name" :label="binlogLabel(f)" :value="f.Log_name" />
            </el-select>
            <el-input v-model="form.start_pos" placeholder="起始解析位置（默认 4，即文件开头）" />
            <el-select v-model="form.end_file" filterable clearable placeholder="终止解析文件（默认同起始文件）" style="width: 100%">
              <el-option v-for="f in binlogFiles" :key="f.Log_name" :label="binlogLabel(f)" :value="f.Log_name" />
            </el-select>
            <el-input v-model="form.end_pos" placeholder="终止解析位置（默认取终止文件大小）" />
            <el-date-picker
              v-model="form.start_time"
              type="datetime"
              value-format="YYYY-MM-DD HH:mm:ss"
              placeholder="起始解析时间（按时间解析，可不选文件）"
              style="width: 100%"
            />
            <el-date-picker
              v-model="form.stop_time"
              type="datetime"
              value-format="YYYY-MM-DD HH:mm:ss"
              placeholder="终止解析时间（可选）"
              style="width: 100%"
            />

            <div class="section-title">对象过滤</div>
            <el-select v-model="form.only_schemas" filterable clearable placeholder="数据库过滤（可选）" style="width: 100%">
              <el-option v-for="d in dbOptions" :key="d" :label="d" :value="d" />
            </el-select>
            <el-select v-model="form.only_tables" multiple filterable placeholder="表过滤（可选，可多选）" style="width: 100%">
              <el-option v-for="t in tableOptions" :key="t" :label="t" :value="t" />
            </el-select>
            <el-select v-model="form.sql_type" multiple placeholder="类型过滤（可选，默认全部）" style="width: 100%">
              <el-option label="INSERT" value="insert" />
              <el-option label="UPDATE" value="update" />
              <el-option label="DELETE" value="delete" />
            </el-select>

            <el-alert
              v-if="rangeHint"
              :title="rangeHint"
              :type="estimatedSeconds > 60 ? 'warning' : 'info'"
              :closable="false"
              show-icon
              class="range-alert"
            />

            <div class="btn-row">
              <el-button type="danger" :loading="loading" @click="onRun">
                {{ form.save_sql ? "提交后台解析" : "获取 SQL" }}
              </el-button>
              <el-button type="success" :disabled="result.length === 0" @click="onSubmitWorkflow">提交工单</el-button>
              <el-button :disabled="result.length === 0" @click="onCopyResult">复制</el-button>
            </div>
          </el-form>

          <el-collapse class="usage">
            <el-collapse-item title="使用说明 / 限制" name="help">
              <ul>
                <li>解析范围决定耗时：范围越大越慢（约 1~10MB/s），先用时间或库表过滤缩小范围。</li>
                <li>回滚需要 binlog_format=ROW 且 binlog_row_image=FULL；只能回滚 DML，不能回滚 DDL。</li>
                <li>解析区间内表结构必须一致：区间内有 DDL（加/删列）会中断解析，请缩小范围后重试。</li>
                <li>实例账号需要 SELECT、REPLICATION SLAVE、REPLICATION CLIENT 权限；MySQL 8 需 mysql_native_password 认证。</li>
                <li>起始/终止时间按工具所在服务器时区解释（-tl 默认 Local）。</li>
                <li>大范围请勾选「保存到文件（异步）」：后台解析，完成后通知并下载完整文件。</li>
              </ul>
            </el-collapse-item>
          </el-collapse>
        </el-card>
      </el-col>

      <!-- 右：结果 -->
      <el-col :span="17">
        <el-card shadow="never">
          <template #header>SQL 语句（{{ result.length }}）</template>

          <el-alert
            v-if="asyncTaskId"
            :type="asyncState === 'failure' ? 'error' : asyncState === 'success' ? 'success' : 'info'"
            :closable="false"
            show-icon
            class="task-alert"
          >
            <template #title>
              后台解析任务
              <template v-if="asyncState === 'queued'">已排队</template>
              <template v-else-if="asyncState === 'running'">解析中…</template>
              <template v-else-if="asyncState === 'success'">已完成，共 {{ asyncSqlCount }} 条 SQL</template>
              <template v-else-if="asyncState === 'failure'">失败</template>
            </template>
            <div class="task-body">
              <span v-if="asyncState === 'failure'">{{ asyncError || "解析失败，请查看通知或联系管理员" }}</span>
              <span v-else-if="asyncState === 'success'">
                结果文件已生成，可下载完整 SQL（解析范围的全部语句，不受展示行数限制）。
              </span>
              <span v-else-if="!asyncRegistered">
                已提交，等待后台进程接手…
                <template v-if="asyncUnregisteredPolls >= 8">
                  已超过 1 分钟仍未开始，请确认 qcluster 进程正在运行（任务会一直排队直到有 worker）。
                </template>
              </span>
              <span v-else>可在本页面等待，完成后会站内通知；也可离开页面稍后回来查看。</span>
              <el-button size="small" @click="onRefreshTask">刷新状态</el-button>
              <el-button v-if="asyncHasFile" size="small" type="primary" @click="onDownload">下载结果文件</el-button>
            </div>
          </el-alert>

          <div class="tip">
            展示数量由「解析范围控制 → 展示行数」决定；勾选「保存到文件（异步）」时可在任务完成后下载完整 SQL
            文件，开启消息通知后执行结束会通知操作人。
          </div>
          <el-table
            :data="result"
            stripe
            border
            max-height="640"
            :default-sort="{ prop: '', order: 'ascending' }"
          >
            <el-table-column type="expand">
              <template #default="{ row }">
                <pre class="sql-full">{{ formatSql((row as My2SqlRow).sql) }}</pre>
                <pre v-if="(row as My2SqlRow).extra_info" class="extra-full">{{ (row as My2SqlRow).extra_info }}</pre>
              </template>
            </el-table-column>
            <el-table-column label="SQL" min-width="400">
              <template #default="{ row }">
                <TruncateCell :value="(row as My2SqlRow).sql" :row="(row as My2SqlRow) as unknown as Record<string,unknown>" col="sql" />
              </template>
            </el-table-column>
            <el-table-column label="ExtraInfo" min-width="200">
              <template #default="{ row }">
                <TruncateCell :value="(row as My2SqlRow).extra_info || ''" :row="(row as My2SqlRow) as unknown as Record<string,unknown>" col="extra_info" />
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped lang="scss">
.my2sql-page {
  .section-title {
    margin: 12px 0 6px;
    font-weight: 600;
    font-size: 13px;
    color: var(--el-text-color-primary);
  }

  .el-select,
  .el-input,
  .el-input-number,
  .el-date-editor {
    margin-bottom: 8px;
  }

  .btn-row {
    margin-top: 16px;
    display: flex;
    gap: 8px;
  }

  .range-alert,
  .task-alert {
    margin-bottom: 12px;
  }

  .task-body {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    line-height: 1.6;
  }

  .tip {
    margin-bottom: 12px;
    color: var(--el-color-danger);
    font-size: 12px;
    line-height: 1.6;
  }

  .usage {
    margin-top: 12px;
    :deep(.el-collapse-item__content) {
      font-size: 12px;
      line-height: 1.7;
      color: var(--el-text-color-secondary);
    }
    ul {
      margin: 0;
      padding-left: 18px;
    }
  }

  .sql-full,
  .extra-full {
    margin: 0;
    padding: 8px 12px;
    background: var(--el-fill-color-light);
    border-radius: 4px;
    font-family: var(--el-font-family-mono, monospace);
    font-size: 13px;
    white-space: pre-wrap;
    word-break: break-all;
  }

  .extra-full {
    margin-top: 8px;
    color: var(--el-text-color-secondary);
  }
}
</style>
