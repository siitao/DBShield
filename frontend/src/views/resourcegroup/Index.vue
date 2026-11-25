<script setup lang="ts">
import { ref, reactive, onMounted } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { useAuthStore } from "@/stores/auth";
import {
  fetchResourceGroups,
  createResourceGroup,
  updateResourceGroup,
  deleteResourceGroup,
  fetchAuditors,
  changeAuditors,
  fetchAuthGroups,
  type ResourceGroupRow,
  type AuthGroupRow,
} from "@/api/resourcegroup";

const router = useRouter();
const auth = useAuthStore();
const isSuperuser = auth.isSuperuser;

const loading = ref(false);
const list = ref<ResourceGroupRow[]>([]);
const total = ref(0);
const query = reactive({ group_name: "", page: 1, size: 15 });

async function loadData() {
  loading.value = true;
  try {
    const { data } = await fetchResourceGroups({
      page: query.page,
      size: query.size,
      group_name__icontains: query.group_name || undefined,
    });
    list.value = data.results || [];
    total.value = data.count || 0;
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false;
  }
}

function onSearch() {
  query.page = 1;
  loadData();
}
function onPageChange(p: number) {
  query.page = p;
  loadData();
}
function onSizeChange(s: number) {
  query.size = s;
  query.page = 1;
  loadData();
}

// ============ 创建/编辑 ============
const formDialog = ref(false);
const formMode = ref<"create" | "edit">("create");
const form = reactive({
  group_id: 0,
  group_name: "",
  ding_webhook: "",
  feishu_webhook: "",
  qywx_webhook: "",
});

function openCreate() {
  formMode.value = "create";
  Object.assign(form, { group_id: 0, group_name: "", ding_webhook: "", feishu_webhook: "", qywx_webhook: "" });
  formDialog.value = true;
}
function openEdit(row: ResourceGroupRow) {
  formMode.value = "edit";
  Object.assign(form, {
    group_id: row.group_id,
    group_name: row.group_name,
    ding_webhook: row.ding_webhook || "",
    feishu_webhook: row.feishu_webhook || "",
    qywx_webhook: row.qywx_webhook || "",
  });
  formDialog.value = true;
}

async function onFormSubmit() {
  if (!form.group_name.trim()) return ElMessage.warning("请输入组名称");
  const payload = {
    group_name: form.group_name,
    ding_webhook: form.ding_webhook,
    feishu_webhook: form.feishu_webhook,
    qywx_webhook: form.qywx_webhook,
  };
  try {
    if (formMode.value === "create") {
      await createResourceGroup(payload);
      ElMessage.success("创建成功");
    } else {
      await updateResourceGroup(form.group_id, payload);
      ElMessage.success("保存成功");
    }
    formDialog.value = false;
    loadData();
  } catch {
    // 业务错误已提示
  }
}

async function onDelete(row: ResourceGroupRow) {
  try {
    await ElMessageBox.confirm(
      `确认删除资源组「${row.group_name}」？（软删除，可恢复）`,
      "提示",
      { type: "warning" }
    );
    await deleteResourceGroup(row.group_id);
    ElMessage.success("已删除");
    loadData();
  } catch (e) {
    if (e !== "cancel") {
      // 业务错误已提示
    }
  }
}

// ============ 审核流配置 ============
const auditorDialog = ref(false);
const auditorTarget = reactive({ group_name: "", workflow_type: 1 });
const auditorDisplay = ref("");
const auditorSelected = ref<string[]>([]); // auth Group name 列表
const authGroups = ref<AuthGroupRow[]>([]);
const auditorLoading = ref(false);

async function openAuditors(row: ResourceGroupRow) {
  auditorTarget.group_name = row.group_name;
  auditorTarget.workflow_type = 1;
  auditorDialog.value = true;
  auditorDisplay.value = "";
  auditorSelected.value = [];
  await loadAuthGroups();
  await loadAuditors();
}

async function loadAuthGroups() {
  try {
    const { data } = await fetchAuthGroups();
    authGroups.value = data.results || [];
  } catch {
    // 拦截器已提示
  }
}

async function loadAuditors() {
  auditorLoading.value = true;
  try {
    const d = await fetchAuditors(
      auditorTarget.group_name,
      auditorTarget.workflow_type
    );
    auditorDisplay.value = d.auditors_display || "无需审批";
    // auditors 是逗号分隔的 auth group id，转成 name 选中
    const ids = (d.auditors || "").split(",").filter(Boolean);
    auditorSelected.value = ids
      .map((id) => authGroups.value.find((g) => String(g.id) === id)?.name)
      .filter((n): n is string => !!n);
  } catch {
    // 拦截器已提示
  } finally {
    auditorLoading.value = false;
  }
}

