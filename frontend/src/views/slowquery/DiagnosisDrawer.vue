<script setup lang="ts">
import { ref, watch, computed, onUnmounted } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { marked } from "marked";
import DOMPurify from "dompurify";
import {
  triggerDiagnosis,
  pollDiagnosisTask,
  getExistingDiagnosis,
  submitDiagnosisFeedback,
  generateWorkflowDraft,
  fetchSlowTrendV2,
  type DiagnosisReport,
} from "@/api/phase2";
import { useBinlogHandoffStore } from "@/stores/binlogHandoff";
import EChart from "@/components/EChart.vue";
import type { EChartsOption } from "echarts";

marked.setOptions({ gfm: true, breaks: false });

const props = defineProps<{
  visible: boolean;
  instanceName: string;
  dbName: string;
  sqlHash: string;
  sqlText: string;
}>();

const emit = defineEmits<{
  "update:visible": [val: boolean];
  // 诊断完成（缓存命中 / 轮询成功）后通知父组件刷新已诊断状态
  diagnosed: [sqlHash: string];
}>();

const router = useRouter();
const handoff = useBinlogHandoffStore();

// ---- 状态 ----
const loading = ref(false);
const taskStatus = ref<"idle" | "pending" | "running" | "success" | "failed">("idle");
const errorMsg = ref("");
const report = ref<DiagnosisReport | null>(null);
// 报告就绪（缓存命中 / 轮询成功）时通知父组件把该指纹标记为"已诊断"
watch(
  () => report.value,
  (val) => {
    if (val) emit("diagnosed", props.sqlHash);
  }
);
const taskId = ref<number | null>(null);
const progressText = ref("");
// 后端上报的阶段进度：
//   collecting / collecting_trend / collecting_ddl / collecting_explain / analyzing / saving
type ProgressStage =
  | ""
  | "collecting"
  | "collecting_trend"
  | "collecting_ddl"
  | "collecting_explain"
  | "analyzing"
  | "saving";
const progressStage = ref<ProgressStage>("");
// 已等待秒数（转圈时显示，缓解焦虑）
const elapsedSeconds = ref(0);
let elapsedTimer: ReturnType<typeof setInterval> | null = null;

// 反馈
const feedbackSubmitted = ref(false);
const feedbackHelpful = ref(false);

// 趋势迷你图
const trendOption = ref<Record<string, unknown>>({});

// 轮询定时器
let pollTimer: ReturnType<typeof setInterval> | null = null;
// 轮询上限：按 2s/4s 动态间隔估算 300s 上限，避免任务异常时无限请求
const MAX_POLL_ATTEMPTS = 100;
let pollAttempts = 0;
// 连续轮询失败上限（网络异常时停止）
const MAX_POLL_FAILURES = 5;
let pollFailures = 0;

// ---- 计算属性 ----

/** 当前阶段文案 */
const stageText = computed(() => {
  switch (progressStage.value) {
    case "collecting":
      return "正在拉取慢查统计指标";
    case "collecting_trend":
      return "正在采集近期趋势";
    case "collecting_ddl":
      return "正在获取相关表结构";
    case "collecting_explain":
      return "正在执行 EXPLAIN 分析";
    case "analyzing":
      return "AI 正在分析根因并生成诊断结论";
    case "saving":
      return "正在生成诊断报告";
    default:
      return progressText.value || "任务已提交，等待后台执行…";
  }
});

/** 等待时间文案 */
const elapsedText = computed(() => {
  const s = elapsedSeconds.value;
  if (s < 60) return `已等待 ${s} 秒`;
  const m = Math.floor(s / 60);
  return `已等待 ${m} 分 ${s % 60} 秒`;
});

