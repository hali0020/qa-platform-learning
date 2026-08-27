<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import {
  qaApi,
  type Project,
  type TestCase,
  type TestCaseSnapshot,
  type TestSuite,
  type TestSuiteStatus,
} from "@/api";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useAuthSession } from "@/auth/session";

const auth = useAuthSession();
const canWrite = computed(() => auth.can("qa.write"));

interface SuiteEditor {
  project_id: string;
  parent_id: string;
  name: string;
  description: string;
  position: number;
}

interface SnapshotEditor {
  project_id: string;
  suite_id: string;
  label: string;
  description: string;
}

interface SuiteTreeItem {
  suite: TestSuite;
  depth: number;
  path: string;
}

const projects = ref<Project[]>([]);
const testCases = ref<TestCase[]>([]);
const suites = ref<TestSuite[]>([]);
const snapshots = ref<TestCaseSnapshot[]>([]);

const loading = ref(true);
const loadFailed = ref(false);
const hasLoadedData = ref(false);
const writeBusyKey = ref("");
const errorMessage = ref("");
const successMessage = ref("");

const selectedProjectId = ref("");
const selectedScope = ref("all");
const caseQuery = ref("");
const selectedSnapshotId = ref("");
const snapshotDetail = ref<TestCaseSnapshot | null>(null);
const snapshotLoading = ref(false);
const snapshotError = ref("");
let snapshotRequestVersion = 0;

const suiteEditorOpen = ref(false);
const editingSuiteId = ref<string | null>(null);
const suiteEditorBaseline = ref("");
const suiteEditor = reactive<SuiteEditor>({
  project_id: "",
  parent_id: "",
  name: "",
  description: "",
  position: 0,
});

const snapshotEditorOpen = ref(false);
const snapshotEditorBaseline = ref("");
const snapshotEditor = reactive<SnapshotEditor>({
  project_id: "",
  suite_id: "",
  label: "",
  description: "",
});

const writeInProgress = computed(() => Boolean(writeBusyKey.value));
const mutationLocked = computed(() => loading.value || writeInProgress.value);
const activeProjects = computed(() => projects.value.filter((item) => item.status === "active"));
const selectedProject = computed(
  () => projects.value.find((item) => item.id === selectedProjectId.value) ?? null,
);
const projectSuites = computed(() =>
  suites.value.filter((item) => item.project_id === selectedProjectId.value),
);
const projectCases = computed(() =>
  testCases.value.filter((item) => item.project_id === selectedProjectId.value),
);
const projectSnapshots = computed(() =>
  snapshots.value
    .filter((item) => item.project_id === selectedProjectId.value)
    .sort((left, right) => right.created_at.localeCompare(left.created_at)),
);

function sortedSuites(items: TestSuite[]): TestSuite[] {
  return [...items].sort(
    (left, right) => left.position - right.position || left.name.localeCompare(right.name, "zh-CN"),
  );
}

const suiteTree = computed<SuiteTreeItem[]>(() => {
  const source = projectSuites.value;
  const ids = new Set(source.map((item) => item.id));
  const children = new Map<string | null, TestSuite[]>();
  source.forEach((suite) => {
    const parentId = suite.parent_id && ids.has(suite.parent_id) ? suite.parent_id : null;
    children.set(parentId, [...(children.get(parentId) ?? []), suite]);
  });

  const result: SuiteTreeItem[] = [];
  const visited = new Set<string>();
  const walk = (suite: TestSuite, depth: number, parentPath: string) => {
    if (visited.has(suite.id)) return;
    visited.add(suite.id);
    const path = parentPath ? `${parentPath} / ${suite.name}` : suite.name;
    result.push({ suite, depth, path });
    sortedSuites(children.get(suite.id) ?? []).forEach((child) => walk(child, depth + 1, path));
  };
  sortedSuites(children.get(null) ?? []).forEach((suite) => walk(suite, 0, ""));
  sortedSuites(source.filter((suite) => !visited.has(suite.id))).forEach((suite) => walk(suite, 0, ""));
  return result;
});

const suitePaths = computed(() => new Map(suiteTree.value.map((item) => [item.suite.id, item.path])));
const selectedSuite = computed(() =>
  projectSuites.value.find((item) => item.id === selectedScope.value) ?? null,
);

function descendantIds(suiteId: string): Set<string> {
  const result = new Set<string>([suiteId]);
  let changed = true;
  while (changed) {
    changed = false;
    projectSuites.value.forEach((suite) => {
      if (suite.parent_id && result.has(suite.parent_id) && !result.has(suite.id)) {
        result.add(suite.id);
        changed = true;
      }
    });
  }
  return result;
}

