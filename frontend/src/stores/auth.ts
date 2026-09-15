import { defineStore } from "pinia";
import { ref, computed } from "vue";
import {
  fetchCurrentUser,
  authenticate,
  logout as apiLogout,
  type CurrentUser,
  type AuthResult,
} from "@/api/user";
import { withAuthSuppressed } from "@/utils/request";
import { useAiOptimizeStore } from "@/stores/aiOptimize";

export const useAuthStore = defineStore("auth", () => {
  const user = ref<CurrentUser | null>(null);
  const permissions = ref<string[]>([]);

  const isLoggedIn = computed(() => !!user.value);
  const displayName = computed(
    () => user.value?.display || user.value?.username || ""
  );
  const isSuperuser = computed(() => !!user.value?.is_superuser);

  /** 是否拥有某权限（超管通行） */
  function hasPerm(perm: string): boolean {
    if (isSuperuser.value) return true;
    return permissions.value.includes(perm);
  }

  function hasAnyPerms(perms: string[]): boolean {
    return perms.some((p) => hasPerm(p));
  }

  /** 拉取当前用户信息与权限（401 时抑制自动跳转，交由路由守卫处理） */
  async function loadCurrentUser(): Promise<CurrentUser> {
    const { data } = await withAuthSuppressed(() => fetchCurrentUser());
    user.value = data;
    permissions.value = data.permissions || [];
    return data;
  }

  /** 调用 /authenticate/，返回原始旧信封结果，由登录页决定后续分支 */
  async function authenticateUser(
    username: string,
    password: string
  ): Promise<AuthResult> {
    const { data } = await authenticate(username, password);
    return data;
  }

  async function logout(): Promise<void> {
    try {
      await apiLogout();
    } catch {
      // 忽略 sign_out 的 302 响应错误
    }
    clear();
  }

  function clear(): void {
    user.value = null;
    permissions.value = [];
    // 登出是 SPA 路由跳转不整页刷新，跨用户的内存态（AI 优化报告等）
    // 必须显式清理，防止同标签页换账号登录后残留展示
    useAiOptimizeStore().reset();
  }

  return {
    user,
    permissions,
    isLoggedIn,
    displayName,
    isSuperuser,
    hasPerm,
    hasAnyPerms,
    loadCurrentUser,
    authenticateUser,
    logout,
    clear,
  };
});
