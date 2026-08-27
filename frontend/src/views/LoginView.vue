<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError } from "@/api";
import { useAuthSession } from "@/auth/session";

const route = useRoute();
const router = useRouter();
const auth = useAuthSession();
const form = reactive({ username: "", display_name: "", password: "" });
const errorMessage = ref("");
const submitting = ref(false);
const statusUnavailable = ref(false);

const oidcMode = computed(() => auth.state.status?.authentication_method === "oidc");
const setupMode = computed(() => auth.state.status?.setup_required === true);
const unavailableSetup = computed(
  () => !oidcMode.value && setupMode.value && auth.state.status?.setup_allowed === false,
);
const oidcNeedsLocalBinding = computed(() => oidcMode.value && setupMode.value);

async function checkStatus() {
  statusUnavailable.value = false;
  errorMessage.value = "";
  try {
    await auth.initialize();
    if (auth.isAuthenticated.value) await goAfterAuth();
  } catch (error) {
    statusUnavailable.value = true;
    errorMessage.value = error instanceof Error ? error.message : "无法读取登录状态";
  }
}

onMounted(checkStatus);

async function goAfterAuth() {
  const requested = typeof route.query.redirect === "string" ? route.query.redirect : "/";
  await router.replace(requested.startsWith("/") && !requested.startsWith("//") ? requested : "/");
}

async function submit() {
  errorMessage.value = "";
  submitting.value = true;
  try {
    if (!auth.state.ready) await auth.refresh();
    if (setupMode.value) {
      await auth.setup({
        username: form.username,
        display_name: form.display_name,
        password: form.password,
      });
    } else {
      await auth.login({ username: form.username, password: form.password });
    }
    await goAfterAuth();
  } catch (error) {
    statusUnavailable.value =
      error instanceof ApiError && (error.status === 0 || error.status === 408);
    errorMessage.value = error instanceof ApiError ? error.message : "认证失败，请重试";
  } finally {
    submitting.value = false;
  }
}

function beginOidcLogin() {
  errorMessage.value = "";
  submitting.value = true;
  // The backend owns the exact authorization endpoint, state, nonce and PKCE
  // transaction. The browser never accepts an IdP URL from query or form data.
  window.location.assign("/api/v1/auth/oidc/start");
}
</script>

<template>
  <main class="auth-page">
    <section class="auth-card">
      <div class="auth-mark">Q</div>
      <p class="eyebrow">QA FORGE · LOCAL LEARNING</p>
      <h1>{{ oidcMode ? "使用本机 Keycloak 登录" : setupMode ? "初始化本地管理员" : "登录 QA 平台" }}</h1>
      <p class="intro">
        {{ oidcMode
          ? "认证与 TOTP 由我们自建的本机 Keycloak 处理；平台仍使用本地 RBAC 决定权限，不会连接公司统一登录。"
          : setupMode
          ? "数据库中还没有用户。首个账号将获得系统管理员角色，初始化完成后此入口自动关闭。"
          : "会话只保存在本机数据库和 HttpOnly Cookie 中，不连接公司统一登录。" }}
      </p>

      <div v-if="auth.state.loading && !auth.state.ready" class="auth-notice">正在检查本地服务…</div>
      <div v-else-if="statusUnavailable" class="auth-error">
        <p>{{ errorMessage || "无法连接本地后端" }}</p>
        <button class="button" type="button" :disabled="auth.state.loading" @click="checkStatus">重新检查本地服务</button>
      </div>
      <div v-else-if="unavailableSetup" class="auth-error">
        当前环境禁止网页初始化，请在服务器终端运行交互式管理员初始化命令。
      </div>
      <div v-else-if="oidcNeedsLocalBinding" class="auth-error">
        平台还没有可绑定的本地用户。请先切回 local_accounts 初始化管理员，
        显式绑定 Keycloak subject 后再开启 OIDC。
      </div>
      <div v-else-if="oidcMode" class="oidc-actions">
        <button class="button primary" type="button" :disabled="submitting" @click="beginOidcLogin">
          {{ submitting ? "正在转到本机认证…" : "使用 Keycloak + TOTP 登录" }}
        </button>
        <p class="oidc-note">Authorization Code · S256 PKCE · 显式账号绑定</p>
      </div>
      <form v-else @submit.prevent="submit">
        <label>
          用户名
          <input v-model.trim="form.username" autocomplete="username" minlength="3" maxlength="50" required />
        </label>
        <label v-if="setupMode">
          显示名称
          <input v-model.trim="form.display_name" autocomplete="name" minlength="1" maxlength="100" required />
        </label>
        <label>
          密码
          <input
            v-model="form.password"
            :autocomplete="setupMode ? 'new-password' : 'current-password'"
            type="password"
            minlength="12"
            maxlength="200"
            required
          />
        </label>
        <p v-if="errorMessage" class="auth-error">{{ errorMessage }}</p>
        <button class="button primary" :disabled="submitting || auth.state.loading">
          {{ submitting ? "处理中…" : setupMode ? "创建管理员并登录" : "登录" }}
        </button>
      </form>

      <footer>
        <span>Session Cookie</span><span>CSRF Protection</span><span>{{ oidcMode ? "OIDC + MFA" : "Argon2id" }}</span>
      </footer>
    </section>
  </main>
</template>

<style scoped>
.auth-page { min-height:100vh; display:grid; place-items:center; padding:28px; background:radial-gradient(circle at 20% 20%,#dff6eb 0,transparent 34%),#edf3f0; }
.auth-card { width:min(440px,100%); padding:38px; border:1px solid #d7e2dc; border-radius:20px; background:#fff; box-shadow:0 24px 70px rgba(20,50,39,.13); }
.auth-mark { display:grid; width:48px; height:48px; place-items:center; border-radius:14px; color:#123228; background:#5cd59e; font-size:22px; font-weight:900; }
.eyebrow { margin:24px 0 8px; color:#20865e; font-size:9px; font-weight:800; letter-spacing:.16em; }
h1 { margin:0; color:#172c24; font-size:28px; }
.intro { margin:12px 0 24px; color:#74817c; font-size:12px; line-height:1.7; }
form { display:grid; gap:15px; }
.oidc-actions { display:grid; gap:10px; }
.oidc-note { margin:0; text-align:center; color:#708078; font-size:9px; }
label { display:grid; gap:7px; color:#52615b; font-size:10px; font-weight:700; }
input { height:43px; padding:0 12px; border:1px solid #d7e1dc; border-radius:9px; outline:none; }
input:focus { border-color:#38a977; box-shadow:0 0 0 3px rgba(56,169,119,.11); }
button { width:100%; margin-top:5px; }
button:disabled { cursor:not-allowed; opacity:.65; }
.auth-notice,.auth-error { padding:11px 13px; border-radius:8px; font-size:10px; line-height:1.5; }
.auth-notice { color:#286d51; background:#eef8f3; }
.auth-error { color:#a74141; background:#fff0f0; }
footer { display:flex; flex-wrap:wrap; gap:7px; margin-top:25px; padding-top:18px; border-top:1px solid #edf1ef; }
footer span { padding:4px 7px; border-radius:999px; color:#587067; background:#edf4f1; font-size:8px; }
@media(max-width:520px){.auth-page{padding:12px}.auth-card{padding:27px 22px}}
</style>
