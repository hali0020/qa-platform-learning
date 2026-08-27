<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { qaApi, type PipelineRun, type PipelineStatus } from "@/api";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useAuthSession } from "@/auth/session";

const auth = useAuthSession();
const canManage = computed(() => auth.can("pipeline.manage"));

const runs = ref<PipelineRun[]>([]);
const selectedId = ref("");
const loading = ref(true);
const hasLoadedRuns = ref(false);
const actionBusy = ref(false);
const errorMessage = ref("");
const successMessage = ref("");
const pipelineName = ref("本地质量门禁");
const jobDurationMs = ref(500);
const shouldFail = ref(false);
let pollTimer: number | undefined;
let disposed = false;
let listRequestVersion = 0;

const labels: Record<PipelineStatus, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
};

const selected = computed(
  () => runs.value.find((run) => run.id === selectedId.value) ?? runs.value[0] ?? null,
);
const selectedLog = computed(() => {
  if (!selected.value) return "暂无运行日志";
  return selected.value.stages
    .flatMap((stage) => [
      `[${stage.status}] stage: ${stage.name}${stage.message ? ` · ${stage.message}` : ""}`,
      ...stage.jobs.map(
        (job) =>
          `  [${job.status}] job: ${job.name} · ${job.duration_ms}ms${job.message ? ` · ${job.message}` : ""}`,
      ),
    ])
    .join("\n");
});

const formatError = (error: unknown) =>
  error instanceof Error ? error.message : "本机 API 请求失败";
const variable = (run: PipelineRun, key: string, fallback: string) =>
  typeof run.variables[key] === "string" ? String(run.variables[key]) : fallback;
const shortId = (value: string) => value.slice(0, 8);

function replaceRun(next: PipelineRun) {
  const index = runs.value.findIndex((run) => run.id === next.id);
  if (index === -1) runs.value.unshift(next);
  else runs.value.splice(index, 1, next);
}

async function loadRuns(silent = false): Promise<boolean> {
  const requestVersion = ++listRequestVersion;
  if (!silent) loading.value = true;
  try {
    const next = await qaApi.listPipelineRuns();
    if (disposed || requestVersion !== listRequestVersion) return false;
    runs.value = next;
    hasLoadedRuns.value = true;
    if (!next.some((run) => run.id === selectedId.value)) {
      selectedId.value = next[0]?.id ?? "";
    }
    errorMessage.value = "";
    return true;
  } catch (error) {
    if (disposed || requestVersion !== listRequestVersion) return false;
    successMessage.value = "";
    errorMessage.value = formatError(error);
    return false;
  } finally {
    if (!silent && !disposed && requestVersion === listRequestVersion) loading.value = false;
  }
}

function stopPolling() {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer);
  pollTimer = undefined;
}

function schedulePolling() {
  stopPolling();
  if (disposed || actionBusy.value || !hasLoadedRuns.value) return;
  if (!runs.value.some((run) => run.status === "queued" || run.status === "running")) return;
  pollTimer = window.setTimeout(async () => {
    pollTimer = undefined;
    if (disposed) return;
    const succeeded = await loadRuns(true);
    if (succeeded && !disposed) schedulePolling();
  }, 800);
}

function beginMutation() {
  actionBusy.value = true;
  successMessage.value = "";
  errorMessage.value = "";
  stopPolling();
  // 使已经发出的列表请求失效，避免旧快照覆盖本次操作结果。
  listRequestVersion += 1;
}

async function triggerPipeline() {
  if (!canManage.value || actionBusy.value || loading.value) return;
  const name = pipelineName.value.trim();
  if (!name) {
    errorMessage.value = "请输入流水线名称";
    return;
  }
  beginMutation();
  try {
    const result = await qaApi.triggerPipeline({
      name,
      auto_start: true,
      idempotency_key: `web-${Date.now()}-${crypto.randomUUID()}`,
      variables: { provider: "local", branch: "main", source: "vue" },
      stages: [
        { name: "代码检查", jobs: [{ name: "lint", duration_ms: jobDurationMs.value }] },
        {
          name: "自动化测试",
          jobs: [
            {
              name: "pytest",
              duration_ms: jobDurationMs.value,
              should_fail: shouldFail.value,
            },
          ],
        },
        { name: "构建", jobs: [{ name: "vite build", duration_ms: jobDurationMs.value }] },
      ],
    });
    if (disposed) return;
    replaceRun(result.pipeline);
    hasLoadedRuns.value = true;
    selectedId.value = result.pipeline.id;
    errorMessage.value = "";
    successMessage.value = result.replayed ? "已返回幂等运行记录" : "本地流水线已触发";
  } catch (error) {
    if (disposed) return;
    errorMessage.value = formatError(error);
  } finally {
    if (!disposed) {
      actionBusy.value = false;
      schedulePolling();
    }
  }
}

async function cancelSelected() {
  if (!canManage.value || actionBusy.value || loading.value) return;
  const run = selected.value;
  if (!run || !["queued", "running"].includes(run.status)) return;
  beginMutation();
  try {
    const result = await qaApi.cancelPipeline(run.id);
    if (disposed) return;
    replaceRun(result.pipeline);
    hasLoadedRuns.value = true;
    errorMessage.value = "";
    successMessage.value = result.replayed ? "该运行此前已经取消" : "流水线已取消";
  } catch (error) {
    if (disposed) return;
    errorMessage.value = formatError(error);
  } finally {
    if (!disposed) {
      actionBusy.value = false;
      schedulePolling();
    }
  }
}

