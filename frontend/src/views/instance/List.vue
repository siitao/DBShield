<script setup lang="ts">
import { ref, reactive, watch, onMounted } from "vue";
import { Search, Refresh, Plus } from "@element-plus/icons-vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { useAuthStore } from "@/stores/auth";
import {
  fetchInstances,
  fetchInstanceTags,
  fetchInstanceEngines,
  fetchTunnels,
  createInstance,
  updateInstance,
  deleteInstance,
  testInstanceConnection,
  fetchInstanceRds,
  createRds,
  updateRds,
  deleteRds,
  type InstanceRow,
  type InstanceForm,
  type InstanceEngineOption,
  type InstanceTagRow,
  type TunnelRow,
} from "@/api/instance";
import { fetchResourceGroups, type ResourceGroupRow } from "@/api/group";

const auth = useAuthStore();
const loading = ref(false);
const list = ref<InstanceRow[]>([]);
const total = ref(0);

const query = reactive({
  instance_name: "",
  db_type: "",
  page: 1,
  size: 15,
});

// 可选数据库类型从后端读取（只包含 ENABLED_ENGINES 中已启用的类型）
const dbTypes = ref<InstanceEngineOption[]>([]);

// 各数据库类型的默认端口（切换类型时自动带出，用户手改过则保留）
const DEFAULT_PORTS: Record<string, number> = {
  mysql: 3306,
  mssql: 1433,
  redis: 6379,
  pgsql: 5432,
  oracle: 1521,
  mongo: 27017,
  phoenix: 8765,
  clickhouse: 9000,
  goinception: 4000,
  cassandra: 9042,
  doris: 9030,
  elasticsearch: 9200,
  opensearch: 9200,
  memcached: 11211,
  tdengine: 6041,
};

function defaultDbType() {
  return dbTypes.value.some((t) => t.value === "mysql")
    ? "mysql"
    : dbTypes.value[0]?.value || "mysql";
}

function defaultPort(dbType: string) {
  return DEFAULT_PORTS[dbType] ?? 3306;
}

const typeMap: Record<string, string> = { master: "主库", slave: "从库" };

async function loadData() {
  loading.value = true;
  try {
    const { data } = await fetchInstances({
      page: query.page,
      size: query.size,
      instance_name__icontains: query.instance_name || undefined,
      db_type: query.db_type || undefined,
    });
    list.value = data.results || [];
    total.value = data.count || 0;
  } catch {
    // 错误提示已由 request 拦截器处理
  } finally {
    loading.value = false;
  }
}

function onSearch() {
  query.page = 1;
  loadData();
}

