<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { ElMessage } from "element-plus";
import { useAuthStore } from "@/stores/auth";
import { fetchConfig, saveConfig, checkGoInception, checkAIConnection } from "@/api/config";
import {
  CONFIG_SECTIONS,
  CONFIG_SUBSECTIONS,
  CONFIG_FIELDS,
  type ConfigField,
} from "@/config/configSchema";
import { fetchInstanceTags, type InstanceTagRow } from "@/api/instance";
import { fetchGroups, type AuthGroupRow } from "@/api/user";
import { fetchResourceGroups, type ResourceGroupRow } from "@/api/group";

const auth = useAuthStore();

const loading = ref(false);
const saving = ref(false);
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const configValues = ref<Record<string, any>>({});

// 分区图标（Element Plus 图标名，auto-import 生效）
const SECTION_ICONS: Record<string, string> = {
  "goInception 配置": "Monitor",
  "功能模块配置": "Setting",
  "通知配置": "Bell",
  "AI 配置": "MagicStick",
  "其他配置": "More",
};

// 动态选项数据
const dynamicOptions = ref<Record<string, { value: string; label: string }[]>>({});

function getOptions(field: ConfigField): { value: string; label: string }[] {
  if (field.options) return field.options;
  if (field.dynamic) return dynamicOptions.value[field.dynamic] || [];
  return [];
}

// 条件显隐判断
function isVisible(field: ConfigField): boolean {
  if (!field.showWhen) return true;
  const val = configValues.value[field.showWhen.key];
  if (field.showWhen.value === true) return val === true || val === "true";
  if (field.showWhen.value === false) return val === false || val === "false";
  return String(val ?? "") === String(field.showWhen.value);
}

// 按分区/子分区组织字段
interface FieldGroup {
  key: string;
  fields: ConfigField[];
}

const sections = computed(() => {
  const result: { name: string; groups: FieldGroup[] }[] = [];
  for (const sectionName of CONFIG_SECTIONS) {
    const sectionFields = CONFIG_FIELDS.filter((f) => f.section === sectionName);
    if (!sectionFields.length) continue;
    const subs = CONFIG_SUBSECTIONS[sectionName];
    const groups: FieldGroup[] = [];
    if (subs && subs.length) {
      for (const sub of subs) {
        const subFields = sectionFields.filter((f) => f.subsection === sub);
        if (subFields.length) groups.push({ key: sub, fields: subFields });
      }
      const leftover = sectionFields.filter((f) => !f.subsection && !subs.includes(f.subsection || ""));
      if (leftover.length) groups.push({ key: "_", fields: leftover });
    } else {
      groups.push({ key: sectionName, fields: sectionFields });
    }
    result.push({ name: sectionName, groups });
  }
  return result;
});

// 统计每个分区可见字段数
function sectionFieldCount(sectionName: string): number {
  return CONFIG_FIELDS.filter((f) => f.section === sectionName).length;
}

// 多选字段
function isMultiselectArray(field: ConfigField): boolean {
  return field.type === "multiselect";
}

async function loadConfig() {
  loading.value = true;
  try {
    const data = await fetchConfig();
    const values: Record<string, any> = {};
    for (const f of CONFIG_FIELDS) {
      const raw = data[f.key];
      if (f.type === "timerange") {
        const parts = typeof raw === "string" ? raw.split("-") : [];
        values[f.key] =
          parts.length === 2 && parts[0].trim() && parts[1].trim()
            ? [parts[0].trim(), parts[1].trim()]
            : null;
      } else if (isMultiselectArray(f)) {
        values[f.key] = typeof raw === "string" && raw ? raw.split(",") : Array.isArray(raw) ? raw : [];
      } else {
        values[f.key] = raw ?? (f.type === "boolean" ? false : f.type === "number" ? "" : "");
      }
    }
    configValues.value = values;
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false;
  }
}

async function loadDynamicOptions() {
  try {
    // fetchGroups 走 /api/v1/user/group/（超管专用），非超管直接跳过避免 403
    const groupsPromise = auth.isSuperuser
      ? fetchGroups().catch(() => [])
      : Promise.resolve([] as AuthGroupRow[]);
    const [tags, groups, resGroups] = await Promise.all([
      fetchInstanceTags().catch(() => []),
      groupsPromise,
      fetchResourceGroups({ size: 1000 }).then((r) => r.data.results || []).catch(() => []),
    ]);
    dynamicOptions.value = {
      tags: (tags as InstanceTagRow[]).map((t) => ({ value: t.tag_code, label: `${t.tag_name}(${t.tag_code})` })),
      groups: (groups as AuthGroupRow[]).map((g) => ({ value: String(g.id), label: g.name })),
      resourceGroups: (resGroups as ResourceGroupRow[]).map((g) => ({
        value: String(g.group_id),
        label: g.group_name,
      })),
    };
  } catch {
    // 拦截器已提示
  }
}

