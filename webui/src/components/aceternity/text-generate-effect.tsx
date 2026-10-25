import type { CSSProperties } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export type TextGenerateTag = "div" | "span" | "p" | "h1" | "h2" | "h3" | "h4";

export interface TextGenerateEffectProps {
  words: string;
  className?: string;
  /** Blur the tokens in (Aceternity default) instead of a plain fade. */
  filter?: boolean;
  /** Per-token animation length in seconds. */
  duration?: number;
  /** Stagger between tokens in seconds. */
  stagger?: number;
  /** Element to render; defaults to `span`. */
  as?: TextGenerateTag;
  /** Extra class for every token, e.g. `text-gradient`. */
  wordClassName?: string;
}

const CJK =
  /[\u2e80-\u2fff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/;

interface Token {
  value: string;
  cjk: boolean;
}

/**
 * Split on whitespace, then break CJK runs into single characters.
 *
 * The old `words.split(" ")` treated a whole Chinese sentence as one token, so
 * "受控端控制台" blurred in as one block. Per-character tokens animate nicely
 * and stay `display: inline`, which keeps the browser's ability to break lines
 * between Han characters on a 320px watch.
 */
function tokenize(text: string): Token[] {
  const tokens: Token[] = [];
  for (const part of text.split(/(\s+)/)) {
    if (!part) continue;
    if (/^\s+$/.test(part)) {
      tokens.push({ value: part, cjk: false });
      continue;
    }
    if (CJK.test(part)) {
      for (const char of Array.from(part)) tokens.push({ value: char, cjk: true });
      continue;
    }
    tokens.push({ value: part, cjk: false });
  }
  return tokens;
}

/**
 * Aceternity text generate effect, CSS-only.
 *
 * Tokens use an `opacity`/`blur`/`transform` keyframe with a per-token delay —
 * no framer-motion, so nothing re-renders after mount, and reduced motion gets
 * plain, fully visible text (the animation is disabled, not the content).
 */
export function TextGenerateEffect({
  words,
  className,
  filter = true,
  duration = 0.55,
  stagger = 0.08,
  as = "span",
  wordClassName,
}: TextGenerateEffectProps) {
  const Tag = as;
  const tokens = tokenize(words);
  let visible = 0;

  return (
    <Tag className={cn("inline-block", className)}>
      {tokens.map((token, index) => {
        if (!/\S/.test(token.value)) {
          return (
            <span key={`space-${index}`} className="whitespace-pre">
              {token.value}
            </span>
          );
        }
        const delay = visible;
        visible += 1;
        return (
          <span
            key={`token-${index}`}
            className={cn(
              "ace-word",
              token.cjk && "ace-word--char",
              !filter && "ace-word--plain",
              wordClassName,
            )}
            style={
              {
                animationDelay: `${(delay * stagger).toFixed(3)}s`,
                animationDuration: `${Math.max(0.1, duration)}s`,
              } as CSSProperties
            }
          >
            {token.value}
          </span>
        );
      })}
    </Tag>
  );
}
