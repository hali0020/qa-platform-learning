<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ApiError, qaApi } from "@/api";
import type {
  AutomationSchedule,
  AutomationTask,
  Device,
  ScheduleCreate,
  ScheduleFire,
  ScheduleMisfirePolicy,
  ScheduleOverlapPolicy,
} from "@/api";
import { useAuthSession } from "@/auth/session";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";

type Tab = "tasks" | "devices" | "schedules";

const auth = useAuthSession();
const canReadDevices = computed(() => auth.can("devices.read"));
const canManageDevices = computed(() => auth.can("devices.manage"));
const canReadSchedules = computed(() => auth.can("schedules.read"));
const canManageSchedules = computed(() => auth.can("schedules.manage"));
const activeTab = ref<Tab>("tasks");
const loading = ref(true);
const busy = ref(false);
const message = ref("");
const errorMessage = ref("");

const tasks = ref<AutomationTask[]>([]);
const devices = ref<Device[]>([]);
const schedules = ref<AutomationSchedule[]>([]);
const selectedScheduleId = ref("");
const fires = ref<ScheduleFire[]>([]);
const heartbeatAgents = ref<Record<string, string>>({});
let firesRequestVersion = 0;

const registeredTaskTypes = [
  ["qa.import.validate", "批量导入预检"],
  ["qa.pipeline.poll", "流水线状态轮询"],
  ["qa.quality.generate", "质量报表生成"],
  ["qa.device.execute", "设备任务执行"],
] as const;

const taskForm = ref({
  taskType: "qa.quality.generate",
  payload: "{\n  \"project_id\": \"learning-project\"\n}",
  queue: "default",
  priority: 50,
  maxAttempts: 3,
  idempotencyKey: "",
});
const deviceForm = ref({
  name: "本地 Android 教学设备",
  agentId: "local-device-agent-01",
  kind: "phone",
  platform: "android",
  capabilities: "android, smoke",
});
const scheduleForm = ref({
  name: "每五分钟生成质量快照",
  taskType: "qa.quality.generate",
  payload: "{\n  \"project_id\": \"learning-project\"\n}",
  queue: "default",
  priority: 50,
  maxAttempts: 3,
  cron: "*/5 * * * *",
  timezone: "Asia/Shanghai",
  misfirePolicy: "fire_once" as ScheduleMisfirePolicy,
  overlapPolicy: "forbid" as ScheduleOverlapPolicy,
  graceSeconds: 60,
  catchUpLimit: 3,
  enabled: true,
});

const selectedSchedule = computed(
  () => schedules.value.find((item) => item.id === selectedScheduleId.value) ?? null,
);
const taskStats = computed(() => ({
  queued: tasks.value.filter((item) => ["queued", "retry_wait"].includes(item.status)).length,
  running: tasks.value.filter((item) => item.status === "running").length,
  failed: tasks.value.filter((item) => ["failed", "dead_letter"].includes(item.status)).length,
  completed: tasks.value.filter((item) => item.status === "succeeded").length,
}));

const taskLabels: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  retry_wait: "等待重试",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
  dead_letter: "死信",
};
const deviceLabels: Record<string, string> = {
  online: "在线",
  offline: "离线",
  busy: "占用中",
  maintenance: "维护中",
  disabled: "已停用",
  available: "可用",
};
const fireLabels: Record<string, string> = {
  enqueued: "已入队",
  skipped_misfire: "跳过补偿",
  skipped_overlap: "跳过重叠",
};

function readableError(error: unknown): string {
  return error instanceof ApiError || error instanceof Error
    ? error.message
    : "本机 API 请求失败";
}

function clearFeedback() {
  message.value = "";
  errorMessage.value = "";
}