/** 诊断阶段配置（纵向时间线） */
const STAGE_DEFS = [
  {
    key: "collecting",
    label: "采集上下文",
    icon: "DataLine",
    sub: "统计、趋势、表结构、执行计划",
    items: [
      { key: "stats", label: "拉取慢查统计指标" },
      { key: "trend", label: "采集近期趋势" },
      { key: "ddl", label: "获取相关表结构" },
      { key: "explain", label: "执行 EXPLAIN 分析" },
    ],
  },
  { key: "analyzing", label: "AI 根因分析", icon: "MagicStick", sub: "聚合上下文生成诊断结论" },
  { key: "saving", label: "生成报告", icon: "DocumentChecked", sub: "落库并展示诊断结果" },
];

// progress 值 → 采集阶段已完成的子步骤数（stats/trend/ddl/explain）
const PROGRESS_SUB_DONE: Record<ProgressStage, number> = {
  "": 0,
  collecting: 0,
  collecting_trend: 1,
  collecting_ddl: 2,
  collecting_explain: 3,
  analyzing: 4,
  saving: 4,
};

interface DiagStageItem {
  key: string;
  label: string;
  done: boolean;
  active: boolean;
}

interface DiagStage {
  key: string;
  label: string;
  icon: string;
  sub: string;
  done: boolean;
  active: boolean;
  items?: DiagStageItem[];
}

/** 各阶段/子步骤状态（用于诊断期间时间线展示） */
const phaseStages = computed<DiagStage[]>(() => {
  const p = progressStage.value;
  const subDone = PROGRESS_SUB_DONE[p] ?? 0;
  const collectingActive = p.startsWith("collecting");
  const analyzingActive = p === "analyzing";
  const savingActive = p === "saving";
  return STAGE_DEFS.map((def) => {
    const key = def.key;
    const done =
      key === "collecting"
        ? analyzingActive || savingActive
        : key === "analyzing"
          ? savingActive
          : false;
    const active =
      key === "collecting"
        ? collectingActive
        : key === "analyzing"
          ? analyzingActive
          : savingActive;
    const items = def.items
      ? def.items.map((it, idx) => ({
          key: it.key,
          label: it.label,
          done: idx < subDone,
          active: idx === subDone && collectingActive,
        }))
      : undefined;
    return { ...def, done, active, items };
  });
});

const severityTagType = computed(() => {
  const s = report.value?.severity;
  if (s === "high") return "danger";
  if (s === "medium") return "warning";
  if (s === "low") return "success";
  return "info";
});

const severityLabel = computed(() => {
  const s = report.value?.severity;
  if (s === "high") return "高危";
  if (s === "medium") return "中危";
  if (s === "low") return "低危";
  return "未知";
});

const bottleneckLabel = computed(() => {
  const map: Record<string, string> = {
    full_scan: "全表扫描",
    missing_index: "缺索引",
    lock_wait: "锁等待",
    filesort: "文件排序",
    tmp_table: "临时表",
    type_cast: "类型转换",
    other: "其他",
  };
  return map[report.value?.bottleneck_type || "other"] || "其他";
});

const renderedMarkdown = computed(() => {
  if (!report.value?.report_markdown) return "";
  const raw = marked.parse(report.value.report_markdown, { async: false }) as string;
  return DOMPurify.sanitize(raw);
});

// ---- 方法 ----

