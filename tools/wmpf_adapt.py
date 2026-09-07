#!/usr/bin/env python3
"""
Auto-adapt WMPFDebugger offsets for a new WeChatAppEx (WMPF) runtime version.

Finds in flue.dll:
  - LoadStartHookOffset : function referencing "applet_index_container.cc"
                          (AppletIndexContainer::OnLoadStart wrapper)
  - CDPFilterHookOffset : first direct call inside the function referencing
                          "SendToClientFilter" / "devtools_message_filter_applet_webview.cc"
  - SceneOffsets        : pointer chain ending at the `!= 1101` magic compare
                          (see WMPFDebugger/ADAPTATION.md)

Usage: python wmpf_adapt.py [path\\to\\flue.dll] [version]
"""

import bisect
import json
import re
import struct
import sys

import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import (
    X86_OP_IMM,
    X86_OP_MEM,
    X86_OP_REG,
    X86_REG_RCX,
    X86_REG_RDX,
    X86_REG_R8,
    X86_REG_R9,
)

DLL_PATH = sys.argv[1] if len(sys.argv) > 1 else (
    r"C:\Users\EDY\AppData\Roaming\Tencent\xwechat\xplugin\Plugins"
    r"\RadiumWMPF\25558\extracted\runtime\flue.dll"
)
WMPF_VERSION = int(sys.argv[2]) if len(sys.argv) > 2 else 25558
OUT_PATH = sys.argv[3] if len(sys.argv) > 3 else "addresses.%d.json" % WMPF_VERSION

MAGIC_SCENE = 1101  # 0x44D, the debug scene number

LOADSTART_MARKERS = [b"applet_index_container.cc", b"OnLoadStart"]
CDP_MARKERS = [b"SendToClientFilter", b"devtools_message_filter_applet_webview.cc"]

ARG_REGS = {X86_REG_RCX, X86_REG_RDX, X86_REG_R8, X86_REG_R9}

# lea r64, [rip+disp32] : REX(48/49/4C/4D) 8D modrm(mod=00, rm=101)
LEA_RIP_RE = re.compile(rb"[\x48\x49\x4C\x4D]\x8D[\x05\x0D\x15\x1D\x25\x2D\x35\x3D].{4}", re.S)


