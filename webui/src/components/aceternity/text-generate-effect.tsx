import { motion } from "framer-motion";

import { cn } from "@/lib/utils";

interface TextGenerateEffectProps {
  words: string;
  className?: string;
  /** 每个词的入场过滤器动画；Chromium 81 支持 filter 动画。 */
  filter?: boolean;
  duration?: number;
}

/**
 * Aceternity Text Generate Effect：逐词模糊浮现的标题。
 * 登录页与空状态用。
 */
export function TextGenerateEffect({
  words,
  className,
  filter = true,
  duration = 0.5,
}: TextGenerateEffectProps) {
  const tokens = words.split(" ");
  return (
    <div className={cn("inline-block", className)}>
      {tokens.map((token, index) => (
        <motion.span
          key={`${token}-${index}`}
          initial={{ opacity: 0, filter: filter ? "blur(10px)" : "none" }}
          animate={{ opacity: 1, filter: "blur(0px)" }}
          transition={{ duration, delay: index * 0.08 }}
          className="inline-block whitespace-pre"
        >
          {token}
          {index < tokens.length - 1 ? " " : ""}
        </motion.span>
      ))}
    </div>
  );
}