function closeDrawer() {
  emit("update:visible", false);
  stopPolling();
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

function startElapsedTimer() {
  if (elapsedTimer) clearInterval(elapsedTimer);
  elapsedSeconds.value = 0;
  elapsedTimer = setInterval(() => {
    elapsedSeconds.value += 1;
  }, 1000);
}

function startPolling(id: number) {
  stopPolling();
  pollAttempts = 0;
  pollFailures = 0;
  startElapsedTimer();
  // 间隔自适应：前 30s 每 2s，之后每 4s，减少无效请求
  let intervalMs = 2000;
  pollTimer = setInterval(async () => {
    pollAttempts += 1;
    // 轮询超时保护：任务异常卡住时停止，避免无限请求后台接口
    if (pollAttempts > MAX_POLL_ATTEMPTS) {
      stopPolling();
      loading.value = false;
      taskStatus.value = "idle";
      progressText.value = "诊断仍在后台执行（已超时），请稍后重新打开抽屉查看结果";
      ElMessage.warning("诊断仍在后台执行，请稍后重新打开抽屉查看结果");
      return;
    }
    // 超过 30s（15 次）后放慢轮询
    if (pollAttempts === 15 && intervalMs === 2000) {
      if (pollTimer) clearInterval(pollTimer);
      intervalMs = 4000;
      pollTimer = setInterval(pollTick, intervalMs);
      return;
    }
    await pollTick();
  }, intervalMs);

  async function pollTick() {
    try {
      const data = await pollDiagnosisTask(id);
      if (!data) return;
      taskStatus.value = data.status as typeof taskStatus.value;
      // 同步后端上报的阶段进度
      if (data.progress) {
        progressStage.value = data.progress as typeof progressStage.value;
      }
      if (data.status === "success") {
        if (data.report) {
          report.value = data.report;
          loadTrendChart();
        }
        stopPolling();
        loading.value = false;
      } else if (data.status === "failed") {
        errorMsg.value = data.error || "诊断失败";
        stopPolling();
        loading.value = false;
      } else {
        // pending / running：阶段文案由 progressStage 计算属性生成
        progressText.value =
          data.status === "running" ? stageText.value : "任务已提交，等待后台执行…";
      }
      pollFailures = 0;
    } catch (e) {
      console.error("轮询诊断任务失败", e);
      pollFailures += 1;
      if (pollFailures >= MAX_POLL_FAILURES) {
        stopPolling();
        loading.value = false;
        taskStatus.value = "idle";
        progressText.value = "轮询诊断任务失败，请重试";
        ElMessage.error("轮询诊断任务失败，请重试");
      }
    }
  }
}

async function startDiagnosis(force = false) {
  if (!props.instanceName || !props.sqlHash) return;

  loading.value = true;
  taskStatus.value = "pending";
  errorMsg.value = "";
  report.value = null;
  feedbackSubmitted.value = false;
  progressText.value = "正在提交诊断任务…";

  try {
    // 先查是否有已有报告（非 force 时）
    if (!force) {
      const existing = await getExistingDiagnosis({
        instance_name: props.instanceName,
        db_name: props.dbName,
        sql_hash: props.sqlHash,
      });
      if (existing?.report) {
        report.value = existing.report;
        taskStatus.value = "success";
        taskId.value = existing.task_id;
        loading.value = false;
        loadTrendChart();
        return;
      }
      if (existing?.task_id && existing.status) {
        // 有进行中的任务，直接轮询
        taskId.value = existing.task_id;
        taskStatus.value = existing.status as typeof taskStatus.value;
        startPolling(existing.task_id);
        return;
      }
    }

    // 触发新诊断
    const result = await triggerDiagnosis({
      instance_name: props.instanceName,
      db_name: props.dbName,
      sql_hash: props.sqlHash,
      force,
    });

    if (!result) {
      errorMsg.value = "诊断请求失败";
      taskStatus.value = "failed";
      loading.value = false;
      return;
    }

    taskId.value = result.task_id;

    // 命中缓存
    if (result.hit_cache && result.report) {
      report.value = result.report;
      taskStatus.value = "success";
      loading.value = false;
      loadTrendChart();
      return;
    }

    // 开始轮询
    startPolling(result.task_id);
  } catch (e) {
    errorMsg.value = (e as Error).message || "诊断请求失败";
    taskStatus.value = "failed";
    loading.value = false;
  }
}

async function retryDiagnosis() {
  await startDiagnosis(true);
}

async function loadTrendChart() {
  if (!props.instanceName || !props.sqlHash) return;
  try {
    const r = await fetchSlowTrendV2({
      instance_name: props.instanceName,
      sql_hash: props.sqlHash,
      days: 14,
    });
    const data = r.data || [];
    if (!data.length) return;
    const dates = data.map((d) => d.date);
    const avgTimes = data.map((d) => d.avg_time);
    const maxTimes = data.map((d) => d.max_time);
    trendOption.value = {
      title: { text: "近14天趋势", left: "center", textStyle: { fontSize: 13 } },
      tooltip: { trigger: "axis" },
      legend: { top: 24, data: ["平均耗时", "最大耗时"] },
      grid: { left: 48, right: 24, top: 56, bottom: 32, containLabel: true },
      xAxis: { type: "category", data: dates, axisLabel: { rotate: dates.length > 7 ? 45 : 0 } },
      yAxis: { type: "value", name: "秒" },
      series: [
        { name: "平均耗时", type: "line", smooth: true, areaStyle: { opacity: 0.2 }, data: avgTimes },
        { name: "最大耗时", type: "line", smooth: true, areaStyle: { opacity: 0.2 }, data: maxTimes },
      ],
    } as EChartsOption;
  } catch {
    // 趋势加载失败不影响报告展示
  }
}

async function copyToClipboard(text: string, label = "内容") {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    ElMessage.success(`${label}已复制到剪贴板`);
  } catch {
    ElMessage.warning("复制失败，请手动复制");
  }
}

