/** Formatting helpers. All timestamps are epoch milliseconds from the server. */

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

export function formatBytes(value: number | null | undefined, digits = 1): string {
  const bytes = Number(value ?? 0);
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
  const total = Math.max(0, Math.floor(Number(seconds ?? 0)));
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

/** "刚刚 / 12 秒前 / 3 分钟前" — computed against the *server* clock. */
export function formatAge(ageMs: number | null | undefined): string {
  if (ageMs === null || ageMs === undefined) return "未知";
  const seconds = Math.max(0, Math.round(ageMs / 1000));
  if (seconds < 3) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  const number = Number(value ?? 0);
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
  const number = Number(value ?? 0);
  return number.toLocaleString("zh-CN");
}