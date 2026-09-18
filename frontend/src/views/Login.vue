<script setup lang="ts">
import { ref, reactive, onMounted } from "vue";
import { useRouter, useRoute } from "vue-router";
import { ElMessage, type FormInstance, type FormRules } from "element-plus";
import { User, Lock } from "@element-plus/icons-vue";
import { useAuthStore } from "@/stores/auth";
import { setCookie } from "@/utils/auth";
import {
  fetch2faContext,
  fetch2faVerify,
  send2faSms,
  fetchLoginOptions,
  type LoginOptions,
  type TwoFaContext,
} from "@/api/user";

const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

const formRef = ref<FormInstance>();
const loading = ref(false);
const form = reactive({ username: "", password: "" });

const rules: FormRules = {
  username: [{ required: true, message: "请输入用户名", trigger: "blur" }],
  password: [{ required: true, message: "请输入密码", trigger: "blur" }],
};

// SSO 登录选项（未登录态从后端拉取，决定是否显示 OIDC/钉钉/CAS 按钮）
const loginOptions = ref<LoginOptions | null>(null);

// 2FA 状态
const twoFaMode = ref(false);
const twoFaCtx = ref<TwoFaContext | null>(null);
const twoFaForm = reactive({
  auth_type: "totp",
  otp: "",
  phone: "",
});
let smsCountdown = 0;
const smsBtnText = ref("获取验证码");

onMounted(() => {
  auth.loadCurrentUser().then(
    () => router.replace((route.query.redirect as string) || "/dashboard"),
    () => {
      /* 未登录，停留 */
    }
  );
  // 拉取 SSO 选项（AllowAny，未登录可访问）
  fetchLoginOptions()
    .then((opts) => {
      loginOptions.value = opts;
    })
    .catch(() => {
      /* 拉取失败不影响本地登录 */
    });
});

async function onSubmit() {
  if (!formRef.value) return;
  await formRef.value.validate(async (valid) => {
    if (!valid) return;
    loading.value = true;
    try {
      const res = await auth.authenticateUser(form.username, form.password);
      if (res.status === 0) {
        if (res.data) {
          // 需要 2FA：临时会话 sessionKey 设为 sessionid cookie，
          // 后续 /api/v1/user/2fa/* 同源带 cookie 即可读到该会话。
          setCookie("sessionid", res.data);
          await enter2fa();
          return;
        }
        await auth.loadCurrentUser();
        router.replace((route.query.redirect as string) || "/dashboard");
      } else {
        ElMessage.error(res.msg || "登录失败");
      }
    } catch {
      // 错误提示已由 request 拦截器处理
    } finally {
      loading.value = false;
    }
  });
}

async function enter2fa() {
  try {
    twoFaCtx.value = await fetch2faContext();
    const types = twoFaCtx.value.auth_types.map((t) => t.code);
    twoFaForm.auth_type = types.includes("totp") ? "totp" : types[0] || "totp";
    twoFaForm.phone = twoFaCtx.value.phone || "";
    twoFaMode.value = true;
  } catch {
    // 拦截器已提示
  }
}

function onAuthTypeChange() {
  twoFaForm.otp = "";
}

async function onSendSms() {
  if (!twoFaForm.phone) return ElMessage.warning("请输入手机号");
  try {
    const { data } = await send2faSms({
      engineer: form.username,
      phone: twoFaForm.phone,
    });
    if (data.status === 0) {
      ElMessage.success("验证码已发送，5 分钟内有效");
      smsCountdown = 60;
      const timer = setInterval(() => {
        smsCountdown -= 1;
        smsBtnText.value = smsCountdown > 0 ? `${smsCountdown}s 后重发` : "获取验证码";
        if (smsCountdown <= 0) clearInterval(timer);
      }, 1000);
    } else {
      ElMessage.error(data.msg);
    }
  } catch {
    // 拦截器已提示
  }
}

async function onVerify() {
  if (!twoFaForm.otp) return ElMessage.warning("请输入验证码");
  loading.value = true;
  try {
    const { data } = await fetch2faVerify({
      engineer: form.username,
      otp: twoFaForm.otp,
      auth_type: twoFaForm.auth_type,
      phone: twoFaForm.auth_type === "sms" ? twoFaForm.phone : undefined,
    });
    if (data.status === 0) {
      await auth.loadCurrentUser();
      router.replace((route.query.redirect as string) || "/dashboard");
    } else {
      ElMessage.error(data.msg || "验证失败");
    }
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false;
  }
}

