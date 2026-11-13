<script setup lang="ts">
import { ref, reactive, onMounted } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { useAuthStore } from "@/stores/auth";
import { fetchUserInstances } from "@/api/group";
import {
  fetchWorkflowDetail,
  type ReviewRow,
  type SqlWorkflowDetail,
} from "@/api/sqlworkflow";
import { parseReviewRows } from "@/utils/reviewRows";
import {
  fetchTodoList,
  auditTodo,
  WORKFLOW_TYPE,
  AUDIT_STATUS,
  type TodoRow,
} from "@/api/todoworkflow";

const auth = useAuthStore();
const router = useRouter();

const loading = ref(false);
const list = ref<TodoRow[]>([]);
const total = ref(0);

// 展开行详情（懒加载）：避免审批人为了看一眼 SQL 就跳转详情页
interface TodoPreview {
  detail: SqlWorkflowDetail;
  rows: ReviewRow[];
}
const previews = reactive<Record<number, TodoPreview | null>>({});
const previewLoading = reactive<Record<number, boolean>>({});
const instanceMap = ref<Record<number, string>>({});

const query = reactive({
  page: 1,
  size: 20,
});

async function loadInstances() {
  try {
    const rows = await fetchUserInstances();
    instanceMap.value = Object.fromEntries(
      rows.map((i) => [i.id, i.instance_name])
    );
  } catch {
    // 错误提示已由 request 拦截器处理
  }
}

async function loadPreview(row: TodoRow) {
  const id = row.workflow_id;
  if (previews[id] || previewLoading[id]) return;
  previewLoading[id] = true;
  try {
    const { data } = await fetchWorkflowDetail(id);
    previews[id] = {
      detail: data,
      rows: parseReviewRows(data.review_content),
    };
  } catch {
    previews[id] = null;
  } finally {
    previewLoading[id] = false;
  }
}

function onExpandChange(row: TodoRow, expanded: TodoRow[] | boolean) {
  // 不同 Element Plus 版本/表格模式分别传 expandedRows 或 boolean
  const isExpanded = Array.isArray(expanded)
    ? expanded.some((r) => r.workflow_id === row.workflow_id)
    : expanded;
  // 只有 SQL 上线工单有统一的 review_content 预览结构
  if (isExpanded && row.workflow_type === 2) loadPreview(row);
}

function rowsOf(id: number): ReviewRow[] {
  return previews[id]?.rows ?? [];
}

function detailOf(id: number): SqlWorkflowDetail | null {
  return previews[id]?.detail ?? null;
}

function previewInstance(id: number): string {
  const instanceId = detailOf(id)?.workflow.instance;
  if (!instanceId) return "-";
  return instanceMap.value[instanceId as number] || String(instanceId);
}

function previewRunDate(id: number): string {
  const w = detailOf(id)?.workflow;
  if (!w) return "-";
  const start = (w.run_date_start as string) || "无限制";
  const end = (w.run_date_end as string) || "无限制";
  return `${start} ~ ${end}`;
}

function riskTagType(
  level?: string
): "danger" | "warning" | "success" | "info" {
  switch (level) {
    case "high":
      return "danger";
    case "medium":
      return "warning";
    case "low":
      return "success";
    default:
      return "info";
  }
}

function riskText(level?: string): string {
  switch (level) {
    case "high":
      return "高风险";
    case "medium":
      return "中风险";
    case "low":
      return "低风险";
    default:
      return "未评估";
  }
}