async function onSave() {
  saving.value = true;
  try {
    // timerange 字段在前端是 [start, end]，落库统一为 "HH:mm-HH:mm"
    const payload: Record<string, unknown> = { ...configValues.value };
    for (const f of CONFIG_FIELDS) {
      if (f.type !== "timerange") continue;
      const range = payload[f.key];
      if (Array.isArray(range) && range.length === 2 && range[0] && range[0] === range[1]) {
        ElMessage.error(`${f.label}的起止时间不能相同`);
        return;
      }
      payload[f.key] =
        Array.isArray(range) && range.length === 2 && range[0] && range[1]
          ? `${range[0]}-${range[1]}`
          : "";
    }
    await saveConfig(payload);
    ElMessage.success("配置已保存");
  } catch (e) {
    ElMessage.error(`保存失败：${(e as Error).message}`);
  } finally {
    saving.value = false;
  }
}

const checkingGoInception = ref(false);

async function onCheckGoInception() {
  checkingGoInception.value = true;
  try {
    await checkGoInception({
      go_inception_host: String(configValues.value.go_inception_host ?? ""),
      go_inception_port: String(configValues.value.go_inception_port ?? ""),
      go_inception_user: String(configValues.value.go_inception_user ?? ""),
      go_inception_password: String(configValues.value.go_inception_password ?? ""),
      inception_remote_backup_host: String(configValues.value.inception_remote_backup_host ?? ""),
      inception_remote_backup_port: String(configValues.value.inception_remote_backup_port ?? ""),
      inception_remote_backup_user: String(configValues.value.inception_remote_backup_user ?? ""),
      inception_remote_backup_password: String(configValues.value.inception_remote_backup_password ?? ""),
    });
    ElMessage.success("goInception 和备份库测试连接成功，请点击保存按钮生效！");
  } catch (e) {
    ElMessage.error(`连接失败：${(e as Error).message}`);
  } finally {
    checkingGoInception.value = false;
  }
}

const checkingAI = ref(false);

async function onCheckAI() {
  checkingAI.value = true;
  try {
    await checkAIConnection({
      openai_base_url: String(configValues.value.openai_base_url ?? ""),
      openai_api_key: String(configValues.value.openai_api_key ?? ""),
      default_chat_model: String(configValues.value.default_chat_model ?? ""),
    });
    ElMessage.success("AI 服务连接成功，请点击保存按钮生效！");
  } catch (e) {
    ElMessage.error(`连接失败：${(e as Error).message}`);
  } finally {
    checkingAI.value = false;
  }
}

// 默认展开第一个分区
const activeSections = ref<string[]>([CONFIG_SECTIONS[0]]);

onMounted(() => {
  loadConfig();
  loadDynamicOptions();
});
</script>

