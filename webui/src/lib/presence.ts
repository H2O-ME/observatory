/**
 * Reading `Presence` defensively.
 *
 * The presence payload grows over time (transport, per-feature capabilities,
 * `self_update_blocked`, ...). The console must keep working against an older
 * server — or a half-upgraded one — so every optional field is normalised here
 * instead of being dereferenced at each call site. A missing field means "not
 * known", never a crash.
 */

import type { Capabilities, Presence, TransportKind } from "@/lib/types";

/** What the agent can do when the platform reports nothing at all. */
const NOTHING: Capabilities = {
  control: false,
  files: false,
  screenshot: false,
  timeline: true,
  update: false,
  uninstall: false,
};

/** Command-bus features survive polling; only live control needs WebSocket. */
const COMMANDS_ONLY: Capabilities = {
  control: false,
  files: true,
  screenshot: true,
  timeline: true,
  update: true,
  uninstall: true,
};

const EVERYTHING: Capabilities = {
  control: true,
  files: true,
  screenshot: true,
  timeline: true,
  update: true,
  uninstall: true,
};

export function transportOf(presence: Presence): TransportKind {
  return presence.transport ?? "none";
}

/**
 * Feature flags for this device.
 *
 * When the server omits `capabilities` (older payload) we fall back to the same
 * mapping the server uses: a plain online device gets everything, a degraded
 * one keeps the command bus but loses live control, and anything else is
 * treated as unreachable.
 */
export function capabilitiesOf(presence: Presence): Capabilities {
  if (presence.capabilities) return presence.capabilities;
  if (presence.state === "online") return EVERYTHING;
  if (presence.state === "degraded" || presence.state === "uninstalling") return COMMANDS_ONLY;
  return NOTHING;
}

/**
 * True when the device is reachable but only over the HTTP polling channel.
 *
 * This is *not* the same as capture being blocked: polling is a transport
 * limitation, the screen may still streak fine.
 */
export function isPollMode(presence: Presence): boolean {
  return transportOf(presence) === "poll";
}

/**
 * Why real-time control cannot be offered right now, or an empty string when it
 * can. Polling is checked first: even if a stale `capabilities` block claims
 * control is available, there is no channel to carry pointer/keyboard input.
 */
export function controlUnavailableReason(presence: Presence): string {
  if (isPollMode(presence)) return "当前为轮询模式，实时控制不可用";
  if (capabilitiesOf(presence).control) return "";
  const reason = (presence.reason || "").trim();
  return reason ? `实时控制不可用：${reason}` : "实时控制不可用";
}

/**
 * Why this agent cannot upgrade itself, or an empty string when it can.
 *
 * The server sends a non-empty string when the installed version is too old for
 * the automatic upgrade path; `null`/absent means "normal" and nothing should
 * be shown. Treated as optional because the field ships with a newer server.
 */
export function selfUpdateBlockedReason(presence: Presence): string {
  const reason = presence.self_update_blocked;
  return typeof reason === "string" ? reason.trim() : "";
}