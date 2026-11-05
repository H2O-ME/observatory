import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Bot, Check, Copy, Download, KeyRound, RefreshCw, TerminalSquare } from "lucide-react";

import { Banner, ErrorBlock } from "@/components/async-state";
import { MetaChip, PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/lib/api";

/**
 * Agent 接入（0.3.19）。
 *
 * 把控制台的全部能力交给本地 AI agent：长效 API 密钥（Bearer）、skill 包下载
 * （SKILL.md + scripts/obs.py）、全能力端点速查与快速上手。api_key 是 secret：
 * 服务端永不回显，本页只显示"已设置/未设置"，重置后**只展示一次**。
 */

type SkillStatus = {
  api_key_set: boolean;
  exec_enabled_default: boolean;
  version: string;
  console_host: string;
};

const ENDPOINTS: Array<{ group: string; rows: Array<[string, string, string]> }> = [
  {
    group: "设备与状态",
    rows: [
      ["GET", "/api/v1/devices", "设备列表（在线状态、版本、资源、最后心跳）"],
      ["GET", "/api/v1/devices/{uid}", "单设备详情（capabilities/state/sysinfo）"],
      ["PATCH", "/api/v1/devices/{uid}", "改备注 / 设备级设置覆盖 {settings:{…}}"],
      ["GET", "/api/v1/system", "服务端状态（磁盘、载波、数量）"],
      ["GET", "/api/v1/audit?limit=50", "审计：谁在何时对哪台设备做了什么"],
    ],
  },
  {
    group: "远程命令",
    rows: [
      ["POST", "/api/v1/devices/{uid}/commands", "下发命令（screenshot/sysinfo/exec/log_tail/reboot/…）"],
      ["GET", "/api/v1/devices/{uid}/commands/{id}", "轮询回执（终态 ok/error/timeout/cancelled）"],
      ["GET", "/api/v1/devices/{uid}/commands?limit=50", "命令流水"],
    ],
  },
  {
    group: "远程执行",
    rows: [
      ["POST", "/api/v1/devices/{uid}/commands", "type=exec：SYSTEM 身份执行 shell，payload {command, timeout_s}，必须 confirm:true"],
    ],
  },
  {
    group: "文件管理",
    rows: [
      ["POST", "/api/v1/devices/{uid}/files/op", "op: list/stat/mkdir/delete/rename/copy/move/search/drives/read/write/hash"],
    ],
  },
  {
    group: "截图与时间线",
    rows: [
      ["POST", "/api/v1/devices/{uid}/commands", "type=screenshot：立即截屏；定点截图用本地调度器在时刻 T 调用"],
      ["GET", "/api/v1/devices/{uid}/frames/latest", "最新一帧"],
      ["GET", "/api/v1/devices/{uid}/frames/{id}?variant=full", "下载帧图片"],
      ["GET", "/api/v1/devices/{uid}/timeline?from_ms&to_ms", "活动时间线"],
    ],
  },
  {
    group: "设置",
    rows: [
      ["GET / PATCH", "/api/v1/settings", "全局设置（secret 不回显）"],
      ["GET", "/api/v1/settings/effective/{uid}", "设备最终生效值"],
    ],
  },
];

const QUICK_START = `# 环境变量
export OBS_API_BASE=https://control.tianhw.top
export OBS_API_KEY=obs-xxxxxxxx...        # 本页生成

# 设备列表
curl -s -H "Authorization: Bearer $OBS_API_KEY" $OBS_API_BASE/api/v1/devices

# 远程执行（SYSTEM 身份，confirm 必须带）
curl -s -X POST -H "Authorization: Bearer $OBS_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"type":"exec","payload":{"command":"whoami","timeout_s":60},"confirm":true}' \\
  $OBS_API_BASE/api/v1/devices/OBS-xxxx/commands

# 轮询回执
curl -s -H "Authorization: Bearer $OBS_API_KEY" \\
  $OBS_API_BASE/api/v1/devices/OBS-xxxx/commands/<command_id>`;

function randomApiKey(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1600);
        } catch {
          setCopied(false);
        }
      }}
    >
      {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
      {copied ? "已复制" : label}
    </Button>
  );
}

