import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CalendarRange, ChevronLeft, ExternalLink, RefreshCw } from "lucide-react";

import {
  Banner,
  EmptyBlock,
  ErrorBlock,
  LoadingBlock,
  PageHeader,
  Pager,
} from "@/components/async-state";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { api, isApiError } from "@/lib/api";
import { formatBytes, formatDateTime, frameTimeMs } from "@/lib/format";
import { dayWindow, formatWindow, parseDateTimeLocal, toDateTimeLocal } from "@/lib/time-window";
import type { Frame, TimelineBucket } from "@/lib/types";
import { useServerNow } from "@/store/clock";
import { cn } from "@/lib/utils";

const BUCKETS_PER_PAGE = 96;

interface DeviceLike {
  uid: string;
}

/**
 * Timeline: a day-window grid of server-bucketed thumbnails.
 *
 * Two rules from the 0.2.x fix list are structural here:
 *  - the query window is derived from `serverNow()` (never `Date.now()`), and it
 *    is rounded to the minute so the 1 Hz clock tick cannot change the query key
 *    on every tick;
 *  - bucket boundaries are the server's (`bucket_ms` + `buckets[]`); the console
 *    only renders them. Clicking a bucket drills into `/frames` for that exact
 *    window, and a second click opens the full-size frame.
 */
