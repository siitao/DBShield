/** 读取浏览器 cookie（用于 CSRF token）。逻辑迁移自旧版 base.html 的 getCookie。 */
export function getCookie(name: string): string | null {
  if (typeof document === "undefined" || !document.cookie) return null;
  const cookies = document.cookie.split(";");
  for (const raw of cookies) {
    const cookie = raw.trim();
    if (cookie.startsWith(name + "=")) {
      return decodeURIComponent(cookie.substring(name.length + 1));
    }
  }
  return null;
}

/** 设置 cookie（用于 2FA 登录时切换临时会话 sessionid）。 */
export function setCookie(name: string, value: string): void {
  // 会话 cookie 限定 SameSite；HTTPS 下追加 Secure（本地 HTTP 开发不加，避免被丢弃）
  const secure =
    typeof location !== "undefined" && location.protocol === "https:"
      ? "; Secure"
      : "";
  document.cookie = `${name}=${value}; path=/; SameSite=Lax${secure}`;
}
