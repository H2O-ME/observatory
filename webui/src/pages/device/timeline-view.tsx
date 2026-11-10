import { useEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useInfiniteQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { ChevronLeft, ChevronRight, ExternalLink, RefreshCw } from "lucide-react";

import {
  Banner,
  EmptyBlock,
  ErrorBlock,
  LoadingBlock,
} from "@/components/async-state";
import { FrameView } from "@/components/frame-view";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useViewport } from "@/hooks/use-viewport";
import { api, isApiError } from "@/lib/api";
import { formatBytes, formatDateTime, frameTimeMs } from "@/lib/format";
import { dayWindow, formatWindow, parseDateTimeLocal, toDateTimeLocal } from "@/lib/time-window";
import type { Frame } from "@/lib/types";
import { useServerNow } from "@/store/clock";
import { cn } from "@/lib/utils";

/** 无极滚动：每页拉取的帧数（服务端上限 500，60 张缩略图一批体感最稳）。 */
const FEED_PAGE_SIZE = 60;

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
  frames: Frame[];
}

/**
 * 按小时分组；入参必须是**最新在前**的降序数组（服务端 frames 接口的统一方向）。
 * 相邻帧的小时号相同即并入当前组。
 */
function groupFramesByHour(frames: Frame[]): HourGroup[] {
  const groups: HourGroup[] = [];
  for (const frame of frames) {
    const stamp = formatDateTime(frame.ts_ms);
    const key = stamp.slice(0, 13);
    const last = groups[groups.length - 1];
    if (last && last.key === key) {
      last.frames.push(frame);
      continue;
    }
    groups.push({
      key,
      dateLabel: stamp.slice(5, 10),
      hourLabel: stamp.slice(11, 13),
      frames: [frame],
    });
  }
  return groups;
}

/**
 * 每台设备的时间线浏览进度（时段选择、滚动位置）。
 *
 * 本视图不在 keepMounted 名单里：切 Tab、路由往返（比如看到一半按了返回
 * 再前进回来）都会重挂载。没有这份记忆时重挂载 = 时段重置成"今天"、滚动
 * 归零——看起来就是整页莫名其妙刷新，进度全丢。
 */
interface TimelineMemo {
  daysAgo: number;
  custom: { fromMs: number; toMs: number } | null;
  scrollTop: number;
}
const viewMemo = new Map<string, TimelineMemo>();

// 组件里的 `window`（查询时间窗）会遮蔽全局 window，历史/滚动从这里走。
const historyBack = (): void => window.history.back();
const pageScrollTop = (): number => window.scrollY;
const pageScrollTo = (top: number): void => window.scrollTo({ top, behavior: "auto" });