export function TimelineView({ device }: { device: DeviceLike }) {
  const uid = device.uid;
  const now = useServerNow();
  const anchor = now === null ? null : Math.floor(now / 60_000) * 60_000;

  const [daysAgo, setDaysAgo] = useState(0);
  const [custom, setCustom] = useState<{ fromMs: number; toMs: number } | null>(null);
  const [fromText, setFromText] = useState("");
  const [toText, setToText] = useState("");
  const [rangeError, setRangeError] = useState("");
  const [bucketOffset, setBucketOffset] = useState(0);
  const [openBucket, setOpenBucket] = useState<TimelineBucket | null>(null);
  const [openFrame, setOpenFrame] = useState<Frame | null>(null);

  const window = useMemo(() => {
    if (custom) return custom;
    if (anchor === null) return null;
    return dayWindow(anchor, daysAgo);
  }, [anchor, custom, daysAgo]);

  const timelineQuery = useQuery({
    queryKey: ["timeline", uid, window?.fromMs ?? 0, window?.toMs ?? 0],
    queryFn: () => api.timeline(uid, window!.fromMs, window!.toMs),
    enabled: Boolean(window),
    refetchInterval: 30_000,
  });

  const framesQuery = useQuery({
    queryKey: ["frames", uid, openBucket?.ts_ms ?? 0, timelineQuery.data?.bucket_ms ?? 0],
    queryFn: () =>
      api.listFrames(uid, {
        fromMs: openBucket!.ts_ms,
        toMs: openBucket!.ts_ms + (timelineQuery.data?.bucket_ms ?? 60_000),
        limit: 60,
      }),
    enabled: openBucket !== null,
  });

  const applyCustom = () => {
    const from = parseDateTimeLocal(fromText);
    const to = parseDateTimeLocal(toText);
    if (from === null || to === null) {
      setRangeError("请选择完整的开始和结束时间");
      return;
    }
    if (to <= from) {
      setRangeError("结束时间必须晚于开始时间");
      return;
    }
    setRangeError("");
    setBucketOffset(0);
    setCustom({ fromMs: from, toMs: to });
  };

  const pickDay = (days: number) => {
    setDaysAgo(days);
    setCustom(null);
    setRangeError("");
    setBucketOffset(0);
  };

  const buckets = timelineQuery.data?.buckets ?? [];
  const visibleBuckets = buckets.slice(bucketOffset, bucketOffset + BUCKETS_PER_PAGE);
  const bucketMs = timelineQuery.data?.bucket_ms ?? 0;
  const firstFrameId = buckets.find((bucket) => bucket.frame_id)?.frame_id ?? null;

  return (
    <div className="space-y-3">
      <PageHeader
        title="时间线"
        subtitle={
          window
            ? `${formatWindow(window)} · ${timelineQuery.data ? `共 ${timelineQuery.data.total_frames} 帧` : "读取中"}`
            : "等待服务端校时…"
        }
        actions={
          <>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void timelineQuery.refetch()}
              disabled={timelineQuery.isFetching || !window}
            >
              <RefreshCw className={cn("size-3.5", timelineQuery.isFetching && "animate-spin")} />
              刷新
            </Button>
            {firstFrameId ? (
              <a
                href={api.frameUrl(uid, firstFrameId, "full")}
                className={cn(buttonVariants({ variant: "ghost", size: "sm" }), "no-underline")}
                title="在新标签页打开该范围第一张原图"
                target="_blank"
                rel="noreferrer"
              >
                <ExternalLink className="size-3.5" />
                打开原图
              </a>
            ) : null}
          </>
        }
      />

      <div className="flex flex-wrap items-center gap-1.5">
        {[
          { days: 0, label: "今天" },
          { days: 1, label: "昨天" },
          { days: 2, label: "前天" },
        ].map((item) => (
          <button
            key={item.days}
            type="button"
            onClick={() => pickDay(item.days)}
            className={cn(
              "rounded-md border px-2.5 py-1 text-xs",
              !custom && daysAgo === item.days
                ? "border-brand/40 bg-brand/10 text-brand"
                : "border-line text-soft hover:bg-sunken",
            )}
          >
            {item.label}
          </button>
        ))}
        <span className="mx-1 hidden h-4 w-px bg-line sm:block" aria-hidden />
        <CalendarRange className="size-3.5 text-faint" aria-hidden />
        <Input
          type="datetime-local"
          className="h-8 w-[11.5rem] text-xs"
          value={fromText}
          onChange={(event) => setFromText(event.target.value)}
          aria-label="开始时间"
        />
        <span className="text-xs text-faint">至</span>
        <Input
          type="datetime-local"
          className="h-8 w-[11.5rem] text-xs"
          value={toText}
          onChange={(event) => setToText(event.target.value)}
          aria-label="结束时间"
        />
        <Button size="sm" variant="outline" onClick={applyCustom} disabled={!fromText || !toText}>
          应用范围
        </Button>
        {custom ? (
          <Button size="sm" variant="ghost" onClick={() => pickDay(0)}>
            回到今天
          </Button>
        ) : null}
        {anchor !== null && window && !fromText && !toText ? (
          <button
            type="button"
            className="text-[0.7rem] text-faint underline-offset-2 hover:underline"
            onClick={() => {
              setFromText(toDateTimeLocal(window.fromMs));
              setToText(toDateTimeLocal(window.toMs));
            }}
          >
            用当前范围填充
          </button>
        ) : null}
      </div>

      {rangeError ? <Banner tone="error">{rangeError}</Banner> : null}

      {!window ? (
        <LoadingBlock label="等待服务端校时后再查询时间线…" />
      ) : timelineQuery.isError ? (
        <ErrorBlock
          message={isApiError(timelineQuery.error) ? timelineQuery.error.message : "无法读取时间线"}
          retryable
          onRetry={() => void timelineQuery.refetch()}
        />
      ) : timelineQuery.isLoading ? (
        <LoadingBlock label="正在读取时间线…" rows={4} />
      ) : buckets.length === 0 ? (
        <EmptyBlock
          title="这个时间段没有画面"
          description="受控端只在有已登录桌面会话时采集屏幕；如果设备当时离线、没有登录用户，或采集被关闭，这里就是空的。"
        />
      ) : (
        <>
          <Banner tone="info">
            桶宽 {bucketMs / 1000} 秒（由服务端按跨度选择）· 共 {buckets.length} 个桶 · 点击缩略图查看该时段全部画面
          </Banner>
          <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-5 md:grid-cols-8">
            {visibleBuckets.map((bucket) => (
              <button
                key={bucket.ts_ms}
                type="button"
                onClick={() => {
                  setOpenFrame(null);
                  setOpenBucket(bucket);
                }}
                className="group relative aspect-video overflow-hidden rounded-md border border-line bg-sunken focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
                title={`${formatDateTime(bucket.ts_ms)} · ${bucket.frame_count} 帧`}
              >
                {bucket.frame_id ? (
                  <img
                    src={api.frameUrl(uid, bucket.frame_id, "thumb")}
                    alt={`${formatDateTime(bucket.ts_ms)} 的画面`}
                    loading="lazy"
                    decoding="async"
                    className="h-full w-full object-cover"
                  />
                ) : (
                  <span className="flex h-full items-center justify-center text-[0.7rem] text-faint">无缩略图</span>
                )}
                {bucket.frame_count > 1 ? (
                  <span className="absolute right-0.5 bottom-0.5 rounded bg-black/60 px-1 text-[0.7rem] text-white">
                    {bucket.frame_count}
                  </span>
                ) : null}
                <span className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/70 to-transparent px-1 pt-3 pb-0.5 text-left text-[0.7rem] text-white/90">
                  {formatDateTime(bucket.ts_ms).slice(11)}
                </span>
              </button>
            ))}
          </div>
          <Pager total={buckets.length} limit={BUCKETS_PER_PAGE} offset={bucketOffset} onChange={setBucketOffset} />
        </>
      )}

      <Dialog
        open={openBucket !== null}
        onOpenChange={(open) => {
          if (!open) {
            setOpenBucket(null);
            setOpenFrame(null);
          }
        }}
      >
        <DialogContent className="max-h-[88vh] w-[calc(100%-1rem)] overflow-y-auto sm:max-w-4xl">
          <DialogHeader>
            <DialogTitle>{openBucket ? formatDateTime(openBucket.ts_ms) : ""}</DialogTitle>
            <DialogDescription>
              {openBucket
                ? `${openBucket.frame_count} 帧 · 窗口 ${bucketMs / 1000} 秒${
                    openFrame ? " · 点击下方缩略图切换" : " · 点击任意画面查看原图"
                  }`
                : ""}
            </DialogDescription>
          </DialogHeader>

          {openFrame ? (
            <div className="space-y-2">
              <Button size="sm" variant="outline" onClick={() => setOpenFrame(null)}>
                <ChevronLeft className="size-3.5" />
                返回该时段
              </Button>
              <img
                src={api.frameUrl(uid, openFrame.id, "full")}
                alt={`${formatDateTime(frameTimeMs(openFrame))} 的原图`}
                className="max-h-[62vh] w-full rounded-md bg-black object-contain"
              />
              <p className="text-xs text-soft">
                设备时间 {formatDateTime(openFrame.device_time_ms)} · 服务端时间 {formatDateTime(openFrame.ts_ms)} ·{" "}
                {openFrame.width}×{openFrame.height} · {formatBytes(openFrame.size_bytes)}
              </p>
            </div>
          ) : framesQuery.isLoading ? (
            <LoadingBlock label="正在读取该时段的画面…" />
          ) : framesQuery.isError ? (
            <ErrorBlock
              message={isApiError(framesQuery.error) ? framesQuery.error.message : "无法读取画面"}
              retryable
              onRetry={() => void framesQuery.refetch()}
            />
          ) : (framesQuery.data?.frames.length ?? 0) === 0 ? (
            <EmptyBlock
              title="该时段的画面已不在本地"
              description="可能已被保留策略清理，或只存在于归档仓库（见备份页）。"
            />
          ) : (
            <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-5">
              {(framesQuery.data?.frames ?? []).map((frame) => (
                <button
                  key={frame.id}
                  type="button"
                  onClick={() => setOpenFrame(frame)}
                  className="aspect-video overflow-hidden rounded-md border border-line bg-sunken"
                  title={formatDateTime(frameTimeMs(frame))}
                >
                  <img
                    src={api.frameUrl(uid, frame.id, "thumb")}
                    alt={formatDateTime(frameTimeMs(frame))}
                    loading="lazy"
                    decoding="async"
                    className="h-full w-full object-cover"
                  />
                </button>
              ))}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}