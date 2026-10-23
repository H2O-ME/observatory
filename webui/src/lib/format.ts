/**
 * Formatting helpers.
 *
 * Every timestamp passed in is epoch milliseconds produced by the server clock
 * (`server_time_ms`) or by the device (`device_time_ms`). Nothing here reads the
 * local clock: the caller decides *which* server value to format.
 */

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

/** Byte size; `null`/`undefined` means "the server did not report it" → `—`. */
export function formatBytes(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  let index = 0;
  let size = bytes;
  while (size >= 1024 && index < BYTE_UNITS.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : digits)} ${BYTE_UNITS[index]}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.max(0, Math.floor(Number(seconds)));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分`;
  return `${total} 秒`;
}

function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

/** Absolute timestamp → local wall-clock string (timezone display only). */
export function formatTime(ms: number): string {
  if (!ms) return "—";
  const date = new Date(ms);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function formatTimeShort(ms: number): string {
  if (!ms) return "—";
  const date = new Date(ms);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function formatDate(ms: number): string {
  if (!ms) return "—";
  const date = new Date(ms);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function formatDateTime(ms: number): string {
  if (!ms) return "—";
  return `${formatDate(ms)} ${formatTime(ms)}`;
}

/** "刚刚 / 12 秒前 / 3 分钟前" for an age in ms that the server computed. */
export function formatAge(ageMs: number | null | undefined): string {
  if (ageMs === null || ageMs === undefined || !Number.isFinite(ageMs)) return "—";
  const seconds = Math.max(0, Math.round(ageMs / 1000));
  if (seconds < 3) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

/**
 * A time reported by a device or a server event. `device_time_ms` is the 0.3.0
 * name, `ts_ms` is the 0.2.x alias still returned while media endpoints migrate
 * (spec §2.2 allows added fields, not renamed ones).
 */
export function frameTimeMs(value: { device_time_ms?: number; ts_ms?: number } | null | undefined): number {
  if (!value) return 0;
  if (typeof value.device_time_ms === "number") return value.device_time_ms;
  if (typeof value.ts_ms === "number") return value.ts_ms;
  return 0;
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number.toFixed(digits)}%`;
}

export function basename(path: string): string {
  const cleaned = path.replace(/[\\/]+$/, "");
  const parts = cleaned.split(/[\\/]/);
  return parts[parts.length - 1] || cleaned;
}

export function dirname(path: string): string | null {
  const cleaned = path.replace(/[\\/]+$/, "");
  if (!cleaned) return null;
  const index = Math.max(cleaned.lastIndexOf("\\"), cleaned.lastIndexOf("/"));
  if (index <= 0) return null;
  const parent = cleaned.slice(0, index);
  // Keep drive roots intact: "C:" -> "C:\"
  if (/^[A-Za-z]:$/.test(parent)) return `${parent}\\`;
  return parent;
}

export function joinPath(parent: string, name: string): string {
  if (!parent) return name;
  const separator = parent.includes("\\") ? "\\" : "/";
  const trimmed = parent.replace(/[\\/]+$/, "");
  return `${trimmed}${separator}${name}`;
}

export function initials(value: string): string {
  const trimmed = (value || "?").trim();
  return trimmed.slice(0, 2).toUpperCase();
}

export function prettyNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString("zh-CN");
}