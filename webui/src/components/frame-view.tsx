import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { ImageOff } from "lucide-react";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  uid: string;
  frameId: number | null | undefined;
  variant?: "thumb" | "full";
  alt?: string;
  className?: string;
  onClick?: () => void;
  eager?: boolean;
}

/**
 * A captured frame.
 *
 * Uses the browser's own image pipeline (and therefore its HTTP cache) instead of
 * fetching blobs in JS. Frame URLs are immutable, so a thumbnail is downloaded once
 * and reused while scrubbing — the previous implementation appended a timestamp to
 * every URL and re-downloaded everything on every interaction.
 */
export function FrameView({ uid, frameId, variant = "thumb", alt = "画面", className, onClick, eager }: Props) {
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
  }, [frameId, uid]);

  if (!frameId) {
    return (
      <div
        className={cn(
          "flex h-full w-full flex-col items-center justify-center gap-1 bg-sunken text-faint",
          className,
        )}
      >
        <ImageOff className="size-5" />
        <span className="text-[0.7rem]">暂无画面</span>
      </div>
    );
  }

  if (failed) {
    return (
      <div
        className={cn(
          "flex h-full w-full flex-col items-center justify-center gap-1 bg-sunken text-faint",
          className,
        )}
      >
        <ImageOff className="size-5" />
        <span className="text-[0.7rem]">画面已清理</span>
      </div>
    );
  }

  return (
    <img
      src={api.frameUrl(uid, frameId, variant)}
      alt={alt}
      loading={eager ? "eager" : "lazy"}
      decoding="async"
      onClick={onClick}
      onError={() => setFailed(true)}
      className={cn("block h-full w-full object-contain", onClick && "cursor-zoom-in", className)}
    />
  );
}

export function FrameStage({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("frame-stage relative overflow-hidden rounded-lg bg-sunken", className)}>{children}</div>;
}