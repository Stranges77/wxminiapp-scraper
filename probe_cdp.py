#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Stranges77  (https://github.com/Stranges77)
# 许可证: 非商业学习许可 (详见仓库 LICENSE)
# 仅供个人学习研究使用, 禁止商业用途与大规模爬取
"""
probe_cdp.py — 微信小程序 CDP 采集可行性体检
=============================================

在运行 wx_scraper.py 之前, 先跑这个脚本确认环境。共 5 项检查:

  1. 微信 / WMPF 小程序运行时版本 (WMPFDebugger 是否支持)
  2. CDP 代理端口 (默认 ws://127.0.0.1:62000) 是否在监听
  3. WebSocket 握手 + CDP Target.getTargets 能否列出调试目标
  4. 小程序页面里 DOM 是否可用 (typeof document)
     —— 用于区分 WebView 渲染(可采集) 与 Skyline 原生渲染(无 DOM)
  5. 图片 URL 样例 (确认商品图能被抓到)

前置条件 (按顺序):
  1. 安装 Node.js LTS v22 (https://nodejs.org) 并安装 yarn: npm i -g yarn
  2. 安装内置 WMPFDebugger 的依赖:
       cd third_party/WMPFDebugger && yarn
  3. 登录微信 (建议小号), 在 third_party/WMPFDebugger 目录运行:
       npx ts-node src/index.ts
     看到 "frida script loaded, ... you can now open any miniapps"
  4. 【顺序不能反】在微信里打开目标小程序并进入一个商品页
  5. 另开终端 (项目根目录) 运行本脚本: python probe_cdp.py

用法:
  python probe_cdp.py                # 默认端口 62000
  python probe_cdp.py --port 62001   # 自定义 CDP 代理端口
"""

__version__ = "1.0.3"

import argparse
import asyncio
import glob
import json
import os
import socket
import sys
from pathlib import Path

try:
    import websockets
except ImportError:
    print("[!] 缺少依赖, 请先运行: pip install -r requirements.txt")
    sys.exit(1)


# WMPFDebugger 官方支持的 WMPF 版本号 (摘自其 README, 2026-09 时点)
# 来源: https://github.com/evi0s/WMPFDebugger
SUPPORTED_WMPF = {
    25558, 25510, 25459, 25364, 25297, 25268, 20089, 20079, 20005, 20001,
    19977, 19921, 19899, 19881, 19871, 19841, 19823, 19769, 19749,
    19481, 19459, 19339, 19201, 19027, 18955, 18891, 18787, 18151,
    18055, 17127, 17071, 17037, 16965, 16815, 16771, 16467, 16389,
    16203, 16133, 14315, 14199, 14161, 13909, 13871, 13655, 13639,
    13487, 13341, 13331, 11633, 11581,
}

CDP_URL_TMPL = "ws://127.0.0.1:{port}"

# 探测页面 DOM 用的 JS
PROBE_JS = r"""
(() => {
    if (typeof document === 'undefined') {
        return { has_dom: false };
    }
    const urls = new Set();
    document.querySelectorAll('img,wx-image,image,[src],[data-src]').forEach(e => {
        const s = e.currentSrc || e.src || e.getAttribute('src') || e.getAttribute('data-src');
        if (s && /^https?:\/\//.test(s)) urls.add(s);
    });
    document.querySelectorAll('[style*="background"]').forEach(e => {
        const m = (e.getAttribute('style') || '').match(/url\(([^)]+)\)/);
        if (m) {
            const u = m[1].replace(/['"]/g, '').trim();
            if (/^https?:\/\//.test(u)) urls.add(u);
        }
    });
    const imgList = Array.from(urls);
    return {
        has_dom: true,
        title: document.title || '',
        url: location.href,
        body_len: (document.body && document.body.innerText || '').length,
        img_count: imgList.length,
        img_samples: imgList.slice(0, 8),
    };
})()
"""


