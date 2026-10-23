"""链路的上行 outbox：**唯一**的上行重投队列（替代 0.2.10 的 ``spool.py``）。

0.2.10 有两条互不相干的上行重传路径：``spool.py`` 只重传采集帧，命令回执则在
WS 失败时临时退回 HTTP（`runner._send_result`），事件干脆直接丢弃。0.3.0 把
"所有要发给服务端的东西"收进这一个有界队列：

- 链路信封（hello / heartbeat / result / event / uninstall_ack / pong）；
- 帧上传单元（``payload`` + ``X-Frame-Id``，唯一的帧上传路径是 HTTP，见规格 C2）。

语义：
- **至少一次**：条目只在被确认后删除（WS 写成功、HTTP 批次 200、帧上传 200）。
  断链重连时 ``replay()`` 把未确认条目重新标为待发；服务端按信封 ``id`` /
  ``X-Frame-Id`` 去重，所以重投安全（规格 §5.3）。
- **last_ack_id 续传**：``last_ack_id`` 记住服务端最后确认过的上行 id，链路在
  每个 hello/heartbeat/HTTP 批次里带上它；服务端据此判断续传边界。
- **有界**：条数与字节数双上限；溢出策略可配置，每次丢弃都记 warning 并计数，
  不允许"悄悄丢"（C6：破坏性动作成功或可观测失败）。

本类不读系统时钟、不做 IO、不 import asyncio——时间由调用方传入。它持有的是
实例状态（不是全局状态），因此可以直接在单测里构造多个独立实例断言。
线程模型：设计为**单 asyncio 事件循环内使用**；其它线程要入队必须经
``loop.call_soon_threadsafe`` 投递，不要直接调用。

W1 持久化：默认仍是纯内存（单测/worker 用一个短命实例最省事），但可注入
:class:`SqliteOutboxStore` 把"未确认条目 + 两个 ack 游标"落盘。进程被 kill -9
后重启，``Outbox`` 从磁盘回放全部未确认项并清零 ``sent_at_ms``，链路重投；
服务端按信封 id / ``X-Frame-Id`` 做 5 分钟去重，所以重投不会重复处理（A10）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from .envelope import Envelope, new_envelope, new_id

log = logging.getLogger(__name__)

#: outbox 里"帧上传单元"的 kind；与信封类型名不冲突。
FRAME_KIND: Final[str] = "frame"

OVERFLOW_DROP_OLDEST: Final[str] = "drop_oldest"
OVERFLOW_REJECT: Final[str] = "reject"
#: ``drop_newest`` 是"拒收新条目"的别名：保留这个拼写是因为配置文件/文档里
#: 用它会更好理解，但行为与 ``reject`` 完全一致（调用方收到 ``OutboxFull``）。
OVERFLOW_DROP_NEWEST: Final[str] = "drop_newest"
OVERFLOW_POLICIES: Final[frozenset[str]] = frozenset(
    {OVERFLOW_DROP_OLDEST, OVERFLOW_REJECT, OVERFLOW_DROP_NEWEST}
)


class OutboxFull(RuntimeError):
    """溢出策略为 reject 且队列已满；调用方必须显式处理（记日志/上报）。"""


@dataclass(frozen=True, slots=True)
class OutboxLimits:
    """上限与溢出策略。全部来自配置，不允许散落硬编码。"""

    max_items: int = 2048
    max_bytes: int = 32 * 1024 * 1024
    overflow: str = OVERFLOW_DROP_OLDEST

    def __post_init__(self) -> None:
        if self.max_items <= 0:
            raise ValueError(f"max_items 必须为正，收到 {self.max_items}")
        if self.max_bytes <= 0:
            raise ValueError(f"max_bytes 必须为正，收到 {self.max_bytes}")
        if self.overflow not in OVERFLOW_POLICIES:
            raise ValueError(f"未知溢出策略 {self.overflow!r}，可选 {sorted(OVERFLOW_POLICIES)}")


@dataclass(slots=True)
class OutboxEntry:
    """outbox 中的一条待确认上行。"""

    seq: int
    kind: str
    msg_id: str
    ts_ms: int
    body: dict[str, Any] = field(default_factory=dict)
    #: 帧上传单元的 JPEG 字节；信封条目为 None。
    payload: bytes | None = None
    #: 帧元数据（服务端用于落库）；信封条目忽略。
    session_id: int = 0
    width: int = 0
    height: int = 0
    score: int = 0
    sent_at_ms: int = 0

    @property
    def is_frame(self) -> bool:
        return self.kind == FRAME_KIND

    @property
    def size_bytes(self) -> int:
        if self.payload is not None:
            return len(self.payload)
        return len(json.dumps(self.body, ensure_ascii=False, separators=(",", ":")))

    def to_envelope(self) -> Envelope:
        """还原成信封对象（仅信封条目可调用）。"""

        if self.is_frame:
            raise ValueError("帧条目没有信封形态")
        return Envelope(id=self.msg_id, type=self.kind, ts_ms=self.ts_ms, body=dict(self.body))


class OutboxStore(Protocol):
    """持久化后端的最小接口（只有 sqlite 实现；单测可注入假实现）。"""

    def load(self) -> list[OutboxEntry]: ...

    def insert(self, entry: OutboxEntry) -> None: ...

    def delete(self, msg_id: str) -> None: ...

    def delete_many(self, msg_ids: Iterable[str]) -> None: ...

    def clear(self) -> None: ...

    def get_meta(self, key: str) -> str: ...

    def set_meta(self, key: str, value: str) -> None: ...

    def close(self) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_entries (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,
    msg_id     TEXT    NOT NULL UNIQUE,
    ts_ms      INTEGER NOT NULL,
    body       TEXT    NOT NULL,
    payload    BLOB,
    session_id INTEGER NOT NULL DEFAULT 0,
    width      INTEGER NOT NULL DEFAULT 0,
    height     INTEGER NOT NULL DEFAULT 0,
    score      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outbox_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SqliteOutboxStore:
    """sqlite 持久化：kill -9 后从磁盘回放未确认上行。

    设计取舍：
    - **每次变更一个事务**（WAL + ``synchronous=FULL``）：进程被杀不会丢已提交
      的条目；帧载荷直接存 BLOB，随 outbox 上限一起受限。
    - **损坏可恢复**：打开时 ``quick_check`` 失败就把文件移到
      ``<name>.corrupt-<时间戳>`` 后重建，并记 error（不静默丢数据）。
    - **sent 标记不落盘**：重启后所有条目都按未发送处理，重投由服务端 id 去重
      兜底；这样崩溃点无论在哪都不需要额外恢复逻辑。
    - 写失败向上抛（由 :class:`Outbox` 统一降级并记 error），本类不吞异常。
    """

    def __init__(self, path: str | Path, *, logger: logging.Logger | None = None) -> None:
        self._path = Path(path)
        self._log = logger or log
        self._lock = threading.RLock()
        self._conn = self._open_with_recovery()

    @property
    def path(self) -> Path:
        return self._path

    # -- 打开 / 恢复 -----------------------------------------------------
    def _open_with_recovery(self) -> sqlite3.Connection:
        try:
            return self._open()
        except (sqlite3.DatabaseError, OSError) as exc:
            quarantined = self._quarantine()
            self._log.error(
                "outbox 数据库不可用（%s），已将 %s 隔离为 %s 并重建（未确认上行会丢失）",
                exc,
                self._path,
                quarantined,
            )
            return self._open()

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self._path), timeout=5.0, isolation_level=None, check_same_thread=False
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA)
            check = conn.execute("PRAGMA quick_check").fetchone()
            if check is None or str(check[0]) != "ok":
                raise sqlite3.DatabaseError(f"quick_check 未通过: {check[0] if check else '无结果'}")
        except (sqlite3.DatabaseError, OSError):
            conn.close()
            raise
        return conn

    def _quarantine(self) -> Path:
        stamp = int(_now_ms())
        target = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        try:
            self._path.replace(target)
        except OSError as exc:
            self._log.error("隔离损坏的 outbox 数据库失败（%s），尝试删除重建", exc)
            try:
                self._path.unlink(missing_ok=True)
            except OSError as unlink_exc:
                raise OSError(f"无法恢复 outbox 数据库 {self._path}: {unlink_exc}") from unlink_exc
        for suffix in ("-wal", "-shm"):
            sidecar = self._path.with_name(self._path.name + suffix)
            try:
                sidecar.unlink(missing_ok=True)
            except OSError as exc:  # noqa: BLE001 - 附属文件清理失败不应阻止重建
                self._log.warning("清理 outbox 附属文件 %s 失败: %s", sidecar, exc)
        return target

    # -- 读写 ------------------------------------------------------------
    def load(self) -> list[OutboxEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, msg_id, ts_ms, body, payload, session_id, width, height, score "
                "FROM outbox_entries ORDER BY seq ASC"
            ).fetchall()
            entries: list[OutboxEntry] = []
            for row in rows:
                try:
                    body = json.loads(row["body"])
                except ValueError as exc:
                    self._log.error(
                        "outbox 行 body 损坏，删除 id=%s: %s", row["msg_id"], exc
                    )
                    self._conn.execute("DELETE FROM outbox_entries WHERE msg_id = ?", (row["msg_id"],))
                    continue
                entries.append(
                    OutboxEntry(
                        seq=int(row["seq"]),
                        kind=str(row["kind"]),
                        msg_id=str(row["msg_id"]),
                        ts_ms=int(row["ts_ms"]),
                        body=dict(body) if isinstance(body, Mapping) else {},
                        payload=bytes(row["payload"]) if row["payload"] is not None else None,
                        session_id=int(row["session_id"] or 0),
                        width=int(row["width"] or 0),
                        height=int(row["height"] or 0),
                        score=int(row["score"] or 0),
                    )
                )
            return entries

    def insert(self, entry: OutboxEntry) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO outbox_entries "
                "(seq, kind, msg_id, ts_ms, body, payload, session_id, width, height, score) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.seq,
                    entry.kind,
                    entry.msg_id,
                    entry.ts_ms,
                    json.dumps(entry.body, ensure_ascii=False, separators=(",", ":")),
                    entry.payload,
                    entry.session_id,
                    entry.width,
                    entry.height,
                    entry.score,
                ),
            )

    def delete(self, msg_id: str) -> None:
        if not msg_id:
            return
        with self._lock:
            self._conn.execute("DELETE FROM outbox_entries WHERE msg_id = ?", (msg_id,))

    def delete_many(self, msg_ids: Iterable[str]) -> None:
        ids = tuple(str(item) for item in msg_ids if item)
        if not ids:
            return
        with self._lock:
            self._conn.executemany("DELETE FROM outbox_entries WHERE msg_id = ?", ((item,) for item in ids))

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM outbox_entries")

    def get_meta(self, key: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM outbox_meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row is not None else ""

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO outbox_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None


def _now_ms() -> int:
    # 只为损坏文件名的时间戳服务；outbox 的业务时间仍全部由调用方注入。
    import time

    return int(time.time() * 1000)


class Outbox:
    """有界、至少一次的上行队列。"""

    def __init__(
        self,
        limits: OutboxLimits | None = None,
        *,
        logger: logging.Logger | None = None,
        store: OutboxStore | None = None,
    ) -> None:
        self._limits = limits or OutboxLimits()
        self._log = logger or log
        self._entries: list[OutboxEntry] = []
        self._seq = 0
        self._bytes = 0
        self._last_ack_id = ""
        self._last_downlink_id = ""
        self._acked = 0
        self._dropped = 0
        self._last_drop_reason = ""
        self._overflow_events = 0
        self._store = store
        self._store_errors = 0
        if store is not None:
            self._load_from_store(store)

    def _load_from_store(self, store: OutboxStore) -> None:
        """从磁盘回放未确认条目与两个游标（损坏时降级为内存并记 error）。"""

        try:
            loaded = store.load()
            self._last_ack_id = store.get_meta("last_ack_id")
            self._last_downlink_id = store.get_meta("last_downlink_id")
        except Exception as exc:  # noqa: BLE001 - 持久化不可用时仍要能上线，但必须可观测
            self._store_errors += 1
            self._log.error(
                "读取持久化 outbox 失败，降级为内存队列（未确认上行可能重复/丢失）: %s", exc
            )
            return
        self._entries = list(loaded)
        self._bytes = sum(entry.size_bytes for entry in self._entries)
        self._seq = max((entry.seq for entry in self._entries), default=0)
        if self._entries:
            self._log.info(
                "从持久化 outbox 恢复 %s 条未确认上行（frames=%s, bytes=%s）",
                len(self._entries),
                sum(1 for entry in self._entries if entry.is_frame),
                self._bytes,
            )

    def _store_call(self, op: str, func: Any, *args: Any) -> None:
        """持久化调用统一入口：失败降级为内存态，但错误必须进日志（C6）。"""

        if self._store is None:
            return
        try:
            func(*args)
        except Exception as exc:  # noqa: BLE001 - 磁盘满/被占用不能打断链路，必须可观测
            self._store_errors += 1
            if self._store_errors == 1 or self._store_errors % 100 == 0:
                self._log.error(
                    "outbox 持久化失败 op=%s（累计 %s 次；本次运行降级为内存队列，"
                    "进程重启会丢未确认项）: %s",
                    op,
                    self._store_errors,
                    exc,
                )

    # -- 只读视图 --------------------------------------------------------
    @property
    def limits(self) -> OutboxLimits:
        return self._limits

    @property
    def last_ack_id(self) -> str:
        """服务端最后确认过的上行 id；用于续传（规格 §5.3）。"""

        return self._last_ack_id

    @property
    def last_downlink_id(self) -> str:
        """已处理的最后一条下行 id（请求 ``last_ack_id`` 的取值，规格 §5.6）。"""

        return self._last_downlink_id

    def note_downlink(self, msg_id: str) -> None:
        """记录已处理的下行信封 id，并落盘（重启后不会让服务端重发旧下行）。"""

        if not msg_id or msg_id == self._last_downlink_id:
            return
        self._last_downlink_id = msg_id
        self._store_call(
            "meta:last_downlink_id", lambda: self._store.set_meta("last_downlink_id", msg_id)
        )

    @property
    def pending_count(self) -> int:
        return len(self._entries)

    @property
    def pending_bytes(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, msg_id: object) -> bool:
        return isinstance(msg_id, str) and any(entry.msg_id == msg_id for entry in self._entries)

    def has(self, msg_id: str) -> bool:
        return msg_id in self

    def entries(self) -> tuple[OutboxEntry, ...]:
        return tuple(self._entries)

    def stats(self) -> dict[str, Any]:
        frames = sum(1 for entry in self._entries if entry.is_frame)
        return {
            "pending": len(self._entries),
            "frames": frames,
            "envelopes": len(self._entries) - frames,
            "bytes": self._bytes,
            "max_items": self._limits.max_items,
            "max_bytes": self._limits.max_bytes,
            "overflow": self._limits.overflow,
            "dropped": self._dropped,
            "acked": self._acked,
            "overflow_events": self._overflow_events,
            "last_ack_id": self._last_ack_id,
            "last_downlink_id": self._last_downlink_id,
            "last_drop_reason": self._last_drop_reason,
            "persistent": self._store is not None,
            "store_errors": self._store_errors,
        }

    # -- 入队 ------------------------------------------------------------
    def append_envelope(
        self,
        kind: str,
        body: Mapping[str, Any] | None = None,
        *,
        ts_ms: int,
        msg_id: str | None = None,
    ) -> Envelope:
        """入队一个上行信封并返回它；同 ``msg_id`` 已存在时复用（幂等）。"""

        envelope = new_envelope(kind, body, ts_ms=ts_ms, msg_id=msg_id)
        existing = self._find(envelope.id)
        if existing is not None:
            return existing.to_envelope()
        entry = OutboxEntry(seq=self._next_seq(), kind=envelope.type, msg_id=envelope.id, ts_ms=ts_ms, body=dict(envelope.body))
        self._insert(entry)
        self._store_call("insert", lambda: self._store.insert(entry))
        return envelope

    def append_frame(
        self,
        payload: bytes,
        *,
        ts_ms: int,
        session_id: int = 0,
        kind: str = "screen",
        frame_id: str | None = None,
        width: int = 0,
        height: int = 0,
        score: int = 0,
    ) -> str:
        """入队一个帧上传单元并返回 ``X-Frame-Id``；同 id 已存在时复用。"""

        if not isinstance(payload, (bytes, bytearray)) or not payload:
            raise ValueError("帧载荷必须是非空 bytes")
        if not isinstance(ts_ms, int) or isinstance(ts_ms, bool) or ts_ms < 0:
            raise ValueError(f"帧 ts_ms 必须是非负整数，收到 {ts_ms!r}")
        identifier = frame_id or new_id()
        existing = self._find(identifier)
        if existing is not None:
            return existing.msg_id
        entry = OutboxEntry(
            seq=self._next_seq(),
            kind=FRAME_KIND,
            msg_id=identifier,
            ts_ms=ts_ms,
            body={"kind": kind, "session_id": session_id},
            payload=bytes(payload),
            session_id=session_id,
            width=width,
            height=height,
            score=score,
        )
        self._insert(entry)
        self._store_call("insert", lambda: self._store.insert(entry))
        return identifier

    def _insert(self, entry: OutboxEntry) -> None:
        size = entry.size_bytes
        # 先腾出空间再插入：drop_oldest 会从最老的条目开始逐条淘汰；队列已空仍
        # 装不下（单条超过 max_bytes）时不能死循环，直接按"拒收"处理。
        while self._over_capacity(adding=size):
            if self._limits.overflow == OVERFLOW_DROP_OLDEST and self._entries:
                self._evict_oldest(reason="overflow_drop_oldest")
                continue
            self._overflow_events += 1
            self._dropped += 1
            self._last_drop_reason = f"overflow_reject:{entry.kind}"
            self._log.warning(
                "outbox 已满（pending=%s bytes=%s 新条目 %s 字节，max_items=%s max_bytes=%s），"
                "按策略 %s 拒收 %s id=%s",
                len(self._entries),
                self._bytes,
                size,
                self._limits.max_items,
                self._limits.max_bytes,
                self._limits.overflow,
                entry.kind,
                entry.msg_id,
            )
            raise OutboxFull(
                f"outbox 已满（max_items={self._limits.max_items}, max_bytes={self._limits.max_bytes}）"
            )
        self._entries.append(entry)
        self._bytes += size

    def _over_capacity(self, *, adding: int) -> bool:
        return (
            len(self._entries) + 1 > self._limits.max_items
            or self._bytes + adding > self._limits.max_bytes
        )

    def _evict_oldest(self, *, reason: str) -> None:
        if not self._entries:
            return
        victim = self._entries.pop(0)
        self._bytes -= victim.size_bytes
        self._dropped += 1
        self._last_drop_reason = f"{reason}:{victim.kind}"
        self._store_call("evict", lambda: self._store.delete(victim.msg_id))
        if self._last_ack_id == victim.msg_id:
            # 被淘汰的恰是续传锚点：清空它，让服务端按 id 去重兜底，而不是继续
            # 声称"已确认到 X"却再也拿不出 X 之后的消息。
            self._last_ack_id = ""
        self._log.warning(
            "outbox 溢出，丢弃最老条目 kind=%s id=%s bytes=%s（累计丢弃 %s，策略 %s）",
            victim.kind,
            victim.msg_id,
            victim.size_bytes,
            self._dropped,
            self._limits.overflow,
        )

    # -- 出队 / 确认 -----------------------------------------------------
    def pending(self, *, limit: int | None = None, frames: bool | None = None) -> list[OutboxEntry]:
        """按入队顺序返回未确认条目；``frames`` 过滤帧/信封。"""

        result: list[OutboxEntry] = []
        for entry in self._entries:
            if frames is not None and entry.is_frame != frames:
                continue
            result.append(entry)
            if limit is not None and len(result) >= limit:
                break
        return result

    def unsent_envelopes(self, *, limit: int | None = None) -> list[OutboxEntry]:
        """尚未写进当前载体的信封（``sent_at_ms == 0``）。"""

        result: list[OutboxEntry] = []
        for entry in self._entries:
            if entry.is_frame or entry.sent_at_ms:
                continue
            result.append(entry)
            if limit is not None and len(result) >= limit:
                break
        return result

    def next_frame(self) -> OutboxEntry | None:
        """最早一个待上传帧；上传成功前会一直返回同一条（至少一次）。"""

        for entry in self._entries:
            if entry.is_frame:
                return entry
        return None

    def mark_sent(self, msg_ids: Iterable[str], *, now_ms: int) -> int:
        """标记信封已写进当前载体（WS 写成功/HTTP 批次成功）；不删除。"""

        wanted = set(msg_ids)
        marked = 0
        for entry in self._entries:
            if entry.msg_id in wanted and not entry.is_frame and not entry.sent_at_ms:
                entry.sent_at_ms = int(now_ms)
                marked += 1
        return marked

    def ack(self, msg_id: str) -> bool:
        """确认单条（服务端 200 / WS 写成功）；返回是否真的删掉了一条。"""

        if not msg_id:
            return False
        for index, entry in enumerate(self._entries):
            if entry.msg_id != msg_id:
                continue
            self._remove_at(index)
            self._last_ack_id = msg_id
            self._acked += 1
            self._store_call("ack", lambda: self._store.delete(msg_id))
            self._store_call("meta:last_ack_id", lambda: self._store.set_meta("last_ack_id", msg_id))
            return True
        # 已经确认过或从未入队：仍记录锚点，避免续传点倒退。
        self._last_ack_id = msg_id
        self._store_call("meta:last_ack_id", lambda: self._store.set_meta("last_ack_id", msg_id))
        return False

    def ack_through(self, msg_id: str) -> int:
        """确认到 ``msg_id`` 为止（含）的所有条目；返回删除条数。

        服务端返回的 ``ack_id`` 可能指向本进程更早的一条（HTTP 批次丢响应后
        客户端重发，服务端按幂等直接确认老 id）。此时只记录锚点，不误删更老的
        未确认条目——那些条目若仍在队列里，序号都 ≤ 该 id，会被一并清掉；
        若已不在队列，删 0 条也是正确结果。
        """

        if not msg_id:
            return 0
        target_index: int | None = None
        for index, entry in enumerate(self._entries):
            if entry.msg_id == msg_id:
                target_index = index
                break
        if target_index is None:
            self._last_ack_id = msg_id
            self._store_call("meta:last_ack_id", lambda: self._store.set_meta("last_ack_id", msg_id))
            return 0
        removed = target_index + 1
        removed_ids = [self._entries[index].msg_id for index in range(removed)]
        for _ in range(removed):
            self._remove_at(0)
        self._acked += removed
        self._last_ack_id = msg_id
        self._store_call("ack_many", lambda: self._store.delete_many(removed_ids))
        self._store_call("meta:last_ack_id", lambda: self._store.set_meta("last_ack_id", msg_id))
        return removed

    def frame_done(self, frame_id: str) -> bool:
        """帧上传成功（HTTP 200）；返回是否删掉了一条。"""

        for index, entry in enumerate(self._entries):
            if entry.is_frame and entry.msg_id == frame_id:
                self._remove_at(index)
                self._acked += 1
                self._store_call("frame_done", lambda: self._store.delete(frame_id))
                return True
        return False

    def note_ack(self, msg_id: str) -> None:
        """只记录服务端确认锚点，不动队列（WS 未逐条确认时用）。"""

        if msg_id:
            self._last_ack_id = msg_id
            self._store_call("meta:last_ack_id", lambda: self._store.set_meta("last_ack_id", msg_id))

    def discard(self, msg_id: str, *, reason: str) -> bool:
        """放弃一条**可重试也无意义**的条目（例如 422 校验失败），必须记日志。"""

        for index, entry in enumerate(self._entries):
            if entry.msg_id != msg_id:
                continue
            removed = self._entries.pop(index)
            self._bytes -= removed.size_bytes
            self._dropped += 1
            self._last_drop_reason = f"discard:{reason}"
            self._store_call("discard", lambda: self._store.delete(removed.msg_id))
            self._log.error(
                "outbox 丢弃不可重试条目 kind=%s id=%s 原因=%s",
                removed.kind,
                removed.msg_id,
                reason,
            )
            return True
        self._log.warning("outbox 未找到要丢弃的条目 id=%s 原因=%s", msg_id, reason)
        return False

    def replay(self) -> list[OutboxEntry]:
        """断链后调用：把全部信封条目重新标为待发并返回，帧不受影响。

        载体切换/重连时不能重放"服务端已确认"的消息，因此重放边界是
        ``last_ack_id`` 之后的条目；本方法只清 ``sent_at_ms``，是否发送由链路
        按未确认集合决定。
        """

        replayed: list[OutboxEntry] = []
        for entry in self._entries:
            if entry.is_frame:
                continue
            entry.sent_at_ms = 0
            replayed.append(entry)
        return replayed

    def clear(self) -> int:
        """清空队列（仅用于关闭/测试）；返回丢弃条数。"""

        count = len(self._entries)
        if count:
            self._log.warning("outbox 清空：丢弃 %s 条未确认上行", count)
        self._entries.clear()
        self._bytes = 0
        self._seq = 0
        self._store_call("clear", lambda: self._store.clear())
        return count

    def close(self) -> None:
        """关闭持久化后端（幂等）；关闭后对象不应再被使用。"""

        self._store_call("close", lambda: self._store.close())
        self._store = None

    # -- 内部 ------------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _find(self, msg_id: str) -> OutboxEntry | None:
        for entry in self._entries:
            if entry.msg_id == msg_id:
                return entry
        return None

    def _remove_at(self, index: int) -> None:
        removed = self._entries.pop(index)
        self._bytes -= removed.size_bytes


__all__ = [
    "FRAME_KIND",
    "OVERFLOW_DROP_NEWEST",
    "OVERFLOW_DROP_OLDEST",
    "OVERFLOW_POLICIES",
    "OVERFLOW_REJECT",
    "Outbox",
    "OutboxEntry",
    "OutboxFull",
    "OutboxLimits",
    "OutboxStore",
    "SqliteOutboxStore",
]