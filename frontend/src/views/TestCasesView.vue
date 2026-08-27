<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import {
  qaApi,
  type Project,
  type TestCase,
  type TestCaseCreate,
  type TestCasePriority,
  type TestCaseStatus,
  type TestCaseType,
  type TestCaseUpdate,
  type TestStep,
  type TestSuite,
} from "@/api";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import CollaborationPanel from "@/components/CollaborationPanel.vue";
import { useAuthSession } from "@/auth/session";

const auth = useAuthSession();
const canWrite = computed(() => auth.can("qa.write"));

interface CaseEditor {
  project_id: string;
  suite_id: string;
  title: string;
  preconditions: string;
  priority: TestCasePriority;
  case_type: TestCaseType;
  tags: string;
  steps: TestStep[];
}

const projects = ref<Project[]>([]);
const testCases = ref<TestCase[]>([]);
const testSuites = ref<TestSuite[]>([]);
const query = ref("");
const selectedProjectId = ref("");
const selectedSuiteScope = ref("");
const loading = ref(true);
const loadFailed = ref(false);
const submitting = ref(false);
const busyCaseId = ref("");
const errorMessage = ref("");
const successMessage = ref("");
const editorOpen = ref(false);
const editingId = ref<string | null>(null);
const editingOriginalSuiteId = ref<string | null>(null);
const editorBaseline = ref("");
const collaborationCase = ref<TestCase | null>(null);

const emptyStep = (): TestStep => ({ action: "", expected_result: "" });
const editor = reactive<CaseEditor>({
  project_id: "",
  suite_id: "",
  title: "",
  preconditions: "",
  priority: "P2",
  case_type: "manual",
  tags: "",
  steps: [emptyStep()],
});

function editorSignature(): string {
  return JSON.stringify({ ...editor, steps: editor.steps.map((step) => ({ ...step })) });
}

const editorDirty = computed(
  () => editorOpen.value && editorSignature() !== editorBaseline.value,
);

const MAX_STEPS = 100;
const MAX_TAGS = 20;
const labels: Record<TestCaseStatus, string> = {
  active: "可执行",
  draft: "草稿",
  disabled: "已停用",
};
const priorities: TestCasePriority[] = ["P0", "P1", "P2", "P3"];
const mutationInProgress = computed(
  () => submitting.value || Boolean(busyCaseId.value),
);
const editorLocked = computed(() => loading.value || mutationInProgress.value);

function normalizedTags(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[,，]/)
        .map((tag) => tag.trim().toLowerCase())
        .filter(Boolean),
    ),
  ];
}

