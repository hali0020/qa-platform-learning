<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import {
  ApiError,
  qaApi,
  type CaseResultStatus,
  type Project,
  type TestCase,
  type TestExecution,
  type TestPlan,
  type TestPlanStatus,
} from "@/api";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useAuthSession } from "@/auth/session";

type EditableResultStatus = Exclude<CaseResultStatus, "not_run">;
type ResultDraft = {
  status: EditableResultStatus;
  actual_result: string;
  comment: string;
};

const router = useRouter();
const auth = useAuthSession();
const canWrite = computed(() => auth.can("qa.write"));
const canManageDefects = computed(() => auth.can("defects.manage"));
const projects = ref<Project[]>([]);
const testCases = ref<TestCase[]>([]);
const plans = ref<TestPlan[]>([]);
const executions = ref<TestExecution[]>([]);
const projectFilter = ref("");
const selectedExecutionId = ref("");
const showCreateForm = ref(false);
const loading = ref(true);
const hasLoadedData = ref(false);
const actionKey = ref("");
const errorMessage = ref("");
const successMessage = ref("");
const resultDrafts = reactive<Record<string, ResultDraft>>({});
const resultBaselines = reactive<Record<string, ResultDraft>>({});
const createForm = reactive({
  project_id: "",
  name: "",
  description: "",
  case_ids: [] as string[],
});

const planLabels: Record<TestPlanStatus, string> = {
  draft: "草稿",
  ready: "待执行",
  running: "执行中",
  completed: "已完成",
  cancelled: "已取消",
};
const executionLabels: Record<TestExecution["status"], string> = {
  created: "尚未开始",
  running: "执行中",
  completed: "已完成",
  cancelled: "已取消",
};
const resultLabels: Record<CaseResultStatus, string> = {
  not_run: "未执行",
  passed: "通过",
  failed: "失败",
  blocked: "阻塞",
  skipped: "跳过",
};
const editableResultStatuses: EditableResultStatus[] = ["passed", "failed", "blocked", "skipped"];

const activeProjects = computed(() => projects.value.filter((project) => project.status === "active"));
const filteredPlans = computed(() =>
  projectFilter.value
    ? plans.value.filter((plan) => plan.project_id === projectFilter.value)
    : plans.value,
);
const formCases = computed(() =>
  testCases.value.filter(
    (testCase) => testCase.project_id === createForm.project_id && testCase.status === "active",
  ),
);
const canCreatePlan = computed(() => {
  if (!createForm.case_ids.length) return false;
  const availableCaseIds = new Set(formCases.value.map((testCase) => testCase.id));
  return createForm.case_ids.every((caseId) => availableCaseIds.has(caseId));
});
const selectedExecution = computed(() =>
  executions.value.find((execution) => execution.id === selectedExecutionId.value),
);
const selectedExecutionPlan = computed(() =>
  plans.value.find((plan) => plan.id === selectedExecution.value?.plan_id),
);
const unfinishedResultCount = computed(
  () => selectedExecution.value?.results.filter((result) => result.status === "not_run").length ?? 0,
);
const dirtyResultCaseIds = computed(() =>
  Object.keys(resultDrafts).filter((caseId) => {
    const draft = resultDrafts[caseId];
    const baseline = resultBaselines[caseId];
    return Boolean(
      draft &&
        baseline &&
        (draft.status !== baseline.status ||
          draft.actual_result !== baseline.actual_result ||
          draft.comment !== baseline.comment),
    );
  }),
);
const hasDirtyResults = computed(() => dirtyResultCaseIds.value.length > 0);
const resultCounts = computed(() => {
  const counts: Record<CaseResultStatus, number> = {
    not_run: 0,
    passed: 0,
    failed: 0,
    blocked: 0,
    skipped: 0,
  };
  selectedExecution.value?.results.forEach((result) => {
    counts[result.status] += 1;
  });
  return counts;
});

const projectName = (projectId: string) =>
  projects.value.find((project) => project.id === projectId)?.name ?? "未知项目";
