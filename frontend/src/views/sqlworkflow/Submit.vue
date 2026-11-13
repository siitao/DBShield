<script setup lang="ts">
import { ref, reactive, watch, onMounted, onBeforeUnmount } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  fetchResourceGroups,
  fetchGroupInstances,
  fetchGroupAuditors,
  type ResourceGroupRow,
  type GroupInstanceRow,
} from "@/api/group";
import { sqlCheck, submitWorkflow, type ReviewRow } from "@/api/sqlworkflow";
import { fetchQueryResources } from "@/api/sqlquery";
import SqlEditor from "@/components/SqlEditor.vue";
import SqlReviewTable from "@/components/SqlReviewTable.vue";
import { useBinlogHandoffStore } from "@/stores/binlogHandoff";

const router = useRouter();
const handoff = useBinlogHandoffStore();

const form = reactive({
  workflow_name: "",
  demand_url: "",
  group_id: undefined as number | undefined,
  instance: undefined as number | undefined,
  db_name: "",
  // 后端默认强制备份（enable_backup_switch 未开启时覆盖为 true），默认值与之对齐
  is_backup: true,
  run_date_start: "",
  run_date_end: "",
});

const sqlContent = ref("");
const groupOptions = ref<ResourceGroupRow[]>([]);
const instanceOptions = ref<GroupInstanceRow[]>([]);
const dbOptions = ref<Array<string | { value: string; text: string }>>([]);
const auditorsDisplay = ref("");

// 工单名默认值：用户手动编辑后不再覆盖
const nameTouched = ref(false);
const autoName = ref("");

const DRAFT_KEY = "archery:sqlworkflow:draft:v1";
let draftTimer: number | null = null;
let draftEnabled = true;
const draftRestored = ref(false);

// SQL 检测结果
const checkRows = ref<ReviewRow[]>([]);
const checkWarning = ref(0);
const checkError = ref(0);
const checked = ref(false);
const submitting = ref(false);
const checking = ref(false);

async function loadGroups() {
  try {
    const { data } = await fetchResourceGroups({ size: 1000 });
    groupOptions.value = data.results || [];
  } catch {
    // 拦截器已提示
  }
}

// 选资源组 → 拉关联实例 + 审批流（旧版 /group/ 接口用 group_name）
watch(
  () => form.group_id,
  async (gid) => {
    instanceOptions.value = [];
    form.instance = undefined;
    dbOptions.value = [];
    form.db_name = "";
    auditorsDisplay.value = "";
    if (!gid) return;
    const grp = groupOptions.value.find((g) => g.group_id === gid);
    if (!grp) return;
    const gname = grp.group_name as string;
    try {
      instanceOptions.value = await fetchGroupInstances(gname);
    } catch {
      // 拦截器已提示
    }
    try {
      const a = await fetchGroupAuditors(gname);
      auditorsDisplay.value = a.auditors_display || "无需审批";
    } catch {
      // 拦截器已提示
    }
  }
);

// 选实例 → 拉数据库列表
watch(
  () => form.instance,
  async (iid) => {
    dbOptions.value = [];
    form.db_name = "";
    if (!iid) return;
    try {
      dbOptions.value = await fetchQueryResources({
        instance_id: iid,
        resource_type: "database",
      });
    } catch {
      // 拦截器已提示
    }
  }
);

// SQL 内容变更后需重新检测（提交时自动重检）
watch(sqlContent, () => {
  if (checked.value) {
    checked.value = false;
    checkRows.value = [];
  }
  maybeFillName();
  scheduleSaveDraft();
});

// 库/表变化时同步刷新默认工单名
watch(
  () => form.db_name,
  () => {
    maybeFillName();
    scheduleSaveDraft();
  }
);

