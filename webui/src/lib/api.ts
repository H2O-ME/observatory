/**
 * The console's single API client (spec §4.3, §5.4, §5.5).
 *
 * Contract:
 *  - Every console response is an `Envelope`: `{ data, server_time_ms, deadline_ms }`.
 *    The envelope is unwrapped here and **every** `server_time_ms` calibrates the
 *    server clock (`syncClock`), so no page has to think about time sync (A12).
 *  - Timeouts are derived from the server's `deadline_ms`: the client waits
 *    `deadline + 5s` (§5.4). There is no fixed 20 s budget anywhere. When the
 *    server has not told us a budget yet, the default is 30 s — exactly the
 *    largest legal server budget (25 s) plus the 5 s grace, so a slow document
 *    endpoint can never be killed by a client that guessed smaller (A8).
 *  - Failures are one type: `ApiError { status, code, message, retryable }`
 *    (§5.5). React Query retries based on `retryable` alone.
 *  - 401 is handled once for the whole application: the handler registered by
 *    the auth store clears the token and the query cache.
 *
 * Uploads are a separate client (`uploadPackage`): a package is not a command,
 * so it gets a long budget, real progress and cancellation instead of a command
 * deadline (A8).
 */

import { syncClock } from "@/lib/clock";
import type {
  ApiErrorBody,
  ApiErrorCode,
  AuditList,
  BackupDetail,
  BackupList,
  BackupOverview,
  Command,
  CommandList,
  ControlGrant,
  DeviceEnvelope,
  DeviceList,
  DevicePatch,
  Envelope,
  FileChunk,
  FileList,
  FileOpRequest,
  FileOpResult,
  FrameList,
  InstallInfo,
  InstallToken,
  InstallTokenCreated,
  LatestFrame,
  LiveEvent,
  LoginResult,
  OkResult,
  Package,
  SessionInfo,
  SettingsPayload,
  EffectiveSettings,
  SystemStatus,
  HealthInfo,
  Timeline,
  TotpSetup,
  UninstallAccepted,
  UpdateStatus,
} from "@/lib/types";

const TOKEN_KEY = "observatory.token";
const API_ROOT = "/api/v1";

/**
 * Largest server budget (spec §5.4: `min(命令类型上限, 25s)`) plus the client
 * grace. Used only until the server has sent us its own `deadline_ms`.
 */
const DEFAULT_TIMEOUT_MS = 30_000;
/** Client grace on top of the server deadline (spec §5.4). */
const CLIENT_GRACE_MS = 5_000;
/** A package upload is not a command and has no command deadline. */
const UPLOAD_TIMEOUT_MS = 15 * 60_000;
/** Below this a "deadline" carries no information and is ignored. */
const MIN_SANE_DEADLINE_MS = 100;

// --------------------------------------------------------------------- errors ---

export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;
  readonly retryable: boolean;

  constructor(status: number, code: ApiErrorCode, message: string, retryable: boolean) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

function isApiErrorCode(value: unknown): value is ApiErrorCode {
  return (
    value === "AUTH" ||
    value === "GONE" ||
    value === "BAD_REQUEST" ||
    value === "UNSUPPORTED" ||
    value === "TIMEOUT" ||
    value === "RATE" ||
    value === "INTERNAL"
  );
}

function codeFromStatus(status: number): ApiErrorCode {
  if (status === 401 || status === 403) return "AUTH";
  if (status === 410) return "GONE";
  if (status === 422) return "BAD_REQUEST";
  if (status === 429) return "RATE";
  if (status === 504) return "TIMEOUT";
  return "INTERNAL";
}

function validationMessage(detail: unknown): string | null {
  if (!Array.isArray(detail)) return null;
  const first = detail[0] as { msg?: unknown } | undefined;
  return first && typeof first.msg === "string" ? first.msg : null;
}

function errorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") {
    if (typeof payload === "string" && payload.trim()) {
      // F43: a reverse-proxy 502/504 returns an HTML error page; showing raw
      // markup to the operator is noise.
      const text = payload.trim();
      return text.startsWith("<") ? fallback : text.slice(0, 240);
    }
    return fallback;
  }
  const record = payload as ApiErrorBody & { detail?: unknown };
  if (typeof record.message === "string" && record.message.trim()) return record.message;
  if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
  return validationMessage(record.detail) ?? fallback;
}