const executionForPlan = (planId: string) =>
  executions.value.find((execution) => execution.plan_id === planId);
const dateLabel = (value: string | null) =>
  value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
const isBusy = (key?: string) =>
  key ? actionKey.value === key : loading.value || Boolean(actionKey.value);

const planStatusForExecution: Partial<Record<TestExecution["status"], TestPlanStatus>> = {
  running: "running",
  completed: "completed",
  cancelled: "cancelled",
};

function readableError(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "操作失败，请稍后重试";
}

function upsertPlan(plan: TestPlan) {
  const index = plans.value.findIndex((item) => item.id === plan.id);
  if (index === -1) plans.value.push(plan);
  else plans.value[index] = plan;
}

function upsertExecution(execution: TestExecution) {
  const index = executions.value.findIndex((item) => item.id === execution.id);
  if (index === -1) executions.value.push(execution);
  else executions.value[index] = execution;
}

function clearResultDrafts() {
  Object.keys(resultDrafts).forEach((key) => delete resultDrafts[key]);
  Object.keys(resultBaselines).forEach((key) => delete resultBaselines[key]);
}

function syncResultDrafts(execution: TestExecution) {
  clearResultDrafts();
  execution.results.forEach((result) => {
    const draft: ResultDraft = {
      status: result.status === "not_run" ? "passed" : result.status,
      actual_result: result.actual_result,
      comment: result.comment,
    };
    resultDrafts[result.case_id] = { ...draft };
    resultBaselines[result.case_id] = { ...draft };
  });
}

function confirmDiscardResultDrafts(action: string): boolean {
  if (!hasDirtyResults.value) return true;
  return window.confirm(
    `还有 ${dirtyResultCaseIds.value.length} 条用例结果未保存。${action}会丢失这些输入，是否继续？`,
  );
}

async function openDefectFromResult(caseId: string): Promise<void> {
  const execution = selectedExecution.value;
  if (!canManageDefects.value || !execution || isBusy()) return;
  if (!confirmDiscardResultDrafts("打开缺陷提单页")) return;
  await router.push({
    path: "/defects",
    query: {
      project_id: execution.project_id,
      execution_id: execution.id,
      case_id: caseId,
    },
  });
}

function replaceSelectedExecution(execution: TestExecution) {
  selectedExecutionId.value = execution.id;
  syncResultDrafts(execution);
}

function selectExecution(execution: TestExecution) {
  if (isBusy() || execution.id === selectedExecutionId.value) return;
  if (!confirmDiscardResultDrafts("切换执行")) return;
  replaceSelectedExecution(execution);
  errorMessage.value = "";
  successMessage.value = "";
}

function collapseExecution() {
  if (isBusy()) return;
  if (!confirmDiscardResultDrafts("收起执行详情")) return;
  selectedExecutionId.value = "";
  clearResultDrafts();
}

function changeCreateProject() {
  createForm.case_ids = [];
}

function openCreateForm() {
  if (!canWrite.value || isBusy() || !activeProjects.value.length) return;
  showCreateForm.value = true;
  if (!createForm.project_id) {
    createForm.project_id = projectFilter.value || activeProjects.value[0]?.id || "";
  }
}

async function runAction(key: string, success: string, action: () => Promise<void>) {
  if (actionKey.value) return;
  actionKey.value = key;
  errorMessage.value = "";
  successMessage.value = "";
  try {
    await action();
    successMessage.value = success;
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    if (actionKey.value === key) actionKey.value = "";
  }
}

