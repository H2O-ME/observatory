/**
 * Reading the server's `presence` — read only.
 *
 * The 0.3.0 rule is absolute (spec C4, A13): `presence.py` produces the only
 * online decision and the only `capabilities` map. This module therefore has no
 * fallback table, no `state → capabilities` mapping and no freshness threshold.
 * It names the fields and turns them into the exact strings a view renders.
 */

import type { Capabilities, Presence, TransportKind } from "@/lib/types";

export function transportOf(presence: Presence): TransportKind {
  return presence.transport;
}

/** The server's capability verdict, verbatim. */
export function capabilitiesOf(presence: Presence): Capabilities {
  return presence.capabilities;
}

/** Reachable only over the HTTP polling carrier: the fact, not a guess. */
export function isPollMode(presence: Presence): boolean {
  return presence.transport === "poll";
}

/**
 * Why real-time control cannot be offered right now, or an empty string when it
 * can. Polling is checked first: without the live carrier there is nothing to
 * carry pointer/keyboard input even if a stale capability block says otherwise.
 */
export function controlUnavailableReason(presence: Presence): string {
  if (isPollMode(presence)) return "当前为轮询模式，实时控制不可用";
  if (presence.capabilities.control) return "";
  const reason = presence.reason.trim();
  return reason ? `实时控制不可用：${reason}` : "实时控制不可用";
}

/** Why this agent cannot upgrade itself, or an empty string when it can. */
export function selfUpdateBlockedReason(presence: Presence): string {
  const reason = presence.self_update_blocked;
  return typeof reason === "string" ? reason.trim() : "";
}

/**
 * Age of the last contact, computed by the server (`presence.age_ms`).
 * The console never derives this from a local clock (spec C5, A12).
 */
export function presenceAgeMs(presence: Presence): number | null {
  return presence.age_ms;
}