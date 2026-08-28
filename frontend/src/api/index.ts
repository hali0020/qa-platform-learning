import { apiClient } from "./client";
import type {
  AutomationSchedule,
  AutomationTask,
  AutomationTaskEnqueue,
  AttachmentRecord,
  AuthResult,
  AuthStatus,
  AuditAction,
  AuditEvent,
  CaseResultUpdate,
  ChangePasswordRequest,
  ClaimedDevice,
  CollaborationEntityType,
  CommentRecord,
  CurrentUser,
  DeleteResult,
  DeletedResult,
  Defect,
  DefectCreate,
  DefectSeverity,
  DefectStatus,
  DefectTransition,
  DefectUpdate,
  Device,
  DeviceAcquire,
  DeviceCreate,
  DeviceLease,
  DeviceLeaseAction,
  DeviceLeaseRenew,
  DevicePatch,
  ExecutionCreate,
  ExecutionStatus,
  ExecutionTransition,
  HealthStatus,
  ImportCommitResult,
  ImportPreview,
  IsoDateTime,
  LoginRequest,
  PasswordResetRequest,
  PipelineCallbackRequest,
  PipelineCallbackResult,
  PipelineCancellationResult,
  PipelineRun,
  PipelineTriggerRequest,
  PipelineTriggerResult,
  ProviderConnection,
  ProviderConnectionCreate,
  ProviderConnectionPatch,
  ProviderGateDecisionRequest,
  ProviderRunArtifact,
  ProviderArtifactKind,
  ProviderRuntimeStatus,
  ProviderRun,
  ProviderTestResult,
  ProviderTriggerIntent,
  ProviderTriggerRequest,
  Project,
  ProjectCreate,
  ProjectStatus,
  ProjectTransition,
  ProjectUpdate,
  QualityReport,
  RoleSummary,
  ScheduleCreate,
  ScheduleFire,
  SchedulePatch,
  SetupRequest,
  SnapshotScopeType,
  TestCase,
  TestCaseCreate,
  TestCaseSnapshot,
  TestCaseSnapshotCreate,
  TestCaseStatus,
  TestCaseTransition,
  TestCaseUpdate,
  TestExecution,
  TestPlan,
  TestPlanCreate,
  TestPlanStatus,
  TestPlanTransition,
  TestPlanUpdate,
  TestSuite,
  TestSuiteCreate,
  TestSuiteStatus,
  TestSuiteTransition,
  TestSuiteUpdate,
  TransferEntity,
  TransferFormat,
  UserAccount,
  UserCreateRequest,
  UserUpdateRequest,
} from "./types";

const API_V1 = "/api/v1";
const resourcePath = (resource: string, id?: string) =>
  id === undefined ? `${API_V1}/${resource}` : `${API_V1}/${resource}/${encodeURIComponent(id)}`;

