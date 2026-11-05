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

import { api, bumpSessionEpoch, currentSessionEpoch, isApiError, setUnauthorizedHandler, writeToken } from "@/lib/api";
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
  // F40: cancel in-flight queries BEFORE clearing — a resolve that lands after
  // clear() would rebuild a cache entry with the previous operator's data.
  void queryClient.cancelQueries();
  queryClient.clear();
  useLive.getState().reset();
}

/**
 * W3: 刷新序号。仅本模块使用。
 *
 * `sessionEpoch`（api.ts）能识别"跨会话"的过期结果（登录/登出/401 之间），但
 * 识别不了同一会话内并发 refresh 的乱序：两个匿名 refresh 同时发出、后发的
 * 先回、先发的后回，后回来的旧结果会把状态覆盖回去。每发起一次 refresh 领一个
 * 递增序号，只有序号仍是最新的那次才允许写状态。
 */
let refreshSeq = 0;

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
    // W3: 捕获"这次请求属于哪个会话"以及"是不是最新的一次刷新"。匿名登录页
    // 启动时发出的 /session 请求可能比登录响应还慢——它返回的
    // `authenticated:false` 不能把刚登录的会话打回匿名、更不能清掉新 token。
    const requestEpoch = currentSessionEpoch();
    const seq = ++refreshSeq;
    const stale = () => requestEpoch !== currentSessionEpoch() || seq !== refreshSeq;
    try {
      const session = await api.session();
      // 过时的成功结果直接丢弃（成功和失败都要丢：旧请求的失败同样不能把
      // 新会话打成 error）。
      if (stale()) return;
      set({
        status: session.authenticated ? "authed" : "anonymous",
        ...sessionFields(session),
        error: "",
      });
      // 过时的 `authenticated:false` 会清掉刚写入的 token，所以这一步必须在
      // stale 检查之后。
      if (!session.authenticated) writeToken("");
    } catch (error) {
      if (stale()) return;
      set({
        status: "error",
        error: isApiError(error) ? error.message : "无法连接服务器",
      });
    }
  },

  async login(password, totp) {
    const result = await api.login(password, totp);
    writeToken(result.token);
    bumpSessionEpoch();
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
    // W3: 显式登出同样推进会话世代——登出前发出、登出后才返回的 refresh
    // （成功或失败）都不得再把状态写回来。401 处理器也会 bump，这里不能省：
    // 正常登出根本没有 401。
    bumpSessionEpoch();
    set({ status: "anonymous", error: "" });
  },
}));

// One 401 handler for the whole application. It must not import a React hook, so
// it reaches the store through `getState`/`setState`.
setUnauthorizedHandler((requestEpoch) => {
  // F40: a 401 from a request that started before the current session (stale
  // in-flight during login/logout) must not tear the new session down.
  if (requestEpoch !== currentSessionEpoch()) return;
  const current = useAuth.getState();
  if (current.status === "anonymous") return;
  bumpSessionEpoch();
  clearSession();
  useAuth.setState({ status: "anonymous", error: "" });
});