const scopeSuiteIds = computed(() =>
  selectedSuite.value ? descendantIds(selectedSuite.value.id) : new Set<string>(),
);
const visibleCases = computed(() => {
  const keyword = caseQuery.value.trim().toLowerCase();
  return projectCases.value.filter((item) => {
    if (selectedScope.value === "unclassified" && item.suite_id) return false;
    if (selectedSuite.value && (!item.suite_id || !scopeSuiteIds.value.has(item.suite_id))) return false;
    if (!keyword) return true;
    return [item.title, item.preconditions, item.tags.join(" "), item.id]
      .join(" ")
      .toLowerCase()
      .includes(keyword);
  });
});

const editingSuiteDescendants = computed(() =>
  editingSuiteId.value ? descendantIds(editingSuiteId.value) : new Set<string>(),
);
const parentSuiteOptions = computed(() =>
  projectSuites.value.filter(
    (item) =>
      item.status === "active" &&
      item.id !== editingSuiteId.value &&
      !editingSuiteDescendants.value.has(item.id),
  ),
);
const activeSuiteOptions = computed(() =>
  suiteTree.value.filter((item) => item.suite.status === "active"),
);

const snapshotScopeCount = computed(() => {
  if (!snapshotEditor.suite_id) return projectCases.value.length;
  const included = descendantIds(snapshotEditor.suite_id);
  return projectCases.value.filter((item) => item.suite_id && included.has(item.suite_id)).length;
});

const activeSuiteCount = computed(
  () => projectSuites.value.filter((item) => item.status === "active").length,
);
const archivedSuiteCount = computed(
  () => projectSuites.value.filter((item) => item.status === "archived").length,
);

function suiteEditorSignature(): string {
  return JSON.stringify({ ...suiteEditor });
}

function snapshotEditorSignature(): string {
  return JSON.stringify({ ...snapshotEditor });
}

const suiteEditorDirty = computed(
  () => suiteEditorOpen.value && suiteEditorSignature() !== suiteEditorBaseline.value,
);
const snapshotEditorDirty = computed(
  () => snapshotEditorOpen.value && snapshotEditorSignature() !== snapshotEditorBaseline.value,
);
const hasDirtyWork = computed(() => suiteEditorDirty.value || snapshotEditorDirty.value);

function readableError(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败，请稍后重试";
}

