import { create } from "zustand";

import { api, ApiError, readToken, writeToken } from "@/lib/api";

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

export const useAuth = create<AuthState>((set, get) => ({
  status: "boot",
  version: "",
  totpEnabled: false,
  consoleHost: "",
  baseUrls: [],
  error: "",

  async bootstrap() {
    if (!readToken()) {
      // Still ask the server for its version so the login page can show it.
      try {
        const session = await api.session();
        set({
          status: "anonymous",
          version: session.version,
          totpEnabled: session.totp_enabled,
          consoleHost: session.console_host,
          baseUrls: session.base_urls,
          error: "",
        });
      } catch {
        set({ status: "anonymous", error: "" });
      }
      return;
    }
    // Must go through `get()`: `bootstrap` is normally destructured out of the
    // store (`const { bootstrap } = useAuth()`), so `this` is undefined and the
    // call threw a TypeError before any request was made. That is what left the
    // console on "正在连接 Observatory…" with an empty network panel.
    await get().refresh();
  },

  async refresh() {
    try {
      const session = await api.session();
      set({
        status: session.authenticated ? "authed" : "anonymous",
        version: session.version,
        totpEnabled: session.totp_enabled,
        consoleHost: session.console_host,
        baseUrls: session.base_urls,
        error: "",
      });
      if (!session.authenticated) writeToken("");
    } catch (error) {
      const detail = error instanceof ApiError ? error.detail : "无法连接服务器";
      set({ status: "error", error: detail });
    }
  },

  async login(password, totp) {
    const result = await api.login(password, totp);
    writeToken(result.token);
    set({ status: "authed", version: result.version, error: "" });
    // Same reason as `bootstrap`: `login` is destructured off the store, so `this`
    // is undefined here too.
    await get().refresh();
  },

  async logout() {
    try {
      await api.logout();
    } catch {
      /* logging out locally is enough */
    }
    writeToken("");
    set({ status: "anonymous" });
  },
}));