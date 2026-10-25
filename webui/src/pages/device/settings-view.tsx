import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Lock, RotateCcw, Save, SlidersHorizontal, TriangleAlert } from "lucide-react";

import { GlowCard, ShimmerButton } from "@/components/aceternity";
import { Banner, EmptyBlock, ErrorBlock, LoadingBlock } from "@/components/async-state";
import { MetaChip } from "@/components/page-header";
import { Section, SectionHeader } from "@/components/section";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useViewport } from "@/hooks/use-viewport";
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
 *
 * Presentation (0.4): responsive override rows (values left, control right,
 * stacked on phones/watches) and a save bar that floats above the dock so the
 * primary action is always reachable.
 */
export function DeviceSettingsView({ device }: { device: Device }) {
  const queryClient = useQueryClient();
  const viewport = useViewport();
  const desktop = viewport === "desktop";
  const watch = viewport === "watch";
  const touch = !desktop;
  const stack = watch ? "space-y-3" : "space-y-5";
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
  const canSave = !busy && editCount > 0;

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

  const loadError =
    settingsQuery.isError || effectiveQuery.isError
      ? isApiError(settingsQuery.error)
        ? settingsQuery.error.message
        : isApiError(effectiveQuery.error)
          ? effectiveQuery.error.message
          : "无法读取设备设置"
      : "";
  const loading = settingsQuery.isLoading || effectiveQuery.isLoading;

  return (
    <div className="w-full min-w-0">
      <div className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:gap-3">
        <p className="min-w-0 text-[0.78rem] leading-relaxed text-soft">
          只影响 <span className="font-mono break-all">{device.name || uid}</span>，留空 = 继承全局；全局默认在{" "}
          <Link to="/settings" className="text-brand no-underline hover:underline">
            设置
          </Link>{" "}
          页修改。
        </p>
        <div className="grid min-w-0 grid-cols-2 gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setEdits({});
                setMessage("已放弃未保存的覆盖");
                setError("");
              }}
              disabled={!canSave}
            >
              <RotateCcw className="size-3.5" />
              放弃修改
            </Button>
            <ShimmerButton busy={busy} disabled={!canSave} onClick={() => void save()}>
              <Save className="mr-1.5 size-4" />
              保存覆盖{editCount > 0 ? `（${editCount} 项）` : ""}
            </ShimmerButton>
        </div>
      </div>

      <div className={cn("mt-4 min-w-0 sm:mt-5", stack)}>
        {message ? <Banner tone="ok">{message}</Banner> : null}
        {error ? (
          <Banner tone="error" title="保存失败">
            {error}
          </Banner>
        ) : null}

        {loadError ? (
          <ErrorBlock
            message={loadError}
            retryable
            onRetry={() => {
              void settingsQuery.refetch();
              void effectiveQuery.refetch();
            }}
          />
        ) : loading ? (
          <LoadingBlock label="正在读取设置与生效值…" rows={6} />
        ) : (
          <>
            <GlowCard isStatic={!desktop} className="min-w-0" contentClassName="p-3 sm:p-5">
              <SectionHeader
                title="可按设备覆盖"
                hint="留空 = 继承全局"
                icon={SlidersHorizontal}
                actions={<MetaChip tone="brand">{overridable.length} 项</MetaChip>}
              />
              {overridable.length === 0 ? (
                <div className="mt-3">
                  <EmptyBlock
                    title="没有可按设备覆盖的设置"
                    description="服务端当前没有声明 per_device 的设置键；全部配置都在全局设置页维护。"
                    action={
                      <Link
                        to="/settings"
                        className={cn(
                          buttonVariants({ variant: "outline", size: "sm" }),
                          "h-11 no-underline sm:h-9",
                        )}
                      >
                        打开全局设置
                      </Link>
                    }
                  />
                </div>
              ) : (
                <div className="mt-2 grid min-w-0">
                  {overridable.map(([key, spec]) => (
                    <DeviceSettingRow
                      key={key}
                      name={key}
                      spec={spec}
                      globalValue={globalValues[key]}
                      effectiveValue={effectiveValues[key]}
                      overridden={isOverridden(key)}
                      editValue={edits[key]}
                      touch={touch}
                      onChange={(value) => setEdit(key, value)}
                      onFillGlobal={() => {
                        const value = globalValues[key];
                        if (spec.kind === "bool") setEdit(key, value ? "true" : "false");
                        else setEdit(key, typeof value === "number" ? value : String(value ?? ""));
                      }}
                    />
                  ))}
                </div>
              )}
            </GlowCard>

            <Section
              title="只支持全局的设置"
              hint={`${globalOnly.length} 项只能在设置页修改`}
              icon={Lock}
              tone="quiet"
            >
              <div className="flex min-w-0 flex-wrap">
                {globalOnly.length === 0 ? (
                  <p className="text-[0.78rem] text-soft">—</p>
                ) : (
                  globalOnly.map((key) => (
                    <span
                      key={key}
                      className="mr-1.5 mt-1.5 min-w-0 break-all rounded-lg border border-line bg-sunken px-2 py-1 font-mono text-[0.68rem] leading-relaxed text-soft"
                    >
                      {key}
                    </span>
                  ))
                )}
              </div>
            </Section>

            <Section title="已知限制" icon={TriangleAlert} tone="warn">
              <p className="text-[0.78rem] leading-relaxed text-soft">
                服务端只支持写入/覆盖，暂不支持删除某条覆盖。要回到全局行为，可点该行的“填全局值”再保存（会留下一条与全局相同的覆盖）。
              </p>
            </Section>
          </>
        )}
      </div>


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
  touch,
  onChange,
  onFillGlobal,
}: {
  name: string;
  spec: SettingSpec;
  globalValue: unknown;
  effectiveValue: unknown;
  overridden: boolean;
  editValue: unknown;
  touch: boolean;
  onChange: (value: unknown) => void;
  onFillGlobal: () => void;
}) {
  const id = `device-setting-${name}`;
  const effectiveText = showValue(effectiveValue);

  return (
    <div className="grid min-w-0 gap-3 border-b border-line py-4 first:pt-0 last:border-b-0 last:pb-0 lg:grid-cols-[minmax(0,1fr)_minmax(0,18rem)] lg:items-center lg:gap-5">
      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center">
          <label htmlFor={id} className="mr-2 min-w-0 text-[0.84rem] font-medium leading-snug text-ink">
            {spec.description || name}
          </label>
          {overridden ? (
            <span className="mt-0.5 rounded-full border border-line bg-brand-soft px-1.5 py-px text-[0.62rem] font-semibold text-brand">
              本设备已覆盖
            </span>
          ) : (
            <span className="mt-0.5 rounded-full border border-line bg-sunken px-1.5 py-px text-[0.62rem] font-medium text-faint">
              继承全局
            </span>
          )}
        </div>
        <p className="mt-1 min-w-0 break-all text-[0.7rem] leading-relaxed text-faint">
          全局 <span className="font-mono text-soft">{showValue(globalValue)}</span>
          <span className="ml-1.5">
            · 有效{" "}
            <span className={cn("font-mono", overridden ? "font-semibold text-brand" : "text-soft")}>
              {effectiveText}
            </span>
          </span>
          {spec.kind === "int" && (spec.min !== null || spec.max !== null)
            ? ` · 范围 ${spec.min ?? "—"}..${spec.max ?? "—"}`
            : ""}
        </p>
      </div>

      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-1.5">
        {spec.kind === "bool" ? (
          <Select
            value={editValue === undefined ? INHERIT : String(editValue)}
            onValueChange={(value) => onChange(String(value) === INHERIT ? INHERIT : String(value))}
          >
            <SelectTrigger id={id} size="sm" className={cn("w-full", touch && "tap")}>
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
            <SelectTrigger id={id} size="sm" className={cn("w-full", touch && "tap")}>
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
              onChange(
                event.target.value === "" ? INHERIT : spec.kind === "int" ? Number(event.target.value) : event.target.value,
              )
            }
            className={cn("h-9", touch && "tap")}
          />
        )}
        <Button
          size="icon-sm"
          variant="ghost"
          className={cn("shrink-0", touch && "size-11")}
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
