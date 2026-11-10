import { useEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
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

/**
 * 每台设备的时间线浏览进度（时段选择、无极滚动页数、滚动位置）。
 *
 * 本视图不在 keepMounted 名单里：切 Tab、路由往返（比如看到一半按了返回
 * 再前进回来）都会重挂载。没有这份记忆时重挂载 = 时段重置成"今天"、只显示
 * 前 96 个桶、滚动归零，加上"今天"窗口随服务端时钟移动导致缓存 miss 重拉
 * ——看起来就是整页莫名其妙刷新，进度全丢。
 */
interface TimelineMemo {
  daysAgo: number;
  custom: { fromMs: number; toMs: number } | null;
  visibleCount: number;
  scrollTop: number;
}
const viewMemo = new Map<string, TimelineMemo>();

// 组件里的 `window`（查询时间窗）会遮蔽全局 window，历史/滚动从这里走。
const historyBack = (): void => window.history.back();
const historyGo = (delta: number): void => window.history.go(delta);
const pageScrollTop = (): number => window.scrollY;
const pageScrollTo = (top: number): void => window.scrollTo({ top, behavior: "auto" });

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
 *
 * 时段弹窗与单帧原图走 URL（`?b=<ts_ms>&f=<frame_id>`）并各推一条历史：
 * 手表/手机上的返回键会逐级关闭弹窗回到列表——之前返回直接跳出页面导致
 * 整页重载，已选的时间段、无极滚动进度和滚动位置全部丢失。UI 的关闭按钮
 * 也走 history.back()（本视图推过的条目数由 oursRef 记账）；直接落地在带
 * 参数的 URL 上时没有可回退的条目，就用 replace 清参。
 */
export function TimelineView({ device }: { device: DeviceLike }) {
  const uid = device.uid;
  const now = useServerNow();
  const viewport = useViewport();
  const watch = viewport === "watch";
  const anchor = now === null ? null : Math.floor(now / 60_000) * 60_000;

  // 浏览进度从记忆恢复（首挂载与换设备各一次），不再是每次进页面都从零开始。
  const initialMemo = viewMemo.get(uid);
  const [daysAgo, setDaysAgo] = useState(initialMemo?.daysAgo ?? 0);
  const [custom, setCustom] = useState<{ fromMs: number; toMs: number } | null>(
    initialMemo?.custom ?? null,
  );
  const [fromText, setFromText] = useState("");
  const [toText, setToText] = useState("");
  const [rangeError, setRangeError] = useState("");
  const [visibleCount, setVisibleCount] = useState(initialMemo?.visibleCount ?? REVEAL_STEP);

  const [searchParams, setSearchParams] = useSearchParams();

  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  /** 滚动位置只恢复一次：等首屏内容渲染后再落位。 */
  const restoredRef = useRef(false);

  // 换设备不重挂载（同一路由组件）：显式换到目标设备的进度档。
  useEffect(() => {
    const saved = viewMemo.get(uid);
    restoredRef.current = false;
    setDaysAgo(saved?.daysAgo ?? 0);
    setCustom(saved?.custom ?? null);
    setVisibleCount(saved?.visibleCount ?? REVEAL_STEP);
  }, [uid]);

  // 持续记档；scrollTop 单独在卸载时写（见下方 cleanup）。
  useEffect(() => {
    const prev = viewMemo.get(uid);
    viewMemo.set(uid, {
      daysAgo,
      custom,
      visibleCount,
      scrollTop: prev?.scrollTop ?? 0,
    });
  }, [uid, daysAgo, custom, visibleCount]);

  // 弹窗层级：0=列表 1=时段 2=单帧。推入的条目数由深度变化统一记账。
  const bucketParam = searchParams.get("b");
  const frameParam = searchParams.get("f");
  const openBucketTs =
    bucketParam !== null && bucketParam !== "" && Number.isFinite(Number(bucketParam))
      ? Number(bucketParam)
      : null;
  const openFrameId =
    frameParam !== null && frameParam !== "" && Number.isFinite(Number(frameParam))
      ? Number(frameParam)
      : null;
  const dialogDepth = openBucketTs !== null ? (openFrameId !== null ? 2 : 1) : 0;

  const oursRef = useRef(0);
  const prevDepthRef = useRef(dialogDepth);
  useEffect(() => {
    const prev = prevDepthRef.current;
    if (dialogDepth > prev) {
      // 深度增加只可能来自本视图的 push（写 ?b/?f 的入口都在这里）。
      oursRef.current += dialogDepth - prev;
    } else if (dialogDepth < prev) {
      // 浏览器/系统返回和自己的 history.back() 都会走到这里，统一核销。
      oursRef.current = Math.max(0, oursRef.current - (prev - dialogDepth));
    }
    prevDepthRef.current = dialogDepth;
  }, [dialogDepth]);

  const window = useMemo(() => {
    if (custom) return custom;
    if (anchor === null) return null;
    const raw = dayWindow(anchor, daysAgo);
    if (daysAgo !== 0) return raw;
    // "今天"的原始终点 = 当前分钟，queryKey 每分钟换一次 key：周期性骨架屏、
    // 重挂载永远打不中缓存（观感即"页面重新加载"）。终点向上取整到整小时，
    // 一小时内 key 稳定；终点仍 ≥ now，最新帧不会缺席，30s 轮询继续保鲜。
    const hourEnd = Math.ceil(anchor / 3_600_000) * 3_600_000;
    return { ...raw, toMs: Math.min(raw.toMs, Math.max(raw.fromMs + 1000, hourEnd)) };
  }, [anchor, custom, daysAgo]);

  const timelineQuery = useQuery({
    queryKey: ["timeline", uid, window?.fromMs ?? 0, window?.toMs ?? 0],
    queryFn: () => api.timeline(uid, window!.fromMs, window!.toMs),
    enabled: Boolean(window),
    refetchInterval: 30_000,
    // "今天"的窗口随服务端时钟每分钟移动，queryKey 跟着变；没有这一行，
    // 每次换 key 都会闪一帧骨架屏（观感就是"页面重新加载"）。
    placeholderData: keepPreviousData,
  });

  const rawBuckets = timelineQuery.data?.buckets ?? [];

  // 弹窗状态由 URL 还原。时段不在当前数据里（直接落地带参 URL、或窗口已
  // 切换）时用最小占位对象——帧列表查询只需要 ts_ms。
  const openBucket = useMemo<TimelineBucket | null>(() => {
    if (openBucketTs === null) return null;
    const found = rawBuckets.find((bucket) => bucket.ts_ms === openBucketTs);
    return found ?? { ts_ms: openBucketTs, device_time_ms: openBucketTs, frame_id: null, frame_count: 0, session: 0 };
  }, [openBucketTs, rawBuckets]);

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

  // 单帧同样由 URL 还原；帧列表还在加载时先落到加载分支。
  const openFrame = useMemo<Frame | null>(() => {
    if (openFrameId === null) return null;
    return (framesQuery.data?.frames ?? []).find((frame) => frame.id === openFrameId) ?? null;
  }, [openFrameId, framesQuery.data]);

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

  /** 推一条历史并切换弹窗层级（push：系统返回键可逐级回退）。 */
  const pushDialogParams = (ts: number | null, frameId: number | null) => {
    const next = new URLSearchParams(searchParams);
    if (ts === null) next.delete("b");
    else next.set("b", String(ts));
    if (frameId === null) next.delete("f");
    else next.set("f", String(frameId));
    setSearchParams(next);
  };

  /** 单帧 → 该时段网格：优先回退本视图推入的那条历史。 */
  const closeFrame = () => {
    if (oursRef.current > 0) {
      historyBack();
      return;
    }
    const next = new URLSearchParams(searchParams);
    next.delete("f");
    setSearchParams(next, { replace: true });
  };

  /** 时段弹窗 → 列表：把本视图推入的条目一次性回退掉，不留死历史。 */
  const closeDialog = () => {
    const n = Math.min(oursRef.current, dialogDepth);
    if (n > 0 && n >= dialogDepth) {
      historyGo(-n);
      return;
    }
    const next = new URLSearchParams(searchParams);
    next.delete("b");
    next.delete("f");
    setSearchParams(next, { replace: true });
  };

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

  // 恢复滚动位置：等列表真正渲染出来，且晚一拍——路由往返时 AppShell 的
  // "路由变化归零滚动" effect 挂在父层、后于子 effect 执行，不能跟它赛跑。
  const hasContent = hourGroups.length > 0;
  useEffect(() => {
    if (restoredRef.current || !hasContent) return;
    restoredRef.current = true;
    const saved = viewMemo.get(uid)?.scrollTop ?? 0;
    if (saved <= 0) return;
    const timer = setTimeout(() => {
      const scroller = rootRef.current?.closest("main");
      if (scroller) scroller.scrollTo({ top: saved, behavior: "auto" });
      else pageScrollTo(saved);
    }, 0);
    return () => clearTimeout(timer);
  }, [hasContent, uid]);

  // 卸载（切 Tab / 路由离开）时把滚动位置写回记忆。
  useEffect(() => {
    return () => {
      const scroller = rootRef.current?.closest("main");
      const top = scroller ? scroller.scrollTop : pageScrollTop();
      const prev = viewMemo.get(uid);
      if (prev) viewMemo.set(uid, { ...prev, scrollTop: top });
    };
  }, [uid]);

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
    <div ref={rootRef} className={cn("min-w-0", watch ? "space-y-3" : viewport === "mobile" ? "space-y-4" : "space-y-5")}>
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
                          // 不用 -translate-x-1/2：Tailwind v4 编译成 translate:
                          // 属性（Chromium 104），手表上圆点会偏出轨道。
                          "absolute top-2 size-3 rounded-full ring-4 ring-background left-[calc(50%-0.375rem)]",
                          groupIndex === 0 ? "bg-primary" : "bg-border-strong",
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
                              pushDialogParams(bucket.ts_ms, null);
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
          if (!open) closeDialog();
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
                <Button variant="outline" className="min-h-9" onClick={closeFrame}>
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
                  onClick={() => pushDialogParams(openBucketTs, frame.id)}
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
