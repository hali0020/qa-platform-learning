<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { onBeforeRouteLeave, useRoute } from "vue-router";
import {
  qaApi,
  type AuditEvent,
  type Defect,
  type DefectPriority,
  type DefectSeverity,
  type DefectStatus,
  type Project,
  type TestCase,
  type TestExecution,
} from "@/api";
import AuditTimeline from "@/components/AuditTimeline.vue";
import CollaborationPanel from "@/components/CollaborationPanel.vue";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useAuthSession } from "@/auth/session";

const auth = useAuthSession();
const canManageDefects = computed(() => auth.can("defects.manage"));

interface DefectEditor {
  project_id: string;
  title: string;
  description: string;
  severity: DefectSeverity;
  priority: DefectPriority;
  reporter: string;
  assignee: string;
  environment: string;
  reproduction_steps: string[];
  expected_result: string;
  actual_result: string;
  execution_id: string;
  case_id: string;
}

const route = useRoute();
const projects = ref<Project[]>([]);
const testCases = ref<TestCase[]>([]);
const executions = ref<TestExecution[]>([]);
const defects = ref<Defect[]>([]);
const auditEvents = ref<AuditEvent[]>([]);

const loading = ref(true);
const loadFailed = ref(false);
const hasLoadedData = ref(false);
const writeBusyKey = ref("");
const errorMessage = ref("");
const successMessage = ref("");
const auditLoading = ref(false);
const auditError = ref("");
let auditRequestVersion = 0;

const query = ref("");
const selectedProjectId = ref("");
const selectedStatus = ref<DefectStatus | "">("");
const selectedSeverity = ref<DefectSeverity | "">("");
const selectedAssignee = ref("");
const selectedDefectId = ref("");

const editorOpen = ref(false);
const editingId = ref<string | null>(null);
const editorBaseline = ref("");
const editor = reactive<DefectEditor>({
  project_id: "",
  title: "",
  description: "",
  severity: "major",
  priority: "P2",
  reporter: "local-user",
  assignee: "",
  environment: "本地测试环境",
  reproduction_steps: [""],
  expected_result: "",
  actual_result: "",
  execution_id: "",
  case_id: "",
});

const transitionOpen = ref(false);
const transitionTarget = ref<DefectStatus | "">("");
const transitionResolution = ref("");
const transitionComment = ref("");
const transitionBaseline = ref("");
const routeContextApplied = ref(false);

const severities: DefectSeverity[] = ["blocker", "critical", "major", "minor", "trivial"];
const priorities: DefectPriority[] = ["P0", "P1", "P2", "P3"];
const statuses: DefectStatus[] = [
  "open",
  "in_progress",
  "resolved",
  "verified",
  "closed",
  "reopened",
];

const statusLabels: Record<DefectStatus, string> = {
  open: "待处理",
  in_progress: "处理中",
  resolved: "已解决",
  verified: "已验证",
  closed: "已关闭",
  reopened: "重新打开",
};

const severityLabels: Record<DefectSeverity, string> = {
  blocker: "阻断",
  critical: "严重",
  major: "主要",
  minor: "次要",
  trivial: "轻微",
};

const transitionMap: Record<DefectStatus, DefectStatus[]> = {
  open: ["in_progress", "resolved"],
  in_progress: ["open", "resolved"],
  resolved: ["verified", "reopened"],
  verified: ["closed", "reopened"],
  closed: ["reopened"],
  reopened: ["in_progress", "resolved"],
};

const writeInProgress = computed(() => Boolean(writeBusyKey.value));
const mutationLocked = computed(() => loading.value || writeInProgress.value);
const activeProjects = computed(() => projects.value.filter((item) => item.status === "active"));
const projectNames = computed(() => new Map(projects.value.map((item) => [item.id, item.name])));
const caseNames = computed(() => new Map(testCases.value.map((item) => [item.id, item.title])));
const selectedDefect = computed(
  () => defects.value.find((item) => item.id === selectedDefectId.value) ?? null,
);
const availableTransitions = computed(() =>
  selectedDefect.value ? transitionMap[selectedDefect.value.status] : [],
);
const transitionTargetLabel = computed(() =>
  transitionTarget.value ? statusLabels[transitionTarget.value] : "",
);
const editorProjectCases = computed(() =>
  testCases.value.filter((item) => item.project_id === editor.project_id),
);
const editorProjectExecutions = computed(() =>
  executions.value.filter(
    (item) => item.project_id === editor.project_id && item.status !== "created",
  ),
);
const editorCaseOptions = computed(() => {
  const execution = editorProjectExecutions.value.find(
    (item) => item.id === editor.execution_id,
  );
  if (!execution) return editorProjectCases.value;
  const includedCaseIds = new Set(execution.results.map((result) => result.case_id));
  return editorProjectCases.value.filter((item) => includedCaseIds.has(item.id));
});
const editorExecutionOptions = computed(() => {
  if (!editor.case_id) return editorProjectExecutions.value;
  return editorProjectExecutions.value.filter((execution) =>
    execution.results.some((result) => result.case_id === editor.case_id),
  );
});
const assignees = computed(() =>
  [...new Set(defects.value.map((item) => item.assignee.trim()).filter(Boolean))].sort(),
);

