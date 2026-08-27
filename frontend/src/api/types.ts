export type IsoDateTime = string;

export interface ApiEnvelope<T> {
  code: number;
  message: string;
  data: T;
}

export interface DeleteResult {
  deleted_id: string;
}

export interface DeletedResult {
  deleted: boolean;
}

export type ProjectStatus = "active" | "archived";

export interface Project {
  id: string;
  key: string;
  name: string;
  description: string;
  status: ProjectStatus;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface ProjectCreate {
  key: string;
  name: string;
  description?: string;
}

export interface ProjectUpdate {
  name?: string;
  description?: string;
}

export interface ProjectTransition {
  status: ProjectStatus;
}

export type TestCasePriority = "P0" | "P1" | "P2" | "P3";
export type TestCaseStatus = "draft" | "active" | "disabled";
export type TestCaseType = "manual" | "automated";

export interface TestStep {
  action: string;
  expected_result: string;
}

export interface TestCase {
  id: string;
  project_id: string;
  suite_id: string | null;
  title: string;
  preconditions: string;
  steps: TestStep[];
  priority: TestCasePriority;
  case_type: TestCaseType;
  status: TestCaseStatus;
  tags: string[];
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface TestCaseCreate {
  project_id: string;
  suite_id?: string | null;
  title: string;
  preconditions?: string;
  steps?: TestStep[];
  priority?: TestCasePriority;
  case_type?: TestCaseType;
  tags?: string[];
}

export interface TestCaseUpdate {
  suite_id?: string | null;
  title?: string;
  preconditions?: string;
  steps?: TestStep[];
  priority?: TestCasePriority;
  case_type?: TestCaseType;
  tags?: string[];
}

export interface TestCaseTransition {
  status: TestCaseStatus;
}

export type TestSuiteStatus = "active" | "archived";

export interface TestSuite {
  id: string;
  project_id: string;
  parent_id: string | null;
  name: string;
  description: string;
  status: TestSuiteStatus;
  position: number;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface TestSuiteCreate {
  project_id: string;
  parent_id?: string | null;
  name: string;
  description?: string;
  position?: number;
}

export interface TestSuiteUpdate {
  parent_id?: string | null;
  name?: string;
  description?: string;
  position?: number;
}

export interface TestSuiteTransition {
  status: TestSuiteStatus;
}

export type SnapshotScopeType = "project" | "suite";

export interface TestCaseSnapshotItem {
  source_case_id: string;
  source_suite_id: string | null;
  suite_path: string[];
  position: number;
  title: string;
  preconditions: string;
  steps: TestStep[];
  priority: TestCasePriority;
  case_type: TestCaseType;
  status: TestCaseStatus;
  tags: string[];
  source_created_at: IsoDateTime;
  source_updated_at: IsoDateTime;
}

export interface TestCaseSnapshot {
  id: string;
  project_id: string;
  scope_type: SnapshotScopeType;
  scope_id: string;
  scope_name: string;
  version: number;
  label: string;
  description: string;
  case_count: number;
  items: TestCaseSnapshotItem[];
  created_at: IsoDateTime;
}

export interface TestCaseSnapshotCreate {
  project_id: string;
  suite_id?: string | null;
  label: string;
  description?: string;
  include_descendants?: boolean;
}

export type TestPlanStatus = "draft" | "ready" | "running" | "completed" | "cancelled";

export interface TestPlan {
  id: string;
  project_id: string;
  name: string;
  description: string;
  case_ids: string[];
  status: TestPlanStatus;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface TestPlanCreate {
  project_id: string;
  name: string;
  description?: string;
  case_ids?: string[];
}

export interface TestPlanUpdate {
  name?: string;
  description?: string;
  case_ids?: string[];
}

export interface TestPlanTransition {
  status: TestPlanStatus;
}

export type ExecutionStatus = "created" | "running" | "completed" | "cancelled";
export type CaseResultStatus = "not_run" | "passed" | "failed" | "blocked" | "skipped";

export interface CaseExecutionResult {
  case_id: string;
  case_title: string;
  status: CaseResultStatus;
  actual_result: string;
  comment: string;
  executed_at: IsoDateTime | null;
}

export interface TestExecution {
  id: string;
  plan_id: string;
  project_id: string;
  status: ExecutionStatus;
  results: CaseExecutionResult[];
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  started_at: IsoDateTime | null;
  completed_at: IsoDateTime | null;
}

export interface ExecutionCreate {
  plan_id: string;
}

export interface ExecutionTransition {
  status: ExecutionStatus;
}

export interface CaseResultUpdate {
  status: CaseResultStatus;
  actual_result?: string;
  comment?: string;
}

export type DefectStatus =
  | "open"
  | "in_progress"
  | "resolved"
  | "verified"
  | "closed"
  | "reopened";
export type DefectSeverity = "blocker" | "critical" | "major" | "minor" | "trivial";
export type DefectPriority = "P0" | "P1" | "P2" | "P3";

export interface Defect {
  id: string;
  project_id: string;
  case_id: string | null;
  execution_id: string | null;
  title: string;
  description: string;
  severity: DefectSeverity;
  priority: DefectPriority;
  status: DefectStatus;
  reporter: string;
  assignee: string;
  environment: string;
  reproduction_steps: string[];
  expected_result: string;
  actual_result: string;
  resolution: string;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  resolved_at: IsoDateTime | null;
  closed_at: IsoDateTime | null;
}

export interface DefectCreate {
  project_id: string;
  case_id?: string | null;
  execution_id?: string | null;
  title: string;
  description?: string;
  severity?: DefectSeverity;
  priority?: DefectPriority;
  reporter?: string;
  assignee?: string;
  environment?: string;
  reproduction_steps?: string[];
  expected_result?: string;
  actual_result?: string;
}

export interface DefectUpdate {
  title?: string;
  description?: string;
  severity?: DefectSeverity;
  priority?: DefectPriority;
  assignee?: string;
  environment?: string;
  reproduction_steps?: string[];
  expected_result?: string;
  actual_result?: string;
}

export interface DefectTransition {
  status: DefectStatus;
  resolution?: string;
  comment?: string;
}

export type AuditAction =
  | "created"
  | "updated"
  | "status_changed"
  | "deleted"
  | "snapshot_created";

export interface AuditChange {
  before: unknown;
  after: unknown;
}

export interface AuditEvent {
  id: string;
  project_id: string | null;
  entity_type: string;
  entity_id: string;
  action: AuditAction;
  actor: string;
  changes: Record<string, AuditChange>;
  comment: string;
  created_at: IsoDateTime;
}

export interface HealthStatus {
  service: string;
  environment: string;
  local_only: boolean;
}

export type UserStatus = "active" | "disabled";

export interface RoleSummary {
  key: string;
  name: string;
  description: string;
  builtin?: boolean;
  permissions: string[];
}

export interface CurrentUser {
  id: string;
  username: string;
  display_name: string;
  status: UserStatus;
  roles: string[];
  permissions: string[];
  last_login_at?: IsoDateTime | null;
}

export interface UserAccount extends CurrentUser {
  created_at: IsoDateTime;
}

export interface AuthStatus {
  enabled: boolean;
  authentication_method: "local_accounts" | "oidc";
  setup_required: boolean;
  setup_allowed: boolean;
  authenticated: boolean;
  csrf_cookie_name: string;
}

export interface AuthResult {
  user: CurrentUser;
  csrf_token?: string;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface SetupRequest extends LoginRequest {
  display_name: string;
}

export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export interface UserCreateRequest {
  username: string;
  display_name: string;
  password: string;
  role: string;
}

export interface UserUpdateRequest {
  display_name?: string;
  status?: UserStatus;
  role?: string;
}

export interface PasswordResetRequest {
  new_password: string;
}

export type TransferEntity = "test-cases" | "defects";
export type TransferFormat = "csv" | "xlsx";
export type ImportRowStatus = "valid" | "invalid" | "created" | "failed" | "skipped";

export interface ImportIssue {
  sheet: string;
  row: number;
  row_key: string;
  field: string;
  code: string;
  message: string;
  severity: "error" | "warning";
  value: unknown | null;
}

export interface ImportRowPreview {
  sheet: string;
  row: number;
  row_key: string;
  status: ImportRowStatus;
  issues: ImportIssue[];
}

export interface ImportPreview {
  entity: TransferEntity;
  filename: string;
  sha256: string;
  template_version: string;
  total_rows: number;
  valid_rows: number;
  invalid_rows: number;
  error_count: number;
  warning_count: number;
  can_commit_clean: boolean;
  can_commit_partial: boolean;
  atomic_commit: false;
  rows: ImportRowPreview[];
  issues: ImportIssue[];
  omitted_issue_count: number;
}

export interface ImportCommitResult {
  entity: TransferEntity;
  filename: string;
  sha256: string;
  mode: "partial_create_only";
  atomic: false;
  clean_preview_required: boolean;
  committed: boolean;
  total_rows: number;
  created_rows: number;
  failed_rows: number;
  skipped_rows: number;
  rows: Array<{
    sheet: string;
    row: number;
    row_key: string;
    status: ImportRowStatus;
    entity_id: string | null;
    issues: ImportIssue[];
  }>;
}

export interface CountAndRate {
  numerator: number;
  denominator: number;
  percent: number | null;
}

export interface QualitySummary {
  project_id: string;
  period: { date_from: string; date_to: string; timezone: string };
  test_cases: {
    total_current: number;
    active_current: number;
    automated_active_current: number;
    automation_coverage: CountAndRate;
    execution_reach: CountAndRate;
  };
  executions: {
    completed_executions: number;
    total_results: number;
    executed_results: number;
    passed: number;
    failed: number;
    blocked: number;
    skipped: number;
    not_run: number;
    completion_rate: CountAndRate;
    pass_rate: CountAndRate;
    failure_defect_coverage: CountAndRate;
  };
  defects: {
    created_in_period: number;
    resolved_in_period: number;
    closed_in_period: number;
    reopened_in_period: number;
    not_closed_current: number;
    unresolved_current: number;
    high_severity_not_closed_current: number;
  };
  generated_at: IsoDateTime;
}

export interface QualityTrendPoint {
  bucket_start: string;
  completed_executions: number;
  passed: number;
  failed: number;
  blocked: number;
  skipped: number;
  not_run: number;
  pass_rate: CountAndRate;
  defects_created: number;
  defects_resolved: number;
  defects_closed: number;
  defects_reopened: number;
}

export interface SuiteCoverage {
  suite_id: string | null;
  suite_path: string;
  suite_status: TestSuiteStatus | null;
  active_cases: number;
  automated_cases: number;
  automation_coverage: CountAndRate;
  executed_cases: number;
  execution_reach: CountAndRate;
  failed_or_blocked_results: number;
  linked_failed_or_blocked_results: number;
  failure_defect_coverage: CountAndRate;
}

export interface QualityReport {
  summary: QualitySummary;
  granularity: "day" | "week";
  trends: QualityTrendPoint[];
  coverage_by_suite: SuiteCoverage[];
}

export type CollaborationEntityType =
  | "project"
  | "test_case"
  | "test_suite"
  | "test_plan"
  | "execution"
  | "defect"
  | "snapshot";

export interface CommentRecord {
  id: string;
  project_id: string;
  entity_type: CollaborationEntityType;
  entity_id: string;
  parent_id: string | null;
  author_id: string;
  author_name: string;
  body: string;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  edited_at: IsoDateTime | null;
  deleted_at: IsoDateTime | null;
  deleted_by_id: string | null;
}

export interface AttachmentRecord {
  id: string;
  project_id: string;
  entity_type: CollaborationEntityType;
  entity_id: string;
  comment_id: string | null;
  uploader_id: string;
  uploader_name: string;
  original_filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  is_image: boolean;
  created_at: IsoDateTime;
  deleted_at: IsoDateTime | null;
}

export type PipelineStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface PipelineJobSpec {
  name: string;
  duration_ms?: number;
  should_fail?: boolean;
}

export interface PipelineStageSpec {
  name: string;
  jobs: PipelineJobSpec[];
}

export interface PipelineTriggerRequest {
  name: string;
  stages: PipelineStageSpec[];
  idempotency_key?: string;
  auto_start?: boolean;
  variables?: Record<string, string>;
}

export interface PipelineJobResult {
  name: string;
  status: PipelineStatus;
  duration_ms: number;
  should_fail: boolean;
  started_at: IsoDateTime | null;
  finished_at: IsoDateTime | null;
  message: string | null;
}

export interface PipelineStageResult {
  name: string;
  status: PipelineStatus;
  jobs: PipelineJobResult[];
  started_at: IsoDateTime | null;
  finished_at: IsoDateTime | null;
  message: string | null;
}

export interface PipelineRun {
  id: string;
  name: string;
  status: PipelineStatus;
  stages: PipelineStageResult[];
  variables: Record<string, unknown>;
  idempotency_key: string | null;
  created_at: IsoDateTime;
  started_at: IsoDateTime | null;
  finished_at: IsoDateTime | null;
  message: string | null;
}

export interface PipelineTriggerResult {
  pipeline: PipelineRun;
  replayed: boolean;
}

export interface PipelineCancellationResult {
  pipeline: PipelineRun;
  replayed: boolean;
}

export type CallbackTarget = "pipeline" | "stage" | "job";

export interface PipelineCallbackRequest {
  event_id: string;
  target?: CallbackTarget;
  status: PipelineStatus;
  stage_name?: string;
  job_name?: string;
  message?: string;
}

export interface PipelineCallbackResult {
  pipeline: PipelineRun;
  duplicate: boolean;
}

export type ProviderKind =
  | "local"
  | "learning_ci"
  | "jenkins"
  | "gitlab"
  | "bk_ci";

export interface ProviderRuntimeStatus {
  mode: "local_lab" | "ci_lab_local" | "self_hosted_lab";
  network_providers_allowed: boolean;
  target_scope: "loopback_only" | "internal_container";
  external_public_mode_supported: false;
}

export interface ProviderConnectionCreate {
  name: string;
  kind: ProviderKind;
  base_url?: string | null;
  definition_ref: string;
  config: Record<string, string>;
  secret_env_var?: string | null;
  enabled?: boolean;
}

export interface ProviderConnectionPatch {
  name?: string;
  base_url?: string | null;
  definition_ref?: string;
  config?: Record<string, string>;
  secret_env_var?: string | null;
  enabled?: boolean;
  version: number;
}

export interface ProviderConnection {
  id: string;
  name: string;
  kind: ProviderKind;
  base_url: string | null;
  definition_ref: string;
  config: Record<string, string>;
  secret_env_var: string | null;
  secret_configured: boolean;
  enabled: boolean;
  version: number;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface ProviderTriggerRequest {
  ref?: string | null;
  variables?: Record<string, string>;
  correlation_id?: string | null;
}

export interface ProviderRun {
  id: string;
  connection_id: string;
  external_id: string;
  status: string;
  raw_status: string;
  web_url: string | null;
  message: string | null;
  metadata: Record<string, unknown>;
  correlation_id: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface ProviderTestResult {
  ready: boolean;
  network_probe_performed: boolean;
  message: string;
}

export type AutomationTaskStatus =
  | "queued"
  | "running"
  | "retry_wait"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "dead_letter";

export interface AutomationTaskEnqueue {
  task_type: string;
  payload?: Record<string, unknown>;
  queue?: string;
  priority?: number;
  max_attempts?: number;
  idempotency_key?: string | null;
  available_at?: IsoDateTime | null;
}

export interface AutomationTask {
  id: string;
  task_type: string;
  payload: Record<string, unknown>;
  queue: string;
  priority: number;
  status: AutomationTaskStatus;
  idempotency_key: string | null;
  source_schedule_id: string | null;
  attempts: number;
  max_attempts: number;
  available_at: IsoDateTime;
  lease_owner: string | null;
  lease_expires_at: IsoDateTime | null;
  heartbeat_at: IsoDateTime | null;
  cancel_requested: boolean;
  result: Record<string, unknown> | null;
  error_code: string | null;
  created_at: IsoDateTime;
  started_at: IsoDateTime | null;
  finished_at: IsoDateTime | null;
}

export interface DeviceCreate {
  name: string;
  agent_id: string;
  kind?: string;
  platform?: string;
  capabilities?: string[];
}

export interface DevicePatch {
  name?: string;
  kind?: string;
  platform?: string;
  capabilities?: string[];
  enabled?: boolean;
  maintenance?: boolean;
  version: number;
}

export interface DeviceAcquire {
  task_id: string;
  owner: string;
  task_lease_token: string;
  required_capabilities?: string[];
  lease_seconds?: number;
}

export interface DeviceLeaseAction {
  owner: string;
  lease_token: string;
}

export interface DeviceLeaseRenew extends DeviceLeaseAction {
  task_lease_token: string;
  lease_seconds?: number;
}

export interface Device {
  id: string;
  name: string;
  kind: string;
  platform: string;
  capabilities: string[];
  enabled: boolean;
  status: string;
  last_heartbeat_at: IsoDateTime | null;
  active_lease_id: string | null;
  version: number;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface DeviceLease {
  id: string;
  device_id: string;
  task_id: string;
  owner: string;
  status: "active" | "released" | "expired";
  acquired_at: IsoDateTime;
  expires_at: IsoDateTime;
  released_at: IsoDateTime | null;
  version: number;
}

export interface ClaimedDevice {
  device: Device;
  lease: DeviceLease;
  lease_token: string;
}

export type ScheduleMisfirePolicy = "fire_once" | "catch_up_limited" | "skip";
export type ScheduleOverlapPolicy = "allow" | "forbid" | "replace";

export interface ScheduleCreate {
  name: string;
  task_type: string;
  payload?: Record<string, unknown>;
  queue?: string;
  priority?: number;
  max_attempts?: number;
  cron: string;
  timezone?: string;
  misfire_policy?: ScheduleMisfirePolicy;
  overlap_policy?: ScheduleOverlapPolicy;
  misfire_grace_seconds?: number;
  catch_up_limit?: number;
  enabled?: boolean;
}

export interface SchedulePatch {
  name?: string;
  payload?: Record<string, unknown>;
  queue?: string;
  priority?: number;
  max_attempts?: number;
  cron?: string;
  timezone?: string;
  misfire_policy?: ScheduleMisfirePolicy;
  overlap_policy?: ScheduleOverlapPolicy;
  misfire_grace_seconds?: number;
  catch_up_limit?: number;
  enabled?: boolean;
  version: number;
}

export interface AutomationSchedule {
  id: string;
  name: string;
  task_type: string;
  payload: Record<string, unknown>;
  queue: string;
  priority: number;
  max_attempts: number;
  cron: string;
  timezone: string;
  misfire_policy: ScheduleMisfirePolicy;
  overlap_policy: ScheduleOverlapPolicy;
  misfire_grace_seconds: number;
  catch_up_limit: number;
  enabled: boolean;
  next_run_at: IsoDateTime | null;
  last_run_at: IsoDateTime | null;
  version: number;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

export interface ScheduleFire {
  id: string;
  schedule_id: string;
  scheduled_for: IsoDateTime;
  status: string;
  task_id: string | null;
  created_at: IsoDateTime;
}