function backToLogin() {
  twoFaMode.value = false;
  twoFaCtx.value = null;
  twoFaForm.otp = "";
}
</script>

<template>
  <div class="login-shell">
    <!-- 左侧品牌区 -->
    <aside class="brand-pane">
      <header class="brand-top reveal" style="--d: 0.05s">
        <span class="emblem">
          <svg viewBox="0 0 48 48" aria-hidden="true">
            <defs>
              <linearGradient id="lg-shield" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stop-color="#2563eb" />
                <stop offset="1" stop-color="#0891b2" />
              </linearGradient>
            </defs>
            <circle
              class="orbit"
              cx="24"
              cy="24"
              r="21"
              fill="none"
              stroke="rgba(37, 99, 235, 0.28)"
              stroke-width="1"
              stroke-dasharray="4 7"
            />
            <path
              d="M24 4 L40 10 V22 C40 33 33 41 24 44 C15 41 8 33 8 22 V10 Z"
              fill="rgba(37, 99, 235, 0.07)"
              stroke="url(#lg-shield)"
              stroke-width="2.2"
              stroke-linejoin="round"
            />
            <ellipse cx="24" cy="18" rx="7.5" ry="3.2" fill="none" stroke="#2563eb" stroke-width="1.7" />
            <path
              d="M16.5 18 V28 C16.5 30 20 31.6 24 31.6 C28 31.6 31.5 30 31.5 28 V18"
              fill="none"
              stroke="#2563eb"
              stroke-width="1.7"
            />
            <path
              d="M16.5 23 C16.5 25 20 26.6 24 26.6 C28 26.6 31.5 25 31.5 23"
              fill="none"
              stroke="#2563eb"
              stroke-width="1.7"
            />
          </svg>
        </span>
        <span class="brand-name">DBShield</span>
      </header>

      <div class="brand-hero">
        <p class="eyebrow reveal mono" style="--d: 0.15s">DATABASE · SECURITY · PLATFORM</p>
        <h1 class="reveal" style="--d: 0.25s">
          让每一次 SQL 变更<br />
          <em>安全落地</em>
        </h1>
        <p class="brand-sub reveal" style="--d: 0.35s">
          面向数据库团队的统一治理平台：工单审核、在线查询、慢查诊断，一个入口全部掌控。
        </p>
        <ul class="brand-points">
          <li class="reveal" style="--d: 0.45s"><i />工单 AI 审核，风险提交前可见</li>
          <li class="reveal" style="--d: 0.53s"><i />慢查询自动采集与 AI 诊断</li>
          <li class="reveal" style="--d: 0.61s"><i />自然语言生成 SQL，效率加倍</li>
        </ul>
      </div>

      <footer class="brand-foot reveal mono" style="--d: 0.7s">
        <span>© 2026 DBShield</span>
        <span>SECURE BY DESIGN</span>
      </footer>
    </aside>

    <!-- 右侧表单区 -->
    <main class="form-pane">
      <div class="form-card">
        <!-- 窄屏品牌头 -->
        <div class="card-brand">
          <span class="emblem emblem-sm">
            <svg viewBox="0 0 48 48" aria-hidden="true">
              <path
                d="M24 4 L40 10 V22 C40 33 33 41 24 44 C15 41 8 33 8 22 V10 Z"
                fill="rgba(37, 99, 235, 0.07)"
                stroke="#2563eb"
                stroke-width="2.2"
                stroke-linejoin="round"
              />
              <ellipse cx="24" cy="19" rx="7.5" ry="3.2" fill="none" stroke="#2563eb" stroke-width="1.7" />
              <path
                d="M16.5 19 V28 C16.5 30 20 31.6 24 31.6 C28 31.6 31.5 30 31.5 28 V19"
                fill="none"
                stroke="#2563eb"
                stroke-width="1.7"
              />
            </svg>
          </span>
          <span class="brand-name-sm">DBShield</span>
        </div>

        <div class="card-head">
          <h2>{{ twoFaMode ? "两步验证" : "欢迎回来" }}</h2>
          <p>{{ twoFaMode ? "输入动态验证码，完成身份校验" : "登录以继续访问 DBShield" }}</p>
        </div>

        <!-- 2FA 输入 -->
        <template v-if="twoFaMode">
          <el-form size="large" class="login-form" @keyup.enter="onVerify">
            <el-form-item v-if="twoFaCtx && twoFaCtx.auth_types.length > 1">
              <el-select
                v-model="twoFaForm.auth_type"
                style="width: 100%"
                @change="onAuthTypeChange"
              >
                <el-option
                  v-for="t in twoFaCtx?.auth_types"
                  :key="t.code"
                  :label="t.display"
                  :value="t.code"
                />
              </el-select>
            </el-form-item>
            <template v-if="twoFaForm.auth_type === 'sms'">
              <el-form-item>
                <el-input v-model="twoFaForm.phone" placeholder="手机号" clearable>
                  <template #append>
                    <el-button :disabled="smsCountdown > 0" @click="onSendSms">
                      {{ smsBtnText }}
                    </el-button>
                  </template>
                </el-input>
              </el-form-item>
              <el-form-item>
                <el-input v-model="twoFaForm.otp" placeholder="短信验证码" class="otp-input" clearable />
              </el-form-item>
            </template>
            <el-form-item v-else>
              <el-input
                v-model="twoFaForm.otp"
                placeholder="动态验证码（TOTP）"
                class="otp-input"
                clearable
              />
            </el-form-item>
            <el-button type="primary" class="login-btn" :loading="loading" @click="onVerify">
              验证并登录
            </el-button>
            <el-button link class="back-btn" @click="backToLogin">
              ← 返回账号密码登录
            </el-button>
          </el-form>
        </template>

        <!-- 账号密码 -->
        <template v-else>
          <!-- SSO 登录按钮（整页跳转，非 axios） -->
          <a
            v-if="loginOptions?.oidc_enabled"
            class="sso-btn"
            :href="loginOptions.oidc_login_url"
          >
            {{ loginOptions.oidc_btn_name || "以 OIDC 登录" }}
          </a>
          <a
            v-else-if="loginOptions?.dingding_enabled"
            class="sso-btn"
            :href="loginOptions.dingding_login_url"
          >
            以钉钉登录
          </a>
          <a
            v-else-if="loginOptions?.cas_enabled"
            class="sso-btn"
            :href="loginOptions.cas_login_url"
          >
            CAS 认证登录
          </a>
          <div
            v-if="loginOptions && (loginOptions.oidc_enabled || loginOptions.dingding_enabled || loginOptions.cas_enabled)"
            class="sso-divider"
          >
            <span>或使用账号密码</span>
          </div>
          <el-form
            ref="formRef"
            :model="form"
            :rules="rules"
            size="large"
            class="login-form"
            @keyup.enter="onSubmit"
          >
            <el-form-item prop="username">
              <el-input
                v-model="form.username"
                placeholder="用户名"
                :prefix-icon="User"
                clearable
                autocomplete="username"
              />
            </el-form-item>
            <el-form-item prop="password">
              <el-input
                v-model="form.password"
                type="password"
                placeholder="密码"
                :prefix-icon="Lock"
                show-password
                clearable
                autocomplete="current-password"
              />
            </el-form-item>
            <el-button
              type="primary"
              class="login-btn"
              :loading="loading"
              @click="onSubmit"
            >
              登 录
            </el-button>
          </el-form>
        </template>

        <p class="card-foot mono">POWERED BY DBSHIELD</p>
      </div>
    </main>
  </div>
