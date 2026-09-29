<script setup lang="ts">
import { ref, watch, onMounted } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { useInstanceSelect } from "@/composables/useInstanceSelect";
import { fetchQueryResources } from "@/api/sqlquery";
import {
  schemaSync,
  tableSchemaDiff,
  type SchemaSyncResult,
  type TableDiffResult,
} from "@/api/phase2";
import { useBinlogHandoffStore } from "@/stores/binlogHandoff";

const router = useRouter();
const handoff = useBinlogHandoffStore();

const { instanceName, instanceGroups, loadInstances } =
  useInstanceSelect();

const activeTab = ref<"db" | "table">("db");

function copyText(text: string) {
  if (!text) return;
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => ElMessage.success("已复制"));
  } else {
    ElMessage.warning("浏览器不支持复制");
  }
}

function findInstance(name: string) {
  return instanceGroups.value
    .flatMap((g) => g.items)
    .find((i) => i.instance_name === name);
}

async function fetchDbOptions(name: string): Promise<string[]> {
  const inst = findInstance(name);
  if (!inst) return [];
  try {
    return await fetchQueryResources({
      instance_id: inst.id,
      resource_type: "database",
    });
  } catch {
    return []; // 拦截器已提示
  }
}

// ---------- 整库对比（schemasync） ----------

const dbName = ref("");
const dbOptions = ref<string[]>([]);
const targetInstanceName = ref("");
const targetDbName = ref("");
const targetDbOptions = ref<string[]>([]);
const syncAutoInc = ref(false);
const syncComments = ref(false);
const loading = ref(false);
const result = ref<SchemaSyncResult | null>(null);

watch(instanceName, () => {
  dbName.value = "";
  dbOptions.value = [];
  if (instanceName.value) {
    fetchDbOptions(instanceName.value).then((o) => (dbOptions.value = o));
  }
});

watch(targetInstanceName, () => {
  targetDbName.value = "";
  targetDbOptions.value = [];
  if (targetInstanceName.value) {
    fetchDbOptions(targetInstanceName.value).then(
      (o) => (targetDbOptions.value = o)
    );
  }
});

async function onCompare() {
  if (!instanceName.value || !dbName.value)
    return ElMessage.warning("请选择源实例和库");
  if (!targetInstanceName.value || !targetDbName.value)
    return ElMessage.warning("请选择目标实例和库");
  loading.value = true;
  result.value = null;
  try {
    result.value = await schemaSync({
      instance_name: instanceName.value,
      db_name: dbName.value,
      target_instance_name: targetInstanceName.value,
      target_db_name: targetDbName.value,
      sync_auto_inc: syncAutoInc.value,
      sync_comments: syncComments.value,
    });
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false;
  }
}

// ---------- 表级对比（information_schema diff） ----------

const tdSrcInstance = ref("");
const tdSrcDb = ref("");
const tdDstInstance = ref("");
const tdDstDb = ref("");
const tdTables = ref<string[]>([]);
const tdSyncAutoInc = ref(false);
const tdSyncComments = ref(false);
const tdSrcDbOptions = ref<string[]>([]);
const tdDstDbOptions = ref<string[]>([]);
const tdTableOptions = ref<string[]>([]);
const tdLoading = ref(false);
const tdResult = ref<TableDiffResult | null>(null);
const expandedTables = ref<string[]>([]);

watch(tdSrcInstance, () => {
  tdSrcDb.value = "";
  tdTables.value = [];
  tdSrcDbOptions.value = [];
  tdTableOptions.value = [];
  if (tdSrcInstance.value) {
    fetchDbOptions(tdSrcInstance.value).then(
      (o) => (tdSrcDbOptions.value = o)
    );
  }
});

watch(tdDstInstance, () => {
  tdDstDb.value = "";
  tdDstDbOptions.value = [];
  if (tdDstInstance.value) {
    fetchDbOptions(tdDstInstance.value).then(
      (o) => (tdDstDbOptions.value = o)
    );
  }
});

watch(tdSrcDb, () => {
  tdTables.value = [];
  tdTableOptions.value = [];
  if (!tdSrcDb.value) return;
  const inst = findInstance(tdSrcInstance.value);
  if (!inst) return;
  fetchQueryResources({
    instance_id: inst.id,
    resource_type: "table",
    db_name: tdSrcDb.value,
  })
    .then((o) => (tdTableOptions.value = o))
    .catch(() => {
      // 拦截器已提示
    });
});

