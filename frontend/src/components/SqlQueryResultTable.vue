<script setup lang="ts">
import {
  ref,
  computed,
  watch,
  onMounted,
  onBeforeUnmount,
} from "vue";
import * as XLSX from "xlsx";
import { ElMessage } from "element-plus";
import {
  Search,
  Filter,
  Grid,
  CopyDocument,
  Download,
  FullScreen,
  Close,
} from "@element-plus/icons-vue";
import type { QueryResultEnvelope } from "@/api/sqlquery";
import TruncateCell from "@/components/TruncateCell.vue";

const props = withDefaults(
  defineProps<{
    /** 执行结果信封（status 三态），null=未执行 */
    envelope: QueryResultEnvelope | null;
    loading?: boolean;
    /** 本次执行生效的行数上限，0=不限 */
    limitNum?: number;
    /** 本次执行的 SQL（用于复制/定位） */
    sql?: string;
    /** 本次查询的表名（用于生成 INSERT/WHERE，可空） */
    tableName?: string;
  }>(),
  { loading: false, limitNum: 0, sql: "", tableName: "" }
);

const emit = defineEmits<{
  (e: "locate-error", position: { line: number; column?: number }): void;
}>();

const SQL_COLUMNS = new Set(["sql", "sqllog", "sql_text"]);

function isSqlColumn(col: string): boolean {
  return SQL_COLUMNS.has(col.toLowerCase());
}

const page = ref(1);
const pageSize = ref(50);
const searchText = ref("");
const sortProp = ref("");
const sortOrder = ref<"" | "ascending" | "descending">("");
const selectedCell = ref<{ row: Record<string, unknown>; col: string } | null>(
  null
);
const fullscreen = ref(false);

type FilterOperator = "contains" | "equals" | "notEmpty";

interface ColumnFilter {
  id: number;
  column: string;
  operator: FilterOperator;
  value: string;
}

const filters = ref<ColumnFilter[]>([]);
const filterDraft = ref<{
  column: string;
  operator: FilterOperator;
  value: string;
}>({ column: "", operator: "contains", value: "" });
let filterSeq = 0;

const result = computed(() =>
  props.envelope && props.envelope.status === 0 ? props.envelope.data : null
);
const errorInfo = computed(() =>
  props.envelope && props.envelope.status !== 0
    ? { status: props.envelope.status, msg: props.envelope.msg }
    : null
);

const columnList = computed(() => result.value?.column_list || []);

/** 把值数组的 rows zip 成对象数组，供 el-table 渲染 */
const objectRows = computed<Record<string, unknown>[]>(() => {
  const r = result.value;
  if (!r) return [];
  const cols = r.column_list || [];
  return (r.rows || []).map((row) => {
    const o: Record<string, unknown> = {};
    cols.forEach((c, i) => (o[c] = row[i]));
    return o;
  });
});

const visibleColumns = ref<string[]>([]);

watch(
  columnList,
  (cols) => {
    visibleColumns.value = [...cols];
    if (cols.length && !cols.includes(filterDraft.value.column)) {
      filterDraft.value.column = cols[0];
    }
    filters.value = filters.value.filter((f) => cols.includes(f.column));
  },
  { immediate: true }
);

const visibleColumnList = computed(() =>
  columnList.value.filter((c) => visibleColumns.value.includes(c))
);

/** show create table 类结果（column_list 形如 ['table','create table']）：用 <pre> 展示 CREATE 文本 */
const isShowCreate = computed(() => {
  const r = result.value;
  if (!r) return false;
  const cols = (r.column_list || []).map((c) => String(c).toLowerCase());
  return (
    cols.length === 2 && cols[0].includes("table") && cols[1].includes("create")
  );
});
const createText = computed(() => {
  if (!isShowCreate.value || !result.value) return "";
  return (result.value.rows || [])
    .map((r) => String(r[1] ?? ""))
    .join("\n\n");
});

const sortOrders: Array<"ascending" | "descending" | null> = [
  "ascending",
  "descending",
  null,
];