/** 从 SQL 里粗略猜一个表名，用于默认工单名 */
function guessTableFromSql(sql: string): string {
  const m = sql.match(/\b(?:from|update|into|table)\s+[`"\[\]]?([\w.$]+)/i);
  if (!m) return "";
  return m[1].split(".").pop()!.replace(/[`"[\]]/g, "");
}

function buildDefaultName(): string {
  const db = form.db_name.trim();
  const table = guessTableFromSql(sqlContent.value);
  const now = new Date();
  const date = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("");
  return ([db, table, date].filter(Boolean).join("_") || `SQL工单_${date}`).slice(
    0,
    50
  );
}

/** 仅当工单名仍为空或仍是自动生成值时刷新，避免覆盖用户输入 */
function maybeFillName() {
  if (nameTouched.value) return;
  if (form.workflow_name.trim() && form.workflow_name !== autoName.value) return;
  autoName.value = buildDefaultName();
  form.workflow_name = autoName.value;
}

function saveDraft() {
  if (!draftEnabled) return;
  const payload = {
    workflow_name: form.workflow_name,
    demand_url: form.demand_url,
    is_backup: form.is_backup,
    run_date_start: form.run_date_start,
    run_date_end: form.run_date_end,
    sql_content: sqlContent.value,
    saved_at: Date.now(),
  };
  try {
    // 没有 SQL 就没有恢复价值，避免把自动生成的工单名当成草稿
    if (!payload.sql_content.trim()) {
      localStorage.removeItem(DRAFT_KEY);
      return;
    }
    localStorage.setItem(DRAFT_KEY, JSON.stringify(payload));
  } catch {
    // 草稿是增强能力，存储失败不影响提交
  }
}

function scheduleSaveDraft() {
  if (draftTimer) window.clearTimeout(draftTimer);
  draftTimer = window.setTimeout(saveDraft, 500);
}

function restoreDraft() {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (!raw) return;
    const draft = JSON.parse(raw) as Record<string, unknown>;
    if (!draft || typeof draft !== "object") return;
    if (draft.workflow_name) {
      form.workflow_name = String(draft.workflow_name);
      nameTouched.value = true;
    }
    if (draft.demand_url) form.demand_url = String(draft.demand_url);
    if (typeof draft.is_backup === "boolean") form.is_backup = draft.is_backup;
    if (draft.run_date_start) form.run_date_start = String(draft.run_date_start);
    if (draft.run_date_end) form.run_date_end = String(draft.run_date_end);
    if (draft.sql_content) sqlContent.value = String(draft.sql_content);
    if (draft.sql_content || draft.workflow_name) {
      draftRestored.value = true;
      ElMessage.info("已恢复上次未提交的工单草稿");
    }
  } catch {
    // 草稿损坏时忽略，不阻塞新建工单
  }
}

function clearDraft() {
  draftEnabled = false;
  localStorage.removeItem(DRAFT_KEY);
}

/** 放弃草稿并清空表单，方便重新发起一张工单 */
async function resetForm() {
  try {
    await ElMessageBox.confirm("确认清空当前草稿和已填内容？", "清空草稿", {
      type: "warning",
    });
  } catch {
    return;
  }
  if (draftTimer) window.clearTimeout(draftTimer);
  form.workflow_name = "";
  form.demand_url = "";
  form.group_id = undefined;
  form.instance = undefined;
  form.db_name = "";
  form.is_backup = true;
  form.run_date_start = "";
  form.run_date_end = "";
  sqlContent.value = "";
  checked.value = false;
  checkRows.value = [];
  checkWarning.value = 0;
  checkError.value = 0;
  nameTouched.value = false;
  autoName.value = "";
  draftRestored.value = false;
  localStorage.removeItem(DRAFT_KEY);
  draftEnabled = true;
}

// 表单内容变化时自动保存草稿（SQL 单独 watch 已处理，这里覆盖其余字段）
watch(
  () => [
    form.workflow_name,
    form.demand_url,
    form.is_backup,
    form.run_date_start,
    form.run_date_end,
  ],
  scheduleSaveDraft
);

async function runCheck(): Promise<boolean> {
  if (!form.instance) {
    ElMessage.warning("请选择实例");
    return false;
  }
  if (!form.db_name.trim()) {
    ElMessage.warning("请输入数据库名");
    return false;
  }
  if (!sqlContent.value.trim()) {
    ElMessage.warning("请输入 SQL");
    return false;
  }
  checking.value = true;
  // 检测期间清除上次结果，避免「旧结果 + 转圈」并存
  checked.value = false;
  checkRows.value = [];
  try {
    const { data } = await sqlCheck({
      instance_id: form.instance,
      db_name: form.db_name,
      full_sql: sqlContent.value,
    });
    checkRows.value = Array.isArray(data.rows) ? (data.rows as ReviewRow[]) : [];
    checkWarning.value = Number(data.warning_count ?? 0);
    checkError.value = Number(data.error_count ?? 0);
    checked.value = true;
    return true;
  } catch {
    // 后端 execute_check 异常以 DRF ValidationError({"errors": "..."}) 返回（HTTP 400），
    // 请求拦截器已弹出 ElMessage 提示，这里只需标记检测未通过。
    checked.value = false;
    return false;
  } finally {
    checking.value = false;
  }
}

async function onCheck() {
  const ok = await runCheck();
  if (!ok) return;
  if (checkError.value > 0) {
    ElMessage.warning(`检测完成，存在 ${checkError.value} 个错误`);
  } else {
    ElMessage.success("检测完成");
  }
}

function validate(): string | null {
  if (!form.group_id) return "请选择资源组";
  if (!form.instance) return "请选择实例";
  if (!form.db_name.trim()) return "请输入数据库名";
  if (!sqlContent.value.trim()) return "请输入 SQL";
  if (!form.workflow_name.trim()) {
    form.workflow_name = buildDefaultName();
    autoName.value = form.workflow_name;
  }
  if (form.workflow_name.length > 50) return "工单名称不能超过 50 字符";
  if (form.run_date_start && form.run_date_end) {
    const s = new Date(form.run_date_start.replace(" ", "T"));
    const e = new Date(form.run_date_end.replace(" ", "T"));
    if (e.getTime() - s.getTime() < 60 * 60 * 1000) {
      return "可执行时间范围间隔不能少于 60 分钟";
    }
  }
  return null;
}

async function onSubmit() {
  const err = validate();
  if (err) return ElMessage.warning(err);
  // 一键提交：未检测时自动先检测；检测失败则不提交
  if (!checked.value) {
    const ok = await runCheck();
    if (!ok) return;
  }
  try {
    if (checkWarning.value > 0 || checkError.value > 0) {
      await ElMessageBox.confirm(
        `检测存在 ${checkWarning.value} 个警告、${checkError.value} 个错误，确认仍要提交？`,
        "提示",
        { type: "warning" }
      );
    }
    submitting.value = true;
    const { data } = await submitWorkflow({
      workflow: {
        workflow_name: form.workflow_name,
        demand_url: form.demand_url,
        group_id: form.group_id as number,
        instance: form.instance as number,
        db_name: form.db_name,
        is_backup: form.is_backup,
        run_date_start: form.run_date_start || "",
        run_date_end: form.run_date_end || "",
        is_offline_export: 0,
      },
      sql_content: sqlContent.value,
      // 回传检测阶段的含 AI 字段的结果，提交时复用，避免重复调用 AI
      ai_review_content: checkRows.value,
    });
    clearDraft();
    ElMessage.success("提交成功");
    router.replace({
      name: "sqlworkflow-detail",
      params: { id: data.workflow_id },
    });
  } catch (e) {
    if (e !== "cancel") {
      // 业务错误已由拦截器提示
    }
  } finally {
    submitting.value = false;
  }
}

onMounted(async () => {
  // 消费 My2SQL「提交工单」handoff：预填工单名 + SQL（实例/组/库留用户选）
  const pending = handoff.consume();
  if (pending) {
    form.workflow_name = pending.workflow_name;
    nameTouched.value = true;
    sqlContent.value = pending.sql_content;
    scheduleSaveDraft();
  } else {
    // 先恢复草稿，避免资源组接口慢时编辑器长时间空白
    restoreDraft();
  }
  await loadGroups();
});

onBeforeUnmount(() => {
  if (draftTimer) window.clearTimeout(draftTimer);
  // 提交成功时 clearDraft 已禁用保存，不会把刚提交的内容又存回草稿
  saveDraft();
});
</script>

<template>
  <div class="submit-page">
    <el-card shadow="never">
      <div class="submit-layout">
        <!-- 左：SQL 内容 -->
        <div class="pane-left">
          <div class="pane-title required">SQL 内容</div>
          <SqlEditor v-model="sqlContent" fill-height class="pane-editor" />
        </div>
        <!-- 右：选项与按钮 -->
        <div class="pane-right">
          <el-form :model="form" label-width="100px">
            <el-form-item label="工单名称" required>
              <el-input
                v-model="form.workflow_name"
                maxlength="50"
                show-word-limit
                placeholder="留空自动生成，也可手动填写（≤50 字符）"
                @input="nameTouched = true"
              />
            </el-form-item>
            <el-form-item label="需求链接">
              <el-input v-model="form.demand_url" placeholder="需求链接（可选）" />
            </el-form-item>
            <el-form-item label="资源组" required>
              <div class="field-stack">
                <el-select
                  v-model="form.group_id"
                  placeholder="请选择资源组"
                  filterable
                  style="width: 100%"
                >
                  <el-option
                    v-for="g in groupOptions"
                    :key="g.group_id"
                    :label="g.group_name"
                    :value="g.group_id"
                  />
                </el-select>
                <span v-if="auditorsDisplay" class="hint">
                  审批流：{{ auditorsDisplay }}
                </span>
              </div>
            </el-form-item>
            <el-form-item label="实例" required>
              <el-select
                v-model="form.instance"
                placeholder="请选择实例"
                filterable
                :disabled="!form.group_id"
                style="width: 100%"
              >
                <el-option
                  v-for="i in instanceOptions"
                  :key="i.id"
                  :label="i.instance_name"
                  :value="i.id"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="数据库" required>
              <el-select
                v-model="form.db_name"
                placeholder="请选择数据库"
                filterable
                :disabled="!form.instance"
                style="width: 100%"
              >
                <el-option
                  v-for="d in dbOptions"
                  :key="typeof d === 'object' ? d.value : d"
                  :label="typeof d === 'object' ? d.text : d"
                  :value="typeof d === 'object' ? d.value : d"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="是否备份">
              <div class="field-stack">
                <el-switch v-model="form.is_backup" />
                <span class="hint">
                  系统未开启「备份选项」配置时，提交后将强制备份，以工单详情为准
                </span>
              </div>
            </el-form-item>
            <el-form-item label="可执行时间">
              <div class="date-range">
                <el-date-picker
                  v-model="form.run_date_start"
                  type="datetime"
                  value-format="YYYY-MM-DD HH:mm"
                  placeholder="开始（可空=无限制）"
                />
                <span class="tilde">~</span>
                <el-date-picker
                  v-model="form.run_date_end"
                  type="datetime"
                  value-format="YYYY-MM-DD HH:mm"
                  placeholder="结束（可空=无限制）"
                />
              </div>
            </el-form-item>
            <el-form-item>
              <div class="submit-actions">
                <el-button
                  type="success"
                  :loading="submitting"
                  :disabled="checking"
                  @click="onSubmit"
                >
                  {{ checked ? "提交工单" : "检测并提交" }}
                </el-button>
                <el-button :loading="checking" :disabled="submitting" @click="onCheck">
                  仅检测
                </el-button>
                <el-button
                  v-if="draftRestored"
                  link
                  type="info"
                  @click="resetForm"
                >
                  清空草稿
                </el-button>
              </div>
              <div v-if="checked" class="check-status">
                <el-tag v-if="checkError > 0" type="danger" size="small">
                  错误 {{ checkError }}
                </el-tag>
                <el-tag v-if="checkWarning > 0" type="warning" size="small">
                  警告 {{ checkWarning }}
                </el-tag>
                <el-tag
                  v-if="!checkError && !checkWarning"
                  type="success"
                  size="small"
                >
                  检测通过
                </el-tag>
              </div>
            </el-form-item>
          </el-form>
        </div>
      </div>
    </el-card>

    <el-card v-if="checked" shadow="never">
      <template #header>
        SQL 检测结果（警告 {{ checkWarning }} / 错误 {{ checkError }}）
      </template>
      <SqlReviewTable :rows="checkRows" phase="review" />
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.submit-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.submit-layout {
  display: flex;
  align-items: stretch;
  gap: 24px;
}

.pane-left {
  flex: 1 1 60%;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.pane-title {
  margin-bottom: 8px;
  font-weight: 600;

  &.required::before {
    content: "*";
    margin-right: 4px;
    color: var(--el-color-danger);
  }
}

.pane-editor {
  flex: 1;
  min-height: 420px;
}

.pane-right {
  flex: 1 1 40%;
  min-width: 320px;
}

.field-stack {
  width: 100%;

  .hint {
    display: block;
    margin-top: 4px;
    line-height: 1.5;
  }
}

.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.submit-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.check-status {
  display: flex;
  gap: 6px;
  margin-top: 8px;
}

.date-range {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;

  :deep(.el-date-editor) {
    flex: 1;
    min-width: 0;
  }
}

.tilde {
  color: var(--el-text-color-secondary);
}

@media (max-width: 992px) {
  .submit-layout {
    flex-direction: column;
  }

  .pane-right {
    min-width: 0;
  }
}
</style>
