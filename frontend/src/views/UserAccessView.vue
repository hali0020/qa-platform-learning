<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { ApiError, qaApi } from "@/api";
import type { RoleSummary, UserAccount, UserStatus } from "@/api";
import { useAuthSession } from "@/auth/session";

const auth = useAuthSession();
const users = ref<UserAccount[]>([]);
const roles = ref<RoleSummary[]>([]);
const selectedId = ref<string | null>(null);
const loading = ref(false);
const saving = ref(false);
const message = ref("");
const errorMessage = ref("");
const createOpen = ref(false);

const createForm = reactive({
  username: "",
  display_name: "",
  password: "",
  role: "",
});
const editForm = reactive({
  display_name: "",
  status: "active" as UserStatus,
  role: "",
  new_password: "",
  oidc_subject: "",
});

const selected = computed(() => users.value.find((user) => user.id === selectedId.value) ?? null);
const canManage = computed(() => auth.can("users.manage"));

onMounted(load);

async function load() {
  loading.value = true;
  errorMessage.value = "";
  try {
    [users.value, roles.value] = await Promise.all([qaApi.listUsers(), qaApi.listRoles()]);
    if (selectedId.value && !users.value.some((user) => user.id === selectedId.value)) {
      selectedId.value = null;
    }
    if (selectedId.value) openUser(selectedId.value);
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "用户与角色加载失败";
  } finally {
    loading.value = false;
  }
}

function openUser(userId: string) {
  const user = users.value.find((item) => item.id === userId);
  if (!user) return;
  selectedId.value = userId;
  editForm.display_name = user.display_name;
  editForm.status = user.status;
  editForm.role = user.roles[0] ?? "";
  editForm.new_password = "";
  editForm.oidc_subject = "";
  message.value = "";
  errorMessage.value = "";
}

function resetCreateForm() {
  Object.assign(createForm, {
    username: "",
    display_name: "",
    password: "",
    role: roles.value[0]?.key ?? "",
  });
}

function openCreate() {
  resetCreateForm();
  createOpen.value = true;
  selectedId.value = null;
}

async function createUser() {
  saving.value = true;
  errorMessage.value = "";
  try {
    const created = await qaApi.createUser({
      username: createForm.username,
      display_name: createForm.display_name,
      password: createForm.password,
      role: createForm.role,
    });
    createOpen.value = false;
    await load();
    openUser(created.id);
    message.value = `已创建用户 ${created.username}`;
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "创建用户失败";
  } finally {
    saving.value = false;
  }
}

async function saveUser() {
  if (!selected.value) return;
  saving.value = true;
  errorMessage.value = "";
  try {
    const updated = await qaApi.updateUser(selected.value.id, {
      display_name: editForm.display_name,
      status: editForm.status,
      role: editForm.role,
    });
    await load();
    openUser(updated.id);
    message.value = "用户资料、状态与角色已保存";
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "保存用户失败";
  } finally {
    saving.value = false;
  }
}

async function resetPassword() {
  if (!selected.value || !editForm.new_password) return;
  if (!window.confirm(`确认重置 ${selected.value.username} 的密码并撤销其现有会话？`)) return;
  saving.value = true;
  errorMessage.value = "";
  try {
    await qaApi.resetUserPassword(selected.value.id, { new_password: editForm.new_password });
    editForm.new_password = "";
    message.value = "密码已重置，原会话已失效";
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "密码重置失败";
  } finally {
    saving.value = false;
  }
}

async function revokeSessions() {
  if (!selected.value || !window.confirm(`确认撤销 ${selected.value.username} 的全部登录会话？`)) return;
  saving.value = true;
  try {
    const result = await qaApi.revokeUserSessions(selected.value.id);
    message.value = `已撤销 ${result.revoked_sessions} 个会话`;
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "会话撤销失败";
  } finally {
    saving.value = false;
  }
}

