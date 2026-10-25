import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface TracingBeamProps {
  className?: string;
  children?: ReactNode;
  /** Distance of the sticky node from the top of the scrollport, in px. */
  nodeOffset?: number;
}

/**
 * Aceternity tracing beam, without the scroll listener.
 *
 * A two-column grid: a gradient rail with a light pulse travelling down it
 * (`background-position` keyframes, compositor-friendly) and a node made
 * `position: sticky`, so it parks in view while the content scrolls instead of
 * being recalculated in JS.
 *
 * The rail is the first grid column, so it moves to the right edge in RTL
 * documents without any hardcoded side.
 */
export function TracingBeam({ className, children, nodeOffset }: TracingBeamProps) {
  const nodeStyle =
    nodeOffset == null
      ? undefined
      : ({ top: `${Math.max(0, nodeOffset)}px` } as CSSProperties);

  return (
    <div className={cn("ace-tracing", className)}>
      <div className="ace-tracing__rail" aria-hidden="true">
        <div className="ace-tracing__node" style={nodeStyle} />
      </div>
      <div className="ace-tracing__content">{children}</div>
    </div>
  );
}
