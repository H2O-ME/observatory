import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, RotateCcw, Save, TriangleAlert } from "lucide-react";

import { Banner, ErrorBlock, LoadingBlock, PageHeader } from "@/components/async-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api, isApiError } from "@/lib/api";
import type { Device, SettingSpec } from "@/lib/types";
import { cn } from "@/lib/utils";

const INHERIT = "__inherit__";

function showValue(value: unknown): string {
  if (value === undefined || value === null || value === "") return "（空）";
  if (typeof value === "boolean") return value ? "开" : "关";
  return String(value);
}

/**
 * Per-device settings.
 *
 * Two different numbers exist for every key and the page shows both:
 *  - 全局默认: the stored global value (`GET /settings` → `values`);
 *  - 本设备有效: the server-computed effective view (`GET /settings/effective/{uid}`).
 * An input left alone means "inherit"; a filled input becomes an override sent
 * through `PATCH /devices/{uid}` (`settings`), which the server stores on the
 * device row and immediately pushes to a connected agent.
 *
 * Server limit (reported as G7): `PATCH /devices/{uid}` can set an override but
 * cannot delete one. The page says so instead of pretending "restore default"
 * works.
 */
export function DeviceSettingsView({ device }: { device: Device }) {
  const queryClient = useQueryClient();
  const uid = device.uid;

  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: api.getSettings, staleTime: 30_000 });
  const effectiveQuery = useQuery({
    queryKey: ["device-settings", uid],
    queryFn: () => api.getEffectiveSettings(uid),
  });

  const [edits, setEdits] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const specs = settingsQuery.data?.specs ?? {};
  const globalValues = settingsQuery.data?.values ?? {};
  const effectiveValues = effectiveQuery.data?.settings ?? {};

  const overridable = useMemo(
    () =>
      Object.entries(specs)
        .filter(([, spec]) => spec.per_device && !spec.secret)
        .sort(([a], [b]) => a.localeCompare(b)),
    [specs],
  );
  const globalOnly = useMemo(
    () =>
      Object.entries(specs)
        .filter(([, spec]) => !spec.per_device && !spec.secret)
        .map(([key]) => key)
        .sort(),
    [specs],
  );

  const editCount = Object.keys(edits).length;

  const setEdit = (key: string, value: unknown) => {
    setEdits((current) => {
      const next = { ...current };
      if (value === INHERIT || value === "" || value === undefined) delete next[key];
      else next[key] = value;
      return next;
    });
  };

  const save = async () => {
    const values: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(edits)) {
      const spec = specs[key];
      if (!spec) continue;
      if (spec.kind === "int") {
        if (value === "" || value === null || value === undefined) {
          setError(`${spec.description || key} 不能为空`);
          return;
        }
        const number = Number(value);
        if (!Number.isFinite(number)) {
          setError(`${spec.description || key} 必须是整数`);
          return;
        }
        values[key] = Math.trunc(number);
      } else {
        values[key] = value;
      }
    }
    if (Object.keys(values).length === 0) {
      setMessage("没有需要保存的覆盖");
      return;
    }
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api.patchDevice(uid, { settings: values });
      setMessage(
        `已保存 ${Object.keys(values).length} 项本设备覆盖；在线受控端会立即收到新配置，离线设备会在重新上线后收到。`,
      );
      setEdits({});
      await queryClient.invalidateQueries({ queryKey: ["device", uid] });
      await queryClient.invalidateQueries({ queryKey: ["device-settings", uid] });
      await queryClient.invalidateQueries({ queryKey: ["devices"] });
    } catch (failure) {
      setError(isApiError(failure) ? failure.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  const isOverridden = (key: string) =>
    Object.prototype.hasOwnProperty.call(effectiveValues, key) &&
    JSON.stringify(effectiveValues[key]) !== JSON.stringify(globalValues[key]);

  return (
    <div className="space-y-3">
      <PageHeader
        title="本设备设置"
        subtitle={
          <>
            只影响 <span className="font-mono">{device.name || uid}</span>；全局默认请到{" "}
            <a href="/settings" className="text-brand no-underline">
              设置
            </a>{" "}
            页修改。保存后服务端会立即推送配置给在线受控端。
          </>
        }
        actions={
          <>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setEdits({});
                setMessage("已放弃未保存的覆盖");
                setError("");
              }}
              disabled={busy || editCount === 0}
            >
              <RotateCcw className="size-3.5" />
              放弃修改
            </Button>
            <Button size="sm" onClick={() => void save()} disabled={busy || editCount === 0}>
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
              保存覆盖{editCount > 0 ? `（${editCount} 项）` : ""}
            </Button>
          </>
        }
      />

      {message ? <Banner tone="ok">{message}</Banner> : null}
      {error ? <Banner tone="error">{error}</Banner> : null}

      <Banner tone="info">
        每一行左侧是<b className="mx-0.5">全局默认</b>，右侧是<b className="mx-0.5">本设备当前有效值</b>。
        输入框留空表示“继承全局”；填了值才会写入本设备覆盖。
      </Banner>

      {settingsQuery.isError || effectiveQuery.isError ? (
        <ErrorBlock
          message={
            isApiError(settingsQuery.error) || isApiError(effectiveQuery.error)
              ? (isApiError(settingsQuery.error) ? settingsQuery.error.message : (effectiveQuery.error as never))
              : "无法读取设备设置"
          }
          retryable
          onRetry={() => {
            void settingsQuery.refetch();
            void effectiveQuery.refetch();
          }}
        />
      ) : settingsQuery.isLoading || effectiveQuery.isLoading ? (
        <LoadingBlock label="正在读取设置与生效值…" rows={6} />
      ) : (
        <>
          <section className="rounded-panel border border-line bg-panel p-4">
            <h2 className="mb-2 text-sm font-medium text-soft">可按设备覆盖（{overridable.length} 项）</h2>
            <div className="space-y-3">
              {overridable.map(([key, spec]) => (
                <DeviceSettingRow
                  key={key}
                  name={key}
                  spec={spec}
                  globalValue={globalValues[key]}
                  effectiveValue={effectiveValues[key]}
                  overridden={isOverridden(key)}
                  editValue={edits[key]}
                  onChange={(value) => setEdit(key, value)}
                  onFillGlobal={() => {
                    const value = globalValues[key];
                    if (spec.kind === "bool") setEdit(key, value ? "true" : "false");
                    else setEdit(key, typeof value === "number" ? value : String(value ?? ""));
                  }}
                />
              ))}
            </div>
          </section>

          <section className="rounded-panel border border-line bg-panel p-4">
            <h2 className="mb-1.5 flex items-center gap-1.5 text-sm font-medium text-soft">
              <TriangleAlert className="size-3.5 text-warn" />
              只支持全局的设置（{globalOnly.length} 项）
            </h2>
            <p className="text-[0.7rem] leading-relaxed text-soft">
              以下设置不支持按设备覆盖，只能在设置页修改：
            </p>
            <p className="mt-1 break-all font-mono text-[0.7rem] leading-relaxed text-faint">
              {globalOnly.join(" · ") || "—"}
            </p>
          </section>

          <section className="rounded-panel border border-line bg-panel p-4">
            <h2 className="mb-1.5 text-sm font-medium text-soft">已知限制</h2>
            <p className="text-[0.7rem] leading-relaxed text-soft">
              服务端当前只支持“写入/覆盖”单设备设置，<b>不支持删除某条覆盖</b>（`PATCH /devices/{"{uid}"}` 没有删除分支）。
              要回到全局行为，可点该行的“填全局值”再保存（这会留下一条与全局相同的覆盖），或等 server-console 提供删除接口。
            </p>
          </section>
        </>
      )}
    </div>
  );
}