async function onSaveAuditors() {
  if (!auditorSelected.value.length) {
    ElMessage.warning("请至少选择一个审批节点");
    return;
  }
  try {
    await changeAuditors({
      group_name: auditorTarget.group_name,
      audit_auth_groups: auditorSelected.value.join(","),
      workflow_type: auditorTarget.workflow_type,
    });
    ElMessage.success("审核流已更新");
    auditorDialog.value = false;
  } catch {
    // 业务错误已提示
  }
}

onMounted(loadData);
</script>

<template>
  <div class="rg-page">
    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent>
        <el-form-item label="组名称">
          <el-input
            v-model="query.group_name"
            placeholder="支持模糊匹配"
            clearable
            @keyup.enter="onSearch"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="onSearch">查询</el-button>
          <el-button v-if="isSuperuser" type="success" @click="openCreate">新建资源组</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <el-table v-loading="loading" :data="list" stripe border style="width: 100%">
        <el-table-column type="index" label="#" width="55" />
        <el-table-column prop="group_name" label="组名称" min-width="160" show-overflow-tooltip />
        <el-table-column prop="ding_webhook" label="钉钉" min-width="160" show-overflow-tooltip />
        <el-table-column prop="feishu_webhook" label="飞书" min-width="160" show-overflow-tooltip />
        <el-table-column prop="qywx_webhook" label="企微" min-width="160" show-overflow-tooltip />
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="router.push({ name: 'resourcegroup-relations', params: { id: (row as ResourceGroupRow).group_id } })">
              管理关联
            </el-button>
            <el-button v-if="isSuperuser" link type="primary" size="small" @click="openAuditors(row as ResourceGroupRow)">
              审核流
            </el-button>
            <el-button v-if="isSuperuser" link type="primary" size="small" @click="openEdit(row as ResourceGroupRow)">
              编辑
            </el-button>
            <el-button v-if="isSuperuser" link type="danger" size="small" @click="onDelete(row as ResourceGroupRow)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          :current-page="query.page"
          :page-size="query.size"
          :page-sizes="[15, 20, 50, 100]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </div>
    </el-card>

    <!-- 创建/编辑 -->
    <el-dialog v-model="formDialog" :title="formMode === 'create' ? '新建资源组' : '编辑资源组'" width="500px">
      <el-form label-width="90px">
        <el-form-item label="组名称" required>
          <el-input v-model="form.group_name" :disabled="formMode === 'edit'" />
        </el-form-item>
        <el-form-item label="钉钉 webhook">
          <el-input v-model="form.ding_webhook" />
        </el-form-item>
        <el-form-item label="飞书 webhook">
          <el-input v-model="form.feishu_webhook" />
        </el-form-item>
        <el-form-item label="企微 webhook">
          <el-input v-model="form.qywx_webhook" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formDialog = false">取消</el-button>
        <el-button type="primary" @click="onFormSubmit">确定</el-button>
      </template>
    </el-dialog>

    <!-- 审核流配置 -->
    <el-dialog v-model="auditorDialog" title="审核流配置" width="560px">
      <el-form label-width="90px" v-loading="auditorLoading">
        <el-form-item label="资源组">
          <span>{{ auditorTarget.group_name }}</span>
        </el-form-item>
        <el-form-item label="工单类型">
          <el-radio-group v-model="auditorTarget.workflow_type" @change="loadAuditors">
            <el-radio :value="1">查询权限</el-radio>
            <el-radio :value="2">SQL 上线</el-radio>
            <el-radio :value="3">数据归档</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="当前审批链">
          <el-tag>{{ auditorDisplay }}</el-tag>
        </el-form-item>
        <el-form-item label="审批节点">
          <el-select v-model="auditorSelected" multiple filterable placeholder="按顺序选择审批权限组" style="width: 100%">
            <el-option v-for="g in authGroups" :key="g.id" :label="g.name" :value="g.name" />
          </el-select>
          <div class="hint">按选择顺序形成审批链（A → B → C）</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="auditorDialog = false">取消</el-button>
        <el-button type="primary" @click="onSaveAuditors">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.rg-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.filter-card :deep(.el-form-item) {
  margin-bottom: 0;
}

.pager {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}

.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
}
</style>
