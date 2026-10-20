import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useViewport } from "@/hooks/use-viewport";

const PAGE_SIZE = 40;

const ACTION_LABEL: Record<string, string> = {
  "auth.login": "登录",
  "auth.login_failed": "登录失败",
  "auth.logout": "退出登录",
  "auth.totp_enable": "开启二次验证",
  "auth.totp_disable": "关闭二次验证",
  "auth.password_change": "修改密码",
  "settings.update": "修改设置",
  "device.update": "修改设备",
  "device.delete": "删除设备",
  "device.uninstall": "远程卸载",
  "device.enroll": "设备注册",
  "command.create": "下发命令",
  "control.grant": "实时控制",
  "files.op": "文件操作",
  "package.upload": "上传安装包",
  "package.delete": "删除安装包",
};

export function AuditPage() {
  const viewport = useViewport();
  const [offset, setOffset] = useState(0);
  const [action, setAction] = useState("");

  const query = useQuery({
    queryKey: ["audit", action, offset],
    queryFn: () => api.audit({ limit: PAGE_SIZE, offset, action: action || undefined }),
  });

  const entries = query.data?.entries ?? [];
  const total = query.data?.total ?? 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h1 className="text-base font-semibold text-ink">审计日志</h1>
        <span className="text-[0.68rem] text-faint">共 {total} 条</span>
      </div>

      <div className="flex gap-2">
        <Input
          value={action}
          onChange={(event) => {
            setAction(event.target.value.trim());
            setOffset(0);
          }}
          placeholder="按动作过滤，例如 device.uninstall"
          className="h-8 text-xs"
        />
        <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
          刷新
        </Button>
      </div>

      {query.isLoading ? (
        <div className="space-y-1.5">
          {Array.from({ length: 8 }).map((_, index) => (
            <Skeleton key={index} className="h-12 w-full rounded-md" />
          ))}
        </div>
      ) : entries.length === 0 ? (
        <p className="rounded-panel border border-dashed border-line px-3 py-10 text-center text-xs text-soft">
          没有匹配的审计记录
        </p>
      ) : (
        <ul className="divide-y divide-line overflow-hidden rounded-panel border border-line bg-panel">
          {entries.map((entry) => (
            <li key={entry.id} className="px-3 py-2">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="text-[0.78rem] font-medium text-ink">
                    {ACTION_LABEL[entry.action] ?? entry.action}
                    {entry.device_name ? (
                      <span className="ml-1.5 text-[0.7rem] font-normal text-soft">· {entry.device_name}</span>
                    ) : null}
                  </p>
                  <p className="mt-0.5 truncate text-[0.64rem] text-faint">
                    {entry.actor} · {entry.ip || "—"}
                    {viewport !== "watch" && Object.keys(entry.detail).length > 0
                      ? ` · ${JSON.stringify(entry.detail).slice(0, 120)}`
                      : ""}
                  </p>
                </div>
                <span className="shrink-0 text-[0.62rem] text-faint">{formatDateTime(entry.ts_ms)}</span>
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="flex items-center justify-between gap-2">
        <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
          上一页
        </Button>
        <span className="text-[0.66rem] text-faint">
          {offset + 1}–{Math.min(offset + PAGE_SIZE, total)}
        </span>
        <Button
          variant="outline"
          size="sm"
          disabled={offset + PAGE_SIZE >= total}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}