const activeProjects = computed(() => projects.value.filter((item) => item.status === "active"));
const projectNames = computed(() => new Map(projects.value.map((item) => [item.id, item.name])));
const suitesById = computed(() => new Map(testSuites.value.map((item) => [item.id, item])));
const suitePaths = computed(() => {
  const result = new Map<string, string>();
  const resolve = (suite: TestSuite, visited = new Set<string>()): string => {
    const cached = result.get(suite.id);
    if (cached) return cached;
    if (visited.has(suite.id)) return suite.name;
    visited.add(suite.id);
    const parent = suite.parent_id ? suitesById.value.get(suite.parent_id) : undefined;
    const path = parent ? `${resolve(parent, visited)} / ${suite.name}` : suite.name;
    result.set(suite.id, path);
    return path;
  };
  testSuites.value.forEach((suite) => resolve(suite));
  return result;
});
const filterSuites = computed(() =>
  testSuites.value.filter(
    (suite) => !selectedProjectId.value || suite.project_id === selectedProjectId.value,
  ),
);
function suiteCanReceiveCases(suite: TestSuite): boolean {
  const visited = new Set<string>();
  let current: TestSuite | undefined = suite;
  while (current) {
    if (current.status !== "active" || visited.has(current.id)) return false;
    visited.add(current.id);
    if (!current.parent_id) return true;
    current = suitesById.value.get(current.parent_id);
  }
  return false;
}
const editorSuites = computed(() =>
  testSuites.value.filter(
    (suite) =>
      suite.project_id === editor.project_id &&
      (suiteCanReceiveCases(suite) ||
        (Boolean(editingId.value) && suite.id === editingOriginalSuiteId.value)),
  ),
);
const uniqueTagCount = computed(() => normalizedTags(editor.tags).length);
const visibleCases = computed(() => {
  const keyword = query.value.trim().toLowerCase();
  return testCases.value.filter((item) => {
    if (selectedProjectId.value && item.project_id !== selectedProjectId.value) return false;
    if (selectedSuiteScope.value === "unassigned" && item.suite_id !== null) return false;
    if (
      selectedSuiteScope.value &&
      selectedSuiteScope.value !== "unassigned" &&
      item.suite_id !== selectedSuiteScope.value
    ) return false;
    if (!keyword) return true;
    const searchable = [
      item.title,
      item.preconditions,
      item.tags.join(" "),
      projectNames.value.get(item.project_id) ?? "",
      item.suite_id ? suitePaths.value.get(item.suite_id) ?? "" : "未分类",
      ...item.steps.flatMap((step) => [step.action, step.expected_result]),
    ]
      .join(" ")
      .toLowerCase();
    return searchable.includes(keyword);
  });
});

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败，请稍后重试";
}

function resetMessages(): void {
  errorMessage.value = "";
  successMessage.value = "";
}

function showSuccess(message: string): void {
  successMessage.value = message;
  errorMessage.value = "";
}

function replaceCase(updated: TestCase): void {
  const index = testCases.value.findIndex((item) => item.id === updated.id);
  if (index >= 0) testCases.value[index] = updated;
  else testCases.value.push(updated);
}

async function loadData(): Promise<void> {
  if (mutationInProgress.value) return;
  loading.value = true;
  loadFailed.value = false;
  resetMessages();
  try {
    const [loadedProjects, loadedCases, loadedSuites] = await Promise.all([
      qaApi.listProjects(),
      qaApi.listTestCases(),
      qaApi.listTestSuites(),
    ]);
    projects.value = loadedProjects;
    testCases.value = loadedCases;
    testSuites.value = loadedSuites;
  } catch (error) {
    loadFailed.value = true;
    errorMessage.value = describeError(error);
  } finally {
    loading.value = false;
  }
}

function confirmDiscardEditor(): boolean {
  return !editorDirty.value || window.confirm("当前用例有尚未保存的修改，确定放弃吗？");
}

async function refreshData(): Promise<void> {
  if (!confirmDiscardEditor()) return;
  forceCloseEditor();
  await loadData();
}

function resetEditor(projectId = ""): void {
  editingId.value = null;
  editingOriginalSuiteId.value = null;
  Object.assign(editor, {
    project_id: projectId,
    suite_id: "",
    title: "",
    preconditions: "",
    priority: "P2" as TestCasePriority,
    case_type: "manual" as TestCaseType,
    tags: "",
    steps: [emptyStep()],
  });
}

function openCreate(): void {
  if (!canWrite.value) return;
  if (editorLocked.value) return;
  if (!confirmDiscardEditor()) return;
  resetMessages();
  const preferredProject = activeProjects.value.find(
    (item) => item.id === selectedProjectId.value,
  );
  const project = preferredProject ?? activeProjects.value[0];
  if (!project) {
    errorMessage.value = "请先创建一个进行中的项目，再新增测试用例";
    return;
  }
  resetEditor(project.id);
  editorOpen.value = true;
  editorBaseline.value = editorSignature();
}

