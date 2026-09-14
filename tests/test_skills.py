"""技能:发现、解析、谁能加载、依赖状况怎么告诉模型。

**为什么值得测**:技能是**提示词的一部分**,它出错的两种方式都很难发现。

  · 坏掉的技能**静默消失** —— 作者以为装上了,而模型从来没见过它。所以坏技能必须在
    清单里露头(带错误),而不是被跳过。
  · 该给子 agent 的技能**没拦住** —— 主 agent 一旦加载了分析型技能,几万 token 就进了
    它最宝贵的上下文,而这是它存在的意义(协调全局,不是自己啃细节)。拦人的地方在
    `load()` 里,而且必须在**返回正文之前**拦:放进去再拦就已经失败了。
"""
from __future__ import annotations

import pytest
from agent import skills


@pytest.fixture
def skill_root(tmp_path, monkeypatch):
    """技能目录和 `.pylibs` 都指到本测试专属的临时目录。"""
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(skills, "SKILLS_DIR", root)
    monkeypatch.setattr(skills, "PYLIBS", tmp_path / ".pylibs")
    return root


def _mk(root, name, frontmatter: str, body: str = "正文", resources=()) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    for r in resources:
        p = d / r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")


# ============================== frontmatter ==============================

def test_a_file_without_frontmatter_is_all_body():
    meta, body = skills._parse_frontmatter("就是正文")
    assert meta == {} and body == "就是正文"


def test_an_unclosed_frontmatter_is_treated_as_none():
    """开了 `---` 却没闭合 —— 当成"没有 frontmatter",而不是把整篇吃掉。"""
    text = "---\nname: x\n\n正文还在下面"
    meta, body = skills._parse_frontmatter(text)
    assert meta == {}
    assert "正文还在下面" in body


def test_broken_yaml_says_so_instead_of_being_swallowed():
    with pytest.raises(skills.SkillError) as e:
        skills._parse_frontmatter("---\nagents: [main\n---\n正文")
    assert "YAML" in str(e.value)


def test_frontmatter_must_be_a_mapping():
    with pytest.raises(skills.SkillError):
        skills._parse_frontmatter("---\n- 只是个列表\n---\n正文")


# ============================== 谁能用 ==============================

def test_the_role_list_is_required_no_default():
    """**不给默认值是故意的**:分析型技能会让加载它的 agent 多吃几万 token,那必须是有意
    为之 —— "没写就放行"等于把新写的重技能默认继续压在主 agent 身上。"""
    with pytest.raises(skills.SkillError) as e:
        skills.parse_agents({"description": "干点活"})
    assert "agents" in str(e.value)


def test_the_role_list_is_validated_and_deduped():
    assert skills.parse_agents({"agents": ["sub", "main", "sub"]}) == ["sub", "main"]
    with pytest.raises(skills.SkillError):
        skills.parse_agents({"agents": "main"})            # 不是列表
    with pytest.raises(skills.SkillError) as e:
        skills.parse_agents({"agents": ["main", "everyone"]})
    assert "everyone" in str(e.value), "报错要说清是哪个字不认识"


# ============================== 依赖声明 ==============================

def test_a_dep_must_say_exactly_one_thing():
    with pytest.raises(skills.SkillError):
        skills.parse_deps({"requires": [{"pip": "a", "skill": "b"}]}, "requires")
    with pytest.raises(skills.SkillError):
        skills.parse_deps({"requires": [{"reason": "随便"}]}, "requires")


def test_a_dep_must_say_why_it_is_needed():
    """不写清为什么需要它,读的人没法判断它到底能不能少。"""
    with pytest.raises(skills.SkillError) as e:
        skills.parse_deps({"requires": [{"pip": "pandas"}]}, "requires")
    assert "reason" in str(e.value)


def test_an_optional_dep_must_say_what_happens_without_it():
    """**可选依赖的缺失代价必须写出来** —— 写不出一个能接受的下场,它就该是硬依赖。"""
    with pytest.raises(skills.SkillError) as e:
        skills.parse_deps({"optional": [{"pip": "rich", "reason": "排版好看"}]}, "optional")
    assert "fallback" in str(e.value)

    ok = skills.parse_deps({"optional": [
        {"pip": "rich", "reason": "排版好看", "fallback": "退化成纯文本,不影响结论"}]},
        "optional")
    assert ok[0]["kind"] == "pip" and ok[0]["fallback"]


