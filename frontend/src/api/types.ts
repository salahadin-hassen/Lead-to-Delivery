// Response shapes of the FastAPI admin API (backend/app/routes/admin*.py).

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Progress {
  total_tasks: number;
  completed_tasks: number;
  open_tasks: number;
  percent: number;
}

export interface Activity {
  id: string;
  event_type: string;
  summary: string;
  actor_type: string;
  client_visible: boolean;
  created_at: string;
  client_id: string | null;
  project_id: string | null;
  client_name: string | null;
  project_name: string | null;
}

export interface DashboardCounts {
  total_clients: number;
  active_clients: number;
  archived_clients: number;
  pending_invitations: number;
  active_projects: number;
  completed_projects: number;
  open_tasks: number;
  completed_tasks: number;
  pending_jobs: number;
  running_jobs: number;
  dead_jobs: number;
}

export interface ProjectSummary {
  id: string;
  name: string;
  status: ProjectStatus;
  start_date?: string | null;
  due_date: string | null;
  created_at: string;
  client_id?: string;
  client_name?: string;
  client_company?: string | null;
  template_name?: string | null;
  progress: Progress;
}

export interface DeadJob {
  id: string;
  job_type: string;
  subject_type: string;
  subject_id: string;
  attempts: number;
  last_error_code: string | null;
  updated_at: string;
}

export interface Dashboard {
  counts: DashboardCounts;
  active_projects: ProjectSummary[];
  dead_jobs: DeadJob[];
  recent_activity: Activity[];
}

export type ClientStatus = 'active' | 'archived';

export interface Client {
  id: string;
  name: string;
  company: string | null;
  email: string | null;
  phone: string | null;
  status: ClientStatus;
  crm_source: string | null;
  created_at: string;
  project_count?: number;
  pending_invitations?: number;
}

export type InvitationStatus = 'pending' | 'submitted' | 'expired' | 'revoked';

export interface Invitation {
  id: string;
  status: InvitationStatus;
  service_type: string | null;
  deal_name: string | null;
  crm_source: string | null;
  crm_deal_id: string | null;
  deal_value_cents: number | null;
  currency: string | null;
  expires_at: string;
  submitted_at: string | null;
  created_at: string;
  access_link_expires_at: string | null;
  project_id: string | null;
}

export interface ClientDetail extends Client {
  crm_external_id: string | null;
  updated_at: string;
  invitations: Invitation[];
  projects: ProjectSummary[];
  activity: Activity[];
}

export interface AccessLink {
  access_url: string;
  expires_at: string;
  kind: 'onboarding' | 'project_access';
}

export type ProjectStatus = 'active' | 'on_hold' | 'completed' | 'cancelled';
export type TaskStatus = 'todo' | 'in_progress' | 'done';

export interface Task {
  id: string;
  milestone_id: string | null;
  title: string;
  status: TaskStatus;
  position: number;
  due_date: string | null;
  client_visible: boolean;
  assignee_name: string | null;
  assignee_email: string | null;
  completed_at: string | null;
  clickup_task_id: string | null;
  clickup_url: string | null;
  clickup_job_status: JobStatus | null;
  clickup_job_error: string | null;
  clickup_job_id: string | null;
}

export interface Milestone {
  id: string;
  name: string;
  position: number;
  status: string;
  due_date: string | null;
}

export interface ProjectDetail {
  id: string;
  name: string;
  status: ProjectStatus;
  start_date: string | null;
  due_date: string | null;
  sync_version: number;
  created_at: string;
  description: string | null;
  client: { id: string; name: string; company: string | null; email: string | null; status: ClientStatus };
  template: { key: string; name: string; version: number } | null;
  progress: Progress;
  milestones: Milestone[];
  tasks: Task[];
  integration_jobs: { job_type: string; status: JobStatus; n: number }[];
  activity: Activity[];
}

export type JobStatus = 'pending' | 'running' | 'succeeded' | 'dead' | 'cancelled';

export interface Job {
  id: string;
  job_type: string;
  subject_type: string;
  subject_id: string;
  status: JobStatus;
  attempts: number;
  max_attempts: number;
  next_run_at: string;
  last_error_code: string | null;
  created_at: string;
  completed_at: string | null;
  updated_at: string;
  project_id: string | null;
  project_name: string | null;
  task_title: string | null;
}

export interface Attempt {
  attempt_number: number;
  outcome: 'success' | 'retryable' | 'permanent' | 'unknown' | null;
  worker_id: string | null;
  started_at: string;
  finished_at: string | null;
  http_status: number | null;
  error_code: string | null;
  error_message: string | null;
  external_id: string | null;
  duration_ms: number | null;
}

export interface JobDetail extends Job {
  idempotency_key: string;
  locked_by: string | null;
  locked_until: string | null;
  last_error: string | null;
  reconciliation: { confirmed_after_attempt: number; confirmed_at: string; confirmed_by: string } | null;
  resolution: { resolved_as: string; after_attempt: number; resolved_at: string; resolved_by: string } | null;
  attempt_history: Attempt[];
  requires_reconciliation: boolean;
}

export interface IntegrationStatus {
  clickup: {
    implemented: true;
    credentials_present: boolean;
    configuration_valid: boolean;
    configuration_error: string | null;
    live_verification: 'verified_by_successful_job' | 'not_verified';
    last_success_at: string | null;
    jobs: Partial<Record<JobStatus, number>>;
    two_way_sync: boolean;
  };
  google_sheets: {
    implemented: true;
    service_account_file_configured: boolean;
    spreadsheet_id_configured: boolean;
    worksheet_configured: boolean;
    credentials_present: boolean;
    configuration_valid: boolean;
    configuration_error: string | null;
    live_verification: 'verified_by_successful_job' | 'not_verified';
    last_success_at: string | null;
    jobs: Partial<Record<JobStatus, number>>;
  };
  slack: {
    implemented: true;
    credentials_present: boolean;
    configuration_valid: boolean;
    configuration_error: string | null;
    live_verification: 'verified_by_successful_job' | 'not_verified';
    last_success_at: string | null;
    jobs: Partial<Record<JobStatus, number>>;
  };
}

export interface WebhookEvent {
  id: string;
  source: string;
  external_event_id: string;
  event_type: string;
  status: 'received' | 'processed' | 'rejected' | 'failed';
  error_code: string | null;
  duplicate_count: number;
  last_duplicate_at: string | null;
  processed_at: string | null;
  created_at: string;
  deal_id: string | null;
  service_type: string | null;
  invitation_id: string | null;
  client_id: string | null;
  client_name: string | null;
}