function onReset() {
  query.instance_name = "";
  query.db_type = "";
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

// ============ CRUD（仅超管，对齐旧版 admin-only）============
const dialogVisible = ref(false);
const isEdit = ref(false);
const submitting = ref(false);
const tags = ref<InstanceTagRow[]>([]);
const tunnels = ref<TunnelRow[]>([]);
const groups = ref<ResourceGroupRow[]>([]);

const form = reactive<InstanceForm>({
  id: undefined,
  instance_name: "",
  type: "master",
  db_type: "mysql",
  mode: "",
  host: "",
  port: 3306,
  user: "",
  password: "",
  is_ssl: false,
  verify_ssl: true,
  db_name: "",
  show_db_name_regex: "",
  denied_db_name_regex: "",
  charset: "",
  service_name: "",
  sid: "",
  resource_group: [],
  instance_tag: [],
  tunnel: null,
});

// 上一次选中的数据库类型，用于判断端口是否仍是旧类型的默认值
let prevDbType = form.db_type;

const testing = ref(false);
const testResult = ref<{ ok: boolean; msg: string } | null>(null);

// Aliyun RDS（可选，OneToOne）
const rdsEnabled = ref(false);
const rdsForm = reactive({
  id: undefined as number | undefined,
  rds_dbinstanceid: "",
  is_enable: false,
  ak_id: undefined as number | undefined,
  key_id: "",
  key_secret: "",
  remark: "",
});

function resetRds() {
  Object.assign(rdsForm, {
    id: undefined,
    rds_dbinstanceid: "",
    is_enable: false,
    ak_id: undefined,
    key_id: "",
    key_secret: "",
    remark: "",
  });
  rdsEnabled.value = false;
}

function resetForm() {
  const dbType = defaultDbType();
  Object.assign(form, {
    id: undefined,
    instance_name: "",
    type: "master",
    db_type: dbType,
    mode: "",
    host: "",
    port: defaultPort(dbType),
    user: "",
    password: "",
    is_ssl: false,
    verify_ssl: true,
    db_name: "",
    show_db_name_regex: "",
    denied_db_name_regex: "",
    charset: "",
    service_name: "",
    sid: "",
    resource_group: [],
    instance_tag: [],
    tunnel: null,
  });
  prevDbType = dbType;
  testResult.value = null;
  resetRds();
}

function onDbTypeChange(value: string) {
  // 只有当前端口为空或仍是旧类型默认值时才自动切换，避免覆盖手工填写的端口
  const oldDefault = DEFAULT_PORTS[prevDbType];
  if (!form.port || form.port === oldDefault) {
    form.port = DEFAULT_PORTS[value] ?? form.port;
  }
  prevDbType = value;
}

async function loadOptions() {
  try {
    const [t, tn, g, e] = await Promise.all([
      fetchInstanceTags(),
      fetchTunnels(),
      fetchResourceGroups({ size: 1000 }),
      fetchInstanceEngines(),
    ]);
    tags.value = t;
    tunnels.value = tn.data.results || [];
    groups.value = g.data.results || [];
    dbTypes.value = e;
  } catch {
    // 拦截器已提示
  }
}

async function openCreate() {
  isEdit.value = false;
  resetForm();
  dialogVisible.value = true;
}

async function openEdit(row: InstanceRow) {
  isEdit.value = true;
  Object.assign(form, {
    id: row.id,
    instance_name: row.instance_name,
    type: row.type,
    db_type: row.db_type,
    mode: (row.mode as string) || "",
    host: row.host,
    port: row.port,
    user: row.user || "",
    password: "", // 编辑时密码留空表示不改
    is_ssl: (row.is_ssl as boolean) ?? false,
    verify_ssl: (row.verify_ssl as boolean) ?? true,
    db_name: row.db_name || "",
    show_db_name_regex: (row.show_db_name_regex as string) || "",
    denied_db_name_regex: (row.denied_db_name_regex as string) || "",
    charset: row.charset || "",
    service_name: (row.service_name as string) || "",
    sid: (row.sid as string) || "",
    resource_group: (row.resource_group as number[]) || [],
    instance_tag: (row.instance_tag as number[]) || [],
    tunnel: (row.tunnel as number) || null,
  });
  prevDbType = form.db_type;
  testResult.value = null;
  dialogVisible.value = true;
  // 回填 Aliyun RDS（如有）
  resetRds();
  const rds = await fetchInstanceRds(row.id);
  if (rds) {
    rdsEnabled.value = true;
    Object.assign(rdsForm, {
      id: rds.id,
      rds_dbinstanceid: rds.rds_dbinstanceid || "",
      is_enable: rds.is_enable,
      ak_id: rds.ak?.id,
      key_id: rds.ak?.key_id || "",
      key_secret: rds.ak?.key_secret || "",
      remark: rds.ak?.remark || "",
    });
  }
}

async function saveRds(instanceId: number) {
  if (!rdsEnabled.value) {
    // 未勾选但有旧配置 → 删除
    if (rdsForm.id) await deleteRds(rdsForm.id).catch(() => {});
    return;
  }
  if (!rdsForm.rds_dbinstanceid.trim() || !rdsForm.key_id.trim()) {
    throw new Error("RDS 配置不完整：需填实例ID、AccessKey ID");
  }
  // 新增必须填 Secret；编辑留空 = 保持原值（key_secret 后端 write_only 不回显，无法回填）
  if (!rdsForm.id && !rdsForm.key_secret.trim()) {
    throw new Error("RDS 配置不完整：需填 AccessKey Secret");
  }
  const payload = {
    rds_dbinstanceid: rdsForm.rds_dbinstanceid.trim(),
    is_enable: rdsForm.is_enable,
    instance: instanceId,
    ak: {
      id: rdsForm.ak_id,
      key_id: rdsForm.key_id.trim(),
      key_secret: rdsForm.key_secret.trim(),
      remark: rdsForm.remark || "",
    },
  };
  if (rdsForm.id) {
    await updateRds(rdsForm.id, payload);
  } else {
    await createRds(payload);
  }
}

async function onSubmit() {
  if (!form.instance_name.trim()) return ElMessage.warning("请填写实例名称");
  if (!form.host.trim()) return ElMessage.warning("请填写实例连接");
  submitting.value = true;
  try {
    const payload: InstanceForm = { ...form };
    // 编辑且密码留空 → 不传 password（保持原密码）
    if (isEdit.value && !payload.password) delete payload.password;
    let instanceId = form.id;
    if (isEdit.value && form.id) {
      await updateInstance(form.id, payload);
      ElMessage.success("已更新");
    } else {
      const { data } = await createInstance(payload);
      instanceId = data.id;
      ElMessage.success("已新增");
    }
    // 保存 Aliyun RDS（编辑用 form.id，新增用返回的 id）
    try {
      await saveRds(instanceId as number);
    } catch (e) {
      ElMessage.warning(`实例已保存，但 RDS 配置保存失败：${(e as Error).message}`);
    }
    dialogVisible.value = false;
    loadData();
  } catch {
    // 拦截器已提示
  } finally {
    submitting.value = false;
  }
}

async function onTestConnection() {
  if (!form.host.trim()) return ElMessage.warning("请填写实例连接");
  testing.value = true;
  testResult.value = null;
  try {
    const payload: InstanceForm = { ...form };
    // 编辑时密码留空表示沿用已保存密码，交给后端按 id 回填
    if (isEdit.value && !payload.password) delete payload.password;
    const { data } = await testInstanceConnection(payload);
    testResult.value =
      data.status === 0
        ? { ok: true, msg: data.msg || "连接成功" }
        : { ok: false, msg: data.msg || "连接失败" };
  } catch (e) {
    testResult.value = { ok: false, msg: (e as Error).message || "连接失败" };
  } finally {
    testing.value = false;
  }
}

// 连接信息变更后清掉上一次的测试结果，避免误导
watch(
  form,
  () => {
    if (testResult.value) testResult.value = null;
  },
  { deep: true },
);

async function onDelete(row: InstanceRow) {
  try {
    await ElMessageBox.confirm(`确认删除实例「${row.instance_name}」？`, "提示", {
      type: "warning",
    });
    await deleteInstance(row.id);
    ElMessage.success("已删除");
    loadData();
  } catch (e) {
    if (e !== "cancel") {
      // 业务错误已由拦截器提示
    }
  }
}

onMounted(() => {
  loadData();
  if (auth.isSuperuser) loadOptions();
});
</script>

<template>
  <div class="instance-page">
    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" :model="query" @submit.prevent>
        <el-form-item label="实例名称">
          <el-input
            v-model="query.instance_name"
            placeholder="支持模糊匹配"
            clearable
            @keyup.enter="onSearch"
          />
        </el-form-item>
        <el-form-item label="数据库类型">
          <el-select v-model="query.db_type" placeholder="全部" clearable style="width: 160px">
            <el-option v-for="t in dbTypes" :key="t.value" :label="t.label" :value="t.value" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :icon="Search" @click="onSearch">查询</el-button>
          <el-button :icon="Refresh" @click="onReset">重置</el-button>
          <el-button v-if="auth.isSuperuser" type="success" :icon="Plus" @click="openCreate">
            新增实例
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <el-table v-loading="loading" :data="list" stripe border style="width: 100%">
        <el-table-column type="index" label="#" width="55" />
        <el-table-column prop="instance_name" label="实例名称" min-width="160" show-overflow-tooltip />
        <el-table-column prop="db_type" label="数据库类型" width="130" />
        <el-table-column label="角色" width="80">
          <template #default="{ row }">
            {{ typeMap[(row as InstanceRow).type as string] || (row as InstanceRow).type }}
          </template>
        </el-table-column>
        <el-table-column prop="host" label="主机" min-width="160" show-overflow-tooltip />
        <el-table-column prop="port" label="端口" width="90" />
        <el-table-column prop="user" label="用户" width="120" show-overflow-tooltip />
        <el-table-column prop="db_name" label="数据库" min-width="120" show-overflow-tooltip />
        <el-table-column v-if="auth.isSuperuser" label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row as InstanceRow)">编辑</el-button>
            <el-button link type="danger" @click="onDelete(row as InstanceRow)">删除</el-button>
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

    <!-- 实例表单弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? '编辑实例' : '新增实例'"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-form :model="form" label-width="120px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="实例名称" required>
              <el-input v-model="form.instance_name" :disabled="isEdit" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="数据库类型" required>
              <el-select v-model="form.db_type" style="width: 100%" @change="onDbTypeChange">
                <el-option v-for="t in dbTypes" :key="t.value" :label="t.label" :value="t.value" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col v-if="form.db_type !== 'mongo'" :span="12">
            <el-form-item label="实例类型" required>
              <el-select v-model="form.type" style="width: 100%">
                <el-option label="主库" value="master" />
                <el-option label="从库" value="slave" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col v-if="form.db_type === 'redis'" :span="12">
            <el-form-item label="运行模式">
              <el-select v-model="form.mode" style="width: 100%" clearable>
                <el-option label="单机" value="standalone" />
                <el-option label="集群" value="cluster" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="实例连接" required>
              <el-input
                v-model="form.host"
                :placeholder="
                  form.db_type === 'mongo' ? '复制集可填 h1:3717,h2:3717,h3:3717' : ''
                "
              />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="端口">
              <el-input-number v-model="form.port" :min="0" :max="65535" controls-position="right" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="用户名">
              <el-input v-model="form.user" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="密码">
              <el-input
                v-model="form.password"
                type="password"
                show-password
                :placeholder="isEdit ? '留空表示不修改' : ''"
              />
            </el-form-item>
          </el-col>
          <el-col v-if="form.db_type === 'mysql'" :span="12">
            <el-form-item label="字符集">
              <el-input v-model="form.charset" placeholder="如 utf8mb4" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="默认数据库">
              <el-input v-model="form.db_name" />
            </el-form-item>
          </el-col>
          <el-col v-if="form.db_type === 'oracle'" :span="12">
            <el-form-item label="service_name">
              <el-input v-model="form.service_name" />
            </el-form-item>
          </el-col>
          <el-col v-if="form.db_type === 'oracle'" :span="12">
            <el-form-item label="sid">
              <el-input v-model="form.sid" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="显示库正则">
              <el-input v-model="form.show_db_name_regex" placeholder="如 ^(test_db|dmp_db)$，留空=全部" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="隐藏库正则">
              <el-input v-model="form.denied_db_name_regex" placeholder="优先级高于显示库正则" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="SSL">
              <el-switch v-model="form.is_ssl" />
            </el-form-item>
          </el-col>
          <el-col v-if="form.is_ssl" :span="12">
            <el-form-item label="验证证书">
              <el-switch v-model="form.verify_ssl" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="连接隧道">
              <el-select v-model="form.tunnel" clearable placeholder="无" style="width: 100%">
                <el-option
                  v-for="t in tunnels"
                  :key="t.id"
                  :label="`${t.tunnel_name} (${t.host}:${t.port})`"
                  :value="t.id"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="资源组">
              <el-select v-model="form.resource_group" multiple filterable placeholder="可多选" style="width: 100%">
                <el-option
                  v-for="g in groups"
                  :key="g.group_id"
                  :label="g.group_name"
                  :value="g.group_id"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="实例标签">
              <el-select v-model="form.instance_tag" multiple filterable placeholder="可多选" style="width: 100%">
                <el-option
                  v-for="t in tags"
                  :key="t.id"
                  :label="t.tag_name"
                  :value="t.id"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-collapse>
              <el-collapse-item title="Aliyun RDS 配置（可选）">
                <el-checkbox v-model="rdsEnabled">该实例是阿里云 RDS</el-checkbox>
                <template v-if="rdsEnabled">
                  <el-row :gutter="12" style="margin-top: 12px">
                    <el-col :span="12">
                      <el-form-item label="RDS实例ID" label-width="120px">
                        <el-input v-model="rdsForm.rds_dbinstanceid" />
                      </el-form-item>
                    </el-col>
                    <el-col :span="12">
                      <el-form-item label="启用" label-width="120px">
                        <el-switch v-model="rdsForm.is_enable" />
                      </el-form-item>
                    </el-col>
                    <el-col :span="12">
                      <el-form-item label="AccessKey ID" label-width="120px">
                        <el-input v-model="rdsForm.key_id" />
                      </el-form-item>
                    </el-col>
                    <el-col :span="12">
                      <el-form-item label="AccessKey Secret" label-width="120px">
                        <el-input v-model="rdsForm.key_secret" type="password" show-password />
                      </el-form-item>
                    </el-col>
                    <el-col :span="24">
                      <el-form-item label="备注" label-width="120px">
                        <el-input v-model="rdsForm.remark" />
                      </el-form-item>
                    </el-col>
                  </el-row>
                </template>
              </el-collapse-item>
            </el-collapse>
          </el-col>
        </el-row>
      </el-form>
      <el-alert
        v-if="testResult"
        :type="testResult.ok ? 'success' : 'error'"
        :title="testResult.msg"
        show-icon
        :closable="false"
        class="test-result"
      />
      <template #footer>
        <div class="dialog-footer">
          <el-button :loading="testing" @click="onTestConnection">测试连接</el-button>
          <div>
            <el-button @click="dialogVisible = false">取消</el-button>
            <el-button type="primary" :loading="submitting" @click="onSubmit">
              {{ isEdit ? "保存" : "新增" }}
            </el-button>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.instance-page {
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

.test-result {
  margin-top: 4px;
}

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
</style>
