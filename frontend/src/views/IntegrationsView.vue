<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { ApiError, qaApi } from "@/api";
import type {
  ProviderConnection,
  ProviderConnectionCreate,
  ProviderKind,
  ProviderRuntimeStatus,
  ProviderRun,
} from "@/api";
import { useAuthSession } from "@/auth/session";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";

const auth = useAuthSession();
const canManage = computed(() => auth.can("integrations.manage"));
const connections = ref<ProviderConnection[]>([]);
const runtimeStatus = ref<ProviderRuntimeStatus | null>(null);
const selectedId = ref("");
const runs = ref<ProviderRun[]>([]);
const loading = ref(true);
const busy = ref(false);
const errorMessage = ref("");
const message = ref("");
const editingId = ref<string | null>(null);
let connectionsRequestVersion = 0;
let runsRequestVersion = 0;

const form = ref({
  name: "本地 CI 教学模拟器",
  kind: "local" as ProviderKind,
  baseUrl: "",
  definitionRef: "local-quality-gate",
  secretEnvVar: "",
  enabled: true,
  username: "",
  jobName: "",
  projectId: "",
  pipelineId: "",
  userId: "",
  apiPrefix: "",
});
const triggerRef = ref("main");
const triggerVariables = ref("SOURCE=qa-platform\nMODE=learning");

const kindLabels: Record<ProviderKind, string> = {
  local: "本地模拟器",
  learning_ci: "自建 Learning CI Lab",
  jenkins: "自建 Jenkins",
  gitlab: "自建 GitLab CI",
  bk_ci: "自建蓝盾 BK-CI",
};
const runLabels: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
  unknown: "未知",
};

const selected = computed(
  () => connections.value.find((item) => item.id === selectedId.value) ?? null,
);
const requiresNetworkSecret = computed(() => form.value.kind !== "local");
const requiresConfiguredBaseUrl = computed(() =>
  ["jenkins", "gitlab", "bk_ci"].includes(form.value.kind),
);
const isEditing = computed(() => editingId.value !== null);
const anyNetworkLabEnabled = computed(
  () => runtimeStatus.value?.network_providers_allowed === true,
);
function runtimeAllowsKind(kind: ProviderKind): boolean {
  if (kind === "local") return true;
  if (kind === "learning_ci") return runtimeStatus.value?.mode === "ci_lab_local";
  return runtimeStatus.value?.mode === "self_hosted_lab";
}
const runtimeAllowsFormKind = computed(() => runtimeAllowsKind(form.value.kind));
const effectiveDefinitionRef = computed(() => {
  if (form.value.kind === "jenkins") return form.value.jobName.trim();
  if (form.value.kind === "gitlab") return form.value.projectId.trim();
  if (form.value.kind === "bk_ci") return form.value.pipelineId.trim();
  return form.value.definitionRef.trim();
});
const canTriggerSelected = computed(() => {
  const item = selected.value;
  return Boolean(
    canManage.value &&
      item?.enabled &&
      (item.kind === "local" ||
        (runtimeAllowsKind(item.kind) && item.secret_configured)),
  );
});

