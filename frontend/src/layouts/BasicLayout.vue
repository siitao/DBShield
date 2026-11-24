<script setup lang="ts">
import { computed } from "vue";
import { useRouter, useRoute } from "vue-router";
import { ElMessageBox } from "element-plus";
import { useAuthStore } from "@/stores/auth";
import { menuItems, type MenuItem } from "@/config/menu";
import { legacyBase } from "@/utils/request";

const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

function childVisible(c: MenuItem): boolean {
  if (c.superuserOnly && !auth.isSuperuser) return false;
  return !c.perm || auth.hasPerm(c.perm);
}

function topVisible(item: MenuItem): boolean {
  if (item.perm && !auth.hasPerm(item.perm)) return false;
  if (item.children) return item.children.some(childVisible);
  return true;
}

const visibleMenu = computed(() => menuItems.filter(topVisible));

function menuIndex(item: MenuItem): string {
  if (item.routeName) {
    const qs = item.routeQuery
      ? new URLSearchParams(item.routeQuery).toString()
      : "";
    return qs ? `${item.routeName}?${qs}` : item.routeName;
  }
  if (item.legacyPath) return "legacy:" + item.legacyPath;
  return item.title;
}

function onSelect(index: string) {
  if (index.startsWith("legacy:")) {
    window.open(legacyBase + index.slice("legacy:".length), "_blank");
    return;
  }
  const [name, qs] = index.split("?");
  router.push({
    name,
    query: qs ? Object.fromEntries(new URLSearchParams(qs)) : {},
  });
}

const activeIndex = computed(() => {
  const name = (route.name as string) || "";
  if (!name) return "";

  // 当前路由 + query，与菜单 index 采用同样的序列化方式
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(route.query)) {
    if (typeof value === "string") qs.append(key, value);
  }
  const full = qs.toString() ? `${name}?${qs.toString()}` : name;

  const indices: string[] = [];
  for (const item of visibleMenu.value) {
    if (item.children) {
      indices.push(...item.children.filter(childVisible).map(menuIndex));
    } else {
      indices.push(menuIndex(item));
    }
  }

  if (indices.includes(full)) return full;
  // 同一页面的子视图在页面内切换时 query 可能不同，回退到该路由的默认菜单项
  return (
    indices.find((i) => i === name) ??
    indices.find((i) => i.startsWith(`${name}?`)) ??
    name
  );
});

async function onLogout() {
  try {
    await ElMessageBox.confirm("确定要退出登录吗？", "提示", {
      type: "warning",
      confirmButtonText: "退出",
      cancelButtonText: "取消",
    });
  } catch {
    return;
  }
  await auth.logout();
  router.replace({ name: "login" });
}

function openUrl(url: string) {
  window.open(url, "_blank");
}
</script>

<template>
  <el-container class="layout">
    <el-aside width="220px" class="layout-aside">
      <div class="layout-brand">
        <svg class="brand-logo" viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
          <defs>
            <linearGradient id="brandShieldGrad" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stop-color="#60a5fa" />
              <stop offset="1" stop-color="#2563eb" />
            </linearGradient>
          </defs>
          <path
            d="M12 2l8 3.2V11c0 5.1-3.4 9.7-8 11-4.6-1.3-8-5.9-8-11V5.2L12 2z"
            fill="url(#brandShieldGrad)"
          />
          <path
            d="M8.8 12.2l2.2 2.2 4.2-4.4"
            stroke="#fff"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            fill="none"
          />
        </svg>
        <span>DBShield</span>
      </div>
      <el-menu
        :default-active="activeIndex"
        class="layout-menu"
        @select="onSelect"
      >
        <template v-for="item in visibleMenu" :key="item.title">
          <!-- 含子菜单 -->
          <el-sub-menu v-if="item.children" :index="item.title">
            <template #title>
              <el-icon v-if="item.icon"><component :is="item.icon" /></el-icon>
              <span>{{ item.title }}</span>
            </template>
            <el-menu-item
              v-for="c in item.children.filter(childVisible)"
              :key="c.title"
              :index="menuIndex(c)"
            >
              {{ c.title }}
            </el-menu-item>
          </el-sub-menu>
          <!-- 单项 -->
          <el-menu-item v-else :index="menuIndex(item)">
            <el-icon v-if="item.icon"><component :is="item.icon" /></el-icon>
            <span>{{ item.title }}</span>
          </el-menu-item>
        </template>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="layout-header">
        <div class="header-left">
          <span class="page-title">{{ route.meta.title || "" }}</span>
        </div>
        <div class="header-right">
          <el-tooltip content="待办工作流" placement="bottom">
            <span class="header-action" @click="router.push({ name: 'todoworkflow' })">
              <el-icon><Bell /></el-icon>
            </span>
          </el-tooltip>
          <el-dropdown>
            <span class="header-user">
              <el-icon><UserFilled /></el-icon>
              <span class="user-name">{{ auth.displayName }}</span>
              <el-icon><CaretBottom /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="auth.isSuperuser" @click="router.push({ name: 'user' })">
                  用户管理
                </el-dropdown-item>
                <el-dropdown-item v-if="auth.isSuperuser" @click="openUrl('/api/debug?full=true')">
                  系统信息
                </el-dropdown-item>
                <!-- 同源相对路径：开发期经 vite 代理到 Django，未登录时中间件 302 /login/ 落回 SPA；
                     生产期 nginx 本就按相对路径转发，不能用 legacyBase 直连后端（跨端口/跨 host 会丢 cookie） -->
                <el-dropdown-item v-if="auth.isSuperuser" @click="openUrl('/admin')">
                  管理后台
                </el-dropdown-item>
                <el-dropdown-item @click="openUrl('/admin/password_change/')">
                  修改密码
                </el-dropdown-item>
                <el-dropdown-item divided @click="onLogout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="layout-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped lang="scss">