const visibleDefects = computed(() => {
  const keyword = query.value.trim().toLowerCase();
  return defects.value
    .filter((item) => {
      if (selectedProjectId.value && item.project_id !== selectedProjectId.value) return false;
      if (selectedStatus.value && item.status !== selectedStatus.value) return false;
      if (selectedSeverity.value && item.severity !== selectedSeverity.value) return false;
      if (selectedAssignee.value && item.assignee !== selectedAssignee.value) return false;
      if (!keyword) return true;
      return [
        item.id,
        item.title,
        item.description,
        item.reporter,
        item.assignee,
        item.environment,
        projectNames.value.get(item.project_id) ?? "",
      ]
        .join(" ")
        .toLowerCase()
        .includes(keyword);
    })
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
});

const openDefectCount = computed(
  () => defects.value.filter((item) => item.status !== "closed").length,
);
const severeDefectCount = computed(
  () =>
    defects.value.filter(
      (item) => item.status !== "closed" && ["blocker", "critical"].includes(item.severity),
    ).length,
);
const verificationCount = computed(
  () => defects.value.filter((item) => item.status === "resolved").length,
);
const closedDefectCount = computed(
  () => defects.value.filter((item) => item.status === "closed").length,
);

function editorSignature(): string {
  return JSON.stringify({ ...editor, reproduction_steps: [...editor.reproduction_steps] });
}

function currentTransitionSignature(): string {
  return JSON.stringify({
    status: transitionTarget.value,
    resolution: transitionResolution.value,
    comment: transitionComment.value,
  });
}

const editorDirty = computed(
  () => editorOpen.value && editorSignature() !== editorBaseline.value,
);
const transitionDirty = computed(
  () => transitionOpen.value && currentTransitionSignature() !== transitionBaseline.value,
);
const hasDirtyWork = computed(() => editorDirty.value || transitionDirty.value);

function readableError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败，请稍后重试";
}

function dateLabel(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function resetMessages(): void {
  errorMessage.value = "";
  successMessage.value = "";
}

function showSuccess(message: string): void {
  successMessage.value = message;
  errorMessage.value = "";
}

function routeValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value) && typeof value[0] === "string") return value[0];
  return "";
}

function confirmDiscard(): boolean {
  return !hasDirtyWork.value || window.confirm("当前有尚未保存的内容，确定放弃这些修改吗？");
}

function forceCloseEditor(): void {
  editorOpen.value = false;
  editingId.value = null;
  editorBaseline.value = "";
}

function forceCloseTransition(): void {
  transitionOpen.value = false;
  transitionTarget.value = "";
  transitionResolution.value = "";
  transitionComment.value = "";
  transitionBaseline.value = "";
}

function discardOpenForms(): void {
  forceCloseEditor();
  forceCloseTransition();
}

function resetEditor(projectId = ""): void {
  Object.assign(editor, {
    project_id: projectId,
    title: "",
    description: "",
    severity: "major" as DefectSeverity,
    priority: "P2" as DefectPriority,
    reporter: auth.state.user?.display_name ?? auth.state.user?.username ?? "local-user",
    assignee: "",
    environment: "本地测试环境",
    reproduction_steps: [""],
    expected_result: "",
    actual_result: "",
    execution_id: "",
    case_id: "",
  });
}

function replaceDefect(defect: Defect): void {
  const index = defects.value.findIndex((item) => item.id === defect.id);
  if (index >= 0) defects.value[index] = defect;
  else defects.value.push(defect);
}

async function loadAudit(defectId: string): Promise<void> {
  const requestVersion = ++auditRequestVersion;
  auditLoading.value = true;
  auditError.value = "";
  try {
    const events = await qaApi.listAuditEvents({ entity_type: "defect", entity_id: defectId });
    if (requestVersion === auditRequestVersion && selectedDefectId.value === defectId) {
      auditEvents.value = events;
    }
  } catch (error) {
    if (requestVersion === auditRequestVersion && selectedDefectId.value === defectId) {
      auditEvents.value = [];
      auditError.value = readableError(error);
    }
  } finally {
    if (requestVersion === auditRequestVersion) auditLoading.value = false;
  }
}

function applyRouteContext(): void {
  if (routeContextApplied.value) return;
  const projectId = routeValue(route.query.project_id);
  const caseId = routeValue(route.query.case_id);
  const executionId = routeValue(route.query.execution_id);
  if (!projectId && !caseId && !executionId) return;

  routeContextApplied.value = true;
  if (!canManageDefects.value) return;
  const linkedCase = testCases.value.find((item) => item.id === caseId);
  const linkedExecution = executions.value.find((item) => item.id === executionId);
  const resolvedProjectId = projectId || linkedCase?.project_id || linkedExecution?.project_id || "";
  const project = activeProjects.value.find((item) => item.id === resolvedProjectId);
  if (!project) {
    errorMessage.value = "关联记录所属项目不存在或已归档，无法直接创建缺陷";
    return;
  }

  openCreate(project.id, true);
  if (!editorOpen.value) return;
  if (linkedCase?.project_id === project.id) {
    editor.case_id = linkedCase.id;
    editor.title = `${linkedCase.title}：`;
  }
  if (linkedExecution?.project_id === project.id) {
    editor.execution_id = linkedExecution.id;
    const result = linkedExecution.results.find((item) => item.case_id === linkedCase?.id);
    if (result) {
      editor.actual_result = result.actual_result;
      editor.description = result.comment;
    }
  }
}

