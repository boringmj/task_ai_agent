#!/usr/bin/env python3
"""校验一个 SKILL.md 的 frontmatter 是否合规。用法:python check_skill.py <技能目录>"""
import re, sys, pathlib

d = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
f = d / "SKILL.md"
if not f.is_file():
    print(f"× 没有 SKILL.md:{f}"); sys.exit(1)
text = f.read_text(encoding="utf-8")
m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
if not m:
    print("× 缺少 frontmatter(文件要以 --- 开头,再用 --- 收尾)"); sys.exit(1)
meta = m.group(1)
ok = True
for key in ("name", "description"):
    if not re.search(rf"^{key}\s*:\s*\S", meta, re.M):
        print(f"× frontmatter 缺 {key}"); ok = False
dir_name = d.resolve().name
if (nm := re.search(r"^name\s*:\s*(\S+)", meta, re.M)) and nm.group(1) != dir_name:
    print(f"! name({nm.group(1)}) 和目录名({dir_name})不一致"); ok = False
body = text[m.end():].strip()
if not body.startswith("#"):
    print("× 正文首行应为一级标题(如 # 标题)"); ok = False
print("√ 通过" if ok else "× 有问题")
sys.exit(0 if ok else 1)