async function onGenerateWorkflow(suggestionIndex: number) {
  if (!report.value) return;
  try {
    const result = await generateWorkflowDraft({
      report_id: report.value.id,
      suggestion_index: suggestionIndex,
    });
    if (!result) return;

    // 通过 handoff store 传递 SQL 到工单提交页
    handoff.set({
      workflow_name: `[AI诊断] ${report.value.root_cause.slice(0, 30)}`,
      sql_content: result.sql,
      instance_name: props.instanceName,
      db_name: props.dbName,
    });

    ElMessage.success("工单草稿已生成，即将跳转到工单提交页面");
    closeDrawer();
    router.push({ name: "sqlworkflow-submit" });
  } catch (e) {
    ElMessage.error(`生成工单草稿失败: ${(e as Error).message}`);
  }
}

async function onFeedback(helpful: boolean) {
  if (!report.value) return;
  try {
    let reason = "";
    if (!helpful) {
      const { value } = await ElMessageBox.prompt(
        "请描述问题（可选）",
        "反馈",
        { inputType: "textarea", confirmButtonText: "提交", cancelButtonText: "取消" }
      );
      reason = value || "";
    }
    await submitDiagnosisFeedback(report.value.id, { helpful, reason });
    feedbackSubmitted.value = true;
    feedbackHelpful.value = helpful;
    ElMessage.success("感谢反馈！");
  } catch {
    // 用户取消或提交失败
  }
}

// ---- 生命周期 ----

watch(
  () => props.visible,
  (val) => {
    if (val) {
      startDiagnosis(false);
    } else {
      stopPolling();
      // 重置状态
      report.value = null;
      taskStatus.value = "idle";
      errorMsg.value = "";
      progressText.value = "";
      progressStage.value = "";
      elapsedSeconds.value = 0;
      trendOption.value = {};
    }
  }
);

onUnmounted(() => {
  stopPolling();
});
</script>

