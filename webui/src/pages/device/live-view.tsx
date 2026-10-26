import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { Expand, Keyboard, MousePointer2, Power, RefreshCw, WifiOff } from "lucide-react";

import { FrameStage } from "@/components/frame-view";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, isApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import { serverNow, serverTimeLeft, useServerNow } from "@/store/clock";
import { controlUnavailableReason, isPollMode } from "@/lib/presence";
import { useViewport } from "@/hooks/use-viewport";
import type { Device } from "@/lib/types";

const NORM = 10000;
const CONTROL_TTL_S = 600;
const PING_INTERVAL_MS = 3_000;

type ControlStatus = "idle" | "granting" | "connecting" | "streaming" | "error" | "closed";

interface ControlSession {
  token: string;
  /** Server clock: when the grant expires. Never compared to a local clock. */
  expiresAtMs: number;
  fps: number;
  quality: number;
}

const STATUS_LABEL: Record<ControlStatus, string> = {
  idle: "待机",
  granting: "申请授权",
  connecting: "连接通道",
  streaming: "实时传输",
  error: "通道错误",
  closed: "会话结束",
};

const STATUS_TONE: Record<ControlStatus, string> = {
  idle: "text-muted-foreground",
  granting: "text-warning",
  connecting: "text-warning",
  streaming: "text-success",
  error: "text-destructive",
  closed: "text-muted-foreground",
};

const STATUS_DOT: Record<ControlStatus, string> = {
  idle: "bg-faint",
  granting: "bg-warning animate-sm-pulse",
  connecting: "bg-warning animate-sm-pulse",
  streaming: "bg-success animate-sm-pulse",
  error: "bg-destructive",
  closed: "bg-faint",
};

/** Windows virtual-key codes for the keys that are not printable text. */
const VK_BY_KEY: Record<string, number> = {
  Backspace: 0x08,
  Tab: 0x09,
  Enter: 0x0d,
  Escape: 0x1b,
  " ": 0x20,
  PageUp: 0x21,
  PageDown: 0x22,
  End: 0x23,
  Home: 0x24,
  ArrowLeft: 0x25,
  ArrowUp: 0x26,
  ArrowRight: 0x27,
  ArrowDown: 0x28,
  Insert: 0x2d,
  Delete: 0x2e,
  Control: 0x11,
  Alt: 0x12,
  Shift: 0x10,
  Meta: 0x5b,
  CapsLock: 0x14,
  F1: 0x70,
  F2: 0x71,
  F3: 0x72,
  F4: 0x73,
  F5: 0x74,
  F6: 0x75,
  F7: 0x76,
  F8: 0x77,
  F9: 0x78,
  F10: 0x79,
  F11: 0x7a,
  F12: 0x7b,
};

function vkCodeFor(event: React.KeyboardEvent): number {
  const mapped = VK_BY_KEY[event.key];
  if (mapped !== undefined) return mapped;
  if (event.code.startsWith("Key") && event.code.length === 4) return event.code.charCodeAt(3);
  if (event.code.startsWith("Digit") && event.code.length === 6) return event.code.charCodeAt(5);
  return 0;
}

/** One telemetry cell of the live status strip. */
function LiveMetric({
  label,
  value,
  unit,
  hint,
  tone,
}: {
  label: string;
  value: string;
  unit?: string;
  hint?: string;
  tone?: string;
}) {
  return (
    <div className="min-w-0 px-6 py-3">
      <p className="truncate text-xs font-medium tracking-[0.08em] text-muted-foreground uppercase">{label}</p>
      <p className={cn("tnum mt-1.5 truncate text-lg font-semibold leading-none tracking-[-0.02em]", tone ?? "text-foreground")}>
        {value}
        {unit ? <span className="ml-0.5 text-xs font-medium text-faint">{unit}</span> : null}
      </p>
      {hint ? <p className="mt-1 truncate text-xs text-faint">{hint}</p> : null}
    </div>
  );
}

interface Props {
  device: Device;
}

