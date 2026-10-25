import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { ImageOff } from "lucide-react";

import { GridPattern } from "@/components/aceternity/grid-pattern";
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
 * Neutral placeholder for the two "there is no picture" states.
 *
 * Deliberately wordless about the cause: an `<img>` error cannot distinguish
 * "retention removed the file" from "the session expired", so both states get
 * the same calm icon + label instead of misleading operator advice.
 */
function FrameFallback({ label, className }: { label: string; className?: string }) {
  return (
    <span
      className={cn(
        "frame-stage relative flex h-full min-h-12 w-full flex-col items-center justify-center overflow-hidden bg-sunken text-center text-faint",
        className,
      )}
    >
      <GridPattern className="opacity-30" dots fade="radial" />
      <span className="relative flex size-7 shrink-0 items-center justify-center rounded-full border border-line bg-panel text-faint">
        <ImageOff className="size-3.5" aria-hidden="true" />
      </span>
      <span className="relative mt-1 px-2 text-[0.7rem] leading-tight">{label}</span>
    </span>
  );
}

/**
 * A captured frame.
 *
 * Uses the browser's image pipeline (and therefore its HTTP cache) instead of
 * fetching blobs in JS: frame URLs are immutable and served `immutable`, so a
 * thumbnail is downloaded once. Authentication rides on the HttpOnly session
 * cookie, which `<img>` sends on its own.
 */
export function FrameView({ uid, frameId, variant = "thumb", alt = "画面", className, onClick, eager }: Props) {
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
  }, [frameId, uid]);

  if (!frameId) {
    return <FrameFallback label="暂无画面" className={className} />;
  }

  if (failed) {
    return <FrameFallback label="画面不可用" className={className} />;
  }

  return (
    <img
      src={api.frameUrl(uid, frameId, variant)}
      alt={alt}
      loading={eager ? "eager" : "lazy"}
      decoding="async"
      draggable={false}
      onClick={onClick}
      onError={() => setFailed(true)}
      className={cn(
        "block h-full w-full object-contain",
        onClick && "cursor-zoom-in",
        className,
      )}
    />
  );
}

/** Neutral checkerboard stage every frame sits on. */
export function FrameStage({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "frame-stage relative min-w-0 overflow-hidden rounded-lg bg-sunken",
        className,
      )}
    >
      {children}
    </div>
  );
}