async function bindOidcIdentity() {
  if (!selected.value || !editForm.oidc_subject) return;
  if (!window.confirm(`确认将 Keycloak subject 显式绑定到 ${selected.value.username}？`)) return;
  saving.value = true;
  errorMessage.value = "";
  try {
    await qaApi.bindUserOidcIdentity(selected.value.id, editForm.oidc_subject);
    editForm.oidc_subject = "";
    message.value = "OIDC 稳定 subject 已绑定；权限仍由平台本地角色决定";
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : "OIDC 账号绑定失败";
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <section>
    <PageHeader
      eyebrow="IDENTITY & ACCESS"
      title="用户、角色与权限"
      description="后端执行真正的 RBAC 校验；页面隐藏按钮只是改善体验，不是安全边界。用户采用禁用而非物理删除，改密会撤销原会话。"
    >
      <template #actions>
        <button v-if="canManage" class="button primary" @click="openCreate">新增本地用户</button>
      </template>
    </PageHeader>

    <div v-if="message" class="notice online">{{ message }}</div>
    <div v-if="errorMessage" class="notice">{{ errorMessage }}</div>

    <div class="access-grid">
      <aside class="panel users-panel">
        <div class="panel-title"><div><small>ACCOUNTS</small><h2>用户列表</h2></div><span>{{ users.length }}</span></div>
        <p v-if="loading" class="empty">加载中…</p>
        <button
          v-for="user in users"
          v-else
          :key="user.id"
          :class="{ active: selectedId === user.id }"
          @click="openUser(user.id)"
        >
          <span><b>{{ user.display_name }}</b><small>@{{ user.username }} · {{ user.roles.join(" / ") }}</small></span>
          <StatusBadge :status="user.status" />
        </button>
      </aside>

      <article v-if="createOpen" class="panel editor">
        <div class="panel-title"><div><small>CREATE</small><h2>创建用户</h2></div><button class="link-button" @click="createOpen=false">关闭</button></div>
        <form @submit.prevent="createUser">
          <div class="field-grid">
            <label>用户名<input v-model.trim="createForm.username" minlength="3" maxlength="50" required /></label>
            <label>显示名称<input v-model.trim="createForm.display_name" maxlength="100" required /></label>
            <label>初始密码<input v-model="createForm.password" type="password" minlength="12" maxlength="200" required /></label>
          </div>
          <fieldset><legend>分配角色（单选）</legend><label v-for="role in roles" :key="role.key" class="role-check"><input v-model="createForm.role" type="radio" :value="role.key" /> <span><b>{{ role.name }}</b><small>{{ role.description }}</small></span></label></fieldset>
          <button class="button primary" :disabled="saving || !createForm.role">{{ saving ? "创建中…" : "创建用户" }}</button>
        </form>
      </article>

      <article v-else-if="selected" class="panel editor">
        <div class="panel-title"><div><small>ACCOUNT</small><h2>{{ selected.display_name }} <code>@{{ selected.username }}</code></h2></div><StatusBadge :status="selected.status" /></div>
        <form @submit.prevent="saveUser">
          <div class="field-grid">
            <label>显示名称<input v-model.trim="editForm.display_name" :disabled="!canManage" maxlength="100" required /></label>
            <label>账号状态<select v-model="editForm.status" :disabled="!canManage"><option value="active">启用</option><option value="disabled">禁用</option></select></label>
          </div>
          <fieldset><legend>角色与权限（单选）</legend><label v-for="role in roles" :key="role.key" class="role-check"><input v-model="editForm.role" type="radio" :value="role.key" :disabled="!canManage" /> <span><b>{{ role.name }} <em v-if="role.builtin">内置</em></b><small>{{ role.description }}</small><code>{{ role.permissions.join(" · ") }}</code></span></label></fieldset>
          <div v-if="canManage" class="form-actions"><button class="button primary" :disabled="saving || !editForm.role">保存资料与角色</button><button class="button" type="button" :disabled="saving" @click="revokeSessions">撤销全部会话</button></div>
        </form>

        <form v-if="canManage" class="password-box" @submit.prevent="resetPassword">
          <label>管理员重置密码<input v-model="editForm.new_password" type="password" minlength="12" maxlength="200" placeholder="至少 12 位" required /></label>
          <button class="button" :disabled="saving">重置并强制重新登录</button>
        </form>

        <form v-if="canManage" class="password-box oidc-box" @submit.prevent="bindOidcIdentity">
          <label>
            Keycloak 稳定 subject（用户 id）
            <input
              v-model.trim="editForm.oidc_subject"
              minlength="1"
              maxlength="255"
              placeholder="从本机 kcadm 查询，不要填用户名或邮箱"
              required
            />
          </label>
          <button class="button" :disabled="saving">显式绑定 OIDC</button>
        </form>
      </article>

      <article v-else class="panel empty">选择用户查看权限，或创建新的本地学习账号。</article>
    </div>
  </section>
</template>

<style scoped>
.access-grid{display:grid;grid-template-columns:340px minmax(0,1fr);gap:16px;align-items:start}.users-panel{padding:18px 0}.users-panel .panel-title{padding:0 18px}.users-panel .panel-title>span{color:var(--muted);font-size:10px}.users-panel>button{width:100%;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 18px;border:0;border-top:1px solid #edf1ef;background:transparent;text-align:left}.users-panel>button.active{background:#eff8f4;box-shadow:inset 3px 0 #35b77d}.users-panel button span b,.users-panel button span small{display:block}.users-panel button span b{font-size:11px}.users-panel button span small{margin-top:4px;color:var(--muted);font-size:8px}.editor form{display:grid;gap:16px}.field-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}label{display:grid;gap:6px;color:#64716c;font-size:9px;font-weight:700}input,select{width:100%;height:39px;padding:0 10px;border:1px solid #d9e3de;border-radius:8px;background:#fff}fieldset{display:grid;gap:8px;margin:0;padding:14px;border:1px solid #e3eae6;border-radius:10px}legend{padding:0 6px;color:#65736d;font-size:9px;font-weight:800}.role-check{display:grid;grid-template-columns:auto 1fr;align-items:start;padding:8px;border-radius:8px;background:#f7faf8}.role-check>input{width:14px;height:14px;margin-top:2px}.role-check b,.role-check small,.role-check code{display:block}.role-check small{margin-top:3px;color:var(--muted);font-weight:400}.role-check code{margin-top:5px;color:#708078;font-size:7px;line-height:1.5}.role-check em{padding:2px 4px;border-radius:4px;color:#287454;background:#e7f5ee;font-size:7px;font-style:normal}.form-actions{display:flex;gap:8px}.password-box{display:grid!important;grid-template-columns:1fr auto;align-items:end;margin-top:20px;padding-top:18px;border-top:1px solid #edf1ef}.oidc-box{margin-top:12px}.link-button{border:0;color:var(--green);background:transparent;font-size:9px}@media(max-width:900px){.access-grid{grid-template-columns:1fr}.field-grid{grid-template-columns:1fr}}@media(max-width:600px){.password-box{grid-template-columns:1fr}}
</style>