export function AgentAccessPage() {
  const statusQuery = useQuery({ queryKey: ["agent-skill-status"], queryFn: api.getAgentSkillStatus });
  const status = statusQuery.data as SkillStatus | undefined;
  const [newKey, setNewKey] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  const baseUrl = useMemo(() => {
    const host = status?.console_host?.trim();
    return host ? `https://${host}` : window.location.origin;
  }, [status?.console_host]);

  const resetKey = async () => {
    setResetting(true);
    setResetError(null);
    try {
      const key = randomApiKey();
      await api.patchSettings({ api_key: key });
      setNewKey(key);
      await statusQuery.refetch();
    } catch (error) {
      setResetError(error instanceof Error ? error.message : "重置失败");
    } finally {
      setResetting(false);
    }
  };

  const download = async () => {
    setDownloading(true);
    setDownloadError(null);
    try {
      const { blob, filename } = await api.downloadAgentSkill();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : "下载失败");
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 pb-16 md:p-6">
      <PageHeader
        title="Agent 接入"
        description="把控制台的全部能力交给本地 AI agent：一把长效密钥调用所有 API，skill 包即装即用"
        meta={status ? <MetaChip tone="brand">v{status.version}</MetaChip> : undefined}
      />

      {statusQuery.isError && (
        <ErrorBlock message={statusQuery.error instanceof Error ? statusQuery.error.message : "加载状态失败"} />
      )}

      <div className="grid items-start gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <KeyRound className="size-4" /> API 密钥
            </CardTitle>
            {status && (
              <Badge variant={status.api_key_set ? "success" : "warning"}>
                {status.api_key_set ? "已设置" : "未设置（禁用）"}
              </Badge>
            )}
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <p className="text-sm text-muted-foreground">
              所有 console API 都接受 <code className="rounded bg-muted px-1">Authorization: Bearer &lt;key&gt;</code>
              （免密码登录、无登录限速、改密不吊销）。密钥只存服务端，不回显、不下发给受控端。
            </p>
            {newKey && (
              <div className="flex flex-col gap-2 rounded-lg border border-warning/40 bg-warning/10 p-3">
                <div className="font-mono text-xs break-all select-all">{newKey}</div>
                <div className="flex items-center gap-2">
                  <CopyButton value={newKey} label="复制密钥" />
                  <span className="text-xs text-muted-foreground">只显示这一次，立即保存</span>
                </div>
              </div>
            )}
            {resetError && <Banner tone="warn">{resetError}</Banner>}
            <div>
              <Button type="button" onClick={() => void resetKey()} disabled={resetting}>
                {resetting ? <RefreshCw className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
                {status?.api_key_set ? "重置密钥（旧密钥立即失效）" : "生成密钥"}
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Bot className="size-4" /> Skill 包
            </CardTitle>
            {status && <Badge variant="neutral">v{status.version}</Badge>}
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <p className="text-sm text-muted-foreground">
              里面是给本地 AI agent 读的 <code className="rounded bg-muted px-1">SKILL.md</code>（全部能力说明书）和
              <code className="rounded bg-muted px-1"> scripts/obs.py</code>（薄 CLI）。解压后把
              <code className="rounded bg-muted px-1"> observatory-agent-skill/</code> 放进 agent 的 skills 目录
              （ZCode：<code className="rounded bg-muted px-1">~/.zcode/skills/</code>）即可。
            </p>
            {downloadError && <Banner tone="warn">{downloadError}</Banner>}
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" onClick={() => void download()} disabled={downloading}>
                {downloading ? <RefreshCw className="size-4 animate-spin" /> : <Download className="size-4" />}
                下载 Skill 包（zip）
              </Button>
              <CopyButton value={baseUrl} label="复制控制台地址" />
            </div>
            {status && (
              <p className="text-xs text-muted-foreground">
                远程命令执行（SYSTEM）默认
                <Badge variant={status.exec_enabled_default ? "success" : "warning"} className="mx-1 align-middle">
                  {status.exec_enabled_default ? "已开启" : "未开启"}
                </Badge>
                ，单台可在设备设置里覆盖。
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <TerminalSquare className="size-4" /> 快速上手
          </CardTitle>
          <CopyButton value={QUICK_START} label="复制" />
        </CardHeader>
        <CardContent>
          <pre className="overflow-x-auto rounded-lg bg-muted p-3 font-mono text-xs leading-relaxed">{QUICK_START}</pre>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>端点速查（全部支持 Bearer 密钥）</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          {ENDPOINTS.map((section) => (
            <div key={section.group} className="flex flex-col gap-2">
              <div className="text-sm font-semibold">{section.group}</div>
              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full min-w-[560px] text-sm">
                  <tbody>
                    {section.rows.map(([method, path, purpose]) => (
                      <tr key={`${method}-${path}-${purpose}`} className="border-b last:border-b-0">
                        <td className="w-24 px-3 py-2 align-top font-mono text-xs text-muted-foreground">{method}</td>
                        <td className="px-3 py-2 align-top font-mono text-xs break-all">{path}</td>
                        <td className="px-3 py-2 align-top text-muted-foreground">{purpose}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

export default AgentAccessPage;
