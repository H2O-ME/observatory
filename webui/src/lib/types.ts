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
 *
 * Cross-checked against `server/observatory/console/{dto,devices,frames,files,
 * settings,audit,updates,backup,install,system}.py` on 2026-10 (0.3.0).
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
  /** Protocol version the agent currently speaks (2 = legacy, 3 = 0.3.0). */
  protocol: number;
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
  protocol: number;
  /** Agent boot identifier; changes on every service restart (A2). */
  boot_id: string | null;
  /** Server clock: enrolment time. */
  enrolled_at_ms: number;
  /** Server clock: last contact. */
  last_seen_ms: number;
  /** Server clock: when the uninstall command was accepted; null while installed. */
  uninstalled_at_ms: number | null;
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
  server_version: string;
  channels?: { ws: number; poll: number };
  server_time_ms: number;
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
  /** Journal id (string: it is a UUID, not a database row id). */
  id: string;
  type: string;
  /** Alias kept by the server (`kind`) for the 0.2.x page. */
  kind: string;
  status: CommandStatus;
  payload: Record<string, unknown>;
  result: unknown;
  error: string | null;
  /** Machine-readable failure reason (A6): GONE / UNSUPPORTED / TIMEOUT / ... */
  error_code: string | null;
  /** Server budget the command was dispatched with (§5.4). */
  deadline_ms: number;
  attempts: number;
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
  /** Server clock: when the server stored the frame (sort key for the grid). */
  ts_ms: number;
  /** Capture time reported by the device (spec C5 naming). */
  device_time_ms: number;
  width: number;
  height: number;
  size_bytes: number;
  has_thumb: boolean;
  kind: string;
  /** Interactive session the frame was captured in; 0 when unknown. */
  session: number;
}

export interface FrameList {
  frames: Frame[];
  total: number;
  limit: number;
  offset: number;
  server_time_ms: number;
}

export interface LatestFrame {
  frame: Frame | null;
  server_time_ms: number;
}

export interface TimelineBucket {
  /** Server clock: first frame in the bucket (the bucket's own timestamp). */
  ts_ms: number;
  /** Device-reported capture time; equal to `ts_ms` until media lands it separately. */
  device_time_ms: number;
  /** Frame id to use for the thumbnail (the first/best frame in the bucket). */
  frame_id: number | null;
  frame_count: number;
  session: number;
}

export interface Timeline {
  from_ms: number;
  to_ms: number;
  /** Server-chosen bucket width; the console never invents its own. */
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
  /** Time reported by the controlled machine (spec C5). */
  device_time_ms: number;
  hidden: boolean;
  /** Drive letter ("C:") for drive entries, "" for ordinary entries. */
  drive: string;
  /** Free bytes; the server sends 0 for ordinary entries, drives carry the value. */
  free: number;
  /** Filesystem label for drives ("NTFS"); "" otherwise. */
  filesystem: string;
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
  /**
   * Raw agent payload. For `list`/`drives` the console maps entries to
   * `FileEntry`; for `search` and `stat` this is the agent's own shape
   * (`mtime_ms`), so callers must normalise before rendering.
   */
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
  /** Total file size reported by the agent (`X-File-Size`). */
  size: number;
  /** Offset of this chunk (`X-Chunk-Offset`). */
  offset: number;
  /** True when this chunk reaches the end of the file (`X-Eof`). */
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
  console_host: string;
  base_urls: string[];
  server_time_ms: number;
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
  /** Secret specs always report `null` here; the value never reaches a browser. */
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
  console_host?: string;
  base_urls?: string[];
  /** Present on PATCH: how many live agents received the new config. */
  pushed_to?: number;
  server_time_ms: number;
}

/** `GET /settings/effective/{uid}` — server truth for one device, secrets stripped. */
export interface EffectiveSettings {
  uid: string;
  /** Global defaults + stored values + this device's overrides, agent-safe view. */
  settings: Record<string, unknown>;
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
  /** Highest semver among uploaded packages (`PackageService.latest`). */
  latest_agent_version: string | null;
  outdated: Array<{ uid: string; name: string; version: string }>;
  packages: Package[];
  server_time_ms: number;
}

// --------------------------------------------------------------------- install ---

export interface InstallArtifact {
  kind: "cmd" | "ps1";
  label: string;
  filename: string;
  /** Served URL; embeds `<一次性令牌>` until the operator picks a token. */
  url: string;
  hint: string;
}