function toApiError(status: number, payload: unknown): ApiError {
  const record = payload && typeof payload === "object" ? (payload as ApiErrorBody) : null;
  const code = isApiErrorCode(record?.code) ? record.code : codeFromStatus(status);
  const retryable =
    typeof record?.retryable === "boolean"
      ? record.retryable
      : status >= 500 || status === 429 || status === 504;
  return new ApiError(status, code, errorMessage(payload, `请求失败（HTTP ${status}）`), retryable);
}

// ---------------------------------------------------------------------- token ---

export function readToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function writeToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage disabled: the session then lives in the HttpOnly cookie only */
  }
}

// ------------------------------------------------------------------ 401 handling --

let unauthorizedHandler: ((requestEpoch: number) => void) | null = null;
/**
 * F40: request epoch. The store bumps it on login/logout; a 401 stamps the
 * epoch it happened in so the handler can tell "401 from the current session"
 * apart from "401 from a request already in flight before a fresh login"
 * (observed as "登录后闪回登录页"). api.ts owns the counter — importing the
 * store here would be circular.
 */
let sessionEpoch = 0;
let unauthorizedEpoch = -1;

export function currentSessionEpoch(): number {
  return sessionEpoch;
}

/** Called by the auth store on login (and by its 401 handler on logout). */
export function bumpSessionEpoch(): void {
  sessionEpoch += 1;
}

/** Registered once by `store/auth.ts`; api.ts must not import the store. */
export function setUnauthorizedHandler(handler: ((requestEpoch: number) => void) | null): void {
  unauthorizedHandler = handler;
}

function notifyUnauthorized(requestEpoch: number): void {
  unauthorizedEpoch = requestEpoch;
  unauthorizedHandler?.(requestEpoch);
}

/**
 * True when this 401 belongs to a superseded request (pre-login in-flight or
 * a stale retry) and must not tear the current session down.
 */
export function isStaleUnauthorized(): boolean {
  return unauthorizedEpoch !== sessionEpoch;
}

// -------------------------------------------------------------- deadline budget ---

const deadlineHints = new Map<string, number>();

function rememberDeadline(opKey: string | undefined, deadlineMs: number | null | undefined): void {
  if (!opKey || typeof deadlineMs !== "number" || !Number.isFinite(deadlineMs)) return;
  if (deadlineMs < MIN_SANE_DEADLINE_MS) return;
  deadlineHints.set(opKey, deadlineMs);
}

/** Client timeout for an operation: server budget + grace, else the default. */
export function timeoutFor(opKey: string | undefined): number {
  if (!opKey) return DEFAULT_TIMEOUT_MS;
  const deadline = deadlineHints.get(opKey);
  return deadline === undefined ? DEFAULT_TIMEOUT_MS : deadline + CLIENT_GRACE_MS;
}

// -------------------------------------------------------------------- request ---

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  json?: unknown;
  body?: BodyInit | null;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  opKey?: string;
  timeoutMs?: number;
}

function requestHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = readToken();
  const base: Record<string, string> = { Accept: "application/json" };
  if (token) base.Authorization = `Bearer ${token}`;
  return extra ? { ...base, ...extra } : base;
}

function timeoutError(timeoutMs: number): ApiError {
  const budget = Math.max(0, Math.round((timeoutMs - CLIENT_GRACE_MS) / 1000));
  return new ApiError(
    0,
    "TIMEOUT",
    `等待服务端响应超时（服务端预算 ${budget}s + ${CLIENT_GRACE_MS / 1000}s 宽限）`,
    true,
  );
}

function networkError(error: unknown): ApiError {
  const message = error instanceof Error ? error.message : "网络请求失败";
  return new ApiError(0, "INTERNAL", message, true);
}

/**
 * Unwrap the 0.3.0 envelope and calibrate the clock from it (A12).
 *
 * While the server endpoints migrate, a DTO may still arrive unwrapped; its
 * top-level `server_time_ms` is then used for calibration and as its `deadline`
 * hint is absent. This tolerance disappears once every endpoint is enveloped.
 */
function unwrap<T>(payload: unknown, opKey: string | undefined): T {
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    if (typeof record.server_time_ms === "number") syncClock(record.server_time_ms);
    if ("data" in record) {
      const envelope = payload as Envelope<T>;
      rememberDeadline(opKey, envelope.deadline_ms);
      return envelope.data;
    }
  }
  return payload as T;
}

