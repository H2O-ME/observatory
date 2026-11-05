import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ExternalLink, RefreshCw } from "lucide-react";

import {
  Banner,
  EmptyBlock,
  ErrorBlock,
  LoadingBlock,
} from "@/components/async-state";
import { FrameView } from "@/components/frame-view";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useViewport } from "@/hooks/use-viewport";
import { api, isApiError } from "@/lib/api";
import { formatBytes, formatDateTime, frameTimeMs } from "@/lib/format";
import { dayWindow, formatWindow, parseDateTimeLocal, toDateTimeLocal } from "@/lib/time-window";
import type { Frame, TimelineBucket } from "@/lib/types";
import { useServerNow } from "@/store/clock";
import { cn } from "@/lib/utils";

/** 无极滚动：观察到底部哨兵后每次多放出的桶数。 */
const REVEAL_STEP = 96;

const DAY_OPTIONS = [
  { days: 0, label: "今天" },
  { days: 1, label: "昨天" },
  { days: 2, label: "前天" },
] as const;

interface DeviceLike {
  uid: string;
}

interface HourGroup {
  key: string;
  /** "MM-DD" of the group, for the rail label. */
  dateLabel: string;
  /** "HH" of the group, for the rail label. */
  hourLabel: string;
  frameTotal: number;
  buckets: TimelineBucket[];
}

/**
 * 按小时分组；入参必须是**最新在前**的降序数组（本视图的统一方向）。
 * 连续分组的逻辑对降序同样成立——相邻桶的小时号相同即并入当前组。
 */
function groupByHour(buckets: TimelineBucket[]): HourGroup[] {
  const groups: HourGroup[] = [];
  for (const bucket of buckets) {
    const stamp = formatDateTime(bucket.ts_ms);
    const key = stamp.slice(0, 13);
    const last = groups[groups.length - 1];
    if (last && last.key === key) {
      last.buckets.push(bucket);
      last.frameTotal += bucket.frame_count;
      continue;
    }
    groups.push({
      key,
      dateLabel: stamp.slice(5, 10),
      hourLabel: stamp.slice(11, 13),
      frameTotal: bucket.frame_count,
      buckets: [bucket],
    });
  }
  return groups;
}

/**
 * Timeline（0.3.20 重排）：最新画面永远在最上，左侧时间轨 + 卡片流，无极滚动。
 *
 * 结构性规则（0.2.x 修复清单沿用）：
 *  - 查询窗口由 `serverNow()` 派生并取整到分钟（1Hz 时钟不会抖动 query key）；
 *  - 桶边界以服务端为准（`bucket_ms` + `buckets[]`），控制台只负责渲染；
 *  - 服务端桶是升序的（bucket_index ASC），这里统一反转为降序——最新置顶。
 * 无极滚动是纯客户端切片：一次查询已覆盖整个窗口，哨兵进入视口就多放一批。
 */
