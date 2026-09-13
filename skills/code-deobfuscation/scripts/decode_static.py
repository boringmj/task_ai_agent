#!/usr/bin/env python3
"""编码链静态解码器:只做解码,绝不执行样本代码。

对输入搜索一条编码链(gzip/zlib、base64、hex、URL、Unicode 转义、字符码位、
rot13、反转),自动挑出"解出来最像明文"的一条,并逐步打印中间产物,形成证据链。
解不动就停,不瞎猜。

用法:
    python3 decode_static.py --str "SGVsbG8gd29ybGQ="
    python3 decode_static.py --file blob.txt
"""
import argparse
import base64
import binascii
import gzip
import urllib.parse
import zlib

_ROT13 = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    "NOPQRSTUVWXYZABCDEFGHIJKLMNOPqrstuvwxyzabcdefghijklm")


def readability(data: bytes) -> float:
    if not data:
        return 0.0
    good = sum(1 for c in data if 32 <= c < 127 or c in (9, 10, 13))
    return good / len(data)


def score(data: bytes) -> float:
    """越像源码/明文分越高:可打印比例 + 有效 UTF-8 + 含空白。"""
    if not data:
        return 0.0
    try:
        text = data.decode("utf-8")
        valid = True
    except UnicodeDecodeError:
        text = data.decode("latin-1", "ignore")
        valid = False
    blank = (text.count(" ") + text.count("\n") + text.count("\t")) / max(len(text), 1)
    return readability(data) + (0.05 if valid else 0.0) + min(blank * 2, 0.5)


def _b64(raw: bytes) -> bytes:
    text = raw.decode("latin-1")
    return base64.b64decode(text + "=" * (-len(text) % 4))


DECODERS = {
    "gzip": gzip.decompress,
    "zlib": zlib.decompress,
    "base64": _b64,
    "hex": lambda raw: binascii.unhexlify("".join(raw.decode("latin-1").split())),
    "url": lambda raw: urllib.parse.unquote(raw.decode("latin-1")).encode("latin-1"),
    "unicode_escape": lambda raw: (
        raw.decode("latin-1").encode("latin-1").decode("unicode_escape").encode("latin-1")),
    "rot13": lambda raw: raw.decode("latin-1").translate(_ROT13).encode("latin-1"),
    "reverse": lambda raw: raw[::-1],
    "char_codes": lambda raw: bytes(
        int(x) for x in raw.decode("latin-1").replace(" ", "").split(",") if x.strip().isdigit()),
}


def candidates(raw: bytes):
    out = []
    for name, fn in DECODERS.items():
        try:
            cand = fn(raw)
        except Exception:
            continue
        if cand and cand != raw:
            out.append((name, cand))
    return out


def best_chain(raw: bytes, max_depth: int):
    best = {"score": score(raw), "path": (), "data": raw}
    seen = set()

    def rec(cur, path, depth):
        if cur in seen:
            return
        seen.add(cur)
        s = score(cur)
        if s > best["score"]:
            best.update(score=s, path=path, data=cur)
        if depth >= max_depth:
            return
        for name, cand in candidates(cur):
            rec(cand, path + (name,), depth + 1)

    rec(raw, (), 0)
    return best


def preview(data: bytes, limit: int = 200) -> str:
    text = data.decode("utf-8", "replace")
    return text if len(text) <= limit else text[:limit] + " …"


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--str", help="待解码的字符串")
    g.add_argument("--file", help="含待解码内容的文件")
    ap.add_argument("--max-depth", type=int, default=6)
    args = ap.parse_args()

    raw = args.str.encode("utf-8") if args.str else open(args.file, "rb").read()
    result = best_chain(raw, args.max_depth)

    print(f"[0] 原始({len(raw)} bytes): {preview(raw)}")
    cur = raw
    for i, name in enumerate(result["path"], 1):
        cur = DECODERS[name](cur)
        print(f"[{i}] {name} -> {preview(cur)}")

    if not result["path"]:
        print("[*] 没解出更可读的结果(可能本就不是编码链,或需要针对性处理)。")
    print("\n==== 最终结果 ====")
    print(result["data"].decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
