"""虚拟机相关的指令。

会话现在带着自己的虚拟机磁盘(见 agent/session.py 的目录结构),它会被持久化,
所以需要一个"推倒重来"的口子 —— 否则那块盘只会越长越大。
"""
from __future__ import annotations

from . import Context, command


@command(
    "/vmreset",
    "重置虚拟机:停掉它、删掉本会话的虚拟机磁盘,再用基础镜像重新开始。"
    "虚拟机里的一切都会丢失(装过的软件、写过的文件),工作区文件与长期记忆不受影响。",
    hint="用户说虚拟机被搞乱了、无法启动、或想从干净状态重来时。"
         "注意重置不可逆、也不可取消,先说清楚再动手。",
)
def cmd_vmreset(ctx: Context) -> str:
    from ..tools.vm import vm_reset
    return vm_reset()