<template>
  <el-drawer
    :model-value="visible"
    title="AI 慢查根因诊断"
    direction="rtl"
    size="720px"
    @update:model-value="closeDrawer"
  >
    <!-- SQL 摘要 -->
    <div class="sql-summary">
      <div class="sql-summary-label">诊断 SQL:</div>
      <div class="sql-summary-text" :title="sqlText">
        {{ sqlText.slice(0, 200) }}{{ sqlText.length > 200 ? "..." : "" }}
      </div>
    </div>

    <!-- 加载中：诊断进度时间线 + 等待计时 -->
    <div
      v-if="loading || taskStatus === 'pending' || taskStatus === 'running'"
      class="diagnosis-loading"
    >
      <!-- 背景光效 -->
      <div class="diag-glow"></div>

      <div class="diag-panel">
        <!-- 头部：状态 + 计时 -->
        <div class="diag-head">
          <div class="diag-title">
            <span class="live-dot"></span>
            <span>AI 诊断进行中</span>
          </div>
          <div class="diag-elapsed">
            <el-icon><Timer /></el-icon>
            <span>{{ elapsedText }}</span>
          </div>
        </div>

        <!-- 阶段时间线 -->
        <div class="diag-timeline">
          <div
            v-for="stage in phaseStages"
            :key="stage.key"
            class="diag-stage"
            :class="{ done: stage.done, active: stage.active }"
          >
            <div class="stage-left">
              <div class="stage-icon">
                <el-icon v-if="stage.done" class="stage-icon-ok"><CircleCheckFilled /></el-icon>
                <el-icon v-else class="stage-icon-spin"><component :is="stage.icon" /></el-icon>
              </div>
              <div class="stage-line" :class="{ lit: stage.done || stage.active }"></div>
            </div>
            <div class="stage-body">
              <div class="stage-title-row">
                <span class="stage-label">{{ stage.label }}</span>
                <span class="stage-status" :class="{ on: stage.active, ok: stage.done }">
                  {{ stage.done ? "完成" : stage.active ? "进行中" : "等待" }}
                </span>
              </div>
              <div class="stage-sub">{{ stage.active ? stageText : stage.sub }}</div>

              <!-- 采集阶段的子步骤清单 -->
              <ul v-if="stage.items" class="stage-items">
                <li
                  v-for="item in stage.items"
                  :key="item.key"
                  :class="{ done: item.done, active: item.active }"
                >
                  <el-icon v-if="item.done" class="item-ok"><CircleCheck /></el-icon>
                  <span v-else class="item-dot" :class="{ on: item.active }"></span>
                  <span class="item-label">{{ item.label }}</span>
                  <span v-if="item.active" class="item-running">…</span>
                </li>
              </ul>
            </div>
          </div>
        </div>

        <!-- 当前阶段提示 -->
        <div class="diag-hint">
          <el-icon><InfoFilled /></el-icon>
          <span>后台分析通常需 10-40 秒，请勿关闭抽屉</span>
        </div>
      </div>
    </div>

    <!-- 失败 -->
    <div v-else-if="taskStatus === 'failed'" class="diagnosis-error">
      <el-result icon="error" title="诊断失败" :sub-title="errorMsg">
        <template #extra>
          <el-button type="primary" @click="retryDiagnosis">重试</el-button>
        </template>
      </el-result>
    </div>

    <!-- 成功：报告展示 -->
    <div v-else-if="report" class="diagnosis-report">
      <!-- 结构化卡片 -->
      <el-card shadow="never" class="report-card">
        <div class="report-header">
          <el-tag :type="severityTagType" size="large" effect="dark">
            {{ severityLabel }}
          </el-tag>
          <el-tag type="info" size="large">{{ bottleneckLabel }}</el-tag>
          <span v-if="report.confidence > 0" class="confidence">
            置信度 {{ (report.confidence * 100).toFixed(0) }}%
          </span>
        </div>
        <div class="root-cause">{{ report.root_cause }}</div>

        <!-- 证据列表 -->
        <div v-if="report.evidence?.length" class="evidence-section">
          <div class="section-title">证据</div>
          <ul class="evidence-list">
            <li v-for="(e, i) in report.evidence" :key="i">{{ e }}</li>
          </ul>
        </div>
      </el-card>

      <!-- 趋势迷你图 -->
      <el-card v-if="Object.keys(trendOption).length" shadow="never" class="trend-card">
        <EChart :option="trendOption" height="200px" />
      </el-card>

      <!-- 优化建议 -->
      <div v-if="report.suggestions?.length" class="suggestions-section">
        <div class="section-title">优化建议</div>
        <el-card
          v-for="(s, i) in report.suggestions"
          :key="i"
          shadow="never"
          class="suggestion-card"
        >
          <div class="suggestion-header">
            <el-tag size="small" :type="s.type === 'index_ddl' ? 'warning' : s.type === 'rewrite' ? 'success' : 'info'">
              {{ s.type === "index_ddl" ? "索引建议" : s.type === "rewrite" ? "SQL 改写" : s.type === "config" ? "配置建议" : s.type }}
            </el-tag>
            <span class="suggestion-desc">{{ s.desc }}</span>
          </div>

          <!-- 索引 DDL -->
          <div v-if="s.index_ddl" class="suggestion-code">
            <div class="code-header">
              <span>建议 DDL</span>
              <el-button link size="small" @click="copyToClipboard(s.index_ddl, 'DDL')">复制</el-button>
            </div>
            <pre><code>{{ s.index_ddl }}</code></pre>
          </div>

          <!-- SQL 改写对比 -->
          <div v-if="s.before && s.after" class="suggestion-diff">
            <div class="diff-block">
              <div class="diff-label">改写前</div>
              <pre class="diff-before"><code>{{ s.before }}</code></pre>
            </div>
            <div class="diff-block">
              <div class="diff-label">改写后</div>
              <pre class="diff-after"><code>{{ s.after }}</code></pre>
            </div>
            <el-button link size="small" @click="copyToClipboard(s.after, '改写SQL')">复制改写SQL</el-button>
          </div>

          <!-- 操作按钮 -->
          <div class="suggestion-actions">
            <el-button
              type="primary"
              size="small"
              plain
              @click="onGenerateWorkflow(i)"
            >
              生成工单草稿
            </el-button>
          </div>
        </el-card>
      </div>

      <!-- Markdown 报告 -->
      <div v-if="report.report_markdown" class="markdown-section">
        <div class="section-title">完整报告</div>
        <div class="markdown-body" v-html="renderedMarkdown"></div>
      </div>

      <!-- 反馈 -->
      <div class="feedback-section">
        <template v-if="!feedbackSubmitted">
          <span class="feedback-label">这份诊断报告有帮助吗？</span>
          <el-button link @click="onFeedback(true)">👍 有帮助</el-button>
          <el-button link @click="onFeedback(false)">👎 需改进</el-button>
        </template>
        <span v-else class="feedback-done">
          {{ feedbackHelpful ? "感谢您的反馈！" : "感谢反馈，我们会持续改进" }}
        </span>
      </div>

      <!-- 元信息 -->
      <div class="report-meta">
        <span>模型: {{ report.model || "N/A" }}</span>
        <span>生成时间: {{ report.created_at }}</span>
        <el-button link size="small" @click="retryDiagnosis">重新诊断</el-button>
      </div>
    </div>

    <!-- 空状态 -->
    <el-empty v-else description="点击下方按钮开始 AI 诊断">
      <el-button type="primary" @click="startDiagnosis(true)">开始诊断</el-button>
    </el-empty>
  </el-drawer>