def test_an_unknown_field_is_refused():
    with pytest.raises(skills.SkillError) as e:
        skills.parse_deps({"requires": [{"pip": "a", "reason": "r", "版本": "1.0"}]}, "requires")
    assert "版本" in str(e.value), "写了不认识的字段要当场说,别当没看见"


# ============================== 依赖满没满 ==============================

def test_a_missing_dep_is_never_reported_as_present(skill_root):
    """**只给两种答案,不给"大概是吧"** —— 把没有的说成有,会让人直接动手,然后在使用
    的那一刻才发现跑不起来。反过来的错只是多确认一句。"""
    assert skills.resolve_dep({"kind": "pip", "name": "numpy"}) == "missing"
    assert skills.resolve_dep({"kind": "skill", "name": "不存在"}) == "missing"


def test_installed_packages_are_found_by_both_names(skill_root, tmp_path):
    """`*.dist-info` 给的是**发行名**(pyyaml),顶层目录给的是**导入名**(yaml),
    技能里两种写法都可能出现,所以两个都得算。"""
    libs = tmp_path / ".pylibs"
    (libs / "PyYAML-6.0.3.dist-info").mkdir(parents=True)
    (libs / "yaml").mkdir()
    (libs / "yaml" / "__init__.py").write_text("", encoding="utf-8")
    (libs / "single.py").write_text("", encoding="utf-8")
    (libs / "__pycache__").mkdir()

    found = skills._installed_packages()

    assert {"pyyaml", "yaml", "single"} <= found
    assert "__pycache__" not in found


def test_package_names_are_compared_the_pep503_way():
    assert skills._norm_pkg("PyYAML") == "pyyaml"
    assert skills._norm_pkg("py-yaml") == skills._norm_pkg("py.yaml") == "py_yaml"


# ============================== 发现 ==============================

def test_a_broken_skill_shows_up_with_its_error(skill_root):
    """**坏掉的技能不能藏起来。** 静默消失是最糟的:作者以为装上了,而模型从来没见过它。"""
    _mk(skill_root, "好的", "agents: [main]\ndescription: 没事")
    _mk(skill_root, "坏的", "agents: [main\n")            # YAML 缺个括号

    out = {s["name"]: s for s in skills.discover()}

    assert set(out) == {"好的", "坏的"}
    assert out["好的"]["error"] == ""
    assert "YAML" in out["坏的"]["error"], "坏在哪儿要写在清单里"


def test_a_directory_without_a_skill_file_is_not_a_skill(skill_root):
    (skill_root / "随手建的目录").mkdir()
    assert skills.names() == []


def test_resources_are_reported(skill_root):
    _mk(skill_root, "带资源", "agents: [main]\ndescription: 有脚本",
        resources=("scripts/run.py", "references/doc.md"))
    entry = skills.discover()[0]
    assert set(entry["resources"]) == {"scripts", "references"}
    assert "assets" not in entry["resources"]


# ============================== 加载与拦截 ==============================

def _load_front(agents: str) -> str:
    return f"agents: {agents}\ndescription: 干点活"


def test_load_returns_the_body_without_the_frontmatter(skill_root):
    _mk(skill_root, "简单", _load_front("[main]"), body="怎么做事的一二三四")
    body = skills.load("简单", role="main")
    assert body.startswith("怎么做事的一二三四")
    assert "agents:" not in body, "frontmatter 不该进上下文"


def test_the_main_agent_is_refused_a_sub_only_skill_and_told_what_to_do(skill_root):
    """**在返回正文之前拦。** 正文一进上下文,省上下文这件事就已经失败了。

    而且拒绝必须**能接着往下走**:只说"不行"的话,模型会开始瞎绕(换个工具硬做、
    或者凭印象编),所以要说清该找谁。
    """
    _mk(skill_root, "重分析", _load_front("[sub]"))

    with pytest.raises(skills.SkillError) as e:
        skills.load("重分析", role="main")

    assert "派" in str(e.value) and "子 agent" in str(e.value)


def test_a_sub_agent_is_refused_a_main_only_skill_and_told_to_report_it(skill_root):
    _mk(skill_root, "协调类的", _load_front("[main]"))

    with pytest.raises(skills.SkillError) as e:
        skills.load("协调类的", role="sub")

    assert "报告" in str(e.value), "子 agent 不能往下派,只能报上去"
    assert "凑" in str(e.value), "得拦住它用别的方法凑一个结果出来"