async function parseBody(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return response.json().catch(() => null);
  }
  return response.text().catch(() => "");
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const requestEpoch = sessionEpoch;
  const timeoutMs = options.timeoutMs ?? timeoutFor(options.opKey);
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const external = options.signal;
  const onExternalAbort = () => controller.abort();
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener("abort", onExternalAbort, { once: true });
  }

  const json = options.json;
  const headers = requestHeaders(
    json === undefined ? options.headers : { "Content-Type": "application/json", ...options.headers },
  );

  try {
    const response = await fetch(path, {
      method: options.method ?? "GET",
      headers,
      body: json === undefined ? options.body : JSON.stringify(json),
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
    });
    const payload = await parseBody(response);
    if (!response.ok) {
      if (response.status === 401) notifyUnauthorized(requestEpoch);
      throw toApiError(response.status, payload);
    }
    return unwrap<T>(payload, options.opKey);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (controller.signal.aborted) {
      if (external?.aborted) throw new ApiError(0, "INTERNAL", "请求已取消", false);
      throw timeoutError(timeoutMs);
    }
    throw networkError(error);
  } finally {
    window.clearTimeout(timer);
    external?.removeEventListener("abort", onExternalAbort);
  }
}

// --------------------------------------------------------------- binary client ---

interface BinaryResponse {
  blob: Blob;
  headers: Headers;
}

/**
 * Binary download (file chunks, frames). It uses the same deadline budget as
 * every other call — the 0.2.x client bypassed the timeout entirely here, which
 * let a stalled chunk hang the UI forever.
 */
async function requestBinary(
  path: string,
  options: { opKey: string; signal?: AbortSignal; timeoutMs?: number },
): Promise<BinaryResponse> {
  const requestEpoch = sessionEpoch;
  const timeoutMs = options.timeoutMs ?? timeoutFor(options.opKey);
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const external = options.signal;
  const onExternalAbort = () => controller.abort();
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener("abort", onExternalAbort, { once: true });
  }

  try {
    const response = await fetch(path, {
      headers: requestHeaders(),
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
    });
    const serverTime = Number(response.headers.get("X-Server-Time-Ms") ?? "");
    if (Number.isFinite(serverTime) && serverTime > 0) syncClock(serverTime);
    const deadlineHeader = Number(response.headers.get("X-Deadline-Ms") ?? "");
    rememberDeadline(options.opKey, Number.isFinite(deadlineHeader) ? deadlineHeader : null);
    if (!response.ok) {
      const payload = await parseBody(response);
      if (response.status === 401) notifyUnauthorized(requestEpoch);
      throw toApiError(response.status, payload);
    }
    return { blob: await response.blob(), headers: response.headers };
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (controller.signal.aborted) {
      if (external?.aborted) throw new ApiError(0, "INTERNAL", "请求已取消", false);
      throw timeoutError(timeoutMs);
    }
    throw networkError(error);
  } finally {
    window.clearTimeout(timer);
    external?.removeEventListener("abort", onExternalAbort);
  }
}

// -------------------------------------------------------------------- uploads ---

interface UploadOptions {
  onProgress?: (loaded: number, total: number) => void;
  signal?: AbortSignal;
}

/**
 * Package upload client: long budget, real progress, cancellable.
 *
 * `XMLHttpRequest` (not `fetch`) because upload progress is still not available
 * on `fetch` in the browsers this console supports (Chromium 81 on the watch).
 */