function dateLabel(value: string): string {
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

function confirmDiscard(): boolean {
  return !hasDirtyWork.value || window.confirm("当前有尚未保存的套件或快照内容，确定放弃吗？");
}

function forceCloseSuiteEditor(): void {
  suiteEditorOpen.value = false;
  editingSuiteId.value = null;
  suiteEditorBaseline.value = "";
}

function forceCloseSnapshotEditor(): void {
  snapshotEditorOpen.value = false;
  snapshotEditorBaseline.value = "";
}

function discardOpenEditors(): void {
  forceCloseSuiteEditor();
  forceCloseSnapshotEditor();
}

function replaceSuite(suite: TestSuite): void {
  const index = suites.value.findIndex((item) => item.id === suite.id);
  if (index >= 0) suites.value[index] = suite;
  else suites.value.push(suite);
}

function replaceSnapshot(snapshot: TestCaseSnapshot): void {
  const index = snapshots.value.findIndex((item) => item.id === snapshot.id);
  if (index >= 0) snapshots.value[index] = snapshot;
  else snapshots.value.push(snapshot);
}

function initializeSelectedProject(): void {
  if (projects.value.some((item) => item.id === selectedProjectId.value)) return;
  selectedProjectId.value = activeProjects.value[0]?.id ?? projects.value[0]?.id ?? "";
  selectedScope.value = "all";
}

async function loadData(): Promise<void> {
  if (writeInProgress.value) return;
  loading.value = true;
  if (!hasLoadedData.value) loadFailed.value = false;
  errorMessage.value = "";
  try {
    const [loadedProjects, loadedCases, loadedSuites, loadedSnapshots] = await Promise.all([
      qaApi.listProjects(),
      qaApi.listTestCases(),
      qaApi.listTestSuites(),
      qaApi.listTestCaseSnapshots(),
    ]);
    projects.value = loadedProjects;
    testCases.value = loadedCases;
    suites.value = loadedSuites;
    snapshots.value = loadedSnapshots;
    initializeSelectedProject();
    hasLoadedData.value = true;
    loadFailed.value = false;

    if (selectedSnapshotId.value) {
      const exists = loadedSnapshots.some((item) => item.id === selectedSnapshotId.value);
      if (exists) void loadSnapshotDetail(selectedSnapshotId.value);
      else clearSnapshotSelection();
    }
  } catch (error) {
    errorMessage.value = readableError(error);
    if (!hasLoadedData.value) loadFailed.value = true;
  } finally {
    loading.value = false;
  }
}

async function refreshData(): Promise<void> {
  if (!confirmDiscard()) return;
  discardOpenEditors();
  await loadData();
}

function changeProject(event: Event): void {
  const select = event.target as HTMLSelectElement;
  if (!confirmDiscard()) {
    select.value = selectedProjectId.value;
    return;
  }
  discardOpenEditors();
  selectedProjectId.value = select.value;
  selectedScope.value = "all";
  caseQuery.value = "";
  clearSnapshotSelection();
  resetMessages();
}

function selectScope(scope: string): void {
  if (writeInProgress.value) return;
  if (scope === selectedScope.value) return;
  if (!confirmDiscard()) return;
  discardOpenEditors();
  selectedScope.value = scope;
}

function resetSuiteEditor(parentId = ""): void {
  Object.assign(suiteEditor, {
    project_id: selectedProjectId.value,
    parent_id: parentId,
    name: "",
    description: "",
    position: projectSuites.value.length,
  });
}

function openCreateSuite(parentId = ""): void {
  if (!canWrite.value || mutationLocked.value) return;
  if (!selectedProject.value || selectedProject.value.status !== "active") {
    errorMessage.value = "请选择一个进行中的项目再创建套件";
    return;
  }
  if (!confirmDiscard()) return;
  resetMessages();
  forceCloseSnapshotEditor();
  const validParent = projectSuites.value.find(
    (item) => item.id === parentId && item.status === "active",
  );
  resetSuiteEditor(validParent?.id ?? "");
  editingSuiteId.value = null;
  suiteEditorOpen.value = true;
  suiteEditorBaseline.value = suiteEditorSignature();
}

function openEditSuite(suite: TestSuite): void {
  if (
    !canWrite.value ||
    mutationLocked.value ||
    selectedProject.value?.status !== "active" ||
    suite.status !== "active" ||
    !confirmDiscard()
  ) return;
  resetMessages();
  forceCloseSnapshotEditor();
  Object.assign(suiteEditor, {
    project_id: suite.project_id,
    parent_id: suite.parent_id ?? "",
    name: suite.name,
    description: suite.description,
    position: suite.position,
  });
  editingSuiteId.value = suite.id;
  suiteEditorOpen.value = true;
  suiteEditorBaseline.value = suiteEditorSignature();
}

function closeSuiteEditor(): void {
  if (writeInProgress.value || !confirmDiscard()) return;
  forceCloseSuiteEditor();
}

async function saveSuite(): Promise<void> {
  if (!canWrite.value || mutationLocked.value) return;
  resetMessages();
  const name = suiteEditor.name.trim();
  if (!suiteEditor.project_id || !name) {
    errorMessage.value = "所属项目和套件名称不能为空";
    return;
  }
  if (suiteEditor.parent_id && editingSuiteDescendants.value.has(suiteEditor.parent_id)) {
    errorMessage.value = "套件不能移动到自身或自己的子套件中";
    return;
  }

  const isEditing = Boolean(editingSuiteId.value);
  writeBusyKey.value = isEditing ? `suite-${editingSuiteId.value}-update` : "suite-create";
  try {
    const values = {
      parent_id: suiteEditor.parent_id || null,
      name,
      description: suiteEditor.description.trim(),
      position: Math.max(0, Math.trunc(suiteEditor.position)),
    };
    const saved = editingSuiteId.value
      ? await qaApi.updateTestSuite(editingSuiteId.value, values)
      : await qaApi.createTestSuite({ project_id: suiteEditor.project_id, ...values });
    replaceSuite(saved);
    selectedScope.value = saved.id;
    forceCloseSuiteEditor();
    showSuccess(isEditing ? `套件“${saved.name}”已更新` : `套件“${saved.name}”已创建`);
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    writeBusyKey.value = "";
  }
}

function suiteCaseCount(suiteId: string): number {
  const included = descendantIds(suiteId);
  return projectCases.value.filter((item) => item.suite_id && included.has(item.suite_id)).length;
}

function suiteHasChildren(suiteId: string): boolean {
  return projectSuites.value.some((item) => item.parent_id === suiteId);
}

async function transitionSuite(suite: TestSuite, status: TestSuiteStatus): Promise<void> {
  if (!canWrite.value || mutationLocked.value) return;
  resetMessages();
  writeBusyKey.value = `suite-${suite.id}-transition`;
  try {
    const updated = await qaApi.transitionTestSuite(suite.id, { status });
    replaceSuite(updated);
    showSuccess(status === "archived" ? `套件“${suite.name}”已归档` : `套件“${suite.name}”已恢复`);
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    writeBusyKey.value = "";
  }
}

async function deleteSuite(suite: TestSuite): Promise<void> {
  if (!canWrite.value || mutationLocked.value) return;
  if (suiteHasChildren(suite.id) || suiteCaseCount(suite.id)) {
    errorMessage.value = "只有不含子套件和用例的空套件才能删除；有内容时请使用归档";
    return;
  }
  const confirmed = window.confirm(
    `确认永久删除空套件“${suite.name}”吗？\n\n已有内容的套件应使用归档，快照不会被修改。`,
  );
  if (!confirmed) return;
  resetMessages();
  writeBusyKey.value = `suite-${suite.id}-delete`;
  try {
    await qaApi.deleteTestSuite(suite.id);
    suites.value = suites.value.filter((item) => item.id !== suite.id);
    if (selectedScope.value === suite.id) selectedScope.value = "all";
    showSuccess(`空套件“${suite.name}”已删除`);
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    writeBusyKey.value = "";
  }
}

function openSnapshotEditor(): void {
  if (!canWrite.value || mutationLocked.value) return;
  if (!selectedProject.value || selectedProject.value.status !== "active") {
    errorMessage.value = "请选择一个进行中的项目再创建归档快照";
    return;
  }
  if (!confirmDiscard()) return;
  resetMessages();
  forceCloseSuiteEditor();
  const preferredSuite = selectedSuite.value?.status === "active" ? selectedSuite.value.id : "";
  Object.assign(snapshotEditor, {
    project_id: selectedProjectId.value,
    suite_id: preferredSuite,
    label: "",
    description: "",
  });
  snapshotEditorOpen.value = true;
  snapshotEditorBaseline.value = snapshotEditorSignature();
}

function closeSnapshotEditor(): void {
  if (writeInProgress.value || !confirmDiscard()) return;
  forceCloseSnapshotEditor();
}

async function createSnapshot(): Promise<void> {
  if (!canWrite.value || mutationLocked.value) return;
  resetMessages();
  const label = snapshotEditor.label.trim();
  if (!snapshotEditor.project_id || !label) {
    errorMessage.value = "归档标签不能为空";
    return;
  }
  if (!snapshotScopeCount.value) {
    errorMessage.value = "当前归档范围中没有测试用例";
    return;
  }

  writeBusyKey.value = "snapshot-create";
  try {
    const created = await qaApi.createTestCaseSnapshot({
      project_id: snapshotEditor.project_id,
      suite_id: snapshotEditor.suite_id || null,
      label,
      description: snapshotEditor.description.trim(),
    });
    replaceSnapshot(created);
    forceCloseSnapshotEditor();
    showSuccess(`不可变快照“${created.label} · v${created.version}”已创建`);
    await selectSnapshot(created.id, true);
  } catch (error) {
    errorMessage.value = readableError(error);
  } finally {
    writeBusyKey.value = "";
  }
}

function clearSnapshotSelection(): void {
  ++snapshotRequestVersion;
  selectedSnapshotId.value = "";
  snapshotDetail.value = null;
  snapshotLoading.value = false;
  snapshotError.value = "";
}

async function loadSnapshotDetail(snapshotId: string): Promise<void> {
  const requestVersion = ++snapshotRequestVersion;
  snapshotLoading.value = true;
  snapshotError.value = "";
  try {
    const detail = await qaApi.getTestCaseSnapshot(snapshotId);
    if (requestVersion === snapshotRequestVersion && selectedSnapshotId.value === snapshotId) {
      snapshotDetail.value = detail;
    }
  } catch (error) {
    if (requestVersion === snapshotRequestVersion && selectedSnapshotId.value === snapshotId) {
      snapshotDetail.value = null;
      snapshotError.value = readableError(error);
    }
  } finally {
    if (requestVersion === snapshotRequestVersion) snapshotLoading.value = false;
  }
}

async function selectSnapshot(snapshotId: string, force = false): Promise<void> {
  if (writeInProgress.value && !force) return;
  if (!force && snapshotId === selectedSnapshotId.value) return;
  if (!force && !confirmDiscard()) return;
  if (!force) discardOpenEditors();
  selectedSnapshotId.value = snapshotId;
  snapshotDetail.value = null;
  await loadSnapshotDetail(snapshotId);
}

function retrySnapshot(): void {
  if (selectedSnapshotId.value) void loadSnapshotDetail(selectedSnapshotId.value);
}

function snapshotScopeLabel(snapshot: TestCaseSnapshot): string {
  return snapshot.scope_type === "project" ? `整个项目 · ${snapshot.scope_name}` : snapshot.scope_name;
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
  ++snapshotRequestVersion;
  window.removeEventListener("beforeunload", handleBeforeUnload);
});

