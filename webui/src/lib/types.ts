/**
 * TypeScript mirrors of the 0.3.0 server DTOs (spec §2.2, §4.3, §5.4).
 *
 * Three rules keep this file honest:
 *  - Every console response is an `Envelope`: `{ data, server_time_ms, deadline_ms }`.
 *    `data` is the DTO below; nothing here describes the transport.
 *  - `server_time_ms` is the server clock. `device_time_ms` is a timestamp the
 *    device itself reported. The two are never mixed (spec C5).
 *  - Optional fields only appear where the server marks them optional, so the
 *    compiler catches a DTO drift instead of a UI silently rendering `undefined`.
 */

// ------------------------------------------------------------------ transport ---

export type ApiErrorCode =
  | "AUTH"
  | "GONE"
  | "BAD_REQUEST"
  | "UNSUPPORTED"
  | "TIMEOUT"
  | "RATE"
  | "INTERNAL";

/** Error body of the 0.3.0 API (spec §5.5). `detail` is legacy-FastAPI tolerance. */
export interface ApiErrorBody {
  code?: ApiErrorCode;
  message?: string;
  retryable?: boolean;
  detail?: unknown;
}

/** One console response. `deadline_ms` is the server budget for the operation. */
export interface Envelope<T> {
  data: T;
  server_time_ms: number;
  deadline_ms?: number | null;
}

// ------------------------------------------------------------------- presence ---

export type PresenceState = "online" | "degraded" | "offline" | "uninstalling" | "uninstalled";
export type TransportKind = "ws" | "poll" | "none";

/**
 * What the device can actually do right now, decided once by the server
 * (`presence.py`). The console gates controls on these flags and never derives
 * its own answer from `state` or from timestamps.
 */
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
  /** Server clock: when the agent last reported this health block. */
  at_ms: number;
}

export interface Presence {
  state: PresenceState;
  transport: TransportKind;
  reason: string;
  /** Server clock: last contact recorded by the server. */
  last_seen_ms: number;
  /** Server-computed age of `last_seen_ms`, in ms. The console only renders it. */
  age_ms: number | null;
  channel_since_ms: number | null;
  capabilities: Capabilities;
  capture: CaptureHealth | null;
  /** Non-empty when the agent cannot auto-upgrade; null when it can. */
  self_update_blocked: string | null;
}

// -------------------------------------------------------------------- devices ---

export interface Device {
  uid: string;
  name: string;
  hostname: string;
  os_name: string;
  os_version: string;
  arch: string;
  agent_version: string;
  /** Protocol version the agent speaks (0.3.0 adds `v3`). */
  protocol?: string;
  /** Agent boot identifier; changes on every service restart (A2). */
  boot_id?: string;
  /** Server clock: enrolment time. */
  enrolled_at_ms: number;
  /** Server clock: last contact. */
  last_seen_ms: number;
  note: string;
  presence: Presence;
  sysinfo: Record<string, unknown>;
  /** Effective settings for this device (global values + per-device overrides). */
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

export interface DevicePatch {
  name?: string;
  note?: string;
  settings?: Record<string, unknown>;
}

// ------------------------------------------------------------------- commands ---

export type CommandStatus = "pending" | "dispatched" | "ok" | "error" | "timeout" | "cancelled";

export interface Command {
  id: string;
  type: string;
  status: CommandStatus;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  error_code?: string | null;
  attempts?: number;
  created_at_ms: number;
  dispatched_at_ms: number | null;
  finished_at_ms: number | null;
}

export interface CommandList {
  commands: Command[];
  server_time_ms: number;
}

export interface UninstallAccepted {
  command_id: string;
  status: CommandStatus;
  message: string;
}

// --------------------------------------------------------------------- frames ---

export interface Frame {
  id: number;
  /** Capture time reported by the device (spec C5 naming). */
  device_time_ms: number;
  /** 0.2.x alias kept while the media endpoints migrate; read via `frameTimeMs`. */
  ts_ms?: number;
  width: number;
  height: number;
  size_bytes: number;
  has_thumb: boolean;
  kind: string;
}

export interface LatestFrame {
  frame: Frame | null;
  server_time_ms: number;
}

export interface TimelineBucket {
  device_time_ms: number;
  /** 0.2.x alias kept while the media endpoints migrate; read via `frameTimeMs`. */
  ts_ms?: number;
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

// ---------------------------------------------------------------------- files ---

export interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  /** Device-reported mtime (spec C5 naming). */
  mtime_ms: number;
  hidden: boolean;
  drive: string;
  /** Drive only: free bytes. Absent for plain entries. */
  free?: number | null;
  /** Drive only: filesystem label. Absent for plain entries. */
  filesystem?: string | null;
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

/** One chunk of a chunked download (`GET /files/raw`). */
export interface FileChunk {
  blob: Blob;
  size: number;
  offset: number;
  eof: boolean;
}

// -------------------------------------------------------------------- control ---

export interface ControlGrant {
  token: string;
  /** Server clock: when the grant stops being valid. */
  expires_at_ms: number;
  fps: number;
  quality: number;
  ws_path: string;
  server_time_ms: number;
}

// ---------------------------------------------------------------------- auth ---

export interface LoginResult {
  token: string;
  expires_at_ms: number;
  version: string;
}

export interface SessionInfo {
  authenticated: boolean;
  totp_enabled: boolean;
  version: string;
  server_time_ms: number;
  console_host: string;
  base_urls: string[];
}

export interface TotpSetup {
  secret: string;
  uri: string;
}

export interface OkResult {
  ok: boolean;
  message: string;
}

// ------------------------------------------------------------------- settings ---

export type SettingKind = "bool" | "int" | "str" | "choice" | "optional_str";

export interface SettingSpec {
  kind: SettingKind;
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

// ---------------------------------------------------------------------- audit ---

export interface AuditEntry {
  id: number;
  /** Server clock. */
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

// -------------------------------------------------------------------- updates ---

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

// --------------------------------------------------------------------- system ---

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

// ------------------------------------------------------------- console socket ---

/** Server→console event envelope (unchanged by the 0.3.0 rewrite). */
export interface LiveEvent {
  type: string;
  /** Server clock: the moment the server emitted the event. */
  ts_ms: number;
  device_uid: string;
  data: Record<string, unknown>;
}