"""Timeline aggregation.

The old timeline asked the browser for full-size frames one at a time and the
client appended a cache-busting nonce to every blob URL, so scrubbing re-downloaded
everything. Here the server aggregates into time buckets and returns *ids*; the
browser then fetches thumbnails, which are cached as immutable.

Bucketed aggregation also means the payload stays constant (~600 entries) no
matter how many thousands of frames a day holds.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MAX_BUCKETS = 900
MIN_BUCKET_MS = 5_000
MAX_SPAN_MS = 31 * 24 * 3600 * 1000


class TimelineError(ValueError):
    pass


def choose_bucket(from_ms: int, to_ms: int, suggested_ms: int | None = None) -> int:
    span = max(1, to_ms - from_ms)
    bucket = suggested_ms or 60_000
    bucket = max(MIN_BUCKET_MS, bucket)
    if span / bucket > MAX_BUCKETS:
        bucket = span // MAX_BUCKETS
        bucket = max(MIN_BUCKET_MS, (bucket // MIN_BUCKET_MS) * MIN_BUCKET_MS)
    return int(bucket)


async def build(
    session: AsyncSession,
    *,
    device_id: int,
    from_ms: int,
    to_ms: int,
    bucket_ms: int | None = None,
) -> dict:
    if to_ms <= from_ms:
        raise TimelineError("结束时间必须晚于开始时间")
    if to_ms - from_ms > MAX_SPAN_MS:
        raise TimelineError("时间线跨度不能超过 31 天")

    bucket = choose_bucket(from_ms, to_ms, bucket_ms)

    # Integer division on the stored epoch milliseconds groups frames without
    # needing a date function, and MIN(ts_ms) keeps the representative frame at
    # the start of each bucket which matches how the UI renders gaps.
    statement = text(
        """
        SELECT (ts_ms / :bucket) AS bucket_index,
               COUNT(*) AS frame_count,
               MIN(id) AS frame_id,
               MIN(ts_ms) AS first_ts,
               MIN(session) AS session
        FROM frames
        WHERE device_id = :device_id AND ts_ms >= :from_ms AND ts_ms < :to_ms
        GROUP BY bucket_index
        ORDER BY bucket_index
        """
    )
    result = await session.execute(
        statement,
        {"bucket": bucket, "device_id": device_id, "from_ms": from_ms, "to_ms": to_ms},
    )
    buckets = [
        {
            "ts_ms": int(row.first_ts or (row.bucket_index * bucket)),
            "frame_id": int(row.frame_id) if row.frame_id is not None else None,
            "frame_count": int(row.frame_count),
            "session": int(row.session or 0),
        }
        for row in result
    ]
    return {
        "from_ms": from_ms,
        "to_ms": to_ms,
        "bucket_ms": bucket,
        "buckets": buckets,
        "total_frames": sum(bucket_row["frame_count"] for bucket_row in buckets),
    }


__all__ = ["MAX_SPAN_MS", "TimelineError", "build", "choose_bucket"]