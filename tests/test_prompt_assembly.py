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


def _sub_system(model, tid_holder: list) -> list[dict]:
    """派一个真子 agent,拿它那几条 system 消息(走生产代码那条路)。"""
    model.reply("干完了。")
    tid = tasks.dispatch("随便一件活", fs=helpers.grant(), wait=False)["task_id"]
    tid_holder.append(tid)
    t = helpers.wait_status(tid, ("done", "failed"))
    return [m for m in t.messages if m.get("role") == "system"]


def test_the_shared_block_comes_first_and_is_identical_for_both_roles(model):
    """**这是整步改动的全部意义**:前两条 system 消息,主和子**逐字相同**,而且排在最前。

    缓存按前缀命中 —— 只有"所有 agent 都一样的部分"排在最前面,主 agent 自己那条请求
    才会替所有子 agent 把它们铺进缓存(实测:并发派出的子 agent 一条都没预热,却都命中了
    这一段)。顺序一乱(比如把角色块提到前面),主 agent 铺下的东西子 agent 一个都用不上。
    """
    from agent import cli

    main = [m["content"] for m in cli._system_messages()]
    sub = [m["content"] for m in _sub_system(model, [])]

    assert main[0] == sub[0] == prompts.load("common"), "通用规则没排到最前"
    assert main[1] == sub[1] == prompts.load("tools_guide"), "工具指南没跟上"
    # 第三条起就该分叉了(角色不同)
    assert main[2] != sub[2], "角色块居然一样 —— 那说明主/子根本没分开"
    assert "子 agent 的角色" in sub[2], sub[2][:40]


def test_the_shared_block_has_nothing_that_varies(model):
    """**共用块里不许有任何会变的东西** —— 尤其是步数上限。

    主 agent 是 50 步、子 agent 是 60 步(`MAX_STEPS` / `SUB_MAX_STEPS`),这条限制
    **本身就是角色专属的**。它原来写在"行为准则"里,而行为准则要搬进共用块 ——
    照搬的话,两边的共用块就差了"50"和"60"这几个字符,**前缀从此对不上**,
    整个共用块白做(而且这种错不报错、只是命中率悄悄掉下来)。
    """
    shared = prompts.load("common") + prompts.load("tools_guide")
    sub = _sub_system(model, [])
    task_prompt = "随便一件活"

    assert task_prompt not in shared, "任务描述混进共用块了"
    assert ".tmp" not in shared and "临时区" not in shared, "临时区路径混进来了"
    assert "长期记忆" not in shared, "记忆混进来了(它会变,必须排最后)"
    from agent.core import MAX_STEPS
    for n in (MAX_STEPS, tasks.SUB_MAX_STEPS):
        # 用「N 步」这种整句形式判,别拿裸数字:指南里本来就有 "110~130 行" 这种数字
        assert f"{n} 步" not in shared, \
            f"步数上限「{n} 步」出现在共用块里 —— 主/子的前缀会因此对不上"
    # 子 agent 那边:这几种可变的东西也都不该在共用块那两条里
    assert task_prompt not in sub[0] and task_prompt not in sub[1]


def test_the_subagent_gets_the_long_term_memory_and_it_comes_last(model, workspace):
    """子 agent 也拿到长期记忆(同一个用户、同一个工作区,偏好对它一样成立)。

    但**必须排在最后**:它是这几条里唯一随"什么时候派的活"变的东西 ——
    排前面的话,记忆一改(或者两次派活之间改过),同批子 agent 的共用前缀就断了。
    """
    (workspace / ".agent").mkdir(exist_ok=True)
    (workspace / ".agent" / "memory.md").write_text(
        "用户偏好:报告先给结论再给证据。[这条是测试写的]", encoding="utf-8")

    sub = _sub_system(model, [])

    assert "先给结论再给证据" in sub[-1]["content"], "子 agent 没拿到记忆"
    assert "先给结论再给证据" not in sub[0]["content"], "记忆跑到共用块里去了"


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
    """所有子 agent 拿到的指南**逐字相同** —— 它和角色提示词一样是缓存前缀的一部分。

    (按内容找,不按下标:下标会随着排布变,那样测的就是"第几条"而不是"有没有"了。)
    """
    model.reply("干完了。")
    a = tasks.dispatch("第一件", fs=helpers.grant(), wait=False)["task_id"]
    b = tasks.dispatch("第二件", fs=helpers.grant(), wait=False)["task_id"]
    helpers.wait_status(a, ("done", "failed"))
    helpers.wait_status(b, ("done", "failed"))

    for tid in (a, b):
        contents = [m["content"] for m in tasks.get(tid).messages
                    if m.get("role") == "system"]
        assert _guide() in contents, f"{tid} 的提示词里没有那份指南"