export function TimelineView({ device }: { device: DeviceLike }) {
  const uid = device.uid;
  const now = useServerNow();
  const viewport = useViewport();
  const watch = viewport === "watch";
  const anchor = now === null ? null : Math.floor(now / 60_000) * 60_000;

  const [daysAgo, setDaysAgo] = useState(0);
  const [custom, setCustom] = useState<{ fromMs: number; toMs: number } | null>(null);
  const [fromText, setFromText] = useState("");
  const [toText, setToText] = useState("");
  const [rangeError, setRangeError] = useState("");
  const [visibleCount, setVisibleCount] = useState(REVEAL_STEP);
  const [openBucket, setOpenBucket] = useState<TimelineBucket | null>(null);
  const [openFrame, setOpenFrame] = useState<Frame | null>(null);

  const sentinelRef = useRef<HTMLDivElement | null>(null);

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
    setVisibleCount(REVEAL_STEP);
    setCustom({ fromMs: from, toMs: to });
  };

  const pickDay = (days: number) => {
    setDaysAgo(days);
    setCustom(null);
    setRangeError("");
    setVisibleCount(REVEAL_STEP);
  };

  const rawBuckets = timelineQuery.data?.buckets ?? [];
  /** 服务端升序 → 最新在前。并列时 ts_ms 相同按 frame_id 降序保持稳定。 */
  const sortedBuckets = useMemo(
    () => [...rawBuckets].sort((a, b) => b.ts_ms - a.ts_ms || (b.frame_id ?? 0) - (a.frame_id ?? 0)),
    [rawBuckets],
  );
  const visibleBuckets = sortedBuckets.slice(0, visibleCount);
  const bucketMs = timelineQuery.data?.bucket_ms ?? 0;
  const newestFrameId = sortedBuckets.find((bucket) => bucket.frame_id)?.frame_id ?? null;
  const hourGroups = useMemo(() => groupByHour(visibleBuckets), [visibleBuckets]);
  const allRevealed = visibleCount >= sortedBuckets.length;

  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisibleCount((count) => count + REVEAL_STEP);
        }
      },
      { rootMargin: "800px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [allRevealed, hourGroups.length]);

  const frameGridClass = watch
    ? "grid-cols-1"
    : "grid-cols-2 md:grid-cols-3 xl:grid-cols-4";

  return (
    <div className={cn("min-w-0", watch ? "space-y-3" : viewport === "mobile" ? "space-y-4" : "space-y-5")}>
      {/* Filter bar: segmented day picker + explicit window + quiet actions. */}
      <div className="flex min-w-0 flex-col justify-between gap-4 rounded-xl bg-card p-4 shadow-sm md:flex-row md:flex-wrap md:items-center">
        <div className="flex min-w-0 flex-col gap-3 md:flex-row md:items-center md:gap-4">
          <div className="flex min-w-0 items-center gap-1 rounded-md bg-muted p-1">
            {DAY_OPTIONS.map((item) => {
              const active = !custom && daysAgo === item.days;
              return (
                <button
                  key={item.days}
                  type="button"
                  onClick={() => pickDay(item.days)}
                  aria-pressed={active}
                  className={cn(
                    "inline-flex h-7 min-w-0 items-center justify-center rounded-sm px-3 text-xs font-medium transition-colors",
                    active
                      ? "bg-card text-foreground shadow-sm"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {item.label}
                </button>
              );
            })}
          </div>

          <div className="flex min-w-0 flex-wrap items-center gap-3">
            <Input
              type="datetime-local"
              className="h-9 w-full min-w-0 text-sm sm:w-[10.5rem]"
              value={fromText}
              onChange={(event) => setFromText(event.target.value)}
              aria-label="开始时间"
            />
            <Input
              type="datetime-local"
              className="h-9 w-full min-w-0 text-sm sm:w-[10.5rem]"
              value={toText}
              onChange={(event) => setToText(event.target.value)}
              aria-label="结束时间"
            />
            {anchor !== null && window && !fromText && !toText ? (
              <button
                type="button"
                className="inline-flex h-9 items-center whitespace-nowrap text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                onClick={() => {
                  setFromText(toDateTimeLocal(window.fromMs));
                  setToText(toDateTimeLocal(window.toMs));
                }}
              >
                用当前范围填充
              </button>
            ) : null}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 md:ml-auto md:justify-end">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void timelineQuery.refetch()}
            disabled={timelineQuery.isFetching || !window}
          >
            <RefreshCw className={cn("size-4", timelineQuery.isFetching && "animate-spin")} aria-hidden />
            刷新
          </Button>
          {newestFrameId ? (
            <a
              href={api.frameUrl(uid, newestFrameId, "full")}
              className={cn(buttonVariants({ variant: "ghost", size: "sm" }), "tap-target no-underline")}
              title="在新标签页打开最新一张原图"
              target="_blank"
              rel="noreferrer"
            >
              <ExternalLink className="size-4" aria-hidden />
              打开最新原图
            </a>
          ) : null}
          <Button
            variant="outline"
            size="sm"
            onClick={applyCustom}
            disabled={!fromText || !toText}
          >
            应用范围
          </Button>
        </div>
      </div>

      {window ? (
        <p className="min-w-0 break-words text-sm text-muted-foreground">
          <span className="tnum">{formatWindow(window)}</span>
          <span className="text-faint"> · </span>
          <span className="tnum">
            {timelineQuery.isError
              ? "读取失败"
              : timelineQuery.data
                ? `共 ${timelineQuery.data.total_frames} 帧 · 最新在前`
                : "读取中…"}
          </span>
        </p>
      ) : null}

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
      ) : sortedBuckets.length === 0 ? (
        <EmptyBlock
          title="这个时间段没有画面"
          description="受控端只在有已登录桌面会话时采集屏幕；如果设备当时离线、没有登录用户，或采集被关闭，这里就是空的。"
        />
      ) : (
        <div className="min-w-0">
          {/* 垂直时间线：左列时间标记 + 轨道 + 卡片流；整页只有一条连续的轨。 */}
          <div className="relative min-w-0">
            <span
              aria-hidden
              className="absolute bottom-2 left-[5.25rem] top-2 hidden w-px bg-border sm:block md:left-[6.25rem]"
            />
            <div className="flex min-w-0 flex-col gap-8">
              {hourGroups.map((group, groupIndex) => (
                <section key={group.key} className="relative min-w-0">
                  <div className="flex min-w-0 gap-4 sm:gap-5">
                    <div className="w-14 shrink-0 pt-0.5 text-right sm:w-16 md:w-20">
                      <div className="tnum text-xl font-semibold leading-tight text-foreground md:text-2xl">
                        {group.hourLabel}
                      </div>
                      <div className="tnum text-xs text-muted-foreground">{group.dateLabel}</div>
                      <div className="tnum mt-1 text-xs text-faint">{group.frameTotal} 帧</div>
                    </div>

                    <div className="relative hidden w-6 shrink-0 self-stretch sm:block" aria-hidden>
                      <span
                        className={cn(
                          "absolute left-1/2 top-2 size-3 -translate-x-1/2 rounded-full ring-4 ring-background",
                          groupIndex === 0 ? "bg-primary" : "bg-muted-foreground/40",
                        )}
                      />
                    </div>

                    <div className="min-w-0 flex-1">
                      {groupIndex === 0 ? (
                        <div className="mb-3">
                          <Badge variant="default">最新</Badge>
                        </div>
                      ) : null}
                      <div className={cn("grid min-w-0 gap-3", frameGridClass)}>
                        {group.buckets.map((bucket) => (
                          <button
                            key={bucket.ts_ms}
                            type="button"
                            onClick={() => {
                              setOpenFrame(null);
                              setOpenBucket(bucket);
                            }}
                            className="relative block w-full min-w-0 overflow-hidden rounded-lg border border-border-soft bg-card text-left shadow-sm transition-shadow hover:shadow-md focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
                            title={`${formatDateTime(bucket.ts_ms)} · ${bucket.frame_count} 帧`}
                          >
                            <span className="relative block w-full overflow-hidden">
                              <span className="ratio-16-9 aspect-video relative block w-full bg-muted">
                                <span className="absolute-fill block overflow-hidden">
                                  <FrameView
                                    uid={uid}
                                    frameId={bucket.frame_id}
                                    variant="thumb"
                                    alt={`${formatDateTime(bucket.ts_ms)} 的画面`}
                                    className="object-cover"
                                  />
                                </span>
                              </span>
                              {bucket.frame_count > 1 ? (
                                <span
                                  className="tnum absolute right-2 top-2 rounded-full px-2 py-0.5 text-xs font-medium text-white backdrop-blur-sm"
                                  style={{ backgroundColor: "rgba(2, 6, 23, 0.6)" }}
                                >
                                  {bucket.frame_count} 帧
                                </span>
                              ) : null}
                            </span>
                            <span className="tnum flex min-w-0 items-center justify-between gap-2 p-2.5">
                              <span className="min-w-0 truncate text-sm font-medium text-foreground">
                                {formatDateTime(bucket.ts_ms).slice(11)}
                              </span>
                              <span className="shrink-0 text-xs text-muted-foreground">
                                {formatDateTime(bucket.ts_ms).slice(5, 10)}
                              </span>
                            </span>
                          </button>
                        ))}
                      </div>
                    </div>
                  </div>
                </section>
              ))}
            </div>
          </div>

          {/* 无极滚动哨兵：进入视口（含 800px 预加载区）就多放一批。 */}
          <div ref={sentinelRef} aria-hidden className="h-2" />
          <div className="pt-4 text-center">
            {allRevealed ? (
              <span className="tnum text-xs text-faint">
                已全部加载 · {sortedBuckets.length} 个时段 · {timelineQuery.data?.total_frames ?? 0} 帧
              </span>
            ) : (
              <span className="tnum animate-pulse text-xs text-muted-foreground">
                向下滚动加载更多（已显示 {visibleBuckets.length}/{sortedBuckets.length}）
              </span>
            )}
          </div>
        </div>
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
        <DialogContent size="xl" className="overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="tnum">{openBucket ? formatDateTime(openBucket.ts_ms) : ""}</DialogTitle>
            <DialogDescription>
              {openBucket
                ? `${openBucket.frame_count} 帧 · 窗口 ${bucketMs / 1000} 秒${
                    openFrame ? " · 已打开单帧原图" : " · 点击任意画面查看原图"
                  }`
                : ""}
            </DialogDescription>
          </DialogHeader>

          {openFrame ? (
            <div className="grid min-w-0 gap-2.5">
              <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center sm:gap-2">
                <Button variant="outline" className="min-h-9" onClick={() => setOpenFrame(null)}>
                  <ChevronLeft className="size-4" aria-hidden />
                  返回该时段
                </Button>
                <a
                  href={api.frameUrl(uid, openFrame.id, "full")}
                  className={cn(buttonVariants({ variant: "ghost", size: "sm" }), "tap-target no-underline")}
                  title="在新标签页打开这张原图"
                  target="_blank"
                  rel="noreferrer"
                >
                  <ExternalLink className="size-4" aria-hidden />
                  打开原图
                </a>
              </div>
              <div className="relative overflow-hidden rounded-md border border-border bg-black">
                <FrameView
                  uid={uid}
                  frameId={openFrame.id}
                  variant="full"
                  alt={`${formatDateTime(frameTimeMs(openFrame))} 的原图`}
                  className="max-h-[62vh] w-full"
                  eager
                />
              </div>
              <p className="text-xs leading-relaxed text-muted-foreground">
                设备时间 <span className="tnum">{formatDateTime(openFrame.device_time_ms)}</span> · 服务端时间{" "}
                <span className="tnum">{formatDateTime(openFrame.ts_ms)}</span> ·{" "}
                <span className="tnum">
                  {openFrame.width}×{openFrame.height}
                </span>{" "}
                · {formatBytes(openFrame.size_bytes)}
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
            <div className={cn("grid min-w-0 gap-2", frameGridClass)}>
              {(framesQuery.data?.frames ?? []).map((frame) => (
                <button
                  key={frame.id}
                  type="button"
                  onClick={() => setOpenFrame(frame)}
                  className="relative block w-full min-w-0 overflow-hidden rounded-md border border-border bg-muted transition-colors hover:border-border-strong focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
                  title={formatDateTime(frameTimeMs(frame))}
                >
                  <span className="ratio-16-9 relative block w-full">
                    <span className="absolute-fill block overflow-hidden">
                      <FrameView
                        uid={uid}
                        frameId={frame.id}
                        variant="thumb"
                        alt={formatDateTime(frameTimeMs(frame))}
                      />
                    </span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