async function refreshRuns() {
  if (actionBusy.value) return;
  stopPolling();
  successMessage.value = "";
  const succeeded = await loadRuns();
  if (succeeded) schedulePolling();
}

onMounted(() => {
  disposed = false;
  void refreshRuns();
});
onBeforeUnmount(() => {
  disposed = true;
  listRequestVersion += 1;
  stopPolling();
});
</script>

<template>
  <section>
    <PageHeader
      eyebrow="PIPELINES"
      title="CI/CD 流水线"
      description="页面通过本机 API 触发、查询和取消异步流水线，状态与幂等记录会保存到 SQLite。"
    >
      <template #actions>
        <button class="button" :disabled="loading || actionBusy" @click="refreshRuns">刷新</button>
        <button v-if="canManage" class="button primary" :disabled="loading || actionBusy" @click="triggerPipeline">
          {{ actionBusy ? "处理中…" : "▶ 触发本地流水线" }}
        </button>
      </template>
    </PageHeader>

    <div class="notice online">
      <b>本机 API 实时数据</b>
      <span>只执行确定性的异步等待，不运行 Shell，也不访问任何外部 CI/CD 服务。</span>
    </div>
    <div v-if="errorMessage" class="notice error"><b>请求失败</b><span>{{ errorMessage }}</span></div>
    <div v-if="successMessage" class="notice online"><b>操作成功</b><span>{{ successMessage }}</span></div>

    <article v-if="canManage" class="panel pipeline-form">
      <label>
        <span>流水线名称</span>
        <input v-model="pipelineName" type="text" maxlength="100" :disabled="loading || actionBusy" />
      </label>
      <label>
        <span>单个 Job 模拟时长</span>
        <select v-model.number="jobDurationMs" :disabled="loading || actionBusy">
          <option :value="100">100 ms</option>
          <option :value="500">500 ms</option>
          <option :value="1000">1 秒</option>
        </select>
      </label>
      <label class="check"><input v-model="shouldFail" type="checkbox" :disabled="loading || actionBusy" />让自动化测试 Job 失败</label>
    </article>

    <p v-if="loading" class="empty">正在读取本机流水线记录…</p>
    <p v-else-if="!hasLoadedRuns" class="empty error-state">流水线记录尚未成功加载，请刷新重试。</p>
    <div v-else class="pipeline-grid">
      <article class="panel run-list">
        <h2>最近运行</h2>
        <button
          v-for="run in runs"
          :key="run.id"
          :class="{ active: run.id === selectedId }"
          @click="selectedId = run.id"
        >
          <code>#{{ shortId(run.id) }}</code>
          <span>
            <b>{{ run.name }}</b>
            <small>{{ variable(run, "branch", "—") }} · {{ variable(run, "provider", "local") }}</small>
          </span>
          <StatusBadge :status="run.status" :label="labels[run.status]" />
        </button>
        <p v-if="!runs.length && !errorMessage" class="empty compact">尚未触发流水线</p>
      </article>

      <article v-if="selected" class="panel run-detail">
        <header>
          <div>
            <code>RUN #{{ shortId(selected.id) }}</code>
            <h2>{{ selected.name }}</h2>
            <small>
              {{ variable(selected, "branch", "—") }} ·
              {{ variable(selected, "provider", "local").toUpperCase() }}
            </small>
          </div>
          <div class="detail-actions">
            <StatusBadge :status="selected.status" :label="labels[selected.status]" />
            <button
              v-if="canManage && (selected.status === 'queued' || selected.status === 'running')"
              class="button"
              :disabled="actionBusy"
              @click="cancelSelected"
            >取消</button>
          </div>
        </header>
        <div class="stages">
          <div v-for="(stage, index) in selected.stages" :key="stage.name">
            <i :class="stage.status">{{ index + 1 }}</i>
            <b>{{ stage.name }}</b>
            <small>{{ labels[stage.status] }}</small>
          </div>
        </div>
        <pre>{{ selectedLog }}</pre>
      </article>
    </div>
  </section>
</template>

<style scoped>
.pipeline-form { display:flex; align-items:end; gap:16px; margin-bottom:16px; }
.pipeline-form label:not(.check) { display:grid; flex:1; gap:7px; color:var(--muted); font-size:10px; }
.pipeline-form input[type="text"], .pipeline-form select { min-height:38px; padding:0 11px; border:1px solid var(--line); border-radius:8px; background:#fff; }
.pipeline-form .check { display:flex; align-items:center; gap:7px; min-height:38px; color:#4d5c56; font-size:10px; }
.notice.error { border-color:#efcaca; color:#a33c3c; background:#fff2f2; }
.error-state { color:#a33c3c; }
.detail-actions { display:flex; align-items:center; gap:8px; }
.empty.compact { padding:28px 12px; }
button:disabled { cursor:not-allowed; opacity:.55; }
@media(max-width:700px) { .pipeline-form { align-items:stretch; flex-direction:column; } }
</style>
