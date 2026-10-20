import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, X } from "lucide-react";

import { FrameStage, FrameView } from "@/components/frame-view";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { formatDate, formatTime, formatTimeShort } from "@/lib/format";
import type { Device } from "@/lib/types";
import { cn } from "@/lib/utils";

const HOUR = 3600_000;
const DAY = 24 * HOUR;

function startOfDay(ms: number): number {
  const date = new Date(ms);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

export function TimelineView({ device }: { device: Device }) {
  const [dayOffset, setDayOffset] = useState(0);
  const [hourFrom, setHourFrom] = useState(0);
  const [hourTo, setHourTo] = useState(24);
  const [selected, setSelected] = useState<number | null>(null);

  const now = Date.now();
  const dayStart = startOfDay(now) - dayOffset * DAY;
  const from = dayStart + hourFrom * HOUR;
  const to = Math.min(dayStart + hourTo * HOUR, dayOffset === 0 ? now : dayStart + DAY);
  const valid = to > from;

  const query = useQuery({
    queryKey: ["timeline", device.uid, from, to],
    queryFn: () => api.timeline(device.uid, from, to),
    enabled: valid,
    refetchInterval: 60_000,
  });

  const buckets = query.data?.buckets ?? [];
  const selectedBucket = selected !== null ? buckets[selected] : null;
  const selectedFrame = selectedBucket?.frame_id ?? null;

  const stats = useMemo(
    () => ({
      total: query.data?.total_frames ?? 0,
      bucketMinutes: Math.max(1, Math.round((query.data?.bucket_ms ?? 60_000) / 60_000)),
    }),
    [query.data],
  );

  const step = (delta: number) => {
    if (selected === null) return;
    const next = selected + delta;
    if (next >= 0 && next < buckets.length) setSelected(next);
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex items-center gap-1">
          <Button variant="outline" size="icon-sm" onClick={() => setDayOffset((value) => value + 1)} aria-label="前一天">
            <ChevronLeft className="size-3.5" />
          </Button>
          <Button
            variant={dayOffset === 0 ? "default" : "outline"}
            size="sm"
            onClick={() => setDayOffset(0)}
          >
            今天
          </Button>
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => setDayOffset((value) => Math.max(0, value - 1))}
            disabled={dayOffset === 0}
            aria-label="后一天"
          >
            <ChevronRight className="size-3.5" />
          </Button>
        </div>
        <div className="flex items-center gap-1 text-[0.7rem] text-soft">
          <Input
            type="number"
            min={0}
            max={23}
            value={hourFrom}
            onChange={(event) => setHourFrom(Math.max(0, Math.min(23, Number(event.target.value) || 0)))}
            className="h-8 w-16 text-center"
            aria-label="起始小时"
          />
          <span>—</span>
          <Input
            type="number"
            min={1}
            max={24}
            value={hourTo}
            onChange={(event) => setHourTo(Math.max(1, Math.min(24, Number(event.target.value) || 24)))}
            className="h-8 w-16 text-center"
            aria-label="结束小时"
          />
          <span>点</span>
        </div>
        <p className="ml-auto text-[0.68rem] text-faint">
          {formatDate(dayStart)} · {stats.total} 张 · 每格 {stats.bucketMinutes} 分钟
        </p>
      </div>

      {!valid ? (
        <p className="rounded-panel border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
          结束时间必须晚于开始时间。
        </p>
      ) : query.isLoading ? (
        <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-4 lg:grid-cols-6">
          {Array.from({ length: 12 }).map((_, index) => (
            <Skeleton key={index} className="aspect-video w-full rounded-md" />
          ))}
        </div>
      ) : query.isError ? (
        <p className="rounded-panel border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
          读取时间线失败：{query.error instanceof Error ? query.error.message : "未知错误"}
        </p>
      ) : buckets.length === 0 ? (
        <p className="rounded-panel border border-dashed border-line px-3 py-10 text-center text-xs text-soft">
          这个时间段没有画面
        </p>
      ) : (
        <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8">
          {buckets.map((bucket, index) => (
            <button
              key={`${bucket.ts_ms}-${index}`}
              type="button"
              onClick={() => setSelected(index)}
              className="group relative aspect-video overflow-hidden rounded-md border border-line bg-sunken p-0 text-left"
            >
              <FrameView uid={device.uid} frameId={bucket.frame_id} variant="thumb" alt={formatTime(bucket.ts_ms)} />
              <span className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/70 to-transparent px-1 pb-0.5 pt-3 text-[0.6rem] font-medium text-white">
                {formatTimeShort(bucket.ts_ms)}
                {bucket.frame_count > 1 ? ` ×${bucket.frame_count}` : ""}
              </span>
            </button>
          ))}
        </div>
      )}

      {selectedBucket && selectedFrame ? (
        <div className="fixed inset-0 z-50 flex flex-col bg-black/85 backdrop-blur-sm">
          <div className="flex items-center justify-between gap-2 px-3 py-2 text-white">
            <span className="text-xs">{formatDate(selectedBucket.ts_ms)} {formatTime(selectedBucket.ts_ms)}</span>
            <div className="flex items-center gap-1">
              <Button variant="ghost" size="icon-sm" className="text-white hover:bg-white/15" onClick={() => step(-1)}>
                <ChevronLeft className="size-4" />
              </Button>
              <Button variant="ghost" size="icon-sm" className="text-white hover:bg-white/15" onClick={() => step(1)}>
                <ChevronRight className="size-4" />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                className="text-white hover:bg-white/15"
                onClick={() => setSelected(null)}
                aria-label="关闭"
              >
                <X className="size-4" />
              </Button>
            </div>
          </div>
          <div className="min-h-0 flex-1 p-2">
            <FrameStage className="h-full w-full bg-black/40">
              <FrameView
                uid={device.uid}
                frameId={selectedFrame}
                variant="full"
                alt="画面"
                eager
                className={cn("h-full w-full object-contain")}
              />
            </FrameStage>
          </div>
          <p className="px-3 pb-2 text-center text-[0.65rem] text-white/60">
            第 {selected === null ? 0 : selected + 1} / {buckets.length} 张
          </p>
        </div>
      ) : null}
    </div>
  );
}