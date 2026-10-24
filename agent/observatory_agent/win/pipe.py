"""服务 ↔ worker 的单一信封管道。

为什么旧实现错（A17）
--------------------

* 旧 ``pipe_server.py:97-109`` 用 ``CreateNamedPipe(..., lpSecurityAttributes=None)``，
  管道的 DACL 完全取决于 SYSTEM 的默认 DACL；旧代码也**从不校验连上来的客户端是
  谁**。于是本机任意能打开该管道的进程都能抢先冒充 worker，向服务发送
  ``hello/frame/ctrl/result``，或让真正的 worker 撞 ``ERROR_PIPE_BUSY``。
* 旧 ``pipe_server.py`` 自己实现了一份 ``_available``/``_read_bytes`` 读循环，而
  ``pipe_proto.read_exact`` 是死代码；且读循环与 pywin32 强耦合，无法在 Linux CI 单测。

0.3.0 的做法：

1. **ACL 收紧**（:func:`pipe_security_attributes`）：只给 SYSTEM、Administrators 和
   该会话登录用户放权；``FILE_FLAG_FIRST_PIPE_INSTANCE`` 防止别的进程抢先创建同名管道。
2. **客户端身份校验**（:func:`query_client_identity` + 纯函数 :func:`identity_trusted`）：
   ``GetNamedPipeClientProcessId`` → 会话 ID + 进程令牌用户 SID（+ 可选镜像路径），
   不匹配立即断开并记录日志。
3. **单一 Envelope 协议**：线格式与 ``core/envelope.py`` 完全一致
   （``uint32 BE 头长 | UTF-8 JSON 头 | payload``，见 :func:`core.envelope.encode_binary`），
   本模块不再复制一份编解码/常量。控制消息的头是 v3 信封（``Envelope.to_dict()``），
   采集/控制画面帧的头是 ``{"t": "frame"|"ctrl", ...}`` 的传输头（规格 §5.1 的二进制帧）。
4. **可单测的读循环**：:func:`read_packet` 只依赖注入的 ``peek/read/sleep/stop``，
   在 Linux CI 上也能跑完整的粘包/半包测试。

0.3.1（F30）的修正：

1. **镜像路径查询**：旧实现用 ``GetModuleFileNameEx`` 查客户端镜像路径，该 API 需要
   ``PROCESS_QUERY_INFORMATION``，对 ``PROCESS_QUERY_LIMITED_INFORMATION`` 句柄大概率
   返回拒绝且异常被静默吞成空串 → service 模式下路径比对恒 False，所有 worker 被拒连。
   改用 LIMITED 权限位可用的 ``QueryFullProcessImageNameW``（pywin32 优先、ctypes 兜底），
   取不到路径时显式记日志并 fail-closed（:func:`identity_trusted` 返回 False）。
2. **路径比对**：expected 与客户端镜像路径都 ``resolve()`` 后再比对。
3. **句柄生命周期**：``_run`` 的 stop 检查移入 ``try``（否则 break 跳过 finally 泄漏
   FIRST_PIPE_INSTANCE 实例）；关闭句柄与 ``send`` 的写入共用 ``_write_lock``。
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import PIPE_PREFIX
from ..core.envelope import (
    HEADER_STRUCT,
    MAX_HEADER_BYTES,
    MAX_PAYLOAD_BYTES,
    Envelope,
    EnvelopeError,
    decode_binary,
    encode_binary,
)
from . import WindowsOnlyOperation

log = logging.getLogger(__name__)

#: 管道缓冲：1MiB；轮询间隔 20ms（只有确认有数据才 ReadFile）。
BUFFER_SIZE = 1024 * 1024
POLL_INTERVAL_S = 0.02
CONNECT_TIMEOUT_S = 90.0
CLIENT_RETRY_S = 0.5

#: ``FILE_FLAG_FIRST_PIPE_INSTANCE``：若已有同名管道实例则创建失败，
#: 防止低权限进程先创建同名管道并放宽 ACL。
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000

SID_LOCAL_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"


class PipeProtocolError(ValueError):
    """帧格式非法/流被破坏。"""


class PipeUnavailable(RuntimeError):
    """管道不存在、已断开或无法创建。"""


class PipeSecurityError(PermissionError):
    """客户端身份未通过校验（疑似冒充）。"""


# ------------------------------------------------------------------ framing --
def encode_message(message: Envelope | Mapping[str, Any], payload: bytes = b"") -> bytes:
    """把信封/传输头与载荷封成一个管道帧（编解码复用 ``core.envelope``）。"""

    header = message.to_dict() if isinstance(message, Envelope) else dict(message)
    try:
        return encode_binary(header, payload)
    except EnvelopeError as exc:
        raise PipeProtocolError(f"无法编码管道帧: {exc}") from exc


def decode_message(blob: bytes) -> tuple[dict[str, Any], bytes]:
    """解析一个完整管道帧；格式错误抛 :class:`PipeProtocolError`。"""

    try:
        return decode_binary(blob)
    except EnvelopeError as exc:
        raise PipeProtocolError(f"无法解码管道帧: {exc}") from exc


def read_packet(
    *,
    peek: Callable[[], int | None],
    read: Callable[[int], bytes | None],
    sleep: Callable[[float], None],
    stop: Callable[[], bool],
    poll_s: float = POLL_INTERVAL_S,
) -> tuple[dict[str, Any], bytes] | None:
    """从字节流读一个管道帧，返回 ``(header, payload)``。

    ``header`` 与 :func:`core.envelope.decode_binary` 返回的 JSON 头同构：控制消息是 v3
    信封 dict，画面帧是 ``{"t": "frame"|"ctrl", ...}`` 传输头。``peek()`` 返回可读字节数，
    ``None`` 表示对端关闭；``read(n)`` 读至多 n 字节，``None`` 表示读失败。
    返回 ``None`` 表示流正常结束或收到 stop。所有阻塞都通过注入的 ``sleep`` 完成。
    """

    head = _read_exact(HEADER_STRUCT.size, peek=peek, read=read, sleep=sleep, stop=stop, poll_s=poll_s)
    if head is None:
        return None
    (header_len,) = HEADER_STRUCT.unpack(head)
    if header_len <= 0 or header_len > MAX_HEADER_BYTES:
        raise PipeProtocolError(f"非法 header 长度: {header_len}")
    header_json = _read_exact(header_len, peek=peek, read=read, sleep=sleep, stop=stop, poll_s=poll_s)
    if header_json is None:
        return None
    header = _parse_header(header_json)
    payload_len = int(header.get("len") or 0)
    if payload_len < 0 or payload_len > MAX_PAYLOAD_BYTES:
        raise PipeProtocolError(f"非法 payload 长度: {payload_len}")
    payload = b""
    if payload_len:
        raw = _read_exact(payload_len, peek=peek, read=read, sleep=sleep, stop=stop, poll_s=poll_s)
        if raw is None:
            return None
        payload = raw
    return header, payload


def _parse_header(raw: bytes) -> dict[str, Any]:
    import json

    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PipeProtocolError(f"管道帧头不是合法 JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise PipeProtocolError("管道帧头必须是 JSON object")
    return header


def _read_exact(
    size: int,
    *,
    peek: Callable[[], int | None],
    read: Callable[[int], bytes | None],
    sleep: Callable[[float], None],
    stop: Callable[[], bool],
    poll_s: float,
) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        if stop():
            return None
        available = peek()
        if available is None:
            return None
        if available <= 0:
            sleep(poll_s)
            continue
        chunk = read(min(remaining, available))
        if not chunk:
            sleep(poll_s)
            continue
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# ------------------------------------------------------------------ identity --
@dataclass(frozen=True)
class ClientIdentity:
    pid: int
    session_id: int
    user_sid: str
    image_path: str = ""


def identity_trusted(
    identity: ClientIdentity,
    *,
    expected_session_id: int,
    expected_user_sid: str,
    expected_image_path: str | Path | None = None,
) -> bool:
    """纯函数：客户端是否就是本会话应有的 worker。

    0.3.1（F30）：``expected_image_path`` 启用时客户端镜像路径**必须**取到且一致——
    取不到（空串）直接判 False（fail-closed），否则冒充者只要让路径查询失败即可绕过
    比对。两侧路径都 ``resolve()`` 后再比对（``Path.resolve`` 对不存在的文件也安全），
    避免相对/大小写形式差异导致误拒。
    """

    if identity.session_id != expected_session_id:
        return False
    if identity.user_sid.upper() != expected_user_sid.upper():
        return False
    if expected_image_path is not None:
        if not identity.image_path:
            # F30 fail-closed：服务端取不到客户端镜像路径时不能放行。
            return False
        expected = str(Path(expected_image_path).resolve()).lower()
        actual = str(Path(identity.image_path).resolve()).lower()
        if actual != expected:
            return False
    return True


def _require_windows(operation: str) -> None:
    if sys.platform != "win32":
        raise WindowsOnlyOperation(f"{operation} 只能在 Windows 上执行")


def _query_image_path(process: Any, win32process: Any) -> str:
    """读取进程镜像路径；失败抛异常，由调用方决定 fail-closed（F30）。

    0.3.1 前用 ``GetModuleFileNameEx``：它需要 ``PROCESS_QUERY_INFORMATION``，对只有
    ``PROCESS_QUERY_LIMITED_INFORMATION`` 的句柄大概率返回拒绝，且异常被静默吞掉，
    等于镜像路径校验在 service 模式下恒为空 → 全部 worker 被拒连。
    ``QueryFullProcessImageNameW`` 正是 LIMITED 权限位可用的 API；当前部署的 pywin32
    构建未导出它，故 pywin32 优先、ctypes 兜底。
    0.3.1 真机验证项：需在真机确认 LIMITED 句柄上 ``QueryFullProcessImageNameW`` 稳定
    成功、且返回常规盘符路径（而非 ``\\Device\\...`` 设备路径）。
    """

    getter = getattr(win32process, "QueryFullProcessImageName", None)
    if getter is not None:
        return str(getter(process, 0))
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    size = wintypes.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(wintypes.HANDLE(int(process)), 0, buffer, ctypes.byref(size)):
        raise OSError(f"QueryFullProcessImageNameW failed (winerror={ctypes.get_last_error()})")
    return buffer.value


def query_client_identity(handle: Any) -> ClientIdentity:
    """读取命名管道对端进程的身份（PID / 会话 / 用户 SID / 镜像路径）。"""

    _require_windows("校验管道客户端身份")
    try:
        import win32api
        import win32con
        import win32pipe
        import win32process
        import win32security
        import win32ts
    except Exception as exc:  # noqa: BLE001 - pywin32 是部署依赖
        raise PipeUnavailable(f"pywin32 不可用: {exc}") from exc

    try:
        pid = int(win32pipe.GetNamedPipeClientProcessId(handle))
        session_id = int(win32ts.ProcessIdToSessionId(pid))
    except Exception as exc:  # noqa: BLE001 - 管道已断开/API 不可用
        raise PipeUnavailable(f"读取客户端 PID/会话失败: {exc}") from exc
    if pid <= 0:
        raise PipeSecurityError("客户端 PID 无效")

    process = None
    token = None
    try:
        try:
            process = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except Exception as exc:  # noqa: BLE001 - 进程可能在 OpenProcess 前退出
            raise PipeUnavailable(f"打开客户端进程失败: {exc}") from exc
        try:
            token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
        except Exception as exc:  # noqa: BLE001
            raise PipeSecurityError(f"读取客户端令牌失败: {exc}") from exc
        try:
            info = win32security.GetTokenInformation(token, win32security.TokenUser)
            sid = info[0] if isinstance(info, (tuple, list)) else info
            user_sid = str(win32security.ConvertSidToStringSid(sid))
        except Exception as exc:  # noqa: BLE001
            raise PipeSecurityError(f"读取客户端 SID 失败: {exc}") from exc
        try:
            image_path = _query_image_path(process, win32process)
        except Exception as exc:  # noqa: BLE001 - 任何失败都按“取不到路径”处理（fail-closed）
            # 0.3.1（F30）：不再 suppress 成空串。``expected_image_path`` 启用时空串会让
            # :func:`identity_trusted` 返回 False（fail-closed）；这条 warning 明确区分
            # “取路径失败”与 _run 拒连日志里的“路径不匹配”。
            log.warning(
                "read client image path failed pid=%s: %s (image check will fail closed)",
                pid,
                exc,
            )
            image_path = ""
        return ClientIdentity(pid=pid, session_id=session_id, user_sid=user_sid, image_path=image_path)
    finally:
        if token is not None:
            with contextlib.suppress(Exception):  # 关闭令牌失败不影响判定结果
                win32api.CloseHandle(token)
        if process is not None:
            with contextlib.suppress(Exception):  # 关闭进程句柄失败同样无害
                win32api.CloseHandle(process)


def pipe_security_attributes(*, allowed_user_sid: str) -> Any:
    """构造只允许 SYSTEM、Administrators、``allowed_user_sid`` 的 SECURITY_ATTRIBUTES。"""

    _require_windows("创建管道安全描述符")
    try:
        import ntsecuritycon
        import win32security
    except Exception as exc:  # noqa: BLE001
        raise PipeUnavailable(f"pywin32 不可用: {exc}") from exc
    try:
        system = win32security.ConvertStringSidToSid(SID_LOCAL_SYSTEM)
        administrators = win32security.ConvertStringSidToSid(SID_ADMINISTRATORS)
        user = win32security.ConvertStringSidToSid(allowed_user_sid)
    except Exception as exc:  # noqa: BLE001 - 非法 SID 字符串说明上层传错
        raise PipeSecurityError(f"非法的 SID: {exc}") from exc

    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, system)
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, administrators)
    user_access = (
        ntsecuritycon.FILE_GENERIC_READ
        | ntsecuritycon.FILE_GENERIC_WRITE
        | ntsecuritycon.SYNCHRONIZE
    )
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, user_access, user)

    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


# ------------------------------------------------------------------- server --
def pipe_name(session_id: int) -> str:
    return f"{PIPE_PREFIX}-{session_id}"


class PipeServer:
    """服务侧：每个会话一个实例，一次只接受一个已校验的 worker。"""

    def __init__(
        self,
        session_id: int,
        *,
        allowed_user_sid: str,
        on_message: Callable[[dict[str, Any], bytes], None],
        expected_image_path: str | Path | None = None,
        expected_session_id: int | None = None,
        security_attributes: Any | None = None,
        poll_s: float = POLL_INTERVAL_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session_id = session_id
        self.allowed_user_sid = allowed_user_sid
        self.on_message = on_message
        self.expected_image_path = expected_image_path
        #: 生产环境等于 ``session_id``；只有测试需要"管道名 ≠ 期望会话"时才单独指定。
        self._expected_session_id = int(session_id if expected_session_id is None else expected_session_id)
        #: 仅 ``tools/win_smoke.py`` 或特殊受限环境使用；``None`` 时必须走
        #: :func:`pipe_security_attributes` 的受限 DACL（A17），生产代码不得传 Everyone。
        self._security_attributes = security_attributes
        self._poll_s = poll_s
        self._sleep = sleep
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._handle: Any = None
        self._thread: threading.Thread | None = None
        self.connected = threading.Event()
        self.last_client: ClientIdentity | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name=f"pipe-server-{self.session_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Windows 上 CloseHandle 对"另一线程正阻塞在 ConnectNamedPipe"的同步管道句柄不可靠
        # （会一直等挂起的 I/O），0.3.0 的服务停止/升级会挂死。所以正常路径**不由 stop 线程
        # 关句柄**：反复用只读哑客户端把 ConnectNamedPipe 解开，让 _run 自己走 finally 关闭，
        # 避免两个线程同时 close 造成实例泄漏。只有线程 10s 还不退时才兜底强关。
        thread = self._thread
        if thread is None:
            self.connected.clear()
            return
        deadline = time.monotonic() + 10.0
        while thread.is_alive() and time.monotonic() <= deadline:
            with contextlib.suppress(Exception):  # 实例可能还在创建/已连接（231）
                import win32file

                probe = win32file.CreateFile(
                    pipe_name(self.session_id),
                    win32file.GENERIC_READ,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
                win32file.CloseHandle(probe)
            thread.join(timeout=0.2)
        if thread.is_alive():
            log.warning("pipe server thread for session %s did not exit in 10s", self.session_id)
            # 0.3.1（F30）：兜底强关故意不取 _write_lock——线程多半阻塞在管道 I/O 上，
            # 关句柄本身会把阻塞的 WriteFile/ConnectNamedPipe 解开；取锁反而可能死锁。
            handle = self._handle
            self._handle = None
            if handle is not None:
                with contextlib.suppress(Exception):
                    import win32file

                    win32file.CloseHandle(handle)
            thread.join(timeout=2.0)
        self.connected.clear()

    # -- io ----------------------------------------------------------------
    def send(self, message: Envelope | Mapping[str, Any], payload: bytes = b"") -> None:
        """发一个信封/传输头；未连接或写失败抛 :class:`PipeUnavailable`。"""

        data = encode_message(message, payload)
        with self._write_lock:
            handle = self._handle
            if handle is None or not self.connected.is_set():
                raise PipeUnavailable(f"session {self.session_id} 的 worker 管道未连接")
            try:
                import win32file

                win32file.WriteFile(handle, data)
            except Exception as exc:  # noqa: BLE001 - 写失败意味着连接已死
                # 0.3.1（F30）：写失败立即弃用句柄；读线程 finally 关句柄时持同一把
                # _write_lock，避免句柄值被复用后写入新实例。
                self._handle = None
                self.connected.clear()
                raise PipeUnavailable(f"写管道失败: {exc}") from exc

    # -- internals ---------------------------------------------------------
    def _run(self) -> None:
        try:
            import pywintypes
            import win32file
            import win32pipe
        except Exception as exc:  # noqa: BLE001 - 缺 pywin32 时该功能整体不可用
            log.error("pywin32 unavailable, pipe server disabled: %s", exc)
            return

        while not self._stop.is_set():
            try:
                handle = self._create_instance(win32pipe)
            except PipeUnavailable as exc:
                log.error("create pipe failed session=%s: %s", self.session_id, exc)
                self._sleep(2.0)
                continue
            self._handle = handle
            try:
                # 0.3.1（F30）：stop 检查必须在 try 内。放在 try 之前 break 会跳过 finally，
                # 泄漏管道实例句柄——FILE_FLAG_FIRST_PIPE_INSTANCE 会让该会话的管道此后
                # 永久创建失败。
                if self._stop.is_set():
                    break
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except pywintypes.error as exc:
                    code = getattr(exc, "winerror", None) or (exc.args[0] if exc.args else None)
                    if code not in (0, 535):  # 535 = ERROR_PIPE_CONNECTED（客户端抢先连上）
                        log.warning("ConnectNamedPipe session=%s failed: %s", self.session_id, exc)
                        continue
                if self._stop.is_set():
                    break
                try:
                    identity = query_client_identity(handle)
                except (PipeUnavailable, PipeSecurityError) as exc:
                    log.warning("rejecting pipe client for session %s: %s", self.session_id, exc)
                    continue
                if not identity_trusted(
                    identity,
                    expected_session_id=self._expected_session_id,
                    expected_user_sid=self.allowed_user_sid,
                    expected_image_path=self.expected_image_path,
                ):
                    log.warning(
                        "rejecting pipe client for session %s: expected sid=%s session=%s, "
                        "got pid=%s sid=%s session=%s image=%s",
                        self.session_id,
                        self.allowed_user_sid,
                        self._expected_session_id,
                        identity.pid,
                        identity.user_sid,
                        identity.session_id,
                        identity.image_path,
                    )
                    continue
                self.last_client = identity
                self.connected.set()
                log.info("worker pipe connected session=%s pid=%s", self.session_id, identity.pid)
                try:
                    self._read_loop(handle, win32file)
                except PipeProtocolError as exc:
                    log.warning("pipe stream corrupted session=%s: %s", self.session_id, exc)
                finally:
                    self.connected.clear()
            finally:
                # 0.3.1（F30）：关闭句柄必须与 send 的 WriteFile 同一把 _write_lock——
                # 否则句柄值被系统复用后，迟到的写入可能打到新管道实例上。
                # 注意：DisconnectNamedPipe 在 win32pipe，不在 win32file；两者分开 suppress，
                # 否则前半句 AttributeError 会让 CloseHandle 永远不执行 → 管道实例句柄泄漏、
                # 下一次 FIRST_PIPE_INSTANCE 创建失败（win_smoke 的 pipe_identity 抓到的真实回归）。
                with self._write_lock:
                    self._handle = None
                    with contextlib.suppress(Exception):
                        import win32pipe

                        win32pipe.DisconnectNamedPipe(handle)
                    with contextlib.suppress(Exception):
                        win32file.CloseHandle(handle)

    def _create_instance(self, win32pipe: Any) -> Any:
        open_mode = win32pipe.PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE
        pipe_mode = win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT
        attributes = self._security_attributes
        if attributes is None:
            attributes = pipe_security_attributes(allowed_user_sid=self.allowed_user_sid)
        try:
            return win32pipe.CreateNamedPipe(
                pipe_name(self.session_id),
                open_mode,
                pipe_mode,
                1,
                BUFFER_SIZE,
                BUFFER_SIZE,
                0,
                attributes,
            )
        except Exception as exc:  # noqa: BLE001 - 统一翻译成 PipeUnavailable
            code = getattr(exc, "winerror", None) or (exc.args[0] if exc.args else None)
            raise PipeUnavailable(f"CreateNamedPipe failed (winerror={code}): {exc}") from exc

    def _read_loop(self, handle: Any, win32file: Any) -> None:
        def peek() -> int | None:
            try:
                _data, available, _left = peek_named_pipe(handle)
                return int(available)
            except Exception:  # noqa: BLE001 - 对端关闭/句柄失效即视为流结束
                return None

        def read(size: int) -> bytes | None:
            try:
                _err, chunk = win32file.ReadFile(handle, size)
                return chunk
            except Exception:  # noqa: BLE001 - 读失败即视为流结束
                return None

        while not self._stop.is_set():
            packet = read_packet(
                peek=peek,
                read=read,
                sleep=self._sleep,
                stop=self._stop.is_set,
                poll_s=self._poll_s,
            )
            if packet is None:
                return
            header, payload = packet
            try:
                self.on_message(header, payload)
            except Exception:  # noqa: BLE001 - 单条消息处理失败不能杀掉整个会话
                log.exception("pipe message handler failed session=%s", self.session_id)


def peek_named_pipe(handle: Any) -> tuple[Any, int, int]:
    """``PeekNamedPipe`` 的跨 pywin32 版本封装（部分构建只在一个模块里导出）。"""

    for module_name in ("win32pipe", "win32file"):
        try:
            module = __import__(module_name)
        except ImportError:
            continue
        peek = getattr(module, "PeekNamedPipe", None)
        if peek is not None:
            return peek(handle, 0)
    raise PipeUnavailable("当前 pywin32 构建未导出 PeekNamedPipe")


# ------------------------------------------------------------------- client --
class PipeClient:
    """worker 侧：连接服务管道并收发信封/画面帧。"""

    def __init__(
        self,
        session_id: int,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_s: float = POLL_INTERVAL_S,
    ) -> None:
        self.session_id = session_id
        self._sleep = sleep
        self._poll_s = poll_s
        self._handle: Any = None
        self._write_lock = threading.Lock()
        self._closed = threading.Event()

    def connect(self, *, timeout_s: float = CONNECT_TIMEOUT_S, retry_s: float = CLIENT_RETRY_S) -> None:
        """连接服务管道；超时抛 :class:`PipeUnavailable`。"""

        _require_windows("连接命名管道")
        try:
            import pywintypes
            import win32file
        except Exception as exc:  # noqa: BLE001
            raise PipeUnavailable(f"pywin32 不可用: {exc}") from exc
        deadline = time.monotonic() + max(0.0, timeout_s)
        last_error: Exception | None = None
        while time.monotonic() < deadline and not self._closed.is_set():
            try:
                handle = win32file.CreateFile(
                    pipe_name(self.session_id),
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
            except pywintypes.error as exc:
                last_error = exc
                self._sleep(retry_s)
                continue
            self._handle = handle
            log.info("connected to service pipe session=%s", self.session_id)
            return
        raise PipeUnavailable(f"在 {timeout_s:.0f}s 内无法连接 {pipe_name(self.session_id)}: {last_error}")

    def send(self, message: Envelope | Mapping[str, Any], payload: bytes = b"") -> None:
        data = encode_message(message, payload)
        with self._write_lock:
            handle = self._handle
            if handle is None:
                raise PipeUnavailable("尚未连接服务管道")
            try:
                import win32file

                win32file.WriteFile(handle, data)
            except Exception as exc:  # noqa: BLE001 - 写失败说明连接已死
                raise PipeUnavailable(f"写管道失败: {exc}") from exc

    def recv(self) -> tuple[dict[str, Any], bytes] | None:
        """读一个帧；``None`` 表示服务端正常关闭了管道。"""

        handle = self._handle
        if handle is None:
            return None
        try:
            import win32file

            def peek() -> int | None:
                try:
                    _data, available, _left = peek_named_pipe(handle)
                    return int(available)
                except Exception:  # noqa: BLE001 - 对端关闭
                    return None

            def read(size: int) -> bytes | None:
                try:
                    _err, chunk = win32file.ReadFile(handle, size)
                    return chunk
                except Exception:  # noqa: BLE001 - 读失败
                    return None

            return read_packet(
                peek=peek,
                read=read,
                sleep=self._sleep,
                stop=self._closed.is_set,
                poll_s=self._poll_s,
            )
        except PipeProtocolError as exc:
            raise PipeProtocolError(f"管道流已损坏: {exc}") from exc

    def close(self) -> None:
        self._closed.set()
        handle = self._handle
        self._handle = None
        if handle is not None:
            with contextlib.suppress(Exception):  # 关闭失败由系统回收
                import win32file

                win32file.CloseHandle(handle)


__all__ = [
    "BUFFER_SIZE",
    "CLIENT_RETRY_S",
    "CONNECT_TIMEOUT_S",
    "ClientIdentity",
    "HEADER_STRUCT",
    "MAX_HEADER_BYTES",
    "MAX_PAYLOAD_BYTES",
    "PIPE_PREFIX",
    "POLL_INTERVAL_S",
    "PipeClient",
    "PipeProtocolError",
    "PipeSecurityError",
    "PipeServer",
    "PipeUnavailable",
    "SID_ADMINISTRATORS",
    "SID_LOCAL_SYSTEM",
    "decode_message",
    "encode_message",
    "identity_trusted",
    "peek_named_pipe",
    "pipe_name",
    "pipe_security_attributes",
    "query_client_identity",
    "read_packet",
]