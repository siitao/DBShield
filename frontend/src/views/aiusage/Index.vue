<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import {
  fetchAiUsageSummary,
  fetchAiUsageList,
  capabilityLabel,
  AI_CAPABILITY_LABELS,
  type AiUsageTotals,
  type AiUsageGroupRow,
  type AiUsageRow,
} from "@/api/aiusage";
import EChart from "@/components/EChart.vue";
import type { EChartsOption } from "echarts";

// ---- 总览 ----
const days = ref(7);
const summaryLoading = ref(false);
const totals = ref<AiUsageTotals>({
  calls: 0, cache_hits: 0, failed: 0,
  prompt_tokens: 0, completion_tokens: 0, avg_latency_ms: 0,
});
const byCapability = ref<AiUsageGroupRow[]>([]);
const byDay = ref<AiUsageGroupRow[]>([]);
const byUser = ref<AiUsageGroupRow[]>([]);

const cacheHitPct = computed(() =>
  totals.value.calls ? Math.round((100 * totals.value.cache_hits) / totals.value.calls) : 0
);
const totalTokens = computed(
  () => totals.value.prompt_tokens + totals.value.completion_tokens
);

const dayChartOption = computed<EChartsOption>(() => ({
  tooltip: { trigger: "axis" },
  legend: { data: ["调用量", "prompt tokens", "completion tokens"] },
  grid: { left: 48, right: 24, top: 40, bottom: 32 },
  xAxis: { type: "category", data: byDay.value.map((r) => r.day || "") },
  yAxis: [{ type: "value" }, { type: "value" }],
  series: [
    {
      name: "调用量", type: "bar", yAxisIndex: 0,
      data: byDay.value.map((r) => r.calls),
      itemStyle: { color: "#409eff" },
    },
    {
      name: "prompt tokens", type: "line", yAxisIndex: 1, smooth: true,
      data: byDay.value.map((r) => r.prompt_tokens),
      itemStyle: { color: "#e6a23c" },
    },
    {
      name: "completion tokens", type: "line", yAxisIndex: 1, smooth: true,
      data: byDay.value.map((r) => r.completion_tokens),
      itemStyle: { color: "#67c23a" },
    },
  ],
}));

async function loadSummary() {
  summaryLoading.value = true;
  try {
    const data = await fetchAiUsageSummary({ days: days.value });
    totals.value = data.totals;
    byCapability.value = data.by_capability || [];
    byDay.value = data.by_day || [];
    byUser.value = data.by_user || [];
  } finally {
    summaryLoading.value = false;
  }
}

// ---- 明细 ----
const listLoading = ref(false);
const rows = ref<AiUsageRow[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = 20;
const offset = computed(() => (page.value - 1) * pageSize);
const filterCapability = ref("");
const filterStatus = ref("");
const filterUser = ref("");

const capabilityOptions = Object.keys(AI_CAPABILITY_LABELS);

async function loadList() {
  listLoading.value = true;
  try {
    const data = await fetchAiUsageList({
      limit: pageSize,
      offset: offset.value,
      capability: filterCapability.value || undefined,
      status: filterStatus.value || undefined,
      user_name: filterUser.value || undefined,
    });
    rows.value = data.rows || [];
    total.value = data.total || 0;
  } finally {
    listLoading.value = false;
  }
}

function searchList() {
  page.value = 1;
  loadList();
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n || 0);
}

onMounted(() => {
  loadSummary();
  loadList();
});
</script>

