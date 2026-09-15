import { defineStore } from "pinia";
import { ref } from "vue";
import { ElMessage } from "element-plus";
import {
  optimizeSqlByAIAsync,
  pollOptimizeTask,
  type AiOptimizeStep,
} from "@/api/phase2";

const POLL_INTERVAL_MS = 3000;
// 单轮轮询时长上限：与后端 Agent 预算（240s）+ 余量对齐
const POLL_TIMEOUT_MS = 300000;

/**
 * AI 优化异步任务状态（模块级内存态，独立于页面组件生命周期）：
 * - 轮询在 store 中进行，切换菜单不中断，完成后全局 toast，回到优化工具页报告仍在；
 * - 不做任何前端持久化：刷新页面/退出登录后状态即清空。在途任务仍会在服务端
 *   跑完并写入 24h 报告缓存，重新提交同一 SQL 立即命中缓存返回，不重复消耗 AI 用量；
 * - 登出时由 auth store 调用 reset()，防止同标签页换账号后看到上一个人的报告。
 */
export const useAiOptimizeStore = defineStore("aiOptimize", () => {
  const taskId = ref<number | null>(null);
  const polling = ref(false);
  const report = ref("");
  const steps = ref<AiOptimizeStep[]>([]);
  const error = ref("");

  let pollTimer: number | null = null;

  function _stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    polling.value = false;
  }

  function _poll(id: number) {
    polling.value = true;
    const startedAt = Date.now();
    pollTimer = window.setInterval(async () => {
      try {
        const t = await pollOptimizeTask(id);
        if (t?.status === "success") {
          report.value = t.report || "";
          steps.value = t.steps || [];
          _stopPolling();
          ElMessage.success("AI 优化报告已完成");
        } else if (t?.status === "failed") {
          _stopPolling();
          taskId.value = null;
          error.value = t.error || "AI 优化失败，请稍后重试";
          ElMessage.error(error.value);
        } else if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
          _stopPolling();
          taskId.value = null;
          error.value =
            "AI 优化任务轮询超时，可重新执行同一 SQL（命中服务端缓存立即返回）";
          ElMessage.warning(error.value);
        }
      } catch {
        // 任务不存在/无权查看/网络错误（拦截器已提示）：终止轮询
        _stopPolling();
        taskId.value = null;
      }
    }, POLL_INTERVAL_MS);
  }

  /** 提交 AI 优化任务（缓存命中直接回填报告）；展示由页面对 report 的 watch 接管 */
  async function submit(params: {
    instance_name: string;
    db_name: string;
    sql_content: string;
  }): Promise<void> {
    _stopPolling();
    taskId.value = null;
    report.value = "";
    steps.value = [];
    error.value = "";
    const submitted = await optimizeSqlByAIAsync(params);
    if (submitted?.hit_cache) {
      report.value = submitted.report || "";
      steps.value = submitted.steps || [];
      return;
    }
    const id = submitted?.task_id;
    if (!id) return;
    taskId.value = id;
    _poll(id);
  }

  /** 清空全部任务状态（登出/切换账号时调用，防止跨账号残留展示） */
  function reset(): void {
    _stopPolling();
    taskId.value = null;
    report.value = "";
    steps.value = [];
    error.value = "";
  }

  return { taskId, polling, report, steps, error, submit, reset };
});
