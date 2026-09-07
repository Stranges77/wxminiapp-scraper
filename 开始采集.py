#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Stranges77  (https://github.com/Stranges77)
# 许可证: 非商业学习许可 (详见仓库 LICENSE)
# 仅供个人学习研究使用, 禁止商业用途与大规模爬取
"""
一键采集: 检测微信 → 自动注入 → 等你打开商品页 → 自动跑采集脚本
用法:  python 开始采集.py
"""

__version__ = "1.0.0"

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
WMPF_DIR = BASE / "third_party" / "WMPFDebugger"
SCRAPER = BASE / "wx_scraper.py"
CDP_HOST, CDP_PORT = "127.0.0.1", 62000


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wechat_running() -> bool:
    for name in ("Weixin.exe", "WeChat.exe"):
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            if name in out:
                return True
        except Exception:
            pass
    return False


def start_injection():
    """新开一个控制台窗口运行 WMPFDebugger (注入日志留在那个窗口)。"""
    ps_cmd = (
        "$Host.UI.RawUI.WindowTitle='WMPFDebugger 注入'; "
        f"Set-Location -LiteralPath '{WMPF_DIR}'; "
        "npx ts-node src/index.ts --debug-frida --debug-main"
    )
    subprocess.Popen(
        ["powershell", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def wait_port(timeout_sec: float) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if port_open(CDP_HOST, CDP_PORT):
            return True
        time.sleep(1)
    return False


async def _get_targets(ws, msg_id):
    """在持久连接上发 Target.getTargets, 返回 targetInfos (失败返回 None)。"""
    try:
        await ws.send(json.dumps({"id": msg_id, "method": "Target.getTargets"}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=3)
            m = json.loads(raw)
            if m.get("id") == msg_id:
                return m.get("result", {}).get("targetInfos", [])
    except Exception:
        return None


async def poll_product_page(timeout_sec: float = 300):
    """轮询 CDP, 检测快手小店商品详情页 (webview target) 是否已打开。
    用一条持久 WebSocket 连接 (避免反复连断刷注入窗口日志)。
    """
    import websockets

    deadline = time.time() + timeout_sec
    start = time.time()
    ws = None
    msg_id = 0
    miniapp_seen = False
    warned_entry = False
    last_line = ""

    while time.time() < deadline:
        if ws is None:
            try:
                ws = await websockets.connect(
                    f"ws://{CDP_HOST}:{CDP_PORT}",
                    max_size=8 * 1024 * 1024, open_timeout=3,
                )
            except Exception:
                ws = None
        infos = None
        if ws is not None:
            msg_id += 1
            infos = await _get_targets(ws, msg_id)
            if infos is None:
                try:
                    await ws.close()
                except Exception:
                    pass
                ws = None

        if infos:
            urls = [(t.get("url") or "") for t in infos]
            has_webview = any(
                t.get("type") == "webview" and "kwaixiaodian" in (t.get("url") or "")
                for t in infos
            )
            has_miniapp = any("servicewechat.com" in u for u in urls)

            if has_webview:
                wt = next(
                    t for t in infos
                    if t.get("type") == "webview" and "kwaixiaodian" in (t.get("url") or "")
                )
                print()
                return wt

            if has_miniapp and not miniapp_seen:
                miniapp_seen = True
                print("\r    ✅ 小程序已连上调试通道, 请进入【商品详情页】...          ")

            if not has_miniapp and not miniapp_seen and not warned_entry and time.time() - start > 45:
                warned_entry = True
                print("\r" + " " * 70 + "\r", end="")
                print("    ⚠️  已等待 45 秒, 小程序还没进入调试模式!")
                print("       —— 这几乎都是『打开入口』不对造成的")
                print("       请看『WMPFDebugger 注入』窗口:")
                print("         · 正常: 会出现 [hook] hook scene condition -> 1101")
                print("                 且 [miniapp] miniapp client connected")
                print("         · 你现在: 只有 [hook] scene: 1000/1096, 没有上面两行")
                print("       解决: 不要用『发现页 → 小程序』入口 (scene 1000 会崩)!")
                print("         改用 ① 微信顶部搜索框搜『快手小店』进入")
                print("              ② 或聊天里点开分享的小程序卡片")
                print("         关掉快手小店重开即可, 本脚本会继续等待...")
                print()

        remaining = int(deadline - time.time())
        line = f"    等待商品页... (剩余 {remaining // 60}分{remaining % 60:02d}秒)"
        if line != last_line:
            print("\r" + line + "          ", end="", flush=True)
            last_line = line
        await asyncio.sleep(2)

    if ws is not None:
        try:
            await ws.close()
        except Exception:
            pass
    print()
    return None


def main() -> int:
    print("=" * 56)
    print("  快手小店 一键采集  v" + __version__)
    print("=" * 56)

    if wechat_running():
        print(" [1/4] 微信运行中")
    else:
        input(" [1/4] 没检测到微信, 请先启动 PC 微信后按回车继续...")

    if port_open(CDP_HOST, CDP_PORT):
        print(f" [2/4] 端口 {CDP_PORT} 已监听 → 注入已就绪 (复用)")
    else:
        print(f" [2/4] 端口 {CDP_PORT} 未监听 → 新窗口启动 WMPFDebugger 注入...")
        start_injection()
        print("       等待注入完成 (最多 90 秒, 注入日志在新开的窗口里)")
        if not wait_port(90):
            print(" [!] 90 秒内端口未就绪, 退出。")
            print("     请看『WMPFDebugger 注入』窗口的报错:")
            print("     · version config not found → 微信升级了, 用 tools\\wmpf_adapt.py 重新适配")
            print("     · 找不到 npx/node → 检查 Node.js 安装")
            return 1
        print(" [2/4] 注入完成, CDP 通道已就绪")

    print(" [3/4] 现在请你在微信里操作:")
    print("       ① 打开 快手小店 小程序 (从聊天卡片/搜索/最近使用进入,")
    print("          不要用发现页小程序入口, 会导致小程序崩溃)")
    print("       ② 进入某个商品详情页 (能看到标题/价格/图片)")
    t = asyncio.run(poll_product_page(300))
    if not t:
        print("\n [!] 5 分钟内未检测到商品页, 退出。可重跑本脚本再试。")
        return 1
    print(f" [3/4] 检测到商品页: {(t.get('url') or '')[:66]}...")

    print(" [4/4] 启动采集脚本 wx_scraper.py\n")
    rc = subprocess.call([sys.executable, str(SCRAPER)])
    return rc or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
