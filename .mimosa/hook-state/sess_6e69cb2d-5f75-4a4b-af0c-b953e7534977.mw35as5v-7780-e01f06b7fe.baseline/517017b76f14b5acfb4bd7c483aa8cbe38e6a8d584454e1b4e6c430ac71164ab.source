"""受控端核心层（0.3.0）。

这里只放"与平台无关、与进程角色无关"的东西：协议信封、唯一链路、上行队列、
配置时钟与日志。``win/`` 负责 SCM/会话/管道，``tasks/`` 负责具体动作，
``main.py`` 只做模式分发。依赖方向永远是 ``main -> win/tasks -> core``，
core 不允许 import 任何旧模块（``transport``/``client``/``runner``/``worker``/
``spool``/``config``/``logging_setup``），也不允许 import ``win``/``tasks``。

这个包内的纯逻辑（信封编解码、退避状态机、变化判定）按约束：
无全局可变状态、不读取系统时钟（时间一律由调用方作为参数传入），
以便 `tests/unit` 直接构造断言。
"""

from __future__ import annotations

__all__: list[str] = []