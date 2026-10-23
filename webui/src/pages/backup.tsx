import { W2Notice } from "@/components/w2-notice";

/**
 * Archive / backup (A14: the archive feature stays, so the console needs an
 * entry point).
 *
 * W0 reserves the route and the navigation item so the feature has a home; the
 * actual backup status, retry queue and remote-purge controls are built in W2
 * together with `server-media`'s `frame_archives` table. Until then the page
 * says exactly that instead of showing invented counters.
 */
export function BackupPage() {
  return (
    <W2Notice
      title="归档备份（W2 重建）"
      description="归档功能保留（A14），控制台页面骨架已就位。"
      items={[
        "将展示 frame_archives 的提交队列、失败重试与可见告警",
        "删除设备时递归删除远端副本，失败进入重试队列，不再静默",
        "上传成功后才删除本地副本，状态由服务端提供",
      ]}
      testId="backup-w2"
    />
  );
}