async function loadData() {
  if (actionKey.value) return;
  if (!confirmDiscardResultDrafts("刷新页面")) return;
  loading.value = true;
  errorMessage.value = "";
  successMessage.value = "";
  try {
    const [projectItems, caseItems, planItems, executionItems] = await Promise.all([
      qaApi.listProjects(),
      qaApi.listTestCases(),
      qaApi.listTestPlans(),
      qaApi.listExecutions(),
    ]);
    projects.value = projectItems;
    testCases.value = caseItems;
    plans.value = planItems;
    executions.value = executionItems;
    hasLoadedData.value = true;
    if (!activeProjects.value.some((project) => project.id === createForm.project_id)) {
      createForm.project_id = activeProjects.value[0]?.id ?? "";
      createForm.case_ids = [];
    } else {
      const availableCaseIds = new Set(formCases.value.map((testCase) => testCase.id));
      createForm.case_ids = createForm.case_ids.filter((caseId) => availableCaseIds.has(caseId));
    }
    if (selectedExecutionId.value) {
      const current = executions.value.find((item) => item.id === selectedExecutionId.value);
      if (current) syncResultDrafts(current);
      else {
        selectedExecutionId.value = "";
        clearResultDrafts();
      }
    }
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    loading.value = false;
  }
}

async function createPlan() {
  if (!canWrite.value || isBusy()) return;
  const name = createForm.name.trim();
  if (!createForm.project_id) {
    errorMessage.value = "请先选择一个进行中的项目";
    return;
  }
  if (!name) {
    errorMessage.value = "请输入计划名称";
    return;
  }
  if (!canCreatePlan.value) {
    errorMessage.value = "请至少选择一个当前仍处于启用状态的测试用例";
    return;
  }
  await runAction("create-plan", "测试计划已写入本地数据库", async () => {
    const plan = await qaApi.createTestPlan({
      project_id: createForm.project_id,
      name,
      description: createForm.description.trim(),
      case_ids: [...createForm.case_ids],
    });
    upsertPlan(plan);
    projectFilter.value = plan.project_id;
    createForm.name = "";
    createForm.description = "";
    createForm.case_ids = [];
    showCreateForm.value = false;
  });
}

async function transitionPlan(plan: TestPlan, status: "draft" | "ready") {
  if (!canWrite.value || isBusy()) return;
  await runAction(`plan-${plan.id}-${status}`, `计划已切换为${planLabels[status]}`, async () => {
    upsertPlan(await qaApi.transitionTestPlan(plan.id, { status }));
  });
}

async function deleteDraftPlan(plan: TestPlan) {
  if (!canWrite.value || isBusy() || plan.status !== "draft") return;
  if (!window.confirm(`确定删除草稿计划“${plan.name}”吗？此操作无法撤销。`)) return;
  await runAction(`plan-${plan.id}-delete`, "草稿计划已删除", async () => {
    await qaApi.deleteTestPlan(plan.id);
    plans.value = plans.value.filter((item) => item.id !== plan.id);
  });
}

async function createExecution(plan: TestPlan) {
  if (!canWrite.value || isBusy()) return;
  if (!confirmDiscardResultDrafts("创建并打开新的执行")) return;
  await runAction(`execution-create-${plan.id}`, "执行记录已创建，可以开始执行", async () => {
    const execution = await qaApi.createExecution({ plan_id: plan.id });
    upsertExecution(execution);
    replaceSelectedExecution(execution);
  });
}

async function transitionExecution(execution: TestExecution, status: "running" | "completed" | "cancelled") {
  if (!canWrite.value || isBusy()) return;
  if (status !== "running" && !confirmDiscardResultDrafts(status === "completed" ? "完成执行" : "取消执行")) {
    return;
  }
  const messages = {
    running: "执行已开始，计划状态已同步更新",
    completed: "执行已完成，结果已保存",
    cancelled: "执行已取消",
  };
  await runAction(`execution-${execution.id}-${status}`, messages[status], async () => {
    const updated = await qaApi.transitionExecution(execution.id, { status });
    upsertExecution(updated);
    replaceSelectedExecution(updated);
    const plan = plans.value.find((item) => item.id === updated.plan_id);
    const planStatus = planStatusForExecution[updated.status];
    if (plan && planStatus) {
      upsertPlan({ ...plan, status: planStatus, updated_at: updated.updated_at });
    }
  });
}

