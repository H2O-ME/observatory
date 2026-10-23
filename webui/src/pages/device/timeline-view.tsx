import { W2Notice } from "@/components/w2-notice";
import { formatAge } from "@/lib/format";
import { presenceAgeMs } from "@/lib/presence";
import type { Device } from "@/lib/types";

/**
 * Timeline (W2 view rebuild).
 *
 * The route and the device context stay; the grid comes back in W2. When it
 * does, the window must be derived from `serverNow()` — the 0.2.x view built
 * "today" out of `Date.now()`, so a browser with a skewed clock asked for the
 * wrong day and showed an empty timeline that looked like "no frames".
 */
export function TimelineView({ device }: { device: Device }) {
  return (
    <W2Notice
      title="时间线视图（W2 重建）"
      description="0.3.0 的客户端层已就位；按桶聚合的画面网格在 W2 波次重建。"
      items={[
        "查询窗口将由 serverNow() 生成，不再使用浏览器时钟",
        `当前设备最近上报：${formatAge(presenceAgeMs(device.presence))}`,
        "画面 URL 仍为不可变 + immutable 缓存，重建后不需要 cache-buster",
      ]}
      testId="timeline-w2"
    />
  );
}