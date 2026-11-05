"""受控端任务层（0.3.0）。

每个模块只负责一类动作，全部由服务进程或 worker 进程装配：

- ``capture.py``  —— 唯一的采屏引擎（agent-core 负责）
- ``input.py``    —— SendInput / 虚拟桌面坐标 / DPI（agent-sys 负责）
- ``fs.py``       —— 文件操作与分片（agent-sys 负责）
- ``update.py``   —— 唯一更新路径 verify→stage→precheck→swap→rollback（agent-sys 负责）
- ``uninstall.py``—— 整目录 staging 的卸载助手（agent-sys 负责）

约定：任务层不直接持有链路；它只返回结果（``core.commands.CommandResult``），
由装配层通过 ``Link.send("result", …)`` 回传。
"""

from __future__ import annotations

__all__: list[str] = []