async function loadData(): Promise<void> {
  if (writeInProgress.value) return;
  loading.value = true;
  if (!hasLoadedData.value) loadFailed.value = false;
  errorMessage.value = "";
  try {
    const [loadedProjects, loadedCases, loadedExecutions, loadedDefects] = await Promise.all([
      qaApi.listProjects(),
      qaApi.listTestCases(),
      qaApi.listExecutions(),
      qaApi.listDefects(),
    ]);
    projects.value = loadedProjects;
    testCases.value = loadedCases;
    executions.value = loadedExecutions;
    defects.value = loadedDefects;
    hasLoadedData.value = true;
    loadFailed.value = false;

    if (selectedDefectId.value) {
      const stillExists = loadedDefects.some((item) => item.id === selectedDefectId.value);
      if (stillExists) void loadAudit(selectedDefectId.value);
      else {
        selectedDefectId.value = "";
        auditEvents.value = [];
        ++auditRequestVersion;
      }
    }
    applyRouteContext();
  } catch (error) {
    errorMessage.value = readableError(error);
    if (!hasLoadedData.value) loadFailed.value = true;
  } finally {
    loading.value = false;
  }
}

async function refreshData(): Promise<void> {
  if (!confirmDiscard()) return;
  discardOpenForms();
  await loadData();
}

function openCreate(preferredProjectId = "", allowWhileLoading = false): void {
  if (
    !canManageDefects.value ||
    writeInProgress.value ||
    (loading.value && !allowWhileLoading)
  ) return;
  if (hasDirtyWork.value && !confirmDiscard()) return;
  const preferred = activeProjects.value.find((item) => item.id === preferredProjectId);
  const filtered = activeProjects.value.find((item) => item.id === selectedProjectId.value);
  const project = preferred ?? filtered ?? activeProjects.value[0];
  if (!project) {
    errorMessage.value = "请先创建一个进行中的项目，再提交缺陷";
    return;
  }
  resetMessages();
  forceCloseTransition();
  resetEditor(project.id);
  editingId.value = null;
  editorOpen.value = true;
  editorBaseline.value = editorSignature();
}

function openEdit(defect: Defect): void {
  if (!canManageDefects.value || mutationLocked.value) return;
  if (hasDirtyWork.value && !confirmDiscard()) return;
  resetMessages();
  forceCloseTransition();
  editingId.value = defect.id;
  Object.assign(editor, {
    project_id: defect.project_id,
    title: defect.title,
    description: defect.description,
    severity: defect.severity,
    priority: defect.priority,
    reporter: defect.reporter,
    assignee: defect.assignee,
    environment: defect.environment,
    reproduction_steps: defect.reproduction_steps.length ? [...defect.reproduction_steps] : [""],
    expected_result: defect.expected_result,
    actual_result: defect.actual_result,
    execution_id: defect.execution_id ?? "",
    case_id: defect.case_id ?? "",
  });
  editorOpen.value = true;
  editorBaseline.value = editorSignature();
}

function closeEditor(): void {
  if (writeInProgress.value || !confirmDiscard()) return;
  forceCloseEditor();
}

function clearIncompatibleLinks(): void {
  if (!editorProjectCases.value.some((item) => item.id === editor.case_id)) editor.case_id = "";
  if (!editorProjectExecutions.value.some((item) => item.id === editor.execution_id)) {
    editor.execution_id = "";
  }
}

function caseAssociationChanged(): void {
  if (!editorExecutionOptions.value.some((item) => item.id === editor.execution_id)) {
    editor.execution_id = "";
  }
}

function executionAssociationChanged(): void {
  if (!editorCaseOptions.value.some((item) => item.id === editor.case_id)) {
    editor.case_id = "";
  }
}

function addReproductionStep(): void {
  if (!mutationLocked.value && editor.reproduction_steps.length < 30) {
    editor.reproduction_steps.push("");
  }
}

function removeReproductionStep(index: number): void {
  if (mutationLocked.value || editor.reproduction_steps.length === 1) return;
  editor.reproduction_steps.splice(index, 1);
}

