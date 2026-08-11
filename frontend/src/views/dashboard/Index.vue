<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { legacyBase } from "@/utils/request";
import {
  fetchDashboardCharts,
  type DashboardCharts,
  type BarLineChart,
  type PieDatum,
} from "@/api/dashboard";
import EChart from "@/components/EChart.vue";
import type { EChartsOption } from "echarts";

const auth = useAuthStore();
const router = useRouter();

const now = new Date();
const hour = now.getHours();
const greeting = computed(() => {
  if (hour < 6) return "凌晨好";
  if (hour < 12) return "上午好";
  if (hour < 14) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
});
const todayStr = `${now.getFullYear()}年${now.getMonth() + 1}月${now.getDate()}日`;
const weekday = "周" + "日一二三四五六"[now.getDay()];

interface QuickEntry {
  title: string;
  desc: string;
  icon: string;
  color: string;
  perm?: string;
  routeName?: string;
  legacyPath?: string;
}

const entries: QuickEntry[] = [
  { title: "在线查询", desc: "对授权实例执行只读 SQL", icon: "Search", color: "#2563eb", perm: "sql.menu_sqlquery", routeName: "sqlquery-index" },
  { title: "SQL 上线", desc: "提交并审核 DDL/DML 工单", icon: "CircleCheck", color: "#16a34a", perm: "sql.menu_sqlworkflow", routeName: "sqlworkflow-list" },
  { title: "慢查日志", desc: "Review 慢查询并优化", icon: "Timer", color: "#ea580c", perm: "sql.menu_slowquery", routeName: "slowquery" },
  { title: "数据字典", desc: "浏览表结构与对象", icon: "Reading", color: "#0891b2", perm: "sql.menu_data_dictionary", routeName: "datadictionary" },
];

function visibleEntries() {
  return entries.filter((e) => !e.perm || auth.hasPerm(e.perm));
}

function go(e: QuickEntry) {
  if (e.routeName) router.push({ name: e.routeName });
  else if (e.legacyPath) window.open(legacyBase + e.legacyPath, "_blank");
}

// ===================== 图表 =====================
const loading = ref(false);
const charts = ref<DashboardCharts | null>(null);

const dateRange = ref<[string, string]>(defaultRange());

function defaultRange(): [string, string] {
  const fmt = (d: Date) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - 29);
  return [fmt(start), fmt(end)];
}

// 统一调色板：与快捷入口/品牌色系呼应
const PALETTE = ["#2563eb", "#16a34a", "#ea580c", "#7c3aed", "#db2777", "#0891b2", "#d97706", "#0d9488"];

function barOption(title: string, c: BarLineChart): EChartsOption {
  const multi = c.series.length > 1;
  return {
    title: { text: title, left: "center", textStyle: { fontSize: 14, fontWeight: 600 } },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    legend: multi ? { top: 28, type: "scroll" } : undefined,
    grid: { left: 48, right: 24, top: multi ? 64 : 40, bottom: 40, containLabel: true },
    color: PALETTE,
    xAxis: {
      type: "category",
      data: c.x,
      axisLabel: { hideOverlap: true, rotate: c.x.length > 12 ? 40 : 0 },
    },
    yAxis: { type: "value", splitLine: { lineStyle: { type: "dashed", color: "#e5e7eb" } } },
    series: c.series.map((s) => ({
      name: s.name,
      type: "bar",
      data: s.data,
      barMaxWidth: 32,
      itemStyle: { borderRadius: [4, 4, 0, 0] },
    })),
  };
}

function lineOption(title: string, c: BarLineChart): EChartsOption {
  return {
    title: { text: title, left: "center", textStyle: { fontSize: 14, fontWeight: 600 } },
    tooltip: { trigger: "axis" },
    legend: { top: 28, type: "scroll" },
    grid: { left: 48, right: 24, top: 64, bottom: 40, containLabel: true },
    color: PALETTE,
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: c.x,
      axisLabel: { hideOverlap: true, rotate: c.x.length > 12 ? 40 : 0 },
    },
    yAxis: { type: "value", splitLine: { lineStyle: { type: "dashed", color: "#e5e7eb" } } },
    series: c.series.map((s) => ({
      name: s.name,
      type: "line",
      smooth: true,
      data: s.data,
      symbol: "circle",
      symbolSize: 6,
      lineStyle: { width: 2 },
      areaStyle: { opacity: 0.1 },
    })),
  };
}

function pieOption(title: string, data: PieDatum[]): EChartsOption {
  return {
    title: { text: title, left: "center", textStyle: { fontSize: 14, fontWeight: 600 } },
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    legend: { type: "scroll", orient: "vertical", left: 8, top: "middle", icon: "circle" },
    color: PALETTE,
    series: [
      {
        type: "pie",
        radius: ["42%", "68%"],
        center: ["62%", "54%"],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: "#fff", borderWidth: 2, borderRadius: 4 },
        label: { show: false },
        emphasis: {
          label: { show: true, fontSize: 14, fontWeight: 600 },
          itemStyle: { shadowBlur: 12, shadowOffsetX: 0, shadowColor: "rgba(0,0,0,0.2)" },
        },
        data: data.map((d) => ({ name: d.name, value: Number(d.value) })),
      },
    ],
  };
}