/**
 * Real-time control.
 *
 * Time handling is the point of this rewrite (A12):
 *  - The grant's `expires_at_ms` is a *server* timestamp; the countdown and the
 *    expiry watchdog compare it with `serverNow()`, never with `Date.now()`. If
 *    the clock is not calibrated yet the view says so instead of guessing.
 *  - `performance.now()` appears only for the input throttle and the ping
 *    round-trip: it is a monotonic stopwatch, not a wall clock.
 *
 * Session hygiene:
 *  - Stopping revokes the grant on the server; closing the tab does too.
 *  - If the device loses its live carrier mid-session, the grant is revoked and
 *    the socket closed, so no orphan control stream keeps running for its TTL.
 *  - `onerror` always closes the socket, which also prevents a second socket
 *    from being opened on top of a half-dead one.
 */
export function LiveView({ device }: Props) {
  const [status, setStatus] = useState<ControlStatus>("idle");
  const [message, setMessage] = useState("");
  const [session, setSession] = useState<ControlSession | null>(null);
  const [frameUrl, setFrameUrl] = useState("");
  const [natural, setNatural] = useState({ width: 0, height: 0 });
  const [fps, setFps] = useState(0);
  const [rttMs, setRttMs] = useState<number | null>(null);
  const [textValue, setTextValue] = useState("");
  const [isFullscreen, setIsFullscreen] = useState(false);

  const socketRef = useRef<WebSocket | null>(null);
  const sessionRef = useRef<ControlSession | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const lastUrlRef = useRef("");
  const lastMoveRef = useRef(0);
  const frameCountRef = useRef(0);
  const pingAtRef = useRef(0);
  // F41: 修饰键按下状态——keydown 对修饰键只发 down、keyup 补 up，组合键
  // （Ctrl+C / Alt+F4 / Win 组合）才可用；repeat 事件靠该集合去重。
  const heldModifiersRef = useRef<Set<number>>(new Set());

  const uid = device.uid;
  const issue = controlUnavailableReason(device.presence);
  const viewport = useViewport();
  const watch = viewport === "watch";
  // Subscribes to the single 1 Hz ticker so the countdown ages while visible.
  const now = useServerNow();

  // Presentation only: while the stage is fullscreen it should fill the screen
  // instead of keeping the letterboxed 16:9 desktop size.
  useEffect(() => {
    const sync = () => setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  const stageSizeStyle: CSSProperties | undefined = isFullscreen
    ? { width: "100%", height: "100%" }
    : viewport === "desktop"
      ? { width: "min(100%, max(20rem, calc((100vh - 26rem) * 16 / 9)))" }
      : undefined;

  const send = useCallback((payload: Record<string, unknown>) => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
    }
  }, []);

  const closeSocket = useCallback(() => {
    const socket = socketRef.current;
    socketRef.current = null;
    if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
  }, []);

  const stop = useCallback(
    (nextMessage = "") => {
      const active = sessionRef.current;
      sessionRef.current = null;
      setSession(null);
      closeSocket();
      if (active) {
        // Best-effort cleanup; the server also expires the grant on its own.
        void api.revokeControl(uid, active.token).catch(() => undefined);
      }
      if (lastUrlRef.current) {
        URL.revokeObjectURL(lastUrlRef.current);
        lastUrlRef.current = "";
      }
      setFrameUrl("");
      setFps(0);
      setStatus(nextMessage ? "closed" : "idle");
      setMessage(nextMessage);
    },
    [closeSocket, uid],
  );

  useEffect(() => () => stop(""), [stop]);

  // A device that falls back to polling (or goes offline) cannot carry input:
  // revoke the grant instead of leaving a control stream running unseen.
  useEffect(() => {
    if (!session) return;
    if (issue) stop(issue);
  }, [issue, session, stop]);

  // Frame-rate counter and expiry watchdog, both anchored on the server clock.
  useEffect(() => {
    if (!session) return;
    const timer = window.setInterval(() => {
      setFps(frameCountRef.current);
      frameCountRef.current = 0;
      const current = serverNow();
      if (current !== null && current >= session.expiresAtMs) stop("控制会话已到期");
    }, 1000);
    return () => window.clearInterval(timer);
  }, [session, stop]);

  // Round-trip measurement: `performance.now()` is the monotonic stopwatch.
  useEffect(() => {
    if (status !== "streaming") return;
    const timer = window.setInterval(() => {
      const socket = socketRef.current;
      if (socket?.readyState === WebSocket.OPEN) {
        pingAtRef.current = performance.now();
        socket.send(JSON.stringify({ type: "ping" }));
      }
    }, PING_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [status]);

  const start = async () => {
    if (issue) return;
    stop("");
    setStatus("granting");
    setMessage("正在申请控制会话…");
    try {
      const grant = await api.grantControl(uid, CONTROL_TTL_S);
      const next: ControlSession = {
        token: grant.token,
        expiresAtMs: grant.expires_at_ms,
        fps: grant.fps,
        quality: grant.quality,
      };
      sessionRef.current = next;
      setSession(next);
      setStatus("connecting");
      setMessage("正在建立实时通道…");

      // 控制观看 WS 的候选域名按序尝试（优先序非锁定）：不同设备/网络到
      // 各域名的质量不同，某个域名握不起手（超时/被 CDN 掐断）就切下一个，
      // 同源 host 永远是最后回退（2026-10-25 生产事故：国内 CDN 静默掐断
      // 长连 WS，单一同源连接必然周期性死）。
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const origins = [
        ...(grant.ws_bases ?? []).map((base) => base.replace(/^http/, "ws").replace(/\/+$/, "")),
        `${scheme}://${location.host}`,
      ].filter((value, index, all) => value && all.indexOf(value) === index);
      let originIndex = 0;
      let ready = false;

      const openSocket = (): void => {
        const socket = new WebSocket(
          `${origins[originIndex]}${grant.ws_path}?token=${encodeURIComponent(grant.token)}`,
        );
        socket.binaryType = "blob";
        socketRef.current = socket;
        // 握手看的是服务端 ready 帧；9 秒没等到就视为该域名不可用。
        const handshakeTimeout = window.setTimeout(() => {
          if (!ready) socket.close();
        }, 9000);

        socket.onmessage = (event) => {
          if (typeof event.data === "string") {
            try {
              const parsed = JSON.parse(event.data) as { type?: string };
              if (parsed.type === "ready") {
                ready = true;
                window.clearTimeout(handshakeTimeout);
                setStatus("streaming");
                setMessage("");
              } else if (parsed.type === "pong" && pingAtRef.current > 0) {
                setRttMs(performance.now() - pingAtRef.current);
                pingAtRef.current = 0;
              }
            } catch {
              /* one malformed control frame must not end the session */
            }
            return;
          }
          const blob = event.data as Blob;
          const nextUrl = URL.createObjectURL(blob);
          const previous = lastUrlRef.current;
          lastUrlRef.current = nextUrl;
          setFrameUrl(nextUrl);
          if (previous) window.setTimeout(() => URL.revokeObjectURL(previous), 1500);
          frameCountRef.current += 1;
        };

        socket.onclose = (event) => {
          window.clearTimeout(handshakeTimeout);
          if (socketRef.current !== socket) return;
          socketRef.current = null;
          if (!ready && originIndex + 1 < origins.length) {
            originIndex += 1;
            setMessage(`通道 ${origins[originIndex - 1]} 不可用，切换到 ${origins[originIndex]} …`);
            openSocket();
            return;
          }
          const active = sessionRef.current;
          sessionRef.current = null;
          // F41: 远端断开（受控端重启/通道丢失）也要撤销 grant——否则控制流
          // 残留到 TTL，设备一直被独占。
          if (active) void api.revokeControl(uid, active.token).catch(() => undefined);
          heldModifiersRef.current.clear();
          setSession(null);
          setFps(0);
          setStatus(event.code === 4409 ? "error" : "closed");
          setMessage(
            event.code === 4409
              ? "受控端当前没有实时通道，实时控制不可用（会显示为“仅轮询”）。"
              : "控制会话已结束",
          );
        };

        socket.onerror = () => {
          // Always close: a half-open socket would otherwise linger and a second
          // "重新连接" would stack another socket on top of it.
          socket.close();
        };
      };
      openSocket();
    } catch (failure) {
      sessionRef.current = null;
      setSession(null);
      setStatus("error");
      setMessage(isApiError(failure) ? failure.message : "无法开始控制会话");
    }
  };

  const normalized = (clientX: number, clientY: number) => {
    const stage = stageRef.current;
    if (!stage || !natural.width || !natural.height) return { x: 0, y: 0 };
    const rect = stage.getBoundingClientRect();
    const imageAspect = natural.width / natural.height;
    const boxAspect = rect.width / rect.height;
    let drawWidth = rect.width;
    let drawHeight = rect.height;
    if (boxAspect > imageAspect) {
      drawWidth = rect.height * imageAspect;
    } else {
      drawHeight = rect.width / imageAspect;
    }
    const offsetX = (rect.width - drawWidth) / 2;
    const offsetY = (rect.height - drawHeight) / 2;
    const x = (clientX - rect.left - offsetX) / drawWidth;
    const y = (clientY - rect.top - offsetY) / drawHeight;
    return {
      x: Math.round(Math.max(0, Math.min(1, x)) * NORM),
      y: Math.round(Math.max(0, Math.min(1, y)) * NORM),
    };
  };

  const onPointerMove = (event: React.PointerEvent) => {
    // 30 Hz input throttle: monotonic stopwatch, unrelated to the server clock.
    const stamp = performance.now();
    if (stamp - lastMoveRef.current < 33) return;
    lastMoveRef.current = stamp;
    const { x, y } = normalized(event.clientX, event.clientY);
    send({ type: "input", ev: { kind: "mouse", action: "move", x, y, norm: true } });
  };

  const onPointerDown = (event: React.PointerEvent) => {
    event.preventDefault();
    (event.target as HTMLElement).setPointerCapture?.(event.pointerId);
    const { x, y } = normalized(event.clientX, event.clientY);
    const button = event.button === 2 ? 3 : event.button === 1 ? 2 : 1;
    send({ type: "input", ev: { kind: "mouse", action: "move", x, y, norm: true } });
    send({ type: "input", ev: { kind: "mouse", action: "down", button, x, y, norm: true } });
  };

  const onPointerUp = (event: React.PointerEvent) => {
    const { x, y } = normalized(event.clientX, event.clientY);
    const button = event.button === 2 ? 3 : event.button === 1 ? 2 : 1;
    send({ type: "input", ev: { kind: "mouse", action: "up", button, x, y, norm: true } });
  };

  // F41: 滚轮只走下面的原生非 passive 监听。React 根节点的 wheel 是
  // passive 且与原生监听同帧触发——若在这里也 send，一次滚动会双发
  // （远端速度翻倍）。

  // F41: 原生非 passive wheel 监听——preventDefault 真正生效，控制画面
  // 滚轮不再同时滚动控制台页面。
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const handler = (event: WheelEvent) => {
      if (!sessionRef.current) return;
      event.preventDefault();
      send({ type: "input", ev: { kind: "wheel", delta: event.deltaY > 0 ? -1 : 1 } });
    };
    stage.addEventListener("wheel", handler, { passive: false });
    return () => stage.removeEventListener("wheel", handler);
  }, [send]);

  const MODIFIER_KEYS: Record<string, number> = {
    Control: 0x11,
    Alt: 0x12,
    Shift: 0x10,
    Meta: 0x5b,
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (!sessionRef.current) return;
    // Ctrl+Alt+Backspace is the console's secure-attention sequence.
    if (event.ctrlKey && event.altKey && event.key === "Backspace") {
      event.preventDefault();
      send({ type: "input", ev: { kind: "key", code: 0x2e, down: true } });
      send({ type: "input", ev: { kind: "key", code: 0x2e, down: false } });
      return;
    }
    // Printable characters travel as text so the remote layout decides the key.
    if (event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey) {
      event.preventDefault();
      send({ type: "input", ev: { kind: "text", text: event.key } });
      return;
    }
    const code = vkCodeFor(event);
    if (!code) return;
    event.preventDefault();
    if (event.key in MODIFIER_KEYS) {
      // F41: 修饰键按下只发 down；keyup 时才发 up。之前 down+up 立即成对
      // 发送，Ctrl+C 到远端只剩单独的 c——所有组合键实际不可用。
      if (event.repeat || heldModifiersRef.current.has(code)) return;
      heldModifiersRef.current.add(code);
      send({ type: "input", ev: { kind: "key", code, down: true } });
      return;
    }
    send({ type: "input", ev: { kind: "key", code, down: true } });
    send({ type: "input", ev: { kind: "key", code, down: false } });
  };

  const onKeyUp = (event: React.KeyboardEvent) => {
    if (!sessionRef.current) return;
    const code = vkCodeFor(event);
    if (!code) return;
    if (event.key in MODIFIER_KEYS) {
      if (!heldModifiersRef.current.delete(code)) return;
      send({ type: "input", ev: { kind: "key", code, down: false } });
    }
  };

  const sendText = (value: string) => {
    if (!value) return;
    send({ type: "input", ev: { kind: "text", text: value } });
  };

  const requestFullscreen = () => {
    const element = stageRef.current?.parentElement ?? stageRef.current;
    void element?.requestFullscreen?.();
  };

  if (issue) {
    const pollMode = isPollMode(device.presence);
    return (
      <div className={cn("min-w-0", watch ? "space-y-3" : "space-y-4")}>
        <div className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
          <p className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-2 text-sm font-medium text-warning">
            <span className="size-2 shrink-0 rounded-full bg-warning" aria-hidden />
            <span className="truncate">实时控制不可用</span>
          </p>
          <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center sm:gap-2">
            <Button disabled className="min-h-9" title={issue}>
              <MousePointer2 className="size-4" aria-hidden />
              开始实时控制
            </Button>
            <Button variant="outline" disabled className="min-h-9" title={issue}>
              <Expand className="size-4" aria-hidden />
              全屏
            </Button>
          </div>
        </div>

        <Alert variant="warning" className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-2">
          <WifiOff className="mt-0.5 size-4 shrink-0" aria-hidden />
          <div className="min-w-0">
            <AlertDescription className="break-words text-warning">{issue}</AlertDescription>
            <p className="mt-1 break-words text-xs leading-relaxed text-muted-foreground">
              {pollMode
                ? "轮询模式下截图、文件、命令与历史画面仍可用；受控端恢复实时通道后会自动可用。"
                : "截图、文件管理、命令下发与历史画面仍可正常使用。"}
            </p>
          </div>
        </Alert>
      </div>
    );
  }

  const secondsLeft =
    session && now !== null ? Math.max(0, Math.round((serverTimeLeft(session.expiresAtMs) ?? 0) / 1000)) : null;
  const streaming = status === "streaming";

  return (
    <div className={cn("min-w-0", watch ? "space-y-3" : "space-y-4")}>
      {/* Compact toolbar: status left, actions right; the stage sits directly below. */}
      <div className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
        <p
          className={cn(
            "grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-2 text-sm font-medium",
            STATUS_TONE[status],
          )}
          title={message || STATUS_LABEL[status]}
        >
          <span className={cn("size-2 shrink-0 rounded-full", STATUS_DOT[status])} aria-hidden />
          <span className="truncate" role="status" aria-live="polite">
            {message || STATUS_LABEL[status]}
          </span>
        </p>
        <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center sm:gap-2">
          {session ? (
            <Button
              variant="destructive"
              className="col-span-2 min-h-9 sm:col-span-1"
              onClick={() => stop("已结束控制会话")}
            >
              <Power className="size-4" aria-hidden />
              结束控制
            </Button>
          ) : (
            <Button
              className="col-span-2 min-h-9 sm:col-span-1"
              onClick={() => void start()}
              disabled={status === "granting" || status === "connecting"}
            >
              <MousePointer2 className="size-4" aria-hidden />
              开始实时控制
            </Button>
          )}
          <Button
            variant="outline"
            className="min-h-9"
            onClick={() => void start()}
            disabled={status === "granting" || status === "connecting"}
          >
            <RefreshCw
              className={cn("size-4", (status === "granting" || status === "connecting") && "animate-spin")}
              aria-hidden
            />
            重新连接
          </Button>
          <Button variant="outline" className="min-h-9" onClick={requestFullscreen}>
            <Expand className="size-4" aria-hidden />
            全屏
          </Button>
        </div>
      </div>

      <div className="min-w-0 rounded-md border border-border bg-card p-2 sm:p-2.5">
        <div
          className={cn(
            "relative flex min-w-0 justify-center",
            isFullscreen && "h-full w-full items-center",
          )}
          style={isFullscreen ? { width: "100%", height: "100%" } : undefined}
        >
          <div
            ref={stageRef}
            tabIndex={0}
            className={cn(
              "ratio-16-9 relative mx-auto block w-full touch-none select-none overflow-hidden bg-muted outline-none",
              "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
              session ? "cursor-crosshair" : "cursor-default",
              !isFullscreen && "rounded-md border border-border",
              watch && "max-h-[48vh]",
            )}
            style={stageSizeStyle}
            onPointerMove={onPointerMove}
            onPointerDown={onPointerDown}
            onPointerUp={onPointerUp}
            onKeyDown={onKeyDown}
            onKeyUp={onKeyUp}
            onContextMenu={(event) => event.preventDefault()}
            aria-label="实时画面与输入区域"
          >
            <FrameStage className="h-full w-full">
              {frameUrl ? (
                <img
                  src={frameUrl}
                  alt="实时画面"
                  draggable={false}
                  className="block h-full w-full object-contain"
                  onLoad={(event) => {
                    const image = event.currentTarget;
                    setNatural({ width: image.naturalWidth, height: image.naturalHeight });
                  }}
                />
              ) : (
                <div className="flex h-full w-full flex-col items-center justify-center bg-muted px-3 text-center">
                  <MousePointer2 className={cn("shrink-0 text-faint", watch ? "size-5" : "size-6")} aria-hidden />
                  <p className="mt-2 break-words text-sm text-muted-foreground">
                    {status === "idle" ? "点击“开始实时控制”建立会话" : message || "等待画面…"}
                  </p>
                </div>
              )}
            </FrameStage>
          </div>
        </div>
      </div>

      {/* Status strip: every number is server-clock / measured, tabular. */}
      <div className="stat-strip min-w-0 overflow-hidden rounded-md border border-border bg-card">
          <LiveMetric
            label="帧率"
            value={streaming ? String(fps) : "—"}
            unit="fps"
            hint={session ? `目标 ${session.fps} fps` : undefined}
            tone={streaming ? "text-success" : undefined}
          />
          <LiveMetric label="画质" value={session ? String(session.quality) : "—"} />
          <LiveMetric
            label="往返"
            value={rttMs === null ? "—" : rttMs.toFixed(0)}
            unit="ms"
            tone={rttMs !== null && rttMs > 400 ? "text-warning" : undefined}
          />
          <LiveMetric
            label="剩余"
            value={secondsLeft === null ? "—" : String(secondsLeft)}
            unit="s"
            hint="服务端时钟"
            tone={secondsLeft !== null && secondsLeft <= 60 ? "text-warning" : undefined}
          />
      </div>

      {/* Keyboard text row: on a watch the send button drops to its own row. */}
      <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-2 rounded-md border border-border bg-card p-2.5 sm:grid-cols-[auto_minmax(0,1fr)_auto]">
        <Keyboard className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        <Input
          placeholder="在此输入文字并按回车，直接发送到受控端"
          className="h-11 text-sm sm:h-9"
          aria-label="发送文字到受控端"
          value={textValue}
          onChange={(event) => setTextValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              sendText(textValue);
              setTextValue("");
            }
          }}
        />
        <Button
          variant="outline"
          className="col-span-2 min-h-9 sm:col-span-1"
          onClick={() => {
            sendText(textValue);
            setTextValue("");
          }}
        >
          发送
        </Button>
      </div>

      <p className="text-xs leading-relaxed text-faint">
        画面与输入经服务器中转并全程审计；点击画面聚焦后键盘快捷键才生效。
      </p>
    </div>
  );
}