async function saveDefect(): Promise<void> {
  if (!canManageDefects.value || mutationLocked.value) return;
  resetMessages();
  const title = editor.title.trim();
  const reporter = editor.reporter.trim();
  const steps = editor.reproduction_steps.map((step) => step.trim());
  if (!editor.project_id || !title || !reporter) {
    errorMessage.value = "所属项目、缺陷标题和报告人不能为空";
    return;
  }
  if (!steps.length || steps.some((step) => !step)) {
    errorMessage.value = "请完整填写每一条复现步骤";
    return;
  }
  if (editor.case_id && !editorCaseOptions.value.some((item) => item.id === editor.case_id)) {
    errorMessage.value = "关联用例不属于当前项目或所选执行";
    return;
  }
  if (
    editor.execution_id &&
    !editorExecutionOptions.value.some((item) => item.id === editor.execution_id)
  ) {
    errorMessage.value = "关联执行尚未开始、不属于当前项目，或不包含所选用例";
    return;
  }

  const values = {
    title,
    description: editor.description.trim(),
    severity: editor.severity,
    priority: editor.priority,
    assignee: editor.assignee.trim(),
    environment: editor.environment.trim(),
    reproduction_steps: steps,
    expected_result: editor.expected_result.trim(),
    actual_result: editor.actual_result.trim(),
  };

  const isEditing = Boolean(editingId.value);
  writeBusyKey.value = editingId.value ? `defect-${editingId.value}-update` : "defect-create";
  try {
    const saved = editingId.value
      ? await qaApi.updateDefect(editingId.value, values)
      : await qaApi.createDefect({
          project_id: editor.project_id,
          reporter,
          execution_id: editor.execution_id || null,
          case_id: editor.case_id || null,
          ...values,
        });
    replaceDefect(saved);
    selectedDefectId.value = saved.id;
    forceCloseEditor();
    showSuccess(isEditing ? `缺陷“${saved.title}”已更新` : `缺陷“${saved.title}”已提交`);
    await loadAudit(saved.id);
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    writeBusyKey.value = "";
  }
}

function selectDefect(defect: Defect): void {
  if (writeInProgress.value) return;
  if (defect.id === selectedDefectId.value) return;
  if (!confirmDiscard()) return;
  discardOpenForms();
  selectedDefectId.value = defect.id;
  auditEvents.value = [];
  void loadAudit(defect.id);
}

function openTransition(status: DefectStatus): void {
  if (!canManageDefects.value || mutationLocked.value || !selectedDefect.value) return;
  if (status === "in_progress" && !selectedDefect.value.assignee.trim()) {
    errorMessage.value = "缺陷进入处理中状态前，请先编辑并指定负责人";
    return;
  }
  if (hasDirtyWork.value && !confirmDiscard()) return;
  forceCloseEditor();
  transitionTarget.value = status;
  transitionResolution.value = status === "resolved" ? selectedDefect.value.resolution ?? "" : "";
  transitionComment.value = "";
  transitionOpen.value = true;
  transitionBaseline.value = currentTransitionSignature();
}

function closeTransition(): void {
  if (writeInProgress.value || !confirmDiscard()) return;
  forceCloseTransition();
}

async function submitTransition(): Promise<void> {
  const defect = selectedDefect.value;
  if (!canManageDefects.value || !defect || !transitionTarget.value || mutationLocked.value) return;
  const resolution = transitionResolution.value.trim();
  const comment = transitionComment.value.trim();
  if (transitionTarget.value === "resolved" && !resolution) {
    errorMessage.value = "进入已解决状态前，请填写解决说明";
    return;
  }
  if (transitionTarget.value === "reopened" && !comment) {
    errorMessage.value = "重新打开缺陷时，请说明验证失败或重开原因";
    return;
  }

  resetMessages();
  writeBusyKey.value = `defect-${defect.id}-transition`;
  try {
    const updated = await qaApi.transitionDefect(defect.id, {
      status: transitionTarget.value,
      resolution: resolution || undefined,
      comment: comment || undefined,
    });
    replaceDefect(updated);
    forceCloseTransition();
    showSuccess(`缺陷状态已切换为“${statusLabels[updated.status]}”`);
    await loadAudit(updated.id);
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    writeBusyKey.value = "";
  }
}

function retryAudit(): void {
  if (selectedDefectId.value) void loadAudit(selectedDefectId.value);
}

function handleBeforeUnload(event: BeforeUnloadEvent): void {
  if (!hasDirtyWork.value) return;
  event.preventDefault();
  event.returnValue = "";
}

onMounted(() => {
  window.addEventListener("beforeunload", handleBeforeUnload);
  void loadData();
});

onBeforeUnmount(() => {
  ++auditRequestVersion;
  window.removeEventListener("beforeunload", handleBeforeUnload);
});

onBeforeRouteLeave(() => confirmDiscard());
</script>

