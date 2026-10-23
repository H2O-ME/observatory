"""Windows 系统层：SCM 宿主、会话跨越、命名管道。

本包是 0.3.0 规格 §4.2 中 ``win/`` 的唯一实现。模块分工：

``win.service``   SCM 生命周期、失败恢复动作、安装/删除服务、**唯一**的进程等待与服务停止/启动辅助，
                  以及 A2 的一次性重启。
``win.session``   WTS 会话发现 + ``CreateProcessAsUserW``（SYSTEM → 用户会话）。
``win.pipe``      服务↔worker 单一信封管道（ACL 收紧 + 客户端身份校验）。

跨平台约定：本包在非 Windows 上**可以 import**（纯函数与规划函数可单测），但任何真正调用 Win32 API 的
函数都会抛 :class:`WindowsOnlyOperation`，绝不静默返回空值。
"""

from __future__ import annotations

__all__ = ["WindowsOnlyOperation"]


class WindowsOnlyOperation(RuntimeError):
    """在当前平台（非 Windows）无法执行该操作。"""