</template>

<style scoped lang="scss">
/* ============================================================
   登录页 · 亮色版（与主应用同源：浅灰蓝底 / 白卡 / 品牌蓝）
   左：品牌叙事（淡网格 + 盾徽 + 能力清单）
   右：白色表单卡（与内容页卡片同一质感语言）
   ============================================================ */

.login-shell {
  display: flex;
  min-height: 100vh;
  background:
    radial-gradient(52rem 36rem at 12% -8%, rgba(37, 99, 235, 0.07), transparent 60%),
    radial-gradient(40rem 30rem at 88% 108%, rgba(8, 145, 178, 0.05), transparent 55%),
    var(--dbshield-bg-page, #f3f6fb);
  color: #1e293b;
  font-family:
    "PingFang SC",
    "Microsoft YaHei",
    "Helvetica Neue",
    Arial,
    sans-serif;
}

/* 淡网格蓝图纹理，铺满整屏（右上淡出） */
.login-shell::before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  background:
    repeating-linear-gradient(0deg, rgba(37, 99, 235, 0.045) 0 1px, transparent 1px 48px),
    repeating-linear-gradient(90deg, rgba(37, 99, 235, 0.045) 0 1px, transparent 1px 48px);
  mask-image: radial-gradient(80rem 60rem at 30% 20%, #000 30%, transparent 80%);
}

/* ── 左侧品牌区 ─────────────────────────────── */
.brand-pane {
  position: relative;
  display: flex;
  flex: 1.15;
  flex-direction: column;
  justify-content: space-between;
  padding: 52px 60px;
  border-right: 1px solid rgba(37, 99, 235, 0.1);
  overflow: hidden;

  // 左下角一道弧形辉光
  &::after {
    content: "";
    position: absolute;
    left: -18%;
    bottom: -32%;
    width: 46rem;
    height: 46rem;
    border-radius: 50%;
    background: radial-gradient(
      closest-side,
      rgba(37, 99, 235, 0.08),
      transparent 70%
    );
    pointer-events: none;
  }
}

.brand-top {
  display: flex;
  align-items: center;
  gap: 14px;
}

.emblem {
  display: inline-flex;
  width: 46px;
  height: 46px;

  svg {
    width: 100%;
    height: 100%;
  }
}

.emblem .orbit {
  transform-origin: center;
  animation: orbit-spin 26s linear infinite;
}

@keyframes orbit-spin {
  to {
    transform: rotate(360deg);
  }
}

.brand-name {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: #0f172a;
}

.brand-hero {
  position: relative;
  max-width: 520px;
}

.mono {
  font-family: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
}

.eyebrow {
  margin: 0 0 18px;
  font-size: 12px;
  letter-spacing: 0.42em;
  color: #2563eb;
}

.brand-hero h1 {
  margin: 0;
  font-size: clamp(30px, 3.4vw, 44px);
  font-weight: 700;
  line-height: 1.28;
  color: #0f172a;

  em {
    font-style: normal;
    background: linear-gradient(100deg, #2563eb 10%, #0891b2 90%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
  }
}

.brand-sub {
  margin: 18px 0 0;
  max-width: 420px;
  font-size: 15px;
  line-height: 1.9;
  color: #64748b;
}

.brand-points {
  margin: 34px 0 0;
  padding: 0;
  list-style: none;

  li {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 9px 0;
    font-size: 14.5px;
    color: #334155;
  }

  i {
    width: 6px;
    height: 6px;
    flex: none;
    border-radius: 50%;
    background: #2563eb;
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14);
  }
}

.brand-foot {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  letter-spacing: 0.12em;
  color: #94a3b8;
}

/* ── 右侧表单区 ─────────────────────────────── */
.form-pane {
  flex: 1;
  display: grid;
  place-items: center;
  padding: 48px 32px;
}

.form-card {
  width: 400px;
  max-width: 100%;
  padding: 42px 38px 26px;
  background: #fff;
  border: 1px solid #e3eaf4;
  border-radius: 16px;
  box-shadow:
    0 24px 60px rgba(15, 23, 42, 0.08),
    0 2px 6px rgba(15, 23, 42, 0.04);
  animation: card-in 0.55s cubic-bezier(0.22, 0.9, 0.32, 1) both;
}

@keyframes card-in {
  from {
    opacity: 0;
    transform: translateY(16px) scale(0.985);
  }
}

.card-brand {
  display: none;
  align-items: center;
  gap: 10px;
  margin-bottom: 22px;

  .brand-name-sm {
    font-size: 18px;
    font-weight: 700;
    color: #0f172a;
  }

  .emblem-sm {
    width: 34px;
    height: 34px;

    svg {
      width: 100%;
      height: 100%;
    }
  }
}

.card-head {
  margin-bottom: 26px;

  h2 {
    margin: 0;
    font-size: 24px;
    font-weight: 700;
    color: #0f172a;
  }

  p {
    margin: 8px 0 0;
    font-size: 13.5px;
    color: #8a97ab;
  }
}

/* 输入框：亮色质感，聚焦品牌蓝辉光 */
.login-form {
  :deep(.el-input__wrapper) {
    border-radius: 10px;
    box-shadow: 0 0 0 1px #dbe3ef inset;
    transition:
      box-shadow var(--dbshield-transition),
      background var(--dbshield-transition);

    &:hover {
      box-shadow: 0 0 0 1px #b9c8e2 inset;
    }

    &.is-focus {
      box-shadow:
        0 0 0 1px rgba(37, 99, 235, 0.85) inset,
        0 0 0 4px rgba(37, 99, 235, 0.12);
    }
  }
}

.otp-input :deep(.el-input__inner) {
  letter-spacing: 0.35em;
}

/* 主按钮：品牌渐变 + 悬停抬升 */
.login-btn {
  width: 100%;
  height: 44px;
  margin-top: 6px;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.3em;
  text-indent: 0.3em;
  border: none;
  border-radius: 10px;
  background: linear-gradient(135deg, #2f6fee 0%, #2563eb 55%, #1e4fbc 100%);
  box-shadow:
    0 8px 22px rgba(37, 99, 235, 0.3),
    inset 0 1px 0 rgba(255, 255, 255, 0.22);
  transition:
    transform var(--dbshield-transition),
    box-shadow var(--dbshield-transition),
    filter var(--dbshield-transition);

  &:hover {
    filter: brightness(1.08);
    transform: translateY(-1px);
    box-shadow:
      0 12px 28px rgba(37, 99, 235, 0.4),
      inset 0 1px 0 rgba(255, 255, 255, 0.25);
  }

  &:active {
    transform: translateY(0);
  }
}

.back-btn {
  width: 100%;
  margin: 14px 0 0;
  color: #94a3b8;

  &:hover {
    color: #2563eb;
  }
}

/* SSO：白底描边按钮 */
.sso-btn {
  display: block;
  width: 100%;
  padding: 11px 0;
  margin-bottom: 12px;
  text-align: center;
  font-size: 14.5px;
  color: #2563eb;
  background: #fff;
  border: 1px solid #d4dff0;
  border-radius: 10px;
  text-decoration: none;
  transition:
    background var(--dbshield-transition),
    border-color var(--dbshield-transition),
    color var(--dbshield-transition),
    box-shadow var(--dbshield-transition);

  &:hover {
    color: #1e4fbc;
    background: #f2f6fe;
    border-color: #a9c0ea;
    box-shadow: 0 4px 14px rgba(37, 99, 235, 0.12);
  }
}

.sso-divider {
  display: flex;
  align-items: center;
  margin: 16px 0 18px;
  font-size: 12px;
  color: #9aa7ba;

  &::before,
  &::after {
    content: "";
    flex: 1;
    border-bottom: 1px solid #e5eaf2;
  }

  span {
    padding: 0 12px;
  }
}

.card-foot {
  margin: 26px 0 0;
  text-align: center;
  font-size: 10px;
  letter-spacing: 0.32em;
  color: #c2cbd8;
}

/* ── 入场编排 ─────────────────────────────── */
.reveal {
  animation: reveal-up 0.6s cubic-bezier(0.22, 0.9, 0.32, 1) both;
  animation-delay: var(--d, 0s);
}

@keyframes reveal-up {
  from {
    opacity: 0;
    transform: translateY(14px);
  }
}

@media (prefers-reduced-motion: reduce) {
  .reveal,
  .form-card,
  .emblem .orbit {
    animation: none;
  }
}

/* ── 窄屏：收起品牌区，单列居中 ─────────────── */
@media (max-width: 960px) {
  .brand-pane {
    display: none;
  }

  .form-pane {
    padding: 32px 20px;
  }

  .form-card {
    width: 420px;
    padding: 34px 28px 22px;
  }

  .card-brand {
    display: flex;
  }
}
</style>