function openEdit(item: TestCase): void {
  if (!canWrite.value) return;
  if (editorLocked.value) return;
  if (!confirmDiscardEditor()) return;
  resetMessages();
  editingId.value = item.id;
  editingOriginalSuiteId.value = item.suite_id;
  Object.assign(editor, {
    project_id: item.project_id,
    suite_id: item.suite_id ?? "",
    title: item.title,
    preconditions: item.preconditions,
    priority: item.priority,
    case_type: item.case_type,
    tags: item.tags.join(", "),
    steps: item.steps.length ? item.steps.map((step) => ({ ...step })) : [emptyStep()],
  });
  editorOpen.value = true;
  editorBaseline.value = editorSignature();
}

function forceCloseEditor(): void {
  editorOpen.value = false;
  resetEditor();
  editorBaseline.value = "";
}

function closeEditor(): void {
  if (editorLocked.value || !confirmDiscardEditor()) return;
  forceCloseEditor();
}

function clearIncompatibleSuite(): void {
  if (!editorSuites.value.some((suite) => suite.id === editor.suite_id)) {
    editor.suite_id = "";
  }
}

function addStep(): void {
  if (editorLocked.value) return;
  if (editor.steps.length >= MAX_STEPS) {
    resetMessages();
    errorMessage.value = `测试步骤最多 ${MAX_STEPS} 个`;
    return;
  }
  editor.steps.push(emptyStep());
}

function removeStep(index: number): void {
  if (editorLocked.value || editor.steps.length === 1) return;
  editor.steps.splice(index, 1);
}

function formValues(): Omit<TestCaseCreate, "project_id"> | null {
  const title = editor.title.trim();
  if (!title) {
    errorMessage.value = "请填写用例标题";
    return null;
  }

  const steps = editor.steps.map((step) => ({
    action: step.action.trim(),
    expected_result: step.expected_result.trim(),
  }));
  if (!steps.length || steps.some((step) => !step.action || !step.expected_result)) {
    errorMessage.value = "每个测试步骤都要填写操作和预期结果";
    return null;
  }
  if (steps.length > MAX_STEPS) {
    errorMessage.value = `测试步骤最多 ${MAX_STEPS} 个`;
    return null;
  }

  const tags = normalizedTags(editor.tags);
  if (tags.length > MAX_TAGS) {
    errorMessage.value = `标签去重后最多保留 ${MAX_TAGS} 个，当前为 ${tags.length} 个`;
    return null;
  }

  return {
    suite_id: editor.suite_id || null,
    title,
    preconditions: editor.preconditions.trim(),
    priority: editor.priority,
    case_type: editor.case_type,
    tags,
    steps,
  };
}

async function saveCase(): Promise<void> {
  if (!canWrite.value) return;
  if (editorLocked.value) return;
  resetMessages();
  const values = formValues();
  if (!values) return;
  if (!editingId.value && !editor.project_id) {
    errorMessage.value = "请选择所属项目";
    return;
  }

  submitting.value = true;
  try {
    if (editingId.value) {
      const { suite_id: selectedSuiteId, ...editableValues } = values;
      const payload: TestCaseUpdate = editableValues;
      if ((selectedSuiteId ?? null) !== editingOriginalSuiteId.value) {
        payload.suite_id = selectedSuiteId ?? null;
      }
      const updated = await qaApi.updateTestCase(editingId.value, payload);
      replaceCase(updated);
      showSuccess(`用例“${updated.title}”已保存到本机数据库`);
    } else {
      const created = await qaApi.createTestCase({ project_id: editor.project_id, ...values });
      replaceCase(created);
      selectedProjectId.value = created.project_id;
      showSuccess(`用例“${created.title}”已创建为草稿`);
    }
    forceCloseEditor();
  } catch (error) {
    errorMessage.value = describeError(error);
  } finally {
    submitting.value = false;
  }
}

function nextStatus(item: TestCase): TestCaseStatus {
  return item.status === "active" ? "disabled" : "active";
}

function statusActionLabel(item: TestCase): string {
  if (item.status === "draft") return "启用";
  return item.status === "active" ? "停用" : "重新启用";
}

