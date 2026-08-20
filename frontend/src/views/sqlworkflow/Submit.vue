<script setup lang="ts">
import { ref, reactive, watch, onMounted } from "vue";
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
  is_backup: false,
  run_date_start: "",
  run_date_end: "",
});

const sqlContent = ref("");
const groupOptions = ref<ResourceGroupRow[]>([]);
const instanceOptions = ref<GroupInstanceRow[]>([]);
const dbOptions = ref<Array<string | { value: string; text: string }>>([]);
const auditorsDisplay = ref("");

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

// SQL 内容变更后需重新检测（提交按钮重新锁定）
watch(sqlContent, () => {
  if (checked.value) {
    checked.value = false;
    checkRows.value = [];
  }
});

async function onCheck() {
  if (!form.instance) return ElMessage.warning("请选择实例");
  if (!form.db_name.trim()) return ElMessage.warning("请输入数据库名");
  if (!sqlContent.value.trim()) return ElMessage.warning("请输入 SQL");
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
    if (checkError.value > 0) {
      ElMessage.warning(`检测完成，存在 ${checkError.value} 个错误`);
    } else {
      ElMessage.success("检测完成");
    }
  } catch {
    // 后端 execute_check 异常以 DRF ValidationError({"errors": "..."}) 返回（HTTP 400），
    // 请求拦截器已弹出 ElMessage 提示，这里只需标记检测未通过、保持提交按钮锁定。
    checked.value = false;
  } finally {
    checking.value = false;
  }
}

function validate(): string | null {
  if (!form.workflow_name.trim()) return "请输入工单名称";
  if (form.workflow_name.length > 50) return "工单名称不能超过 50 字符";
  if (!form.group_id) return "请选择资源组";
  if (!form.instance) return "请选择实例";
  if (!form.db_name.trim()) return "请输入数据库名";
  if (!sqlContent.value.trim()) return "请输入 SQL";
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
  if (!checked.value) return ElMessage.warning("请先进行 SQL 检测");
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

onMounted(() => {
  // 消费 My2SQL「提交工单」handoff：预填工单名 + SQL（实例/组/库留用户选）
  const pending = handoff.consume();
  if (pending) {
    form.workflow_name = pending.workflow_name;
    sqlContent.value = pending.sql_content;
  }
  loadGroups();
});
</script>

<template>
  <div class="submit-page">
    <el-card shadow="never">
      <el-form :model="form" label-width="100px">
        <el-form-item label="工单名称" required>
          <el-input
            v-model="form.workflow_name"
            maxlength="50"
            show-word-limit
            placeholder="工单名称（≤50 字符）"
            style="max-width: 480px"
          />
        </el-form-item>
        <el-form-item label="需求链接">
          <el-input
            v-model="form.demand_url"
            placeholder="需求链接（可选）"
            style="max-width: 480px"
          />
        </el-form-item>
        <el-form-item label="资源组" required>
          <el-select
            v-model="form.group_id"
            placeholder="请选择资源组"
            filterable
            style="width: 280px"
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
        </el-form-item>
        <el-form-item label="实例" required>
          <el-select
            v-model="form.instance"
            placeholder="请选择实例"
            filterable
            :disabled="!form.group_id"
            style="width: 280px"
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
            style="width: 280px"
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
          <el-switch v-model="form.is_backup" />
        </el-form-item>
        <el-form-item label="可执行时间">
          <el-date-picker
            v-model="form.run_date_start"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm"
            placeholder="开始（可空=无限制）"
            style="width: 200px"
          />
          <span class="tilde">~</span>
          <el-date-picker
            v-model="form.run_date_end"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm"
            placeholder="结束（可空=无限制）"
            style="width: 200px"
          />
        </el-form-item>
        <el-form-item label="SQL 内容" required>
          <SqlEditor v-model="sqlContent" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="checking" @click="onCheck">
            {{ checking ? "检测中..." : "SQL 检测" }}
          </el-button>
          <el-button
            type="success"
            :disabled="!checked || checking"
            :loading="submitting"
            @click="onSubmit"
          >
            提交工单
          </el-button>
        </el-form-item>
      </el-form>
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

.hint {
  margin-left: 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.tilde {
  margin: 0 8px;
}
</style>
