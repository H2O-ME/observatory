import { W2Notice } from "@/components/w2-notice";
import type { Device } from "@/lib/types";

/**
 * Command history (W2 view rebuild).
 *
 * Commands are created from the overview page in W0 (screenshot / reload /
 * restart / uninstall); the list view returns in W2 and will read the same
 * `Command` DTO, including the new `attempts` and `error_code` fields.
 */
export function CommandsView({ device }: { device: Device }) {
  return (
    <W2Notice
      title="命令记录（W2 重建）"
      description="命令创建路径在 0.3.0 收敛为一条总线；记录列表在 W2 波次重建。"
      items={[
        `设备标识：${device.uid}`,
        "将展示 attempts / error_code 与重投历史",
        "状态来自服务端 Command.status，不在前端推断完成",
      ]}
      testId="commands-w2"
    />
  );
}