/**
 * Timeline（0.3.25 重做）：截图直接平铺在时间线上，按小时分组 + 无极滚动。
 *
 * 之前的「时段卡片 → 弹窗网格 → 单帧」三层钻取被砍掉——看图不该让用户先猜
 * 哪个时段有货再点进去。数据源也从聚合的 timeline 桶换成 frames 接口的
 * offset 分页（`ts_ms DESC` 新帧在前），哨兵进视口就向后拉 60 帧。
 *
 * 结构性规则（沿用）：
 *  - 查询窗口由 `serverNow()` 派生；"今天"的终点向上取整到整小时，queryKey
 *    一小时内稳定——否则每分钟漂移的 key 打不中缓存，观感即"页面重新加载"；
 *  - 时段弹窗走 URL（`?f=<frame_id>`）并推一条历史：系统返回键原地关闭弹窗，
 *    不会跳出页面；弹窗内 上一张/下一张 用 replace 翻页，不灌历史。
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
  }, [uid]);

  // 持续记档；scrollTop 单独在卸载时写（见下方 cleanup）。
  useEffect(() => {
    viewMemo.set(uid, { daysAgo, custom, scrollTop: viewMemo.get(uid)?.scrollTop ?? 0 });
  }, [uid, daysAgo, custom]);

  const window = useMemo(() => {
    if (custom) return custom;
    if (anchor === null) return null;
    const raw = dayWindow(anchor, daysAgo);
    if (daysAgo !== 0) return raw;
    // "今天"的原始终点 = 当前分钟，queryKey 每分钟换一次 key：周期性骨架屏、
    // 重挂载永远打不中缓存。终点向上取整到整小时，一小时内 key 稳定；终点仍
    // ≥ now，最新帧不会缺席，WS frame_added 失效继续保鲜。
    const hourEnd = Math.ceil(anchor / 3_600_000) * 3_600_000;
    return { ...raw, toMs: Math.min(raw.toMs, Math.max(raw.fromMs + 1000, hourEnd)) };
  }, [anchor, custom, daysAgo]);

  const feedQuery = useInfiniteQuery({
    queryKey: ["frames", uid, window?.fromMs ?? 0, window?.toMs ?? 0],
    queryFn: ({ pageParam }) =>
      api.listFrames(uid, {
        fromMs: window!.fromMs,
        toMs: window!.toMs,
        limit: FEED_PAGE_SIZE,
        offset: pageParam,
      }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      if (lastPage.frames.length < FEED_PAGE_SIZE) return undefined;
      return allPages.reduce((sum, page) => sum + page.frames.length, 0);
    },
    enabled: Boolean(window),
    // "今天"的窗口每小时换 key；换窗口/重挂载时旧数据先撑住画面。
    placeholderData: keepPreviousData,
  });

  const flatFrames = useMemo(() => feedQuery.data?.pages.flatMap((page) => page.frames) ?? [], [feedQuery.data]);
  const totalFrames = feedQuery.data?.pages[0]?.total ?? 0;
  const newestFrameId = flatFrames[0]?.id ?? null;
  const hourGroups = useMemo(() => groupFramesByHour(flatFrames), [flatFrames]);

  // 弹窗层级走 URL：0=列表 1=单帧。推入的条目数由深度变化统一记账。
  const frameParam = searchParams.get("f");
  const openFrameId =
    frameParam !== null && frameParam !== "" && Number.isFinite(Number(frameParam))
      ? Number(frameParam)
      : null;
  const dialogOpen = openFrameId !== null;

  const oursRef = useRef(0);
  const prevDepthRef = useRef(dialogOpen ? 1 : 0);
  useEffect(() => {
    const depth = dialogOpen ? 1 : 0;
    const prev = prevDepthRef.current;
    if (depth > prev) oursRef.current += depth - prev;
    else if (depth < prev) oursRef.current = Math.max(0, oursRef.current - (prev - depth));
    prevDepthRef.current = depth;
  }, [dialogOpen]);

  const openFrameIndex = dialogOpen ? flatFrames.findIndex((frame) => frame.id === openFrameId) : -1;
  const openFrame = openFrameIndex >= 0 ? flatFrames[openFrameIndex] : null;

  /** 推一条历史并打开单帧（push：系统返回键可原地关闭）。 */
  const openFrameDetail = (frameId: number) => {
    const next = new URLSearchParams(searchParams);
    next.set("f", String(frameId));
    setSearchParams(next);
  };

  /** 弹窗内翻页：replace，不灌历史（返回键仍然一步关闭弹窗）。 */
  const stepFrame = (delta: number) => {
    if (openFrameIndex < 0) return;
    const next = flatFrames[openFrameIndex + delta];
    if (!next) return;
    const params = new URLSearchParams(searchParams);
    params.set("f", String(next.id));
    setSearchParams(params, { replace: true });
  };

  /** 弹窗 → 列表：优先回退本视图推入的那条历史。 */
  const closeDialog = () => {
    if (oursRef.current > 0) {
      historyBack();
      return;
    }
    const next = new URLSearchParams(searchParams);
    next.delete("f");
    setSearchParams(next, { replace: true });
  };

  // 方向键翻图：← = 上一张（更新），→ = 下一张（更早）。监听挂在 document
  // 上（组件内的 `window` 变量遮蔽了全局 window）；弹窗开着才生效，且在输入
  // 框聚焦时不抢按键。
  const stepFrameRef = useRef<(delta: number) => void>(() => undefined);
  stepFrameRef.current = stepFrame;
  useEffect(() => {
    if (!dialogOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        stepFrameRef.current(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        stepFrameRef.current(1);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dialogOpen]);

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
    setCustom({ fromMs: from, toMs: to });
  };

  const pickDay = (days: number) => {
    setDaysAgo(days);
    setCustom(null);
    setRangeError("");
  };

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

  // 无极滚动哨兵：进入视口（含 800px 预加载区）就向后拉一页。
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (
          entries.some((entry) => entry.isIntersecting) &&
          feedQuery.hasNextPage &&
          !feedQuery.isFetchingNextPage
        ) {
          void feedQuery.fetchNextPage();
        }
      },
      { rootMargin: "800px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [feedQuery.hasNextPage, feedQuery.isFetchingNextPage, feedQuery.fetchNextPage]);

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
            onClick={() => void feedQuery.refetch()}
            disabled={feedQuery.isFetching || !window}
          >
            <RefreshCw className={cn("size-4", feedQuery.isFetching && "animate-spin")} aria-hidden />
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
            {feedQuery.isError
              ? "读取失败"
              : feedQuery.data
                ? `共 ${totalFrames} 帧 · 最新在前`
                : "读取中…"}
          </span>
        </p>
      ) : null}

      {rangeError ? <Banner tone="error">{rangeError}</Banner> : null}

      {!window ? (
        <LoadingBlock label="等待服务端校时后再查询时间线…" />
      ) : feedQuery.isError ? (
        <ErrorBlock
          message={isApiError(feedQuery.error) ? feedQuery.error.message : "无法读取时间线"}
          retryable
          onRetry={() => void feedQuery.refetch()}
        />
      ) : feedQuery.isLoading ? (
        <LoadingBlock label="正在读取时间线…" rows={4} />
      ) : flatFrames.length === 0 ? (
        <EmptyBlock
          title="这个时间段没有画面"
          description="受控端只在有已登录桌面会话时采集屏幕；如果设备当时离线、没有登录用户，或采集被关闭，这里就是空的。"
        />
      ) : (
        <div className="min-w-0">
          {/* 垂直时间线：左列时间标记 + 轨道 + 截图流；整页只有一条连续的轨。 */}
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
                      <div className="tnum mt-1 text-xs text-faint">{group.frames.length} 帧</div>
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
                        <div className="mb-3 text-xs font-medium text-muted-foreground">最新</div>
                      ) : null}
                      <div className={cn("grid min-w-0 gap-3", frameGridClass)}>
                        {group.frames.map((frame) => (
                          <button
                            key={frame.id}
                            type="button"
                            onClick={() => openFrameDetail(frame.id)}
                            className="relative block w-full min-w-0 overflow-hidden rounded-lg border border-border-soft bg-card text-left shadow-sm transition-shadow hover:shadow-md focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
                            title={`${formatDateTime(frame.ts_ms)} 的画面`}
                          >
                            <span className="relative block w-full overflow-hidden">
                              <span className="ratio-16-9 aspect-video relative block w-full bg-muted">
                                <span className="absolute-fill block overflow-hidden">
                                  <FrameView
                                    uid={uid}
                                    frameId={frame.id}
                                    variant="thumb"
                                    alt={`${formatDateTime(frame.ts_ms)} 的画面`}
                                    className="object-cover"
                                  />
                                </span>
                              </span>
                            </span>
                            <span className="tnum flex min-w-0 items-center justify-between gap-2 p-2.5">
                              <span className="min-w-0 truncate text-sm font-medium text-foreground">
                                {formatDateTime(frame.ts_ms).slice(11)}
                              </span>
                              <span className="shrink-0 text-xs text-muted-foreground">
                                {formatDateTime(frame.ts_ms).slice(5, 10)}
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

          {/* 无极滚动哨兵。 */}
          <div ref={sentinelRef} aria-hidden className="h-2" />
          <div className="pt-4 text-center">
            {feedQuery.hasNextPage ? (
              <span className="tnum animate-pulse text-xs text-muted-foreground">
                {feedQuery.isFetchingNextPage
                  ? "正在加载更多…"
                  : `向下滚动加载更多（已显示 ${flatFrames.length}/${totalFrames}）`}
              </span>
            ) : (
              <span className="tnum text-xs text-faint">已全部加载 · 共 {totalFrames} 帧</span>
            )}
          </div>
        </div>
      )}

      <Dialog
        open={dialogOpen}
        onOpenChange={(open) => {
          if (!open) closeDialog();
        }}
      >
        <DialogContent size="xl" className="overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="tnum">
              {openFrame ? formatDateTime(frameTimeMs(openFrame)) : "画面原图"}
            </DialogTitle>
            <DialogDescription>
              {openFrameIndex >= 0
                ? `第 ${openFrameIndex + 1} / ${flatFrames.length} 张 · 已加载范围内`
                : "原图"}
            </DialogDescription>
          </DialogHeader>

          <div className="grid min-w-0 gap-2.5">
            <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap sm:items-center sm:gap-2">
              <Button
                variant="outline"
                className="min-h-9"
                disabled={openFrameIndex <= 0}
                onClick={() => stepFrame(-1)}
                title="更新一点的画面"
              >
                <ChevronLeft className="size-4" aria-hidden />
                上一张
              </Button>
              <Button
                variant="outline"
                className="min-h-9"
                disabled={openFrameIndex < 0 || openFrameIndex >= flatFrames.length - 1}
                onClick={() => stepFrame(1)}
                title="更早一点的画面"
              >
                下一张
                <ChevronRight className="size-4" aria-hidden />
              </Button>
              <a
                href={api.frameUrl(uid, openFrameId ?? 0, "full")}
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
                frameId={openFrameId}
                variant="full"
                alt={openFrame ? `${formatDateTime(frameTimeMs(openFrame))} 的原图` : "画面原图"}
                className="max-h-[62vh] w-full"
                eager
              />
            </div>
            {openFrame ? (
              <p className="text-xs leading-relaxed text-muted-foreground">
                设备时间 <span className="tnum">{formatDateTime(openFrame.device_time_ms)}</span> · 服务端时间{" "}
                <span className="tnum">{formatDateTime(openFrame.ts_ms)}</span> ·{" "}
                <span className="tnum">
                  {openFrame.width}×{openFrame.height}
                </span>{" "}
                · {formatBytes(openFrame.size_bytes)}
              </p>
            ) : null}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
