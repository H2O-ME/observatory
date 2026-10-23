import { W2Notice } from "@/components/w2-notice";

/**
 * Updates (W2 view rebuild).
 *
 * The upload client already exists in the single API module
 * (`api.uploadPackage`): long budget, progress callback, AbortSignal. The W2
 * page wires it to a progress bar; W0 does not expose a half-working uploader.
 */
export function UpdatesPage() {
  return (
    <W2Notice
      title="版本与更新（W2 重建）"
      description="自动更新链路在服务端/受控端侧保持；控制台的上传与版本列表在 W2 波次重建。"
      items={[
        "上传走独立客户端：长超时 + 进度 + 可取消，不再受命令 deadline 约束",
        "最新版本、待升级设备与包校验信息来自服务器状态",
        "上传成功后由服务端事件驱动刷新，不靠本地推断",
      ]}
      testId="updates-w2"
    />
  );
}