async function saveResult(execution: TestExecution, caseId: string) {
  if (!canWrite.value || isBusy()) return;
  const draft = resultDrafts[caseId];
  if (!draft) return;
  await runAction(`result-${caseId}`, "用例结果已保存", async () => {
    const updated = await qaApi.updateCaseResult(execution.id, caseId, {
      status: draft.status,
      actual_result: draft.actual_result.trim(),
      comment: draft.comment.trim(),
    });
    upsertExecution(updated);
    const saved = updated.results.find((result) => result.case_id === caseId);
    if (saved) {
      const savedDraft: ResultDraft = {
        status: saved.status === "not_run" ? "passed" : saved.status,
        actual_result: saved.actual_result,
        comment: saved.comment,
      };
      resultDrafts[caseId] = { ...savedDraft };
      resultBaselines[caseId] = { ...savedDraft };
    }
  });
}

onMounted(loadData);
</script>

<template>
  <section>
    <PageHeader
      eyebrow="PLANS & EXECUTIONS"
      title="计划与执行"
      description="从本机 API 加载测试资产：选择用例形成计划，启动执行并逐条保存测试结果。"
    >
      <template #actions>
        <button class="button" :disabled="loading || isBusy()" @click="loadData">刷新</button>
        <button
          v-if="canWrite"
          class="button primary"
          :disabled="!activeProjects.length || isBusy()"
          :title="!activeProjects.length ? '请先创建或恢复一个进行中的项目' : ''"
          @click="openCreateForm"
        >
          ＋ 创建测试计划
        </button>
      </template>
    </PageHeader>

    <div v-if="errorMessage" class="notice notice--error" role="alert">
      <b>操作未完成</b><span>{{ errorMessage }}</span>
    </div>
    <div v-if="successMessage" class="notice online" role="status">
      <b>操作成功</b><span>{{ successMessage }}</span>
    </div>

    <article v-if="canWrite && showCreateForm" class="panel create-panel">
      <div class="section-heading">
        <div><small>NEW PLAN</small><h2>创建测试计划</h2></div>
        <button class="text-button" type="button" :disabled="isBusy()" @click="showCreateForm = false">关闭</button>
      </div>
      <form class="plan-form" @submit.prevent="createPlan">
        <label>
          <span>所属项目</span>
          <select v-model="createForm.project_id" required :disabled="isBusy()" @change="changeCreateProject">
            <option disabled value="">请选择进行中的项目</option>
            <option v-for="project in activeProjects" :key="project.id" :value="project.id">
              {{ project.key }} · {{ project.name }}
            </option>
          </select>
        </label>
        <label>
          <span>计划名称</span>
          <input v-model="createForm.name" required maxlength="150" :disabled="isBusy()" placeholder="例如：登录模块冒烟测试" />
        </label>
        <label class="full-width">
          <span>说明</span>
          <textarea v-model="createForm.description" maxlength="1000" rows="3" :disabled="isBusy()" placeholder="记录本轮目标、范围或版本信息"></textarea>
        </label>
        <fieldset class="full-width case-picker">
          <legend>选择同项目的启用用例（{{ createForm.case_ids.length }}/{{ formCases.length }}）</legend>
          <label v-for="testCase in formCases" :key="testCase.id" class="case-option">
            <input v-model="createForm.case_ids" type="checkbox" :value="testCase.id" :disabled="isBusy()" />
            <span><b>{{ testCase.title }}</b><small>{{ testCase.priority }} · {{ testCase.case_type === "manual" ? "手工" : "自动化" }}</small></span>
          </label>
          <p v-if="!createForm.project_id" class="inline-empty">请先选择项目</p>
          <p v-else-if="!formCases.length" class="inline-empty">该项目还没有启用的测试用例，请先到“测试用例”页面创建并启用用例。</p>
        </fieldset>
        <div class="form-actions full-width">
          <button class="button" type="button" :disabled="isBusy()" @click="showCreateForm = false">取消</button>
          <button
            class="button primary"
            type="submit"
            :disabled="!canCreatePlan || isBusy()"
            :title="!canCreatePlan ? '请至少选择一个当前仍处于启用状态的测试用例' : ''"
          >
            {{ isBusy("create-plan") ? "正在保存…" : "保存为草稿" }}
          </button>
        </div>
      </form>
    </article>

    <div class="toolbar">
      <label>
        <span>按项目筛选</span>
        <select v-model="projectFilter">
          <option value="">全部项目</option>
          <option v-for="project in projects" :key="project.id" :value="project.id">
            {{ project.key }} · {{ project.name }}
          </option>
        </select>
      </label>
      <small>计划 {{ filteredPlans.length }} 个 · 执行 {{ executions.length }} 次</small>
    </div>

    <div v-if="!loading && projects.length && !activeProjects.length" class="notice notice--warning">
      <b>没有进行中的项目</b><span>可以查看已有计划，但新建计划前需要先到“项目管理”恢复或创建一个进行中的项目。</span>
    </div>

    <div v-if="loading && !hasLoadedData" class="state-card" aria-live="polite">
      <span class="spinner"></span><b>正在从本机数据库加载计划与执行…</b>
    </div>
    <div v-else-if="!hasLoadedData" class="state-card">
      <b>计划与执行加载失败</b><span>请确认本机后端已经启动；当前状态不代表数据库为空。</span>
      <button class="button primary" @click="loadData">重新加载</button>
    </div>
    <div v-else-if="!projects.length" class="state-card">
      <b>还没有项目</b><span>请先在项目页面创建一个进行中的项目，再添加测试用例和计划。</span>
    </div>
    <div v-else-if="!filteredPlans.length" class="state-card">
      <b>{{ !activeProjects.length ? "当前没有可创建计划的项目" : projectFilter ? "该项目还没有测试计划" : "还没有测试计划" }}</b>
      <span v-if="activeProjects.length">点击“创建测试计划”，选择当前项目中的启用用例。</span>
      <span v-else>请先到“项目管理”恢复或创建一个进行中的项目。</span>
    </div>
    <div v-else class="plan-list">
      <article v-for="plan in filteredPlans" :key="plan.id" class="plan plan-card">
        <div class="plan-copy">
          <div><code>{{ plan.id.slice(0, 8) }}</code><h2>{{ plan.name }}</h2></div>
          <small>{{ projectName(plan.project_id) }} · {{ plan.description || "暂无说明" }}</small>
        </div>
        <StatusBadge :status="plan.status" :label="planLabels[plan.status]" />
        <dl>
          <div><dt>包含用例</dt><dd>{{ plan.case_ids.length }}</dd></div>
          <div><dt>最近更新</dt><dd>{{ dateLabel(plan.updated_at) }}</dd></div>
        </dl>
        <div class="plan-actions">
          <button
            v-if="canWrite && plan.status === 'draft'"
            class="button primary"
            :disabled="!plan.case_ids.length || isBusy()"
            :title="!plan.case_ids.length ? '空计划不能进入待执行状态' : ''"
            @click="transitionPlan(plan, 'ready')"
          >
            {{ isBusy(`plan-${plan.id}-ready`) ? "提交中…" : "提交待执行" }}
          </button>
          <button
            v-if="canWrite && plan.status === 'draft'"
            class="button danger-button"
            :disabled="isBusy()"
            @click="deleteDraftPlan(plan)"
          >
            {{ isBusy(`plan-${plan.id}-delete`) ? "删除中…" : "删除草稿" }}
          </button>
          <button
            v-if="canWrite && plan.status === 'ready' && !executionForPlan(plan.id)"
            class="button primary"
            :disabled="isBusy()"
            @click="createExecution(plan)"
          >
            {{ isBusy(`execution-create-${plan.id}`) ? "创建中…" : "创建执行" }}
          </button>
          <button
            v-if="canWrite && plan.status === 'ready' && !executionForPlan(plan.id)"
            class="button"
            :disabled="isBusy()"
            @click="transitionPlan(plan, 'draft')"
          >
            {{ isBusy(`plan-${plan.id}-draft`) ? "退回中…" : "退回草稿" }}
          </button>
          <button
            v-if="executionForPlan(plan.id)"
            class="button"
            :disabled="isBusy()"
            @click="selectExecution(executionForPlan(plan.id)!)"
          >
            查看执行
          </button>
        </div>
      </article>
    </div>

    <article v-if="selectedExecution" class="panel execution-panel">
      <div class="execution-header">
        <div>
          <small>EXECUTION {{ selectedExecution.id.slice(0, 8) }}</small>
          <h2>{{ selectedExecutionPlan?.name ?? "测试执行" }}</h2>
          <p>{{ projectName(selectedExecution.project_id) }} · 创建于 {{ dateLabel(selectedExecution.created_at) }}</p>
        </div>
        <div class="execution-actions">
          <StatusBadge :status="selectedExecution.status" :label="executionLabels[selectedExecution.status]" />
          <button
            v-if="canWrite && selectedExecution.status === 'created'"
            class="button primary"
            :disabled="isBusy()"
            @click="transitionExecution(selectedExecution, 'running')"
          >{{ isBusy(`execution-${selectedExecution.id}-running`) ? "启动中…" : "开始执行" }}</button>
          <button
            v-if="canWrite && selectedExecution.status === 'running'"
            class="button primary"
            :disabled="unfinishedResultCount > 0 || isBusy()"
            :title="unfinishedResultCount ? `还有 ${unfinishedResultCount} 条用例未执行` : ''"
            @click="transitionExecution(selectedExecution, 'completed')"
          >{{ isBusy(`execution-${selectedExecution.id}-completed`) ? "完成中…" : "完成执行" }}</button>
          <button
            v-if="canWrite && (selectedExecution.status === 'created' || selectedExecution.status === 'running')"
            class="button danger-button"
            :disabled="isBusy()"
            @click="transitionExecution(selectedExecution, 'cancelled')"
          >{{ isBusy(`execution-${selectedExecution.id}-cancelled`) ? "取消中…" : "取消执行" }}</button>
          <button class="text-button" :disabled="isBusy()" @click="collapseExecution">收起</button>
        </div>
      </div>

      <div class="result-stats">
        <span>未执行 <b>{{ resultCounts.not_run }}</b></span>
        <span>通过 <b>{{ resultCounts.passed }}</b></span>
        <span>失败 <b>{{ resultCounts.failed }}</b></span>
        <span>阻塞 <b>{{ resultCounts.blocked }}</b></span>
        <span>跳过 <b>{{ resultCounts.skipped }}</b></span>
      </div>

      <p v-if="selectedExecution.status === 'created'" class="execution-tip">点击“开始执行”后才能填写用例结果。</p>
      <p v-else-if="selectedExecution.status === 'running' && unfinishedResultCount" class="execution-tip">
        还有 {{ unfinishedResultCount }} 条用例未记录结果；全部处理后才能完成执行。
      </p>
      <p v-else-if="selectedExecution.status === 'completed'" class="execution-tip success-tip">
        本次执行已完成，以下结果为只读记录。
      </p>

      <div v-if="!selectedExecution.results.length" class="inline-empty">该执行没有关联用例。</div>
      <div v-else class="result-list">
        <section v-for="result in selectedExecution.results" :key="result.case_id" class="result-item">
          <header>
            <div><code>{{ result.case_id.slice(0, 8) }}</code><b>{{ result.case_title }}</b></div>
            <div class="result-actions">
              <button
                v-if="canManageDefects && (result.status === 'failed' || result.status === 'blocked')"
                class="text-button"
                type="button"
                :disabled="isBusy()"
                @click="openDefectFromResult(result.case_id)"
              >提交缺陷</button>
              <StatusBadge :status="result.status" :label="resultLabels[result.status]" />
            </div>
          </header>
          <div v-if="canWrite && selectedExecution.status === 'running' && resultDrafts[result.case_id]" class="result-form">
            <label>
              <span>本次结果</span>
              <select v-model="resultDrafts[result.case_id]!.status" :disabled="isBusy()">
                <option v-for="status in editableResultStatuses" :key="status" :value="status">
                  {{ resultLabels[status] }}
                </option>
              </select>
            </label>
            <label>
              <span>实际结果</span>
              <textarea v-model="resultDrafts[result.case_id]!.actual_result" maxlength="2000" rows="2" :disabled="isBusy()" placeholder="记录实际观察到的结果"></textarea>
            </label>
            <label>
              <span>备注</span>
              <textarea v-model="resultDrafts[result.case_id]!.comment" maxlength="1000" rows="2" :disabled="isBusy()" placeholder="可填写 Bug 编号或补充说明"></textarea>
            </label>
            <button
              class="button primary"
              :disabled="isBusy()"
              @click="saveResult(selectedExecution, result.case_id)"
            >
              {{ isBusy(`result-${result.case_id}`) ? "保存中…" : "保存结果" }}
            </button>
          </div>
          <dl v-else class="result-readonly">
            <div><dt>实际结果</dt><dd>{{ result.actual_result || "—" }}</dd></div>
            <div><dt>备注</dt><dd>{{ result.comment || "—" }}</dd></div>
            <div><dt>执行时间</dt><dd>{{ dateLabel(result.executed_at) }}</dd></div>
          </dl>
        </section>
      </div>
    </article>
  </section>
