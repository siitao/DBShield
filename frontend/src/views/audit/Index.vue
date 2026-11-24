<script setup lang="ts">
import { ref, reactive, watch, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  fetchAuditLog,
  fetchWorkflowAudit,
  fetchQueryLogAudit,
} from "@/api/phase2";
import TruncateCell from "@/components/TruncateCell.vue";

const route = useRoute();
const router = useRouter();

type AuditType = "general" | "workflow" | "query";
const AUDIT_TYPES: AuditType[] = ["general", "workflow", "query"];
function tabFromQuery(value: unknown): AuditType {
  const t = String(value || "");
  return (AUDIT_TYPES as string[]).includes(t) ? (t as AuditType) : "general";
}
const activeTab = ref<AuditType>(tabFromQuery(route.query.type));

const loading = ref(false);
const list = ref<Record<string, unknown>[]>([]);
const cols = ref<string[]>([]);
const total = ref(0);
const query = reactive({
  search: "",
  action: "",
  dateRange: null as [string, string] | null,
  page: 1,
  size: 20,
});

/** SQL/长文本列名集合（审计日志中 SQL 字段常见名） */
const SQL_COLUMNS = new Set(["sql", "sql_content", "sqllog", "query_sql", "content", "detail", "sql_text"]);

function isSqlColumn(col: string): boolean {
  return SQL_COLUMNS.has(col.toLowerCase());
}

/**
 * 各审计 tab 的表头中文映射（按后端返回的英文字段名）。
 * 未命中的字段原样显示，避免后端新增字段时表头空白。
 */
const COL_LABEL_MAP: Record<AuditType, Record<string, string>> = {
  general: {
    user_id: "用户ID",
    user_name: "用户名",
    user_display: "用户姓名",
    action: "操作",
    extra_info: "附加信息",
    action_time: "操作时间",
  },
  workflow: {
    id: "工单ID",
    workflow_name: "工单名称",
    engineer_display: "发起人",
    status: "状态",
    is_backup: "是否备份",
    create_time: "创建时间",
    "instance__instance_name": "实例",
    db_name: "数据库",
    group_name: "资源组",
    syntax_type: "工单类型",
    export_format: "导出格式",
  },
  query: {
    id: "日志ID",
    username: "用户名",
    user_display: "用户姓名",
    target_instance: "实例",
    search_db: "数据库",
    sqllog: "SQL语句",
    effect_row: "影响行数",
    cost_time: "耗时(ms)",
    execute_time: "执行时间",
    create_time: "查询时间",
  },
};

function colLabel(col: string): string {
  return COL_LABEL_MAP[activeTab.value]?.[col] || col;
}

/** 通用审计里部分 action 是代码（如 slowquery.diagnose_start），展示时转成中文 */
const ACTION_LABEL_MAP: Record<string, string> = {
  "slowquery.diagnose_start": "发起慢查询诊断",
  "slowquery.feedback": "慢查询诊断反馈",
  "slowquery.suggestion_adopt": "采纳优化建议",
};

function actionLabel(action: unknown): string {
  const raw = String(action ?? "");
  return ACTION_LABEL_MAP[raw] || raw;
}

async function loadData() {
  loading.value = true;
  list.value = [];
  try {
    const params = {
      search: query.search || undefined,
      limit: query.size,
      offset: (query.page - 1) * query.size,
    };
    let r: { total: number; rows: Record<string, unknown>[] };
    if (activeTab.value === "general") {
      r = await fetchAuditLog({
        ...params,
        action: query.action || undefined,
        start_date: query.dateRange?.[0],
        end_date: query.dateRange?.[1],
      });
    } else if (activeTab.value === "workflow") {
      r = await fetchWorkflowAudit(params);
    } else {
      r = await fetchQueryLogAudit(params);
    }
    list.value = r.rows;
    total.value = r.total;
    cols.value = r.rows.length ? Object.keys(r.rows[0]) : [];
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

function onTabChange(name: string | number) {
  activeTab.value = tabFromQuery(name);
  // 同步到 URL query，让侧边栏「系统审计」下的三个子项高亮跟随当前页签
  if (route.query.type !== activeTab.value) {
    router.replace({ query: { ...route.query, type: activeTab.value } });
  }
  query.page = 1;
  loadData();
}

// 从侧边栏切换同一页面的其他审计子项时，路由复用组件，需要手动同步页签
watch(
  () => route.query.type,
  (value) => {
    const next = tabFromQuery(value);
    if (next === activeTab.value) return;
    activeTab.value = next;
    query.page = 1;
    loadData();
  }
);

watch(
  () => query.page,
  () => loadData()
);

onMounted(loadData);
</script>

<template>
  <div class="audit-page">
    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent>
        <el-form-item label="搜索">
          <el-input
            v-model="query.search"
            placeholder="关键字"
            clearable
            style="width: 200px"
            @keyup.enter="onSearch"
          />
        </el-form-item>
        <el-form-item v-if="activeTab === 'general'" label="操作">
          <el-input v-model="query.action" placeholder="操作代码，如 slowquery.feedback" style="width: 220px" />
        </el-form-item>
        <el-form-item v-if="activeTab === 'general'" label="时间">
          <el-date-picker
            v-model="query.dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            range-separator="-"
            start-placeholder="开始"
            end-placeholder="结束"
            style="width: 240px"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="onSearch">查询</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <el-tabs v-model="activeTab" @tab-change="onTabChange">
        <el-tab-pane label="通用审计" name="general" />
        <el-tab-pane label="SQL 上线审计" name="workflow" />
        <el-tab-pane label="查询审计" name="query" />
      </el-tabs>
      <el-table v-loading="loading" :data="list" stripe border max-height="600">
        <el-table-column
          v-for="col in cols"
          :key="col"
          :prop="col"
          :label="colLabel(col)"
          min-width="140"
          :show-overflow-tooltip="!isSqlColumn(col) && !(activeTab === 'general' && col === 'action')"
        >
          <template
            v-if="activeTab === 'general' && col === 'action'"
            #default="{ row }"
          >
            <span :title="String((row as Record<string, unknown>).action ?? '')">
              {{ actionLabel((row as Record<string, unknown>).action) }}
            </span>
          </template>
          <template v-else-if="isSqlColumn(col)" #default="{ row }">
            <TruncateCell :value="(row as Record<string,unknown>)[col]" :row="row as Record<string,unknown>" :col="col" />
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          :total="total"
          :current-page="query.page"
          :page-size="query.size"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="(p: number) => (query.page = p)"
          @size-change="(s: number) => { query.size = s; query.page = 1; loadData(); }"
        />
      </div>
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.audit-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.filter-card :deep(.el-form-item) {
  margin-bottom: 0;
}

.pager {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}
</style>