function formatCell(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function rawText(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function sqlLiteral(v: unknown): string {
  if (v === null || v === undefined) return "NULL";
  if (typeof v === "number" || typeof v === "bigint") return String(v);
  if (typeof v === "boolean") return v ? "1" : "0";
  const s = typeof v === "object" ? JSON.stringify(v) : String(v);
  return "'" + s.replace(/'/g, "''") + "'";
}

const searchActive = computed(
  () => !!searchText.value.trim() || filters.value.length > 0
);

const filteredRows = computed(() => {
  let rows = objectRows.value;
  const kw = searchText.value.trim().toLowerCase();
  if (kw) {
    rows = rows.filter((row) =>
      columnList.value.some((col) =>
        formatCell(row[col]).toLowerCase().includes(kw)
      )
    );
  }
  for (const f of filters.value) {
    rows = rows.filter((row) => {
      const text = formatCell(row[f.column]);
      if (f.operator === "notEmpty") return text !== "";
      if (f.operator === "equals") return text === f.value;
      return text.toLowerCase().includes(f.value.toLowerCase());
    });
  }
  return rows;
});

function compareValues(a: unknown, b: unknown): number {
  const aEmpty = a === null || a === undefined || a === "";
  const bEmpty = b === null || b === undefined || b === "";
  if (aEmpty && bEmpty) return 0;
  if (aEmpty) return 1;
  if (bEmpty) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  const sa = String(a);
  const sb = String(b);
  const na = Number(sa);
  const nb = Number(sb);
  if (sa.trim() !== "" && sb.trim() !== "" && !Number.isNaN(na) && !Number.isNaN(nb)) {
    return na - nb;
  }
  return sa.localeCompare(sb, "zh-CN", { numeric: true, sensitivity: "base" });
}

const sortedRows = computed(() => {
  if (!sortProp.value || !sortOrder.value) return filteredRows.value;
  const dir = sortOrder.value === "ascending" ? 1 : -1;
  return [...filteredRows.value].sort(
    (a, b) => compareValues(a[sortProp.value], b[sortProp.value]) * dir
  );
});

const displayRows = computed(() => sortedRows.value);

const pagedRows = computed(() => {
  const start = (page.value - 1) * pageSize.value;
  return displayRows.value.slice(start, start + pageSize.value);
});

const selectedRow = computed(() => selectedCell.value?.row ?? null);

const isTruncated = computed(
  () => props.limitNum > 0 && objectRows.value.length >= props.limitNum
);

const emptyText = computed(() =>
  searchActive.value ? "没有匹配的数据" : "暂无数据"
);

const maxTableHeight = computed(() =>
  fullscreen.value ? "calc(100vh - 260px)" : 520
);

const errorPosition = computed<{ line: number; column?: number } | null>(() => {
  const msg = errorInfo.value?.msg || "";
  if (!msg) return null;
  const lineMatch = msg.match(/(?:\bat\s+line|\bline|第)\s*(\d+)\s*行?/i);
  if (!lineMatch) return null;
  const colMatch = msg.match(/(?:column|列)\s*(\d+)/i);
  return {
    line: Number(lineMatch[1]),
    column: colMatch ? Number(colMatch[1]) : undefined,
  };
});

const canAddFilter = computed(
  () =>
    !!filterDraft.value.column &&
    (filterDraft.value.operator === "notEmpty" ||
      filterDraft.value.value.trim() !== "")
);

// 新查询结果到达时重置所有视图状态：
// 结果 tab 组件实例按 key 复用，若停留在旧页码/旧筛选，新结果会显示为空。
watch(
  () => props.envelope,
  () => {
    page.value = 1;
    searchText.value = "";
    filters.value = [];
    sortProp.value = "";
    sortOrder.value = "";
    selectedCell.value = null;
  }
);

watch([searchText, filters, sortProp, sortOrder], () => {
  page.value = 1;
}, { deep: true });

watch(
  () => displayRows.value.length,
  (len) => {
    const maxPage = Math.max(1, Math.ceil(len / pageSize.value));
    if (page.value > maxPage) page.value = maxPage;
  }
);

function onPageSizeChange(size: number) {
  pageSize.value = size;
  page.value = 1;
}

function onSortChange(payload: {
  prop: string | null;
  order: string | null;
}) {
  if (
    payload.prop &&
    (payload.order === "ascending" || payload.order === "descending")
  ) {
    sortProp.value = payload.prop;
    sortOrder.value = payload.order;
  } else {
    sortProp.value = "";
    sortOrder.value = "";
  }
}

function onCellClick(
  row: Record<string, unknown>,
  column: { property?: string }
) {
  if (column.property) {
    selectedCell.value = { row, col: column.property };
  }
}

function cellClassName({
  row,
  column,
}: {
  row: Record<string, unknown>;
  column: { property?: string };
}): string {
  if (!selectedCell.value || !column.property) return "";
  return selectedCell.value.row === row &&
    selectedCell.value.col === column.property
    ? "cell-selected"
    : "";
}

function addFilter() {
  if (!canAddFilter.value) return;
  filters.value.push({
    id: ++filterSeq,
    column: filterDraft.value.column,
    operator: filterDraft.value.operator,
    value: filterDraft.value.value.trim(),
  });
  filterDraft.value.value = "";
}

function removeFilter(id: number) {
  filters.value = filters.value.filter((f) => f.id !== id);
}

function clearFilters() {
  filters.value = [];
}

function filterOperatorText(op: FilterOperator): string {
  switch (op) {
    case "contains":
      return "包含";
    case "equals":
      return "等于";
    case "notEmpty":
      return "非空";
    default:
      return op;
  }
}

function rowToObject(
  row: Record<string, unknown>,
  cols: string[]
): Record<string, unknown> {
  const obj: Record<string, unknown> = {};
  for (const col of cols) obj[col] = row[col] ?? null;
  return obj;
}

function buildTsv(rows: Record<string, unknown>[], cols: string[]): string {
  const header = cols.join("\t");
  const lines = rows.map((row) =>
    cols.map((col) => rawText(row[col]).replace(/[\t\r\n]+/g, " ")).join("\t")
  );
  return [header, ...lines].join("\n");
}

function buildInsertSql(
  rows: Record<string, unknown>[],
  cols: string[]
): string {
  const table = props.tableName || "table_name";
  return rows
    .map(
      (row) =>
        `INSERT INTO ${table} (${cols.join(", ")}) VALUES (${cols
          .map((col) => sqlLiteral(row[col]))
          .join(", ")});`
    )
    .join("\n");
}

function buildWhereSql(row: Record<string, unknown>, cols: string[]): string {
  const conditions = cols.map((col) => {
    const v = row[col];
    if (v === null || v === undefined) return `${col} IS NULL`;
    return `${col} = ${sqlLiteral(v)}`;
  });
  return `WHERE ${conditions.join(" AND ")}`;
}

function buildMarkdown(
  rows: Record<string, unknown>[],
  cols: string[]
): string {
  const escape = (v: unknown) =>
    formatCell(v).replace(/\|/g, "\\|").replace(/\r?\n/g, "<br>");
  const lines = [
    `| ${cols.join(" | ")} |`,
    `| ${cols.map(() => "---").join(" | ")} |`,
    ...rows.map((row) => `| ${cols.map((col) => escape(row[col])).join(" | ")} |`),
  ];
  return lines.join("\n");
}

function downloadText(text: string, filename: string, mime: string) {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

async function copyText(text: string, label = "内容") {
  if (!text) {
    ElMessage.warning("没有可复制的内容");
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    ElMessage.success(`${label}已复制到剪贴板`);
  } catch {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    document.body.removeChild(textarea);
    ElMessage.success(`${label}已复制到剪贴板`);
  }
}

function onCopyCommand(command: string) {
  const cols = visibleColumnList.value;
  const rows = displayRows.value;
  switch (command) {
    case "cell": {
      if (!selectedCell.value) return;
      copyText(
        rawText(selectedCell.value.row[selectedCell.value.col]),
        "单元格"
      );
      break;
    }
    case "row": {
      if (!selectedRow.value) return;
      copyText(buildTsv([selectedRow.value], cols), "当前行");
      break;
    }
    case "tsv":
      copyText(buildTsv(rows, cols), "全部结果");
      break;
    case "json":
      copyText(
        JSON.stringify(rows.map((row) => rowToObject(row, cols)), null, 2),
        "JSON"
      );
      break;
    case "insert":
      copyText(buildInsertSql(rows, cols), "INSERT 语句");
      break;
    case "where": {
      if (!selectedRow.value) return;
      copyText(buildWhereSql(selectedRow.value, cols), "WHERE 条件");
      break;
    }
    case "sql":
      copyText(props.sql, "执行 SQL");
      break;
    default:
      break;
  }
}

// Excel 单元格最多保留 15 位有效数字，双击打开 CSV 时 16 位以上的纯整数
// （如 BIGINT 雪花 ID 219034306784399364）会被截断显示成 219034306784399000。
// 写成 ="数字" 公式文本可让 Excel 按字符串原样展示，其它工具读到的是带引号的字段。
const LONG_INTEGER_RE = /^-?\d{16,}$/;

function csvCell(v: unknown): string {
  let s = formatCell(v);
  if (LONG_INTEGER_RE.test(s)) s = `="${s}"`;
  if (/[",\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}

function onExportCommand(command: string) {
  const cols = visibleColumnList.value;
  const rows = displayRows.value;
  if (!cols.length) return;
  const stamp = Date.now();

  switch (command) {
    case "csv": {
      const lines = [cols.join(",")];
      for (const row of rows) {
        lines.push(cols.map((col) => csvCell(row[col])).join(","));
      }
      // BOM 防止 Excel 中文乱码
      downloadText(
        "\uFEFF" + lines.join("\n"),
        `query_result_${stamp}.csv`,
        "text/csv;charset=utf-8;"
      );
      break;
    }
    case "xlsx": {
      const aoa: unknown[][] = [
        cols,
        ...rows.map((row) => cols.map((col) => row[col] ?? "")),
      ];
      const ws = XLSX.utils.aoa_to_sheet(aoa);
      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, "result");
      XLSX.writeFile(wb, `query_result_${stamp}.xlsx`);
      break;
    }
    case "json":
      downloadText(
        JSON.stringify(rows.map((row) => rowToObject(row, cols)), null, 2),
        `query_result_${stamp}.json`,
        "application/json;charset=utf-8;"
      );
      break;
    case "markdown":
      downloadText(
        buildMarkdown(rows, cols),
        `query_result_${stamp}.md`,
        "text/markdown;charset=utf-8;"
      );
      break;
    case "sql":
      downloadText(
        buildInsertSql(rows, cols),
        `query_result_${stamp}.sql`,
        "text/plain;charset=utf-8;"
      );
      break;
    default:
      break;
  }
}

function showAllColumns() {
  visibleColumns.value = [...columnList.value];
}

function locateError() {
  if (errorPosition.value) emit("locate-error", errorPosition.value);
}

function toggleFullscreen() {
  fullscreen.value = !fullscreen.value;
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === "Escape" && fullscreen.value) fullscreen.value = false;
}

onMounted(() => window.addEventListener("keydown", onKeydown));
onBeforeUnmount(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <div
    v-loading="props.loading"
    class="result-wrap"
    :class="{ 'is-fullscreen': fullscreen }"
  >
    <!-- 错误态 -->
    <el-alert
      v-if="errorInfo"
      :title="errorInfo.status === 2 ? '无查询权限' : '查询失败'"
      :type="errorInfo.status === 2 ? 'warning' : 'error'"
      :closable="false"
      show-icon
    >
      <pre class="error-message">{{ errorInfo.msg || "查询失败" }}</pre>
      <div class="error-actions">
        <el-button
          v-if="errorPosition"
          size="small"
          type="primary"
          plain
          @click="locateError"
        >
          定位到第 {{ errorPosition.line }} 行
        </el-button>
        <el-button size="small" @click="copyText(errorInfo.msg, '错误信息')">
          复制错误
        </el-button>
        <el-link
          v-if="errorInfo.status === 2"
          type="primary"
          href="/queryapplylist"
          target="_blank"
        >
          去申请查询权限
        </el-link>
      </div>
    </el-alert>

    <!-- 结果 -->
    <template v-else-if="result">
      <div class="result-toolbar">
        <div class="meta-tags">
          <!--
            有结果集（column_list 非空）：显示实际返回行数（rows.length）。
            注意：MySQL 引擎的 affected_rows 是 cursor.execute() 返回值，
            对 SELECT 为匹配总行数（可能被 limit 截断）、对 UPDATE/INSERT/DELETE
            为影响行数且无结果集，直接展示会造成“行数 N 但表格无数据”的误导。
          -->
          <el-tag size="small" type="info">
            {{
              columnList.length
                ? `返回 ${objectRows.length} 行`
                : `影响行数 ${result.affected_rows}`
            }}
          </el-tag>
          <el-tag
            v-if="searchActive"
            size="small"
            type="success"
            effect="plain"
          >
            筛选后 {{ displayRows.length }} 行
          </el-tag>
          <el-tooltip
            v-if="isTruncated"
            :content="`本次查询上限 ${limitNum} 行，结果可能不完整；可调整行数限制或补充 WHERE 条件后重查`"
          >
            <el-tag size="small" type="warning" effect="dark">
              已达上限，可能被截断
            </el-tag>
          </el-tooltip>
          <el-tag size="small" type="info">耗时 {{ result.query_time }}s</el-tag>
          <el-tag v-if="result.is_masked" size="small" type="warning">
            已脱敏
          </el-tag>
          <el-tag
            v-if="result.seconds_behind_master != null"
            size="small"
            type="info"
          >
            主从延迟 {{ result.seconds_behind_master }}s
          </el-tag>
        </div>

        <div v-if="columnList.length && !isShowCreate" class="toolbar-actions">
          <el-input
            v-model="searchText"
            size="small"
            clearable
            class="search-input"
            placeholder="在结果中搜索"
            :prefix-icon="Search"
          />

          <el-popover
            placement="bottom-end"
            :width="340"
            trigger="click"
            popper-class="sql-result-filter-popper"
          >
            <template #reference>
              <el-button
                size="small"
                :type="filters.length ? 'primary' : ''"
                :plain="filters.length > 0"
                :icon="Filter"
              >
                筛选{{ filters.length ? `（${filters.length}）` : "" }}
              </el-button>
            </template>
            <div class="filter-builder">
              <el-select
                v-model="filterDraft.column"
                size="small"
                filterable
                placeholder="选择列"
                style="width: 100%"
              >
                <el-option
                  v-for="col in columnList"
                  :key="col"
                  :label="col"
                  :value="col"
                />
              </el-select>
              <div class="filter-draft-row">
                <el-select
                  v-model="filterDraft.operator"
                  size="small"
                  style="width: 110px"
                >
                  <el-option label="包含" value="contains" />
                  <el-option label="等于" value="equals" />
                  <el-option label="非空" value="notEmpty" />
                </el-select>
                <el-input
                  v-if="filterDraft.operator !== 'notEmpty'"
                  v-model="filterDraft.value"
                  size="small"
                  placeholder="匹配值"
                  @keyup.enter="addFilter"
                />
              </div>
              <el-button
                size="small"
                type="primary"
                :disabled="!canAddFilter"
                @click="addFilter"
              >
                添加条件
              </el-button>
              <div v-if="filters.length" class="active-filters">
                <el-tag
                  v-for="f in filters"
                  :key="f.id"
                  size="small"
                  closable
                  @close="removeFilter(f.id)"
                >
                  {{ f.column }} {{ filterOperatorText(f.operator) }}
                  {{ f.value }}
                </el-tag>
                <el-button link size="small" @click="clearFilters">
                  清空
                </el-button>
              </div>
            </div>
          </el-popover>

          <el-popover
            placement="bottom-end"
            :width="240"
            trigger="click"
            popper-class="sql-result-column-popper"
          >
            <template #reference>
              <el-button size="small" :icon="Grid">
                列{{ visibleColumns.length }}/{{ columnList.length }}
              </el-button>
            </template>
            <div class="column-panel">
              <div class="column-panel-head">
                <span>显示列</span>
                <el-button link size="small" @click="showAllColumns">
                  全选
                </el-button>
              </div>
              <el-checkbox-group v-model="visibleColumns" :min="1" class="column-checks">
                <el-checkbox
                  v-for="col in columnList"
                  :key="col"
                  :value="col"
                >
                  {{ col }}
                </el-checkbox>
              </el-checkbox-group>
            </div>
          </el-popover>

          <el-dropdown trigger="click" @command="onCopyCommand">
            <el-button size="small" :icon="CopyDocument">复制</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="cell" :disabled="!selectedCell">
                  复制单元格
                </el-dropdown-item>
                <el-dropdown-item command="row" :disabled="!selectedRow">
                  复制当前行（含表头）
                </el-dropdown-item>
                <el-dropdown-item command="tsv" :disabled="!displayRows.length">
                  复制全部结果（TSV）
                </el-dropdown-item>
                <el-dropdown-item command="json" :disabled="!displayRows.length">
                  复制为 JSON
                </el-dropdown-item>
                <el-dropdown-item command="insert" :disabled="!displayRows.length">
                  复制为 INSERT
                </el-dropdown-item>
                <el-dropdown-item command="where" :disabled="!selectedRow">
                  复制为 WHERE 条件
                </el-dropdown-item>
                <el-dropdown-item command="sql" :disabled="!sql" divided>
                  复制执行 SQL
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>

          <el-dropdown trigger="click" @command="onExportCommand">
            <el-button size="small" :icon="Download">导出</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>
                  当前筛选结果 {{ displayRows.length }} 行
                </el-dropdown-item>
                <el-dropdown-item command="csv" divided>导出 CSV</el-dropdown-item>
                <el-dropdown-item command="xlsx">导出 XLSX</el-dropdown-item>
                <el-dropdown-item command="json">导出 JSON</el-dropdown-item>
                <el-dropdown-item command="markdown">导出 Markdown</el-dropdown-item>
                <el-dropdown-item command="sql">导出 INSERT SQL</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>

          <el-button
            size="small"
            :icon="fullscreen ? Close : FullScreen"
            @click="toggleFullscreen"
          >
            {{ fullscreen ? "退出全屏" : "全屏" }}
          </el-button>
        </div>
      </div>

      <div v-if="filters.length" class="filter-tags">
        <span class="filter-tags-label">筛选：</span>
        <el-tag
          v-for="f in filters"
          :key="f.id"
          size="small"
          closable
          @close="removeFilter(f.id)"
        >
          {{ f.column }} {{ filterOperatorText(f.operator) }} {{ f.value }}
        </el-tag>
        <el-button link size="small" @click="clearFilters">清空</el-button>
      </div>

      <pre
        v-if="isShowCreate"
        class="create-text"
        :style="{ maxHeight: fullscreen ? 'calc(100vh - 260px)' : '520px' }"
      >{{ createText }}</pre>
      <el-table
        v-else-if="columnList.length"
        class="sql-result-table"
        :data="pagedRows"
        stripe
        border
        :max-height="maxTableHeight"
        style="width: 100%"
        :empty-text="emptyText"
        :cell-class-name="cellClassName"
        @sort-change="onSortChange"
        @cell-click="onCellClick"
      >
        <el-table-column
          v-for="col in visibleColumnList"
          :key="col"
          :prop="col"
          :label="col"
          min-width="140"
          sortable="custom"
          :sort-orders="sortOrders"
          :show-overflow-tooltip="!isSqlColumn(col)"
        >
          <template v-if="isSqlColumn(col)" #default="{ row }">
            <TruncateCell :value="row[col]" :row="row" :col="col" />
          </template>
          <template v-else #default="{ row }">{{ formatCell(row[col]) }}</template>
        </el-table-column>
      </el-table>
      <!-- 无结果集（UPDATE/INSERT/DELETE 等 DML、DDL）：执行成功但无数据返回，属正常情况 -->
      <el-empty v-else description="语句执行成功，无结果集返回" />

      <div v-if="displayRows.length > pageSize" class="pager">
        <el-pagination
          :total="displayRows.length"
          :current-page="page"
          :page-size="pageSize"
          :page-sizes="[50, 100, 500, 1000]"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="(p: number) => (page = p)"
          @size-change="onPageSizeChange"
        />
      </div>
    </template>

    <!-- 未执行 -->
    <el-empty v-else description="执行查询后在此展示结果" />
  </div>
</template>

<style scoped lang="scss">
.result-wrap {
  min-height: 200px;
}

.result-wrap.is-fullscreen {
  position: fixed;
  inset: 0;
  z-index: 3000;
  padding: 16px 20px;
  background: var(--el-bg-color);
  overflow: auto;
}

.result-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin-bottom: 10px;
}

.meta-tags,
.toolbar-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.search-input {
  width: 200px;
}

.filter-builder {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.filter-draft-row {
  display: flex;
  gap: 8px;
}

.active-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  padding-top: 4px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.filter-tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}

.filter-tags-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.column-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.column-panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
}

.error-message {
  margin: 8px 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 12px;
  line-height: 1.6;
}

.error-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.create-text {
  margin: 0;
  padding: 12px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  overflow: auto;
}

.pager {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}

/* 表头与数据行拉开层次：浅主色底 + 更重的下边框 + 加粗字重 */
.sql-result-table {
  --el-table-header-bg-color: var(--el-color-primary-light-8);
  --el-table-header-text-color: var(--el-text-color-primary);

  :deep(.el-table__header-wrapper th.el-table__cell) {
    font-weight: 600;
    letter-spacing: 0.2px;
    border-bottom: 1px solid var(--el-color-primary-light-5) !important;
  }

  /* 悬停排序时保持表头主色底，不要退回和数据行一样的灰色 */
  :deep(.el-table__header-wrapper th.el-table__cell:hover) {
    background-color: var(--el-color-primary-light-7) !important;
  }

  /* 选中行高亮不要盖住表头 */
  :deep(.el-table__header-wrapper) {
    position: relative;
    z-index: 2;
  }
}

:deep(.cell-selected) {
  background: var(--el-color-primary-light-8) !important;
}
</style>

<style lang="scss">
/* Popover 内容会被 teleport 到 body，scoped 样式无法命中，使用独立 popper-class */
.sql-result-column-popper .column-checks,
.sql-result-filter-popper .column-checks {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 280px;
  overflow-y: auto;
}
</style>