# ---------------- 第 1 项: 微信 / WMPF 版本 ----------------
def detect_wmpf_versions():
    """在常见路径搜索 RadiumWMPf 插件目录, 返回 [(版本号, 路径), ...]"""
    appdata = os.environ.get("APPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    roots = [
        # 微信 4.x (Weixin)
        os.path.join(appdata, "Tencent", "xwechat", "XPlugin", "Plugins", "RadiumWMPf"),
        os.path.join(pf, "Tencent", "Weixin", "XPlugin", "Plugins", "RadiumWMPf"),
        os.path.join(pf86, "Tencent", "Weixin", "XPlugin", "Plugins", "RadiumWMPf"),
        # 微信 3.x (WeChat)
        os.path.join(appdata, "Tencent", "WeChat", "XPlugin", "Plugins", "RadiumWMPf"),
        os.path.join(pf, "Tencent", "WeChat", "XPlugin", "Plugins", "RadiumWMPf"),
        os.path.join(pf86, "Tencent", "WeChat", "XPlugin", "Plugins", "RadiumWMPf"),
    ]
    found = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        # 结构: <root>\<数字版本号>\extracted\WeChatAppEx.exe
        for exe in glob.glob(os.path.join(root, "*", "extracted", "WeChatAppEx.exe")):
            ver_dir = Path(exe).parent.parent.name  # extracted 的上一级就是版本号目录
            if ver_dir.isdigit():
                found.append((int(ver_dir), exe))
        # 新版结构: <root>\<数字版本号>\extracted\runtime\WeChatAppEx.exe
        for exe in glob.glob(os.path.join(root, "*", "extracted", "runtime", "WeChatAppEx.exe")):
            ver_dir = Path(exe).parent.parent.parent.name
            if ver_dir.isdigit():
                found.append((int(ver_dir), exe))
        # 个别版本目录直接就是 <root>\<数字>\WeChatAppEx.exe
        for exe in glob.glob(os.path.join(root, "*", "WeChatAppEx.exe")):
            ver_dir = Path(exe).parent.name
            if ver_dir.isdigit():
                found.append((int(ver_dir), exe))
    # 去重 (同一版本可能多条路径)
    uniq = {}
    for ver, exe in found:
        uniq.setdefault(ver, exe)
    return sorted(uniq.items(), reverse=True)


def check_versions():
    print("[检查 1/5] 微信 / WMPF 小程序运行时版本")
    versions = detect_wmpf_versions()
    if not versions:
        print("  [!] 没有找到 WMPF 小程序运行时。")
        print("      可能原因: 还没装微信, 或装完微信后从未打开过任何小程序。")
        print("      处理: 先在微信里随便打开一个小程序, 让运行时下载完成, 再跑本脚本。")
        return False
    for ver, exe in versions:
        ok = ver in SUPPORTED_WMPF
        tag = "支持" if ok else ("未知(比工具新, 需更新 WMPFDebugger)" if ver > max(SUPPORTED_WMPF) else "不支持")
        print(f"  WMPF 版本 {ver:<6} [{tag}]")
        print(f"    路径: {exe}")
    latest = versions[0][0]
    if latest in SUPPORTED_WMPF:
        print("  [通过] 当前 WMPF 版本受 WMPFDebugger 支持。")
        return True
    print("  [!] 最新 WMPF 版本不在已知支持列表。")
    print("      处理: 去 https://github.com/evi0s/WMPFDebugger 拉最新代码 (yarn),")
    print("            或关注其版本适配 issue; 也可试 GUI 工具 First:")
    print("            https://github.com/Spade-sec/First")
    return False


# ---------------- 第 2 项: 端口监听 ----------------
def check_port(port: int):
    print(f"[检查 2/5] CDP 代理端口 {port} 是否在监听")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        print(f"  [通过] 127.0.0.1:{port} 可连接。")
        return True
    except Exception:
        print(f"  [失败] 127.0.0.1:{port} 连不上。")
        print("      处理: 确认 WMPFDebugger 已启动 (npx ts-node src/index.ts),")
        print("            且看到 'frida script loaded' 字样; 微信和小程序保持打开。")
        return False


# ---------------- 极简 CDP 客户端 (体检专用) ----------------
class MiniCDP:
    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._waiters = {}
        self._listener = None

    async def _listen(self):
        async for raw in self.ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mid = msg.get("id")
            if mid in self._waiters:
                if not self._waiters[mid].done():
                    self._waiters[mid].set_result(msg)

    async def start(self):
        self._listener = asyncio.create_task(self._listen())

    async def call(self, method, params=None, session_id=None, timeout=15):
        self._id += 1
        mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._waiters[mid] = fut
        payload = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        await self.ws.send(json.dumps(payload))
        try:
            res = await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.pop(mid, None)
        if "error" in res:
            raise RuntimeError(f"CDP {method} 返回错误: {res['error']}")
        return res.get("result", {})

    async def close(self):
        if self._listener:
            self._listener.cancel()


