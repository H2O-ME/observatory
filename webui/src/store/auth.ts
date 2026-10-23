/**
 * Authentication state.
 *
 * Three rules from the 0.3.0 spec shape this store:
 *  - Every request goes through the single client (`lib/api.ts`), so the
 *    envelope is unwrapped and the server clock is calibrated there.
 *  - A 401 anywhere ends the session once: the api client notifies this module,
 *    which clears the token, the query cache and the live store.
 *  - Logging out clears the React Query cache. A cache that survives a logout
 *    leaks the previous operator's devices and audit entries into the next
 *    session.
 */

import { create } from "zustand";

import { api, isApiError, setUnauthorizedHandler, writeToken } from "@/lib/api";
import { queryClient } from "@/lib/query-client";
import { useLive } from "@/store/live";
import type { SessionInfo } from "@/lib/types";

type Status = "boot" | "anonymous" | "authed" | "error";

interface AuthState {
  status: Status;
  version: string;
  totpEnabled: boolean;
  consoleHost: string;
  baseUrls: string[];
  error: string;
  bootstrap: () => Promise<void>;
  login: (password: string, totp?: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

function sessionFields(session: SessionInfo) {
  return {
    version: session.version,
    totpEnabled: session.totp_enabled,
    consoleHost: session.console_host,
    baseUrls: session.base_urls,
  };
}

/** Drop everything that belongs to the current session. */
function clearSession(): void {
  writeToken("");
  queryClient.clear();
  useLive.getState().reset();
}

export const useAuth = create<AuthState>((set, get) => ({
  status: "boot",
  version: "",
  totpEnabled: false,
  consoleHost: "",
  baseUrls: [],
  error: "",

  /**
   * The console asks the server for a session even when no token is stored: the
   * login page needs the server version, the console host and whether TOTP is
   * enabled.
   */
  async bootstrap() {
    await get().refresh();
  },

  async refresh() {
    try {
      const session = await api.session();
      set({
        status: session.authenticated ? "authed" : "anonymous",
        ...sessionFields(session),
        error: "",
      });
      if (!session.authenticated) writeToken("");
    } catch (error) {
      set({
        status: "error",
        error: isApiError(error) ? error.message : "无法连接服务器",
      });
    }
  },

  async login(password, totp) {
    const result = await api.login(password, totp);
    writeToken(result.token);
    set({ status: "authed", version: result.version, error: "" });
    await get().refresh();
  },

  async logout() {
    try {
      await api.logout();
    } catch {
      /* the local session is cleared below regardless of the server's answer */
    }
    clearSession();
    set({ status: "anonymous", error: "" });
  },
}));

// One 401 handler for the whole application. It must not import a React hook, so
// it reaches the store through `getState`/`setState`.
setUnauthorizedHandler(() => {
  const current = useAuth.getState();
  if (current.status === "anonymous") return;
  clearSession();
  useAuth.setState({ status: "anonymous", error: "" });
});