function queryString(values: object): string {
  const search = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export interface ProjectListQuery {
  status?: ProjectStatus;
}

export interface TestCaseListQuery {
  project_id?: string;
  status?: TestCaseStatus;
  suite_id?: string;
  unassigned?: boolean;
}

export interface TestSuiteListQuery {
  project_id?: string;
  status?: TestSuiteStatus;
}

export type TestCaseSnapshotListQuery =
  | { project_id?: string; scope_type?: never; scope_id?: never }
  | { project_id?: string; scope_type: SnapshotScopeType; scope_id: string };

export interface TestPlanListQuery {
  project_id?: string;
  status?: TestPlanStatus;
}

export interface ExecutionListQuery {
  plan_id?: string;
  status?: ExecutionStatus;
}

export interface DefectListQuery {
  project_id?: string;
  status?: DefectStatus;
  severity?: DefectSeverity;
  assignee?: string;
  case_id?: string;
  execution_id?: string;
}

export interface AuditEventListQuery {
  project_id?: string;
  entity_type?: string;
  entity_id?: string;
  action?: AuditAction;
  limit?: number;
}

export const qaApi = {
  getHealth: () => apiClient.get<HealthStatus>(`${API_V1}/health`),

  getAuthStatus: () => apiClient.get<AuthStatus>(`${API_V1}/auth/status`),
  setupAdmin: (payload: SetupRequest) =>
    apiClient.post<AuthResult>(`${API_V1}/auth/setup`, payload),
  login: (payload: LoginRequest) =>
    apiClient.post<AuthResult>(`${API_V1}/auth/login`, payload),
  logout: () => apiClient.post<{ logged_out: boolean }>(`${API_V1}/auth/logout`),
  getCurrentUser: () => apiClient.get<CurrentUser>(`${API_V1}/auth/me`),
  changePassword: (payload: ChangePasswordRequest) =>
    apiClient.post<{ changed: boolean }>(`${API_V1}/auth/change-password`, payload),
  listUsers: () => apiClient.get<UserAccount[]>(`${API_V1}/users`),
  createUser: (payload: UserCreateRequest) =>
    apiClient.post<UserAccount>(`${API_V1}/users`, payload),
  updateUser: (userId: string, payload: UserUpdateRequest) =>
    apiClient.patch<UserAccount>(`${resourcePath("users", userId)}`, payload),
  resetUserPassword: (userId: string, payload: PasswordResetRequest) =>
    apiClient.post<{ reset: boolean; sessions_revoked: boolean }>(
      `${resourcePath("users", userId)}/reset-password`,
      payload,
    ),
  revokeUserSessions: (userId: string) =>
    apiClient.post<{ revoked_sessions: number }>(
      `${resourcePath("users", userId)}/revoke-sessions`,
    ),
  bindUserOidcIdentity: (userId: string, subject: string) =>
    apiClient.post<{ bound: boolean }>(
      `${resourcePath("users", userId)}/oidc-binding`,
      { subject },
    ),
  listRoles: () => apiClient.get<RoleSummary[]>(`${API_V1}/roles`),

  listComments: (entityType: CollaborationEntityType, entityId: string) =>
    apiClient.get<CommentRecord[]>(
      `${API_V1}/comments${queryString({ entity_type: entityType, entity_id: entityId })}`,
    ),
  createComment: (payload: {
    project_id: string;
    entity_type: CollaborationEntityType;
    entity_id: string;
    body: string;
    parent_id?: string | null;
  }) => apiClient.post<CommentRecord>(`${API_V1}/comments`, payload),
  updateComment: (commentId: string, body: string) =>
    apiClient.patch<CommentRecord>(resourcePath("comments", commentId), { body }),
  deleteComment: (commentId: string) =>
    apiClient.delete<CommentRecord>(resourcePath("comments", commentId)),
  listAttachments: (entityType: CollaborationEntityType, entityId: string) =>
    apiClient.get<AttachmentRecord[]>(
      `${API_V1}/attachments${queryString({ entity_type: entityType, entity_id: entityId })}`,
    ),
  uploadAttachment: (
    projectId: string,
    entityType: CollaborationEntityType,
    entityId: string,
    file: File,
    commentId?: string,
  ) => {
    const form = new FormData();
    form.set("project_id", projectId);
    form.set("entity_type", entityType);
    form.set("entity_id", entityId);
    if (commentId) form.set("comment_id", commentId);
    form.set("file", file);
    return apiClient.upload<AttachmentRecord>(`${API_V1}/attachments`, form);
  },
  downloadAttachment: (attachmentId: string, inline = false) =>
    apiClient.download(
      `${resourcePath("attachments", attachmentId)}/content${queryString({ inline })}`,
    ),
  deleteAttachment: (attachmentId: string) =>
    apiClient.delete<AttachmentRecord>(resourcePath("attachments", attachmentId)),

  downloadTransferTemplate: (entity: TransferEntity, format: TransferFormat) =>
    apiClient.download(
      `${API_V1}/data-transfer/templates/${entity}${queryString({ format })}`,
    ),
  previewImport: (entity: TransferEntity, projectId: string, file: File) => {
    const form = new FormData();
    form.set("project_id", projectId);
    form.set("file", file);
    return apiClient.upload<ImportPreview>(
      `${API_V1}/data-transfer/imports/${entity}/preview`,
      form,
    );
  },
  commitImport: (
    entity: TransferEntity,
    projectId: string,
    file: File,
    expectedSha256: string,
    requireClean = true,
  ) => {
    const form = new FormData();
    form.set("project_id", projectId);
    form.set("file", file);
    form.set("expected_sha256", expectedSha256);
    form.set("require_clean", String(requireClean));
    return apiClient.upload<ImportCommitResult>(
      `${API_V1}/data-transfer/imports/${entity}`,
      form,
    );
  },
  exportData: (
    entity: TransferEntity,
    projectId: string,
    format: TransferFormat,
  ) => apiClient.download(
    `${API_V1}/data-transfer/exports/${entity}${queryString({ project_id: projectId, format })}`,
  ),
  getQualityReport: (query: {
    project_id: string;
    date_from?: string;
    date_to?: string;
    granularity?: "day" | "week";
    timezone?: string;
  }) => apiClient.get<QualityReport>(
    `${API_V1}/quality/report${queryString(query)}`,
  ),

  listProviderConnections: () =>
    apiClient.get<ProviderConnection[]>(`${API_V1}/integrations/connections`),
  getProviderRuntimeStatus: () =>
    apiClient.get<ProviderRuntimeStatus>(
      `${API_V1}/integrations/connections/runtime-status`,
    ),
  getProviderConnection: (connectionId: string) =>
    apiClient.get<ProviderConnection>(
      `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}`,
    ),
  createProviderConnection: (payload: ProviderConnectionCreate) =>
    apiClient.post<ProviderConnection>(`${API_V1}/integrations/connections`, payload),
  updateProviderConnection: (connectionId: string, payload: ProviderConnectionPatch) =>
    apiClient.patch<ProviderConnection>(
      `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}`,
      payload,
    ),
  deleteProviderConnection: (connectionId: string) =>
    apiClient.delete<DeletedResult>(
      `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}`,
    ),
  testProviderConnection: (connectionId: string) =>
    apiClient.post<ProviderTestResult>(
      `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}/test`,
    ),
  triggerProviderConnection: (
    connectionId: string,
    payload: ProviderTriggerRequest = {},
  ) => apiClient.post<ProviderRun>(
    `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}/trigger`,
    payload,
  ),
  listProviderRuns: (connectionId: string) =>
    apiClient.get<ProviderRun[]>(
      `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}/runs`,
    ),
  getProviderRun: (connectionId: string, runId: string) =>
    apiClient.get<ProviderRun>(
      `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}/runs/${encodeURIComponent(runId)}`,
    ),
  cancelProviderRun: (connectionId: string, runId: string) =>
    apiClient.post<ProviderRun>(
      `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}/runs/${encodeURIComponent(runId)}/cancel`,
    ),
  listProviderTriggerIntents: () =>
    apiClient.get<ProviderTriggerIntent[]>(
      `${API_V1}/integrations/connections/trigger-intents/all`,
    ),
  dispatchOneProviderTrigger: (workerId = "web-manual-dispatcher", leaseSeconds = 30) =>
    apiClient.post<ProviderRun | null>(
      `${API_V1}/integrations/connections/trigger-intents/dispatch-one`,
      { worker_id: workerId, lease_seconds: leaseSeconds },
    ),
  decideProviderQualityGate: (
    connectionId: string,
    runId: string,
    payload: ProviderGateDecisionRequest,
  ) => apiClient.post<ProviderRun>(
    `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}/runs/${encodeURIComponent(runId)}/gate-decisions`,
    payload,
  ),
  listProviderRunArtifacts: (connectionId: string, runId: string) =>
    apiClient.get<ProviderRunArtifact[]>(
      `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}/runs/${encodeURIComponent(runId)}/artifacts`,
    ),
  uploadProviderRunArtifact: (
    connectionId: string,
    runId: string,
    kind: ProviderArtifactKind,
    file: File,
  ) => {
    const form = new FormData();
    form.set("kind", kind);
    form.set("file", file);
    return apiClient.upload<ProviderRunArtifact>(
      `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}/runs/${encodeURIComponent(runId)}/artifacts`,
      form,
    );
  },
  downloadProviderRunArtifact: (connectionId: string, runId: string, artifactId: string) =>
    apiClient.download(
      `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}/content`,
    ),
  deleteProviderRunArtifact: (connectionId: string, runId: string, artifactId: string) =>
    apiClient.delete<ProviderRunArtifact>(
      `${API_V1}/integrations/connections/${encodeURIComponent(connectionId)}/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`,
    ),

  listAutomationTasks: () =>
    apiClient.get<AutomationTask[]>(`${API_V1}/automation/tasks`),
  getAutomationTask: (taskId: string) =>
    apiClient.get<AutomationTask>(`${API_V1}/automation/tasks/${encodeURIComponent(taskId)}`),
  enqueueAutomationTask: (payload: AutomationTaskEnqueue) =>
    apiClient.post<{ task: AutomationTask; replayed: boolean }>(
      `${API_V1}/automation/tasks`,
      payload,
    ),
  cancelAutomationTask: (taskId: string) =>
    apiClient.post<AutomationTask>(
      `${API_V1}/automation/tasks/${encodeURIComponent(taskId)}/cancel`,
    ),
  retryAutomationTask: (taskId: string) =>
    apiClient.post<AutomationTask>(
      `${API_V1}/automation/tasks/${encodeURIComponent(taskId)}/retry`,
    ),
  deadLetterAutomationTask: (taskId: string, errorCode = "manual_dead_letter") =>
    apiClient.post<AutomationTask>(
      `${API_V1}/automation/tasks/${encodeURIComponent(taskId)}/dead-letter`,
      { error_code: errorCode },
    ),

  listDevices: () => apiClient.get<Device[]>(`${API_V1}/automation/devices`),
  getDevice: (deviceId: string) =>
    apiClient.get<Device>(`${API_V1}/automation/devices/${encodeURIComponent(deviceId)}`),
  createDevice: (payload: DeviceCreate) =>
    apiClient.post<Device>(`${API_V1}/automation/devices`, payload),
  updateDevice: (deviceId: string, payload: DevicePatch) =>
    apiClient.patch<Device>(
      `${API_V1}/automation/devices/${encodeURIComponent(deviceId)}`,
      payload,
    ),
  deleteDevice: (deviceId: string) =>
    apiClient.delete<DeletedResult>(
      `${API_V1}/automation/devices/${encodeURIComponent(deviceId)}`,
    ),
  heartbeatDevice: (deviceId: string, agentId: string) =>
    apiClient.post<Device>(
      `${API_V1}/automation/devices/${encodeURIComponent(deviceId)}/heartbeat`,
      { agent_id: agentId },
    ),
  acquireDevice: (payload: DeviceAcquire) =>
    apiClient.post<ClaimedDevice | null>(`${API_V1}/automation/devices/acquire`, payload),
  startDeviceLease: (leaseId: string, payload: DeviceLeaseAction) =>
    apiClient.post<ClaimedDevice>(
      `${API_V1}/automation/devices/leases/${encodeURIComponent(leaseId)}/start`,
      payload,
    ),
  renewDeviceLease: (leaseId: string, payload: DeviceLeaseRenew) =>
    apiClient.post<DeviceLease>(
      `${API_V1}/automation/devices/leases/${encodeURIComponent(leaseId)}/renew`,
      payload,
    ),
  releaseDeviceLease: (leaseId: string, payload: DeviceLeaseAction) =>
    apiClient.post<DeviceLease>(
      `${API_V1}/automation/devices/leases/${encodeURIComponent(leaseId)}/release`,
      payload,
    ),

  listSchedules: () =>
    apiClient.get<AutomationSchedule[]>(`${API_V1}/automation/schedules`),
  getSchedule: (scheduleId: string) =>
    apiClient.get<AutomationSchedule>(
      `${API_V1}/automation/schedules/${encodeURIComponent(scheduleId)}`,
    ),
  createSchedule: (payload: ScheduleCreate) =>
    apiClient.post<AutomationSchedule>(`${API_V1}/automation/schedules`, payload),
  updateSchedule: (scheduleId: string, payload: SchedulePatch) =>
    apiClient.patch<AutomationSchedule>(
      `${API_V1}/automation/schedules/${encodeURIComponent(scheduleId)}`,
      payload,
    ),
  deleteSchedule: (scheduleId: string) =>
    apiClient.delete<DeletedResult>(
      `${API_V1}/automation/schedules/${encodeURIComponent(scheduleId)}`,
    ),
  runScheduleNow: (scheduleId: string) =>
    apiClient.post<ScheduleFire>(
      `${API_V1}/automation/schedules/${encodeURIComponent(scheduleId)}/run-now`,
    ),
  tickSchedules: (now?: IsoDateTime) =>
    apiClient.post<ScheduleFire[]>(`${API_V1}/automation/schedules/tick`, { now: now ?? null }),
  listScheduleFires: (scheduleId: string) =>
    apiClient.get<ScheduleFire[]>(
      `${API_V1}/automation/schedules/${encodeURIComponent(scheduleId)}/fires`,
    ),

  listProjects: (query: ProjectListQuery = {}) =>
    apiClient.get<Project[]>(`${resourcePath("projects")}${queryString(query)}`),
  getProject: (projectId: string) => apiClient.get<Project>(resourcePath("projects", projectId)),
  createProject: (payload: ProjectCreate) => apiClient.post<Project>(resourcePath("projects"), payload),
  updateProject: (projectId: string, payload: ProjectUpdate) =>
    apiClient.patch<Project>(resourcePath("projects", projectId), payload),
  transitionProject: (projectId: string, payload: ProjectTransition) =>
    apiClient.post<Project>(`${resourcePath("projects", projectId)}/transition`, payload),
  deleteProject: (projectId: string) =>
    apiClient.delete<DeleteResult>(resourcePath("projects", projectId)),

  listTestCases: (query: TestCaseListQuery = {}) =>
    apiClient.get<TestCase[]>(`${resourcePath("test-cases")}${queryString(query)}`),
  getTestCase: (caseId: string) => apiClient.get<TestCase>(resourcePath("test-cases", caseId)),
  createTestCase: (payload: TestCaseCreate) =>
    apiClient.post<TestCase>(resourcePath("test-cases"), payload),
  updateTestCase: (caseId: string, payload: TestCaseUpdate) =>
    apiClient.patch<TestCase>(resourcePath("test-cases", caseId), payload),
  transitionTestCase: (caseId: string, payload: TestCaseTransition) =>
    apiClient.post<TestCase>(`${resourcePath("test-cases", caseId)}/transition`, payload),
  deleteTestCase: (caseId: string) =>
    apiClient.delete<DeleteResult>(resourcePath("test-cases", caseId)),

  listTestSuites: (query: TestSuiteListQuery = {}) =>
    apiClient.get<TestSuite[]>(`${resourcePath("test-suites")}${queryString(query)}`),
  getTestSuite: (suiteId: string) =>
    apiClient.get<TestSuite>(resourcePath("test-suites", suiteId)),
  createTestSuite: (payload: TestSuiteCreate) =>
    apiClient.post<TestSuite>(resourcePath("test-suites"), payload),
  updateTestSuite: (suiteId: string, payload: TestSuiteUpdate) =>
    apiClient.patch<TestSuite>(resourcePath("test-suites", suiteId), payload),
  transitionTestSuite: (suiteId: string, payload: TestSuiteTransition) =>
    apiClient.post<TestSuite>(`${resourcePath("test-suites", suiteId)}/transition`, payload),
  deleteTestSuite: (suiteId: string) =>
    apiClient.delete<DeleteResult>(resourcePath("test-suites", suiteId)),

  listTestCaseSnapshots: (query: TestCaseSnapshotListQuery = {}) =>
    apiClient.get<TestCaseSnapshot[]>(
      `${resourcePath("test-case-snapshots")}${queryString(query)}`,
    ),
  getTestCaseSnapshot: (snapshotId: string) =>
    apiClient.get<TestCaseSnapshot>(resourcePath("test-case-snapshots", snapshotId)),
  createTestCaseSnapshot: (payload: TestCaseSnapshotCreate) =>
    apiClient.post<TestCaseSnapshot>(resourcePath("test-case-snapshots"), payload),

  listTestPlans: (query: TestPlanListQuery = {}) =>
    apiClient.get<TestPlan[]>(`${resourcePath("test-plans")}${queryString(query)}`),
  getTestPlan: (planId: string) => apiClient.get<TestPlan>(resourcePath("test-plans", planId)),
  createTestPlan: (payload: TestPlanCreate) =>
    apiClient.post<TestPlan>(resourcePath("test-plans"), payload),
  updateTestPlan: (planId: string, payload: TestPlanUpdate) =>
    apiClient.patch<TestPlan>(resourcePath("test-plans", planId), payload),
  transitionTestPlan: (planId: string, payload: TestPlanTransition) =>
    apiClient.post<TestPlan>(`${resourcePath("test-plans", planId)}/transition`, payload),
  deleteTestPlan: (planId: string) =>
    apiClient.delete<DeleteResult>(resourcePath("test-plans", planId)),

  listExecutions: (query: ExecutionListQuery = {}) =>
    apiClient.get<TestExecution[]>(`${resourcePath("executions")}${queryString(query)}`),
  getExecution: (executionId: string) =>
    apiClient.get<TestExecution>(resourcePath("executions", executionId)),
  createExecution: (payload: ExecutionCreate) =>
    apiClient.post<TestExecution>(resourcePath("executions"), payload),
  transitionExecution: (executionId: string, payload: ExecutionTransition) =>
    apiClient.post<TestExecution>(`${resourcePath("executions", executionId)}/transition`, payload),
  updateCaseResult: (executionId: string, caseId: string, payload: CaseResultUpdate) =>
    apiClient.put<TestExecution>(
      `${resourcePath("executions", executionId)}/results/${encodeURIComponent(caseId)}`,
      payload,
    ),
  deleteExecution: (executionId: string) =>
    apiClient.delete<DeleteResult>(resourcePath("executions", executionId)),

  listDefects: (query: DefectListQuery = {}) =>
    apiClient.get<Defect[]>(`${resourcePath("defects")}${queryString(query)}`),
  getDefect: (defectId: string) => apiClient.get<Defect>(resourcePath("defects", defectId)),
  createDefect: (payload: DefectCreate) =>
    apiClient.post<Defect>(resourcePath("defects"), payload),
  updateDefect: (defectId: string, payload: DefectUpdate) =>
    apiClient.patch<Defect>(resourcePath("defects", defectId), payload),
  transitionDefect: (defectId: string, payload: DefectTransition) =>
    apiClient.post<Defect>(`${resourcePath("defects", defectId)}/transition`, payload),

  listAuditEvents: (query: AuditEventListQuery = {}) =>
    apiClient.get<AuditEvent[]>(`${resourcePath("audit-events")}${queryString(query)}`),

  listPipelineRuns: () => apiClient.get<PipelineRun[]>(resourcePath("pipelines")),
  getPipelineRun: (runId: string) => apiClient.get<PipelineRun>(resourcePath("pipelines", runId)),
  triggerPipeline: (payload: PipelineTriggerRequest) =>
    apiClient.post<PipelineTriggerResult>(resourcePath("pipelines"), payload),
  cancelPipeline: (runId: string) =>
    apiClient.post<PipelineCancellationResult>(`${resourcePath("pipelines", runId)}/cancel`),
  receivePipelineCallback: (runId: string, payload: PipelineCallbackRequest) =>
    apiClient.post<PipelineCallbackResult>(
      `${resourcePath("pipelines", runId)}/callbacks`,
      payload,
    ),
};

export {
  API_BASE_LABEL,
  API_BASE_URL,
  ApiError,
  apiClient,
  setCsrfCookieName,
  setUnauthorizedHandler,
} from "./client";
export type * from "./types";