async function onTableDiff() {
  if (!tdSrcInstance.value || !tdSrcDb.value)
    return ElMessage.warning("请选择源实例和库");
  if (!tdDstInstance.value || !tdDstDb.value)
    return ElMessage.warning("请选择目标实例和库");
  tdLoading.value = true;
  tdResult.value = null;
  expandedTables.value = [];
  try {
    tdResult.value = await tableSchemaDiff({
      instance_name: tdSrcInstance.value,
      db_name: tdSrcDb.value,
      target_instance_name: tdDstInstance.value,
      target_db_name: tdDstDb.value,
      tables: tdTables.value,
      sync_auto_inc: tdSyncAutoInc.value,
      sync_comments: tdSyncComments.value,
    });
    // 默认展开有差异的表
    expandedTables.value = tdResult.value.tables
      .filter((t) => t.status !== "same")
      .map((t) => t.table);
  } catch {
    // 拦截器已提示
  } finally {
    tdLoading.value = false;
  }
}

function submitPatchAsWorkflow() {
  if (!tdResult.value?.patch_sql) return;
  handoff.set({
    workflow_name: `表级结构同步-${tdSrcDb.value}`,
    sql_content: tdResult.value.patch_sql,
    instance_name: tdDstInstance.value,
    db_name: tdDstDb.value,
  });
  router.push({ name: "sqlworkflow-submit" });
}

const KIND_LABELS: Record<string, string> = {
  column: "列",
  index: "索引",
  table_option: "表选项",
  table: "表",
};
const ACTION_LABELS: Record<string, string> = {
  add: "新增",
  drop: "删除",
  modify: "修改",
  rename: "改名",
  missing: "缺失",
};
const STATUS_LABELS: Record<string, string> = {
  diff: "差异",
  same: "一致",
  only_in_source: "仅源存在",
  only_in_target: "仅目标存在",
};

function actionTagType(action: string) {
  if (action === "drop") return "danger";
  if (action === "add") return "success";
  if (action === "rename") return "warning";
  return "info";
}

function statusTagType(status: string) {
  if (status === "diff") return "warning";
  if (status === "same") return "success";
  return "info";
}

onMounted(loadInstances);
</script>