# ---------------- 第 3/4/5 项: CDP 连通 + DOM + 图片 ----------------
async def check_cdp(port: int):
    url = CDP_URL_TMPL.format(port=port)
    print(f"[检查 3/5] WebSocket 握手 + Target.getTargets")
    try:
        ws = await websockets.connect(url, max_size=64 * 1024 * 1024, open_timeout=5)
    except Exception as e:
        print(f"  [失败] WS 连接失败: {e}")
        return False

    cdp = MiniCDP(ws)
    await cdp.start()
    try:
        try:
            await cdp.call("Target.setDiscoverTargets", {"discover": True})
            result = await cdp.call("Target.getTargets", timeout=10)
        except Exception as e:
            print(f"  [失败] CDP 握手/枚举目标失败: {e}")
            print("      可能原因: WMPFDebugger 已启动但小程序还没打开,")
            print("                或打开小程序的顺序不对 (要先启动调试器, 再开小程序)。")
            return False

        targets = result.get("targetInfos", [])
        pages = [t for t in targets if t.get("type") == "page"]
        mini_pages = [t for t in pages if "servicewechat" in (t.get("url") or "")]
        print(f"  CDP 目标总数 {len(targets)}, 页面目标 {len(pages)}, 小程序页面 {len(mini_pages)}")
        for t in pages:
            print(f"    - [{t.get('type')}] {t.get('title','(无标题)')[:30]} | {t.get('url','')[:80]}")

        if not pages:
            print("  [失败] 没有任何页面目标。请在微信里打开小程序后重试。")
            return False
        if not mini_pages:
            print("  [!] 没看到 servicewechat.com 的小程序页面, 将对全部页面尝试探测。")

        # 第 4/5 项: 对每个小程序页面探测 DOM
        print("[检查 4/5] 小程序页面 DOM 可用性 (区分 WebView / Skyline)")
        print("[检查 5/5] 图片 URL 样例")
        candidates = mini_pages or pages
        any_ok = False
        for t in candidates[:5]:
            tid = t.get("targetId")
            try:
                att = await cdp.call("Target.attachToTarget",
                                     {"targetId": tid, "flatten": True}, timeout=10)
                sid = att.get("sessionId")
                if not sid:
                    continue
                info = await cdp.call("Runtime.evaluate", {
                    "expression": PROBE_JS,
                    "returnByValue": True,
                    "awaitPromise": False,
                }, session_id=sid, timeout=10)
                val = (info.get("result") or {}).get("value")
                if not val:
                    print(f"  - {t.get('title','')[:20]}: evaluate 无返回")
                    continue
                if not val.get("has_dom"):
                    print(f"  - {t.get('title','')[:20]}: [!] 无 DOM (疑似 Skyline 原生渲染)")
                    print("      Skyline 页面无法用 DOM 方式采集, 需换抓包方案。")
                    continue
                any_ok = True
                print(f"  - 页面: {val.get('title','(无标题)')[:40]}")
                print(f"    URL     : {val.get('url','')[:90]}")
                print(f"    正文字数: {val.get('body_len',0)}")
                print(f"    图片数  : {val.get('img_count',0)}")
                for s in val.get("img_samples", [])[:5]:
                    print(f"      样例: {s[:100]}")
            except Exception as e:
                print(f"  - 目标 {tid} 探测失败: {e}")

        if any_ok:
            print()
            print("  [通过] DOM 与图片均可访问! 下一步运行: python wx_scraper.py")
            return True
        print()
        print("  [失败] 没有可采集的小程序页面。")
        print("      若提示 Skyline: 该小程序用原生渲染, DOM 路线不适用。")
        print("      若全部探测失败: 把本屏输出截图, 反馈排查。")
        return False
    finally:
        await cdp.close()
        await ws.close()


def main():
    ap = argparse.ArgumentParser(description="微信小程序 CDP 采集可行性体检")
    ap.add_argument("--port", type=int, default=62000, help="CDP 代理端口 (默认 62000)")
    args = ap.parse_args()

    print("=" * 60)
    print("微信小程序 CDP 采集可行性体检  v" + __version__)
    print("=" * 60)
    print()

    r1 = check_versions()
    print()
    r2 = check_port(args.port)
    print()
    r3 = asyncio.run(check_cdp(args.port)) if r2 else False

    print()
    print("=" * 60)
    print("体检结果汇总")
    print("=" * 60)
    print(f"  WMPF 版本受支持 : {'是' if r1 else '否/未知'}")
    print(f"  CDP 端口可连    : {'是' if r2 else '否'}")
    print(f"  页面可采集      : {'是' if r3 else '否'}")
    print()
    if r1 and r2 and r3:
        print("全部通过! 现在可以运行: python wx_scraper.py")
    else:
        print("存在未通过项, 按上面的提示处理后重新体检。")
        print("工具链: WMPFDebugger  https://github.com/evi0s/WMPFDebugger")


if __name__ == "__main__":
    main()
