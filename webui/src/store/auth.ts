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

export const useAuth = create<AuthState>((set) => ({
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
    await this.refresh();
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
    await this.refresh();
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