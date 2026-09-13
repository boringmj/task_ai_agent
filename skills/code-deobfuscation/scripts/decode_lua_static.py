#!/usr/bin/env python3
"""Lua 静态解码小工具:char 数组 / hex / base64 / 反转 / 异或。

用法:
    python3 decode_lua_static.py char    "112,114,105,110,116"
    python3 decode_lua_static.py char    "string.char(112,114,105,110,116)"
    python3 decode_lua_static.py base64  "cHJpbnQ="
    python3 decode_lua_static.py hex     "7072696e74"
    python3 decode_lua_static.py reverse "tnirp"
    python3 decode_lua_static.py xor     "1,2,3" --key 42
"""
import argparse
import base64
import binascii
import re
import sys


def as_bytes(s: str) -> bytes:
    """把参数当字节串:优先按 utf-8,失败则 latin-1。"""
    try:
        return s.encode("latin-1")
    except UnicodeEncodeError:
        return s.encode("utf-8")


def dec_char(s: str) -> bytes:
    nums = [int(n) for n in re.findall(r"\d+", s)]
    return bytes(n for n in nums if 0 <= n < 256)


def dec_base64(s: str) -> bytes:
    t = s.strip()
    return base64.b64decode(t + "=" * (-len(t) % 4))


def dec_hex(s: str) -> bytes:
    return binascii.unhexlify("".join(s.split()))


def dec_reverse(s: str) -> bytes:
    return s[::-1].encode("utf-8", "replace")


def dec_xor(s: str, key: int) -> bytes:
    data = dec_char(s) if re.fullmatch(r"[\d,\s]+", s.strip()) else as_bytes(s)
    return bytes(b ^ key for b in data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["char", "base64", "hex", "reverse", "xor"])
    ap.add_argument("value")
    ap.add_argument("--key", type=int, default=0, help="xor 模式的密钥(单字节)")
    args = ap.parse_args()

    if args.mode == "char":
        out = dec_char(args.value)
    elif args.mode == "base64":
        out = dec_base64(args.value)
    elif args.mode == "hex":
        out = dec_hex(args.value)
    elif args.mode == "reverse":
        out = dec_reverse(args.value)
    else:
        out = dec_xor(args.value, args.key)

    try:
        print(out.decode("utf-8"))
    except UnicodeDecodeError:
        print(out.decode("latin-1"))


if __name__ == "__main__":
    main()
