import axios, {
  type AxiosInstance,
  type InternalAxiosRequestConfig,
} from "axios";
import { ElMessage } from "element-plus";
import { getCookie } from "./auth";

declare module "axios" {
  export interface AxiosRequestConfig {
    /** 静默请求：失败时不弹全局错误提示，由调用方自行处理（如轮询） */
    silent?: boolean;
  }
}

/** 需要 CSRF 校验的 HTTP 方法 */
const CSRF_METHODS = ["post", "put", "patch", "delete"];

const service: AxiosInstance = axios.create({
  baseURL: "/",
  timeout: 60000,
  withCredentials: true,
});

// 请求拦截：为不安全方法注入 X-CSRFToken（从 csrftoken cookie 读取）
service.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const method = (config.method || "get").toLowerCase();
  if (CSRF_METHODS.includes(method)) {
    const token = getCookie("csrftoken");
    if (token) {
      config.headers.set("X-CSRFToken", token);
    }
  }
  return config;
});

// 401 处理回调（由 main.ts 注入，避免循环依赖 router）
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

// 显式探测登录态（如 /me）时，抑制自动跳转，由调用方自行处理
let suppressAuthRedirect = false;
export function withAuthSuppressed<T>(fn: () => Promise<T>): Promise<T> {
  suppressAuthRedirect = true;
  return fn().finally(() => {
    suppressAuthRedirect = false;
  });
}

// 响应拦截：统一错误处理
service.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status;
    const data = error?.response?.data;
    // 静默请求（轮询等）：错误交给调用方处理，不弹全局提示
    const silent = error?.config?.silent === true;

    if (status === 401) {
      if (!suppressAuthRedirect) onUnauthorized?.();
      return Promise.reject(error);
    }

    // 无响应：超时/断网/后端未起。以前这里静默失败，页面只看到 loading 一直转，
    // 用户以为"没反应"，必须明确提示
    if (!error?.response) {
      if (!silent) {
        const timedOut =
          error?.code === "ECONNABORTED" || /timeout/i.test(error?.message || "");
        ElMessage.error(
          timedOut
            ? "请求超时：服务端可能仍在处理，请稍后确认结果或改用异步方式"
            : "网络异常或后端服务不可用，请稍后重试"
        );
      }
      return Promise.reject(error);
    }

    // 尝试从旧信封 {status,msg} 或 DRF {detail}/{errors} 提取提示信息
    // errors：DRF ValidationError({"errors": "..."})（如 SQL 检测 GoInception 报错）
    let msg = "";
    if (typeof data === "string") msg = data;
    else if (data?.msg) msg = data.msg;
    else if (data?.detail) msg = data.detail;
    else if (data?.message) msg = data.message;
    else if (data?.errors) {
      // errors 可能是字符串，也可能是「字段 -> 错误数组」的 DRF 校验结构
      const errs = data.errors;
      msg = typeof errs === "string" ? errs : JSON.stringify(errs);
    }
    if (silent) return Promise.reject(error);
    // 防御：当响应体是 HTML（Django 异常页/登录重定向页等）或超长内容时，
    // 不要把整段原文弹到页面，改用通用提示
    if (msg) {
      const looksLikeHtml = /^\s*</.test(msg) || /<!doctype/i.test(msg);
      const tooLong = msg.length > 200;
      if (looksLikeHtml || tooLong) {
        msg = `服务器错误（${status ?? "未知状态"}），请稍后重试或联系管理员`;
      }
      ElMessage.error(msg);
    } else if (status && status >= 500) {
      ElMessage.error(`服务器错误（${status}），请稍后重试或联系管理员`);
    }

    return Promise.reject(error);
  }
);

/** 旧版 {status, msg, data} 信封业务态判定：status 非 0 抛错并提示 */
export interface LegacyEnvelope<T = unknown> {
  status: number;
  msg: string;
  data: T;
}

export function unwrapLegacy<T = unknown>(env: LegacyEnvelope<T>): T {
  if (env.status !== 0) {
    ElMessage.error(env.msg || "操作失败");
    throw new Error(env.msg || "operation failed");
  }
  return env.data;
}

/** 旧版页面前缀：开发期直连后端，生产期为空（同源相对路径） */
export const legacyBase = import.meta.env.VITE_LEGACY_BASE ?? "";

export default service;
