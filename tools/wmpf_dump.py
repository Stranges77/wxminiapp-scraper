#!/usr/bin/env python3
"""Dump disassembly of specific functions from flue.dll for manual inspection."""
import struct
import sys

import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

DLL_PATH = (
    r"C:\Users\EDY\AppData\Roaming\Tencent\xwechat\xplugin\Plugins"
    r"\RadiumWMPF\25558\extracted\runtime\flue.dll"
)

data = open(DLL_PATH, "rb").read()
pe = pefile.PE(data=data, fast_load=True)

sections = []
for s in pe.sections:
    sections.append((s.Name.rstrip(b"\x00").decode(errors="ignore"), s))

pdata = next(s for n, s in sections if n == ".pdata")
raw = data[pdata.PointerToRawData : pdata.PointerToRawData + pdata.Misc_VirtualSize]
funcs = []
for i in range(0, len(raw) - 11, 12):
    b, e, _ = struct.unpack_from("<III", raw, i)
    if b and e > b:
        funcs.append((b, e))
funcs.sort()
begins = [f[0] for f in funcs]

import bisect


def func_of(rva):
    i = bisect.bisect_right(begins, rva) - 1
    if i >= 0 and funcs[i][0] <= rva < funcs[i][1]:
        return funcs[i]
    return None


def rva_to_raw(rva):
    return pe.get_offset_from_rva(rva)


md = Cs(CS_ARCH_X86, CS_MODE_64)
md.detail = False
md.skipdata = True

targets = [int(x, 16) for x in sys.argv[1:]]

for t in targets:
    f = func_of(t)
    if not f:
        print(f"0x{t:x}: not inside a pdata function")
        continue
    begin, end = f
    print(f"\n===== function 0x{begin:x} - 0x{end:x} (size 0x{end-begin:x}) =====")
    off = rva_to_raw(begin)
    n = 0
    for i in md.disasm(data[off : off + (end - begin)], begin):
        marker = "  <<<" if i.address == t else ""
        print(f"  0x{i.address - begin:06x}  {i.mnemonic} {i.op_str}{marker}")
        n += 1
        if n > 400:
            print("  ... truncated")
            break