function upload<T>(path: string, form: FormData, options: UploadOptions): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const requestEpoch = sessionEpoch;
    const xhr = new XMLHttpRequest();
    const token = readToken();
    xhr.open("POST", path, true);
    xhr.timeout = Math.max(UPLOAD_TIMEOUT_MS, timeoutFor("updates.upload"));
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.setRequestHeader("Accept", "application/json");

    const onAbort = () => xhr.abort();
    options.signal?.addEventListener("abort", onAbort, { once: true });

    xhr.upload.onprogress = (event) => {
      if (options.onProgress && event.lengthComputable) options.onProgress(event.loaded, event.total);
    };
    xhr.onerror = () => {
      options.signal?.removeEventListener("abort", onAbort);
      reject(new ApiError(0, "INTERNAL", "上传过程中网络中断", true));
    };
    xhr.ontimeout = () => {
      options.signal?.removeEventListener("abort", onAbort);
      reject(timeoutError(xhr.timeout));
    };
    xhr.onabort = () => {
      options.signal?.removeEventListener("abort", onAbort);
      reject(new ApiError(0, "INTERNAL", "上传已取消", false));
    };
    xhr.onload = () => {
      options.signal?.removeEventListener("abort", onAbort);
      let payload: unknown = null;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch {
        payload = xhr.responseText;
      }
      if (xhr.status === 401) notifyUnauthorized(requestEpoch);
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(toApiError(xhr.status, payload));
        return;
      }
      try {
        resolve(unwrap<T>(payload, "updates.upload"));
      } catch (error) {
        reject(error instanceof ApiError ? error : networkError(error));
      }
    };
    xhr.send(form);
  });
}

// ----------------------------------------------------------------- api client ---

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

const devicePath = (uid: string) => `${API_ROOT}/devices/${encodeURIComponent(uid)}`;

const rawFileUrl = (uid: string, path: string, offset: number, length: number) =>
  `${devicePath(uid)}/files/raw${query({ path, offset, length })}`;