function readableError(error: unknown): string {
  return error instanceof ApiError || error instanceof Error
    ? error.message
    : "本机 API 请求失败";
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function dateTime(value: string | null): string {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
}

function safeRunUrl(value: string | null): string | null {
  const connection = selected.value;
  if (
    !value ||
    !connection ||
    !runtimeAllowsKind(connection.kind) ||
    !connection.base_url ||
    connection.kind === "local"
  ) return null;
  try {
    const parsed = new URL(value);
    const base = new URL(connection.base_url);
    return (
      (parsed.protocol === "http:" || parsed.protocol === "https:") &&
      parsed.origin === base.origin
    ) ? parsed.href : null;
  } catch {
    return null;
  }
}

function clearFeedback() {
  errorMessage.value = "";
  message.value = "";
}

function resetForm(kind: ProviderKind = "local") {
  editingId.value = null;
  form.value = {
    name: kind === "local" ? "本地 CI 教学模拟器" : `${kindLabels[kind]} 教学连接`,
    kind,
    baseUrl: "",
    definitionRef: kind === "local"
      ? "local-quality-gate"
      : kind === "learning_ci"
        ? "local-quality-gate"
        : "",
    secretEnvVar: kind === "learning_ci" ? "QA_PROVIDER_SECRET_CI_LAB" : "",
    enabled: kind === "local",
    username: "",
    jobName: "",
    projectId: "",
    pipelineId: "",
    userId: "",
    apiPrefix: "",
  };
  clearFeedback();
}

function selectConnection(item: ProviderConnection) {
  if (loading.value || busy.value) return;
  if (selectedId.value === item.id) return;
  selectedId.value = item.id;
  runs.value = [];
  if (editingId.value && editingId.value !== item.id) resetForm();
}

function editConnection(item: ProviderConnection) {
  editingId.value = item.id;
  form.value = {
    name: item.name,
    kind: item.kind,
    baseUrl: item.base_url ?? "",
    definitionRef: item.definition_ref,
    secretEnvVar: item.secret_env_var ?? "",
    enabled: item.enabled,
    username: item.config.username ?? "",
    jobName: item.config.job_name ?? "",
    projectId: item.config.project_id ?? "",
    pipelineId: item.config.pipeline_id ?? "",
    userId: item.config.user_id ?? "",
    apiPrefix: item.config.api_prefix ?? "",
  };
  clearFeedback();
}

function connectionConfig(): Record<string, string> {
  if (form.value.kind === "jenkins") {
    return { username: form.value.username.trim(), job_name: form.value.jobName.trim() };
  }
  if (form.value.kind === "gitlab") {
    return { project_id: form.value.projectId.trim() };
  }
  if (form.value.kind === "bk_ci") {
    const config: Record<string, string> = {
      project_id: form.value.projectId.trim(),
      pipeline_id: form.value.pipelineId.trim(),
      user_id: form.value.userId.trim(),
    };
    if (form.value.apiPrefix.trim()) config.api_prefix = form.value.apiPrefix.trim();
    return config;
  }
  return {};
}

function parseVariables(): Record<string, string> {
  const variables: Record<string, string> = {};
  for (const [index, raw] of triggerVariables.value.split(/\r?\n/).entries()) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf("=");
    if (separator < 1) throw new Error(`变量第 ${index + 1} 行应为 KEY=value`);
    const key = line.slice(0, separator).trim();
    if (/secret|token|password|passwd|credential|api_key/i.test(key)) {
      throw new Error(`变量 ${key} 疑似凭据；请改用服务端环境变量`);
    }
    variables[key] = line.slice(separator + 1).trim();
  }
  return variables;
}

async function loadConnections() {
  const requestVersion = ++connectionsRequestVersion;
  loading.value = true;
  try {
    const [next, nextRuntimeStatus] = await Promise.all([
      qaApi.listProviderConnections(),
      qaApi.getProviderRuntimeStatus(),
    ]);
    if (requestVersion !== connectionsRequestVersion) return;
    connections.value = next;
    runtimeStatus.value = nextRuntimeStatus;
    if (!connections.value.some((item) => item.id === selectedId.value)) {
      selectedId.value = connections.value[0]?.id ?? "";
    }
    errorMessage.value = "";
  } catch (error) {
    if (requestVersion === connectionsRequestVersion) errorMessage.value = readableError(error);
  } finally {
    if (requestVersion === connectionsRequestVersion) loading.value = false;
  }
}

async function loadRuns() {
  const connectionId = selectedId.value;
  const requestVersion = ++runsRequestVersion;
  if (!connectionId) {
    runs.value = [];
    return;
  }
  try {
    const next = await qaApi.listProviderRuns(connectionId);
    if (requestVersion === runsRequestVersion && selectedId.value === connectionId) {
      runs.value = next;
    }
  } catch (error) {
    if (requestVersion === runsRequestVersion) errorMessage.value = readableError(error);
  }
}

async function refresh() {
  clearFeedback();
  await loadConnections();
  await loadRuns();
}