function DeviceSettingRow({
  name,
  spec,
  globalValue,
  effectiveValue,
  overridden,
  editValue,
  onChange,
  onFillGlobal,
}: {
  name: string;
  spec: SettingSpec;
  globalValue: unknown;
  effectiveValue: unknown;
  overridden: boolean;
  editValue: unknown;
  onChange: (value: unknown) => void;
  onFillGlobal: () => void;
}) {
  const id = `device-setting-${name}`;
  const effectiveText = showValue(effectiveValue);

  return (
    <div className="grid gap-1.5 border-b border-line/60 pb-2.5 last:border-b-0 last:pb-0 sm:grid-cols-[minmax(0,1fr)_minmax(0,20rem)] sm:items-center sm:gap-3">
      <div className="min-w-0">
        <p className="flex flex-wrap items-center gap-1.5 text-xs text-ink">
          <label htmlFor={id}>{spec.description || name}</label>
          {overridden ? (
            <span className="rounded border border-brand/30 bg-brand/10 px-1 py-0.5 text-[0.7rem] text-brand">
              本设备已覆盖
            </span>
          ) : (
            <span className="rounded border border-line bg-sunken px-1 py-0.5 text-[0.7rem] text-faint">
              继承全局
            </span>
          )}
        </p>
        <p className="text-[0.7rem] text-faint">
          <span className="font-mono">{name}</span> · 全局默认{" "}
          <span className="font-mono text-soft">{showValue(globalValue)}</span> · 本设备有效{" "}
          <span className={cn("font-mono", overridden ? "text-brand" : "text-soft")}>{effectiveText}</span>
          {spec.kind === "int" && (spec.min !== null || spec.max !== null)
            ? ` · 范围 ${spec.min ?? "—"}..${spec.max ?? "—"}`
            : ""}
        </p>
      </div>

      <div className="flex items-center gap-1.5">
        {spec.kind === "bool" ? (
          <Select
            value={editValue === undefined ? INHERIT : String(editValue)}
            onValueChange={(value) => onChange(String(value) === INHERIT ? INHERIT : String(value))}
          >
            <SelectTrigger id={id} size="sm" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={INHERIT}>继承全局（{showValue(globalValue)}）</SelectItem>
              <SelectItem value="true">覆盖为：开</SelectItem>
              <SelectItem value="false">覆盖为：关</SelectItem>
            </SelectContent>
          </Select>
        ) : spec.kind === "choice" ? (
          <Select
            value={editValue === undefined ? INHERIT : String(editValue)}
            onValueChange={(value) => onChange(String(value) === INHERIT ? INHERIT : String(value))}
          >
            <SelectTrigger id={id} size="sm" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={INHERIT}>继承全局（{showValue(globalValue)}）</SelectItem>
              {spec.choices.map((choice) => (
                <SelectItem key={choice} value={choice}>
                  覆盖为：{choice}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <Input
            id={id}
            type={spec.kind === "int" ? "number" : "text"}
            inputMode={spec.kind === "int" ? "numeric" : undefined}
            value={editValue === undefined ? "" : String(editValue)}
            placeholder={`继承全局：${showValue(globalValue)}`}
            onChange={(event) =>
              onChange(event.target.value === "" ? INHERIT : spec.kind === "int" ? Number(event.target.value) : event.target.value)
            }
            className="h-9"
          />
        )}
        <Button
          size="icon-sm"
          variant="ghost"
          title="填入全局默认值（保存后成为覆盖）"
          aria-label={`填入 ${name} 的全局默认值`}
          onClick={onFillGlobal}
        >
          <Check className="size-3.5" />
        </Button>
      </div>
    </div>
  );
}