.layout {
  height: 100vh;
}

.layout-aside {
  // 深色导航渐变：从墨蓝过渡到深海军蓝，右侧留一道极淡的分隔光
  background: linear-gradient(180deg, #0b1222 0%, #101a2e 55%, #16233c 100%);
  border-right: 1px solid rgba(148, 163, 184, 0.08);
  overflow-x: hidden;
  overflow-y: auto;

  // 细滚动条：平时完全透明，鼠标悬停时浮现半透明细条
  // Firefox
  scrollbar-width: thin;
  scrollbar-color: transparent transparent;
  &:hover {
    scrollbar-color: rgba(255, 255, 255, 0.25) transparent;
  }

  // WebKit (Chrome / Edge / Safari)
  &::-webkit-scrollbar {
    width: 6px;
  }
  &::-webkit-scrollbar-track {
    background: transparent;
  }
  &::-webkit-scrollbar-thumb {
    background-color: transparent;
    border-radius: 3px;
  }
  &:hover::-webkit-scrollbar-thumb {
    background-color: rgba(255, 255, 255, 0.25);
  }
}

.layout-brand {
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: #fff;
  font-size: 19px;
  font-weight: 700;
  letter-spacing: 0.5px;
  background: rgba(255, 255, 255, 0.025);
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);

  .brand-logo {
    display: block;
    filter: drop-shadow(0 2px 8px rgba(37, 99, 235, 0.5));
  }
}

// 深色侧边栏菜单样式
.layout-menu {
  border-right: none;
  background: transparent;
  padding: 10px 10px 16px;

  :deep(.el-menu-item),
  :deep(.el-sub-menu__title) {
    height: 42px;
    line-height: 42px;
    margin-bottom: 3px;
    border-radius: 9px;
    font-size: 13.5px;
    color: var(--dbshield-nav-text);
    transition: background var(--dbshield-transition),
      color var(--dbshield-transition), box-shadow var(--dbshield-transition);
  }
  :deep(.el-menu-item:hover),
  :deep(.el-sub-menu__title:hover) {
    background: rgba(148, 163, 184, 0.12);
    color: #fff;
  }
  :deep(.el-menu-item.is-active) {
    background: linear-gradient(90deg, #2563eb 0%, #3b82f6 100%);
    color: #fff;
    font-weight: 600;
    box-shadow: 0 6px 16px rgba(37, 99, 235, 0.32),
      inset 0 0 0 1px rgba(147, 197, 253, 0.18);
  }
  :deep(.el-sub-menu .el-menu) {
    background: transparent;
  }
  :deep(.el-sub-menu .el-menu-item) {
    height: 38px;
    line-height: 38px;
    font-size: 13px;
    margin-left: 2px;
    margin-right: 2px;
  }
}

.layout-header {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--dbshield-border-light);
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.03);
}

.page-title {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  font-size: 15.5px;
  font-weight: 600;
  color: var(--el-text-color-primary);

  &::before {
    content: "";
    width: 4px;
    height: 15px;
    border-radius: 2px;
    background: linear-gradient(180deg, #60a5fa 0%, #2563eb 100%);
  }
}

.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.header-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 9px;
  font-size: 17px;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  transition: background-color var(--dbshield-transition),
    color var(--dbshield-transition);

  &:hover {
    background: var(--el-fill-color-light);
    color: var(--el-color-primary);
  }
}

.header-user {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 10px;
  border-radius: 9px;
  cursor: pointer;
  color: var(--el-text-color-regular);
  transition: background-color var(--dbshield-transition),
    color var(--dbshield-transition);

  &:hover {
    background: var(--el-fill-color-light);
    color: var(--el-color-primary);
  }

  .user-name {
    font-size: 13.5px;
    font-weight: 500;
  }
}

.layout-main {
  background: var(--el-bg-color-page);
  padding: 20px 24px 24px;
}
</style>
