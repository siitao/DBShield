<script setup lang="ts">
import { ref, reactive, computed, watch, nextTick, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { useInstanceSelect } from "@/composables/useInstanceSelect";
import {
  fetchUsers,
  createAccount,
  editAccount,
  grantAccount,
  grantMongoAccount,
  MONGO_ROLES,
  resetPwd,
  lockAccount,
  deleteAccount,
  type AccountRow,
} from "@/api/instance_admin";
import { fetchQueryResources } from "@/api/sqlquery";
import { useAuthStore } from "@/stores/auth";
import { parseShowGrants, type ParsedGrants } from "@/utils/mysqlGrants";
import {
  MYSQL_PRIVILEGES,
  PRIV_LEVEL_LABEL,
  type PrivLevelMeta,
} from "@/config/mysqlPrivileges";

const auth = useAuthStore();
const { instanceName, instanceGroups, currentInstance, currentDbType, loadInstances } =
  useInstanceSelect();

const list = ref<AccountRow[]>([]);
const loading = ref(false);
const savedOnly = ref(0);

const canManage = computed(() => auth.hasPerm("sql.instance_account_manage"));

// ============ 创建/编辑 ============
const formDialog = ref(false);
const formMode = ref<"create" | "edit">("create");
const form = reactive({
  user: "",
  host: "%",
  password1: "",
  password2: "",
  password: "",
  remark: "",
});

function openCreate() {
  formMode.value = "create";
  Object.assign(form, { user: "", host: "%", password1: "", password2: "", password: "", remark: "" });
  formDialog.value = true;
}
function openEdit(row: AccountRow) {
  formMode.value = "edit";
  Object.assign(form, {
    user: String(row.user || ""),
    host: String(row.host || ""),
    password1: "",
    password2: "",
    password: "",
    remark: String(row.remark || ""),
  });
  formDialog.value = true;
}

async function onFormSubmit() {
  if (!currentInstance.value) return;
  if (!form.user.trim()) return ElMessage.warning("请输入用户名");
  try {
    if (formMode.value === "create") {
      if (form.password1 !== form.password2) return ElMessage.warning("两次密码不一致");
      if (!form.password1) return ElMessage.warning("请输入密码");
      await createAccount({
        instance_id: currentInstance.value.id,
        user: form.user,
        host: form.host,
        password1: form.password1,
        password2: form.password2,
        remark: form.remark,
      });
      ElMessage.success("创建成功");
    } else {
      await editAccount({
        instance_id: currentInstance.value.id,
        user: form.user,
        host: form.host,
        password: form.password,
        remark: form.remark,
      });
      ElMessage.success("保存成功");
    }
    formDialog.value = false;
    loadData();
  } catch {
    // 业务错误已提示
  }
}

// ============ 授权（el-tree 选择） ============
const grantDialog = ref(false);
const grantTarget = reactive({ user_host: "", display: "" });
const grantForm = reactive({
  op_type: 0 as 0 | 1, // 0=赋权 1=回收
  priv_type: 1 as 0 | 1 | 2 | 3, // 0全局/1库/2表/3列
  privs: [] as string[],
  db_name: [] as string[],
  tb_name: [] as string[],
  col_name: [] as string[],
});
const dbOptions = ref<string[]>([]);
const tbOptions = ref<string[]>([]);
const colOptions = ref<string[]>([]);
const grantSubmitting = ref(false);

/** 权限树数据（按级别过滤组） */
const currentLevelMeta = computed<PrivLevelMeta | undefined>(
  () => MYSQL_PRIVILEGES[grantForm.priv_type]
);

// 账号当前已有权限（列表接口每行自带 SHOW GRANTS 结果，解析后用于弹窗回显）
const currentGrants = ref<ParsedGrants | null>(null);

/** 当前层级/库/表范围内实际持有的权限名（大写；ALL 展开为该级全选）。
 * 库/表名大小写不敏感匹配：grant 语句中的写法可能与实际库名大小写不一致。 */
const heldPrivsForScope = computed<Set<string>>(() => {
  const held = new Set<string>();
  const grants = currentGrants.value;
  if (!grants) return held;
  const addAll = (names: string[]) => {
    for (const n of names) {
      if (n === "ALL" || n === "ALL PRIVILEGES") {
        for (const g of currentLevelMeta.value?.groups ?? []) {
          g.privs.forEach((p) => held.add(p.toUpperCase()));
        }
      } else {
        held.add(n.toUpperCase());
      }
    }
  };
  const dbEntry = (db: string) => {
    const key = Object.keys(grants.dbs).find(
      (k) => k.toLowerCase() === db.toLowerCase()
    );
    return key ? grants.dbs[key] : [];
  };
  const tbEntry = (db: string, tb: string) => {
    const key = Object.keys(grants.tables).find(
      (k) => k.toLowerCase() === `${db}.${tb}`.toLowerCase()
    );
    return key ? grants.tables[key] : [];
  };
  if (grantForm.priv_type === 0) {
    addAll(grants.global);
  } else if (grantForm.priv_type === 1) {
    const db = grantForm.db_name[0];
    if (db) addAll(dbEntry(db));
  } else if (grantForm.priv_type === 2) {
    const db = grantForm.db_name[0];
    const tb = grantForm.tb_name[0];
    if (db && tb) addAll(tbEntry(db, tb));
  }
  return held;
});

/** 权限在当前操作下是否可选：
 * 赋权时已持有的禁用（重复授权无意义）；回收时未持有的禁用（回收不存在的权限会报错）。
 * 列级不限制——勾选对象是"列"，已有列级授权在摘要中查看。 */
function isPrivEnabled(priv: string): boolean {
  if (grantForm.priv_type === 3) return true;
  const isHeld = heldPrivsForScope.value.has(priv.toUpperCase());
  return grantForm.op_type === 0 ? !isHeld : isHeld;
}

const privTreeData = computed(() => {
  const meta = currentLevelMeta.value;
  if (!meta) return [];
  return meta.groups.map((g) => ({
    id: `group-${meta.level}-${g.group}`,
    label: g.group,
    disabled: true,
    children: g.privs.map((p) => ({
      id: p,
      label: p,
      disabled: !isPrivEnabled(p),
    })),
  }));
});

const grantPermFilter = ref("");
const grantTreeRef = ref<any>(null);

function onGrantTreeCheck() {
  const checked = grantTreeRef.value?.getCheckedKeys(true) ?? [];
  grantForm.privs = checked.filter((k: unknown) => typeof k === "string") as string[];
}

/** 已选权限标签 */
const grantSelectedPermNames = computed(() => {
  const set = new Set(grantForm.privs);
  return (currentLevelMeta.value?.groups ?? []).flatMap((g) =>
    g.privs.filter((p) => set.has(p))
  );
});

function removeGrantPerm(p: string) {
  grantForm.privs = grantForm.privs.filter((x) => x !== p);
  grantTreeRef.value?.setCheckedKeys(grantForm.privs);
}

function selectAllGrantPerms() {
  // 全选仅作用于当前操作下可选项（禁用节点不参与）
  const all = (currentLevelMeta.value?.groups ?? [])
    .flatMap((g) => g.privs)
    .filter((p) => isPrivEnabled(p));
  grantForm.privs = [...new Set(all)];
  grantTreeRef.value?.setCheckedKeys(grantForm.privs);
}

function deselectAllGrantPerms() {
  grantForm.privs = [];
  grantTreeRef.value?.setCheckedKeys([]);
}

watch(grantPermFilter, (val) => {
  grantTreeRef.value?.filter(val);
});

function filterGrantPermNode(value: string, data: { label?: string }): boolean {
  if (!value) return true;
  return (data.label || "").toLowerCase().includes(value.toLowerCase());
}

/** 已有权限摘要（逐行可读文本） */
const grantSummaryLines = computed(() => {
  const g = currentGrants.value;
  if (!g) return [];
  const lines: string[] = [];
  if (g.global.length) lines.push(`全局：${g.global.join("、")}`);
  for (const [db, privs] of Object.entries(g.dbs)) {
    lines.push(`库 ${db}：${privs.join("、")}`);
  }
  for (const [tb, privs] of Object.entries(g.tables)) {
    lines.push(`表 ${tb}：${privs.join("、")}`);
  }
  for (const c of g.columns) lines.push(`列：${c}`);
  return lines;
});

/** 按操作类型回显勾选：回收预勾选实际持有的权限（未持有的已禁用）；
 * 赋权不预勾——已持有的已禁用，勾选只针对新增权限。
 * 列级的目标对象是"列"而非权限，不回显，仅在摘要中展示。 */
function syncGrantChecks() {
  const meta = currentLevelMeta.value;
  if (!meta || grantForm.priv_type === 3) return;
  let checked: string[] = [];
  if (grantForm.op_type === 1) {
    const held = heldPrivsForScope.value;
    checked = meta.groups.flatMap((g) => g.privs).filter((p) => held.has(p.toUpperCase()));
  }
  grantForm.privs = checked;
  grantTreeRef.value?.setCheckedKeys(checked);
}

// 操作类型/层级/库/表变化时重新计算禁用与回显；手动增删勾选不经过此逻辑。
// flush=post：权限树 data 变化重建节点会重置勾选态，须等重渲染完成后再回填
watch(
  () => [grantForm.op_type, grantForm.priv_type, grantForm.db_name, grantForm.tb_name],
  () => syncGrantChecks(),
  { flush: "post" }
);

async function openGrant(row: AccountRow) {
  if (!currentInstance.value) return;
  grantTarget.user_host = String(row.user_host || `${row.user}@${row.host}`);
  grantTarget.display = grantTarget.user_host;
  // 回显该账号已有权限（SHOW GRANTS 解析）与 Mongo 现有角色
  //（Mongo updateUser 语义是整体替换，不回显会导致提交时误清空全部角色）
  currentGrants.value = parseShowGrants(row.privileges);
  mongoGrantForm.db_name_user = String(row.db_name_user || "");
  mongoGrantForm.roles = Array.isArray(row.roles)
    ? (row.roles as string[]).map(String)
    : [];
  grantPermFilter.value = "";
  Object.assign(grantForm, {
    op_type: 0,
    priv_type: currentDbType.value === "mongo" ? 1 : 1,
    privs: [],
    db_name: [],
    tb_name: [],
    col_name: [],
  });
  grantDialog.value = true;
  if (currentDbType.value !== "mongo") {
    loadGrantDbs();
  }
  // 弹窗复用不销毁，清掉上一轮在权限树上的勾选残留（表单 privs 已重置）
  await nextTick();
  grantTreeRef.value?.setCheckedKeys(grantForm.privs);
}

// Mongo 授权（角色模型）
const mongoGrantForm = reactive({ db_name_user: "", roles: [] as string[] });

async function onMongoGrantSubmit() {
  if (!mongoGrantForm.db_name_user) return ElMessage.warning("缺少账号信息");
  if (!mongoGrantForm.roles.length) return ElMessage.warning("请选择数据库角色");
  grantSubmitting.value = true;
  try {
    await grantMongoAccount({
      instance_id: currentInstance.value!.id,
      db_name_user: mongoGrantForm.db_name_user,
      roles: mongoGrantForm.roles,
    });
    ElMessage.success("授权成功");
    grantDialog.value = false;
    loadData();
  } catch {
    // 拦截器已提示
  } finally {
    grantSubmitting.value = false;
  }
}

async function loadGrantDbs() {
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

watch(
  () => grantForm.db_name,
  async (dbs) => {
    tbOptions.value = [];
    colOptions.value = [];
    if (!currentInstance.value || !dbs.length) return;
    try {
      tbOptions.value = await fetchQueryResources({
        instance_id: currentInstance.value.id,
        resource_type: "table",
        db_name: dbs[0],
      });
    } catch {
      // 拦截器已提示
    }
  }
);

watch(
  () => grantForm.tb_name,
  async (tbs) => {
    colOptions.value = [];
    if (!currentInstance.value || !tbs.length || grantForm.priv_type !== 3) return;
    try {
      colOptions.value = await fetchQueryResources({
        instance_id: currentInstance.value.id,
        resource_type: "column",
        db_name: grantForm.db_name[0],
        tb_name: tbs[0],
      });
    } catch {
      // 拦截器已提示
    }
  }
);

async function onGrantSubmit() {
  if (!currentInstance.value) return;
  if (!grantForm.privs.length) return ElMessage.warning("请选择权限");
  if (grantForm.priv_type >= 1 && !grantForm.db_name.length)
    return ElMessage.warning("请选择库");
  if (grantForm.priv_type >= 2 && !grantForm.tb_name.length)
    return ElMessage.warning("请选择表");
  if (grantForm.priv_type === 3 && !grantForm.col_name.length)
    return ElMessage.warning("请选择列");
  grantSubmitting.value = true;
  try {
    await grantAccount({
      instance_id: currentInstance.value.id,
      user_host: grantTarget.user_host,
      op_type: grantForm.op_type,
      priv_type: grantForm.priv_type,
      privs: grantForm.privs,
      db_name: grantForm.db_name,
      tb_name: grantForm.tb_name,
      col_name: grantForm.col_name,
    });
    ElMessage.success(grantForm.op_type === 0 ? "授权成功" : "回收成功");
    grantDialog.value = false;
    loadData();
  } catch {
    // 业务错误已提示
  } finally {
    grantSubmitting.value = false;
  }
}

// ============ 改密 ============
const pwdDialog = ref(false);
const pwdTarget = reactive({ user_host: "" });
const pwdForm = reactive({ reset_pwd1: "", reset_pwd2: "" });
function openResetPwd(row: AccountRow) {
  pwdTarget.user_host = String(row.user_host || `${row.user}@${row.host}`);
  Object.assign(pwdForm, { reset_pwd1: "", reset_pwd2: "" });
  pwdDialog.value = true;
}
async function onPwdSubmit() {
  if (!currentInstance.value) return;
  if (pwdForm.reset_pwd1 !== pwdForm.reset_pwd2)
    return ElMessage.warning("两次密码不一致");
  if (!pwdForm.reset_pwd1) return ElMessage.warning("请输入新密码");
  try {
    await resetPwd({
      instance_id: currentInstance.value.id,
      user_host: pwdTarget.user_host,
      reset_pwd1: pwdForm.reset_pwd1,
      reset_pwd2: pwdForm.reset_pwd2,
    });
    ElMessage.success("改密成功");
    pwdDialog.value = false;
    loadData();
  } catch {
    // 业务错误已提示
  }
}

// ============ 锁定 ============
async function onToggleLock(row: AccountRow) {
  if (!currentInstance.value) return;
  const userHost = String(row.user_host || `${row.user}@${row.host}`);
  const isLocked = String(row.is_locked || "").toUpperCase() === "Y";
  const action = isLocked ? "解锁" : "锁定";
  try {
    await ElMessageBox.confirm(`确认${action}账号 ${userHost}？`, "提示", { type: "warning" });
    await lockAccount({
      instance_id: currentInstance.value.id,
      user_host: userHost,
      is_locked: isLocked ? "N" : "Y",
    });
    ElMessage.success(`${action}成功`);
    loadData();
  } catch (e) {
    if (e !== "cancel") {
      // 业务错误已提示
    }
  }
}

// ============ 删除 ============
async function onDelete(row: AccountRow) {
  if (!currentInstance.value) return;
  const userHost = String(row.user_host || `${row.user}@${row.host}`);
  try {
    await ElMessageBox.confirm(`确认删除账号 ${userHost}？此操作不可逆`, "危险操作", {
      type: "error",
      confirmButtonText: "删除",
      confirmButtonClass: "el-button--danger",
    });
    await deleteAccount({
      instance_id: currentInstance.value.id,
      user_host: userHost,
    });
    ElMessage.success("删除成功");
    loadData();
  } catch (e) {
    if (e !== "cancel") {
      // 业务错误已提示
    }
  }
}

// ============ 列表加载 ============
async function loadData() {
  if (!currentInstance.value) {
    list.value = [];
    return;
  }
  loading.value = true;
  try {
    list.value = await fetchUsers(currentInstance.value.id, savedOnly.value);
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false;
  }
}

watch(instanceName, loadData);
watch(savedOnly, loadData);

onMounted(() => loadInstances());
</script>

<template>
  <div class="account-page">
    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent>
        <el-form-item label="实例">
          <el-select v-model="instanceName" filterable placeholder="选择实例" style="width: 240px">
            <el-option-group v-for="grp in instanceGroups" :key="grp.label" :label="grp.label">
              <el-option v-for="i in grp.items" :key="i.id" :label="i.instance_name" :value="i.instance_name" />
            </el-option-group>
          </el-select>
        </el-form-item>
        <el-form-item label="过滤">
          <el-select v-model="savedOnly" style="width: 140px">
            <el-option :value="0" label="全部" />
            <el-option :value="1" label="仅已录入" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button
            v-if="canManage"
            type="primary"
            :disabled="!currentInstance"
            @click="openCreate"
          >
            创建账号
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <el-table v-loading="loading" :data="list" stripe border style="width: 100%">
        <el-table-column prop="user" label="用户" min-width="140" show-overflow-tooltip />
        <el-table-column prop="host" label="Host" min-width="140" show-overflow-tooltip />
        <el-table-column v-if="currentDbType === 'mongo'" prop="db_name" label="库" min-width="120" show-overflow-tooltip />
        <el-table-column label="锁定" width="80">
          <template #default="{ row }">
            <el-tag
              :type="String((row as AccountRow).is_locked || '').toUpperCase() === 'Y' ? 'danger' : 'success'"
              size="small"
            >
              {{ String((row as AccountRow).is_locked || "").toUpperCase() === "Y" ? "已锁" : "正常" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="remark" label="备注" min-width="160" show-overflow-tooltip />
        <el-table-column v-if="canManage" label="操作" width="320" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row as AccountRow)">编辑</el-button>
            <el-button link type="primary" size="small" @click="openGrant(row as AccountRow)">授权</el-button>
            <el-button link type="primary" size="small" @click="openResetPwd(row as AccountRow)">改密</el-button>
            <el-button
              v-if="currentDbType === 'mysql'"
              link
              :type="String((row as AccountRow).is_locked || '').toUpperCase() === 'Y' ? 'success' : 'warning'"
              size="small"
              @click="onToggleLock(row as AccountRow)"
            >
              {{ String((row as AccountRow).is_locked || "").toUpperCase() === "Y" ? "解锁" : "锁定" }}
            </el-button>
            <el-button link type="danger" size="small" @click="onDelete(row as AccountRow)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 创建/编辑 -->
    <el-dialog
      v-model="formDialog"
      :title="formMode === 'create' ? '创建账号' : '编辑账号'"
      width="460px"
    >
      <el-form label-width="90px">
        <el-form-item label="用户名" required>
          <el-input v-model="form.user" :disabled="formMode === 'edit'" />
        </el-form-item>
        <el-form-item label="Host">
          <el-input v-model="form.host" placeholder="如 %" />
        </el-form-item>
        <template v-if="formMode === 'create'">
          <el-form-item label="密码" required>
            <el-input v-model="form.password1" type="password" show-password />
          </el-form-item>
          <el-form-item label="确认密码" required>
            <el-input v-model="form.password2" type="password" show-password />
          </el-form-item>
        </template>
        <el-form-item v-else label="新密码">
          <el-input v-model="form.password" type="password" show-password placeholder="留空则不改" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.remark" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formDialog = false">取消</el-button>
        <el-button type="primary" @click="onFormSubmit">确定</el-button>
      </template>
    </el-dialog>

    <!-- 授权（el-tree 选择） -->
    <el-dialog
      v-model="grantDialog"
      :title="`授权 - ${grantTarget.display}`"
      width="780px"
      top="5vh"
      @closed="grantTreeRef = null"
    >
      <el-form label-width="80px" v-if="currentDbType !== 'mongo'">
        <el-form-item label="操作">
          <el-radio-group v-model="grantForm.op_type">
            <el-radio :value="0">赋权</el-radio>
            <el-radio :value="1">回收</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="已有权限">
          <div class="current-grants">
            <template v-if="grantSummaryLines.length">
              <div v-for="line in grantSummaryLines" :key="line">{{ line }}</div>
            </template>
            <span v-else class="muted">无（该账号当前未授予任何权限）</span>
          </div>
        </el-form-item>
        <el-form-item label="权限范围">
          <el-radio-group v-model="grantForm.priv_type">
            <el-radio v-for="(meta, idx) in MYSQL_PRIVILEGES" :key="meta.level" :value="idx">
              {{ PRIV_LEVEL_LABEL[meta.level] }}
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="grantForm.priv_type >= 1" label="库">
          <el-select
            v-model="grantForm.db_name"
            filterable
            multiple
            placeholder="选择库"
            style="width: 100%"
          >
            <el-option v-for="d in dbOptions" :key="d" :label="d" :value="d" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="grantForm.priv_type >= 2" label="表">
          <el-select
            v-model="grantForm.tb_name"
            filterable
            multiple
            placeholder="选择表"
            style="width: 100%"
          >
            <el-option v-for="t in tbOptions" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="grantForm.priv_type === 3" label="列">
          <el-select
            v-model="grantForm.col_name"
            filterable
            multiple
            placeholder="选择列"
            style="width: 100%"
          >
            <el-option v-for="c in colOptions" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>

        <!-- 权限树选择 -->
        <el-form-item label="权限">
          <div class="perm-selector">
            <div
              v-if="grantForm.priv_type >= 1 && !grantForm.db_name.length"
              class="scope-hint"
            >
              先选择{{ grantForm.priv_type >= 2 ? "库和表" : "库" }}，下方权限将按该范围
              <b>已有权限</b> 置灰与回显（赋权=已有的不可选，回收=只可选已有的）
            </div>
            <div class="perm-tags">
              <el-tag
                v-for="p in grantSelectedPermNames"
                :key="p"
                closable
                size="small"
                type="info"
                class="perm-tag"
                @close="removeGrantPerm(p)"
              >
                {{ p }}
              </el-tag>
              <span v-if="!grantSelectedPermNames.length" class="muted">未选择权限</span>
            </div>
            <div class="perm-actions">
              <el-button size="small" @click="selectAllGrantPerms">全选</el-button>
              <el-button size="small" @click="deselectAllGrantPerms">全不选</el-button>
              <el-input
                v-model="grantPermFilter"
                placeholder="搜索权限..."
                clearable
                size="small"
                style="width: 200px; margin-left: auto"
              />
            </div>
            <el-tree
              ref="grantTreeRef"
              :data="privTreeData"
              show-checkbox
              node-key="id"
              :default-expanded-keys="privTreeData.map((g) => g.id)"
              :filter-node-method="filterGrantPermNode"
              :default-checked-keys="grantForm.privs"
              @check="onGrantTreeCheck"
            >
              <template #default="{ data }">
                <span
                  v-if="String(data.id).startsWith('group-')"
                  class="priv-group"
                >{{ data.label }}</span>
                <span
                  v-else
                  class="priv-node"
                  :class="{ 'is-unavailable': data.disabled }"
                >
                  {{ data.label }}
                  <el-tag
                    v-if="data.disabled"
                    size="small"
                    effect="plain"
                    :type="grantForm.op_type === 0 ? 'success' : 'info'"
                    class="priv-flag"
                  >
                    {{ grantForm.op_type === 0 ? "已持有" : "未持有" }}
                  </el-tag>
                </span>
              </template>
            </el-tree>
          </div>
        </el-form-item>
      </el-form>

      <!-- Mongo 授权 -->
      <template v-if="currentDbType === 'mongo'">
        <el-alert
          type="info"
          :closable="false"
          :title="`Mongo 账号：${mongoGrantForm.db_name_user}`"
          style="margin-bottom: 12px"
        />
        <el-form label-width="80px">
          <el-form-item label="角色" required>
            <el-select
              v-model="mongoGrantForm.roles"
              multiple
              filterable
              placeholder="请选择数据库角色"
              style="width: 100%"
            >
              <el-option v-for="r in MONGO_ROLES" :key="r" :label="r" :value="r" />
            </el-select>
          </el-form-item>
        </el-form>
      </template>

      <template #footer>
        <el-button @click="grantDialog = false">取消</el-button>
        <el-button
          v-if="currentDbType !== 'mongo'"
          type="primary"
          :loading="grantSubmitting"
          @click="onGrantSubmit"
        >
          确定
        </el-button>
        <el-button
          v-else
          type="primary"
          :loading="grantSubmitting"
          @click="onMongoGrantSubmit"
        >
          确定
        </el-button>
      </template>
    </el-dialog>

    <!-- 改密 -->
    <el-dialog v-model="pwdDialog" :title="`改密 - ${pwdTarget.user_host}`" width="420px">
      <el-form label-width="90px">
        <el-form-item label="新密码" required>
          <el-input v-model="pwdForm.reset_pwd1" type="password" show-password />
        </el-form-item>
        <el-form-item label="确认密码" required>
          <el-input v-model="pwdForm.reset_pwd2" type="password" show-password />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdDialog = false">取消</el-button>
        <el-button type="primary" @click="onPwdSubmit">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.account-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.filter-card :deep(.el-form-item) {
  margin-bottom: 0;
}

.perm-selector {
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  padding: 12px;
  max-height: 460px;
  overflow-y: auto;
  width: 100%;

  .scope-hint {
    margin-bottom: 10px;
    padding: 6px 10px;
    background: var(--el-fill-color-light);
    border-radius: 4px;
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }
}

.perm-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;

  .perm-tag { cursor: pointer; }

  .muted {
    color: var(--el-text-color-placeholder);
    font-size: 13px;
  }
}

.perm-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  align-items: center;
}

.priv-group {
  font-weight: 600;
  color: var(--el-text-color-regular);
  font-size: 13px;
}

// 不可操作的权限：置灰 + 删除线 + 原因标签，与可勾选项拉开视觉差
.priv-node {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;

  &.is-unavailable {
    color: var(--el-text-color-placeholder);
    text-decoration: line-through;
  }

  .priv-flag {
    transform: scale(0.85);
    text-decoration: none;
  }
}

.current-grants {
  width: 100%;
  padding: 8px 12px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  font-size: 13px;
  line-height: 1.9;
  word-break: break-all;

  .muted {
    color: var(--el-text-color-placeholder);
  }
}
</style>