async function saveConnection() {
  if (!canManage.value || loading.value || busy.value) return;
  clearFeedback();
  const data = form.value;
  const definitionRef = effectiveDefinitionRef.value;
  if (!data.name.trim() || !definitionRef) {
    errorMessage.value = "连接名称和定义引用不能为空";
    return;
  }
  if (requiresNetworkSecret.value) {
    if ((requiresConfiguredBaseUrl.value && !data.baseUrl.trim()) || !data.secretEnvVar.trim()) {
      errorMessage.value = "自建实验室 Provider 的固定地址/凭据引用不完整";
      return;
    }
    if (!/^QA_PROVIDER_SECRET_[A-Z0-9_]{1,109}$/.test(data.secretEnvVar.trim())) {
      errorMessage.value = "凭据变量必须使用专用 QA_PROVIDER_SECRET_ 前缀，并加入后端白名单";
      return;
    }
    if (data.kind === "learning_ci" && data.secretEnvVar.trim() !== "QA_PROVIDER_SECRET_CI_LAB") {
      errorMessage.value = "Learning CI 只能使用固定的 QA_PROVIDER_SECRET_CI_LAB 引用";
      return;
    }
    if (Object.values(connectionConfig()).some((value) => !value)) {
      errorMessage.value = "请补齐当前 Provider 的必填配置";
      return;
    }
  }
  busy.value = true;
  try {
    const payload: ProviderConnectionCreate = {
      name: data.name.trim(),
      kind: data.kind,
      base_url: requiresConfiguredBaseUrl.value ? data.baseUrl.trim() : null,
      definition_ref: definitionRef,
      config: connectionConfig(),
      secret_env_var: requiresNetworkSecret.value ? data.secretEnvVar.trim() : null,
      enabled: requiresNetworkSecret.value && !runtimeAllowsFormKind.value
        ? false
        : data.enabled,
    };
    let saved: ProviderConnection;
    if (editingId.value) {
      const current = connections.value.find((item) => item.id === editingId.value);
      if (!current) throw new Error("待编辑连接已不存在，请刷新后重试");
      saved = await qaApi.updateProviderConnection(editingId.value, {
        name: payload.name,
        base_url: payload.base_url,
        definition_ref: payload.definition_ref,
        config: payload.config,
        secret_env_var: payload.secret_env_var,
        enabled: payload.enabled,
        version: current.version,
      });
      message.value = "连接配置已更新";
    } else {
      saved = await qaApi.createProviderConnection(payload);
      message.value = saved.kind === "local"
        ? "本地模拟连接已创建"
        : "自建实验室元数据已保存；local_lab 下保持禁用且不会联网";
    }
    const index = connections.value.findIndex((item) => item.id === saved.id);
    if (index < 0) connections.value.unshift(saved);
    else connections.value.splice(index, 1, saved);
    selectedId.value = saved.id;
    editConnection(saved);
    message.value = saved.kind === "local"
      ? "连接保存成功，本地模拟不会访问网络"
      : runtimeAllowsKind(saved.kind)
        ? "自建实验室连接已保存；仍受固定目标、凭据和内部网络限制"
        : "仅保存自建实验室元数据；local_lab 硬锁仍在生效";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function removeConnection(item: ProviderConnection) {
  if (!canManage.value || loading.value || busy.value) return;
  if (!window.confirm(`删除连接“${item.name}”？存在运行历史时后端会拒绝删除。`)) return;
  busy.value = true;
  clearFeedback();
  try {
    await qaApi.deleteProviderConnection(item.id);
    connections.value = connections.value.filter((entry) => entry.id !== item.id);
    if (selectedId.value === item.id) selectedId.value = connections.value[0]?.id ?? "";
    resetForm();
    message.value = "未产生运行历史的连接已删除";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function testConnection(item: ProviderConnection) {
  if (!canManage.value || loading.value || busy.value) return;
  busy.value = true;
  clearFeedback();
  try {
    const result = await qaApi.testProviderConnection(item.id);
    message.value = `${result.message}；网络探测：${result.network_probe_performed ? "已执行" : "未执行"}`;
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function triggerConnection() {
  const item = selected.value;
  if (!item || !canTriggerSelected.value || loading.value || busy.value) return;
  const connectionId = item.id;
  runsRequestVersion += 1;
  busy.value = true;
  clearFeedback();
  try {
    const created = await qaApi.triggerProviderConnection(connectionId, {
      ref: triggerRef.value.trim() || null,
      variables: parseVariables(),
      correlation_id: `web-${crypto.randomUUID()}`,
    });
    if (selectedId.value === connectionId) {
      runs.value.unshift(created);
      message.value = item.kind === "local"
        ? "已创建本地模拟运行，没有访问外部网络"
        : "已触发自建实验室运行，并受四类白名单与内部网络保护";
    }
  } catch (error) {
    if (selectedId.value === connectionId) errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function refreshRun(run: ProviderRun) {
  const item = selected.value;
  if (!item || loading.value || busy.value) return;
  const connectionId = item.id;
  runsRequestVersion += 1;
  busy.value = true;
  clearFeedback();
  try {
    const updated = await qaApi.getProviderRun(connectionId, run.id);
    if (selectedId.value === connectionId) {
      replaceRun(updated);
      message.value = "运行状态已刷新";
    }
  } catch (error) {
    if (selectedId.value === connectionId) errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function cancelRun(run: ProviderRun) {
  const item = selected.value;
  if (!item || !canManage.value || loading.value || busy.value) return;
  const connectionId = item.id;
  runsRequestVersion += 1;
  busy.value = true;
  clearFeedback();
  try {
    const updated = await qaApi.cancelProviderRun(connectionId, run.id);
    if (selectedId.value === connectionId) {
      replaceRun(updated);
      message.value = "取消请求已处理";
    }
  } catch (error) {
    if (selectedId.value === connectionId) errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

function replaceRun(next: ProviderRun) {
  const index = runs.value.findIndex((item) => item.id === next.id);
  if (index < 0) runs.value.unshift(next);
  else runs.value.splice(index, 1, next);
}

watch(
  () => form.value.kind,
  (kind, previous) => {
    if (!editingId.value && kind !== previous) resetForm(kind);
  },
);
watch(selectedId, () => void loadRuns());
onMounted(() => void refresh());
</script>

<template>
  <section>
    <PageHeader
      eyebrow="INTEGRATIONS"
      title="CI Provider 集成"
      description="默认只运行无网络的 Local CI；Learning CI Lab 是我们自建的独立 HTTP 服务，不是 Jenkins、GitLab 或蓝盾。"
    >
      <template #actions>
        <button class="button" :disabled="loading || busy" @click="refresh">刷新</button>
        <button v-if="canManage" class="button primary" :disabled="loading || busy" @click="resetForm()">新建连接</button>
      </template>
    </PageHeader>

    <div class="notice safety">
      <b>{{ runtimeStatus?.mode === 'ci_lab_local' ? 'Learning CI 固定本机模式' : runtimeStatus?.mode === 'self_hosted_lab' ? '自建实验室模式' : 'local_lab 离线硬锁' }}</b>
      <span v-if="runtimeStatus?.mode === 'ci_lab_local'">只能访问代码内固定的 CI Lab IP、端口和 /32 网络；页面不能改写目标地址。</span>
      <span v-else-if="anyNetworkLabEnabled">只允许我们自建的目标，并同时受所有权确认、连接、Host/Port/CIDR/Secret 白名单和内部容器网络约束。</span>
      <span v-else>Jenkins、GitLab、蓝盾会在读取 Secret、DNS 和 HTTP 之前被拒绝；项目不存在 external/public 模式。</span>
    </div>
    <div v-if="message" class="notice online"><b>完成</b><span>{{ message }}</span></div>
    <div v-if="errorMessage" class="notice error"><b>请求失败</b><span>{{ errorMessage }}</span></div>

    <div class="integration-grid">
      <article class="panel connection-list">
        <div class="panel-title"><div><small>CONNECTIONS</small><h2>连接清单</h2></div><b>{{ connections.length }}</b></div>
        <p v-if="loading" class="empty compact">正在读取本机数据库…</p>
        <p v-else-if="!connections.length" class="empty compact">尚未创建连接，建议先建一个本地模拟器。</p>
        <button
          v-for="item in connections"
          :key="item.id"
          :class="{ active: selectedId === item.id }"
          :disabled="loading || busy"
          @click="selectConnection(item)"
        >
          <span class="provider-mark">{{ item.kind === "local" ? "L" : item.kind === "learning_ci" ? "LC" : item.kind === "bk_ci" ? "BK" : item.kind.slice(0, 2).toUpperCase() }}</span>
          <span><b>{{ item.name }}</b><small>{{ kindLabels[item.kind] }} · {{ item.kind === "learning_ci" ? "固定独立本机服务" : item.base_url ?? "本地进程内模拟" }}</small></span>
          <StatusBadge :status="item.enabled ? 'active' : 'disabled'" :label="item.enabled ? '已启用' : '默认禁用'" />
        </button>
      </article>

      <article class="panel editor">
        <div class="panel-title">
          <div><small>{{ isEditing ? "EDIT CONNECTION" : "NEW CONNECTION" }}</small><h2>{{ isEditing ? `编辑：${form.name}` : "登记 Provider" }}</h2></div>
          <button v-if="isEditing" class="text-button" :disabled="loading || busy" @click="resetForm()">退出编辑</button>
        </div>
        <fieldset :disabled="!canManage || loading || busy">
          <div class="form-grid">
            <label><span>连接名称</span><input v-model="form.name" maxlength="150" /></label>
            <label><span>Provider 类型</span><select v-model="form.kind" :disabled="isEditing"><option value="local">本地模拟器</option><option value="learning_ci">自建 Learning CI Lab</option><option value="jenkins">自建 Jenkins</option><option value="gitlab">自建 GitLab CI</option><option value="bk_ci">自建蓝盾 BK-CI</option></select></label>
            <label v-if="form.kind === 'local' || form.kind === 'learning_ci'" class="wide"><span>{{ form.kind === "learning_ci" ? "CI Lab 固定定义" : "本地定义引用" }}</span><input v-model="form.definitionRef" maxlength="300" placeholder="local-quality-gate" /></label>
            <label v-else class="wide"><span>Provider 定义引用（由资源标识自动生成）</span><input :value="effectiveDefinitionRef" disabled /><small>Jenkins 使用 Job 名，GitLab 使用 Project ID，BK-CI 使用 Pipeline ID，避免重复配置不一致。</small></label>
            <template v-if="requiresNetworkSecret">
              <label v-if="requiresConfiguredBaseUrl" class="wide"><span>自建服务地址</span><input v-model="form.baseUrl" type="url" placeholder="https://ci.lab.test" /></label>
              <p v-else class="wide fixed-target">Learning CI 目标由后端固定：宿主机 127.0.0.1:23020，容器内 172.30.60.2:8080。</p>
              <label class="wide secret-name"><span>凭据环境变量名（不是 Token）</span><input v-model="form.secretEnvVar" autocomplete="off" placeholder="QA_PROVIDER_SECRET_JENKINS_TOKEN" /><small>必须使用专用前缀并加入服务端 allowlist；这里只保存变量名，值由运维注入后端进程。</small></label>
            </template>
            <template v-if="form.kind === 'jenkins'">
              <label><span>Jenkins 用户名</span><input v-model="form.username" /></label>
              <label><span>Job 名称</span><input v-model="form.jobName" /></label>
            </template>
            <template v-if="form.kind === 'gitlab'">
              <label class="wide"><span>GitLab Project ID</span><input v-model="form.projectId" /></label>
            </template>
            <template v-if="form.kind === 'bk_ci'">
              <label><span>BK-CI Project ID</span><input v-model="form.projectId" /></label>
              <label><span>Pipeline ID</span><input v-model="form.pipelineId" /></label>
              <label><span>User ID</span><input v-model="form.userId" /></label>
              <label><span>API Prefix（可选）</span><input v-model="form.apiPrefix" placeholder="/ms/process/api/user/builds" /></label>
            </template>
          </div>
          <label class="enable-line"><input v-model="form.enabled" type="checkbox" :disabled="requiresNetworkSecret && !runtimeAllowsFormKind" />启用该连接 <small v-if="requiresNetworkSecret && !runtimeAllowsFormKind">（当前运行模式不匹配，固定禁用且不会联网）</small><small v-else-if="requiresNetworkSecret">（仍不会绕过固定目标、凭据与内部网络门禁）</small></label>
          <button class="button primary" type="button" @click="saveConnection">{{ busy ? "保存中…" : "保存连接" }}</button>
        </fieldset>
        <p v-if="!canManage" class="read-only">当前角色只有查看权限，不能修改或触发集成。</p>
      </article>
    </div>

    <article v-if="selected" class="panel selected-panel">
      <header>
        <div><small>SELECTED · {{ kindLabels[selected.kind] }}</small><h2>{{ selected.name }}</h2><p><code>{{ selected.definition_ref }}</code> · 更新于 {{ dateTime(selected.updated_at) }}</p></div>
        <div class="selected-actions">
          <StatusBadge :status="selected.secret_configured || selected.kind === 'local' ? 'ready' : 'disabled'" :label="selected.kind === 'local' ? '无需凭据' : selected.secret_configured ? '凭据环境已就绪' : '环境变量未配置'" />
          <button v-if="canManage" class="button" :disabled="loading || busy" @click="editConnection(selected)">编辑</button>
          <button v-if="canManage" class="button danger-button" :disabled="loading || busy" @click="removeConnection(selected)">删除</button>
          <button v-if="canManage" class="button" :disabled="loading || busy || (selected.kind !== 'local' && (!selected.enabled || !runtimeAllowsKind(selected.kind)))" @click="testConnection(selected)">静态测试</button>
        </div>
      </header>
      <div class="trigger-box">
        <label><span>本次 Ref</span><input v-model="triggerRef" placeholder="main" /></label>
        <label class="variables"><span>非敏感变量（每行 KEY=value）</span><textarea v-model="triggerVariables" rows="3" /></label>
        <button class="button primary" :disabled="loading || busy || !canTriggerSelected" @click="triggerConnection">触发运行</button>
      </div>
      <p v-if="!canTriggerSelected" class="gate-hint">
        Local 连接需要启用；网络 Provider 还要求后端处于对应的 ci_lab_local/self_hosted_lab、凭据已配置且通过全部安全门禁。页面不会提供临时 Token 输入框。
      </p>
    </article>

    <div class="table-wrap run-table">
      <table>
        <thead><tr><th>运行</th><th>状态</th><th>外部标识</th><th>关联 ID</th><th>更新时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="run in runs" :key="run.id">
            <td><code>#{{ shortId(run.id) }}</code><b>{{ run.message ?? "Provider 运行" }}</b></td>
            <td><StatusBadge :status="run.status" :label="runLabels[run.status] ?? run.status" /><small class="raw">{{ run.raw_status }}</small></td>
            <td><a v-if="safeRunUrl(run.web_url)" :href="safeRunUrl(run.web_url) ?? undefined" target="_blank" rel="noopener noreferrer">{{ run.external_id }}</a><code v-else>{{ run.external_id }}</code></td>
            <td><code>{{ run.correlation_id ?? "—" }}</code></td>
            <td>{{ dateTime(run.updated_at) }}</td>
            <td class="row-actions"><button :disabled="loading || busy" @click="refreshRun(run)">刷新</button><button v-if="canManage && ['queued','running'].includes(run.status)" :disabled="loading || busy" @click="cancelRun(run)">取消</button></td>
          </tr>
          <tr v-if="!runs.length"><td colspan="6" class="empty compact">选择连接后查看运行历史</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.fixed-target{margin:0;padding:10px 12px;border:1px solid #dcebe4;border-radius:8px;color:#47705f;background:#f2faf6;font-size:9px;line-height:1.5}
.notice.error{border-color:#efcaca;color:#a33c3c;background:#fff2f2}.integration-grid{display:grid;grid-template-columns:minmax(300px,.75fr) minmax(440px,1.25fr);gap:16px;align-items:start}.connection-list{padding:20px 0}.connection-list>.panel-title{padding:0 20px 10px}.connection-list>.panel-title>b{color:var(--green);font-size:18px}.connection-list>button{display:grid;grid-template-columns:34px 1fr auto;align-items:center;gap:10px;width:100%;padding:13px 18px;border:0;border-top:1px solid #edf1ef;color:inherit;background:transparent;text-align:left}.connection-list>button.active{background:#eff8f4;box-shadow:inset 3px 0 #35b77d}.connection-list button span:nth-child(2) b,.connection-list button span:nth-child(2) small{display:block}.connection-list button span:nth-child(2) b{font-size:10px}.connection-list button span:nth-child(2) small{max-width:260px;margin-top:4px;overflow:hidden;color:var(--muted);font-size:8px;text-overflow:ellipsis;white-space:nowrap}.provider-mark{display:grid;width:32px;height:32px;place-items:center;border-radius:9px;color:#277357;background:#e8f7f0;font-size:9px;font-weight:800}.compact{padding:28px 15px}.text-button{border:0;color:var(--green);background:transparent;font-size:9px}.editor fieldset{padding:0;border:0}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.form-grid label,.trigger-box label{display:grid;gap:6px}.form-grid label>span,.trigger-box label>span{color:#64716c;font-size:9px;font-weight:700}.form-grid input,.form-grid select,.trigger-box input,.trigger-box textarea{width:100%;min-height:39px;padding:9px 10px;border:1px solid #d9e3de;border-radius:8px;background:#fff;outline:none}.form-grid input:focus,.form-grid select:focus,.trigger-box input:focus,.trigger-box textarea:focus{border-color:#74b99b;box-shadow:0 0 0 3px #edf8f3}.form-grid .wide{grid-column:1/-1}.secret-name{padding:11px;border:1px solid #e5dfc6;border-radius:9px;background:#fffaf0}.secret-name small{color:#92743a;font-size:8px;line-height:1.5}.enable-line{display:flex;align-items:center;gap:7px;margin:15px 0;color:#44534d;font-size:10px}.enable-line small{color:var(--muted)}button:disabled{cursor:not-allowed;opacity:.5}.read-only,.gate-hint{margin:13px 0 0;color:var(--muted);font-size:9px}.selected-panel{margin-top:16px}.selected-panel>header{display:flex;align-items:flex-start;justify-content:space-between;gap:15px;padding-bottom:15px;border-bottom:1px solid #edf1ef}.selected-panel header>div:first-child>small{color:var(--green);font-size:8px;letter-spacing:.1em}.selected-panel h2{margin:5px 0}.selected-panel p{margin:0;color:var(--muted);font-size:9px}.selected-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap;justify-content:flex-end}.danger-button{color:#b44343}.trigger-box{display:grid;grid-template-columns:minmax(140px,.45fr) minmax(260px,1fr) auto;align-items:end;gap:12px;padding-top:16px}.trigger-box textarea{resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:9px}.run-table{margin-top:16px}.run-table a{color:var(--green);font-weight:700}.run-table .raw{display:block;margin-top:5px;color:var(--muted);font-size:8px}.row-actions{white-space:nowrap}.row-actions button{margin-right:5px;padding:5px 7px;border:1px solid var(--line);border-radius:6px;color:#56645e;background:#fff;font-size:8px}
@media(max-width:1000px){.integration-grid{grid-template-columns:1fr}.trigger-box{grid-template-columns:1fr 1fr}.trigger-box .variables{grid-row:1/3}.trigger-box .button{grid-column:1}}
@media(max-width:700px){.form-grid,.trigger-box{grid-template-columns:1fr}.trigger-box .variables,.trigger-box .button{grid-column:auto;grid-row:auto}.selected-panel>header{flex-direction:column}.selected-actions{justify-content:flex-start}.connection-list button span:nth-child(2) small{max-width:150px}}
</style>