<template>
  <div class="aiusage-page">
    <el-card shadow="never" class="filter-card">
      <el-form :inline="true" @submit.prevent>
        <el-form-item label="统计范围">
          <el-select v-model="days" style="width: 140px" @change="loadSummary">
            <el-option :value="7" label="近 7 天" />
            <el-option :value="14" label="近 14 天" />
            <el-option :value="30" label="近 30 天" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="loadSummary">刷新</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 概览指标 -->
    <div v-loading="summaryLoading" class="stat-row">
      <el-card shadow="never" class="stat-card">
        <div class="stat-value">{{ totals.calls }}</div>
        <div class="stat-label">总调用量</div>
      </el-card>
      <el-card shadow="never" class="stat-card">
        <div class="stat-value">{{ cacheHitPct }}%</div>
        <div class="stat-label">缓存命中率</div>
      </el-card>
      <el-card shadow="never" class="stat-card">
        <div class="stat-value">{{ fmtTokens(totalTokens) }}</div>
        <div class="stat-label">Token 总量</div>
      </el-card>
      <el-card shadow="never" class="stat-card">
        <div class="stat-value">{{ totals.avg_latency_ms }} ms</div>
        <div class="stat-label">平均耗时</div>
      </el-card>
      <el-card shadow="never" class="stat-card">
        <div class="stat-value" :class="{ 'stat-danger': totals.failed > 0 }">
          {{ totals.failed }}
        </div>
        <div class="stat-label">失败次数</div>
      </el-card>
    </div>

    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never" header="按能力">
          <el-table :data="byCapability" size="small">
            <el-table-column label="能力" min-width="140">
              <template #default="{ row }">{{ capabilityLabel(row.capability) }}</template>
            </el-table-column>
            <el-table-column prop="calls" label="调用" width="70" />
            <el-table-column label="命中率" width="80">
              <template #default="{ row }">
                {{ row.calls ? Math.round((100 * row.cache_hits) / row.calls) : 0 }}%
              </template>
            </el-table-column>
            <el-table-column label="Tokens" width="110">
              <template #default="{ row }">
                {{ fmtTokens((row.prompt_tokens || 0) + (row.completion_tokens || 0)) }}
              </template>
            </el-table-column>
            <el-table-column prop="avg_latency_ms" label="均耗时(ms)" width="100" />
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never" header="按用户 Top 10">
          <el-table :data="byUser" size="small">
            <el-table-column prop="user_name" label="用户" min-width="110" />
            <el-table-column prop="calls" label="调用" width="70" />
            <el-table-column label="Tokens" width="110">
              <template #default="{ row }">{{ fmtTokens(row.total_tokens || 0) }}</template>
            </el-table-column>
            <el-table-column prop="avg_latency_ms" label="均耗时(ms)" width="100" />
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" header="每日趋势" class="trend-card">
      <EChart v-if="byDay.length" :option="dayChartOption" height="260px" />
      <el-empty v-else description="暂无数据" :image-size="60" />
    </el-card>

    <!-- 明细 -->
    <el-card shadow="never" header="调用明细" class="detail-card">
      <el-form :inline="true" @submit.prevent>
        <el-form-item label="能力">
          <el-select v-model="filterCapability" clearable style="width: 170px" @change="searchList">
            <el-option
              v-for="key in capabilityOptions"
              :key="key" :value="key" :label="capabilityLabel(key)"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="filterStatus" clearable style="width: 110px" @change="searchList">
            <el-option value="success" label="成功" />
            <el-option value="failed" label="失败" />
          </el-select>
        </el-form-item>
        <el-form-item label="用户">
          <el-input
            v-model="filterUser" placeholder="用户名" clearable style="width: 150px"
            @keyup.enter="searchList" @clear="searchList"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="searchList">查询</el-button>
        </el-form-item>
      </el-form>

      <el-table v-loading="listLoading" :data="rows" size="small">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="created_at" label="时间" width="165" />
        <el-table-column label="能力" min-width="130">
          <template #default="{ row }">{{ capabilityLabel(row.capability) }}</template>
        </el-table-column>
        <el-table-column prop="user_name" label="用户" width="100" show-overflow-tooltip />
        <el-table-column prop="instance_name" label="实例" width="110" show-overflow-tooltip />
        <el-table-column prop="db_name" label="库" width="90" show-overflow-tooltip />
        <el-table-column prop="model" label="模型" width="120" show-overflow-tooltip />
        <el-table-column label="Tokens" width="110">
          <template #default="{ row }">
            {{ row.prompt_tokens }} / {{ row.completion_tokens }}
          </template>
        </el-table-column>
        <el-table-column prop="latency_ms" label="耗时(ms)" width="90" />
        <el-table-column label="缓存" width="70">
          <template #default="{ row }">
            <el-tag v-if="row.cache_hit" size="small" type="success">命中</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
              {{ row.status === "success" ? "成功" : "失败" }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="error" label="错误" min-width="140" show-overflow-tooltip />
      </el-table>

      <el-pagination
        v-model:current-page="page"
        class="pager"
        layout="total, prev, pager, next"
        :total="total"
        :page-size="pageSize"
        @current-change="loadList"
      />
    </el-card>
  </div>
</template>

<style scoped lang="scss">
.aiusage-page {
  padding: 16px;

  .filter-card {
    margin-bottom: 16px;

    :deep(.el-card__body) {
      padding-bottom: 2px;
    }
  }

  .stat-row {
    display: flex;
    gap: 16px;
    margin-bottom: 16px;

    .stat-card {
      flex: 1;
      text-align: center;

      .stat-value {
        font-size: 24px;
        font-weight: 600;
        color: var(--el-text-color-primary);

        &.stat-danger {
          color: var(--el-color-danger);
        }
      }

      .stat-label {
        margin-top: 4px;
        font-size: 13px;
        color: var(--el-text-color-secondary);
      }
    }
  }

  .trend-card {
    margin-top: 16px;
  }

  .detail-card {
    margin-top: 16px;

    .pager {
      margin-top: 12px;
      justify-content: flex-end;
    }
  }
}
</style>