export interface InstallInfo {
  enabled: boolean;
  enroll_required: boolean;
  legacy_code_enabled: boolean;
  /** Unused, unexpired one-shot tokens currently in the database. */
  available_tokens: number;
  token_ttl_minutes: number;
  version: string;
  console_host: string;
  base_urls: string[];
  install_token_endpoint: string;
  script_url: string;
  cmd_url: string;
  artifacts: InstallArtifact[];
  /** Only `GET /install` adds the operator note. */
  note?: string;
}

export interface InstallToken {
  id: number;
  created_by: string;
  created_at_ms: number;
  expires_at_ms: number;
  used_at_ms: number | null;
  used_device_id: number | null;
  max_uses: number;
  expired: boolean;
  used: boolean;
}

export interface InstallTokenCreated {
  id: number;
  /** Plaintext token. Returned exactly once by the server; show it and let the
   *  operator copy it — it can never be read again. */
  token: string;
  expires_at_ms: number;
  max_uses: number;
  install_cmd_url: string;
  install_ps1_url: string;
}

// ---------------------------------------------------------------------- backup ---

export interface BackupSummary {
  /** Database rows per archive status (`待上传` / `已上传` / `失败` / `已删除`). */
  statuses: Record<string, number>;
  pending: number;
  failed: number;
  uploaded: number;
  deleted: number;
  /** Process counters since the server started. */
  enqueued: number;
  verify_failed: number;
  queue_full: number;
  remote_deleted: number;
  worker_errors: number;
  queue_size: number;
  worker_running: boolean;
  last_error: string;
}

export interface BackupFailure {
  id: number;
  device_id: number;
  frame_id: string | null;
  day: string;
  remote_path: string;
  status: string;
  attempts: number;
  created_at_ms: number;
  uploaded_at_ms: number | null;
  error: string;
}

export interface BackupOverview {
  configured: boolean;
  archive_enabled: boolean;
  repo: string;
  branch: string;
  summary: BackupSummary;
  recent_failures: BackupFailure[];
  statuses: string[];
  server_time_ms: number;
}

export interface BackupEntry {
  id: number;
  device_id: number;
  device_uid: string;
  device_name: string;
  frame_id: string | null;
  /** UTC day the archive belongs to (`YYYY-MM-DD`). */
  day: string;
  remote_path: string;
  sha256: string;
  size_bytes: number;
  status: string;
  attempts: number;
  created_at_ms: number;
  uploaded_at_ms: number | null;
  error: string;
  /** True when a local frame row still exists (download is possible). */
  local_available: boolean;
  server_time_ms: number;
}

export interface BackupList {
  entries: BackupEntry[];
  total: number;
  limit: number;
  offset: number;
  statuses: string[];
  server_time_ms: number;
}

export interface BackupFrameInfo {
  id: number;
  ts_ms: number;
  device_time_ms: number;
  width: number;
  height: number;
  size_bytes: number;
  sha256: string;
}

export interface BackupDetailEntry extends Omit<BackupEntry, "device_uid" | "device_name" | "local_available"> {
  device_uid: string;
  device_name: string;
  local_available: boolean;
  frame?: BackupFrameInfo;
}

export interface BackupDetail {
  entry: BackupDetailEntry;
  server_time_ms: number;
}

// --------------------------------------------------------------------- system ---

export interface SystemStatus {
  version: string;
  uptime_s: number;
  devices: number;
  frames: number;
  commands: number;
  pending_commands: number;
  /** Carrier → session count (`link/registry.counts()`: ws / poll / …). */
  channels: Record<string, number>;
  control_sessions: number;
  /** Server process storage footprint; all sizes are bytes. */
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
  server_time_ms: number;
}

/** `GET /api/v1/health` — public liveness payload (no auth). */
export interface HealthInfo {
  ok: boolean;
  product: string;
  version: string;
  server_time_ms: number;
  uptime_s: number;
  /** Carrier → session count, same source as `SystemStatus.channels`. */
  carriers: Record<string, number>;
}

// ------------------------------------------------------------- console socket ---

/** Server→console event envelope (`console/events.py::build`). */
export interface LiveEvent {
  type: string;
  /** Server clock: the moment the server emitted the event. */
  ts_ms: number;
  /** Also present, kept for clients that only know the new name. */
  server_time_ms?: number;
  device_uid?: string;
  data?: Record<string, unknown>;
}