export const api = {
  // ---- auth ----------------------------------------------------------
  login(password: string, totp?: string) {
    return request<LoginResult>(`${API_ROOT}/auth/login`, {
      method: "POST",
      json: { password, totp: totp || null },
      opKey: "auth.login",
    });
  },
  logout() {
    return request<OkResult>(`${API_ROOT}/auth/logout`, { method: "POST", json: {}, opKey: "auth.logout" });
  },
  session() {
    return request<SessionInfo>(`${API_ROOT}/auth/session`, { opKey: "auth.session" });
  },
  totpSetup() {
    return request<TotpSetup>(`${API_ROOT}/auth/totp/setup`, { method: "POST", json: {}, opKey: "auth.totpSetup" });
  },
  totpEnable(secret: string, code: string) {
    return request<OkResult>(`${API_ROOT}/auth/totp/enable`, {
      method: "POST",
      json: { secret, code },
      opKey: "auth.totpEnable",
    });
  },
  totpDisable(code: string) {
    return request<OkResult>(`${API_ROOT}/auth/totp/disable`, {
      method: "POST",
      json: { code },
      opKey: "auth.totpDisable",
    });
  },
  changePassword(currentPassword: string, newPassword: string) {
    return request<OkResult>(`${API_ROOT}/auth/password`, {
      method: "POST",
      json: { current_password: currentPassword, new_password: newPassword },
      opKey: "auth.password",
    });
  },

  // ---- devices -------------------------------------------------------
  listDevices() {
    return request<DeviceList>(`${API_ROOT}/devices`, { opKey: "devices.list" });
  },
  getDevice(uid: string) {
    return request<DeviceEnvelope>(devicePath(uid), { opKey: "devices.get" });
  },
  patchDevice(uid: string, patch: DevicePatch) {
    return request<DeviceEnvelope>(devicePath(uid), {
      method: "PATCH",
      json: patch,
      opKey: "devices.patch",
    });
  },
  deleteDevice(uid: string, purgeRemote = true) {
    return request<OkResult>(`${devicePath(uid)}${query({ purge_remote: purgeRemote ? "true" : "false" })}`, {
      method: "DELETE",
      opKey: "devices.delete",
    });
  },
  uninstallDevice(uid: string) {
    // `confirm` is part of the server contract (A1 / §2.3); omitting it was a 422.
    return request<UninstallAccepted>(`${devicePath(uid)}/uninstall`, {
      method: "POST",
      json: { confirm: true },
      opKey: "devices.uninstall",
    });
  },
  createCommand(uid: string, type: string, payload: Record<string, unknown> = {}, confirm = false) {
    return request<Command>(`${devicePath(uid)}/commands`, {
      method: "POST",
      json: { type, payload, confirm },
      opKey: "devices.commands.create",
    });
  },
  listCommands(uid: string, limit = 50, offset = 0) {
    return request<CommandList>(`${devicePath(uid)}/commands${query({ limit, offset })}`, {
      opKey: "devices.commands.list",
    });
  },

  // ---- frames --------------------------------------------------------
  /**
   * Immutable frame URL. No cache-busting nonce: the media endpoints answer
   * `immutable` with a long TTL, so the browser cache does the work. `<img>`
   * authenticates with the HttpOnly session cookie (README 0.3.0 §2.2).
   */
  frameUrl(uid: string, frameId: number, variant: "thumb" | "full") {
    return `${devicePath(uid)}/frames/${frameId}${query({ variant })}`;
  },
  latestFrame(uid: string) {
    return request<LatestFrame>(`${devicePath(uid)}/frames/latest`, { opKey: "frames.latest" });
  },
  listFrames(
    uid: string,
    params: { fromMs?: number; toMs?: number; limit?: number; offset?: number } = {},
  ) {
    return request<FrameList>(
      `${devicePath(uid)}/frames${query({
        from_ms: params.fromMs,
        to_ms: params.toMs,
        limit: params.limit,
        offset: params.offset,
      })}`,
      { opKey: "frames.list" },
    );
  },
  timeline(uid: string, fromMs: number, toMs: number, bucketMs?: number) {
    return request<Timeline>(
      `${devicePath(uid)}/timeline${query({ from_ms: fromMs, to_ms: toMs, bucket_ms: bucketMs })}`,
      { opKey: "frames.timeline" },
    );
  },
  purgeFrames(uid: string) {
    return request<OkResult>(`${devicePath(uid)}/frames`, { method: "DELETE", opKey: "frames.purge" });
  },

  // ---- files ---------------------------------------------------------
  listFiles(uid: string, path: string) {
    return request<FileList>(`${devicePath(uid)}/files${query({ path })}`, { opKey: "files.list" });
  },
  fileOp(uid: string, body: FileOpRequest) {
    return request<FileOpResult>(`${devicePath(uid)}/files/op`, {
      method: "POST",
      json: body,
      opKey: "files.op",
    });
  },
  rawUrl(uid: string, path: string, offset: number, length: number) {
    return rawFileUrl(uid, path, offset, length);
  },
  async readChunk(uid: string, path: string, offset: number, length: number, signal?: AbortSignal) {
    const { blob, headers } = await requestBinary(rawFileUrl(uid, path, offset, length), {
      opKey: "files.readChunk",
      signal,
    });
    const chunk: FileChunk = {
      blob,
      size: Number(headers.get("X-File-Size") ?? 0),
      offset: Number(headers.get("X-Chunk-Offset") ?? offset),
      eof: headers.get("X-Eof") === "1",
    };
    return chunk;
  },
  writeChunk(uid: string, path: string, offset: number, chunk: Blob, truncate: boolean, signal?: AbortSignal) {
    return request<FileOpResult>(`${devicePath(uid)}/files/raw${query({ path, offset, truncate: truncate ? "true" : "false" })}`, {
      method: "PUT",
      headers: { "Content-Type": "application/octet-stream" },
      body: chunk,
      signal,
      opKey: "files.writeChunk",
    });
  },

  // ---- control -------------------------------------------------------
  grantControl(uid: string, ttlS = 300) {
    return request<ControlGrant>(`${devicePath(uid)}/control`, {
      method: "POST",
      json: { confirm: true, ttl_s: ttlS },
      opKey: "control.grant",
    });
  },
  revokeControl(uid: string, token: string) {
    return request<OkResult>(`${devicePath(uid)}/control/${encodeURIComponent(token)}`, {
      method: "DELETE",
      opKey: "control.revoke",
    });
  },

  // ---- settings / audit / system -------------------------------------
  getSettings() {
    return request<SettingsPayload>(`${API_ROOT}/settings`, { opKey: "settings.get" });
  },
  patchSettings(values: Record<string, unknown>) {
    return request<SettingsPayload>(`${API_ROOT}/settings`, {
      method: "PATCH",
      json: { values },
      opKey: "settings.patch",
    });
  },
  /** Server-truth effective config for one device (global + this device's overrides). */
  getEffectiveSettings(uid: string) {
    return request<EffectiveSettings>(`${API_ROOT}/settings/effective/${encodeURIComponent(uid)}`, {
      opKey: "settings.effective",
    });
  },
  listAudit(params: { limit?: number; offset?: number; action?: string; deviceUid?: string } = {}) {
    return request<AuditList>(
      `${API_ROOT}/audit${query({
        limit: params.limit,
        offset: params.offset,
        action: params.action,
        device_uid: params.deviceUid,
      })}`,
      { opKey: "audit.list" },
    );
  },
  getSystem() {
    return request<SystemStatus>(`${API_ROOT}/system`, { opKey: "system.get" });
  },
  /** Public liveness endpoint (`main.py`), used by the system page for carriers. */
  getHealth() {
    return request<HealthInfo>(`${API_ROOT}/health`, { opKey: "system.health" });
  },

  // ---- updates -------------------------------------------------------
  getUpdates() {
    return request<UpdateStatus>(`${API_ROOT}/updates`, { opKey: "updates.status" });
  },
  uploadPackage(file: File, notes: string, options: UploadOptions = {}) {
    const form = new FormData();
    form.append("file", file);
    form.append("notes", notes);
    return upload<Package>(`${API_ROOT}/updates/agent`, form, options);
  },
  deletePackage(version: string) {
    return request<OkResult>(`${API_ROOT}/updates/agent/${encodeURIComponent(version)}`, {
      method: "DELETE",
      opKey: "updates.delete",
    });
  },

  // ---- install / enrolment tokens ------------------------------------
  /** Installer artifacts + enrolment note (the console's `GET /install`). */
  getInstall() {
    return request<InstallInfo>(`${API_ROOT}/install`, { opKey: "install.get" });
  },
  listInstallTokens() {
    return request<{ tokens: InstallToken[] }>(`${API_ROOT}/install-tokens`, {
      opKey: "install.tokens.list",
    });
  },
  /** Mint a one-shot enrolment token. The plaintext token is returned once. */
  createInstallToken(ttlS?: number) {
    return request<InstallTokenCreated>(`${API_ROOT}/install-tokens`, {
      method: "POST",
      json: ttlS === undefined ? {} : { ttl_s: ttlS },
      opKey: "install.tokens.create",
    });
  },
  deleteInstallToken(id: number) {
    return request<OkResult>(`${API_ROOT}/install-tokens/${id}`, {
      method: "DELETE",
      opKey: "install.tokens.delete",
    });
  },

  // ---- backup / archive (A14) ----------------------------------------
  backupOverview() {
    return request<BackupOverview>(`${API_ROOT}/backup`, { opKey: "backup.overview" });
  },
  listBackup(params: { status?: string; deviceUid?: string; limit?: number; offset?: number } = {}) {
    return request<BackupList>(
      `${API_ROOT}/backup/entries${query({
        status: params.status,
        device_uid: params.deviceUid,
        limit: params.limit,
        offset: params.offset,
      })}`,
      { opKey: "backup.list" },
    );
  },
  backupDetail(entryId: number) {
    return request<BackupDetail>(`${API_ROOT}/backup/entries/${entryId}`, { opKey: "backup.detail" });
  },
  /** Local copy download URL (cookie-authenticated, like frame images). */
  backupFileUrl(entryId: number) {
    return `${API_ROOT}/backup/entries/${entryId}/file`;
  },
  retrieveBackup(entryId: number) {
    return request<OkResult & { frame_id: number }>(`${API_ROOT}/backup/entries/${entryId}/retrieve`, {
      method: "POST",
      json: {},
      opKey: "backup.retrieve",
    });
  },
  deleteBackup(entryId: number) {
    return request<OkResult>(`${API_ROOT}/backup/entries/${entryId}`, {
      method: "DELETE",
      opKey: "backup.delete",
    });
  },
};

