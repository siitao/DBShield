import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import AutoImport from "unplugin-auto-import/vite";
import Components from "unplugin-vue-components/vite";
import { ElementPlusResolver } from "unplugin-vue-components/resolvers";
import { fileURLToPath, URL } from "node:url";

// 后端 Django 地址（开发期）。生产期 SPA 与 API 同源，无需代理。
const apiTarget = process.env.DBSHIELD_API_TARGET || "http://localhost:8000";

// 仅代理 API 与少量非 /api 的接口。
// 注意：不要代理与 SPA 路由同前缀的路径（如 /dashboard、/instance、/login），
// 否则刷新页面会被转发到 Django 而非返回 index.html。
// /admin、/static 是 Django 侧路径（管理后台页与其静态资源），不在 SPA 路由表内，
// 走代理可保证未登录时 CheckLoginMiddleware 的 302 /login/ 落回本 SPA 登录页。
const proxy = {
  // 所有 DRF 接口（含登录/登出/配置/导出/回滚等，均已迁入 /api/v1/）
  "/api": { target: apiTarget, changeOrigin: true },
  "/jsi18n": { target: apiTarget, changeOrigin: true },
  // Django admin（管理后台/修改密码入口）与其静态资源
  "/admin": { target: apiTarget, changeOrigin: true },
  "/static": { target: apiTarget, changeOrigin: true },
  // SSO 登录入口（整页跳转，开发期需代理到后端）
  "/oidc": { target: apiTarget, changeOrigin: true },
  "/dingding": { target: apiTarget, changeOrigin: true },
  "/cas": { target: apiTarget, changeOrigin: true },
};

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      imports: ["vue", "vue-router", "pinia"],
      resolvers: [ElementPlusResolver()],
      dts: "src/auto-imports.d.ts",
    }),
    Components({
      resolvers: [ElementPlusResolver()],
      dts: "src/components.d.ts",
    }),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy,
  },
});
