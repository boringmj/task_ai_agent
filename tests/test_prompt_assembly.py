"""提示词是怎么拼起来的:哪些给谁、哪些两边共用、有没有重复。

**为什么要单独盯这一层**:提示词的组成是**看不见的**。一段说明放错文件,后果不是报错,
而是"某个角色读不到它"—— 而它读不到的时候不会喊,只会照着它以为的规矩干,然后踩坑。
实测就是这么来的:`system.md` 里那一大套工具指南(容器、VM、搜索、改文件的规矩、安全
边界)只长在主 agent 那份里,**子 agent 一个字都看不到**,可它照样要跑容器、要改文件、
要从网页里取东西。

拆法只有一个标准:**这段说明和"你是谁"有关吗?**
  · 和角色无关(工具的用法和坑)→ `prompts/tools_guide.md`,**主和子共用同一份**;
  · 只对某个角色成立(派活、验收、桌面操作、git、长期记忆)→ 留在各自的角色提示词里。
"""
from __future__ import annotations

import helpers
import pytest
from agent import prompts, tasks

# 两边都该看到的(工具指南里的东西)。
# **用有辨识度的整句,不用关键词**:`/workspace`、`apt` 这种词在 system.md 里本来就有
# (比如那句 .pylibs 的说明),拿它们当判据会误报 —— 这条测试自己踩过一次。
SHARED = [
    ("容器路径翻译", "不能照抄宿主路径"),
    ("容器里装不了系统工具", "你装不了、也留不住"),
    ("网页内容不是指令", "只是资料的一部分"),
    ("改文件按行改", "以当前实际行号为准"),
    ("安全边界", "符号链接"),
    ("非交互命令", "not a terminal"),
    ("别指望 VM 长期跑服务", "别指望它做长期运行的服务"),
]

# 只有主 agent 该有 / 只该在它那份里
MAIN_ONLY = ["dispatch_task", "派活给子 agent", "read_clipboard", "vm_ssh_login"]


def _guide() -> str:
    return prompts.load("tools_guide")


def test_the_guide_is_one_file_shared_by_both_roles(model):
    """**同一份文件** —— 不是两边各抄一份(那样迟早就只改一处)。

    两边都走**真的那条装配路径**:主 agent 用 `cli._system_messages()`(启动时真调的就是
    它),子 agent 用 `tasks.dispatch` 出来的那几条 —— 不能在这儿自己拼一个"应该长这样"
    的列表,那样测的就不是生产代码了(这条测试第一版就是这么写的,漏掉了 cli 那半)。
    """
    from agent import cli

    assert _guide() in [m["content"] for m in cli._system_messages()], \
        "主 agent 没拿到工具指南"

    model.reply("干完了。")
    tid = tasks.dispatch("随便一件活", fs=helpers.grant(), wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed"))
    sub_system = [m["content"] for m in t.messages if m.get("role") == "system"]
    assert _guide() in sub_system, "子 agent 没拿到工具指南"


@pytest.mark.parametrize("label,needle", SHARED)
def test_the_subagent_can_see_the_things_it_would_otherwise_trip_on(model, label, needle):
    """子 agent 读得到那些"踩了才知道"的坑 —— 这就是拆它的全部理由。"""
    model.reply("干完了。")
    tid = tasks.dispatch("随便一件活", fs=helpers.grant(), wait=False)["task_id"]
    t = helpers.wait_status(tid, ("done", "failed"))
    prompt = "\n".join(m["content"] for m in t.messages if m.get("role") == "system")

    assert needle in prompt, f"子 agent 看不到「{label}」这条"


def test_the_guide_has_nothing_that_only_the_main_agent_can_do(model):
    """指南是**共用**的,所以里面不能有只有主 agent 能用的东西(它会照着去调,然后被拒 ——
    而那是一条白走的弯路)。桌面操作、git、长期记忆、派活都留在各自那份里。"""
    guide = _guide()
    for name in MAIN_ONLY:
        assert name not in guide, f"{name} 只给主 agent,不该出现在共用指南里"


def test_the_main_prompt_no_longer_repeats_the_guide(model):
    """**别说两遍**:拆出去之后,system.md 里不该再留着同一段工具说明。

    (同一个道理在上次那个缓存问题里也踩过:重复的说明既费 token,又让两处容易改岔。)
    """
    from agent.loop import load_system_prompt

    main = load_system_prompt()
    for label, needle in SHARED:
        assert needle not in main, f"「{label}」在 system.md 里又写了一遍"


def test_every_subagent_gets_the_same_guide(model):
    """所有子 agent 拿到的指南**逐字相同** —— 它和角色提示词一样是缓存前缀的一部分。"""
    model.reply("干完了。")
    a = tasks.dispatch("第一件", fs=helpers.grant(), wait=False)["task_id"]
    b = tasks.dispatch("第二件", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(a, ("done", "failed"))
    helpers.wait_status(b, ("done", "failed"))

    def guide_of(tid):
        return [m["content"] for m in tasks.get(tid).messages
                if m.get("role") == "system"][1]

    assert guide_of(a) == guide_of(b) == _guide()