</template>

<style scoped lang="scss">
.sql-summary {
  margin-bottom: 16px;
  padding: 12px;
  background: var(--el-fill-color-light);
  border-radius: 8px;

  .sql-summary-label {
    font-size: 12px;
    color: var(--el-text-color-secondary);
    margin-bottom: 4px;
  }

  .sql-summary-text {
    font-family: "JetBrains Mono", "Fira Code", monospace;
    font-size: 13px;
    color: var(--el-text-color-primary);
    word-break: break-all;
    max-height: 60px;
    overflow: hidden;
  }
}

.diagnosis-loading {
  position: relative;
  padding: 28px 8px;
  min-height: 460px;

  // 背景光效：两个径向光斑缓慢呼吸
  .diag-glow {
    position: absolute;
    inset: 0;
    pointer-events: none;
    border-radius: 12px;
    background:
      radial-gradient(560px 240px at 18% 0%, color-mix(in srgb, var(--el-color-primary) 9%, transparent), transparent 62%),
      radial-gradient(480px 220px at 88% 100%, color-mix(in srgb, var(--el-color-success) 7%, transparent), transparent 60%);
    animation: diag-breathe 4s ease-in-out infinite;
  }

  @keyframes diag-breathe {
    0%, 100% { opacity: 0.55; }
    50% { opacity: 1; }
  }

  .diag-panel {
    position: relative;
    max-width: 520px;
    margin: 0 auto;
    padding: 24px 26px 20px;
    background: var(--el-bg-color);
    border: 1px solid var(--el-border-color-light);
    border-radius: 12px;
    box-shadow: 0 10px 34px color-mix(in srgb, var(--el-color-primary) 8%, rgba(0, 0, 0, 0.06));
  }

  // 头部
  .diag-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
  }

  .diag-title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 15px;
    font-weight: 600;
    color: var(--el-text-color-primary);
  }

  .live-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--el-color-primary);
    animation: diag-ping 1.6s ease-out infinite;
  }

  @keyframes diag-ping {
    0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--el-color-primary) 45%, transparent); }
    70% { box-shadow: 0 0 0 10px transparent; }
    100% { box-shadow: 0 0 0 0 transparent; }
  }

  .diag-elapsed {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 3px 12px;
    border-radius: 20px;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    color: var(--el-text-color-secondary);
    background: var(--el-fill-color-light);
  }

  // 时间线
  .diag-timeline {
    display: flex;
    flex-direction: column;
  }

  .diag-stage {
    display: flex;
    gap: 14px;
  }

  .stage-left {
    display: flex;
    flex-direction: column;
    align-items: center;
  }

  .stage-icon {
    width: 36px;
    height: 36px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 17px;
    border-radius: 50%;
    background: var(--el-fill-color-light);
    border: 1px solid var(--el-border-color-lighter);
    color: var(--el-text-color-placeholder);
    transition: all 0.3s;
  }

  .diag-stage.active .stage-icon {
    color: #fff;
    background: var(--el-color-primary);
    border-color: var(--el-color-primary);
    box-shadow: 0 0 0 5px color-mix(in srgb, var(--el-color-primary) 18%, transparent);
  }

  .diag-stage.active .stage-icon-spin {
    animation: diag-spin 1.4s linear infinite;
  }

  @keyframes diag-spin {
    to { transform: rotate(360deg); }
  }

  .diag-stage.done .stage-icon {
    color: #fff;
    background: var(--el-color-success);
    border-color: var(--el-color-success);
  }

  .stage-line {
    width: 2px;
    flex: 1;
    min-height: 30px;
    margin: 4px 0;
    border-radius: 2px;
    background: var(--el-border-color-lighter);
    transition: background 0.4s;
  }

  .stage-line.lit {
    background: color-mix(in srgb, var(--el-color-primary) 45%, var(--el-border-color));
  }

  .diag-stage:last-child .stage-line {
    display: none;
  }

  .stage-body {
    flex: 1;
    padding-bottom: 22px;
  }

  .diag-stage:last-child .stage-body {
    padding-bottom: 4px;
  }

  .stage-title-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .stage-label {
    font-size: 14px;
    font-weight: 600;
    color: var(--el-text-color-primary);
  }

  .stage-status {
    padding: 1px 9px;
    border-radius: 10px;
    font-size: 11px;
    line-height: 17px;
    color: var(--el-text-color-placeholder);
    background: var(--el-fill-color-light);
  }

  .stage-status.on {
    color: var(--el-color-primary);
    background: color-mix(in srgb, var(--el-color-primary) 13%, var(--el-bg-color));
  }

  .stage-status.ok {
    color: var(--el-color-success);
    background: color-mix(in srgb, var(--el-color-success) 13%, var(--el-bg-color));
  }

  .stage-sub {
    margin-top: 3px;
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }

  .diag-stage.active .stage-sub {
    color: var(--el-color-primary);
  }

  // 采集阶段子步骤清单
  .stage-items {
    list-style: none;
    margin: 12px 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 9px;

    li {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--el-text-color-secondary);
      transition: color 0.3s;

      &.done { color: var(--el-text-color-regular); }
      &.active {
        color: var(--el-color-primary);
        font-weight: 500;
      }
    }
  }

  .item-ok {
    color: var(--el-color-success);
    font-size: 14px;
  }

  .item-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--el-border-color);

    &.on {
      background: var(--el-color-primary);
      animation: diag-blink 1s ease-in-out infinite;
    }
  }

  @keyframes diag-blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.2; }
  }

  .item-running {
    font-size: 13px;
    letter-spacing: 1px;
  }

  // 底部提示
  .diag-hint {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 6px;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 12px;
    color: var(--el-text-color-secondary);
    background: var(--el-fill-color-light);

    .el-icon { font-size: 14px; }
  }
}