def test_loading_something_that_does_not_exist_lists_the_ones_that_do(skill_root):
    _mk(skill_root, "有的", _load_front("[main]"))
    with pytest.raises(skills.SkillError) as e:
        skills.load("没的", role="main")
    assert "有的" in str(e.value)


def test_every_resource_is_listed_with_where_it_lives_in_the_container(skill_root):
    """**一个都不能少。** 没列出来的文件模型根本不知道它存在,于是永远不会去用它 ——
    而且这不报错、不被察觉,只表现为"这个技能能干的活比实际少"。"""
    _mk(skill_root, "带脚本", _load_front("[main]"),
        resources=("scripts/a.py", "scripts/b.py", "references/c.md"))
    body = skills.load("带脚本", role="main")

    for want in ["scripts/a.py", "scripts/b.py", "references/c.md"]:
        assert want in body, f"{want} 没列出来"
    assert f"{skills.CONTAINER_SKILLS_DIR}/带脚本/scripts/a.py" in body, \
        "得说清它在容器里挂在哪(技能目录在工作区之外,文件工具够不到)"


def test_build_artifacts_are_not_listed_as_resources(skill_root):
    """反过来的一面:`__pycache__/*.pyc` 是跑脚本掉出来的垃圾,列出来纯属占地方。"""
    _mk(skill_root, "跑过的", _load_front("[main]"),
        resources=("scripts/a.py", "scripts/__pycache__/a.cpython-311.pyc"))
    body = skills.load("跑过的", role="main")
    assert "scripts/a.py" in body
    assert ".pyc" not in body
    assert "带了 1 个附带资源" in body, "数量也要对得上"


# ============================== 依赖状况怎么说给模型 ==============================

def test_a_missing_hard_dependency_forbids_improvising(skill_root):
    """模型的本能是"绕过去",而绕出来的东西用户看不出来是残的 —— 那是**看着像结论的
    猜测**,比明说做不了糟糕得多。"""
    _mk(skill_root, "要 pandas", _load_front("[main]") +
        "\nrequires:\n  - pip: pandas\n    reason: 要读表格")
    body = skills.load("要 pandas", role="main")

    assert "[!]" in body and "pandas" in body
    assert "不要换别的方法硬做" in body
    assert "告诉用户" in body


def test_without_a_dependency_note_it_says_nothing_at_all(skill_root):
    """可选依赖全缺时**静默** —— 它的缺失代价本该是可接受的,不值得占注意力。
    留一个光秃秃的 "### 依赖" 标题纯属占地方(还容易让人以为下面漏印了东西)。"""
    _mk(skill_root, "没依赖", _load_front("[main]"))
    assert "### 依赖" not in skills.load("没依赖", role="main")


# ============================== 提示词里那份清单 ==============================

def test_the_main_agent_sees_the_sub_only_skills_too_and_they_are_tagged(skill_root):
    """**看得见、加载不了** —— 这是有意的:不知道有哪些活可以派出去,就没法合理分配任务。
    真正拦人的地方在 load()。"""
    _mk(skill_root, "给子的", _load_front("[sub]") + "\ndescription: 重活")
    _mk(skill_root, "给主的", _load_front("[main]") + "\ndescription: 轻活")

    text = skills.prompt_section(role="main")

    assert "- `给子的` **[子 agent 专用]** —— 重活" in text, "标记要跟在对的那一条后面"
    assert "- `给主的` —— 轻活" in text, "主 agent 自己也能用的不该被标"


def test_a_sub_agent_only_sees_what_it_can_load(skill_root):
    """子 agent 反过来:它不能往下派,给它看一堆用不了的技能只是白占上下文,还会诱导它去试。"""
    _mk(skill_root, "给子的", _load_front("[sub]") + "\ndescription: 重活")
    _mk(skill_root, "给主的", _load_front("[main]") + "\ndescription: 轻活")

    text = skills.prompt_section(role="sub")

    assert "给子的" in text
    assert "给主的" not in text
    assert "不能再往下派活" in text


def test_a_broken_skill_is_still_listed_in_the_prompt(skill_root):
    _mk(skill_root, "坏的", "agents: [main\n")
    text = skills.prompt_section(role="main")
    assert "坏的" in text, "坏技能也得露头,不然作者永远不知道它没生效"
    assert "加载会失败" in text


def test_an_empty_skill_dir_says_so(skill_root):
    text = skills.prompt_section(role="main")
    assert "没有任何技能" in text
