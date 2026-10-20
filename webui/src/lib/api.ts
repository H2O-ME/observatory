/**
 * Typed API client.
 *
 * Two rules:
 *  - Every call surfaces the server's message instead of a generic failure, so the
 *    console can say "设备当前不在线，无法下发卸载指令" rather than "请求失败".
 *  - Liveness is never inferred here; only the server's `presence` payload is used.
 */

import type {
  AuditList,
  Command,
  ControlGrant,
  DeviceEnvelope,
  DeviceList,
  FileList,
  FileOpRequest,
  FileOpResult,
  Frame,
  LiveEvent,
  Package,
  SessionInfo,
  SettingsPayload,
  SystemStatus,
  Timeline,
  UpdateStatus,
} from "./types";

const TOKEN_KEY = "observatory.token";

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

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
    /* storage disabled: session stays in memory only */
  }
}

function headers(extra?: Record<string, string>): Record<string, string> {
  const token = readToken();
  const base: Record<string, string> = { Accept: "application/json" };
  if (token) base.Authorization = `Bearer ${token}`;
  if (extra) Object.assign(base, extra);
  return base;
}

function errorDetail(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    const detail = record.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const first = detail[0] as Record<string, unknown> | undefined;
      if (first && typeof first.msg === "string") return first.msg;
    }
  }
  return fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: headers(init?.headers as Record<string, string> | undefined),
    cache: "no-store",
  });
  if (response.status === 204) return undefined as T;
  const isJson = (response.headers.get("content-type") ?? "").includes("application/json");
  const payload = isJson ? await response.json() : await response.text();
  if (!response.ok) {
    throw new ApiError(response.status, errorDetail(payload, `请求失败 (${response.status})`));
  }
  return payload as T;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function patch<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export const api = {
  // ---- auth ----------------------------------------------------------
  login(password: string, totp?: string) {
    return post<{ token: string; expires_at_ms: number; version: string }>("/api/v1/auth/login", {
      password,
      totp: totp || null,
    });
  },
  logout() {
    return post<{ ok: boolean }>("/api/v1/auth/logout", {});
  },
  session() {
    return request<SessionInfo>("/api/v1/auth/session");
  },
  totpSetup() {
    return post<{ secret: string; uri: string }>("/api/v1/auth/totp/setup", {});
  },
  totpEnable(secret: string, code: string) {
    return post<{ ok: boolean; message: string }>("/api/v1/auth/totp/enable", { secret, code });
  },
  totpDisable(code: string) {
    // The server verifies this code; it is not decorative.
    return post<{ ok: boolean; message: string }>("/api/v1/auth/totp/disable", { code });
  },
  changePassword(currentPassword: string, newPassword: string) {
    return post<{ ok: boolean; message: string }>("/api/v1/auth/password", {
      current_password: currentPassword,
      new_password: newPassword,
    });
  },

  // ---- devices -------------------------------------------------------
  devices() {
    return request<DeviceList>("/api/v1/devices");
  },
  device(uid: string) {
    return request<DeviceEnvelope>(`/api/v1/devices/${encodeURIComponent(uid)}`);
  },
  patchDevice(uid: string, body: { name?: string; note?: string; settings?: Record<string, unknown> }) {
    return patch<DeviceEnvelope>(`/api/v1/devices/${encodeURIComponent(uid)}`, body);
  },
  deleteDevice(uid: string) {
    return request<{ ok: boolean; message: string }>(`/api/v1/devices/${encodeURIComponent(uid)}`, {
      method: "DELETE",
    });
  },
  uninstall(uid: string) {
    // `confirm` is required by the server; the old console omitted the body and
    // every remote uninstall failed with 422.
    return post<{ command_id: string; status: string; message: string }>(
      `/api/v1/devices/${encodeURIComponent(uid)}/uninstall`,
      { confirm: true },
    );
  },
  createCommand(uid: string, type: string, payload: Record<string, unknown> = {}, confirm = false) {
    return post<Command>(`/api/v1/devices/${encodeURIComponent(uid)}/commands`, { type, payload, confirm });
  },
  commands(uid: string, limit = 50) {
    return request<{ commands: Command[]; server_time_ms: number }>(
      `/api/v1/devices/${encodeURIComponent(uid)}/commands?limit=${limit}`,
    );
  },

  // ---- frames --------------------------------------------------------
  latestFrame(uid: string) {
    return request<{ frame: Frame | null; server_time_ms: number }>(
      `/api/v1/devices/${encodeURIComponent(uid)}/frames/latest`,
    );
  },
  frameUrl(uid: string, frameId: number, variant: "thumb" | "full") {
    // No cache-busting nonce: frames are immutable and served with a long TTL.
    return `/api/v1/devices/${encodeURIComponent(uid)}/frames/${frameId}?variant=${variant}`;
  },
  timeline(uid: string, fromMs: number, toMs: number, bucketMs?: number) {
    const params = new URLSearchParams({ from_ms: String(fromMs), to_ms: String(toMs) });
    if (bucketMs) params.set("bucket_ms", String(bucketMs));
    return request<Timeline>(`/api/v1/devices/${encodeURIComponent(uid)}/timeline?${params.toString()}`);
  },
  purgeFrames(uid: string) {
    return request<{ ok: boolean; message: string }>(`/api/v1/devices/${encodeURIComponent(uid)}/frames`, {
      method: "DELETE",
    });
  },

  // ---- files ---------------------------------------------------------
  files(uid: string, path: string) {
    const params = path ? `?path=${encodeURIComponent(path)}` : "";
    return request<FileList>(`/api/v1/devices/${encodeURIComponent(uid)}/files${params}`);
  },
  fileOp(uid: string, body: FileOpRequest) {
    return post<FileOpResult>(`/api/v1/devices/${encodeURIComponent(uid)}/files/op`, body);
  },
  rawUrl(uid: string, path: string, offset: number, length: number) {
    const params = new URLSearchParams({ path, offset: String(offset), length: String(length) });
    return `/api/v1/devices/${encodeURIComponent(uid)}/files/raw?${params.toString()}`;
  },
  async readChunk(uid: string, path: string, offset: number, length: number) {
    const response = await fetch(this.rawUrl(uid, path, offset, length), {
      headers: headers(),
      cache: "no-store",
    });
    if (!response.ok) {
      throw new ApiError(response.status, errorDetail(await response.json().catch(() => null), "读取失败"));
    }
    const blob = await response.blob();
    return {
      blob,
      size: Number(response.headers.get("X-File-Size") ?? 0),
      offset: Number(response.headers.get("X-Chunk-Offset") ?? offset),
      eof: response.headers.get("X-Eof") === "1",
    };
  },
  async writeChunk(uid: string, path: string, offset: number, chunk: Blob, truncate: boolean) {
    const params = new URLSearchParams({
      path,
      offset: String(offset),
      truncate: truncate ? "true" : "false",
    });
    return request<FileOpResult>(`/api/v1/devices/${encodeURIComponent(uid)}/files/raw?${params.toString()}`, {
      method: "PUT",
      headers: { "Content-Type": "application/octet-stream" },
      body: chunk,
    });
  },

  // ---- control -------------------------------------------------------
  controlGrant(uid: string, ttlS = 300) {
    return post<ControlGrant>(`/api/v1/devices/${encodeURIComponent(uid)}/control`, {
      confirm: true,
      ttl_s: ttlS,
    });
  },
  controlRevoke(uid: string, token: string) {
    return request<{ ok: boolean }>(
      `/api/v1/devices/${encodeURIComponent(uid)}/control/${encodeURIComponent(token)}`,
      { method: "DELETE" },
    );
  },

  // ---- settings / audit / system -------------------------------------
  settings() {
    return request<SettingsPayload>("/api/v1/settings");
  },
  patchSettings(values: Record<string, unknown>) {
    return patch<SettingsPayload>("/api/v1/settings", { values });
  },
  audit(params: { limit?: number; offset?: number; action?: string; device_uid?: string } = {}) {
    const search = new URLSearchParams();
    if (params.limit) search.set("limit", String(params.limit));
    if (params.offset) search.set("offset", String(params.offset));
    if (params.action) search.set("action", params.action);
    if (params.device_uid) search.set("device_uid", params.device_uid);
    return request<AuditList>(`/api/v1/audit?${search.toString()}`);
  },
  system() {
    return request<SystemStatus>("/api/v1/system");
  },

  // ---- updates -------------------------------------------------------
  updates() {
    return request<UpdateStatus>("/api/v1/updates");
  },
  uploadPackage(file: File, notes: string) {
    const form = new FormData();
    form.append("file", file);
    form.append("notes", notes);
    return request<Package>("/api/v1/updates/agent", { method: "POST", body: form });
  },
  deletePackage(version: string) {
    return request<{ ok: boolean; message: string }>(
      `/api/v1/updates/agent/${encodeURIComponent(version)}`,
      { method: "DELETE" },
    );
  },
};