.diagnosis-error {
  padding: 40px 20px;
}

.diagnosis-report {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.report-card {
  .report-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
  }

  .confidence {
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }

  .root-cause {
    font-size: 16px;
    font-weight: 600;
    color: var(--el-text-color-primary);
    line-height: 1.6;
  }
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 8px;
  padding-left: 8px;
  border-left: 3px solid var(--el-color-primary);
}

.evidence-section {
  margin-top: 12px;

  .evidence-list {
    margin: 0;
    padding-left: 20px;

    li {
      font-size: 13px;
      color: var(--el-text-color-regular);
      line-height: 1.8;
    }
  }
}

.trend-card {
  :deep(.el-card__body) {
    padding: 12px;
  }
}

.suggestion-card {
  margin-bottom: 12px;

  .suggestion-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }

  .suggestion-desc {
    font-size: 13px;
    color: var(--el-text-color-primary);
  }

  .suggestion-code {
    margin: 8px 0;

    .code-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 12px;
      color: var(--el-text-color-secondary);
      margin-bottom: 4px;
    }

    pre {
      margin: 0;
      padding: 10px;
      background: var(--el-fill-color-darker);
      border-radius: 6px;
      overflow-x: auto;

      code {
        font-family: "JetBrains Mono", "Fira Code", monospace;
        font-size: 12px;
        color: var(--el-color-warning);
      }
    }
  }

  .suggestion-diff {
    margin: 8px 0;

    .diff-block {
      margin-bottom: 8px;
    }

    .diff-label {
      font-size: 12px;
      color: var(--el-text-color-secondary);
      margin-bottom: 4px;
    }

    pre {
      margin: 0;
      padding: 10px;
      border-radius: 6px;
      overflow-x: auto;

      code {
        font-family: "JetBrains Mono", "Fira Code", monospace;
        font-size: 12px;
      }
    }

    .diff-before pre {
      background: var(--el-color-danger-light-9);

      code { color: var(--el-color-danger); }
    }

    .diff-after pre {
      background: var(--el-color-success-light-9);

      code { color: var(--el-color-success); }
    }
  }

  .suggestion-actions {
    margin-top: 8px;
  }
}

.markdown-section {
  .markdown-body {
    padding: 16px;
    background: var(--el-fill-color-light);
    border-radius: 8px;
    font-size: 13px;
    line-height: 1.8;

    :deep(h1), :deep(h2), :deep(h3) {
      margin: 12px 0 8px;
      font-weight: 600;
    }

    :deep(pre) {
      padding: 10px;
      background: var(--el-fill-color-darker);
      border-radius: 6px;
      overflow-x: auto;

      code {
        font-family: "JetBrains Mono", "Fira Code", monospace;
        font-size: 12px;
      }
    }

    :deep(table) {
      width: 100%;
      border-collapse: collapse;

      th, td {
        border: 1px solid var(--el-border-color);
        padding: 6px 10px;
      }
    }
  }
}

.feedback-section {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  background: var(--el-fill-color-light);
  border-radius: 8px;

  .feedback-label {
    font-size: 13px;
    color: var(--el-text-color-regular);
  }

  .feedback-done {
    font-size: 13px;
    color: var(--el-color-success);
  }
}

.report-meta {
  display: flex;
  align-items: center;
  gap: 16px;
  padding-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
