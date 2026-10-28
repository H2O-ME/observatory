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

/** 触屏手势参数（对齐 Windows/安卓远程桌面的手感）。 */
const TOUCH_MOVE_SLOP_PX = 12;
const LONG_PRESS_MS = 450;
const DOUBLE_TAP_MS = 300;
const DOUBLE_TAP_SLOP_PX = 40;
const WHEEL_STEP_PX = 36;

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
  // W5: stage 节点也暴露成 state。初始不可控制（polling/offline）时 stage 根本
  // 没渲染，滚轮监听不能只依赖稳定的 send；节点出现/消失时 effect 需要重绑。
  const [stageNode, setStageNode] = useState<HTMLDivElement | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const sessionRef = useRef<ControlSession | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  // W1: 异步启动世代。stop()、设备切换、卸载都会推进它；grant 返回后只要
  // 世代对不上就说明这次启动已作废，必须撤销 grant、绝不能装回会话。
  const lifecycleGenRef = useRef(0);
  // W1: 卸载检查。世代号在 effect 清理里会推进，这里再保留一个显式的
  // "已卸载" 标记，异步返回后双重确认。
  const mountedRef = useRef(true);
  const lastUrlRef = useRef("");
  const lastMoveRef = useRef(0);
  const frameCountRef = useRef(0);
  const pingAtRef = useRef(0);
  // F41: 修饰键按下状态——keydown 对修饰键只发 down、keyup 补 up，组合键
  // （Ctrl+C / Alt+F4 / Win 组合）才可用；repeat 事件靠该集合去重。
  const heldModifiersRef = useRef<Set<number>>(new Set());
  // W4: 已按下但可能永远收不到 pointerup 的鼠标键；失焦/停止时补发 up。
  const heldButtonsRef = useRef<Set<number>>(new Set());
  // W4: 最后一次归一化坐标（0..NORM）。补发鼠标 up 必须带真实坐标——agent
  // 收到 mouse 事件会先移动光标，用 0,0 会把远端光标拖到左上角。
  const lastPointRef = useRef({ x: NORM / 2, y: NORM / 2 });
  // 触屏手势状态：单指点按/双击/长按拖拽、双指滚轮（与 Pointer 事件互斥）。
  const touchRef = useRef({
    startX: 0,
    startY: 0,
    lastX: 0,
    lastY: 0,
    moved: false,
    dragging: false,
    longPress: 0,
    lastTapAt: 0,
    lastTapX: 0,
    lastTapY: 0,
    twoFinger: false,
    wheelY: 0,
    wheelAccum: 0,
  });

  const uid = device.uid;
  const issue = controlUnavailableReason(device.presence);
  // W1: 授权请求在途时设备可能失去实时能力。这种情况下世代号不会变（没有
  // 调用 stop），但 start 的闭包拿到的是发起时的旧 issue，所以用 ref 读"当前"
  // 可用性，授权返回后再判一次。在 effect 里同步，避免渲染期写 ref。
  const issueRef = useRef(issue);
  useEffect(() => {
    issueRef.current = issue;
  }, [issue]);
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

  /**
   * W4：释放所有“本地已压下、但 up 帧可能永远收不到”的输入。
   *
   * 浏览器失焦、页面隐藏、停止会话或通道断开时，keyup / pointerup /
   * touchend 不会再派发到 stage；远端会把 Ctrl/Shift/Alt/Meta 或鼠标键一直
   * 按住（下次点击变成组合键/拖拽），直到 agent 侧兜底超时。这里主动补发
   * up 帧并清空本地状态。socket 已关时 send() 是 no-op，所以断线清理可以
   * 复用同一函数。
   */
  const releaseAllInputs = useCallback(() => {
    const point = lastPointRef.current;
    for (const button of heldButtonsRef.current) {
      send({ type: "input", ev: { kind: "mouse", action: "up", button, x: point.x, y: point.y, norm: true } });
    }
    heldButtonsRef.current.clear();

    // 触屏长按拖拽同样可能收不到 touchend。
    const touch = touchRef.current;
    if (touch.longPress) {
      window.clearTimeout(touch.longPress);
      touch.longPress = 0;
    }
    if (touch.dragging) {
      touch.dragging = false;
      send({ type: "input", ev: { kind: "mouse", action: "up", button: 1, x: point.x, y: point.y, norm: true } });
    }
    touch.twoFinger = false;
    touch.wheelAccum = 0;
    touch.moved = false;

    for (const code of heldModifiersRef.current) {
      send({ type: "input", ev: { kind: "key", code, down: false } });
    }
    heldModifiersRef.current.clear();
  }, [send]);

  const stop = useCallback(
    (nextMessage = "") => {
      // W1：任何停止（用户点击、失能、卸载、换设备）都让在途的 start 过期；
      // 它返回后走撤销分支，而不是把会话重新装回来。
      lifecycleGenRef.current += 1;
      // W4：先补发 up 帧再关 socket——停止/失焦时本地不会再产生 keyup。
      releaseAllInputs();
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
    [closeSocket, releaseAllInputs, uid],
  );

  // W1/W4：卸载（包括 keepMounted 下切换设备导致 stop 依赖变化）时标记
  // 已卸载并作废在途启动；releaseAllInputs 已经在 stop 里。
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      stop("");
    };
  }, [stop]);

  // W4：窗口失焦 / 页面隐藏时 keyup 与 pointerup 不会再来，统一补发释放帧。
  // 控制区域自身的失焦（Tab 离开 stage、点进文本输入框）在 JSX 的 onBlur 处理。
  useEffect(() => {
    const onBlur = () => releaseAllInputs();
    const onVisibility = () => {
      if (document.visibilityState === "hidden") releaseAllInputs();
    };
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [releaseAllInputs]);

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
    // W1：stop() 已推进过一次世代，这里再领一个本次启动专属的世代号。
    const generation = ++lifecycleGenRef.current;
    setStatus("granting");
    setMessage("正在申请控制会话…");
    // 记录已签发的 grant：任何失败/过时分支都要把它还给服务端，否则控制流
    // 会一直占着设备直到 TTL（600s）到期。
    let grantedToken = "";
    try {
      const grant = await api.grantControl(uid, CONTROL_TTL_S);
      grantedToken = grant.token;
      // W1：授权返回得比停止/卸载/换设备更晚。此时绝不能写 sessionRef、更
      // 不能开 WS；立即撤销 grant，避免"用户已经离开，远程控制却刚开始"。
      if (!mountedRef.current || generation !== lifecycleGenRef.current) {
        void api.revokeControl(uid, grantedToken).catch(() => undefined);
        return;
      }
      // W1：授权等待期间设备失去实时能力（presence 变化不推进世代）：同样
      // 撤销，绝不打开一条没人看得到的控制流。
      if (issueRef.current) {
        void api.revokeControl(uid, grantedToken).catch(() => undefined);
        setStatus("idle");
        setMessage(issueRef.current);
        return;
      }
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
          // W1：已被 stop()/新连接替换的旧 socket 迟到的帧不得再写回舞台。
          if (socketRef.current !== socket) return;
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
          // W4：断线后补发不了任何帧（send 是 no-op），但必须清空本地按下
          // 状态，否则重连后第一次按键会被当成 repeat 吞掉。
          releaseAllInputs();
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
      // 失败路径同样撤销：grant 可能已经签发，只是建 WS 时同步抛了错。
      if (grantedToken) void api.revokeControl(uid, grantedToken).catch(() => undefined);
      // 过时的失败不要改写当前 UI（可能已经是另一次启动/已回到 idle）。
      if (!mountedRef.current || generation !== lifecycleGenRef.current) return;
      closeSocket();
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
    const point = {
      x: Math.round(Math.max(0, Math.min(1, x)) * NORM),
      y: Math.round(Math.max(0, Math.min(1, y)) * NORM),
    };
    // W4：记录最后一次真实坐标。失焦补发 mouse up 时 agent 会先按这个坐标
    // 移动光标，不能是 0,0。
    lastPointRef.current = point;
    return point;
  };

  const onPointerMove = (event: React.PointerEvent) => {
    // 触屏手势统一走 touch 处理器：Pointer 与 Touch 对同一次触摸都会触发，
    // 两边都送会变成双倍输入。
    if (event.pointerType === "touch") return;
    // 30 Hz input throttle: monotonic stopwatch, unrelated to the server clock.
    const stamp = performance.now();
    if (stamp - lastMoveRef.current < 33) return;
    lastMoveRef.current = stamp;
    const { x, y } = normalized(event.clientX, event.clientY);
    send({ type: "input", ev: { kind: "mouse", action: "move", x, y, norm: true } });
  };

  const onPointerDown = (event: React.PointerEvent) => {
    if (event.pointerType === "touch") return;
    event.preventDefault();
    (event.target as HTMLElement).setPointerCapture?.(event.pointerId);
    const { x, y } = normalized(event.clientX, event.clientY);
    const button = event.button === 2 ? 3 : event.button === 1 ? 2 : 1;
    // W4：记下按下的键，失焦时补 up。
    heldButtonsRef.current.add(button);
    send({ type: "input", ev: { kind: "mouse", action: "move", x, y, norm: true } });
    send({ type: "input", ev: { kind: "mouse", action: "down", button, x, y, norm: true } });
  };

  const onPointerUp = (event: React.PointerEvent) => {
    if (event.pointerType === "touch") return;
    const { x, y } = normalized(event.clientX, event.clientY);
    const button = event.button === 2 ? 3 : event.button === 1 ? 2 : 1;
    heldButtonsRef.current.delete(button);
    send({ type: "input", ev: { kind: "mouse", action: "up", button, x, y, norm: true } });
  };

  const onPointerCancel = () => {
    // W4：指针被系统取消（触摸转滚动、窗口失焦等）时收不到 pointerup，
    // 统一走一次释放，避免远端鼠标键卡在按下状态。
    releaseAllInputs();
  };

  // -- 触屏手势（移动端远程桌面惯例）--------------------------------------
  //   单指点按         = 左键单击
  //   快速双击         = 左键双击（两次 down/up）
  //   单指滑动         = 只移动光标，不点击
  //   单指长按后拖动   = 按住左键拖拽，抬起结束
  //   双指上下滑       = 滚轮上/下
  // `touch-none` 已经在 stage 上关掉了浏览器的滚动与缩放，所以不需要
  // preventDefault，React 的合成 touch 事件够用。

  const sendMouseAt = (action: "move" | "down" | "up", clientX: number, clientY: number, button = 1) => {
    const { x, y } = normalized(clientX, clientY);
    send({ type: "input", ev: { kind: "mouse", action, x, y, button, norm: true } });
  };

  const cancelLongPress = () => {
    const touch = touchRef.current;
    if (touch.longPress) {
      window.clearTimeout(touch.longPress);
      touch.longPress = 0;
    }
  };

  const onTouchStart = (event: React.TouchEvent) => {
    if (!sessionRef.current) return;
    const touch = touchRef.current;
    if (event.touches.length >= 2) {
      cancelLongPress();
      if (touch.dragging) {
        touch.dragging = false;
        sendMouseAt("up", touch.lastX, touch.lastY);
      }
      touch.twoFinger = true;
      touch.wheelY = (event.touches[0].clientY + event.touches[1].clientY) / 2;
      touch.wheelAccum = 0;
      return;
    }
    const first = event.touches[0];
    touch.startX = touch.lastX = first.clientX;
    touch.startY = touch.lastY = first.clientY;
    touch.moved = false;
    touch.dragging = false;
    touch.twoFinger = false;
    cancelLongPress();
    touch.longPress = window.setTimeout(() => {
      touch.longPress = 0;
      touch.dragging = true;
      // 长按判定成功：先落点再按下左键，随后拖动沿用 move。
      sendMouseAt("move", touch.lastX, touch.lastY);
      sendMouseAt("down", touch.lastX, touch.lastY);
      navigator.vibrate?.(10);
    }, LONG_PRESS_MS);
  };

  const onTouchMove = (event: React.TouchEvent) => {
    if (!sessionRef.current) return;
    const touch = touchRef.current;
    if (touch.twoFinger) {
      if (event.touches.length < 2) return;
      const midpointY = (event.touches[0].clientY + event.touches[1].clientY) / 2;
      // 手指上滑 → 滚轮上滑（正值 = 远端向上滚）。
      touch.wheelAccum += touch.wheelY - midpointY;
      touch.wheelY = midpointY;
      const steps = Math.trunc(touch.wheelAccum / WHEEL_STEP_PX);
      if (steps !== 0) {
        touch.wheelAccum -= steps * WHEEL_STEP_PX;
        const delta = steps > 0 ? 1 : -1;
        // 一次最多补 3 格，避免快速滑动积累出巨大的滚轮跳变。
        for (let i = 0; i < Math.min(3, Math.abs(steps)); i += 1) {
          send({ type: "input", ev: { kind: "wheel", delta } });
        }
      }
      return;
    }
    const first = event.touches[0];
    if (!first) return;
    touch.lastX = first.clientX;
    touch.lastY = first.clientY;
    if (!touch.dragging && !touch.moved) {
      const dx = first.clientX - touch.startX;
      const dy = first.clientY - touch.startY;
      if (Math.hypot(dx, dy) > TOUCH_MOVE_SLOP_PX) {
        touch.moved = true;
        cancelLongPress();
      }
    }
    if (touch.dragging || touch.moved) {
      // 与鼠标一致的 30 Hz 节流。
      const stamp = performance.now();
      if (stamp - lastMoveRef.current < 33) return;
      lastMoveRef.current = stamp;
      sendMouseAt("move", touch.lastX, touch.lastY);
    }
  };

  const onTouchEnd = (event: React.TouchEvent) => {
    const touch = touchRef.current;
    if (touch.twoFinger) {
      if (event.touches.length < 2) {
        touch.twoFinger = false;
        touch.wheelAccum = 0;
        // 剩余手指抬起时不能再当点按处理。
        touch.moved = true;
      }
      return;
    }
    cancelLongPress();
    if (!sessionRef.current) return;
    if (touch.dragging) {
      touch.dragging = false;
      sendMouseAt("up", touch.lastX, touch.lastY);
      return;
    }
    if (touch.moved) return;
    const nowStamp = performance.now();
    const isDouble =
      nowStamp - touch.lastTapAt <= DOUBLE_TAP_MS &&
      Math.abs(touch.lastX - touch.lastTapX) <= DOUBLE_TAP_SLOP_PX &&
      Math.abs(touch.lastY - touch.lastTapY) <= DOUBLE_TAP_SLOP_PX;
    stageRef.current?.focus();
    if (isDouble) {
      touch.lastTapAt = 0;
      sendMouseAt("move", touch.lastX, touch.lastY);
      sendMouseAt("down", touch.lastX, touch.lastY);
      sendMouseAt("up", touch.lastX, touch.lastY);
      sendMouseAt("down", touch.lastX, touch.lastY);
      sendMouseAt("up", touch.lastX, touch.lastY);
      return;
    }
    touch.lastTapAt = nowStamp;
    touch.lastTapX = touch.lastX;
    touch.lastTapY = touch.lastY;
    sendMouseAt("move", touch.lastX, touch.lastY);
    sendMouseAt("down", touch.lastX, touch.lastY);
    sendMouseAt("up", touch.lastX, touch.lastY);
  };

  const onTouchCancel = () => {
    // W4：触摸被系统取消（来电、通知、手势冲突）时 touchend 不会再来，
    // 用统一释放清掉长按拖拽与双指状态。
    releaseAllInputs();
  };

  // F41: 滚轮只走下面的原生非 passive 监听。React 根节点的 wheel 是
  // passive 且与原生监听同帧触发——若在这里也 send，一次滚动会双发
  // （远端速度翻倍）。

  // F41: 原生非 passive wheel 监听——preventDefault 真正生效，控制画面
  // 滚轮不再同时滚动控制台页面。
  //
  // W5：effect 依赖 stageNode 而不是在挂载时读 stageRef.current。设备处于
  // offline/polling 时 stage 根本不渲染（上面的早期 return），此时读到的
  // 是 null；设备恢复实时能力后 stage 才出现，旧的 effect 已经结束，监听
  // 永远装不上——滚轮失效。反过来 WS→polling 时 stage 被移除，监听会留在
  // 已移除的节点上。做成 callback ref + effect 依赖节点本身，节点出现即
  // 绑定、消失即解绑。
  useEffect(() => {
    const stage = stageNode;
    if (!stage) return;
    const handler = (event: WheelEvent) => {
      if (!sessionRef.current) return;
      event.preventDefault();
      send({ type: "input", ev: { kind: "wheel", delta: event.deltaY > 0 ? -1 : 1 } });
    };
    stage.addEventListener("wheel", handler, { passive: false });
    return () => stage.removeEventListener("wheel", handler);
  }, [send, stageNode]);

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

  // W5：callback ref。stage 只在设备具备实时能力时渲染，用 state 记录节点，
  // 让 wheel effect 随节点出现/消失重新绑定；stageRef 仍然保留给坐标换算。
  const attachStage = useCallback((node: HTMLDivElement | null) => {
    stageRef.current = node;
    setStageNode(node);
  }, []);

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
            ref={attachStage}
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
            onPointerCancel={onPointerCancel}
            onTouchStart={onTouchStart}
            onTouchMove={onTouchMove}
            onTouchEnd={onTouchEnd}
            onTouchCancel={onTouchCancel}
            onKeyDown={onKeyDown}
            onKeyUp={onKeyUp}
            // W4：控制区域失去焦点（Tab 移走、点进下方文本框、窗口切走）时
            // keyup 会落在别的元素上，必须主动释放已按下的键与鼠标键。
            onBlur={releaseAllInputs}
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
        画面与输入经服务器中转并全程审计；点击画面聚焦后键盘快捷键才生效。移动端：点按 = 单击，双击 = 双击，长按后拖动 =
        拖拽，双指上下滑 = 滚轮。
      </p>
    </div>
  );
}