async function transitionCase(item: TestCase): Promise<void> {
  if (!canWrite.value) return;
  if (editorLocked.value) return;
  if (!confirmDiscardEditor()) return;
  if (editorOpen.value) forceCloseEditor();
  resetMessages();
  busyCaseId.value = item.id;
  try {
    const updated = await qaApi.transitionTestCase(item.id, { status: nextStatus(item) });
    replaceCase(updated);
    showSuccess(`用例“${updated.title}”的状态已切换为${labels[updated.status]}`);
  } catch (error) {
    errorMessage.value = describeError(error);
  } finally {
    busyCaseId.value = "";
  }
}

async function deleteCase(item: TestCase): Promise<void> {
  if (!canWrite.value) return;
  if (editorLocked.value) return;
  if (!confirmDiscardEditor()) return;
  const confirmed = window.confirm(
    `确定删除用例“${item.title}”吗？\n\n删除后无法恢复；已被测试计划引用的用例会由后端拒绝删除。`,
  );
  if (!confirmed) return;
  if (editorOpen.value) forceCloseEditor();

  resetMessages();
  busyCaseId.value = item.id;
  try {
    await qaApi.deleteTestCase(item.id);
    testCases.value = testCases.value.filter((candidate) => candidate.id !== item.id);
    if (collaborationCase.value?.id === item.id) collaborationCase.value = null;
    showSuccess(`用例“${item.title}”已删除`);
  } catch (error) {
    errorMessage.value = describeError(error);
  } finally {
    busyCaseId.value = "";
  }
}

function handleBeforeUnload(event: BeforeUnloadEvent): void {
  if (!editorDirty.value) return;
  event.preventDefault();
  event.returnValue = "";
}

onMounted(() => {
  window.addEventListener("beforeunload", handleBeforeUnload);
  void loadData();
});

onBeforeUnmount(() => {
  window.removeEventListener("beforeunload", handleBeforeUnload);
});

onBeforeRouteLeave(() => confirmDiscardEditor());
</script>