<template>
  <div v-loading="loading" class="config-page">
    <!-- 顶部工具栏 -->
    <el-affix :offset="60">
      <div class="config-toolbar">
        <div class="toolbar-left">
          <span class="toolbar-title">系统配置项管理</span>
          <el-tag size="small" type="info" round>{{ CONFIG_FIELDS.length }} 项</el-tag>
        </div>
        <el-button type="primary" :loading="saving" @click="onSave" round>
          保存全部配置
        </el-button>
      </div>
    </el-affix>

    <el-form label-width="140px" label-position="right" class="config-form">
      <el-collapse v-model="activeSections">
        <el-collapse-item
          v-for="section in sections"
          :key="section.name"
          :name="section.name"
        >
          <template #title>
            <div class="section-header">
              <el-icon class="section-icon"><component :is="SECTION_ICONS[section.name] || 'Folder'" /></el-icon>
              <span class="section-name">{{ section.name }}</span>
              <el-tag size="small" effect="plain">{{ sectionFieldCount(section.name) }}</el-tag>
            </div>
          </template>

          <template v-for="group in section.groups" :key="group.key">
            <!-- 子分区标题 -->
            <div v-if="group.key !== section.name && group.key !== '_'" class="subsection-title">
              <span class="sub-dot"></span>
              {{ group.key }}
            </div>

            <!-- 字段列表（双列网格） -->
            <el-row :gutter="16">
              <el-col
                v-for="field in group.fields.filter(isVisible)"
                :key="field.key"
                :span="12"
              >
                <el-form-item :label="field.label">
                  <div class="field-row">
                    <!-- boolean -->
                    <el-switch
                      v-if="field.type === 'boolean'"
                      v-model="configValues[field.key]"
                      inline-prompt
                      active-text="开"
                      inactive-text="关"
                    />
                    <!-- number -->
                    <el-input-number
                      v-else-if="field.type === 'number'"
                      v-model="configValues[field.key]"
                      controls-position="right"
                      class="field-input"
                    />
                    <!-- password -->
                    <el-input
                      v-else-if="field.type === 'password'"
                      v-model="configValues[field.key]"
                      type="password"
                      show-password
                      :placeholder="field.desc || ''"
                      class="field-input"
                    />
                    <!-- select -->
                    <el-select
                      v-else-if="field.type === 'select'"
                      v-model="configValues[field.key]"
                      filterable
                      clearable
                      class="field-input"
                    >
                      <el-option
                        v-for="o in getOptions(field)"
                        :key="o.value"
                        :label="o.label"
                        :value="o.value"
                      />
                    </el-select>
                    <!-- multiselect -->
                    <el-select
                      v-else-if="field.type === 'multiselect'"
                      v-model="configValues[field.key]"
                      multiple
                      filterable
                      collapse-tags
                      collapse-tags-tooltip
                      class="field-input"
                    >
                      <el-option
                        v-for="o in getOptions(field)"
                        :key="o.value"
                        :label="o.label"
                        :value="o.value"
                      />
                    </el-select>
                    <!-- timerange -->
                    <el-time-picker
                      v-else-if="field.type === 'timerange'"
                      v-model="configValues[field.key]"
                      is-range
                      format="HH:mm"
                      value-format="HH:mm"
                      range-separator="至"
                      start-placeholder="开始时间"
                      end-placeholder="结束时间"
                      class="field-input field-time-range"
                    />
                    <!-- text (default) -->
                    <el-input
                      v-else
                      v-model="configValues[field.key]"
                      :placeholder="field.desc || ''"
                      class="field-input"
                    />
                    <span v-if="field.type === 'timerange'" class="field-hint">留空为全天</span>
                    <!-- 描述 tooltip -->
                    <el-tooltip
                      v-if="field.desc"
                      :content="field.desc"
                      placement="top"
                    >
                      <el-icon class="field-help"><QuestionFilled /></el-icon>
                    </el-tooltip>
                  </div>
                </el-form-item>
              </el-col>
            </el-row>
            <!-- goInception 分区测试连接按钮 -->
            <div v-if="section.name === 'goInception 配置'" class="check-row">
              <el-button
                type="warning"
                :loading="checkingGoInception"
                @click="onCheckGoInception"
              >
                测试连接
              </el-button>
              <span class="field-hint">测试 goInception 和备份库连接是否正常</span>
            </div>
            <!-- AI 配置分区测试连接按钮 -->
            <div v-if="section.name === 'AI 配置'" class="check-row">
              <el-button
                type="success"
                :loading="checkingAI"
                @click="onCheckAI"
              >
                测试连接
              </el-button>
              <span class="field-hint">测试 AI 服务（地址 / API Key / 模型）是否可用</span>
            </div>
          </template>
        </el-collapse-item>
      </el-collapse>
    </el-form>
  </div>
</template>

<style scoped lang="scss">
.config-page {
  .config-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 20px;
    margin-bottom: 16px;
    background: var(--el-bg-color);
    border: 1px solid var(--el-border-color-light);
    border-radius: 8px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.04);
  }

  .toolbar-left {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .toolbar-title {
    font-size: 16px;
    font-weight: 600;
  }

  // 分区折叠面板
  :deep(.el-collapse) {
    border: none;
  }

  :deep(.el-collapse-item__header) {
    height: 48px;
    padding: 0 16px;
    border: 1px solid var(--el-border-color-light);
    border-radius: 8px;
    margin-bottom: 4px;
    font-size: 15px;
    font-weight: 600;
    transition: all 0.2s;

    &:hover {
      border-color: var(--el-color-primary-light-5);
      background: var(--el-color-primary-light-9);
    }

    &.is-active {
      border-bottom-left-radius: 0;
      border-bottom-right-radius: 0;
      margin-bottom: 0;
    }
  }

  :deep(.el-collapse-item__wrap) {
    border: 1px solid var(--el-border-color-light);
    border-top: none;
    border-radius: 0 0 8px 8px;
    margin-bottom: 12px;
  }

  :deep(.el-collapse-item__content) {
    padding: 16px 20px 4px;
  }

  .section-header {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
  }

  .section-icon {
    font-size: 18px;
  }

  .section-name {
    flex: 1;
  }

  // 子分区标题
  .subsection-title {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 8px 0 12px;
    padding-bottom: 6px;
    font-weight: 600;
    font-size: 13px;
    color: var(--el-text-color-secondary);
    border-bottom: 1px dashed var(--el-border-color-lighter);

    &:first-child {
      margin-top: 0;
    }
  }

  .sub-dot {
    width: 4px;
    height: 14px;
    border-radius: 2px;
    background: var(--el-color-primary);
  }

  // 字段行
  .field-row {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
  }

  .field-input {
    flex: 1;
    max-width: 320px;
  }

  .field-time-range {
    max-width: 360px;
  }

  .field-help {
    color: var(--el-text-color-placeholder);
    cursor: help;
    font-size: 15px;
    flex-shrink: 0;

    &:hover {
      color: var(--el-color-primary);
    }
  }

  .check-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 8px 0 4px;
  }

  .field-hint {
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }

  // 表单项
  :deep(.el-form-item) {
    margin-bottom: 14px;
  }

  :deep(.el-form-item__label) {
    font-size: 13px;
  }
}
</style>
