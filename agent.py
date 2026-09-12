"""task-ai-agent 命令行入口。

实现都在同名的 agent/ 包(agent/ 目录)里;这里只保留入口,保持 `python agent.py` 的用法不变。
"""
from agent.cli import main

if __name__ == "__main__":
    main()