<template>
  <section>
    <PageHeader
      eyebrow="TEST CASES"
      title="测试用例"
      description="用结构化前置条件、步骤和预期结果沉淀可复用的测试知识。"
    >
      <template #actions>
        <button class="button" :disabled="editorLocked" @click="refreshData">刷新</button>
        <button v-if="canWrite" class="button primary" :disabled="editorLocked || !activeProjects.length" @click="openCreate">
          ＋ 新建用例
        </button>
      </template>
    </PageHeader>

    <div class="notice online">
      <b>已接入本机 TestCase API</b>
      <span>创建、编辑、状态变更和删除都会真实写入本机 SQLite。</span>
    </div>
    <div v-if="errorMessage" class="notice feedback error" role="alert">
      <b>{{ errorMessage }}</b>
      <button type="button" @click="errorMessage = ''">关闭</button>
    </div>
    <div v-if="successMessage" class="notice online feedback" role="status">
      <b>{{ successMessage }}</b>
      <button type="button" @click="successMessage = ''">关闭</button>
    </div>

    <form v-if="canWrite && editorOpen" class="editor panel" @submit.prevent="saveCase">
      <div class="editor-heading">
        <div>
          <small>{{ editingId ? "EDIT CASE" : "NEW CASE" }}</small>
          <h2>{{ editingId ? "编辑测试用例" : "新建测试用例" }}</h2>
        </div>
        <button
          type="button"
          class="icon-button"
          :disabled="editorLocked"
          aria-label="关闭编辑器"
          @click="closeEditor"
        >
          ×
        </button>
      </div>

      <div class="form-grid">
        <label>
          <span>所属项目</span>
          <select
            v-model="editor.project_id"
            required
            :disabled="Boolean(editingId) || editorLocked"
            @change="clearIncompatibleSuite"
          >
            <option
              v-for="project in projects"
              :key="project.id"
              :value="project.id"
              :disabled="project.status === 'archived'"
            >
              {{ project.key }} · {{ project.name }}{{ project.status === "archived" ? "（已归档）" : "" }}
            </option>
          </select>
        </label>
        <label>
          <span>所属套件</span>
          <select v-model="editor.suite_id" :disabled="editorLocked">
            <option value="">未分类</option>
            <option v-for="suite in editorSuites" :key="suite.id" :value="suite.id">
              {{ suitePaths.get(suite.id) ?? suite.name }}{{ suiteCanReceiveCases(suite) ? "" : "（当前归属，只读）" }}
            </option>
          </select>
        </label>
        <label class="wide">
          <span>用例标题</span>
          <input
            v-model="editor.title"
            :disabled="editorLocked"
            maxlength="200"
            required
            placeholder="例如：断线重连后任务进度不回退"
          />
        </label>
        <label class="wide">
          <span>前置条件</span>
          <textarea
            v-model="editor.preconditions"
            :disabled="editorLocked"
            maxlength="1000"
            rows="3"
            placeholder="账号、版本、服务器、已完成的准备操作等"
          />
        </label>
        <label>
          <span>优先级</span>
          <select v-model="editor.priority" :disabled="editorLocked">
            <option v-for="priority in priorities" :key="priority" :value="priority">{{ priority }}</option>
          </select>
        </label>
        <label>
          <span>测试类型</span>
          <select v-model="editor.case_type" :disabled="editorLocked">
            <option value="manual">手工</option>
            <option value="automated">自动化</option>
          </select>
        </label>
        <label class="wide">
          <span>标签</span>
          <input
            v-model="editor.tags"
            :disabled="editorLocked"
            placeholder="战斗, 冒烟, 弱网（使用逗号分隔）"
          />
          <small :class="{ 'limit-error': uniqueTagCount > MAX_TAGS }">
            去重后 {{ uniqueTagCount }} / {{ MAX_TAGS }} 个标签
          </small>
        </label>
      </div>

      <div class="steps-heading">
        <div>
          <b>测试步骤</b>
          <small>至少保留一个完整步骤 · {{ editor.steps.length }} / {{ MAX_STEPS }}</small>
        </div>
        <button
          type="button"
          class="button small"
          :disabled="editorLocked || editor.steps.length >= MAX_STEPS"
          @click="addStep"
        >
          ＋ 添加步骤
        </button>
      </div>
      <div class="steps">
        <div v-for="(step, index) in editor.steps" :key="index" class="step-row">
          <strong>{{ index + 1 }}</strong>
          <label>
            <span>操作</span>
            <input v-model="step.action" :disabled="editorLocked" required placeholder="执行什么操作" />
          </label>
          <label>
            <span>预期结果</span>
            <input
              v-model="step.expected_result"
              :disabled="editorLocked"
              required
              placeholder="应该出现什么结果"
            />
          </label>
          <button
            type="button"
            class="icon-button danger-text"
            :disabled="editorLocked || editor.steps.length === 1"
            aria-label="删除该步骤"
            @click="removeStep(index)"
          >
            ×
          </button>
        </div>
      </div>

      <div class="form-actions">
        <button type="button" class="button" :disabled="editorLocked" @click="closeEditor">取消</button>
        <button type="submit" class="button primary" :disabled="editorLocked">
          {{ submitting ? "保存中…" : editingId ? "保存修改" : "创建草稿" }}
        </button>
      </div>
    </form>

    <div class="toolbar">
      <input v-model="query" class="search" type="search" placeholder="搜索标题、项目、套件、步骤、前置条件或标签" />
      <label class="project-filter">
        <span>项目</span>
        <select v-model="selectedProjectId" @change="selectedSuiteScope = ''">
          <option value="">全部项目</option>
          <option v-for="project in projects" :key="project.id" :value="project.id">
            {{ project.key }} · {{ project.name }}{{ project.status === "archived" ? "（已归档）" : "" }}
          </option>
        </select>
      </label>
      <label class="project-filter">
        <span>套件</span>
        <select v-model="selectedSuiteScope">
          <option value="">全部套件</option>
          <option value="unassigned">未分类</option>
          <option v-for="suite in filterSuites" :key="suite.id" :value="suite.id">
            {{ suitePaths.get(suite.id) ?? suite.name }}
          </option>
        </select>
      </label>
      <RouterLink class="button" to="/test-suites">管理套件与归档</RouterLink>
    </div>

    <div v-if="loading" class="empty state-panel">正在从本机数据库加载项目和测试用例…</div>
    <div v-else-if="loadFailed" class="empty state-panel" role="alert">
      <b>项目和测试用例加载失败</b>
      <span>请确认本机后端已启动，再重新加载；当前状态不代表数据库为空。</span>
      <button class="button primary" @click="loadData">重新加载</button>
    </div>
    <template v-else>
      <div v-if="visibleCases.length" class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>用例</th>
              <th>所属项目</th>
              <th>所属套件</th>
              <th>标签</th>
              <th>优先级 / 类型</th>
              <th>步骤</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in visibleCases" :key="item.id">
              <td><code>{{ item.id.slice(0, 8) }}</code><b>{{ item.title }}</b></td>
              <td>{{ projectNames.get(item.project_id) ?? "未知项目" }}</td>
              <td>{{ item.suite_id ? suitePaths.get(item.suite_id) ?? "已删除套件" : "未分类" }}</td>
              <td>{{ item.tags.join(" · ") || "—" }}</td>
              <td>
                <span class="priority">{{ item.priority }}</span>
                <small>{{ item.case_type === "automated" ? "自动化" : "手工" }}</small>
              </td>
              <td>{{ item.steps.length }} 步</td>
              <td><StatusBadge :status="item.status" :label="labels[item.status]" /></td>
              <td>
                <div class="row-actions">
                  <button v-if="canWrite" class="text-button" :disabled="editorLocked" @click="openEdit(item)">编辑</button>
                  <button class="text-button" :disabled="editorLocked" @click="collaborationCase = item">协作</button>
                  <button v-if="canWrite" class="text-button" :disabled="editorLocked" @click="transitionCase(item)">
                    {{ busyCaseId === item.id ? "处理中…" : statusActionLabel(item) }}
                  </button>
                  <button v-if="canWrite" class="text-button danger-text" :disabled="editorLocked" @click="deleteCase(item)">
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="empty state-panel">
        <b>{{ testCases.length ? "没有符合筛选条件的用例" : "本机数据库中还没有测试用例" }}</b>
        <span v-if="!testCases.length && activeProjects.length">点击“新建用例”完成第一条真实数据。</span>
        <span v-else-if="!activeProjects.length">请先在项目管理中创建一个进行中的项目。</span>
      </div>
      <article v-if="collaborationCase" class="panel collaboration-panel">
        <div class="panel-title">
          <div><small>CASE COLLABORATION</small><h2>{{ collaborationCase.title }}</h2></div>
          <button class="text-button" @click="collaborationCase = null">关闭</button>
        </div>
        <CollaborationPanel
          :project-id="collaborationCase.project_id"
          :entity-id="collaborationCase.id"
          entity-type="test_case"
        />
      </article>
    </template>
  </section>
