/** Types mirroring `server/observatory/schemas.py`. */

export type PresenceState = "online" | "degraded" | "offline" | "uninstalling" | "uninstalled";
export type TransportKind = "ws" | "poll" | "none";
export type CommandStatus = "pending" | "dispatched" | "ok" | "error" | "timeout" | "cancelled";

export interface Capabilities {
  control: boolean;
  files: boolean;
  screenshot: boolean;
  timeline: boolean;
  update: boolean;
  uninstall: boolean;
}

export interface CaptureHealth {
  state: string;
  reason: string;
  sessions: number | null;
  workers: number[];
  at_ms: number;
}

export interface Presence {
  state: PresenceState;
  /*
   * Transport and capabilities are optional on purpose: they were added to the
   * payload after the first consoles shipped, so an older server may omit them.
   * Read them through `@/lib/presence` helpers, which fall back to the server's
   * own state→capabilities mapping instead of crashing on `undefined`.
   */
  transport?: TransportKind;
  reason: string;
  last_seen_ms: number;
  age_ms: number | null;
  channel_since_ms: number | null;
  capabilities?: Capabilities;
  capture?: CaptureHealth | null;
  /**
   * Non-empty when this agent is too old (or its updater is broken) for the
   * automatic upgrade path: the console must tell the operator to rerun the
   * install script rather than wait forever. `null`/absent means "normal" and
   * nothing should be rendered. Optional because the server field is newer
   * than this console build.
   */
  self_update_blocked?: string | null;
}

export interface Device {
  uid: string;
  name: string;
  hostname: string;
  os_name: string;
  os_version: string;
  arch: string;
  agent_version: string;
  enrolled_at_ms: number;
  last_seen_ms: number;
  note: string;
  presence: Presence;
  sysinfo: Record<string, unknown>;
  settings: Record<string, unknown>;
  latest_frame_id: number | null;
  server_time_ms: number;
}

export interface DeviceList {
  devices: Device[];
  server_time_ms: number;
  server_version: string;
}

export interface DeviceEnvelope {
  device: Device;
  server_time_ms: number;
}

export interface Command {
  id: string;
  type: string;
  status: CommandStatus;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  created_at_ms: number;
  dispatched_at_ms: number | null;
  finished_at_ms: number | null;
}

export interface Frame {
  id: number;
  ts_ms: number;
  width: number;
  height: number;
  size_bytes: number;
  has_thumb: boolean;
  kind: string;
}

export interface TimelineBucket {
  ts_ms: number;
  frame_id: number | null;
  frame_count: number;
  session: number;
}

export interface Timeline {
  from_ms: number;
  to_ms: number;
  bucket_ms: number;
  buckets: TimelineBucket[];
  total_frames: number;
  server_time_ms: number;
}

export interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  mtime_ms: number;
  hidden: boolean;
  drive: string;
  free?: number;
  filesystem?: string;
}

export interface FileList {
  path: string;
  parent: string | null;
  entries: FileEntry[];
  drives: FileEntry[];
  truncated: boolean;
  server_time_ms: number;
}

export interface FileOpResult {
  ok: boolean;
  op: string;
  data: Record<string, unknown>;
  error: string | null;
  elapsed_ms: number;
}

export type FileOpName =
  | "list"
  | "stat"
  | "mkdir"
  | "delete"
  | "rename"
  | "copy"
  | "move"
  | "search"
  | "drives"
  | "read"
  | "write"
  | "hash";

export interface FileOpRequest {
  op: FileOpName;
  path?: string;
  target?: string | null;
  recursive?: boolean;
  pattern?: string | null;
  offset?: number;
  length?: number;
}

export interface ControlGrant {
  token: string;
  expires_at_ms: number;
  fps: number;
  quality: number;
  ws_path: string;
  server_time_ms: number;
}

export interface SettingSpec {
  kind: "bool" | "int" | "str" | "choice" | "optional_str";
  default: unknown;
  min: number | null;
  max: number | null;
  choices: string[];
  per_device: boolean;
  secret: boolean;
  description: string;
}

export interface SettingsPayload {
  values: Record<string, unknown>;
  specs: Record<string, SettingSpec>;
  server_version: string;
  server_time_ms: number;
}

export interface AuditEntry {
  id: number;
  ts_ms: number;
  actor: string;
  action: string;
  device_uid: string;
  device_name: string;
  ip: string;
  detail: Record<string, unknown>;
}

export interface AuditList {
  entries: AuditEntry[];
  total: number;
  server_time_ms: number;
}

export interface Package {
  version: string;
  filename: string;
  size_bytes: number;
  sha256: string;
  notes: string;
  created_at_ms: number;
  download_url: string;
}

export interface UpdateStatus {
  server_version: string;
  latest_agent_version: string | null;
  outdated: Array<{ uid: string; name: string; version: string }>;
  packages: Package[];
  server_time_ms: number;
}

export interface SystemStatus {
  version: string;
  uptime_s: number;
  server_time_ms: number;
  devices: number;
  channels: { ws: number; poll: number };
  ws_clients: number;
  pending_commands: number;
  control_sessions: number;
  storage: {
    frames_bytes: number;
    thumbs_bytes: number;
    db_bytes: number;
    disk_total: number;
    disk_free: number;
  };
  base_urls: string[];
  console_host: string;
  install: InstallInfo;
}

export interface InstallArtifact {
  kind: "cmd" | "ps1";
  label: string;
  filename: string;
  url: string;
  hint: string;
}

export interface InstallInfo {
  enabled: boolean;
  enroll_code: string;
  version: string;
  console_host: string;
  base_urls: string[];
  script_url: string;
  cmd_url: string;
  artifacts: InstallArtifact[];
}

export interface SessionInfo {
  authenticated: boolean;
  totp_enabled: boolean;
  version: string;
  server_time_ms: number;
  console_host: string;
  base_urls: string[];
}

export interface LiveEvent {
  type: string;
  ts_ms: number;
  device_uid: string;
  data: Record<string, unknown>;
}