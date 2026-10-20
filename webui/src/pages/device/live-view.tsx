import { useCallback, useEffect, useRef, useState } from "react";
import { Expand, Keyboard, MousePointer2, Power, RefreshCw, WifiOff } from "lucide-react";

import { FrameStage } from "@/components/frame-view";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import type { Device } from "@/lib/types";
import { cn } from "@/lib/utils";

const NORM = 10000;

interface Props {
  device: Device;
}

interface ControlState {
  status: "idle" | "granting" | "connecting" | "streaming" | "error" | "closed";
  message: string;
  fps: number;
  quality: number;
  expiresAt: number;
}

function mapButton(button: number): number {
  if (button === 2) return 3; // middle
  if (button === 1) return 2; // right
  return 1;
}

export function LiveView({ device }: Props) {
  const [state, setState] = useState<ControlState>({
    status: "idle",
    message: "",
    fps: 8,
    quality: 55,
    expiresAt: 0,
  });
  const [frameUrl, setFrameUrl] = useState("");
  const [natural, setNatural] = useState({ width: 0, height: 0 });
  const [frames, setFrames] = useState(0);
  const [latency, setLatency] = useState(0);
  const [textValue, setTextValue] = useState("");

  const socketRef = useRef<WebSocket | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const lastUrlRef = useRef("");
  const lastMoveRef = useRef(0);
  const frameCountRef = useRef(0);

  const capabilities = device.presence.capabilities;

  const send = useCallback((payload: Record<string, unknown>) => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
    }
  }, []);

  const stop = useCallback(() => {
    const socket = socketRef.current;
    socketRef.current = null;
    if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
    if (lastUrlRef.current) {
      URL.revokeObjectURL(lastUrlRef.current);
      lastUrlRef.current = "";
    }
    setFrameUrl("");
    setState((previous) => ({ ...previous, status: "idle", message: "" }));
  }, []);

  useEffect(() => () => stop(), [stop]);

  const start = async () => {
    setState({ status: "granting", message: "正在申请控制会话…", fps: 8, quality: 55, expiresAt: 0 });
    try {
      const grant = await api.controlGrant(device.uid, 600);
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const url = `${scheme}://${location.host}${grant.ws_path}?token=${encodeURIComponent(grant.token)}`;
      setState({
        status: "connecting",
        message: "正在建立实时通道…",
        fps: grant.fps,
        quality: grant.quality,
        expiresAt: grant.expires_at_ms,
      });

      const socket = new WebSocket(url);
      socket.binaryType = "blob";
      socketRef.current = socket;

      socket.onmessage = (event) => {
        if (typeof event.data === "string") {
          try {
            const message = JSON.parse(event.data) as { type?: string };
            if (message.type === "ready") {
              setState((previous) => ({
                ...previous,
                status: "streaming",
                message: "",
                expiresAt: grant.expires_at_ms,
              }));
            }
          } catch {
            /* ignore */
          }
          return;
        }
        const blob = event.data as Blob;
        const url = URL.createObjectURL(blob);
        const previous = lastUrlRef.current;
        setFrameUrl(url);
        lastUrlRef.current = url;
        if (previous) window.setTimeout(() => URL.revokeObjectURL(previous), 1500);
        frameCountRef.current += 1;
      };

      socket.onclose = (event) => {
        if (socketRef.current !== socket) return;
        socketRef.current = null;
        setState((previous) => ({
          ...previous,
          status: event.code === 4409 ? "error" : "closed",
          message:
            event.code === 4409
              ? "受控端当前没有 WebSocket 通道，实时控制不可用（会显示为“仅轮询”）。"
              : "控制会话已结束",
        }));
      };

      socket.onerror = () => {
        setState((previous) => ({ ...previous, status: "error", message: "控制通道发生错误" }));
      };
    } catch (failure) {
      setState({
        status: "error",
        message: failure instanceof ApiError ? failure.detail : "无法开始控制会话",
        fps: 8,
        quality: 55,
        expiresAt: 0,
      });
    }
  };

  // Frame rate + expiry watchdog.
  useEffect(() => {
    if (state.status !== "streaming") return;
    const timer = window.setInterval(() => {
      setFrames(frameCountRef.current);
      frameCountRef.current = 0;
      if (state.expiresAt && Date.now() > state.expiresAt) stop();
    }, 1000);
    return () => window.clearInterval(timer);
  }, [state.status, state.expiresAt, stop]);

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
    const now = performance.now();
    if (now - lastMoveRef.current < 33) return;
    lastMoveRef.current = now;
    const { x, y } = normalized(event.clientX, event.clientY);
    send({ type: "input", ev: { kind: "mouse", action: "move", x, y, norm: true } });
  };

  const onPointerDown = (event: React.PointerEvent) => {
    event.preventDefault();
    (event.target as HTMLElement).setPointerCapture?.(event.pointerId);
    const { x, y } = normalized(event.clientX, event.clientY);
    send({ type: "input", ev: { kind: "mouse", action: "move", x, y, norm: true } });
    send({ type: "input", ev: { kind: "mouse", action: "down", button: mapButton(event.button), x, y, norm: true } });
  };

  const onPointerUp = (event: React.PointerEvent) => {
    const { x, y } = normalized(event.clientX, event.clientY);
    send({ type: "input", ev: { kind: "mouse", action: "up", button: mapButton(event.button), x, y, norm: true } });
  };

  const onWheel = (event: React.WheelEvent) => {
    event.preventDefault();
    send({ type: "input", ev: { kind: "wheel", delta: event.deltaY > 0 ? -1 : 1 } });
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.ctrlKey && event.altKey && event.key === "Backspace") {
      event.preventDefault();
      send({ type: "input", ev: { kind: "key", code: 0x2E, down: true } });
      send({ type: "input", ev: { kind: "key", code: 0x2E, down: false } });
      return;
    }
    if (event.key.length === 1) {
      // Printable characters are sent as text so the remote layout decides the key.
      send({ type: "input", ev: { kind: "text", text: event.key } });
      event.preventDefault();
      return;
    }
    const code = event.keyCode || 0;
    if (code) {
      send({ type: "input", ev: { kind: "key", code, down: true } });
      send({ type: "input", ev: { kind: "key", code, down: false } });
      event.preventDefault();
    }
    setLatency(performance.now() - event.timeStamp);
  };

  const sendText = (value: string) => {
    if (!value) return;
    send({ type: "input", ev: { kind: "text", text: value } });
  };

  const requestFullscreen = () => {
    const element = stageRef.current?.parentElement ?? stageRef.current;
    void element?.requestFullscreen?.();
  };

  if (!capabilities.control) {
    return (
      <div className="space-y-2 rounded-panel border border-warn/30 bg-warn/10 p-3">
        <p className="flex items-center gap-1.5 text-xs font-medium text-warn">
          <WifiOff className="size-3.5" />
          实时控制不可用
        </p>
        <p className="text-[0.72rem] leading-relaxed text-warn/90">
          实时控制需要受控端保持 WebSocket 通道。当前状态：{device.presence.reason}
          {device.presence.transport === "poll" ? "（受控端已回退到 HTTPS 轮询）" : ""}
        </p>
        <p className="text-[0.68rem] leading-relaxed text-soft">
          轮询模式下仍然可以截图、管理文件、下发命令；受控端会在网络允许时自动重新升级到 WebSocket。
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {state.status === "streaming" || state.status === "connecting" ? (
          <Button size="sm" variant="destructive" onClick={stop}>
            <Power className="size-3.5" />
            结束控制
          </Button>
        ) : (
          <Button size="sm" onClick={() => void start()}>
            <MousePointer2 className="size-3.5" />
            开始实时控制
          </Button>
        )}
        <Button size="sm" variant="outline" onClick={() => void start()} disabled={state.status === "streaming"}>
          <RefreshCw className="size-3.5" />
          重新连接
        </Button>
        <Button size="sm" variant="outline" onClick={requestFullscreen}>
          <Expand className="size-3.5" />
          全屏
        </Button>
        <span
          className={cn(
            "ml-auto text-[0.66rem]",
            state.status === "streaming" ? "text-ok" : state.status === "error" ? "text-danger" : "text-faint",
          )}
        >
          {state.status === "streaming"
            ? `${frames} fps · 质量 ${state.quality} · 目标 ${state.fps} fps`
            : state.message || "未开始"}
        </span>
      </div>

      <div
        ref={stageRef}
        tabIndex={0}
        className="relative outline-none"
        onPointerMove={onPointerMove}
        onPointerDown={onPointerDown}
        onPointerUp={onPointerUp}
        onWheel={onWheel}
        onKeyDown={onKeyDown}
        onContextMenu={(event) => event.preventDefault()}
      >
        <FrameStage className="aspect-video w-full cursor-crosshair select-none touch-none">
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
            <div className="flex h-full w-full flex-col items-center justify-center gap-2 text-soft">
              <MousePointer2 className="size-6 text-faint" />
              <p className="text-[0.72rem]">
                {state.status === "idle" ? "点击“开始实时控制”建立会话" : state.message || "等待画面…"}
              </p>
            </div>
          )}
        </FrameStage>
      </div>

      <div className="flex items-center gap-1.5">
        <Keyboard className="size-3.5 shrink-0 text-soft" />
        <Input
          placeholder="在此输入文字并按回车，直接发送到受控端"
          className="h-9 text-xs"
          value={textValue}
          onChange={(event) => setTextValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              sendText(textValue);
              setTextValue("");
              event.preventDefault();
            }
          }}
        />
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            sendText(textValue);
            setTextValue("");
          }}
        >
          发送
        </Button>
      </div>

      <p className="text-[0.66rem] leading-relaxed text-faint">
        画面与输入都经过服务器中转，全程审计。会话 {Math.round((state.expiresAt ? Math.max(0, state.expiresAt - Date.now()) : 0) / 1000)} 秒后自动结束。
        {latency > 0 ? ` 键盘往返约 ${latency.toFixed(0)}ms。` : ""}
      </p>
    </div>
  );
}