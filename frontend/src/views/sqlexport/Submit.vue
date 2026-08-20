<script setup lang="ts">
import { ref, reactive, watch, onMounted, computed } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  fetchResourceGroups,
  fetchGroupInstances,
  fetchGroupAuditors,
  type ResourceGroupRow,
  type GroupInstanceRow,
} from "@/api/group";
import { submitWorkflow, type ReviewRow } from "@/api/sqlworkflow";
import { fetchQueryResources } from "@/api/sqlquery";
import {
  exportPreCheck,
  EXPORT_FORMATS,
  type ExportPreCheckResult,
} from "@/api/sqlexport";
import SqlEditor from "@/components/SqlEditor.vue";
import SqlReviewTable from "@/components/SqlReviewTable.vue";

const router = useRouter();

const form = reactive({
  workflow_name: "",
  export_format: "csv",
  group_id: undefined as number | undefined,
  instance: undefined as number | undefined,
  db_name: "",
  run_date_start: "",
  run_date_end: "",
});

const sqlContent = ref("");
const groupOptions = ref<ResourceGroupRow[]>([]);
const instanceOptions = ref<GroupInstanceRow[]>([]);
const auditorsDisplay = ref("");
const dbOptions = ref<Array<string | { value: string; text: string }>>([]);

// 导出校验结果
const checkResult = ref<ExportPreCheckResult | null>(null);
const checked = ref(false);
const submitting = ref(false);

const affectedRows = computed(() => {
  const rows = checkResult.value?.rows || [];
  return Number(rows[0]?.affected_rows ?? 0);
});

async function loadGroups() {
  try {
    const { data } = await fetchResourceGroups({ size: 1000 });
    groupOptions.value = data.results || [];
  } catch {
    // 拦截器已提示
  }
}

// 选资源组 → 拉关联实例 + 审批流（旧版 /group/ 接口用 group_name，workflow_type=2）
watch(
  () => form.group_id,
  async (gid) => {
    instanceOptions.value = [];
    form.instance = undefined;
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

// SQL 内容变更后需重新校验（提交按钮重新锁定）
watch(sqlContent, () => {
  if (checked.value) {
    checked.value = false;
    checkResult.value = null;
  }
});

async function onCheck() {
  if (!form.instance) return ElMessage.warning("请选择实例");
  if (!form.db_name.trim()) return ElMessage.warning("请输入数据库名");
  if (!sqlContent.value.trim()) return ElMessage.warning("请输入 SQL");
  try {
    const r = await exportPreCheck({
      instance_name: (
        instanceOptions.value.find((i) => i.id === form.instance)?.instance_name ||
        ""
      ),
      db_name: form.db_name,
      sql_content: sqlContent.value,
    });
    checkResult.value = r;
    checked.value = true;
    if (r.error_count > 0) {
      ElMessage.warning(`校验未通过，存在 ${r.error_count} 个错误`);
    } else {
      ElMessage.success(`校验通过，预计导出 ${affectedRows.value} 行`);
    }
  } catch {
    // 拦截器已提示
  }
}

function validate(): string | null {
  if (!form.workflow_name.trim()) return "请输入工单名称";
  if (form.workflow_name.length > 50) return "工单名称不能超过 50 字符";
  if (!form.group_id) return "请选择资源组";
  if (!form.instance) return "请选择实例";
  if (!form.db_name.trim()) return "请输入数据库名";
  if (!form.export_format) return "请选择导出格式";
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
  if (!checked.value) return ElMessage.warning("请先进行导出校验");
  if (checkResult.value && checkResult.value.error_count > 0) {
    return ElMessage.warning("校验存在错误，请修正后重新校验");
  }
  try {
    if (checkResult.value && checkResult.value.warning_count > 0) {
      await ElMessageBox.confirm(
        `校验存在 ${checkResult.value.warning_count} 个警告，确认仍要提交？`,
        "提示",
        { type: "warning" }
      );
    }
    submitting.value = true;
    const { data } = await submitWorkflow({
      workflow: {
        workflow_name: form.workflow_name,
        group_id: form.group_id as number,
        instance: form.instance as number,
        db_name: form.db_name,
        is_backup: false,
        run_date_start: form.run_date_start || "",
        run_date_end: form.run_date_end || "",
        is_offline_export: 1,
        export_format: form.export_format,
      },
      sql_content: sqlContent.value,
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

onMounted(loadGroups);
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
            placeholder="导出工单名称（≤50 字符），如：XX项目会员数据导出"
            style="max-width: 480px"
          />
        </el-form-item>
        <el-form-item label="导出格式" required>
          <el-select v-model="form.export_format" style="width: 200px">
            <el-option
              v-for="f in EXPORT_FORMATS"
              :key="f.value"
              :label="f.label"
              :value="f.value"
            />
          </el-select>
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
          <el-button type="primary" @click="onCheck">导出校验</el-button>
          <el-button
            type="success"
            :disabled="!checked"
            :loading="submitting"
            @click="onSubmit"
          >
            提交导出
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="checked && checkResult" shadow="never">
      <template #header>
        导出校验结果（预计 {{ affectedRows }} 行 / 警告
        {{ checkResult.warning_count }} / 错误 {{ checkResult.error_count }}）
      </template>
      <SqlReviewTable :rows="checkResult.rows as ReviewRow[]" phase="review" />
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