// ------------------------------------------------------------- console socket ---

export interface SocketEvent {
  type: string;
  /** Server clock; used to calibrate the console clock. */
  ts_ms?: number;
  device_uid?: string;
  data?: Record<string, unknown>;
  /** WebSocket close code, present on synthetic `socket_closed` events. */
  code?: number;
}

export interface EventSocketHandle {
  close(): void;
  /** Server heartbeat → local connection watchdog. */
  setWatchdogMs(ms: number): void;
}

const CLOSE_UNAUTHORIZED = 4401;
const RECONNECT_MAX_MS = 15_000;

/**
 * 事件 WS 的候选域名（/public/bootstrap 拉取后进程内缓存）。
 *
 * 控制台域名可能挂在不支持 WebSocket 的 CDN 后面（2026-10-25 生产：control
 * 域换阿里云 ESA）——同源 WS 永远连不上，必须逐个尝试服务端给出的域名
 * 列表（优先序非锁定），同源 host 排最后兜底。
 */
let eventWsOriginsCache: string[] | null = null;

async function fetchEventWsOrigins(): Promise<string[]> {
  if (eventWsOriginsCache) return eventWsOriginsCache;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const sameOrigin = `${scheme}://${location.host}`;
  try {
    const bootstrap = await request<{ base_urls?: string[] }>(`${API_ROOT}/public/bootstrap`, {
      opKey: "bootstrap",
    });
    const origins = [
      ...(bootstrap.base_urls ?? []).map((base) => base.replace(/^http/, "ws").replace(/\/+$/, "")),
      sameOrigin,
    ].filter((value, index, all) => value && all.indexOf(value) === index);
    eventWsOriginsCache = origins;
  } catch {
    eventWsOriginsCache = [sameOrigin];
  }
  return eventWsOriginsCache;
}

