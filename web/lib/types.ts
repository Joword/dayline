// Shapes returned by the Dayline API (backend/app/api/*). Kept minimal: only what the UI reads.

export type InputType = "text" | "string" | "date" | "date_range" | "enum" | "int" | "bool" | "json";

export interface InputSpec {
  name: string;
  type: InputType;
  required: boolean;
  description: string;
  default: unknown;
  choices: string[] | null;
  max_chars: number | null;
}

export interface SkillInfo {
  name: string;
  version: string;
  description: string;
  layer: "builtin" | "team" | "user";
  roles: ("member" | "manager")[];
  model: string;
  langs: string[];
  commands: string[];
  intents: string[];
  inputs: InputSpec[];
  render: string[];
  template: string;
}

export interface Usage {
  input_tokens: number;
  output_tokens: number;
}

export interface SkillRunResult {
  run_id: string;
  skill: string;
  version: string;
  lang: string;
  status: "ok" | "invalid";
  output: Record<string, unknown> | string | null;
  markdown: string;
  errors: string[];
  warnings: string[];
  notice: string | null;
  failover: boolean;
  degraded: boolean;
  endpoint: string | null;
  model: string | null;
  usage: Usage;
  cost_usd: number | null;
  latency_ms: number;
}

export interface UserBrief {
  id: string;
  name: string;
  org_unit: string | null;
}

export interface ReportItem {
  kind: "done" | "progress" | "blocker" | "plan";
  text: string;
  source?: string;
  cross_team?: boolean;
}

export interface ReportView {
  id: string;
  user: UserBrief;
  date: string;
  kind: string;
  version: number;
  status: "draft" | "confirmed" | "sent";
  lang: string;
  access: "full" | "dependencies" | "none";
  own_items: ReportItem[];
  team_summary: Record<string, unknown> | null;
  rendered_md: string;
  confirmed_at: string | null;
  sync_status: "none" | "pending" | "synced" | "failed";
}

export interface TeamSnapshot {
  manager: UserBrief;
  date: string;
  members: { user: UserBrief; report: ReportView | null }[];
}

export interface Me {
  user: { id: string; name: string; org_unit: string | null; system_roles: string[]; lang: string | null };
  manager: { id: string; name: string } | null;
  direct_reports: UserBrief[];
  consents: { scope: "git" | "calendar"; grantee_id: string; granted_at: string }[];
  tokens: { id: string; label: string; created_at: string; last_used_at: string | null }[];
}

export interface EscalationEntry {
  escalation_id: string;
  text: string;
  status: EscalationStatus;
  holder: string;
  origin: string;
  need_from?: string | null;
}

export interface StoredReport {
  id: string;
  date: string;
  version: number;
  status: string;
  kind: string;
  team_summary?: {
    escalation_candidates?: { text: string; reason?: string; need_from?: string | null }[];
    pending_escalations?: EscalationEntry[];
    raised_escalations?: EscalationEntry[];
  } | null;
}

export type EscalationStatus = "open" | "escalated" | "in_progress" | "resolved";

export interface Escalation {
  id: string;
  text: string;
  need_from: string | null;
  severity: string | null;
  origin: { user_id: string; date: string; report_id: string | null; report_version: number | null };
  created_by: string;
  current_holder_id: string;
  status: EscalationStatus;
  participants: string[];
  chain: { at: string; actor_id: string; action: string; from_holder: string | null; to_holder: string | null; note: string | null }[];
  resolution: string | null;
  updated_at: string;
}

export interface Notification {
  id: string;
  kind: string;
  title: string;
  body: string;
  link: string | null;
  created_at: string;
  read_at: string | null;
}

export interface Integration {
  id: string;
  type: "git" | "calendar";
  provider: string;
  label: string;
  status: "active" | "error";
  mode_used: string | null;
  last_ok_at: string | null;
  last_error: string | null;
}

export interface GitSummary {
  repos: { repo: string; commit_count: number; prs: { number: number; title: string; url: string; state: string }[] }[];
  totals: { commits: number; repos: number; prs: number };
}

export interface CalendarSummary {
  events: { title: string; start: string; end: string; minutes: number; attendees: number }[];
  count: number;
  total_minutes: number;
}

export interface Activity {
  date: string;
  git: GitSummary | null;
  calendar: CalendarSummary | null;
  unavailable: Record<string, string>;
  errors: Record<string, string>;
}