onBeforeRouteLeave(() => confirmDiscard());
</script>

<template>
  <section>
    <PageHeader
      eyebrow="TEST ASSETS"
      title="用例套件与归档"
      description="用树形套件组织当前测试资产，并用不可变快照保存版本验收时的历史基线。"
    >
      <template #actions>
        <button class="button" :disabled="mutationLocked" @click="refreshData">刷新</button>
        <RouterLink class="button" to="/test-cases">管理测试用例</RouterLink>
        <button
          v-if="canWrite"
          class="button"
          :disabled="mutationLocked || selectedProject?.status !== 'active'"
          @click="openCreateSuite()"
        >＋ 新建套件</button>
        <button
          v-if="canWrite"
          class="button primary"
          :disabled="mutationLocked || selectedProject?.status !== 'active'"
          @click="openSnapshotEditor"
        >创建归档快照</button>
      </template>
    </PageHeader>

    <div class="notice online">
      <b>套件归档 ≠ 用例快照</b>
      <span>套件归档可恢复；快照创建后不可修改，活跃用例继续变化也不会覆盖历史版本。</span>
    </div>
    <div v-if="errorMessage" class="notice notice--error" role="alert"><b>操作未完成</b><span>{{ errorMessage }}</span></div>
    <div v-if="successMessage" class="notice notice--success" role="status"><b>操作成功</b><span>{{ successMessage }}</span></div>

    <form v-if="canWrite && suiteEditorOpen" class="panel editor" @submit.prevent="saveSuite">
      <div class="section-heading">
        <div><small>{{ editingSuiteId ? "EDIT SUITE" : "NEW SUITE" }}</small><h2>{{ editingSuiteId ? "编辑用例套件" : "新建用例套件" }}</h2></div>
        <button class="icon-button" type="button" :disabled="mutationLocked" aria-label="关闭套件编辑器" @click="closeSuiteEditor">×</button>
      </div>
      <div class="form-grid">
        <label><span>所属项目</span><input :value="selectedProject?.name" disabled /></label>
        <label>
          <span>父套件</span>
          <select v-model="suiteEditor.parent_id" :disabled="mutationLocked">
            <option value="">根套件</option>
            <option v-for="item in parentSuiteOptions" :key="item.id" :value="item.id">{{ suitePaths.get(item.id) ?? item.name }}</option>
          </select>
        </label>
        <label><span>套件名称</span><input v-model="suiteEditor.name" maxlength="120" required :disabled="mutationLocked" placeholder="例如：战斗 / 技能" /></label>
        <label><span>排序位置</span><input v-model.number="suiteEditor.position" type="number" min="0" max="10000" :disabled="mutationLocked" /></label>
        <label class="wide"><span>套件说明</span><textarea v-model="suiteEditor.description" maxlength="1000" rows="3" :disabled="mutationLocked"></textarea></label>
      </div>
      <div class="form-actions"><small v-if="suiteEditorDirty">有尚未保存的修改</small><button class="button" type="button" :disabled="mutationLocked" @click="closeSuiteEditor">取消</button><button class="button primary" type="submit" :disabled="mutationLocked">{{ writeInProgress ? "保存中…" : "保存套件" }}</button></div>
    </form>

    <form v-if="canWrite && snapshotEditorOpen" class="panel editor snapshot-editor" @submit.prevent="createSnapshot">
      <div class="section-heading">
        <div><small>IMMUTABLE SNAPSHOT</small><h2>创建用例归档快照</h2></div>
        <button class="icon-button" type="button" :disabled="mutationLocked" aria-label="关闭快照编辑器" @click="closeSnapshotEditor">×</button>
      </div>
      <div class="form-grid">
        <label>
          <span>归档范围</span>
          <select v-model="snapshotEditor.suite_id" :disabled="mutationLocked">
            <option value="">整个项目</option>
            <option v-for="item in activeSuiteOptions" :key="item.suite.id" :value="item.suite.id">{{ item.path }}</option>
          </select>
          <small>将冻结 {{ snapshotScopeCount }} 条当前用例，并包含所选套件的子套件。</small>
        </label>
        <label><span>归档标签</span><input v-model="snapshotEditor.label" maxlength="100" required :disabled="mutationLocked" placeholder="例如：1.3.0 上线验收基线" /></label>
        <label class="wide"><span>归档说明</span><textarea v-model="snapshotEditor.description" maxlength="1000" rows="3" :disabled="mutationLocked" placeholder="记录版本、包体、分支或验收背景"></textarea></label>
      </div>
      <div class="form-actions"><small v-if="snapshotEditorDirty">有尚未保存的修改</small><button class="button" type="button" :disabled="mutationLocked" @click="closeSnapshotEditor">取消</button><button class="button primary" type="submit" :disabled="mutationLocked || !snapshotScopeCount">{{ writeInProgress ? "归档中…" : "创建不可变快照" }}</button></div>
    </form>

    <div v-if="hasLoadedData" class="project-toolbar">
      <label><span>当前项目</span><select :value="selectedProjectId" :disabled="writeInProgress" @change="changeProject"><option v-for="project in projects" :key="project.id" :value="project.id">{{ project.key }} · {{ project.name }}{{ project.status === "archived" ? "（已归档）" : "" }}</option></select></label>
      <small v-if="selectedProject?.status === 'archived'">此页的套件和快照为只读，可继续查看当前用例与历史版本。</small>
    </div>

    <div v-if="loading && !hasLoadedData" class="panel state-panel" role="status"><span class="spinner" aria-hidden="true"></span><b>正在加载本机测试资产…</b></div>
    <div v-else-if="loadFailed && !hasLoadedData" class="panel state-panel" role="alert"><b>测试资产首次加载失败</b><span>请确认本机后端已经启动；失败状态不代表数据为空。</span><button class="button primary" @click="loadData">重新加载</button></div>
    <div v-else-if="!projects.length" class="panel state-panel"><b>还没有项目</b><span>请先创建项目，再建立套件和归档基线。</span><RouterLink class="button primary" to="/projects">前往项目管理</RouterLink></div>

    <template v-else>
      <div class="stats asset-stats">
        <article><span>启用套件</span><strong>{{ activeSuiteCount }}</strong><small>可继续接收和组织测试用例</small></article>
        <article><span>归档套件</span><strong>{{ archivedSuiteCount }}</strong><small>保留历史结构，可恢复</small></article>
        <article><span>当前用例</span><strong>{{ projectCases.length }}</strong><small>{{ projectCases.filter((item) => !item.suite_id).length }} 条尚未分类</small></article>
        <article><span>历史快照</span><strong>{{ projectSnapshots.length }}</strong><small>不可修改的验收基线</small></article>
      </div>

      <div class="asset-layout">
        <article class="panel suite-panel">
          <div class="panel-title"><div><small>SUITE TREE</small><h2>套件目录</h2></div></div>
          <button class="scope-row" :class="{ active: selectedScope === 'all' }" :disabled="writeInProgress" @click="selectScope('all')"><span><b>全部用例</b><small>当前项目的完整测试资产</small></span><em>{{ projectCases.length }}</em></button>
          <button class="scope-row" :class="{ active: selectedScope === 'unclassified' }" :disabled="writeInProgress" @click="selectScope('unclassified')"><span><b>未分类</b><small>尚未放入任何套件</small></span><em>{{ projectCases.filter((item) => !item.suite_id).length }}</em></button>
          <p v-if="!suiteTree.length" class="inline-empty">当前项目还没有套件。</p>
          <div v-for="item in suiteTree" :key="item.suite.id" class="suite-row" :class="{ active: selectedScope === item.suite.id }" :style="{ paddingLeft: `${12 + item.depth * 14}px` }">
            <button class="suite-select" type="button" :disabled="writeInProgress" @click="selectScope(item.suite.id)"><span><b>{{ item.suite.name }}</b><small>{{ item.suite.description || item.path }}</small></span><em>{{ suiteCaseCount(item.suite.id) }}</em></button>
            <div class="suite-actions">
              <StatusBadge :status="item.suite.status" :label="item.suite.status === 'active' ? '启用' : '已归档'" />
              <button v-if="canWrite && item.suite.status === 'active'" type="button" :disabled="mutationLocked || selectedProject?.status !== 'active'" @click="openCreateSuite(item.suite.id)">＋子套件</button>
              <button v-if="canWrite" type="button" :disabled="mutationLocked || selectedProject?.status !== 'active' || item.suite.status !== 'active'" @click="openEditSuite(item.suite)">编辑</button>
              <button v-if="canWrite" type="button" :disabled="mutationLocked || selectedProject?.status !== 'active'" @click="transitionSuite(item.suite, item.suite.status === 'active' ? 'archived' : 'active')">{{ item.suite.status === "active" ? "归档" : "恢复" }}</button>
              <button v-if="canWrite" class="danger-text" type="button" :disabled="mutationLocked || selectedProject?.status !== 'active' || suiteHasChildren(item.suite.id) || Boolean(suiteCaseCount(item.suite.id))" @click="deleteSuite(item.suite)">删除</button>
            </div>
          </div>
        </article>

        <article class="panel case-panel">
          <div class="panel-title"><div><small>CURRENT CASES</small><h2>{{ selectedSuite ? suitePaths.get(selectedSuite.id) : selectedScope === "unclassified" ? "未分类用例" : "全部当前用例" }} · {{ visibleCases.length }}</h2></div><RouterLink to="/test-cases">编辑用例 →</RouterLink></div>
          <input v-model="caseQuery" class="search" type="search" placeholder="搜索当前范围内的用例" />
          <p v-if="loading" class="inline-empty">正在刷新，当前保留上次成功数据…</p>
          <div v-if="visibleCases.length" class="table-wrap"><table><thead><tr><th>用例</th><th>套件路径</th><th>优先级 / 类型</th><th>状态</th></tr></thead><tbody><tr v-for="item in visibleCases" :key="item.id"><td><code>{{ item.id.slice(0, 8) }}</code><b>{{ item.title }}</b></td><td>{{ item.suite_id ? suitePaths.get(item.suite_id) ?? "已删除套件" : "未分类" }}</td><td><span class="priority">{{ item.priority }}</span> · {{ item.case_type === "automated" ? "自动化" : "手工" }}</td><td><StatusBadge :status="item.status" /></td></tr></tbody></table></div>
          <p v-else class="inline-empty">当前范围没有符合条件的测试用例。</p>
        </article>
      </div>

      <div class="snapshot-layout">
        <article class="panel snapshot-list">
          <div class="panel-title"><div><small>ARCHIVE VERSIONS</small><h2>历史快照 · {{ projectSnapshots.length }}</h2></div></div>
          <p v-if="!projectSnapshots.length" class="inline-empty">还没有创建过归档快照。</p>
          <button v-for="snapshot in projectSnapshots" :key="snapshot.id" class="snapshot-row" :class="{ active: snapshot.id === selectedSnapshotId }" type="button" :disabled="writeInProgress" @click="selectSnapshot(snapshot.id)"><code>v{{ snapshot.version }}</code><span><b>{{ snapshot.label }}</b><small>{{ snapshotScopeLabel(snapshot) }} · {{ snapshot.case_count }} 条用例</small></span><time>{{ dateLabel(snapshot.created_at) }}</time></button>
        </article>

        <article class="panel snapshot-detail">
          <div v-if="snapshotLoading" class="detail-state" role="status"><span class="spinner" aria-hidden="true"></span><span>正在读取快照详情…</span></div>
          <div v-else-if="snapshotError" class="detail-state error-state" role="alert"><b>快照详情加载失败</b><span>{{ snapshotError }}</span><button class="button" @click="retrySnapshot">重新加载</button></div>
          <div v-else-if="!snapshotDetail" class="detail-state"><b>选择一个历史快照</b><span>快照内容只读，不会随当前用例变化。</span></div>
          <template v-else>
            <header class="snapshot-heading"><div><small>IMMUTABLE · v{{ snapshotDetail.version }}</small><h2>{{ snapshotDetail.label }}</h2><p>{{ snapshotScopeLabel(snapshotDetail) }} · 创建于 {{ dateLabel(snapshotDetail.created_at) }}</p></div><StatusBadge status="archived" label="只读快照" /></header>
            <p class="snapshot-description">{{ snapshotDetail.description || "未填写归档说明。" }}</p>
            <div class="table-wrap"><table><thead><tr><th>冻结用例</th><th>原套件路径</th><th>优先级 / 类型</th><th>状态</th><th>步骤</th></tr></thead><tbody><tr v-for="item in snapshotDetail.items" :key="`${item.source_case_id}-${item.source_updated_at}`"><td><code>{{ item.source_case_id.slice(0, 8) }}</code><b>{{ item.title }}</b></td><td>{{ item.suite_path.length ? item.suite_path.join(" / ") : "未分类" }}</td><td><span class="priority">{{ item.priority }}</span> · {{ item.case_type === "automated" ? "自动化" : "手工" }}</td><td><StatusBadge :status="item.status" /></td><td>{{ item.steps.length }} 步</td></tr></tbody></table></div>
          </template>
        </article>
      </div>
    </template>
  </section>
