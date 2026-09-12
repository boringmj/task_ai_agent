from __future__ import annotations

import atexit
import sys

from rich.markdown import Markdown

from .core import (
    AUTO_COMPACT_RATIO,
    ROOT,
    TRASH_MAX_AGE_DAYS,
    console,
)
from .llm import usage_detail, usage_line
from .loop import compact, load_system_prompt, run
from .session import (
    claim_owner,
    clear_session,
    load_session,
    release_owner,
    rewrite_session,
)
from .tools.container import _docker_cleanup_stale, _docker_health
from .tools.memory import _read_memory
from .tools.trash import purge_trash
from .tools.vm import _vm_kickoff, _vm_state_get, vm_status


def _cleanup_trash_on_start() -> None:
    """启动时删除超过保留期(默认 7 天)的回收站文件。

    必须传 TRASH_MAX_AGE_DAYS —— 若调 purge_trash()(无参),会变成"清空全部",
    把还没过保留期的文件也一起删了,那就违背"只删 7 天以上"的本意了。
    """
    try:
        result = purge_trash(TRASH_MAX_AGE_DAYS)
        if "删除 0 个" not in result:  # 只在确有清理时提示,免得每次启动都罗嗦
            console.print(f"回收站:{result}", style="dim")
    except Exception as exc:  # noqa: BLE001 - 回收站清理失败不应阻止 agent 启动
        console.print(f"回收站清理失败(不影响使用):{exc}", style="dim")


def _read_multiline(prompt: str = "你 > ") -> str:
    """读取一段多行输入,空行提交 —— 支持粘贴多行并保留换行。

    console.input() 只读单行,没法粘贴多行代码/文本。改为逐行读取,
    用户输入完(或粘贴完)按一个空行结束。空行只作提交信号,不会进消息。
    """
    lines: list[str] = []
    try:
        console.print(prompt, style="bold cyan", end="")
        while True:
            line = sys.stdin.readline()
            if line == "":  # EOF(Ctrl+D / Ctrl+Z),停止
                break
            if line in {"\n", "\r\n"}:  # 空行 = 提交
                break
            lines.append(line.rstrip("\r\n"))
            console.print("… ", style="dim", end="")
    except KeyboardInterrupt:
        # 空闲时 Ctrl+C = 退出信号(返回 None)。
        # 必须包住整个函数(含提示打印),否则中断落在 console.print 里的
        # os.get_terminal_size() 时(像这次的栈)会从这个函数逃逸,直接崩掉进程。
        # 恢复打印用裸 write(不走 Rich),避免 get_terminal_size 又被中断引发二次异常。
        sys.stdout.write("\n")
        return None
    except EOFError:
        pass
    return "\n".join(lines)