<template>
  <section>
    <PageHeader
      eyebrow="DEFECTS & AUDIT"
      title="缺陷与审计"
      description="用结构化 Bug 单沉淀复现证据，并通过只读审计历史追踪每一次修改与状态流转。"
    >
      <template #actions>
        <button class="button" :disabled="mutationLocked" @click="refreshData">刷新</button>
        <button
          v-if="canManageDefects"
          class="button primary"
          :disabled="mutationLocked || !activeProjects.length"
          @click="openCreate()"
        >
          ＋ 提交缺陷
        </button>
      </template>
    </PageHeader>

    <div class="notice online">
      <b>本机缺陷闭环</b>
      <span>缺陷、关联用例、执行记录与审计事件只写入本机 SQLite，不连接公司平台。</span>
    </div>
    <div v-if="errorMessage" class="notice notice--error" role="alert">
      <b>操作未完成</b><span>{{ errorMessage }}</span>
    </div>
    <div v-if="successMessage" class="notice notice--success" role="status">
      <b>操作成功</b><span>{{ successMessage }}</span>
    </div>

    <form v-if="canManageDefects && editorOpen" class="panel defect-editor" @submit.prevent="saveDefect">
      <div class="section-heading">
        <div>
          <small>{{ editingId ? "EDIT DEFECT" : "NEW DEFECT" }}</small>
          <h2>{{ editingId ? "编辑缺陷信息" : "提交 Bug 单" }}</h2>
        </div>
        <button
          class="icon-button"
          type="button"
          :disabled="mutationLocked"
          aria-label="关闭缺陷编辑器"
          @click="closeEditor"
        >×</button>
      </div>

      <div class="form-grid">
        <label>
          <span>所属项目</span>
          <select
            v-model="editor.project_id"
            required
            :disabled="Boolean(editingId) || mutationLocked"
            @change="clearIncompatibleLinks"
          >
            <option v-for="project in activeProjects" :key="project.id" :value="project.id">
              {{ project.key }} · {{ project.name }}
            </option>
          </select>
        </label>
        <label class="wide">
          <span>缺陷标题</span>
          <input
            v-model="editor.title"
            maxlength="200"
            required
            :disabled="mutationLocked"
            placeholder="什么条件下，哪个功能出现了什么问题"
          />
        </label>
        <label>
          <span>严重程度</span>
          <select v-model="editor.severity" :disabled="mutationLocked">
            <option v-for="severity in severities" :key="severity" :value="severity">
              {{ severityLabels[severity] }} · {{ severity }}
            </option>
          </select>
        </label>
        <label>
          <span>处理优先级</span>
          <select v-model="editor.priority" :disabled="mutationLocked">
            <option v-for="priority in priorities" :key="priority" :value="priority">{{ priority }}</option>
          </select>
        </label>
        <label>
          <span>报告人</span>
          <input v-model="editor.reporter" maxlength="100" required disabled title="报告人由当前登录用户确定" />
        </label>
        <label>
          <span>负责人</span>
          <input v-model="editor.assignee" maxlength="100" :disabled="mutationLocked" placeholder="可暂时留空" />
        </label>
        <label class="wide">
          <span>环境 / 版本 / 设备</span>
          <input
            v-model="editor.environment"
            maxlength="200"
            :disabled="mutationLocked"
            placeholder="例如：本地测试服 · Windows 11 · build 1024"
          />
        </label>
        <label>
          <span>关联用例</span>
          <select
            v-model="editor.case_id"
            :disabled="mutationLocked || Boolean(editingId)"
            @change="caseAssociationChanged"
          >
            <option value="">不关联</option>
            <option v-for="item in editorCaseOptions" :key="item.id" :value="item.id">
              {{ item.title }}
            </option>
          </select>
        </label>
        <label>
          <span>关联执行</span>
          <select
            v-model="editor.execution_id"
            :disabled="mutationLocked || Boolean(editingId)"
            @change="executionAssociationChanged"
          >
            <option value="">不关联</option>
            <option v-for="item in editorExecutionOptions" :key="item.id" :value="item.id">
              {{ item.id.slice(0, 8) }} · {{ item.status }}
            </option>
          </select>
        </label>
        <label class="wide">
          <span>问题描述 / 影响范围</span>
          <textarea v-model="editor.description" maxlength="2000" rows="3" :disabled="mutationLocked"></textarea>
        </label>
      </div>

      <div class="steps-heading">
        <div><b>复现步骤</b><small>{{ editor.reproduction_steps.length }} / 30</small></div>
        <button
          class="button"
          type="button"
          :disabled="mutationLocked || editor.reproduction_steps.length >= 30"
          @click="addReproductionStep"
        >＋ 添加步骤</button>
      </div>
      <div class="reproduction-steps">
        <div v-for="(_, index) in editor.reproduction_steps" :key="index" class="step-row">
          <strong>{{ index + 1 }}</strong>
          <input
            v-model="editor.reproduction_steps[index]"
            maxlength="500"
            required
            :disabled="mutationLocked"
            :placeholder="`第 ${index + 1} 步操作`"
          />
          <button
            class="icon-button danger-text"
            type="button"
            :disabled="mutationLocked || editor.reproduction_steps.length === 1"
            aria-label="删除复现步骤"
            @click="removeReproductionStep(index)"
          >×</button>
        </div>
      </div>

      <div class="form-grid result-fields">
        <label>
          <span>预期结果</span>
          <textarea v-model="editor.expected_result" maxlength="2000" rows="3" :disabled="mutationLocked"></textarea>
        </label>
        <label>
          <span>实际结果</span>
          <textarea v-model="editor.actual_result" maxlength="2000" rows="3" :disabled="mutationLocked"></textarea>
        </label>
      </div>
      <div class="form-actions">
        <small v-if="editorDirty">有尚未保存的修改</small>
        <button class="button" type="button" :disabled="mutationLocked" @click="closeEditor">取消</button>
        <button class="button primary" type="submit" :disabled="mutationLocked">
          {{ writeInProgress ? "保存中…" : editingId ? "保存修改" : "提交缺陷" }}
        </button>
      </div>
    </form>

    <div v-if="hasLoadedData" class="stats defect-stats">
      <article><span>未关闭缺陷</span><strong>{{ openDefectCount }}</strong><small>包含待处理、处理中和待验证</small></article>
      <article><span>高严重度</span><strong>{{ severeDefectCount }}</strong><small>未关闭的 Blocker + Critical</small></article>
      <article><span>等待验证</span><strong>{{ verificationCount }}</strong><small>开发已提交解决说明</small></article>
      <article><span>已关闭</span><strong>{{ closedDefectCount }}</strong><small>完成验证的历史缺陷</small></article>
    </div>

    <div class="toolbar">
      <input v-model="query" class="search" type="search" placeholder="搜索缺陷编号、标题、人员、环境或项目" />
      <select v-model="selectedProjectId">
        <option value="">全部项目</option>
        <option v-for="project in projects" :key="project.id" :value="project.id">{{ project.key }} · {{ project.name }}</option>
      </select>
      <select v-model="selectedStatus">
        <option value="">全部状态</option>
        <option v-for="status in statuses" :key="status" :value="status">{{ statusLabels[status] }}</option>
      </select>
      <select v-model="selectedSeverity">
        <option value="">全部严重程度</option>
        <option v-for="severity in severities" :key="severity" :value="severity">{{ severityLabels[severity] }}</option>
      </select>
      <select v-model="selectedAssignee">
        <option value="">全部负责人</option>
        <option v-for="assignee in assignees" :key="assignee" :value="assignee">{{ assignee }}</option>
      </select>
    </div>

    <div v-if="loading && !hasLoadedData" class="panel state-panel" role="status">
      <span class="spinner" aria-hidden="true"></span><b>正在加载本机缺陷数据…</b>
    </div>
    <div v-else-if="loadFailed && !hasLoadedData" class="panel state-panel" role="alert">
      <b>缺陷数据首次加载失败</b>
      <span>请确认本机后端已启动；失败状态不代表数据库为空。</span>
      <button class="button primary" @click="loadData">重新加载</button>
    </div>

    <div v-else class="defect-layout">
      <article class="panel defect-list">
        <div class="panel-title">
          <div><small>DEFECT LIST</small><h2>缺陷列表 · {{ visibleDefects.length }}</h2></div>
        </div>
        <p v-if="loading" class="inline-state">正在刷新，当前保留上次成功数据…</p>
        <p v-if="!visibleDefects.length" class="inline-state">
          {{ defects.length ? "没有符合筛选条件的缺陷。" : "本机数据库中还没有缺陷。" }}
        </p>
        <button
          v-for="defect in visibleDefects"
          :key="defect.id"
          type="button"
          class="defect-row"
          :class="{ active: defect.id === selectedDefectId }"
          :disabled="writeInProgress"
          @click="selectDefect(defect)"
        >
          <div class="defect-row__head">
            <code>{{ defect.id.slice(0, 8) }}</code>
            <StatusBadge :status="defect.status" :label="statusLabels[defect.status]" />
          </div>
          <b>{{ defect.title }}</b>
          <small>{{ projectNames.get(defect.project_id) ?? "未知项目" }} · {{ defect.assignee || "未分配" }}</small>
          <div class="defect-row__meta">
            <StatusBadge :status="defect.severity" :label="severityLabels[defect.severity]" />
            <span>{{ defect.priority }}</span><time>{{ dateLabel(defect.updated_at) }}</time>
          </div>
        </button>
      </article>

      <article class="panel defect-detail">
        <div v-if="!selectedDefect" class="detail-empty">
          <b>选择一条缺陷查看详情</b>
          <span>这里会展示业务字段、状态操作和不可编辑的审计历史。</span>
        </div>
        <template v-else>
          <header class="detail-heading">
            <div>
              <small>DEFECT {{ selectedDefect.id.slice(0, 8) }}</small>
              <h2>{{ selectedDefect.title }}</h2>
              <p>{{ projectNames.get(selectedDefect.project_id) ?? "未知项目" }} · 更新于 {{ dateLabel(selectedDefect.updated_at) }}</p>
            </div>
            <div class="detail-actions">
              <StatusBadge :status="selectedDefect.status" :label="statusLabels[selectedDefect.status]" />
              <button v-if="canManageDefects && selectedDefect.status !== 'closed'" class="button" :disabled="mutationLocked" @click="openEdit(selectedDefect)">编辑信息</button>
            </div>
          </header>

          <div v-if="canManageDefects" class="transition-actions">
            <span>下一步状态</span>
            <button
              v-for="status in availableTransitions"
              :key="status"
              class="button"
              :disabled="mutationLocked"
              @click="openTransition(status)"
            >{{ statusLabels[status] }}</button>
            <small v-if="!availableTransitions.length">当前没有可执行的状态动作</small>
          </div>

          <form v-if="canManageDefects && transitionOpen" class="transition-form" @submit.prevent="submitTransition">
            <b>切换为“{{ transitionTargetLabel }}”</b>
            <label v-if="transitionTarget === 'resolved'">
              <span>解决说明</span>
              <textarea v-model="transitionResolution" maxlength="2000" rows="2" :disabled="mutationLocked" required></textarea>
            </label>
            <label>
              <span>{{ transitionTarget === "reopened" ? "重开原因" : "流转备注" }}</span>
              <textarea
                v-model="transitionComment"
                maxlength="1000"
                rows="2"
                :required="transitionTarget === 'reopened'"
                :disabled="mutationLocked"
              ></textarea>
            </label>
            <div><button class="button" type="button" :disabled="mutationLocked" @click="closeTransition">取消</button><button class="button primary" type="submit" :disabled="mutationLocked">确认流转</button></div>
          </form>

          <dl class="detail-grid">
            <div><dt>严重程度</dt><dd>{{ severityLabels[selectedDefect.severity] }} · {{ selectedDefect.severity }}</dd></div>
            <div><dt>优先级</dt><dd>{{ selectedDefect.priority }}</dd></div>
            <div><dt>报告人</dt><dd>{{ selectedDefect.reporter }}</dd></div>
            <div><dt>负责人</dt><dd>{{ selectedDefect.assignee || "未分配" }}</dd></div>
            <div class="wide"><dt>环境 / 版本 / 设备</dt><dd>{{ selectedDefect.environment || "—" }}</dd></div>
            <div class="wide"><dt>问题描述</dt><dd>{{ selectedDefect.description || "—" }}</dd></div>
            <div class="wide"><dt>复现步骤</dt><dd><ol><li v-for="step in selectedDefect.reproduction_steps" :key="step">{{ step }}</li></ol></dd></div>
            <div><dt>关联用例</dt><dd>{{ selectedDefect.case_id ? caseNames.get(selectedDefect.case_id) ?? selectedDefect.case_id.slice(0, 8) : "—" }}</dd></div>
            <div><dt>关联执行</dt><dd>{{ selectedDefect.execution_id ? selectedDefect.execution_id.slice(0, 8) : "—" }}</dd></div>
            <div class="wide"><dt>预期结果</dt><dd>{{ selectedDefect.expected_result || "—" }}</dd></div>
            <div class="wide"><dt>实际结果</dt><dd>{{ selectedDefect.actual_result || "—" }}</dd></div>
            <div v-if="selectedDefect.resolution" class="wide"><dt>解决说明</dt><dd>{{ selectedDefect.resolution }}</dd></div>
          </dl>

          <section class="audit-section">
            <div class="section-heading"><div><small>AUDIT TRAIL</small><h2>审计历史</h2></div></div>
            <AuditTimeline
              :events="auditEvents"
              :loading="auditLoading"
              :error="auditError"
              @retry="retryAudit"
            />
          </section>
          <CollaborationPanel
            :project-id="selectedDefect.project_id"
            :entity-id="selectedDefect.id"
          />
        </template>
      </article>
    </div>
  </section>