// 各图 option（数据为空时占位）
const optBar1 = computed(() => (charts.value ? barOption("SQL 上线数量（按日）", charts.value.bar1) : {}));
const optBar2 = computed(() => (charts.value ? barOption("SQL 上线用户", charts.value.bar2) : {}));
const optBar3 = computed(() => (charts.value ? barOption("慢查询 db 维度", charts.value.bar3) : {}));
const optBar5 = computed(() => (charts.value ? barOption("SQL 上线工单", charts.value.bar5) : {}));
const optLine1 = computed(() => (charts.value ? lineOption("SQL 查询统计（按日）", charts.value.line1) : {}));
const optPie1 = computed(() => (charts.value ? pieOption("SQL 上线统计（按组）", charts.value.pie1) : {}));
const optPie2 = computed(() => (charts.value ? pieOption("SQL 语法类型", charts.value.pie2) : {}));
const optPie3 = computed(() => (charts.value ? pieOption("慢查询 db/user 维度", charts.value.pie3) : {}));
const optPie4 = computed(() => (charts.value ? pieOption("SQL 查询用户（检索行数）", charts.value.pie4) : {}));
const optPie5 = computed(() => (charts.value ? pieOption("DB 检索行数", charts.value.pie5) : {}));

// 从图表数据派生核心指标
function sumSeries(b: BarLineChart): number {
  let total = 0;
  for (const s of b.series) {
    for (const v of s.data) total += Number(v || 0);
  }
  return total;
}
const stats = computed(() => {
  const c = charts.value;
  if (!c) return null;
  return {
    workflowCount: sumSeries(c.bar5),
    onlineCount: sumSeries(c.bar1),
    onlineUsers: c.bar2.x.length,
    slowQueryCount: sumSeries(c.bar3),
  };
});
function fmtNum(v: number | null | undefined): string {
  if (v === null || v === undefined) return "--";
  return v.toLocaleString("en-US");
}
const statCards = computed(() => {
  const s = stats.value;
  return [
    { label: "上线工单", value: s?.workflowCount ?? null, icon: "Document", color: "#16a34a", tint: "rgba(22,163,74,0.10)" },
    { label: "上线 SQL", value: s?.onlineCount ?? null, icon: "CircleCheck", color: "#2563eb", tint: "rgba(37,99,235,0.10)" },
    { label: "上线用户", value: s?.onlineUsers ?? null, icon: "User", color: "#7c3aed", tint: "rgba(124,58,237,0.10)" },
    { label: "慢查询", value: s?.slowQueryCount ?? null, icon: "Timer", color: "#ea580c", tint: "rgba(234,88,12,0.10)" },
  ];
});

// 图表按业务分组：主图跨两列突出
interface ChartCell { opt: EChartsOption | Record<string, unknown>; span?: boolean }
const sections = computed<{ title: string; sub: string; color: string; charts: ChartCell[] }[]>(() => {
  if (!charts.value) return [];
  return [
    {
      title: "SQL 上线分析",
      sub: "工单与上线 SQL 多维统计",
      color: "#16a34a",
      charts: [
        { opt: optBar1.value, span: true },
        { opt: optBar5.value },
        { opt: optBar2.value },
        { opt: optPie1.value },
        { opt: optPie2.value },
      ],
    },
    {
      title: "SQL 查询分析",
      sub: "查询频次与检索行数分布",
      color: "#2563eb",
      charts: [
        { opt: optLine1.value, span: true },
        { opt: optPie4.value },
        { opt: optPie5.value },
      ],
    },
    {
      title: "慢查询分析",
      sub: "慢 SQL 数据库与用户分布",
      color: "#ea580c",
      charts: [
        { opt: optBar3.value },
        { opt: optPie3.value },
      ],
    },
  ];
});

async function loadCharts() {
  if (!dateRange.value || dateRange.value.length < 2) return;
  loading.value = true;
  try {
    charts.value = await fetchDashboardCharts(dateRange.value[0], dateRange.value[1]);
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false;
  }
}

function onDateChange() {
  loadCharts();
}

onMounted(loadCharts);
</script>

