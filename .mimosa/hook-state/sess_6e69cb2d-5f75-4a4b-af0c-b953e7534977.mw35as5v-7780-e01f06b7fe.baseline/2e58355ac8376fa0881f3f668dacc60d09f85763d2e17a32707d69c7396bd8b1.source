/**
 * Timeline windows computed from the *server* clock.
 *
 * The 0.2.x timeline asked for "today" using `Date.now()`, so a browser whose
 * clock was a day off requested the wrong window and rendered an empty grid that
 * looked like "no frames were captured". Here the caller passes `serverNow()`;
 * `Date` is used only as a calendar for the operator's timezone (display and
 * day boundaries), never as a source of "now".
 */

export interface TimeWindow {
  fromMs: number;
  toMs: number;
  /** Human label for the picker ("今天" / "昨天" / 具体日期). */
  label: string;
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** Local midnight of the day containing `ms`. */
export function localDayStart(ms: number): number {
  const date = new Date(ms);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

/** `daysAgo = 0` → today, `1` → yesterday, … */
export function dayWindow(nowMs: number, daysAgo = 0): TimeWindow {
  const date = new Date(nowMs);
  date.setDate(date.getDate() - Math.max(0, daysAgo));
  date.setHours(0, 0, 0, 0);
  const fromMs = date.getTime();
  const next = new Date(fromMs);
  next.setDate(next.getDate() + 1);
  const end = next.getTime();
  // Today's window ends "now"; if the operator opens the page in the first
  // second after midnight, keep `to > from` so the API does not reject it.
  const toMs = daysAgo === 0 ? Math.min(end, Math.max(nowMs, fromMs + 1000)) : end;
  const label = daysAgo === 0 ? "今天" : daysAgo === 1 ? "昨天" : formatDay(fromMs);
  return { fromMs, toMs, label };
}

export function formatDay(ms: number): string {
  const date = new Date(ms);
  const pad = (value: number) => (value < 10 ? `0${value}` : String(value));
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function formatWindow(window: { fromMs: number; toMs: number }): string {
  const clock = (ms: number) => {
    const date = new Date(ms);
    const pad = (value: number) => (value < 10 ? `0${value}` : String(value));
    return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };
  if (window.toMs - window.fromMs <= DAY_MS) {
    return `${formatDay(window.fromMs)} ${clock(window.fromMs)}–${clock(window.toMs)}`;
  }
  return `${formatDay(window.fromMs)} → ${formatDay(window.toMs)}`;
}

/** `datetime-local` value ("2026-10-23T08:00") → epoch ms in local time. */
export function parseDateTimeLocal(value: string): number | null {
  const text = value.trim();
  if (!text) return null;
  const date = new Date(text);
  const ms = date.getTime();
  return Number.isFinite(ms) ? ms : null;
}

/** Render an epoch ms as the value expected by `<input type="datetime-local">`. */
export function toDateTimeLocal(ms: number): string {
  const date = new Date(ms);
  const pad = (value: number) => (value < 10 ? `0${value}` : String(value));
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}`;
}