function parseObject(value: string, label: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value || "{}");
  } catch {
    throw new Error(`${label}必须是有效 JSON`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${label}必须是 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

function capabilities(value: string): string[] {
  return [...new Set(value.split(",").map((item) => item.trim()).filter(Boolean))];
}

function dateTime(value: string | null): string {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function replaceTask(next: AutomationTask) {
  const index = tasks.value.findIndex((item) => item.id === next.id);
  if (index < 0) tasks.value.unshift(next);
  else tasks.value.splice(index, 1, next);
}

function replaceDevice(next: Device) {
  const index = devices.value.findIndex((item) => item.id === next.id);
  if (index < 0) devices.value.unshift(next);
  else devices.value.splice(index, 1, next);
}

async function loadTasks() {
  if (!canReadSchedules.value) return;
  tasks.value = await qaApi.listAutomationTasks();
}

async function loadDevices() {
  if (!canReadDevices.value) return;
  devices.value = await qaApi.listDevices();
}

async function loadSchedules() {
  if (!canReadSchedules.value) return;
  schedules.value = await qaApi.listSchedules();
  if (!schedules.value.some((item) => item.id === selectedScheduleId.value)) {
    selectedScheduleId.value = schedules.value[0]?.id ?? "";
  }
  await loadFires();
}

async function loadFires() {
  const requestVersion = ++firesRequestVersion;
  const scheduleId = selectedScheduleId.value;
  if (!canReadSchedules.value || !scheduleId) {
    fires.value = [];
    return;
  }
  try {
    const next = await qaApi.listScheduleFires(scheduleId);
    if (
      requestVersion === firesRequestVersion &&
      selectedScheduleId.value === scheduleId
    ) {
      fires.value = next;
    }
  } catch (error) {
    if (
      requestVersion === firesRequestVersion &&
      selectedScheduleId.value === scheduleId
    ) {
      throw error;
    }
  }
}

async function refresh() {
  loading.value = true;
  clearFeedback();
  try {
    await Promise.all([loadTasks(), loadDevices(), loadSchedules()]);
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    loading.value = false;
  }
}

async function enqueueTask() {
  if (!canManageSchedules.value || loading.value || busy.value) return;
  busy.value = true;
  clearFeedback();
  try {
    const result = await qaApi.enqueueAutomationTask({
      task_type: taskForm.value.taskType,
      payload: parseObject(taskForm.value.payload, "任务 Payload"),
      queue: taskForm.value.queue.trim(),
      priority: taskForm.value.priority,
      max_attempts: taskForm.value.maxAttempts,
      idempotency_key: taskForm.value.idempotencyKey.trim() || null,
    });
    replaceTask(result.task);
    message.value = result.replayed ? "幂等键命中，返回已有任务" : "任务已持久化入队";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function cancelTask(task: AutomationTask) {
  if (!canManageSchedules.value || loading.value || busy.value) return;
  busy.value = true;
  clearFeedback();
  try {
    replaceTask(await qaApi.cancelAutomationTask(task.id));
    message.value = task.status === "running"
      ? "已登记协作式取消请求，worker 将在安全点结束"
      : "任务已取消";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function retryTask(task: AutomationTask) {
  if (!canManageSchedules.value || loading.value || busy.value) return;
  busy.value = true;
  clearFeedback();
  try {
    replaceTask(await qaApi.retryAutomationTask(task.id));
    message.value = "失败任务已进入重试等待队列";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function deadLetterTask(task: AutomationTask) {
  if (!canManageSchedules.value || loading.value || busy.value) return;
  if (!window.confirm("将该任务标记为死信？运行中的任务必须由持有租约的 worker 结束。")) return;
  busy.value = true;
  clearFeedback();
  try {
    replaceTask(await qaApi.deadLetterAutomationTask(task.id));
    message.value = "任务已进入死信，等待人工审计";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function createDevice() {
  if (!canManageDevices.value || loading.value || busy.value) return;
  busy.value = true;
  clearFeedback();
  try {
    const agentId = deviceForm.value.agentId.trim();
    const created = await qaApi.createDevice({
      name: deviceForm.value.name.trim(),
      agent_id: agentId,
      kind: deviceForm.value.kind.trim(),
      platform: deviceForm.value.platform.trim(),
      capabilities: capabilities(deviceForm.value.capabilities),
    });
    heartbeatAgents.value[created.id] = agentId;
    replaceDevice(created);
    message.value = "设备已登记；agent_id 仅用于设备身份校验，不是设备租约 Token";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function heartbeatDevice(device: Device) {
  if (!canManageDevices.value || loading.value || busy.value) return;
  const agentId = heartbeatAgents.value[device.id]?.trim();
  if (!agentId) {
    errorMessage.value = "请输入登记设备时使用的 agent_id";
    return;
  }
  busy.value = true;
  clearFeedback();
  try {
    replaceDevice(await qaApi.heartbeatDevice(device.id, agentId));
    message.value = "设备心跳已写入本机数据库";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function createSchedule() {
  if (!canManageSchedules.value || loading.value || busy.value) return;
  busy.value = true;
  clearFeedback();
  try {
    const payload: ScheduleCreate = {
      name: scheduleForm.value.name.trim(),
      task_type: scheduleForm.value.taskType,
      payload: parseObject(scheduleForm.value.payload, "定时任务 Payload"),
      queue: scheduleForm.value.queue.trim(),
      priority: scheduleForm.value.priority,
      max_attempts: scheduleForm.value.maxAttempts,
      cron: scheduleForm.value.cron.trim(),
      timezone: scheduleForm.value.timezone.trim(),
      misfire_policy: scheduleForm.value.misfirePolicy,
      overlap_policy: scheduleForm.value.overlapPolicy,
      misfire_grace_seconds: scheduleForm.value.graceSeconds,
      catch_up_limit: scheduleForm.value.catchUpLimit,
      enabled: scheduleForm.value.enabled,
    };
    const created = await qaApi.createSchedule(payload);
    schedules.value.push(created);
    schedules.value.sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
    selectedScheduleId.value = created.id;
    fires.value = [];
    message.value = "定时计划已创建；当前教学版由显式 Tick 驱动，不伪装成常驻分布式调度器";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function runNow(schedule: AutomationSchedule) {
  if (!canManageSchedules.value || loading.value || busy.value) return;
  firesRequestVersion += 1;
  busy.value = true;
  clearFeedback();
  try {
    const fire = await qaApi.runScheduleNow(schedule.id);
    selectedScheduleId.value = schedule.id;
    fires.value.unshift(fire);
    await loadTasks();
    message.value = "手动触发记录和对应任务已在同一数据库事务中创建";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function tickSchedules() {
  if (!canManageSchedules.value || loading.value || busy.value) return;
  busy.value = true;
  clearFeedback();
  try {
    const created = await qaApi.tickSchedules();
    await Promise.all([loadSchedules(), loadTasks()]);
    message.value = created.length
      ? `Tick 完成：生成 ${created.length} 条触发/跳过记录`
      : "Tick 完成：当前没有到期计划";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

async function toggleSchedule(schedule: AutomationSchedule) {
  if (!canManageSchedules.value || loading.value || busy.value) return;
  busy.value = true;
  clearFeedback();
  try {
    const updated = await qaApi.updateSchedule(schedule.id, {
      enabled: !schedule.enabled,
      version: schedule.version,
    });
    const index = schedules.value.findIndex((item) => item.id === updated.id);
    if (index >= 0) schedules.value.splice(index, 1, updated);
    message.value = updated.enabled ? "计划已启用" : "计划已停用";
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    busy.value = false;
  }
}

function switchTab(tab: Tab) {
  activeTab.value = tab;
  clearFeedback();
}

async function selectSchedule(scheduleId: string) {
  if (loading.value || busy.value) return;
  selectedScheduleId.value = scheduleId;
  fires.value = [];
  try {
    await loadFires();
  } catch (error) {
    errorMessage.value = readableError(error);
  }
}

onMounted(() => {
  if (!canReadSchedules.value && canReadDevices.value) activeTab.value = "devices";
  void refresh();
});
</script>

<template>
  <section>
    <PageHeader
      eyebrow="AUTOMATION RUNTIME"
      title="设备、任务队列与定时调度"
      description="用持久化状态机学习 at-least-once 任务、设备心跳和 Cron 调度；当前实现明确限定为本机单进程教学适配器。"
    >
      <template #actions><button class="button" :disabled="loading || busy" @click="refresh">刷新全部</button></template>
    </PageHeader>

    <div class="notice online">
      <b>安全的教学控制面</b>
      <span>页面不会 Claim worker 任务、获取设备租约或保存 lease_token；这些接口只应由受控 worker/agent 调用。</span>
    </div>
    <div v-if="message" class="notice online"><b>完成</b><span>{{ message }}</span></div>
    <div v-if="errorMessage" class="notice error"><b>请求失败</b><span>{{ errorMessage }}</span></div>

    <div class="tabs">
      <button v-if="canReadSchedules" :class="{ active: activeTab === 'tasks' }" @click="switchTab('tasks')">任务队列</button>
      <button v-if="canReadDevices" :class="{ active: activeTab === 'devices' }" @click="switchTab('devices')">设备管理</button>
      <button v-if="canReadSchedules" :class="{ active: activeTab === 'schedules' }" @click="switchTab('schedules')">定时调度</button>
    </div>

    <template v-if="activeTab === 'tasks' && canReadSchedules">
      <div class="stats runtime-stats">
        <article><span>等待执行</span><strong>{{ taskStats.queued }}</strong><small>queued + retry_wait</small></article>
        <article><span>正在执行</span><strong>{{ taskStats.running }}</strong><small>持有短期 worker 租约</small></article>
        <article><span>失败 / 死信</span><strong>{{ taskStats.failed }}</strong><small>需要重试或人工审计</small></article>
        <article><span>已成功</span><strong>{{ taskStats.completed }}</strong><small>持久化结果已确认</small></article>
      </div>
      <article v-if="canManageSchedules" class="panel compact-form task-form">
        <div class="panel-title"><div><small>ENQUEUE</small><h2>入队服务端注册任务</h2></div></div>
        <label><span>任务类型</span><select v-model="taskForm.taskType"><option v-for="item in registeredTaskTypes" :key="item[0]" :value="item[0]">{{ item[1] }} · {{ item[0] }}</option></select></label>
        <label><span>队列</span><input v-model="taskForm.queue" /></label>
        <label><span>优先级</span><input v-model.number="taskForm.priority" type="number" min="0" max="100" /></label>
        <label><span>最大尝试</span><input v-model.number="taskForm.maxAttempts" type="number" min="1" max="100" /></label>
        <label><span>幂等键（可选）</span><input v-model="taskForm.idempotencyKey" placeholder="同键同参数返回原任务" /></label>
        <label class="payload"><span>Payload JSON</span><textarea v-model="taskForm.payload" rows="4" /></label>
        <button class="button primary" :disabled="loading || busy" @click="enqueueTask">持久化入队</button>
      </article>
      <div class="table-wrap task-table">
        <table><thead><tr><th>任务</th><th>状态</th><th>队列 / 优先级</th><th>尝试次数</th><th>租约状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>
          <tr v-for="task in tasks" :key="task.id">
            <td><code>#{{ shortId(task.id) }}</code><b>{{ task.task_type }}</b><small v-if="task.source_schedule_id">来自计划 #{{ shortId(task.source_schedule_id) }}</small></td>
            <td><StatusBadge :status="task.status" :label="taskLabels[task.status] ?? task.status" /><small v-if="task.error_code" class="error-code">{{ task.error_code }}</small></td>
            <td>{{ task.queue }} / {{ task.priority }}</td>
            <td>{{ task.attempts }} / {{ task.max_attempts }}</td>
            <td><span v-if="task.lease_owner">{{ task.lease_owner }}<small>至 {{ dateTime(task.lease_expires_at) }}</small></span><span v-else>无活动租约</span></td>
            <td>{{ dateTime(task.created_at) }}</td>
            <td class="row-actions">
              <button v-if="canManageSchedules && ['queued','retry_wait','running'].includes(task.status)" :disabled="loading || busy" @click="cancelTask(task)">取消</button>
              <button v-if="canManageSchedules && task.status === 'failed'" :disabled="loading || busy || task.attempts >= task.max_attempts" @click="retryTask(task)">重试</button>
              <button v-if="canManageSchedules && ['queued','retry_wait','failed'].includes(task.status)" :disabled="loading || busy" @click="deadLetterTask(task)">死信</button>
            </td>
          </tr>
          <tr v-if="!tasks.length"><td colspan="7" class="empty compact">暂无任务</td></tr>
        </tbody></table>
      </div>
      <article class="worker-note"><b>Worker 学习边界</b><span>完整流程是 claim → 获得一次性 lease_token → heartbeat → complete/fail。Token 只存在于 worker 内存，浏览器控制台不应成为 worker。</span></article>
    </template>

    <template v-if="activeTab === 'devices' && canReadDevices">
      <article v-if="canManageDevices" class="panel compact-form device-form">
        <div class="panel-title"><div><small>REGISTER</small><h2>登记本地教学设备</h2></div></div>
        <label><span>设备名</span><input v-model="deviceForm.name" /></label>
        <label><span>Agent ID</span><input v-model="deviceForm.agentId" autocomplete="off" /></label>
        <label><span>类型</span><input v-model="deviceForm.kind" placeholder="phone / browser / console" /></label>
        <label><span>平台</span><input v-model="deviceForm.platform" placeholder="android / ios / windows" /></label>
        <label class="payload"><span>能力标签（逗号分隔）</span><input v-model="deviceForm.capabilities" /></label>
        <button class="button primary" :disabled="loading || busy" @click="createDevice">登记设备</button>
      </article>
      <div class="device-grid">
        <article v-for="device in devices" :key="device.id" class="panel device-card">
          <header><div><code>#{{ shortId(device.id) }}</code><h2>{{ device.name }}</h2></div><StatusBadge :status="device.status" :label="deviceLabels[device.status] ?? device.status" /></header>
          <dl><div><dt>平台</dt><dd>{{ device.platform }}</dd></div><div><dt>类型</dt><dd>{{ device.kind }}</dd></div><div><dt>上次心跳</dt><dd>{{ dateTime(device.last_heartbeat_at) }}</dd></div></dl>
          <div class="capabilities"><span v-for="capability in device.capabilities" :key="capability">{{ capability }}</span><small v-if="!device.capabilities.length">未声明能力</small></div>
          <div v-if="canManageDevices" class="heartbeat"><input v-model="heartbeatAgents[device.id]" autocomplete="off" placeholder="输入该设备的 agent_id" /><button class="button" :disabled="loading || busy" @click="heartbeatDevice(device)">发送心跳</button></div>
          <p v-if="device.active_lease_id">活动租约：<code>#{{ shortId(device.active_lease_id) }}</code></p>
        </article>
        <p v-if="!devices.length" class="empty panel">暂无登记设备</p>
      </div>
      <article class="worker-note"><b>设备独占租约</b><span>acquire / renew / release 是 agent 职责。控制面只显示设备与心跳，避免把租约 Token 暴露给浏览器或误操作并发设备。</span></article>
    </template>

    <template v-if="activeTab === 'schedules' && canReadSchedules">
      <div class="schedule-layout">
        <article v-if="canManageSchedules" class="panel schedule-form">
          <div class="panel-title"><div><small>CRON</small><h2>创建定时计划</h2></div></div>
          <label><span>计划名称</span><input v-model="scheduleForm.name" /></label>
          <label><span>任务类型</span><select v-model="scheduleForm.taskType"><option v-for="item in registeredTaskTypes" :key="item[0]" :value="item[0]">{{ item[1] }}</option></select></label>
          <div class="two"><label><span>Cron（5 段）</span><input v-model="scheduleForm.cron" /></label><label><span>IANA 时区</span><input v-model="scheduleForm.timezone" /></label></div>
          <div class="two"><label><span>错过策略</span><select v-model="scheduleForm.misfirePolicy"><option value="fire_once">补触发一次</option><option value="catch_up_limited">有限追赶</option><option value="skip">按宽限跳过</option></select></label><label><span>重叠策略</span><select v-model="scheduleForm.overlapPolicy"><option value="forbid">禁止重叠</option><option value="allow">允许重叠</option><option value="replace">替换旧任务</option></select></label></div>
          <div class="two"><label><span>宽限秒数</span><input v-model.number="scheduleForm.graceSeconds" type="number" min="0" max="86400" /></label><label><span>追赶上限</span><input v-model.number="scheduleForm.catchUpLimit" type="number" min="1" max="100" /></label></div>
          <div class="two"><label><span>队列</span><input v-model="scheduleForm.queue" /></label><label><span>优先级 / 最大尝试</span><span class="inline-numbers"><input v-model.number="scheduleForm.priority" type="number" min="0" max="100" /><input v-model.number="scheduleForm.maxAttempts" type="number" min="1" max="100" /></span></label></div>
          <label><span>Payload JSON</span><textarea v-model="scheduleForm.payload" rows="5" /></label>
          <label class="check"><input v-model="scheduleForm.enabled" type="checkbox" />创建后启用</label>
          <button class="button primary" :disabled="loading || busy" @click="createSchedule">创建计划</button>
        </article>
        <article class="panel schedule-list">
          <div class="panel-title"><div><small>SCHEDULES</small><h2>计划与下一次运行</h2></div><button v-if="canManageSchedules" class="button" :disabled="loading || busy" @click="tickSchedules">执行一次 Tick</button></div>
          <button v-for="schedule in schedules" :key="schedule.id" :class="{ active: selectedScheduleId === schedule.id }" :disabled="loading || busy" @click="selectSchedule(schedule.id)">
            <span><b>{{ schedule.name }}</b><small>{{ schedule.cron }} · {{ schedule.timezone }}<br />下次：{{ dateTime(schedule.next_run_at) }}</small></span>
            <StatusBadge :status="schedule.enabled ? 'active' : 'disabled'" :label="schedule.enabled ? '已启用' : '已停用'" />
            <span v-if="canManageSchedules" class="schedule-actions"><i @click.stop="runNow(schedule)">立即运行</i><i @click.stop="toggleSchedule(schedule)">{{ schedule.enabled ? "停用" : "启用" }}</i></span>
          </button>
          <p v-if="!schedules.length" class="empty compact">暂无定时计划</p>
        </article>
      </div>
      <article v-if="selectedSchedule" class="panel fire-panel">
        <div class="panel-title"><div><small>FIRE HISTORY</small><h2>{{ selectedSchedule.name }} · 触发记录</h2></div><button class="button" :disabled="loading || busy" @click="loadFires">刷新记录</button></div>
        <div class="fire-list">
          <div v-for="fire in fires" :key="fire.id"><code>#{{ shortId(fire.id) }}</code><span>{{ dateTime(fire.scheduled_for) }}</span><StatusBadge :status="fire.status" :label="fireLabels[fire.status] ?? fire.status" /><code>任务 {{ fire.task_id ? `#${shortId(fire.task_id)}` : "—" }}</code></div>
          <p v-if="!fires.length" class="empty compact">尚无触发记录</p>
        </div>
      </article>
      <article class="worker-note"><b>调度器边界</b><span>教学版用显式 Tick 展示 misfire、overlap 与幂等触发；生产环境还需 leader election、数据库行锁/消息代理和 outbox。</span></article>
    </template>
  </section>
</template>

<style scoped>
.notice.error{border-color:#efcaca;color:#a33c3c;background:#fff2f2}.tabs{display:flex;gap:5px;margin-bottom:16px;padding:5px;border:1px solid var(--line);border-radius:10px;background:#fff;width:max-content}.tabs button{padding:8px 14px;border:0;border-radius:7px;color:#66746e;background:transparent;font-size:10px;font-weight:700}.tabs button.active{color:#fff;background:var(--green)}button:disabled{cursor:not-allowed;opacity:.5}.runtime-stats{margin-bottom:16px}.compact-form{display:grid;grid-template-columns:1.2fr .65fr .45fr .45fr 1fr auto;align-items:end;gap:11px;margin-bottom:16px}.compact-form .panel-title{grid-column:1/-1}.compact-form label,.schedule-form label{display:grid;gap:6px}.compact-form label>span,.schedule-form label>span{color:#65736d;font-size:9px;font-weight:700}.compact-form input,.compact-form select,.compact-form textarea,.schedule-form input,.schedule-form select,.schedule-form textarea,.heartbeat input{width:100%;min-height:39px;padding:8px 10px;border:1px solid #d9e3de;border-radius:8px;background:#fff;outline:none}.compact-form .payload{grid-column:1/-2}.compact-form textarea,.schedule-form textarea{resize:vertical;font-family:ui-monospace,Consolas,monospace;font-size:9px}.task-table{margin-bottom:14px}.task-table td small{display:block;margin-top:4px;color:var(--muted);font-size:8px}.task-table .error-code{color:#b64848}.row-actions{white-space:nowrap}.row-actions button{margin:2px;padding:5px 7px;border:1px solid var(--line);border-radius:6px;color:#56645e;background:#fff;font-size:8px}.worker-note{display:flex;justify-content:space-between;gap:18px;padding:13px 16px;border:1px dashed #b7d6c8;border-radius:10px;color:#287253;background:#f2faf6;font-size:9px}.worker-note span{max-width:850px;color:#688078;line-height:1.55}.device-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin:16px 0}.device-card header{display:flex;align-items:flex-start;justify-content:space-between}.device-card header code{color:var(--green);font-size:8px}.device-card h2{margin:5px 0}.device-card dl{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:17px 0;padding:14px 0;border-block:1px solid #edf1ef}.device-card dl>div:last-child{grid-column:1/-1}.device-card dt{color:var(--muted);font-size:8px}.device-card dd{margin:4px 0 0;font-size:10px;font-weight:700}.capabilities{display:flex;flex-wrap:wrap;gap:5px;min-height:23px}.capabilities span{padding:5px 7px;border-radius:6px;color:#34795e;background:#eaf7f1;font-size:8px}.capabilities small{color:var(--muted)}.heartbeat{display:flex;gap:7px;margin-top:15px}.heartbeat input{min-width:0;font-size:9px}.device-card>p{margin:12px 0 0;color:var(--muted);font-size:8px}.device-form{grid-template-columns:1fr 1fr .6fr .6fr auto}.device-form .payload{grid-column:1/-2}.schedule-layout{display:grid;grid-template-columns:410px minmax(0,1fr);gap:16px;align-items:start}.schedule-form{display:grid;gap:12px}.schedule-form .two{display:grid;grid-template-columns:1fr 1fr;gap:10px}.inline-numbers{display:grid!important;grid-template-columns:1fr 1fr;gap:5px}.schedule-form .check{display:flex;align-items:center;gap:7px;color:#586760;font-size:9px}.schedule-form .check input{width:auto;min-height:0}.schedule-list{padding:20px 0}.schedule-list>.panel-title{padding:0 20px 10px}.schedule-list>button{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:12px;width:100%;padding:13px 18px;border:0;border-top:1px solid #edf1ef;color:inherit;background:transparent;text-align:left}.schedule-list>button.active{background:#eff8f4;box-shadow:inset 3px 0 #35b77d}.schedule-list button span:first-child b,.schedule-list button span:first-child small{display:block}.schedule-list button span:first-child b{font-size:10px}.schedule-list button span:first-child small{margin-top:4px;color:var(--muted);font-size:8px;line-height:1.55}.schedule-actions{display:flex;gap:5px}.schedule-actions i{padding:5px 6px;border:1px solid var(--line);border-radius:5px;color:#53635c;background:#fff;font-size:8px;font-style:normal}.fire-panel{margin-top:16px}.fire-list>div{display:grid;grid-template-columns:auto 1fr auto auto;align-items:center;gap:15px;padding:12px 0;border-top:1px solid #edf1ef;font-size:9px}.fire-list code{color:#708079;font-size:8px}.fire-panel+.worker-note{margin-top:14px}.empty.compact{padding:28px 12px}
@media(max-width:1100px){.compact-form{grid-template-columns:1fr 1fr 1fr}.compact-form .panel-title,.compact-form .payload{grid-column:1/-1}.device-form{grid-template-columns:1fr 1fr}.device-form .payload{grid-column:1/-1}.device-grid{grid-template-columns:repeat(2,1fr)}.schedule-layout{grid-template-columns:1fr}}
@media(max-width:700px){.runtime-stats,.device-grid{grid-template-columns:1fr}.compact-form,.device-form{grid-template-columns:1fr}.compact-form .panel-title,.compact-form .payload,.device-form .payload{grid-column:auto}.worker-note{flex-direction:column}.schedule-form .two{grid-template-columns:1fr}.fire-list>div{grid-template-columns:auto 1fr}.tabs{width:100%;overflow:auto}.tabs button{min-width:max-content;flex:1}.schedule-list>button{grid-template-columns:1fr auto}.schedule-actions{grid-column:1/-1}}
</style>