</template>

<style scoped>
.feedback { align-items: center; }
.feedback button { border: 0; color: inherit; background: transparent; font-size: 10px; font-weight: 700; }
.notice.error { border-color: #efcaca; color: #a43939; background: #fff1f1; }
.editor { margin-bottom: 18px; }
.editor-heading,.steps-heading,.form-actions { display: flex; align-items: center; justify-content: space-between; gap: 15px; }
.editor-heading { margin-bottom: 18px; }
.editor-heading small { color: var(--green); font-size: 8px; letter-spacing: .15em; }
.editor-heading h2 { margin: 5px 0 0; }
.icon-button { display: grid; width: 30px; height: 30px; place-items: center; border: 0; border-radius: 7px; color: #68766f; background: #f1f5f3; font-size: 18px; }
.icon-button:disabled { cursor: not-allowed; opacity: .35; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.form-grid label,.step-row label { display: grid; gap: 6px; }
.form-grid label>span,.step-row label>span,.project-filter>span { color: #74817c; font-size: 9px; font-weight: 700; }
.form-grid label>small { color: var(--muted); font-size: 9px; }
.form-grid label>small.limit-error { color: #b94848; }
.form-grid .wide { grid-column: 1 / -1; }
.form-grid input,.form-grid select,.form-grid textarea,.step-row input,.project-filter select { width: 100%; border: 1px solid #d9e3de; border-radius: 8px; color: inherit; outline: none; background: #fff; font: inherit; font-size: 11px; }
.form-grid input,.form-grid select,.step-row input,.project-filter select { height: 39px; padding: 0 11px; }
.form-grid textarea { resize: vertical; padding: 10px 11px; line-height: 1.5; }
.form-grid input:focus,.form-grid select:focus,.form-grid textarea:focus,.step-row input:focus,.project-filter select:focus { border-color: var(--green); box-shadow: 0 0 0 3px rgba(32,134,94,.08); }
.form-grid select:disabled { color: #7c8883; background: #f1f4f2; }
.steps-heading { margin: 20px 0 10px; }
.steps-heading div b,.steps-heading div small { display: block; }
.steps-heading div b { font-size: 11px; }
.steps-heading div small { margin-top: 4px; color: var(--muted); font-size: 9px; }
.button.small { min-height: 32px; }
.steps { display: grid; gap: 8px; }
.step-row { display: grid; grid-template-columns: 28px 1fr 1fr 30px; align-items: end; gap: 9px; padding: 11px; border: 1px solid #e6ece9; border-radius: 9px; background: #f9fbfa; }
.step-row>strong { display: grid; height: 39px; place-items: center; color: var(--green); font-size: 10px; }
.form-actions { justify-content: flex-end; margin-top: 18px; padding-top: 16px; border-top: 1px solid #edf1ef; }
.toolbar { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 17px; }
.toolbar .search { flex: 1; margin: 0; }
.project-filter { display: flex; align-items: center; gap: 8px; }
.project-filter select { min-width: 230px; }
.state-panel { display: grid; gap: 8px; min-height: 150px; place-content: center; border: 1px dashed #d6e0dc; border-radius: 12px; background: #fff; }
.state-panel b { color: #36463f; font-size: 12px; }
.state-panel span { font-size: 10px; }
td small { display: block; margin-top: 6px; color: var(--muted); font-size: 8px; }
.row-actions { display: flex; align-items: center; gap: 9px; white-space: nowrap; }
.text-button { padding: 0; border: 0; color: var(--green); background: transparent; font-size: 9px; font-weight: 700; }
.text-button:disabled { cursor: not-allowed; opacity: .45; }
.danger-text { color: #b94848; }
.button:disabled { cursor: not-allowed; opacity: .55; }
.collaboration-panel { margin-top: 17px; }
.collaboration-panel :deep(.collaboration) { margin-top: 0; border: 0; padding: 0; }

@media(max-width: 800px) {
  .form-grid { grid-template-columns: 1fr; }
  .form-grid .wide { grid-column: auto; }
  .step-row { grid-template-columns: 28px 1fr 30px; }
  .step-row label:last-of-type { grid-column: 2 / 3; }
  .toolbar { align-items: stretch; flex-direction: column; }
  .project-filter { align-items: stretch; flex-direction: column; }
  .project-filter select { min-width: 0; }
}
</style>
