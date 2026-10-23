import { W2Notice } from "@/components/w2-notice";
import { capabilitiesOf } from "@/lib/presence";
import type { Device } from "@/lib/types";

/**
 * Files (W2 view rebuild).
 *
 * The capability gate already comes from the server (`presence.capabilities.files`),
 * which is the A13 requirement. The drive capacity contract is encoded in
 * `lib/types.ts` (`FileEntry.free` / `FileEntry.filesystem`); the W2 view must
 * render `—` when the server omits them instead of formatting `undefined` as
 * `0 B`, which is what the 0.2.x page did.
 */
export function FilesView({ device }: { device: Device }) {
  const capabilities = capabilitiesOf(device.presence);
  return (
    <W2Notice
      title="文件管理（W2 重建）"
      description={
        capabilities.files
          ? "服务端 capability 允许文件管理；分片读写与目录网格在 W2 波次重建。"
          : `服务端 capability 关闭了文件管理：${device.presence.reason}`
      }
      items={[
        "能力门控直接读 presence.capabilities.files，不再自行判断在线",
        "驱动器容量读取 free/filesystem，缺字段显示 — 而不是 0 B",
        "分片读写沿用同一个客户端（deadline 预算 + 取消），不再被固定 20s 杀死",
      ]}
      testId="files-w2"
    />
  );
}