async function loadData() {
  loading.value = true;
  try {
    const { data } = await fetchTodoList({
      page: query.page,
      size: query.size,
      engineer: auth.user?.username,
    });
    list.value = data.results || [];
    total.value = data.count || 0;
  } catch {
    // 错误提示已由 request 拦截器处理
  } finally {
    loading.value = false;
  }
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

/** 按 workflow_type 跳转对应详情页 */
function goDetail(row: TodoRow) {
  const id = row.workflow_id;
  if (row.workflow_type === 2) {
    router.push({ name: "sqlworkflow-detail", params: { id } });
  } else if (row.workflow_type === 1) {
    router.push({ name: "queryapply-detail", params: { id } });
  } else if (row.workflow_type === 3) {
    router.push({ name: "archive-detail", params: { id } });
  }
}

// ============ 快捷审批 ============
const auditVisible = ref(false);
const auditLoading = ref(false);
const auditForm = reactive({
  audit_id: 0,
  workflow_id: 0,
  workflow_type: 0,
  workflow_title: "",
  audit_type: "pass" as "pass" | "cancel",
  audit_remark: "",
});

function openAudit(row: TodoRow, type: "pass" | "cancel") {
  auditForm.audit_id = row.audit_id;
  auditForm.workflow_id = row.workflow_id;
  auditForm.workflow_type = row.workflow_type;
  auditForm.workflow_title = row.workflow_title;
  auditForm.audit_type = type;
  // 通过默认「同意」，减少重复输入；驳回必须说明原因
  auditForm.audit_remark = type === "pass" ? "同意" : "";
  auditVisible.value = true;
}

async function onAudit() {
  if (auditForm.audit_type === "cancel" && !auditForm.audit_remark.trim()) {
    return ElMessage.warning("请填写驳回原因");
  }
  if (!auditForm.audit_remark.trim()) auditForm.audit_remark = "同意";
  auditLoading.value = true;
  try {
    await auditTodo({
      engineer: auth.user?.username || "",
      workflow_id: auditForm.workflow_id,
      workflow_type: auditForm.workflow_type,
      audit_type: auditForm.audit_type,
      audit_remark: auditForm.audit_remark,
    });
    ElMessage.success(auditForm.audit_type === "pass" ? "已通过" : "已驳回");
    auditVisible.value = false;
    delete previews[auditForm.workflow_id];
    loadData();
  } catch {
    // 错误提示已由 request 拦截器处理
  } finally {
    auditLoading.value = false;
  }
}

onMounted(() => {
  loadData();
  loadInstances();
});
</script>

<template>
  <div class="todoworkflow-page">
    <el-card shadow="never">
      <template #header>
        待办工作流
        <span class="header-hint">展开行可查看 SQL 预览并直接审批</span>
      </template>
      <el-table
        v-loading="loading"
        :data="list"
        stripe
        border
        style="width: 100%"
        @expand-change="onExpandChange"
      >
        <el-table-column type="expand" width="44">
          <template #default="{ row }">
            <div class="todo-preview">
              <template v-if="(row as TodoRow).workflow_type !== 2">
                <span class="preview-unsupported-text">
                  该类型工单请打开详情查看并审批。
                </span>
                <el-button
                  size="small"
                  link
                  type="primary"
                  @click="goDetail(row as TodoRow)"
                >
                  打开详情
                </el-button>
              </template>
              <div
                v-else-if="previewLoading[(row as TodoRow).workflow_id]"
                v-loading="true"
                class="preview-loading"
              />
              <template v-else-if="detailOf((row as TodoRow).workflow_id)">
                <el-descriptions
                  :column="3"
                  size="small"
                  border
                  class="preview-meta"
                >
                  <el-descriptions-item label="实例">
                    {{ previewInstance((row as TodoRow).workflow_id) }}
                  </el-descriptions-item>
                  <el-descriptions-item label="数据库">
                    {{
                      detailOf((row as TodoRow).workflow_id)?.workflow.db_name
                    }}
                  </el-descriptions-item>
                  <el-descriptions-item label="发起人">
                    {{
                      detailOf((row as TodoRow).workflow_id)?.workflow
                        .engineer_display
                    }}
                  </el-descriptions-item>
                  <el-descriptions-item label="AI 风险">
                    <el-tag
                      :type="
                        riskTagType(
                          detailOf((row as TodoRow).workflow_id)
                            ?.ai_max_risk_level as string
                        )
                      "
                      size="small"
                    >
                      {{
                        riskText(
                          detailOf((row as TodoRow).workflow_id)
                            ?.ai_max_risk_level as string
                        )
                      }}
                      <template
                        v-if="
                          detailOf((row as TodoRow).workflow_id)
                            ?.ai_max_risk_score
                        "
                      >
                        ·
                        {{
                          detailOf((row as TodoRow).workflow_id)
                            ?.ai_max_risk_score
                        }}/100
                      </template>
                    </el-tag>
                  </el-descriptions-item>
                  <el-descriptions-item label="SQL 条数">
                    {{ rowsOf((row as TodoRow).workflow_id).length }}
                  </el-descriptions-item>
                  <el-descriptions-item label="可执行时间">
                    {{ previewRunDate((row as TodoRow).workflow_id) }}
                  </el-descriptions-item>
                </el-descriptions>

                <div class="preview-sql-list">
                  <div
                    v-for="(r, idx) in rowsOf((row as TodoRow).workflow_id).slice(
                      0,
                      3
                    )"
                    :key="idx"
                    class="preview-sql-item"
                  >
                    <div class="preview-sql-head">
                      <span class="preview-sql-index">#{{ idx + 1 }}</span>
                      <el-tag
                        v-if="Number(r.errlevel) === 2"
                        type="danger"
                        size="small"
                      >
                        错误
                      </el-tag>
                      <el-tag
                        v-else-if="Number(r.errlevel) === 1"
                        type="warning"
                        size="small"
                      >
                        警告
                      </el-tag>
                    </div>
                    <pre>{{ r.sql }}</pre>
                    <div v-if="r.errormessage" class="preview-sql-error">
                      {{ r.errormessage }}
                    </div>
                  </div>
                  <div
                    v-if="rowsOf((row as TodoRow).workflow_id).length > 3"
                    class="preview-more"
                  >
                    还有
                    {{ rowsOf((row as TodoRow).workflow_id).length - 3 }}
                    条，打开详情查看
                  </div>
                  <el-empty
                    v-if="!rowsOf((row as TodoRow).workflow_id).length"
                    description="暂无检测结果"
                    :image-size="48"
                  />
                </div>

                <div class="preview-actions">
                  <el-button
                    size="small"
                    type="success"
                    @click="openAudit(row as TodoRow, 'pass')"
                  >
                    通过
                  </el-button>
                  <el-button
                    size="small"
                    type="danger"
                    @click="openAudit(row as TodoRow, 'cancel')"
                  >
                    驳回
                  </el-button>
                  <el-button
                    size="small"
                    link
                    type="primary"
                    @click="goDetail(row as TodoRow)"
                  >
                    打开详情
                  </el-button>
                </div>
              </template>
              <el-empty v-else description="加载详情失败" :image-size="60" />
            </div>
          </template>
        </el-table-column>
        <el-table-column type="index" label="#" width="55" />
        <el-table-column label="工单标题" min-width="200">
          <template #default="{ row }">
            <el-link type="primary" @click="goDetail(row as TodoRow)">
              {{ (row as TodoRow).workflow_title }}
            </el-link>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <el-tag
              v-if="WORKFLOW_TYPE[(row as TodoRow).workflow_type]"
              :type="WORKFLOW_TYPE[(row as TodoRow).workflow_type].type"
              size="small"
            >
              {{ WORKFLOW_TYPE[(row as TodoRow).workflow_type].label }}
            </el-tag>
            <span v-else>{{ (row as TodoRow).workflow_type }}</span>
          </template>
        </el-table-column>
        <el-table-column label="资源组" prop="group_name" width="130" show-overflow-tooltip />
        <el-table-column label="申请人" prop="create_user_display" width="120" show-overflow-tooltip />
        <el-table-column label="当前审批组" prop="current_audit" width="110" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag
              v-if="AUDIT_STATUS[(row as TodoRow).current_status]"
              :type="AUDIT_STATUS[(row as TodoRow).current_status].type"
              size="small"
            >
              {{ AUDIT_STATUS[(row as TodoRow).current_status].label }}
            </el-tag>
            <span v-else>{{ (row as TodoRow).current_status }}</span>
          </template>
        </el-table-column>
        <el-table-column label="申请时间" prop="create_time" width="160" />
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="success" @click="openAudit(row as TodoRow, 'pass')">通过</el-button>
            <el-button link type="danger" @click="openAudit(row as TodoRow, 'cancel')">驳回</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          :current-page="query.page"
          :page-size="query.size"
          :page-sizes="[20, 30, 50, 100]"
          :total="total"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </div>
    </el-card>

    <!-- 审批弹窗 -->
    <el-dialog
      v-model="auditVisible"
      :title="`${auditForm.audit_type === 'pass' ? '通过' : '驳回'} - ${auditForm.workflow_title}`"
      width="560px"
    >
      <el-form label-width="90px">
        <el-form-item
          label="审核备注"
          :required="auditForm.audit_type === 'cancel'"
        >
          <el-input
            v-model="auditForm.audit_remark"
            type="textarea"
            :rows="4"
            :placeholder="
              auditForm.audit_type === 'pass'
                ? '可选，默认「同意」'
                : '请填写驳回原因（必填）'
            "
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="auditVisible = false">取消</el-button>
        <el-button type="primary" :loading="auditLoading" @click="onAudit">确认</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.todoworkflow-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.pager {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}

.header-hint {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: normal;
}

.todo-preview {
  padding: 4px 12px 8px 48px;
}

.preview-loading {
  min-height: 80px;
}

.preview-unsupported-text {
  margin-right: 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.preview-meta {
  margin-bottom: 10px;
}

.preview-sql-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.preview-sql-item {
  padding: 8px 10px;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}

.preview-sql-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}

.preview-sql-index {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.preview-sql-item pre {
  margin: 0;
  max-height: 120px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
}

.preview-sql-error {
  margin-top: 4px;
  color: var(--el-color-danger);
  font-size: 12px;
  word-break: break-all;
}

.preview-more {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.preview-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}
</style>