def main():
    print(f"[*] loading {DLL_PATH}")
    data = open(DLL_PATH, "rb").read()
    pe = pefile.PE(data=data, fast_load=True)
    sections = [s for s in pe.sections if s.Name.rstrip(b"\x00").decode(errors="ignore")]

    def raw_slice(sec):
        return data[sec.PointerToRawData : PointerToRawData_end(sec)]

    def PointerToRawData_end(sec):
        return sec.PointerToRawData + min(sec.SizeOfRawData, sec.Misc_VirtualSize or sec.SizeOfRawData)

    def rva_to_raw(rva):
        return pe.get_offset_from_rva(rva)

    exec_secs = [s for s in sections if s.Characteristics & 0x20000000]
    print("[*] sections:", ", ".join(f"{s.Name.rstrip(chr(0).encode()).decode()}@0x{s.VirtualAddress:x}" for s in sections))

    # ---------- 1. locate marker strings (full null-terminated strings containing markers)
    strings = {}  # rva -> str
    for marker in LOADSTART_MARKERS + CDP_MARKERS:
        for m in re.finditer(re.escape(marker), data):
            start = data.rfind(b"\x00", 0, m.start()) + 1
            end = data.find(b"\x00", m.end())
            if end == -1:
                continue
            rva = None
            for s in sections:
                if s.PointerToRawData <= start < s.PointerToRawData + s.SizeOfRawData:
                    rva = s.VirtualAddress + (start - s.PointerToRawData)
                    break
            if rva is None:
                continue
            try:
                text = data[start:end].decode("utf-8", "replace")
            except Exception:
                continue
            strings.setdefault(rva, text)
    print(f"[*] found {len(strings)} candidate strings")
    for rva, text in sorted(strings.items()):
        short = text if len(text) < 80 else text[:77] + "..."
        print(f"    0x{rva:x}: {short!r}")

    # ---------- 2. find lea rip-relative xrefs to those strings in executable sections
    xrefs = []  # (insn_rva, string_rva, text)
    for sec in exec_secs:
        sec_data = data[sec.PointerToRawData : sec.PointerToRawData + sec.SizeOfRawData]
        base_rva = sec.VirtualAddress
        for m in LEA_RIP_RE.finditer(sec_data):
            insn_end = base_rva + m.start() + 7
            disp = struct.unpack("<i", m.group(0)[3:7])[0]  # group(0) = REX+8D+modrm+disp32
            target = insn_end + disp
            if target in strings:
                xrefs.append((insn_end - 7, target, strings[target]))
    print(f"[*] found {len(xrefs)} string xrefs")

    # ---------- 3. parse .pdata -> function ranges
    pdata = next((s for s in sections if s.Name.rstrip(b"\x00").decode() == ".pdata"), None)
    funcs = []
    raw = data[pdata.PointerToRawData : pdata.PointerToRawData + pdata.Misc_VirtualSize]
    for i in range(0, len(raw) - 11, 12):
        b, e, _ = struct.unpack_from("<III", raw, i)
        if b and e > b:
            funcs.append((b, e))
    funcs.sort()
    begins = [f[0] for f in funcs]
    print(f"[*] parsed {len(funcs)} function ranges from .pdata")

    def func_of(rva):
        i = bisect.bisect_right(begins, rva) - 1
        if i >= 0 and funcs[i][0] <= rva < funcs[i][1]:
            return funcs[i]
        return None

    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    md.skipdata = True

    def disasm(begin, end, limit=None):
        off = rva_to_raw(begin)
        size = end - begin
        if limit:
            size = min(size, limit)
        return list(md.disasm(data[off : off + size], begin))

    def safe_ops(i):
        try:
            return i.operands
        except Exception:
            return []  # skipdata pseudo-insn has no operand info

    def xref_funcs(marker_substrs):
        """map: func_begin -> set of marker strings referenced"""
        markers = [m.decode() if isinstance(m, bytes) else m for m in marker_substrs]
        out = {}
        for insn_rva, str_rva, text in xrefs:
            if not any(ms in text for ms in markers):
                continue
            f = func_of(insn_rva)
            if f:
                out.setdefault(f[0], set()).add(text)
        return out

    # ---------- 4. CDPFilterHookOffset: first direct call in the xref function
    print("\n=== CDP filter analysis ===")
    cdp_offset = None
    for fbegin, refs in xref_funcs(CDP_MARKERS).items():
        fend = func_of(fbegin)[1]
        print(f"[*] xref function 0x{fbegin:x} refs {sorted(refs)}")
        insns = disasm(fbegin, fend, limit=0x400)
        calls = [
            safe_ops(i)[0].imm
            for i in insns
            if i.mnemonic == "call" and safe_ops(i) and safe_ops(i)[0].type == X86_OP_IMM
        ][:5]
        for t in calls:
            print(f"    call -> 0x{t:x} (pdata func: {func_of(t) is not None})")
        if calls:
            cdp_offset = calls[0]

    # ---------- 5. LoadStartHookOffset + SceneOffsets
    print("\n=== OnLoadStart analysis ===")

    def has_earlier_write(insns, end_idx, reg):
        """whether any instruction before end_idx writes `reg`"""
        for k in range(end_idx):
            o = safe_ops(insns[k])
            if o and o[0].type == X86_OP_REG and o[0].reg == reg:
                return True
        return False

    def resolve_chain(insns, start_idx, reg, stop_regs):
        """walk back from start_idx resolving `reg` through mov rA,[rB+disp] / mov rA,rB.
        stop when the base register holds an incoming argument (no earlier write).
        returns (stop_reg, [disp,...] backtrack order) or (None, chain)"""
        chain = []
        cur, idx = reg, start_idx
        for _ in range(12):
            j = None
            for k in range(idx - 1, -1, -1):
                o = safe_ops(insns[k])
                if o and o[0].type == X86_OP_REG and o[0].reg == cur:
                    j = k
                    break
            if j is None:
                # cur was never written inside the function -> incoming argument
                return (cur if cur in stop_regs else None), chain
            i = insns[j]
            ops = safe_ops(i)
            if i.mnemonic not in ("mov", "lea") or len(ops) < 2:
                return None, chain
            if ops[1].type == X86_OP_MEM and ops[1].mem.index == 0:
                chain.append(ops[1].mem.disp)
                base = ops[1].mem.base
                if not has_earlier_write(insns, j, base):
                    return (base if base in stop_regs else None), chain
                cur, idx = base, j
            elif ops[1].type == X86_OP_REG:
                src = ops[1].reg
                if not has_earlier_write(insns, j, src):
                    return (src if src in stop_regs else None), chain
                cur, idx = src, j
            else:
                return None, chain
        return None, chain

    def find_magic_callee(fbegin, depth=2, seen=None):
        """BFS callees looking for one containing the 1101 compare"""
        seen = seen or set()
        if depth < 0 or fbegin in seen:
            return None, None
        seen.add(fbegin)
        fend = func_of(fbegin)[1]
        insns = disasm(fbegin, fend)
        calls = [
            safe_ops(i)[0].imm
            for i in insns
            if i.mnemonic == "call" and safe_ops(i) and safe_ops(i)[0].type == X86_OP_IMM
        ]
        for c in calls:
            cb = func_of(c)
            if not cb:
                continue
            cinsns = disasm(cb[0], cb[1])
            for idx, i in enumerate(cinsns):
                if any(o.type == X86_OP_IMM and o.imm == MAGIC_SCENE for o in safe_ops(i)):
                    return cb[0], (cb, cinsns, idx, insns, calls)
        if depth > 0:
            for c in calls:
                cb = func_of(c)
                if cb:
                    r = find_magic_callee(cb[0], depth - 1, seen)
                    if r[0] is not None:
                        return r
        return None, None

    results = []
    for fbegin, refs in xref_funcs(LOADSTART_MARKERS).items():
        fend = func_of(fbegin)[1]
        print(f"[*] candidate wrapper 0x{fbegin:x} refs {sorted(r[:40] for r in refs)}")
        cb_begin, info = find_magic_callee(fbegin)
        if not info:
            print("    no 1101-magic callee found")
            continue
        cb, cinsns, cidx, winsns, wincalls = info
        print(f"    1101-magic callee: 0x{cb_begin:x}")

        # callee chain: compare reg/mem -> back to rcx (arg0 = miniapp config ptr)
        cmp_i = cinsns[cidx]
        cops = safe_ops(cmp_i)
        so_tail = None
        if cops and cops[0].type == X86_OP_MEM:
            m = cops[0].mem
            if m.index == 0:
                stop, ch = resolve_chain(cinsns, cidx, m.base, {X86_REG_RCX})
                if stop is not None:
                    so_tail = [m.disp] + ch
        elif cops and cops[0].type == X86_OP_REG:
            stop, ch = resolve_chain(cinsns, cidx, cops[0].reg, {X86_REG_RCX})
            if stop is not None:
                so_tail = ch
        if not so_tail:
            print("    failed to resolve scene pointer chain in callee")
            continue
        print(f"    callee chain (backtrack order): {so_tail}")

        # wrapper chain: rcx at call site -> back to wrapper arg0 (this).
        # the compiler may split the pointer chain between wrapper and callee.
        call_to_callee = next(
            (k for k, i in enumerate(winsns) if i.mnemonic == "call" and safe_ops(i)
             and safe_ops(i)[0].type == X86_OP_IMM and safe_ops(i)[0].imm == cb_begin),
            None,
        )
        if call_to_callee is None:
            print("    call to magic callee not found in wrapper (indirect?)")
            continue
        # callee arg0 register = rcx
        stop, ch = resolve_chain(winsns, call_to_callee, X86_REG_RCX, set(ARG_REGS))
        if stop != X86_REG_RCX:
            print(f"    failed to resolve wrapper chain (stop={stop}, chain={ch})")
            continue
        print(f"    wrapper chain (backtrack order): {ch}")

        # forward order = reversed(backtrack order)
        scene = list(reversed(ch)) + list(reversed(so_tail))
        if len(scene) != 6:
            print(f"    combined chain length {len(scene)} != 6: {scene}")
            continue
        print(f"    SceneOffsets: {scene}")
        results.append({"wrapper": fbegin, "scene": scene, "refs": sorted(refs)})

    if not results:
        print("[!] no valid OnLoadStart candidate")
        return

    # pick the candidate referencing applet_index_container.cc, else first
    pick = next((r for r in results if any("applet_index_container.cc" in t for t in r["refs"])), results[0])
    load_start = pick["wrapper"]
    scene = pick["scene"]

    print("\n=== result ===")
    config = {
        "Version": WMPF_VERSION,
        "LoadStartHookOffset": hex(load_start),
        "CDPFilterHookOffset": hex(cdp_offset) if cdp_offset else None,
        "SceneOffsets": scene,
    }
    print(json.dumps(config, indent=2))
    if cdp_offset is None:
        print("[!] CDPFilterHookOffset not resolved")
        return
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
    print(f"[*] written {OUT_PATH}")


if __name__ == "__main__":
    main()