<template>
  <div class="schemasync-page">
    <el-card shadow="never">
      <template #header>SchemaSync 结构对比</template>
      <el-tabs v-model="activeTab">
        <!-- 整库对比（schemasync 工具） -->
        <el-tab-pane label="整库对比" name="db">
          <el-form label-width="100px">
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="源实例">
                  <el-select v-model="instanceName" filterable placeholder="源实例" style="width: 100%">
                    <el-option-group v-for="g in instanceGroups" :key="g.label" :label="g.label">
                      <el-option v-for="i in g.items" :key="i.id" :label="i.instance_name" :value="i.instance_name" />
                    </el-option-group>
                  </el-select>
                </el-form-item>
                <el-form-item label="源库">
                  <el-select v-model="dbName" filterable placeholder="源库" style="width: 100%">
                    <el-option v-for="d in dbOptions" :key="d" :label="d" :value="d" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="目标实例">
                  <el-select v-model="targetInstanceName" filterable placeholder="目标实例" style="width: 100%">
                    <el-option-group v-for="g in instanceGroups" :key="g.label" :label="g.label">
                      <el-option v-for="i in g.items" :key="i.id" :label="i.instance_name" :value="i.instance_name" />
                    </el-option-group>
                  </el-select>
                </el-form-item>
                <el-form-item label="目标库">
                  <el-select
                    v-model="targetDbName"
                    filterable
                    allow-create
                    default-first-option
                    placeholder="目标库（可输入 all 同步全部）"
                    style="width: 100%"
                  >
                    <el-option v-for="d in targetDbOptions" :key="d" :label="d" :value="d" />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="选项">
              <el-checkbox v-model="syncAutoInc">同步自增列</el-checkbox>
              <el-checkbox v-model="syncComments">同步注释</el-checkbox>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="loading" @click="onCompare">开始对比</el-button>
            </el-form-item>
          </el-form>

          <template v-if="result">
            <el-card shadow="never">
              <template #header>
                对比结果（diff）
                <el-button link type="primary" size="small" @click="copyText(result!.diff_stdout)">
                  复制
                </el-button>
              </template>
              <pre class="sql-text">{{ result.diff_stdout || "（无差异）" }}</pre>
            </el-card>
            <el-card shadow="never">
              <template #header>
                Patch SQL（变更脚本）
                <el-button link type="primary" size="small" @click="copyText(result!.patch_stdout)">
                  复制
                </el-button>
              </template>
              <pre class="sql-text">{{ result.patch_stdout || "（无）" }}</pre>
            </el-card>
            <el-card shadow="never">
              <template #header>Revert SQL（回滚脚本）</template>
              <pre class="sql-text">{{ result.revert_stdout || "（无）" }}</pre>
            </el-card>
          </template>
        </el-tab-pane>

        <!-- 表级对比（information_schema diff） -->
        <el-tab-pane label="表级对比" name="table">
          <el-form label-width="100px">
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="源实例">
                  <el-select v-model="tdSrcInstance" filterable placeholder="源实例" style="width: 100%">
                    <el-option-group v-for="g in instanceGroups" :key="g.label" :label="g.label">
                      <el-option v-for="i in g.items" :key="i.id" :label="i.instance_name" :value="i.instance_name" />
                    </el-option-group>
                  </el-select>
                </el-form-item>
                <el-form-item label="源库">
                  <el-select v-model="tdSrcDb" filterable placeholder="源库" style="width: 100%">
                    <el-option v-for="d in tdSrcDbOptions" :key="d" :label="d" :value="d" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="目标实例">
                  <el-select v-model="tdDstInstance" filterable placeholder="目标实例" style="width: 100%">
                    <el-option-group v-for="g in instanceGroups" :key="g.label" :label="g.label">
                      <el-option v-for="i in g.items" :key="i.id" :label="i.instance_name" :value="i.instance_name" />
                    </el-option-group>
                  </el-select>
                </el-form-item>
                <el-form-item label="目标库">
                  <el-select v-model="tdDstDb" filterable placeholder="目标库" style="width: 100%">
                    <el-option v-for="d in tdDstDbOptions" :key="d" :label="d" :value="d" />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="对比表">
              <el-select
                v-model="tdTables"
                multiple
                filterable
                clearable
                placeholder="选择表（留空=整库对比）"
                style="width: 100%"
              >
                <el-option v-for="t in tdTableOptions" :key="t" :label="t" :value="t" />
              </el-select>
            </el-form-item>
            <el-form-item label="选项">
              <el-checkbox v-model="tdSyncAutoInc">同步自增值</el-checkbox>
              <el-checkbox v-model="tdSyncComments">同步注释</el-checkbox>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="tdLoading" @click="onTableDiff">开始对比</el-button>
              <span class="form-hint">生成 SQL 不直接执行，可复制或提交为 SQL 上线工单审核执行</span>
            </el-form-item>
          </el-form>

          <template v-if="tdResult">
            <div class="stat-row">
              <span>共 {{ tdResult.summary.total }} 张表</span>
              <el-tag type="warning" size="small">差异 {{ tdResult.summary.diff }}</el-tag>
              <el-tag type="success" size="small">一致 {{ tdResult.summary.same }}</el-tag>
              <el-tag type="info" size="small">单侧缺失 {{ tdResult.summary.missing }}</el-tag>
              <el-tag v-if="tdResult.summary.objects" type="info" size="small">
                视图/触发器/存储过程差异 {{ tdResult.summary.objects }}
              </el-tag>
            </div>

            <el-alert
              v-if="tdResult.objects.length"
              type="info"
              :closable="false"
              class="objects-alert"
            >
              <div v-for="o in tdResult.objects" :key="`${o.kind}-${o.name}`">
                {{ o.kind }} <code>{{ o.name }}</code> 仅{{ o.side === "source_only" ? "源" : "目标" }}存在（仅提示，不生成 DDL）
              </div>
            </el-alert>

            <el-collapse v-model="expandedTables" class="tables-collapse">
              <el-collapse-item
                v-for="t in tdResult.tables"
                :key="t.table"
                :name="t.table"
              >
                <template #title>
                  <span class="table-title">
                    <code>{{ t.table }}</code>
                    <el-tag :type="statusTagType(t.status)" size="small">
                      {{ STATUS_LABELS[t.status] || t.status }}
                    </el-tag>
                  </span>
                </template>

                <el-table
                  v-if="t.items.length"
                  :data="t.items"
                  stripe
                  border
                  size="small"
                  max-height="320"
                >
                  <el-table-column type="expand">
                    <template #default="{ row }">
                      <div class="expand-sql">
                        <div v-if="row.patch">
                          <div class="expand-label">patch 子句（源 → 目标）</div>
                          <pre class="sql-text">{{ row.patch }}</pre>
                        </div>
                        <div v-if="row.revert">
                          <div class="expand-label">revert 子句（目标 → 源）</div>
                          <pre class="sql-text">{{ row.revert }}</pre>
                        </div>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column label="对象" width="220">
                    <template #default="{ row }">
                      <span>{{ KIND_LABELS[row.kind] || row.kind }} </span>
                      <code>{{ row.object }}</code>
                    </template>
                  </el-table-column>
                  <el-table-column label="操作" width="90">
                    <template #default="{ row }">
                      <el-tag :type="actionTagType(row.action)" size="small">
                        {{ ACTION_LABELS[row.action] || row.action }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="源" show-overflow-tooltip>
                    <template #default="{ row }">{{ row.source ?? "—" }}</template>
                  </el-table-column>
                  <el-table-column label="目标" show-overflow-tooltip>
                    <template #default="{ row }">{{ row.target ?? "—" }}</template>
                  </el-table-column>
                  <el-table-column label="级别" width="80">
                    <template #default="{ row }">
                      <el-tag v-if="row.danger" type="danger" size="small">危险</el-tag>
                      <span v-else>—</span>
                    </template>
                  </el-table-column>
                </el-table>
                <div v-else class="same-hint">结构一致</div>

                <div v-if="t.patch_sql" class="table-sql">
                  <div class="expand-label">
                    Patch SQL
                    <el-button link type="primary" size="small" @click="copyText(t.patch_sql)">复制</el-button>
                  </div>
                  <pre class="sql-text">{{ t.patch_sql }}</pre>
                  <div class="expand-label">
                    Revert SQL
                    <el-button link type="primary" size="small" @click="copyText(t.revert_sql)">复制</el-button>
                  </div>
                  <pre class="sql-text">{{ t.revert_sql }}</pre>
                </div>
              </el-collapse-item>
            </el-collapse>

            <el-card v-if="tdResult.patch_sql" shadow="never" class="merged-card">
              <template #header>
                全部 Patch SQL（源 → 目标）
                <el-button link type="primary" size="small" @click="copyText(tdResult.patch_sql)">复制</el-button>
                <el-button
                  type="primary"
                  size="small"
                  @click="submitPatchAsWorkflow"
                >提交为 SQL 工单</el-button>
              </template>
              <pre class="sql-text">{{ tdResult.patch_sql }}</pre>
            </el-card>
            <el-card v-if="tdResult.revert_sql" shadow="never" class="merged-card">
              <template #header>
                全部 Revert SQL（目标 → 源）
                <el-button link type="primary" size="small" @click="copyText(tdResult.revert_sql)">复制</el-button>
              </template>
              <pre class="sql-text">{{ tdResult.revert_sql }}</pre>
            </el-card>
          </template>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.schemasync-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.sql-text {
  margin: 0;
  padding: 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  font-family: monospace;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 320px;
  overflow: auto;
}

.form-hint {
  margin-left: 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.stat-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.objects-alert {
  margin-bottom: 12px;

  code {
    margin: 0 4px;
  }
}

.tables-collapse {
  margin-bottom: 12px;
}

.table-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.same-hint {
  color: var(--el-text-color-secondary);
  padding: 4px 0;
}

.expand-sql,
.table-sql {
  .expand-label {
    margin: 8px 0 4px;
    font-size: 12px;
    color: var(--el-text-color-secondary);

    .el-button {
      margin-left: 8px;
    }
  }
}

.merged-card {
  margin-top: 12px;
}
</style>