<template>
  <div class="dashboard">
    <!-- Hero 欢迎区 -->
    <el-card shadow="never" class="hero">
      <div class="hero-row">
        <div class="hero-greet">
          <h2>{{ greeting }}，{{ auth.displayName || "DBShield" }}</h2>
          <p>欢迎使用 DBShield SQL 审核查询平台</p>
        </div>
        <div class="hero-date">
          <div class="hero-date-day">{{ weekday }}</div>
          <div class="hero-date-text">{{ todayStr }}</div>
        </div>
      </div>
    </el-card>

    <!-- 核心指标 -->
    <div class="stats">
      <el-card v-for="s in statCards" :key="s.label" shadow="hover" class="stat">
        <div class="stat-icon" :style="{ background: s.tint, color: s.color }">
          <el-icon :size="22"><component :is="s.icon" /></el-icon>
        </div>
        <div class="stat-body">
          <div class="stat-value">{{ fmtNum(s.value) }}</div>
          <div class="stat-label">{{ s.label }}</div>
        </div>
      </el-card>
    </div>

    <!-- 快捷入口 -->
    <div class="entries">
      <el-card
        v-for="e in visibleEntries()"
        :key="e.title"
        shadow="hover"
        class="entry"
        @click="go(e)"
      >
        <div class="entry-icon" :style="{ background: e.color }">
          <el-icon :size="24"><component :is="e.icon" /></el-icon>
        </div>
        <div class="entry-body">
          <div class="entry-title">{{ e.title }}</div>
          <div class="entry-desc">{{ e.desc }}</div>
        </div>
      </el-card>
    </div>

    <!-- 图表区 -->
    <el-card shadow="never" class="charts-card" v-loading="loading">
      <div class="charts-head">
        <div>
          <h3>数据图表</h3>
          <p class="charts-sub">SQL 上线 / 查询 / 慢查等多维度统计</p>
        </div>
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          range-separator="-"
          start-placeholder="开始"
          end-placeholder="结束"
          @change="onDateChange"
        />
      </div>

      <div v-for="sec in sections" :key="sec.title" class="section">
        <div class="section-title">
          <span class="section-bar" :style="{ background: sec.color }"></span>
          <h4>{{ sec.title }}</h4>
          <span class="section-sub">{{ sec.sub }}</span>
        </div>
        <div class="charts-grid">
          <div
            v-for="(ch, i) in sec.charts"
            :key="i"
            class="chart-cell"
            :class="{ 'span-2': ch.span }"
          >
            <EChart :option="ch.opt" height="320px" />
          </div>
        </div>
      </div>

      <el-empty v-if="!loading && !charts" description="暂无数据" />
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.dashboard {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* ---------------- Hero ---------------- */
.hero {
  background: linear-gradient(120deg, #1e3a8a 0%, #2563eb 100%);
  border: none;
  border-radius: 12px;
  color: #fff;
  overflow: hidden;
  :deep(.el-card__body) {
    padding: 28px 32px;
  }
  .hero-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 24px;
    flex-wrap: wrap;
  }
  .hero-greet {
    h2 {
      margin: 0;
      font-size: 24px;
    }
    p {
      margin: 8px 0 0;
      opacity: 0.9;
      font-size: 14px;
    }
  }
  .hero-date {
    text-align: right;
    line-height: 1.3;
    .hero-date-day {
      font-size: 18px;
      font-weight: 600;
    }
    .hero-date-text {
      margin-top: 4px;
      font-size: 13px;
      opacity: 0.85;
    }
  }
}

/* ---------------- 核心指标 ---------------- */
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}
.stat {
  :deep(.el-card__body) {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 18px 20px;
  }
}
.stat-icon {
  width: 44px;
  height: 44px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.stat-value {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.1;
  color: var(--el-text-color-primary);
}
.stat-label {
  margin-top: 4px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

/* ---------------- 快捷入口 ---------------- */
.entries {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}
.entry {
  cursor: pointer;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
  &:hover {
    transform: translateY(-2px);
  }
  :deep(.el-card__body) {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 20px;
  }
}
.entry-icon {
  width: 48px;
  height: 48px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  flex-shrink: 0;
}
.entry-title {
  font-size: 16px;
  font-weight: 600;
}
.entry-desc {
  margin-top: 4px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

/* ---------------- 图表区 ---------------- */
.charts-card {
  h3 {
    margin: 0;
    font-size: 16px;
  }
  .charts-sub {
    margin: 4px 0 0;
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }
}
.charts-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  flex-wrap: wrap;
  gap: 12px;
}

.section {
  margin-top: 8px;
  & + .section {
    margin-top: 24px;
  }
}
.section-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  .section-bar {
    width: 4px;
    height: 16px;
    border-radius: 2px;
  }
  h4 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
  }
  .section-sub {
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }
}

.charts-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}
.chart-cell {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 8px 8px 0;
}
.chart-cell.span-2 {
  grid-column: span 2;
}

/* ---------------- 响应式 ---------------- */
@media (max-width: 1200px) {
  .stats {
    grid-template-columns: repeat(2, 1fr);
  }
  .entries {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 900px) {
  .charts-grid {
    grid-template-columns: 1fr;
  }
  .chart-cell.span-2 {
    grid-column: span 1;
  }
  .entries {
    grid-template-columns: 1fr;
  }
}
</style>

<!-- 非 scoped 全局样式：日期选择框宽度。
     Element Plus 内部用 .el-date-editor.el-input__wrapper { width: var(--el-date-editor-width) }
     且 .el-date-editor--daterange 把该变量设为 350px。
     scoped 下的 :deep 受父选择器属性约束，多次尝试难以稳定覆盖，
     这里用全局块 + 父级 .charts-head 前缀 + 变量覆盖，确定性生效。 -->
<style lang="scss">
.charts-head {
  .el-date-editor--daterange {
    --el-date-editor-width: 260px;
    width: 260px;
  }
}
</style>
