// Mirrors backend/app/schemas/portal.py (the portal returns exactly these fields).

export type InvitationStatus = 'pending' | 'submitted' | 'expired' | 'revoked';

export interface PortalMe {
  client: { name: string; company: string | null };
  invitation: {
    status: InvitationStatus;
    service_type: string | null;
    template_name: string | null;
    deal_name: string | null;
    expires_at: string;
    submitted_at: string | null;
  };
  next_step: 'onboarding' | 'project' | 'contact_agency';
}

export interface JsonSchema {
  type?: string;
  title?: string;
  enum?: string[];
  format?: string;
  default?: unknown;
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  maxItems?: number;
  items?: JsonSchema;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  anyOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
}

export interface OnboardingFormResponse {
  status: InvitationStatus;
  service_type: string | null;
  template_name: string | null;
  submitted_at: string | null;
  form_schema: JsonSchema;
  prefill: Record<string, unknown>;
}

export interface PortalTask {
  title: string;
  description: string | null;
  status: 'todo' | 'in_progress' | 'done';
  due_date: string | null;
  completed_at: string | null;
}

export interface PortalProject {
  id: string;
  name: string;
  status: 'active' | 'on_hold' | 'completed' | 'cancelled';
  description: string | null;
  start_date: string | null;
  due_date: string | null;
  created_at: string;
  progress: { total_tasks: number; completed_tasks: number; open_tasks: number; percent: number };
  milestones: { name: string; position: number; status: 'pending' | 'in_progress' | 'done'; due_date: string | null;
                tasks: PortalTask[] }[];
  unassigned_tasks: PortalTask[];
  activity: { type: string; summary: string; created_at: string }[];
}