</template>

<style scoped>
.notice--error { border-color:#efcaca; color:#a43f3f; background:#fff2f2; }
.notice--success { border-color:#cde6da; color:#236f50; background:#eff9f4; }
.defect-editor { margin-bottom:18px; border-color:#cde6da; }
.section-heading,.detail-heading,.detail-actions,.steps-heading,.form-actions,.transition-actions { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.section-heading small,.detail-heading>div>small { color:var(--green); font-size:8px; font-weight:700; letter-spacing:.15em; }
.section-heading h2,.detail-heading h2 { margin:5px 0 0; }
.icon-button { width:32px; height:32px; border:0; border-radius:8px; color:#687770; background:#f0f4f2; font-size:18px; }
.form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:13px; margin-top:17px; }
.form-grid label,.transition-form label { display:grid; gap:6px; }
.form-grid label>span,.transition-form label>span { color:#5f6f68; font-size:9px; font-weight:700; }
.form-grid input,.form-grid select,.form-grid textarea,.transition-form textarea,.toolbar select,.reproduction-steps input { width:100%; padding:10px 11px; border:1px solid #d8e2dd; border-radius:8px; outline:none; color:#273730; background:#fff; font:inherit; font-size:10px; }
.form-grid input:focus,.form-grid select:focus,.form-grid textarea:focus,.transition-form textarea:focus,.toolbar select:focus,.reproduction-steps input:focus { border-color:#6aba97; box-shadow:0 0 0 3px rgba(32,134,94,.1); }
.form-grid textarea,.transition-form textarea { resize:vertical; line-height:1.55; }
.wide { grid-column:1/-1; }
.steps-heading { margin:18px 0 9px; }
.steps-heading b,.steps-heading small { display:block; }.steps-heading b { font-size:11px; }.steps-heading small { margin-top:4px; color:var(--muted); font-size:8px; }
.reproduction-steps { display:grid; gap:7px; }
.step-row { display:grid; grid-template-columns:24px minmax(0,1fr) 32px; align-items:center; gap:8px; }
.step-row>strong { display:grid; width:23px; height:23px; place-items:center; border-radius:50%; color:#347a5d; background:#e8f5ef; font-size:8px; }
.danger-text { color:#ad4848; }
.result-fields { padding-top:15px; border-top:1px solid #edf1ef; }
.form-actions { justify-content:flex-end; margin-top:16px; padding-top:14px; border-top:1px solid #edf1ef; }
.form-actions small { margin-right:auto; color:#9a6b20; font-size:8px; }
.button:disabled,.icon-button:disabled { cursor:not-allowed; opacity:.5; }
.defect-stats { margin-top:2px; }
.toolbar { display:grid; grid-template-columns:minmax(220px,1fr) repeat(4,minmax(115px,auto)); gap:8px; margin-bottom:16px; }
.toolbar .search { width:100%; margin:0; }
.toolbar select { padding:0 9px; }
.state-panel { display:grid; min-height:190px; place-content:center; justify-items:center; gap:9px; color:var(--muted); text-align:center; }
.state-panel b { color:#405149; font-size:12px; }.state-panel span { font-size:9px; }
.spinner { width:23px; height:23px; border:3px solid #dce9e3; border-top-color:var(--green); border-radius:50%; animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.defect-layout { display:grid; grid-template-columns:minmax(280px,.72fr) minmax(440px,1.28fr); gap:15px; align-items:start; }
.defect-list { padding:18px 0; overflow:hidden; }
.defect-list .panel-title { padding:0 18px 10px; }
.inline-state { margin:0; padding:24px 16px; color:var(--muted); font-size:9px; text-align:center; }
.defect-row { display:grid; width:100%; gap:8px; padding:13px 17px; border:0; border-top:1px solid #edf1ef; color:inherit; background:#fff; text-align:left; }
.defect-row:hover,.defect-row.active { background:#f0f8f4; }.defect-row.active { box-shadow:inset 3px 0 var(--green); }
.defect-row__head,.defect-row__meta { display:flex; align-items:center; gap:8px; }
.defect-row__head { justify-content:space-between; }.defect-row code { color:#71847b; font-size:8px; }.defect-row>b { font-size:10px; line-height:1.45; }.defect-row>small { color:var(--muted); font-size:8px; }
.defect-row__meta span,.defect-row__meta time { color:var(--muted); font-size:8px; }.defect-row__meta time { margin-left:auto; }
.defect-detail { min-height:300px; }
.detail-empty { display:grid; min-height:260px; place-content:center; justify-items:center; gap:8px; color:var(--muted); text-align:center; }.detail-empty b { color:#3c4e46; font-size:12px; }.detail-empty span { font-size:9px; }
.detail-heading { align-items:flex-start; padding-bottom:15px; border-bottom:1px solid #e8efeb; }.detail-heading p { margin:7px 0 0; color:var(--muted); font-size:8px; }.detail-actions { justify-content:flex-end; }
.transition-actions { justify-content:flex-start; flex-wrap:wrap; margin:15px 0; padding:11px; border-radius:9px; background:#f5f8f6; }.transition-actions>span { margin-right:auto; color:#63736c; font-size:9px; font-weight:700; }.transition-actions>small { color:var(--muted); font-size:8px; }
.transition-form { display:grid; gap:10px; margin-bottom:15px; padding:13px; border:1px solid #d8e7df; border-radius:9px; background:#f8fcfa; }.transition-form>b { font-size:10px; }.transition-form>div { display:flex; justify-content:flex-end; gap:7px; }
.detail-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px; margin:0; }.detail-grid>div { padding:11px; border:1px solid #e5ebe8; border-radius:8px; background:#fcfdfc; }.detail-grid dt { color:#87938e; font-size:8px; }.detail-grid dd { margin:6px 0 0; color:#35463e; font-size:9px; line-height:1.65; overflow-wrap:anywhere; white-space:pre-wrap; }.detail-grid ol { margin:0; padding-left:17px; }
.audit-section { margin-top:18px; padding-top:17px; border-top:1px solid #e8efeb; }.audit-section .section-heading { margin-bottom:12px; }
@media(max-width:1100px) { .toolbar { grid-template-columns:repeat(2,minmax(0,1fr)); }.toolbar .search { grid-column:1/-1; }.defect-layout { grid-template-columns:1fr; } }
@media(max-width:700px) { .form-grid,.detail-grid,.toolbar { grid-template-columns:1fr; }.wide,.toolbar .search { grid-column:auto; }.section-heading,.detail-heading,.detail-actions,.form-actions { align-items:flex-start; flex-direction:column; }.detail-actions { justify-content:flex-start; }.form-actions small { margin:0; }.defect-stats { grid-template-columns:1fr; } }
</style>
