<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  qaApi,
  type Defect,
  type Project,
  type TestCase,
  type TestExecution,
  type TestPlan,
} from "@/api";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";

const health = ref<"checking" | "online" | "offline">("checking");
const loading = ref(true);
const healthError = ref("");
const dataError = ref("");
const hasLoadedData = ref(false);
const projects = ref<Project[]>([]);
const testCases = ref<TestCase[]>([]);
const plans = ref<TestPlan[]>([]);
const executions = ref<TestExecution[]>([]);
const defects = ref<Defect[]>([]);
const activePipelineCount = ref(0);

const projectNames = computed(
  () => new Map(projects.value.map((project) => [project.id, project.name])),
);
const planNames = computed(() => new Map(plans.value.map((plan) => [plan.id, plan.name])));
const activeProjects = computed(
  () => projects.value.filter((project) => project.status === "active").length,
);
const activeTestCases = computed(() =>
  testCases.value.filter((item) => item.status === "active"),
);
const automationCoverage = computed(() => {
  if (!activeTestCases.value.length) return 0;
  const automated = activeTestCases.value.filter(
    (item) => item.case_type === "automated",
  ).length;
  return Math.round((automated / activeTestCases.value.length) * 100);
});
const allResults = computed(() =>
  executions.value.flatMap((execution) => execution.results),
);
const passRateResults = computed(() =>
  allResults.value.filter((result) => result.status === "passed" || result.status === "failed"),
);
const passRate = computed(() => {
  if (!passRateResults.value.length) return 0;
  const passed = passRateResults.value.filter((result) => result.status === "passed").length;
  return Math.round((passed / passRateResults.value.length) * 1000) / 10;
});
const pendingFailures = computed(
  () => allResults.value.filter((result) => ["failed", "blocked"].includes(result.status)).length,
);
const openDefects = computed(() =>
  defects.value.filter((defect) => defect.status !== "closed"),
);
const severeDefects = computed(() =>
  openDefects.value.filter((defect) => ["blocker", "critical"].includes(defect.severity)),
);
const activeTasks = computed(() =>
  executions.value
    .filter((execution) => execution.status === "created" || execution.status === "running")
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
    .slice(0, 5)
    .map((execution) => {
      const completed = execution.results.filter((result) => result.status !== "not_run").length;
      const progress = execution.results.length
        ? Math.round((completed / execution.results.length) * 100)
        : 0;
      return {
        id: execution.id,
        name: planNames.value.get(execution.plan_id) ?? `执行 ${execution.id.slice(0, 8)}`,
        project: projectNames.value.get(execution.project_id) ?? "未知项目",
        progress,
        status: execution.status,
      };
    }),
);

const formatError = (error: unknown) =>
  error instanceof Error ? error.message : "无法读取本机业务数据";

async function loadDashboard() {
  loading.value = true;
  health.value = "checking";
  healthError.value = "";
  dataError.value = "";

  try {
    await qaApi.getHealth();
    health.value = "online";
  } catch (error) {
    health.value = "offline";
    healthError.value = formatError(error);
  }

  try {
    const [projectData, caseData, planData, executionData, defectData, pipelineData] = await Promise.all([
      qaApi.listProjects(),
      qaApi.listTestCases(),
      qaApi.listTestPlans(),
      qaApi.listExecutions(),
      qaApi.listDefects(),
      qaApi.listPipelineRuns(),
    ]);
    projects.value = projectData;
    testCases.value = caseData;
    plans.value = planData;
    executions.value = executionData;
    defects.value = defectData;
    activePipelineCount.value = pipelineData.filter(
      (run) => run.status === "queued" || run.status === "running",
    ).length;
    hasLoadedData.value = true;
  } catch (error) {
    dataError.value = formatError(error);
  } finally {
    loading.value = false;
  }
}

onMounted(loadDashboard);
</script>

<template>
  <section>
    <PageHeader
      eyebrow="QUALITY OVERVIEW"
      title="质量仪表盘"
      description="从本机 SQLite 实时汇总项目、用例、执行结果、缺陷与流水线状态。"
    >
      <template #actions>
        <button class="button" :disabled="loading" @click="loadDashboard">刷新</button>
        <RouterLink class="button primary" to="/test-plans">开始测试执行</RouterLink>
      </template>
    </PageHeader>
    <div class="notice" :class="health">
      <b>
        {{ health === "online" ? "本地后端已连接" : health === "checking" ? "正在检查本地后端" : "本地后端未连接" }}
      </b>
      <span>{{ health === "offline" ? healthError : "数据来自 /api/v1 与本机 SQLite" }}</span>
    </div>
    <div v-if="dataError" class="notice error">
      <b>业务数据加载失败</b>
      <span>{{ dataError }}{{ hasLoadedData ? "；当前保留上次成功加载的数据" : "" }}</span>
    </div>

    <div v-if="hasLoadedData" class="stats">
      <article><span>活跃项目</span><strong>{{ activeProjects }}</strong><small>共 {{ projects.length }} 个项目</small></article>
      <article><span>测试用例</span><strong>{{ testCases.length }}</strong><small>{{ activeTestCases.length }} 条活跃 · 自动化覆盖 {{ automationCoverage }}%</small></article>
      <article><span>执行通过率</span><strong>{{ passRate }}%</strong><small>Passed + Failed 共 {{ passRateResults.length }} 条</small></article>
      <article><span>未关闭缺陷</span><strong>{{ openDefects.length }}</strong><small>{{ severeDefects.length }} 个高严重度 · {{ pendingFailures }} 条失败/阻塞 · {{ activePipelineCount }} 条流水线运行中</small></article>
    </div>
    <p v-if="loading && !hasLoadedData" class="empty">正在汇总本机业务数据…</p>
    <p v-else-if="!hasLoadedData" class="empty error-state">业务数据尚未成功加载，请刷新重试。</p>
    <article v-else class="panel">
      <div class="panel-title">
        <div><small>ACTIVE RUNS</small><h2>进行中的质量任务</h2></div>
        <RouterLink to="/test-plans">查看全部 →</RouterLink>
      </div>
      <p v-if="loading" class="empty compact">正在汇总本机数据…</p>
      <div v-for="task in loading ? [] : activeTasks" :key="task.id" class="task-row">
        <div><strong>{{ task.name }}</strong><small>{{ task.project }}</small></div>
        <div class="progress"><i :style="{ width: `${task.progress}%` }"></i></div>
        <span>{{ task.progress }}%</span>
        <StatusBadge :status="task.status" :label="task.status === 'running' ? '执行中' : '待开始'" />
      </div>
      <p v-if="!loading && !dataError && !activeTasks.length" class="empty compact">暂无进行中的测试执行</p>
    </article>
    <article class="flow"><b>需求</b><i>→</i><b>用例</b><i>→</i><b>执行</b><i>→</i><b>缺陷</b><i>→</i><b>流水线</b><i>→</i><b>报告</b></article>
  </section>
</template>

<style scoped>
.empty.compact { padding:28px 12px; }
.notice.error { border-color:#efcaca; color:#a33c3c; background:#fff2f2; }
.error-state { color:#a33c3c; }
button:disabled { cursor:not-allowed; opacity:.55; }
</style>