</template>

<style scoped>
.notice--error { border-color:#efcaca; color:#a43f3f; background:#fff2f2; }.notice--success { border-color:#cde6da; color:#236f50; background:#eff9f4; }
.editor { margin-bottom:18px; border-color:#cde6da; }.snapshot-editor { border-color:#d9dfef; }
.section-heading,.form-actions,.project-toolbar,.snapshot-heading { display:flex; align-items:center; justify-content:space-between; gap:14px; }.section-heading small,.snapshot-heading small { color:var(--green); font-size:8px; font-weight:700; letter-spacing:.15em; }.section-heading h2,.snapshot-heading h2 { margin:5px 0 0; }
.icon-button { width:32px; height:32px; border:0; border-radius:8px; color:#687770; background:#f0f4f2; font-size:18px; }
.form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:13px; margin-top:17px; }.form-grid label { display:grid; gap:6px; }.form-grid label>span,.project-toolbar label>span { color:#5f6f68; font-size:9px; font-weight:700; }.form-grid label>small { color:var(--muted); font-size:8px; }.form-grid input,.form-grid select,.form-grid textarea,.project-toolbar select { width:100%; padding:10px 11px; border:1px solid #d8e2dd; border-radius:8px; outline:none; color:#273730; background:#fff; font:inherit; font-size:10px; }.form-grid textarea { resize:vertical; line-height:1.55; }.form-grid input:disabled { color:#77857f; background:#f2f5f3; }.wide { grid-column:1/-1; }
.form-actions { justify-content:flex-end; margin-top:16px; padding-top:14px; border-top:1px solid #edf1ef; }.form-actions small { margin-right:auto; color:#9a6b20; font-size:8px; }.button:disabled,.icon-button:disabled,.suite-actions button:disabled,.scope-row:disabled,.suite-select:disabled,.snapshot-row:disabled { cursor:not-allowed; opacity:.5; }
.project-toolbar { margin-bottom:16px; padding:12px 14px; border:1px solid var(--line); border-radius:10px; background:#fff; }.project-toolbar label { display:flex; align-items:center; gap:10px; }.project-toolbar select { min-width:300px; padding:8px 10px; }.project-toolbar>small { color:#987026; font-size:8px; }
.state-panel,.detail-state { display:grid; min-height:180px; place-content:center; justify-items:center; gap:9px; color:var(--muted); text-align:center; }.state-panel b,.detail-state b { color:#405149; font-size:12px; }.state-panel span,.detail-state span { font-size:9px; }.spinner { width:23px; height:23px; border:3px solid #dce9e3; border-top-color:var(--green); border-radius:50%; animation:spin .8s linear infinite; }@keyframes spin { to { transform:rotate(360deg); } }
.asset-layout,.snapshot-layout { display:grid; grid-template-columns:minmax(280px,.65fr) minmax(500px,1.35fr); gap:15px; align-items:start; }.snapshot-layout { margin-top:15px; grid-template-columns:minmax(280px,.65fr) minmax(500px,1.35fr); }.asset-stats { margin-bottom:15px; }
.suite-panel,.snapshot-list { padding:18px 0; overflow:hidden; }.suite-panel .panel-title,.snapshot-list .panel-title { padding:0 18px 10px; }
.scope-row,.suite-row,.snapshot-row { border-top:1px solid #edf1ef; }.scope-row { display:flex; width:100%; align-items:center; justify-content:space-between; gap:9px; padding:12px 16px; border-inline:0; border-bottom:0; color:inherit; background:#fff; text-align:left; }.scope-row.active,.suite-row.active,.snapshot-row.active { background:#eef8f3; box-shadow:inset 3px 0 var(--green); }.scope-row span b,.scope-row span small,.suite-select span b,.suite-select span small,.snapshot-row span b,.snapshot-row span small { display:block; }.scope-row b,.suite-select b,.snapshot-row b { font-size:10px; }.scope-row small,.suite-select small,.snapshot-row small { margin-top:4px; color:var(--muted); font-size:8px; }.scope-row em,.suite-select em { color:#66776f; font-size:9px; font-style:normal; }
.suite-row { display:grid; grid-template-columns:minmax(0,1fr); padding-top:8px; padding-bottom:8px; }.suite-select { display:flex; min-width:0; align-items:center; justify-content:space-between; gap:8px; padding:3px 12px 5px 0; border:0; color:inherit; background:transparent; text-align:left; }.suite-select span { min-width:0; }.suite-select small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.suite-actions { display:flex; align-items:center; flex-wrap:wrap; gap:8px; padding-right:10px; }.suite-actions button { padding:2px 0; border:0; color:#52675e; background:transparent; font-size:8px; font-weight:700; }.suite-actions .danger-text { color:#ad4848; }
.case-panel .search { width:100%; margin:0 0 12px; }.inline-empty { margin:0; padding:26px 14px; color:var(--muted); font-size:9px; text-align:center; }.table-wrap table { min-width:620px; }
.snapshot-row { display:grid; width:100%; grid-template-columns:auto minmax(0,1fr) auto; align-items:center; gap:10px; padding:12px 16px; border-inline:0; border-bottom:0; color:inherit; background:#fff; text-align:left; }.snapshot-row code { padding:5px 7px; border-radius:6px; color:#3f6e5c; background:#edf5f1; font-size:8px; }.snapshot-row time { color:var(--muted); font-size:8px; }.snapshot-detail { min-height:260px; }.error-state { color:#a43f3f; }.snapshot-heading { align-items:flex-start; padding-bottom:14px; border-bottom:1px solid #e8efeb; }.snapshot-heading p { margin:7px 0 0; color:var(--muted); font-size:8px; }.snapshot-description { margin:13px 0; padding:10px 12px; border-radius:8px; color:#596a62; background:#f4f8f6; font-size:9px; line-height:1.6; }
@media(max-width:1100px) { .asset-layout,.snapshot-layout { grid-template-columns:1fr; } }
@media(max-width:700px) { .form-grid { grid-template-columns:1fr; }.wide { grid-column:auto; }.section-heading,.form-actions,.project-toolbar,.snapshot-heading { align-items:flex-start; flex-direction:column; }.form-actions small { margin:0; }.project-toolbar label,.project-toolbar select { width:100%; min-width:0; }.asset-stats { grid-template-columns:1fr; }.snapshot-row { grid-template-columns:auto 1fr; }.snapshot-row time { grid-column:2; } }
</style>
