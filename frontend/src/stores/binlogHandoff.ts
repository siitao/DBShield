import { defineStore } from "pinia";
import { ref } from "vue";

/**
 * My2SQL → SQL 上线提交页 的跨页 handoff。
 * 旧版用 sessionStorage 传 SQL，SPA 用 Pinia store 更干净（无 URL 长度限制）。
 * my2sql 页点「提交工单」写入 pending，sqlworkflow/Submit.vue 挂载时消费并清空。
 */
export interface My2SqlHandoff {
  workflow_name: string;
  sql_content: string;
  instance_name?: string;
  db_name?: string;
}

export const useBinlogHandoffStore = defineStore("binlogHandoff", () => {
  const pending = ref<My2SqlHandoff | null>(null);

  function set(data: My2SqlHandoff) {
    pending.value = data;
  }

  /** 取出并清空（一次性消费） */
  function consume(): My2SqlHandoff | null {
    const data = pending.value;
    pending.value = null;
    return data;
  }

  /** 清空（登出/切换账号时调用） */
  function reset(): void {
    pending.value = null;
  }

  return { pending, set, consume, reset };
});
