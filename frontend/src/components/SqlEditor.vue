<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from "vue";
import ace from "ace-builds";
import "ace-builds/src-noconflict/theme-github";
import "ace-builds/src-noconflict/mode-mysql";
import "ace-builds/src-noconflict/mode-pgsql";
import "ace-builds/src-noconflict/mode-sqlserver";
import "ace-builds/src-noconflict/mode-sql";
import "ace-builds/src-noconflict/ext-language_tools";
import { ElMessage } from "element-plus";
import { format } from "sql-formatter";

/** 自动补全数据（库名/schema/表名/列名） */
export interface SqlCompleters {
  databases?: string[];
  schemas?: string[];
  tables?: string[];
  columns?: string[];
}

interface AceCompletion {
  name: string;
  value: string;
  caption: string;
  meta: string;
  score: number;
}

// 可选 Boolean prop 不传时 Vue 会强转为 false，必须用 withDefaults 给出 true 默认值，
// 否则「美化」按钮在未显式传参的页面永远不渲染
const props = withDefaults(
  defineProps<{
    modelValue: string;
    completers?: SqlCompleters;
    dbType?: string;
    /** 是否在 toolbar 显示「美化」按钮（默认 true；查询页把它移到控制栏时传 false） */
    showBeautify?: boolean;
    /** 填满父容器高度（编辑器随父伸缩，需配合父级 flex 布局） */
    fillHeight?: boolean;
  }>(),
  { showBeautify: true }
);
const emit = defineEmits<{ "update:modelValue": [value: string] }>();

const editorEl = ref<HTMLElement>();
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let editor: any = null;
let ro: ResizeObserver | null = null;

// 补全数据闭包：只 addCompleter 一次，watch 时更新数据即可
// （ace 没有 removeCompleter API，避免累积补全器）
let completionData: AceCompletion[] = [];
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const langTools: any =
  (ace as any).require?.("ace/ext/language_tools") ?? null;

// db_type → ace mode（对齐旧版 ace-init.js:167-210）
const modeMap: Record<string, string> = {
  mysql: "ace/mode/mysql",
  mariadb: "ace/mode/mysql",
  pgsql: "ace/mode/pgsql",
  mssql: "ace/mode/sqlserver",
  oracle: "ace/mode/sql",
  mongo: "ace/mode/text",
  redis: "ace/mode/text",
};

function buildCompletions(c?: SqlCompleters): AceCompletion[] {
  if (!c) return [];
  const arr: AceCompletion[] = [];
  const push = (meta: string, items?: string[]) => {
    if (!items) return;
    for (const name of items) {
      arr.push({ name, value: name, caption: name, meta, score: 100 });
    }
  };
  push("database", c.databases);
  push("schema", c.schemas);
  push("table", c.tables);
  push("column", c.columns);
  return arr;
}

onMounted(() => {
  editor = ace.edit(editorEl.value!);
  editor.setOptions({
    mode: modeMap[props.dbType || ""] || "ace/mode/mysql",
    theme: "ace/theme/github",
    fontSize: 13,
    tabSize: 2,
    useSoftTabs: true,
    enableBasicAutocompletion: true,
    enableSnippets: true,
    enableLiveAutocompletion: true,
    showPrintMargin: false,
  });
  editor.setValue(props.modelValue || "", -1);
  editor.on("change", () => {
    emit("update:modelValue", editor.getValue());
  });
  // 注入自定义补全（仅一次，靠闭包数据更新）
  if (langTools && langTools.addCompleter) {
    langTools.addCompleter({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      getCompletions: (
        _e: any,
        _s: any,
        _pos: any,
        prefix: string,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        callback: any
      ) => {
        if (!prefix || prefix.length === 0) return callback(null, []);
        return callback(null, completionData);
      },
    });
  }
  // 容器尺寸变化时让 ace 自适应（fillHeight 模式下随右栏等高伸缩）
  if (editorEl.value && typeof ResizeObserver !== "undefined") {
    ro = new ResizeObserver(() => editor?.resize());
    ro.observe(editorEl.value);
  }
});

watch(
  () => props.modelValue,
  (v) => {
    if (editor && editor.getValue() !== v) {
      editor.setValue(v || "", -1);
    }
  }
);

watch(
  () => props.completers,
  (c) => {
    completionData = buildCompletions(c);
  },
  { deep: true, immediate: true }
);

watch(
  () => props.dbType,
  (t) => {
    if (editor && t && modeMap[t]) {
      editor.session.setMode(modeMap[t]);
    }
  }
);

onBeforeUnmount(() => {
  ro?.disconnect();
  ro = null;
  editor?.destroy();
  editor = null;
});

function beautify() {
  if (!editor) return;
  try {
    const formatted = format(editor.getValue());
    editor.setValue(formatted, -1);
  } catch {
    ElMessage.error("SQL 格式化失败");
  }
}

function onUpload(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    ElMessage.warning("文件不能超过 10MB");
    input.value = "";
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const text = String(reader.result || "");
    if (editor) {
      editor.setValue(editor.getValue() + text, -1);
    }
  };
  reader.readAsText(file);
  input.value = "";
}

defineExpose({
  beautify,
  getSelection: () =>
    editor ? editor.session.getTextRange(editor.getSelectionRange()) : "",
  getValue: () => (editor ? editor.getValue() : ""),
  insert: (text: string) => editor?.insert(text),
});
</script>

<template>
  <div class="sql-editor-wrap" :class="{ fill: fillHeight }">
    <div class="toolbar">
      <el-button v-if="showBeautify !== false" size="small" @click="beautify">
        美化
      </el-button>
      <label class="upload-btn">
        <input type="file" accept=".sql" hidden @change="onUpload" />
        <el-button size="small" tag="span">上传 SQL 文件</el-button>
      </label>
      <span class="upload-hint">仅 .sql 文本文件，≤10MB，超限将拒绝导入</span>
    </div>
    <div ref="editorEl" class="editor"></div>
  </div>
</template>

<style scoped lang="scss">
.sql-editor-wrap {
  width: 100%;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  overflow: hidden;
}

.toolbar {
  padding: 6px 8px;
  border-bottom: 1px solid var(--el-border-color);
  background: var(--el-fill-color-light);
  display: flex;
  gap: 8px;
  align-items: center;
}

.upload-hint {
  margin-left: auto;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.editor {
  height: 300px;
  width: 100%;
}

.sql-editor-wrap.fill {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.sql-editor-wrap.fill .editor {
  flex: 1;
  height: auto;
  min-height: 0;
}

.upload-btn {
  display: inline-flex;
  cursor: pointer;
}
</style>