</template>

<style scoped>
.notice--error { border-color:#efc9c9; color:#a33d3d; background:#fff2f2; }
.notice--warning { border-color:#ead6a7; color:#7c5c1c; background:#fff9eb; }
.create-panel { margin-bottom:18px; }
.section-heading,.execution-header,.toolbar { display:flex; align-items:center; justify-content:space-between; gap:18px; }
.section-heading small,.execution-header>div>small { color:var(--green); font-size:8px; font-weight:700; letter-spacing:.15em; }
.section-heading h2,.execution-header h2 { margin:5px 0 0; }
.text-button { padding:5px; border:0; color:var(--green); background:transparent; font-size:10px; font-weight:700; }
.plan-form { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; margin-top:18px; }
.plan-form label>span,.result-form label>span,.toolbar label>span { display:block; margin-bottom:6px; color:#687771; font-size:9px; font-weight:700; }
.plan-form input:not([type="checkbox"]),.plan-form select,.plan-form textarea,.result-form select,.result-form textarea,.toolbar select { width:100%; padding:10px 11px; border:1px solid #d8e2dd; border-radius:8px; color:#26362f; outline:none; background:#fff; font:inherit; font-size:11px; }
.plan-form textarea,.result-form textarea { resize:vertical; line-height:1.5; }
.plan-form input:focus,.plan-form select:focus,.plan-form textarea:focus,.result-form select:focus,.result-form textarea:focus,.toolbar select:focus { border-color:#6aba97; box-shadow:0 0 0 3px rgba(32,134,94,.1); }
.full-width { grid-column:1/-1; }
.case-picker { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:0; padding:13px; border:1px solid #dfe7e3; border-radius:9px; }
.case-picker legend { padding:0 5px; color:#687771; font-size:9px; font-weight:700; }
.case-option { display:flex; align-items:flex-start; gap:9px; padding:10px; border:1px solid #e3eae6; border-radius:8px; background:#f9fbfa; cursor:pointer; }
.case-option input { margin-top:2px; accent-color:var(--green); }
.case-option b,.case-option small { display:block; }
.case-option b { font-size:10px; }
.case-option small { margin-top:4px; color:var(--muted); font-size:8px; }
.form-actions { display:flex; justify-content:flex-end; gap:8px; }
.toolbar { margin:0 0 15px; padding:11px 14px; border:1px solid var(--line); border-radius:10px; background:#fff; }
.toolbar label { display:flex; align-items:center; gap:10px; }
.toolbar label>span { margin:0; white-space:nowrap; }
.toolbar select { min-width:230px; padding:7px 9px; }
.toolbar>small { color:var(--muted); font-size:9px; }
.state-card { min-height:150px; display:grid; place-content:center; justify-items:center; gap:9px; padding:35px; border:1px dashed #cfdcd6; border-radius:12px; color:var(--muted); background:#fff; text-align:center; }
.state-card b { color:#34463f; font-size:12px; }.state-card span { font-size:10px; }
.spinner { width:24px; height:24px; border:3px solid #deebe5; border-top-color:var(--green); border-radius:50%; animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.plan-card { grid-template-columns:minmax(240px,1fr) auto auto minmax(190px,auto); }
.plan-copy>div { display:flex; align-items:center; gap:8px; }.plan-copy h2 { margin:0; }
.plan-actions,.execution-actions { display:flex; align-items:center; justify-content:flex-end; gap:7px; flex-wrap:wrap; }
.button:disabled { cursor:not-allowed; opacity:.5; }
.danger-button { border-color:#e7c5c5; color:#a84343; background:#fff7f7; }
.execution-panel { margin-top:22px; }
.execution-header { padding-bottom:16px; border-bottom:1px solid #eaf0ed; }
.execution-header p { margin:7px 0 0; color:var(--muted); font-size:9px; }
.result-stats { display:grid; grid-template-columns:repeat(5,1fr); gap:8px; margin:16px 0; }
.result-stats span { padding:10px; border-radius:8px; color:var(--muted); background:#f5f8f6; font-size:9px; text-align:center; }
.result-stats b { display:block; margin-top:4px; color:#24342d; font-size:15px; }
.execution-tip { margin:0 0 14px; padding:10px 12px; border-radius:8px; color:#825f23; background:#fff6e7; font-size:9px; }
.success-tip { color:#247452; background:#edf9f3; }
.result-list { display:grid; gap:10px; }
.result-item { padding:14px; border:1px solid #e0e8e4; border-radius:10px; background:#fcfdfc; }
.result-item>header { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.result-item>header code,.result-item>header b { display:block; }
.result-item>header code { margin-bottom:4px; color:#788980; font-size:8px; }
.result-item>header b { font-size:11px; }
.result-actions { display:flex; align-items:center; gap:10px; }
.result-form { display:grid; grid-template-columns:140px minmax(180px,1fr) minmax(180px,1fr) auto; align-items:end; gap:9px; margin-top:12px; padding-top:12px; border-top:1px solid #e8eeeb; }
.result-readonly { display:grid; grid-template-columns:1fr 1fr auto; gap:15px; margin:12px 0 0; padding-top:12px; border-top:1px solid #e8eeeb; }
.result-readonly dt { color:#87938e; font-size:8px; }.result-readonly dd { margin:5px 0 0; color:#3e4c46; font-size:9px; white-space:pre-wrap; }
.inline-empty { grid-column:1/-1; margin:0; padding:18px; color:var(--muted); font-size:9px; text-align:center; }
@media(max-width:1100px) {
  .plan-card { grid-template-columns:1fr auto; }.plan-card dl,.plan-actions { grid-column:1/-1; }.plan-actions { justify-content:flex-start; }
  .result-form { grid-template-columns:1fr 1fr; }.result-form>label:first-child,.result-form>.button { grid-column:auto; }
}
@media(max-width:700px) {
  .plan-form,.case-picker,.result-form,.result-readonly { grid-template-columns:1fr; }
  .section-heading,.execution-header,.toolbar { align-items:flex-start; flex-direction:column; }
  .toolbar label,.toolbar select { width:100%; }.execution-actions { justify-content:flex-start; }
  .result-stats { grid-template-columns:repeat(2,1fr); }
}
</style>