/**
 * Console event socket.
 *
 * Authenticates with the bearer token in the query string (the server accepts
 * `?token=` — a browser WebSocket cannot set headers, and a cross-origin socket
 * never carries the console domain's HttpOnly cookie). A close with 4401 is
 * reported as an auth failure instead of being retried forever.
 */
export function openEventSocket(options: {
  onEvent: (event: SocketEvent) => void;
  onAuthFailure?: () => void;
}): EventSocketHandle {
  let socket: WebSocket | null = null;
  let closed = false;
  let retry = 0;
  let timer: number | undefined;
  let watchdogTimer: number | undefined;
  let watchdogMs = 75_000;
  let lastMessageAt = 0;
  let originCursor = 0;

  const clearWatchdog = () => {
    if (watchdogTimer !== undefined) {
      window.clearInterval(watchdogTimer);
      watchdogTimer = undefined;
    }
  };

  const connect = async () => {
    if (closed) return;
    const origins = await fetchEventWsOrigins();
    const list = origins.length ? origins : [`wss://${location.host}`];
    if (originCursor >= list.length) originCursor = 0;
    const token = readToken();
    const query = token ? `?token=${encodeURIComponent(token)}` : "";
    socket = new WebSocket(`${list[originCursor]}${API_ROOT}/ws${query}`);
    let opened = false;
    lastMessageAt = performance.now();

    socket.onopen = () => {
      opened = true;
      retry = 0;
      options.onEvent({ type: "socket_open", data: {} });
    };
    socket.onmessage = (message) => {
      lastMessageAt = performance.now();
      try {
        const parsed = JSON.parse(String(message.data)) as LiveEvent;
        if (parsed && typeof parsed.type === "string") {
          options.onEvent(parsed as SocketEvent);
        }
      } catch {
        /* ignore malformed frames: one bad event must not kill the console */
      }
    };
    socket.onclose = (event) => {
      if (socket === null) return;
      socket = null;
      clearWatchdog();
      options.onEvent({ type: "socket_closed", code: event.code, data: {} });
      if (event.code === CLOSE_UNAUTHORIZED) options.onAuthFailure?.();
      if (closed) return;
      // 连接从未建立（该域名不支持 WS / 不可达）：轮换到下一个候选域名
      if (!opened) originCursor = (originCursor + 1) % list.length;
      const delay = Math.min(RECONNECT_MAX_MS, 1000 * 2 ** retry);
      retry += 1;
      timer = window.setTimeout(() => void connect(), delay);
    };
    socket.onerror = () => {
      // An error is always followed by `onclose`; closing here guarantees it.
      socket?.close();
    };

    // Connection watchdog: `performance.now()` is monotonic and is used only to
    // detect a silently dead socket, never to judge device freshness (spec C5).
    clearWatchdog();
    watchdogTimer = window.setInterval(() => {
      if (closed || !socket) return;
      if (performance.now() - lastMessageAt > watchdogMs) socket.close();
    }, 5_000);
  };

  void connect();

  return {
    close() {
      closed = true;
      clearWatchdog();
      if (timer !== undefined) window.clearTimeout(timer);
      socket?.close();
      socket = null;
    },
    setWatchdogMs(ms: number) {
      if (Number.isFinite(ms) && ms >= 20_000) watchdogMs = ms;
    },
  };
}