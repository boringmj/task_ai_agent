"""task-ai-agent 命令行入口。

实现都在同名的 agent/ 包(agent/ 目录)里;这里只保留入口,保持 `python agent.py` 的用法不变。
"""
import signal

# **启动期先忽略 Ctrl+C。** 导入(openai 那套要好几秒)和初始化(会话恢复、Docker 探活、
# 回放历史)都在主循环之前,那一段**没有任何东西接得住 KeyboardInterrupt** —— 实测:
# 启动后一两秒内按 Ctrl+C,进程直接以 0xC000013A(被 Ctrl+C 结束)退出,连一句提示都没有。
# 用户完全可能因为"怎么还没起来"而按一下,于是看起来就像"Ctrl+C 就是把程序关掉"。
#
# 进入 REPL 之前会在 cli._session_loop 里恢复成默认处理(见那里的 restore)。那之后
# Ctrl+C 由 REPL 自己接:只清空输入 / 打断当前一轮,**永不退出**;退出走 /exit 或 Ctrl+D。
signal.signal(signal.SIGINT, signal.SIG_IGN)

from agent.cli import main  # noqa: E402  (必须在上面那行之后再导入)

if __name__ == "__main__":
    main()
