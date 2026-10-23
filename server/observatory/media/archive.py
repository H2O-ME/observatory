"""远端归档闭环（A14）。

0.2.10 的问题不是"归档做错了"，而是**归档根本没有闭环**：每帧一个 fire-and-forget
``create_task``、不记状态、不校验上传结果、本地保留完全不等归档；路径把 uid 拼了
两次，删除设备时只扫一层目录，所以恒删 0 个文件。

0.3.0 的闭环由四件事组成：

1. **状态机落库**：``frame_archives`` 每帧一行（待上传 → 已上传 / 失败 → 已删除），
   任何时刻都能回答"这一帧归档到哪一步了"；
2. **有界队列 + 单 worker**：队列满不丢数据——行仍是"待上传"，由 :func:`sweep`
   按指数退避重新入队；禁止每帧裸 ``create_task``；
3. **上传后回读校验**：PUT 成功后 GET 回读远端内容，比对 sha256，不一致记失败，
   不把"HTTP 200"当成"归档成功"；
4. **递归删除**：删设备时用 Git Trees API 建一棵不含该设备子树的树，提交后移动分支，
   一次删干净（不再逐层扫目录）。

保留侧通过 ``frame_archives.status`` 判断能否删本地：未上传成功的帧在宽限期内不删，
超期才删并记告警，避免归档长期失败导致历史无限膨胀。

纯决策（远端路径、重试时刻、树裁剪）都是模块级函数，可脱离 DB/网络单测。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import posixpath
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncIterator, Callable

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from observatory.core.clock import now_ms as _now_ms
from observatory.core.models import Device, Frame, FrameArchive

from . import settings as media_settings_mod
from . import storage, transcode

log = logging.getLogger(__name__)

GITHUB_API = media_settings_mod.GITHUB_API_DEFAULT

# §6bis 的状态取值（直接落库，控制台按此展示）
STATUS_PENDING = "待上传"
STATUS_UPLOADED = "已上传"
STATUS_FAILED = "失败"
STATUS_DELETED = "已删除"
STATUSES = (STATUS_PENDING, STATUS_UPLOADED, STATUS_FAILED, STATUS_DELETED)
# 供控制台/校验方做"状态是否合法"的判断（与 §6bis 的四个取值一致）
ARCHIVE_STATUS = STATUSES

_HTTP_TIMEOUT_S = 45.0


class ArchiveError(RuntimeError):
    """归档远端操作失败（可观测异常：调用方必须向用户显示原因）。"""


def _day_of(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# 纯函数：路径 / 重试时刻 / 树裁剪
# --------------------------------------------------------------------------
def remote_path_for(prefix: str, frame_path: str) -> str:
    """本地相对路径 → 远端仓库路径。

    本地路径形如 ``frames/<uid>/<day>/<hour>/<stem>.webp``；远端布局是
    ``<prefix>/<uid>/<day>/<hour>/<stem>.webp``（去掉本地的 ``frames/`` 根，
    **uid 只出现一次**——旧实现把 frames/ 去掉后又拼了一次 uid，导致
    ``prefix/uid/uid/...``）。
    """

    rel = str(frame_path or "").replace("\\", "/").strip("/")
    if not rel:
        raise storage.StorageError("空帧路径无法生成归档路径")
    head, _, tail = rel.partition("/")
    if head in {"frames", "thumbs"} and tail:
        rel = tail
    cleaned_prefix = str(prefix or "").strip().strip("/")
    return posixpath.normpath(posixpath.join(cleaned_prefix, rel)) if cleaned_prefix else posixpath.normpath(rel)


def local_path_for(prefix: str, remote_path: str) -> str:
    """远端仓库路径 → 本地相对帧路径（:func:`remote_path_for` 的逆函数）。"""

    remote = str(remote_path or "").replace("\\", "/").strip("/")
    if not remote:
        raise storage.StorageError("空归档路径无法还原本地路径")
    cleaned_prefix = str(prefix or "").strip().strip("/")
    if cleaned_prefix:
        if remote == cleaned_prefix:
            raise storage.StorageError(f"归档路径缺少设备子路径: {remote_path!r}")
        if remote.startswith(cleaned_prefix + "/"):
            remote = remote[len(cleaned_prefix) + 1 :]
    if remote.startswith("frames/"):
        return remote
    return f"frames/{remote}"


def retry_delay_ms(attempts: int, *, base_s: int, cap_s: int) -> int:
    """指数退避（纯函数，无抖动，便于断言）：base, 2·base, 4·base … 封顶 cap。"""

    base_ms = max(1, int(base_s)) * 1000
    cap_ms = max(base_ms, int(cap_s) * 1000)
    exponent = max(0, min(20, int(attempts) - 1))
    return min(cap_ms, base_ms * (2**exponent))


def retry_at_ms(row: Any, settings: media_settings_mod.MediaSettings) -> int:
    """该归档行最早可重试的时刻（以创建时间为锚，DB 之外不存额外状态）。"""

    created = int(getattr(row, "created_at_ms", 0) or 0)
    attempts = max(0, int(getattr(row, "attempts", 0) or 0))
    return created + retry_delay_ms(attempts, base_s=settings.archive_retry_base_s, cap_s=settings.archive_retry_cap_s)


def retry_eligible(row: Any, settings: media_settings_mod.MediaSettings, *, now_ms: int, max_attempts: int | None = None) -> bool:
    """是否应把这一行重新入队。"""

    status = getattr(row, "status", "")
    if status not in (STATUS_PENDING, STATUS_FAILED):
        return False
    limit = int(settings.archive_max_attempts if max_attempts is None else max_attempts)
    if int(getattr(row, "attempts", 0) or 0) >= max(1, limit):
        return False
    return now_ms >= retry_at_ms(row, settings)


def topmost_entries(tree_entries: list[dict[str, Any]], base_path: str) -> list[dict[str, Any]]:
    """从 Git 树列表里挑出覆盖 ``base_path`` 整棵子树的最少条目。

    Trees API 的批量删除只接受"要删的路径"，删目录等价于删整棵子树，所以优先返回
    ``base_path`` 本身（若它在树里），否则返回它的直接子目录/文件。
    """

    base = str(base_path or "").strip("/")
    if not base:
        return []
    candidates = {base}
    for entry in tree_entries:
        path = str(entry.get("path", "")).strip("/")
        if not path.startswith(base + "/"):
            continue
        remainder = path[len(base) + 1 :]
        first = remainder.split("/", 1)[0]
        if first:
            candidates.add(f"{base}/{first}")
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in tree_entries:
        path = str(entry.get("path", "")).strip("/")
        if path in candidates and path not in seen:
            picked.append(entry)
            seen.add(path)
    # 只保留最上层条目：删父目录已经覆盖整棵子树，再删子项是冗余请求
    # （Git 树接口对"父项与子项同时删除"的行为并不保证）。
    picked.sort(key=lambda item: len(str(item.get("path", ""))))
    kept: list[dict[str, Any]] = []
    kept_paths: list[str] = []
    for entry in picked:
        path = str(entry.get("path", "")).strip("/")
        if any(path == parent or path.startswith(parent + "/") for parent in kept_paths):
            continue
        kept.append(entry)
        kept_paths.append(path)
    return kept


def verify_digest(data: bytes, expected_sha256: str) -> bool:
    return bool(expected_sha256) and storage.sha256_of(data) == expected_sha256


def kind_from_remote_path(remote_path: str) -> str:
    """从远端文件名还原帧类型（``<kind>-<ts>`` 前缀约定；纯时间戳即 screen）。"""

    stem = posixpath.splitext(posixpath.basename(str(remote_path or "")))[0]
    head = stem.split("-", 1)[0]
    if head and not head.isdigit():
        return head[:16]
    return "screen"


def thumb_relative_for(local_relative: str) -> str | None:
    """本地帧相对路径 → 缩略图相对路径（``frames/`` → ``thumbs/``）。"""

    relative = str(local_relative or "").replace("\\", "/").strip("/")
    if relative.startswith("frames/"):
        return f"thumbs/{relative[len('frames/'):]}"
    return None


# --------------------------------------------------------------------------
# 队列任务
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ArchiveJob:
    archive_id: int
    settings: media_settings_mod.MediaSettings


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)


class ArchiveService:
    """有界队列 + 状态机的归档执行器（进程内单例，见 :func:`get_service`）。"""

    def __init__(
        self,
        *,
        settings: media_settings_mod.MediaSettings | None = None,
        session_scope: Callable[[], Any] | None = None,
        client_factory: Callable[[], Any] = _default_client,
        clock: Callable[[], int] = _now_ms,
        api_base: str | None = None,
    ) -> None:
        self._settings = settings
        self._session_scope = session_scope
        self._client_factory = client_factory
        self._clock = clock
        #: 显式覆盖 GitHub API 根地址（e2e/自建实例）；空值表示用 settings.archive_api_base。
        self._api_base_override = str(api_base or "").strip().rstrip("/")
        self._queue: asyncio.Queue[ArchiveJob] = asyncio.Queue(maxsize=256)
        self._worker: asyncio.Task[None] | None = None
        self._stats: dict[str, int] = {
            "enqueued": 0,
            "uploaded": 0,
            "failed": 0,
            "verify_failed": 0,
            "queue_full": 0,
            "remote_deleted": 0,
            "worker_errors": 0,
        }
        self._last_error = ""

    # -- 生命周期 ---------------------------------------------------------
    async def start(self, *, queue_size: int | None = None) -> None:
        """启动唯一 worker。由 lifespan 调用；不随每帧启动。"""

        if self._worker is not None and not self._worker.done():
            return
        if queue_size is not None and int(queue_size) > 0:
            self._queue = asyncio.Queue(maxsize=int(queue_size))
        self._worker = asyncio.create_task(self._run_worker(), name="media-archive-worker")
        self._worker.add_done_callback(self._on_worker_done)

    async def stop(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.cancel()
        with suppress(asyncio.CancelledError):
            # 取消是这个 worker 的唯一正常退出方式；不记录为异常。
            await worker
        log.info("归档 worker 已停止")

    def _on_worker_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._stats["worker_errors"] += 1
            self._last_error = f"归档 worker 异常退出: {exc!r}"
            log.error("归档 worker 异常退出", exc_info=exc)

    def stats(self) -> dict[str, Any]:
        return {
            **self._stats,
            "queue_size": self._queue.qsize(),
            "worker_running": bool(self._worker is not None and not self._worker.done()),
            "last_error": self._last_error,
        }

    # -- 公共入口 ---------------------------------------------------------
    async def schedule(
        self,
        *,
        session: AsyncSession,
        device: Any,
        frame: Frame,
        settings: media_settings_mod.MediaSettings | Any | None = None,
        now_ms: int | None = None,
    ) -> FrameArchive | None:
        """登记一帧的归档（幂等：同一远端路径只有一行）并尝试入队。

        返回状态行；归档未启用时返回 ``None``。队列满时行保持"待上传"，
        由 :meth:`sweep` 重试——绝不丢帧，也绝不 fire-and-forget。
        """

        media_settings = media_settings_mod.resolve(settings, base=self._settings or media_settings_mod.defaults())
        if not media_settings.archive_ready:
            return None
        moment = self._clock() if now_ms is None else int(now_ms)
        remote = remote_path_for(media_settings.archive_path_prefix, frame.path)

        existing = (
            await session.execute(
                select(FrameArchive).where(
                    FrameArchive.device_id == device.id,
                    FrameArchive.remote_path == remote,
                )
            )
        ).scalars().first()
        if existing is not None:
            if existing.status in (STATUS_PENDING, STATUS_FAILED):
                self._enqueue(ArchiveJob(existing.id, media_settings))
            return existing

        row = FrameArchive(
            device_id=int(device.id),
            frame_id=getattr(frame, "frame_id", None),
            day=_day_of(int(frame.ts_ms)),
            remote_path=remote,
            sha256=str(frame.sha256 or ""),
            size_bytes=int(frame.size_bytes or 0),
            status=STATUS_PENDING,
            attempts=0,
            created_at_ms=moment,
            uploaded_at_ms=None,
            error="",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        self._enqueue(ArchiveJob(row.id, media_settings))
        return row

    async def sweep(self, session: AsyncSession, *, settings: Any | None = None, limit: int = 32) -> int:
        """把到期可重试的归档行重新入队；由 retention 的周期批次驱动。"""

        media_settings = media_settings_mod.resolve(settings, base=self._settings or media_settings_mod.defaults())
        if not media_settings.archive_ready:
            return 0
        moment = self._clock()
        rows = (
            await session.execute(
                select(FrameArchive)
                .where(FrameArchive.status.in_((STATUS_PENDING, STATUS_FAILED)))
                .order_by(FrameArchive.created_at_ms.asc())
                .limit(max(1, int(limit)))
            )
        ).scalars().all()
        enqueued = 0
        for row in rows:
            if retry_eligible(row, media_settings, now_ms=moment):
                if self._enqueue(ArchiveJob(row.id, media_settings)):
                    enqueued += 1
        if enqueued:
            log.info("归档重试入队 %d 条", enqueued)
        return enqueued

    async def purge_device(self, session: AsyncSession, device: Any, *, settings: Any | None = None) -> int:
        """删设备时递归删除远端归档，成功后才把状态行标记"已删除"。

        返回远端删除的条目数；失败时行保持原状态并记录 ``error``（可观测、可重试）。
        """

        media_settings = media_settings_mod.resolve(settings, base=self._settings or media_settings_mod.defaults())
        rows = (
            await session.execute(select(FrameArchive).where(FrameArchive.device_id == int(device.id)))
        ).scalars().all()
        if not rows:
            return 0

        removed = 0
        failure = ""
        if media_settings.archive_ready and media_settings.archive_remote_delete:
            base_path = posixpath.join(
                str(media_settings.archive_path_prefix or "").strip("/"),
                storage.safe_uid(str(device.uid)),
            ).strip("/")
            try:
                removed = await self._delete_remote_tree(media_settings, base_path)
            except (httpx.HTTPError, storage.StorageError, ValueError, KeyError, TypeError) as exc:
                # KeyError/TypeError 只在 GitHub 返回结构异常时出现，同样属于"可观测失败"，
                # 不能让删设备请求半途抛 500。
                failure = f"远端递归删除失败: {exc}"[:500]
                log.error("归档远端递归删除失败 device=%s path=%s: %s", device.uid, base_path, exc)

        if failure:
            for row in rows:
                row.error = failure
                row.status = STATUS_FAILED
            await session.commit()
            self._last_error = failure
            return 0

        for row in rows:
            row.status = STATUS_DELETED
            row.error = ""
        await session.commit()
        self._stats["remote_deleted"] += removed
        log.info("归档清理完成 device=%s 远端条目=%d 状态行=%d", device.uid, removed, len(rows))
        return removed

    # -- 备份页（C-12）：取回 / 单条远端删除 --------------------------------
    async def fetch_bytes(
        self, row: FrameArchive, *, settings: media_settings_mod.MediaSettings | Any | None = None
    ) -> bytes:
        """从远端取回一帧原始字节并校验 sha256；失败抛 :class:`ArchiveError`。"""

        media_settings = media_settings_mod.resolve(settings, base=self._settings or media_settings_mod.defaults())
        if not media_settings.archive_ready:
            raise ArchiveError("归档未启用或未配置仓库/令牌")
        try:
            async with self._client_factory() as client:
                response = await client.get(
                    self._contents_url(media_settings, row.remote_path),
                    headers=self._headers(media_settings.archive_token),
                    params={"ref": media_settings.archive_branch},
                )
                status = int(getattr(response, "status_code", 0))
                if status != 200:
                    raise ArchiveError(f"取回归档失败 HTTP {status}: {row.remote_path}")
                body = response.json()
        except httpx.HTTPError as exc:
            raise ArchiveError(f"取回归档网络错误: {exc}") from exc
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            raise ArchiveError("取回归档响应缺少 content 字段")
        try:
            data = base64.b64decode(content)
        except (ValueError, TypeError) as exc:
            raise ArchiveError(f"取回归档内容 base64 解码失败: {exc}") from exc
        if row.sha256 and not verify_digest(data, str(row.sha256)):
            raise ArchiveError("取回归档内容 sha256 与登记值不一致")
        return data

    async def restore(
        self,
        session: AsyncSession,
        row: FrameArchive,
        *,
        settings: media_settings_mod.MediaSettings | Any | None = None,
    ) -> Frame:
        """把远端归档的一帧取回本地并补回 ``frames`` 行（幂等）。

        用途：本地已被保留策略删除后，控制台"备份页"仍能取回历史画面。
        失败抛 :class:`ArchiveError`；成功返回 Frame 行。
        """

        media_settings = media_settings_mod.resolve(settings, base=self._settings or media_settings_mod.defaults())
        data = await self.fetch_bytes(row, settings=media_settings)
        local_relative = local_path_for(media_settings.archive_path_prefix, row.remote_path)
        try:
            frame_path = storage.absolute(media_settings, local_relative)
        except storage.StorageError as exc:
            raise ArchiveError(f"归档路径无法映射到本地: {exc}") from exc

        existing: Frame | None = None
        if row.frame_id:
            existing = (
                await session.execute(
                    select(Frame).where(Frame.device_id == row.device_id, Frame.frame_id == row.frame_id).limit(1)
                )
            ).scalars().first()
        if existing is None:
            existing = (
                await session.execute(select(Frame).where(Frame.path == local_relative).limit(1))
            ).scalars().first()

        try:
            await asyncio.to_thread(storage.write_atomic, frame_path, data)
        except OSError as exc:
            raise ArchiveError(f"取回后写入本地失败: {exc}") from exc

        thumb_relative = thumb_relative_for(local_relative)
        width, height, thumb_bytes = 0, 0, b""
        try:
            width, height = await asyncio.to_thread(transcode.image_size, data)
            thumb_bytes, _, _ = await asyncio.to_thread(
                transcode.encode_thumbnail, data, media_settings.thumb_width, media_settings.thumb_quality
            )
            if thumb_relative:
                await asyncio.to_thread(
                    storage.write_atomic, storage.absolute(media_settings, thumb_relative), thumb_bytes
                )
        except (transcode.ImageError, storage.StorageError, OSError) as exc:
            # 缩略图是派生数据：失败只降级（列表仍能显示原图），原图已安全落盘。
            log.warning("取回归档缩略图生成失败 %s: %s", row.remote_path, exc)
            thumb_relative = None if not thumb_relative else thumb_relative

        if existing is not None:
            existing.archived_at_ms = int(self._clock())
            await session.commit()
            return existing

        frame = Frame(
            device_id=int(row.device_id),
            ts_ms=int(row.created_at_ms),
            device_ts_ms=None,
            session=0,
            kind=kind_from_remote_path(row.remote_path),
            frame_id=row.frame_id,
            path=local_relative,
            thumb_path=thumb_relative,
            width=int(width),
            height=int(height),
            size_bytes=len(data),
            sha256=str(row.sha256 or storage.sha256_of(data)),
            archived_at_ms=int(self._clock()),
        )
        session.add(frame)
        await session.commit()
        await session.refresh(frame)
        log.info("归档取回成功 %s -> %s", row.remote_path, local_relative)
        return frame

    async def delete_entry(
        self,
        session: AsyncSession,
        row: FrameArchive,
        *,
        settings: media_settings_mod.MediaSettings | Any | None = None,
    ) -> bool:
        """删除单条远端归档；成功后才把状态行置"已删除"，失败抛 :class:`ArchiveError`。"""

        media_settings = media_settings_mod.resolve(settings, base=self._settings or media_settings_mod.defaults())
        if not media_settings.archive_ready:
            raise ArchiveError("归档未启用或未配置仓库/令牌，无法删除远端条目")
        url = self._contents_url(media_settings, row.remote_path)
        headers = self._headers(media_settings.archive_token)
        try:
            async with self._client_factory() as client:
                head = await client.get(url, headers=headers, params={"ref": media_settings.archive_branch})
                head_status = int(getattr(head, "status_code", 0))
                if head_status == 404:
                    row.status = STATUS_DELETED
                    row.error = ""
                    await session.commit()
                    return True
                if head_status != 200:
                    raise ArchiveError(f"读取远端条目失败 HTTP {head_status}")
                body = head.json()
                blob_sha = str(body.get("sha") or "")
                if not blob_sha:
                    raise ArchiveError("远端响应缺少 sha，拒绝在无法确认版本的情况下删除")
                response = await client.request(
                    "DELETE",
                    url,
                    headers=headers,
                    json={
                        "message": f"purge: {row.remote_path}",
                        "sha": blob_sha,
                        "branch": media_settings.archive_branch,
                    },
                )
                status = int(getattr(response, "status_code", 0))
                if status not in (200, 204):
                    raise ArchiveError(f"删除远端条目失败 HTTP {status}")
        except httpx.HTTPError as exc:
            raise ArchiveError(f"删除远端条目网络错误: {exc}") from exc

        row.status = STATUS_DELETED
        row.error = ""
        await session.commit()
        self._stats["remote_deleted"] += 1
        log.info("归档远端条目已删除 %s", row.remote_path)
        return True

    # -- 队列 -------------------------------------------------------------
    def _enqueue(self, job: ArchiveJob) -> bool:
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            # 队列满不是错误：状态行仍是"待上传"，sweep 会再入队。
            self._stats["queue_full"] += 1
            self._last_error = "归档队列已满，等待 sweep 重试"
            log.warning("归档队列已满（容量 %d），归档行 %d 留待重试", self._queue.maxsize, job.archive_id)
            return False
        self._stats["enqueued"] += 1
        return True

    async def _run_worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker 不能因为单帧异常退出，否则整条归档链停摆
                self._stats["worker_errors"] += 1
                self._last_error = f"归档任务异常: {exc!r}"
                log.exception("归档任务异常 archive_id=%d", job.archive_id)
            finally:
                self._queue.task_done()

    # -- 单任务处理 -------------------------------------------------------
    async def _process(self, job: ArchiveJob) -> None:
        media_settings = job.settings
        async with self._open_session() as session:
            row = await session.get(FrameArchive, job.archive_id)
            if row is None:
                log.warning("归档行 %d 已不存在，跳过", job.archive_id)
                return
            if row.status in (STATUS_UPLOADED, STATUS_DELETED):
                return

            local_relative = ""
            try:
                local_relative = local_path_for(media_settings.archive_path_prefix, row.remote_path)
                path = storage.absolute(media_settings, local_relative)
            except storage.StorageError as exc:
                await self._mark_failed(session, row, f"本地路径非法: {exc}")
                return

            try:
                data = await asyncio.to_thread(path.read_bytes)
            except OSError as exc:
                await self._mark_failed(session, row, f"读取本地帧失败: {exc}")
                return

            if not verify_digest(data, str(row.sha256 or "")):
                await self._mark_failed(session, row, "本地帧摘要与入库记录不一致，拒绝上传")
                return

            ok, message = await self._upload(media_settings, row, data)
            if not ok:
                await self._mark_failed(session, row, message)
                return

            moment = self._clock()
            row.status = STATUS_UPLOADED
            row.uploaded_at_ms = moment
            row.error = ""
            if row.frame_id:
                await session.execute(
                    update(Frame)
                    .where(Frame.device_id == row.device_id, Frame.frame_id == row.frame_id)
                    .values(archived_at_ms=moment)
                )
            await session.commit()
            self._stats["uploaded"] += 1
            log.debug("归档成功 %s", row.remote_path)

    async def _mark_failed(self, session: AsyncSession, row: FrameArchive, message: str) -> None:
        row.attempts = int(row.attempts or 0) + 1
        row.status = STATUS_FAILED
        row.error = str(message)[:500]
        await session.commit()
        self._stats["failed"] += 1
        if row.attempts <= 1:
            log.warning("归档失败（将按退避重试）%s: %s", row.remote_path, message)

    @asynccontextmanager
    async def _open_session(self) -> AsyncIterator[AsyncSession]:
        scope = self._session_scope
        if scope is None:
            from observatory.core import db as core_db

            scope = getattr(core_db, "session_scope", None) or getattr(core_db, "get_session", None)
        if scope is None:
            raise RuntimeError("core.db.session_scope 未提供，归档 worker 无法独立落库")
        async with scope() as session:
            yield session

    # -- HTTP -------------------------------------------------------------
    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _api_base(self, settings: media_settings_mod.MediaSettings) -> str:
        """GitHub API 根地址：显式覆盖 > settings.archive_api_base > 默认值。"""

        return self._api_base_override or (settings.archive_api_base or GITHUB_API).rstrip("/")

    def _contents_url(self, settings: media_settings_mod.MediaSettings, path: str) -> str:
        return f"{self._api_base(settings)}/repos/{settings.archive_repo}/contents/{path}"

    async def _upload(
        self, settings: media_settings_mod.MediaSettings, row: FrameArchive, data: bytes
    ) -> tuple[bool, str]:
        payload = {
            "message": f"archive: {row.remote_path}",
            "content": base64.b64encode(data).decode("ascii"),
            "branch": settings.archive_branch,
        }
        try:
            async with self._client_factory() as client:
                response = await client.put(
                    self._contents_url(settings, row.remote_path),
                    headers=self._headers(settings.archive_token),
                    json=payload,
                )
                status = int(getattr(response, "status_code", 0))
                if status not in (200, 201):
                    text = str(getattr(response, "text", ""))[:200]
                    self._last_error = f"归档 PUT 失败 {row.remote_path}: {status} {text}"
                    return False, f"PUT {status}: {text}"

                if not settings.archive_verify_readback:
                    return True, ""

                verified, message = await self._verify_remote(client, settings, row)
                if not verified:
                    self._stats["verify_failed"] += 1
                    self._last_error = message
                    return False, message
                return True, ""
        except httpx.HTTPError as exc:
            self._last_error = f"归档网络错误 {row.remote_path}: {exc}"
            return False, f"网络错误: {exc}"

    async def _verify_remote(
        self, client: Any, settings: media_settings_mod.MediaSettings, row: FrameArchive
    ) -> tuple[bool, str]:
        """回读远端内容并比对 sha256——HTTP 200 不等于内容正确。"""

        response = await client.get(
            self._contents_url(settings, row.remote_path),
            headers=self._headers(settings.archive_token),
            params={"ref": settings.archive_branch},
        )
        status = int(getattr(response, "status_code", 0))
        if status != 200:
            return False, f"回读 HTTP {status}，无法确认归档内容"
        try:
            body = response.json()
        except ValueError as exc:
            return False, f"回读响应不是 JSON: {exc}"
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            return False, "回读响应缺少 content 字段"
        try:
            remote_bytes = base64.b64decode(content)
        except (ValueError, TypeError) as exc:
            return False, f"回读内容 base64 解码失败: {exc}"
        if not verify_digest(remote_bytes, str(row.sha256 or "")):
            return False, "回读摘要与本地摘要不一致"
        return True, ""

    async def _delete_remote_tree(self, settings: media_settings_mod.MediaSettings, base_path: str) -> int:
        headers = self._headers(settings.archive_token)
        repo = settings.archive_repo
        branch = settings.archive_branch
        api = self._api_base(settings)
        async with self._client_factory() as client:
            ref = await client.get(f"{api}/repos/{repo}/git/ref/heads/{branch}", headers=headers)
            if int(getattr(ref, "status_code", 0)) != 200:
                raise ValueError(f"读取分支引用失败 HTTP {ref.status_code}")
            commit_sha = str(ref.json()["object"]["sha"])

            commit = await client.get(f"{api}/repos/{repo}/git/commits/{commit_sha}", headers=headers)
            if int(getattr(commit, "status_code", 0)) != 200:
                raise ValueError(f"读取提交失败 HTTP {commit.status_code}")
            tree_sha = str(commit.json()["tree"]["sha"])

            tree = await client.get(
                f"{api}/repos/{repo}/git/trees/{tree_sha}", headers=headers, params={"recursive": "1"}
            )
            if int(getattr(tree, "status_code", 0)) != 200:
                raise ValueError(f"读取树失败 HTTP {tree.status_code}")
            tree_body = tree.json()
            entries = tree_body.get("tree") or []
            if tree_body.get("truncated"):
                log.warning("归档仓库树过大被截断，递归删除可能不完整 repo=%s", repo)

            matched = [
                entry
                for entry in entries
                if str(entry.get("path", "")).strip("/") == base_path
                or str(entry.get("path", "")).strip("/").startswith(base_path + "/")
            ]
            picked = topmost_entries(entries, base_path)
            if not picked:
                log.info("归档远端不存在该设备子树，无需删除 path=%s", base_path)
                return 0

            deletions = [
                {"path": str(item["path"]), "mode": item.get("mode", "100644"), "type": item.get("type", "blob"), "sha": None}
                for item in picked
            ]
            new_tree = await client.post(
                f"{api}/repos/{repo}/git/trees",
                headers=headers,
                json={"base_tree": tree_sha, "tree": deletions},
            )
            if int(getattr(new_tree, "status_code", 0)) not in (200, 201):
                raise ValueError(f"创建删除树失败 HTTP {new_tree.status_code}: {str(new_tree.text)[:200]}")

            new_commit = await client.post(
                f"{api}/repos/{repo}/git/commits",
                headers=headers,
                json={
                    "message": f"purge: {base_path}",
                    "tree": new_tree.json()["sha"],
                    "parents": [commit_sha],
                },
            )
            if int(getattr(new_commit, "status_code", 0)) not in (200, 201):
                raise ValueError(f"创建删除提交失败 HTTP {new_commit.status_code}")

            moved = await client.patch(
                f"{api}/repos/{repo}/git/refs/heads/{branch}",
                headers=headers,
                json={"sha": new_commit.json()["sha"], "force": False},
            )
            if int(getattr(moved, "status_code", 0)) not in (200, 201):
                raise ValueError(f"移动分支失败 HTTP {moved.status_code}")

            return len(matched)


_service: ArchiveService | None = None


def get_service(**kwargs: Any) -> ArchiveService:
    global _service
    if _service is None:
        _service = ArchiveService(**kwargs)
    return _service


def set_service(service: ArchiveService | None) -> None:
    """测试/装配用：替换单例。"""

    global _service
    _service = service


# ---- 模块级门面：ingest / retention / console 统一从这里调用 -----------------
async def schedule(
    *,
    session: AsyncSession,
    device: Any,
    frame: Frame,
    settings: Any | None = None,
    now_ms: int | None = None,
) -> FrameArchive | None:
    return await get_service().schedule(session=session, device=device, frame=frame, settings=settings, now_ms=now_ms)


async def sweep(session: AsyncSession, *, settings: Any | None = None, limit: int = 32) -> int:
    return await get_service().sweep(session, settings=settings, limit=limit)


async def purge_device(session: AsyncSession, device: Any, *, settings: Any | None = None) -> int:
    return await get_service().purge_device(session, device, settings=settings)


def stats() -> dict[str, Any]:
    return get_service().stats()


async def summary(session: AsyncSession) -> dict[str, Any]:
    """给控制台备份页用的归档总览：状态分布 + 队列/失败计数（A14 可观测）。"""

    rows = (
        await session.execute(select(FrameArchive.status, func.count()).group_by(FrameArchive.status))
    ).all()
    counts = {status: 0 for status in STATUSES}
    for status, number in rows:
        counts[str(status)] = int(number)
    return {
        "statuses": counts,
        "pending": counts[STATUS_PENDING],
        "failed": counts[STATUS_FAILED],
        "uploaded": counts[STATUS_UPLOADED],
        "deleted": counts[STATUS_DELETED],
        **get_service().stats(),
    }


async def recent_failures(session: AsyncSession, *, limit: int = 20) -> list[dict[str, Any]]:
    """最近失败/待重试的归档行（控制台展示"哪一帧没备份成功"）。"""

    rows = (
        await session.execute(
            select(FrameArchive)
            .where(FrameArchive.status.in_((STATUS_PENDING, STATUS_FAILED)))
            .order_by(FrameArchive.created_at_ms.desc())
            .limit(max(1, int(limit)))
        )
    ).scalars().all()
    return [
        {
            "id": row.id,
            "device_id": row.device_id,
            "frame_id": row.frame_id,
            "day": row.day,
            "remote_path": row.remote_path,
            "status": row.status,
            "attempts": row.attempts,
            "created_at_ms": row.created_at_ms,
            "uploaded_at_ms": row.uploaded_at_ms,
            "error": row.error,
        }
        for row in rows
    ]


async def list_page(
    session: AsyncSession,
    *,
    status: str | None = None,
    device_uid: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """备份页分页查询（只读，控制台无需自己 join ``devices``）。

    返回 ``{"items": [...], "total": int, "limit": int, "offset": int}``。
    ``items`` 同时带 ``device_uid``，取回/删除时可直接用 ``items[i]["id"]`` 再查行。
    """

    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    conditions = []
    if status:
        conditions.append(FrameArchive.status == str(status))
    if device_uid:
        conditions.append(Device.uid == str(device_uid))

    joined = select(FrameArchive, Device.uid).join(Device, Device.id == FrameArchive.device_id, isouter=True)
    counted = select(func.count()).select_from(FrameArchive).join(
        Device, Device.id == FrameArchive.device_id, isouter=True
    )
    for condition in conditions:
        joined = joined.where(condition)
        counted = counted.where(condition)

    total = int((await session.execute(counted)).scalar() or 0)
    rows = (
        await session.execute(joined.order_by(FrameArchive.created_at_ms.desc()).limit(limit).offset(offset))
    ).all()
    items = [
        {
            "id": row.id,
            "device_id": row.device_id,
            "device_uid": uid or "",
            "frame_id": row.frame_id,
            "day": row.day,
            "remote_path": row.remote_path,
            "sha256": row.sha256,
            "size_bytes": row.size_bytes,
            "status": row.status,
            "attempts": row.attempts,
            "created_at_ms": row.created_at_ms,
            "uploaded_at_ms": row.uploaded_at_ms,
            "error": row.error,
        }
        for row, uid in rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def fetch_bytes(row: FrameArchive, *, settings: Any | None = None) -> bytes:
    return await get_service().fetch_bytes(row, settings=settings)


async def restore(session: AsyncSession, row: FrameArchive, *, settings: Any | None = None) -> Frame:
    return await get_service().restore(session, row, settings=settings)


async def delete_entry(session: AsyncSession, row: FrameArchive, *, settings: Any | None = None) -> bool:
    return await get_service().delete_entry(session, row, settings=settings)


__all__ = [
    "ARCHIVE_STATUS",
    "ArchiveError",
    "ArchiveJob",
    "ArchiveService",
    "STATUSES",
    "STATUS_DELETED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_UPLOADED",
    "delete_entry",
    "fetch_bytes",
    "get_service",
    "kind_from_remote_path",
    "list_page",
    "local_path_for",
    "purge_device",
    "recent_failures",
    "remote_path_for",
    "restore",
    "retry_at_ms",
    "retry_delay_ms",
    "retry_eligible",
    "schedule",
    "set_service",
    "stats",
    "summary",
    "sweep",
    "thumb_relative_for",
    "topmost_entries",
    "verify_digest",
]