def main() -> None:
    messages: list[dict] = [{"role": "system", "content": load_system_prompt()}]
    memory = _read_memory().strip()  # 跨会话记住的关键事实最先注入,始终在场
    if memory:
        messages.append(
            {
                "role": "system",
                "content": f"以下是跨会话保留的长期记忆,和你的对话无关,仅供参考:\n{memory}",
            }
        )
    # 会话恢复要赶在别的事情前面:先登记占用者(发现别的实例仍在用就提醒),
    # 再把上次的对话读回来接在最新的 system 消息之后。
    conflict = claim_owner()
    history, resume_note = load_session()
    messages.extend(history)
    atexit.register(release_owner)   # 正常退出时摘掉占用者标记
    _cleanup_trash_on_start()
    # 预处理 Docker 健康状态(非阻断):可用则做残留清理,不可用仅警告,agent 照常启动
    ok, msg = _docker_health()
    if ok:
        n = _docker_cleanup_stale()
        if n:
            console.print(f"已清理 {n} 个上次残留的容器", style="dim")
    console.print(f"Docker:{'✅ ' if ok else '⚠ 不可用 —— '}{msg}", style="dim" if ok else "yellow")
    # 后台拉起虚拟机(非阻断,失败仅提示,agent 照常启动)
    try:
        _vm_kickoff()  # 后台线程启动/配置虚拟机,不阻塞
        ready = _vm_state_get()["status"] == "ready"
        console.print(f"VM:{vm_status()}", style="dim" if ready else "yellow")
    except Exception as exc:  # noqa: BLE001
        console.print(f"VM:启动失败(不影响 agent)—— {exc}", style="yellow")
    console.print("Agent 已启动。", style="bold")
    console.print("输入多行:连续输入,最后一个空行提交(支持粘贴)。", style="dim")
    console.print("执行中 Ctrl+C=取消本轮;空闲时 Ctrl+C=退出;exit 退出。", style="dim")
    console.print(f"/compact 压缩上下文(省 token);/tokens 看用量;/new 开新会话;占用达 "
                  f"{AUTO_COMPACT_RATIO:.0%} 会自动压缩。", style="dim")
    console.print(f"工作区:{ROOT}", style="dim")
    console.print(f"会话:{resume_note}", style="dim")
    if conflict:
        console.print(conflict, style="yellow")
    console.print()

    while True:
        try:
            user_input = _read_multiline()  # 多行读取,空行提交
            if user_input is None:  # 空闲时 Ctrl+C = 退出
                console.print("再见。", style="dim")
                break
            text = user_input.rstrip()  # 去掉粘贴时多带的结尾空行,保留行内缩进
            if not text.strip():
                continue
            if text in {"exit", "quit"}:
                break
            if text == "/compact":
                console.print(compact(messages), style="dim")
                rewrite_session(messages)   # 历史被换掉了,磁盘上同步成压缩后的样子
                continue
            if text == "/tokens":
                console.print(usage_detail(), style="dim")
                continue
            if text == "/new":
                # 开新会话:只保留 system(提示词与长期记忆),其余清掉
                kept = 0
                while kept < len(messages) and messages[kept].get("role") == "system":
                    kept += 1
                del messages[kept:]
                clear_session()
                console.print("已开新会话,之前的对话不再带入。", style="dim")
                continue

            # 快照这一轮开始前的整份历史。存"内容"而不是长度 —— 本轮里可能发生
            # 自动压缩(把历史改短),那时按长度回滚会算错位置:短了删不掉、长了会
            # 把压缩后的新历史也削掉一截。整份快照才能精确还原。
            # 浅拷贝即可,消息字典本身不再改动;还原用切片赋值,list 对象身份不变。
            snapshot = list(messages)
            try:
                reply = run(text, messages)
            except KeyboardInterrupt:
                # 执行中 Ctrl+C:回滚半截对话,回到提示,会话不退出
                messages[:] = snapshot
                console.print("\n[已取消]", style="bold red")
                continue
            except Exception as exc:  # noqa: BLE001
                # 未预期的错误(渲染、网络、工具内部崩了……)绝不能掀翻整个会话 ——
                # 丢掉这一轮的对话会让人白等,还要从头把上下文喂一遍。
                # 这里 **同样要回滚**:半截历史里很可能留着一个没有 tool 结果配对的
                # tool_calls,那个发给 API 会直接 400,回滚才能保证历史始终合法。
                messages[:] = snapshot
                console.print(f"\n[出错,本轮已回滚]{type(exc).__name__}: {exc}", style="bold red")
                continue

            # 这一轮成功了才落盘(回滚的两个分支上面都 continue 了,不会被写进去)。
            # 整份重写而不是追加:本轮里可能发生过自动压缩,历史已被整体换掉,
            # 追加会把"压缩后"和"压缩前"的消息混在一个文件里。文件本身有界
            # (受上下文上限与压缩约束,通常几百 KB),整体重写的开销可忽略,
            # 换来的是怎么都不会错。
            rewrite_session(messages)

            console.print("AI >", style="bold green")
            # Markdown 要拿到完整文本才能正确解析,所以是等模型说完再一次性渲染
            console.print(Markdown(reply) if reply.strip() else "(模型没有返回内容)")
            line = usage_line()
            if line:
                console.print(line, style="dim")
            console.print()
        except KeyboardInterrupt:
            # 兜底:任何没被上面捕获的 Ctrl+C(例如渲染 Markdown 那一下),
            # 一律干净退出,而不是抛栈崩掉。
            console.print("\n再见。", style="dim")
            break


if __name__ == "__main__":
    main()