export interface SocketEvent {
  type: string;
  ts_ms?: number;
  device_uid?: string;
  data?: Record<string, unknown>;
}

/** Open the console event socket. Returns a disposer. */
export function openEventSocket(onEvent: (event: SocketEvent) => void): () => void {
  const token = readToken();
  if (!token) return () => undefined;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const url = `${scheme}://${location.host}/api/v1/ws?token=${encodeURIComponent(token)}`;
  let socket: WebSocket | null = null;
  let closed = false;
  let retry = 0;
  let timer: number | undefined;

  const connect = () => {
    if (closed) return;
    socket = new WebSocket(url);
    socket.onopen = () => {
      retry = 0;
      onEvent({ type: "socket_open", data: {} });
    };
    socket.onmessage = (message) => {
      try {
        const parsed = JSON.parse(String(message.data)) as LiveEvent;
        if (parsed && typeof parsed.type === "string") onEvent(parsed);
      } catch {
        /* ignore malformed frames */
      }
    };
    socket.onclose = () => {
      onEvent({ type: "socket_closed", data: {} });
      if (closed) return;
      const delay = Math.min(15000, 1000 * 2 ** retry);
      retry += 1;
      timer = window.setTimeout(connect, delay);
    };
    socket.onerror = () => {
      socket?.close();
    };
  };

  connect();
  return () => {
    closed = true;
    if (timer) window.clearTimeout(timer);
    socket?.close();
  };
}