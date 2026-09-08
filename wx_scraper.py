#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Stranges77  (https://github.com/Stranges77)
# 许可证: 非商业学习许可 (详见仓库 LICENSE)
# 仅供个人学习研究使用, 禁止商业用途与大规模爬取
"""
wx_scraper.py — 微信小程序商品页图片/文本采集器
================================================

原理: PC 微信小程序运行时 (WMPF) 经 WMPFDebugger (Frida 注入) 解锁后,
在 ws://127.0.0.1:62000 暴露标准 Chrome DevTools Protocol。
本脚本用 Python 直连该 WebSocket, 像操作网页一样:
  滚动加载 → 点击"展开/查看全部" → 提取图片 URL 与正文 → httpx 下载图片
  → 保存格式化的 商品信息.txt

前置条件 (首次使用):
  1. pip install -r requirements.txt
  2. 安装 Node.js LTS v22 + yarn, 安装内置 WMPFDebugger 的依赖:
       cd third_party/WMPFDebugger
       yarn
  3. 登录微信 (建议小号), 在 third_party/WMPFDebugger 目录运行:
       npx ts-node src/index.ts
     看到 "frida script loaded" 后, 【再】在微信里打开目标小程序
  4. 先跑体检:  python probe_cdp.py
  5. 本脚本:    python wx_scraper.py
     按提示在微信里进入商品详情页, 回到终端回车开始采集

用法:
  python wx_scraper.py                  # 自动匹配含"快手"的小程序页面
  python wx_scraper.py --keyword 商品    # 按关键词匹配页面
  python wx_scraper.py --list           # 列出所有调试目标后手动选择
  python wx_scraper.py --port 62001     # 自定义 CDP 端口
  python wx_scraper.py --out D:\采集     # 自定义输出根目录
"""

__version__ = "1.0.3"

import argparse
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path

try:
    import websockets
    import httpx
except ImportError:
    print("[!] 缺少依赖, 请先运行: pip install -r requirements.txt")
    sys.exit(1)


CDP_URL_TMPL = "ws://127.0.0.1:{port}"
DEFAULT_PORT = 62000

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://servicewechat.com/",
}

# Pillow 缺失时给一次提示，避免每张 webp 图片都刷屏
_PILLOW_WARNED = False


# ========================= CDP 客户端 =========================
class CDPClient:
    """极简 Chrome DevTools Protocol 客户端 (WebSocket, flatten session)。

    除了处理 call() 的请求/响应配对, 额外新增事件订阅能力:
    - 所有 CDP method 响应 (带 id) 仍走 _waiters
    - 所有 CDP 事件 (method + params, 不带 id) 追加进 self.events 队列,
      由调用方后台任务消费 (目前用来抓 Network.responseReceived 作为 DOM 失败兜底)
    """

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._waiters = {}
        self._listener = None
        self.events = asyncio.Queue()        # 异步队列 (method, params, sessionId)
        self._listening_stop = False

    async def _listen(self):
        async for raw in self.ws:
            if self._listening_stop:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None and mid in self._waiters and not self._waiters[mid].done():
                self._waiters[mid].set_result(msg)
                continue
            # CDP 事件: 不带 id, 有 method
            m = msg.get("method")
            if m:
                # flatten session 的事件会带 sessionId 在消息外层
                sid = msg.get("sessionId")
                await self.events.put((m, msg.get("params") or {}, sid))

    async def start(self):
        self._listener = asyncio.create_task(self._listen())

    async def call(self, method, params=None, session_id=None, timeout=20):
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
            raise RuntimeError("CDP %s 错误: %s" % (method, res["error"]))
        return res.get("result", {})

    async def evaluate(self, session_id, expression, await_promise=False, timeout=60):
        res = await self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        }, session_id=session_id, timeout=timeout)
        if res.get("exceptionDetails"):
            raise RuntimeError("JS 执行异常: %s"
                               % json.dumps(res["exceptionDetails"], ensure_ascii=False)[:300])
        return (res.get("result") or {}).get("value")

    async def close(self):
        self._listening_stop = True
        if self._listener:
            self._listener.cancel()


# ========================= 页面操作 JS =========================
# 点击「立即购买 / 立即下单 / 选规格 / 加入购物车」等按钮,
# 弹出规格选择弹层 (颜色 / 尺码 / 价格 / 库存 都在这里),
# 等待 1.5s 让内容渲染到 DOM 后关闭弹层。
OPEN_SKU_JS = r"""
new Promise(async (resolve) => {
    if (typeof document === 'undefined') { resolve({opened:false}); return; }
    const SKU_BTN_RE = /^(立即购买|加入购物车|马上抢|领券购买|购买|选规格|立即订购|抢购|去购买|选规格购买|已选|立即秒杀|限时秒杀|秒杀购买)/;
    let btn = null;
    // 优先: 底部大按钮 (一般是 button / view), 文字完全匹配
    for (const el of document.querySelectorAll('button, div, span, wx-view, view, a')) {
        const t = (el.innerText || '').trim();
        if (!t || t.length > 12) continue;
        if (SKU_BTN_RE.test(t) && t.indexOf('进店') === -1 && t.indexOf('详情') === -1
            && t.indexOf('客服') === -1 && t.indexOf('收藏') === -1 && t.indexOf('分享') === -1) {
            // 过滤"立即购买"被包在 商品详情头部标题栏 里那种极小容器 (<50px)
            const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {width:100,height:40};
            if ((r.width === 0 && r.height === 0) || (r.width > 30 && r.height > 20)) { btn = el; break; }
        }
    }
    if (!btn) { resolve({opened:false, reason:'no-buy-btn'}); return; }
    let clicked = false;
    try { btn.click(); clicked = true; } catch (e) {}
    // 模拟点击失败时的兜底: dispatchEvent
    if (!clicked) {
        try {
            const ev = new MouseEvent('click', {bubbles:true, cancelable:true});
            clicked = btn.dispatchEvent(ev);
        } catch (e) {}
    }
    await new Promise(r => setTimeout(r, 1500));
    resolve({opened: clicked, btnText: (btn.innerText || '').trim()});
})
"""

# 滚动到底部若干轮, 触发懒加载, 最后回到顶部
SCROLL_JS = r"""
new Promise(async (resolve) => {
    if (typeof document === 'undefined') { resolve({error: 'no dom'}); return; }
    let lastH = -1, stable = 0;
    for (let i = 0; i < 40; i++) {
        window.scrollTo(0, document.body.scrollHeight);
        await new Promise(r => setTimeout(r, 500));
        const h = document.body.scrollHeight;
        if (h === lastH) { stable++; if (stable >= 3) break; }
        else { stable = 0; lastH = h; }
    }
    window.scrollTo(0, 0);
    resolve({height: document.body.scrollHeight});
})
"""

# 点击所有 "展开/查看全部/查看更多" 类按钮
EXPAND_JS = r"""
(() => {
    if (typeof document === 'undefined') return 0;
    let n = 0;
    document.querySelectorAll('button, div, span, a, wx-view, view').forEach(el => {
        const t = (el.innerText || '').trim();
        if (/^(展开|查看全部|查看更多|显示更多|展开全部|展开详情|查看完整)/.test(t)
            && t.indexOf('收起') === -1 && t.length <= 12) {
            try { el.click(); n++; } catch (e) {}
        }
    });
    return n;
})()
"""

# 提取: 图片 URL / 正文 / 标题
EXTRACT_JS = r"""
(() => {
    if (typeof document === 'undefined') return {error: 'no dom'};

    // ---- 图片 ----
    const urls = new Set();
    let blobCount = 0;
    // 微信小程序壳文件, 绝对不是商品图片, 直接跳过 (避免采到壳导致 404)
    const SHELL_MATCH = /(page-frame\.html|context\.html|preload-\d+|__dev__|WAPCADapterAppIndex|AppService|wxworker\/|xworker\/|index\.html$|servicewechat\.com\/(s|wx)(\.|\/))/;
    // 非图片扩展名 (JS/CSS/HTML 等), 直接拒
    const EXT_RE = /\.(js|css|html|htm|json|map|wasm|svg|ttf|woff2?|eot)(\?|#|$)/i;
    document.querySelectorAll('img,wx-image,image,[src],[data-src]').forEach(e => {
        const s = e.currentSrc || e.src || e.getAttribute('src') || e.getAttribute('data-src');
        if (!s) return;
        if (s.indexOf('blob:') === 0 || s.indexOf('data:') === 0) { blobCount++; return; }
        if (SHELL_MATCH.test(s) || EXT_RE.test(s.split('?')[0])) return;
        if (!/^https?:\/\//.test(s)) return;
        urls.add(s);
    });
    document.querySelectorAll('[style*="background"]').forEach(e => {
        const m = (e.getAttribute('style') || '').match(/url\(([^)]+)\)/);
        if (m) {
            const u = m[1].replace(/['"]/g, '').trim();
            if (/^https?:\/\//.test(u) && !SHELL_MATCH.test(u) && !EXT_RE.test(u.split('?')[0])) urls.add(u);
        }
    });

    // ---- 正文 ----
    const bodyText = (document.body && document.body.innerText || '').trim();
    const bt = bodyText.split('\n').map(s => s.trim()).filter(Boolean);

    // ---- 标题: 多层启发式 ----
    // 1. 优先 "已售" 后面那行 (允许年份开头 2026/2027 等, 只要含商品关键词)
    // 2. 其次找含衣物/商品关键词的最长行 (排除参数值/规格状态)
    // 3. 兜底: 最长行 (排除已知非标题特征)
    let title = '';
    const junk = ['退货', '服务', '保障', '发货', '评价', '爆款', '热卖', '用户', '人买',
        '门店', '现货', '预计', '品牌', '资质', '物流', '新人', '点击', '购物', '查看',
        '了解', '进入', '下载', '打开', '引导', '推荐', '客服', '收藏', '分享', '店铺',
        '首页', '购物车', '我的', '搜索', '加入购物车', '立即购买', '立即秒杀', '限时秒杀', '秒杀购买', '券后', '已售', '¥'];
    // 非标题特征: 规格层状态/尺码描述/参数值
    const nonTitle = ['已选择', '已选', '规格', '库存', '建议', '斤', 'cm', '℃',
        '适用年龄', '适用季节', '适用性别', '适用场景', '适用人群',
        '风格', '工艺', '面料', '成分', '材质', '填充物', '厚度', '弹力',
        '版型', '腰型', '裤型', '领型', '款式', '袖长', '衣长', '裤长',
        '上市年份', '货号', '产地', '重量', '尺寸', '净重', '毛重',
        '流行元素', '图案', '花型', '花色', '是否连帽', '是否外贸'];
    // 商品关键词: 衣物/品类/材质词 (出现其一就算商品名)
    const goodsKw = ['针织衫', '卫衣', '衬衫', 'POLO衫', 'polo衫', 'T恤', '体恤', '衫', '裤', '鞋', '包', '风衣', '大衣',
        '夹克', '西装', '外套', '羽绒服', '毛衣', '棉衣', '内衣', '袜', '帽', '围巾', '手套',
        '手机', '电脑', '平板', '耳机', '键盘', '鼠标', '显示器', '手表', '手环', '相机',
        '零食', '水果', '饮料', '酒', '茶', '咖啡', '奶粉', '纸', '米', '油',
        '套装', '两件套', '三件套', '组合', '礼盒', '单件',
        '男士', '女士', '儿童', '婴儿', '宝宝', '男', '女',
        '包邮', '正品', '折扣', '特价', '新款', '2025', '2026', '2027',
        '圆领', '立领', 'V领', '翻领', '亨利领', '半高领', '堆堆领',
        '长袖', '短袖', '无袖', '七分袖', '九分袖',
        '宽松', '修身', '显瘦', '直筒', '阔腿', '紧身',
        '秋冬', '春秋', '冬季', '夏季', '春季', '秋季', '四季',
        '加厚', '加绒', '保暖', '防风', '防水', '透气', '速干',
        '轻奢', '休闲', '商务', '运动', '潮流', '复古', '简约'];
    function isTitleLike(l) {
        if (l.length < 10 || l.length > 120) return false;
        if (/^\d+$/.test(l)) return false;
        // 排除纯参数值: 逗号分隔多值 + 含参数关键词
        if (l.indexOf(',') > -1 && nonTitle.some(k => l.indexOf(k) > -1)) return false;
        // 排除年龄/尺码参数值: 18-25周岁 / 90-115斤 / 160-170cm
        if (/\d+[-~]\d+\s*(斤|cm|周岁)/.test(l)) return false;
        // 排除规格层: 含 "已选择"/"已选"
        if (l.indexOf('已选择') > -1 || l.indexOf('已选') > -1) return false;
        // junk 词(服务/导航/营销文案)只对"短行或不含商品词的行"生效;
        // 长标题(>=15字)尾部常粘着「退货补运费」等服务标签, 不能因此误杀整行
        if (!(l.length >= 15 && hasGoodsKw(l))) {
            if (junk.some(k => l.indexOf(k) > -1)) return false;
        }
        return true;
    }
    function hasGoodsKw(l) {
        return goodsKw.some(k => l.indexOf(k) > -1);
    }
    // 阶段 1: "已售" 后面找 (允许年份开头)
    for (let i = 0; i < bt.length; i++) {
        if (bt[i].indexOf('已售') > -1) {
            for (let j = i + 1; j < Math.min(i + 8, bt.length); j++) {
                const l = bt[j];
                if (isTitleLike(l)) { title = l; break; }
            }
            if (title) break;
        }
    }
    // 阶段 2: 找含商品关键词的最长行 (优先级高, 更精准)
    if (!title) {
        const withKw = bt.filter(l => isTitleLike(l) && hasGoodsKw(l)).sort((a, b) => b.length - a.length);
        if (withKw.length) title = withKw[0];
    }
    // 阶段 3: 兜底最长行
    if (!title) {
        const cand = bt.filter(isTitleLike).sort((a, b) => b.length - a.length);
        if (cand.length) title = cand[0];
    }
    if (!title) title = document.title || '(未提取到标题)';

    // 剥掉标题尾部粘着的服务/营销标签(快手常把「退货补运费」「包邮」等拼在标题末尾)
    if (title && title.length > 10) {
        const suffixTags = ['退货补运费', '退货包运费', '破损包退', '极速退款', '闪电退货',
            '晚发立赔', '7天价保', '七天价保', '7天无理由', '七天无理由', '假一赔十',
            '假一赔三', '正品保障', '品质保障', '运费险', '全国包邮', '包邮',
            '闪电发货', '极速发货', '当日发货', '现货速发'];
        let _t = title, _changed = true;
        while (_changed) {
            _changed = false;
            for (const tag of suffixTags) {
                if (_t.endsWith(tag)) { _t = _t.slice(0, -tag.length); _changed = true; }
            }
            _t = _t.trim();
        }
        if (_t.length >= 4) title = _t;
    }

    // ============ 结构化商品字段 ============
    // 价格: ￥ / ¥ 开头, 数字最多 2 位小数 (取数值最大的那个, 详情页和弹层各有一个, 弹层的通常在上方)
    let price = '';
    const priceCandidates = [];
    for (const l of bt) {
        const m = l.match(/[¥￥]\s*(\d+(?:\.\d{1,2})?)/);
        if (m) priceCandidates.push(parseFloat(m[1]));
    }
    if (priceCandidates.length) {
        // 选一个"页面上最靠上的合理价格": 去重后从小到大, 取第1个
        const uniq = Array.from(new Set(priceCandidates)).sort((a,b)=>a-b);
        price = '¥' + uniq[0];
    }

    // 销量: 已售 / 已售xx件 / xx人付款 / 总销量
    let sales = '';
    for (const l of bt) {
        if (/已售|人付款|销量|人购买/.test(l)) { sales = l; break; }
    }

    // 库存: 库存:xxxx / 库存 xxxx
    let stock = '';
    for (const l of bt) {
        const m = l.match(/库存[:：\s]*\s*([0-9]+)/);
        if (m) { stock = m[1]; break; }
    }

    // 已选择: 已选择: xxxx / 已选 xxxx
    let selectedDesc = '';
    for (const l of bt) {
        if (/^已选择?[:：]?\s*/.test(l)) { selectedDesc = l; break; }
    }

    // 数量: 「数量」 行后面紧邻的数字
    let quantity = '';
    for (let i = 0; i < bt.length; i++) {
        if (bt[i].trim() === '数量' && i + 1 < bt.length) {
            const nxt = bt[i + 1].trim();
            if (/^\d+$/.test(nxt)) { quantity = nxt; break; }
            // 数量 1 同一行
        } else if (/^数量[:：\s]*\d+/.test(bt[i])) {
            const m = bt[i].match(/(\d+)/);
            if (m) { quantity = m[1]; break; }
        }
    }

    // 商品标签行: 「破损包退」「15人说质量好」「180+用户买了又买」「一周内...」
    // 特征: 短 (2-20 字), 含中文, 不是操作按钮, 也不是 导航/标题/规格
    const tagBlacklist = new Set(['立即购买','加入购物车','立即秒杀','限时秒杀','秒杀购买','进店','客服','收藏','分享','店铺',
        '首页','购物车','我的','搜索','详情','商品详情','已选择','数量','颜色','尺码',
        '库存','已售','券后','现货','价格','规格','参数','去咨询','品牌','发货','资质',
        '收货','评价','爆款','热卖','新人专享','查看详情','商品介绍','查看全部','展开']);
    const tags = [];
    const TAG_RE_STRICT = /^(破损包退|7天无理由|顺丰|包邮|假一赔|质保|闪电退|正品|极速退款|运费险)/;
    for (const l of bt) {
        const s = l.trim();
        if (s.length < 2 || s.length > 25) continue;
        if (tagBlacklist.has(s)) continue;
        if (/^[¥￥0-9.\-]+$/.test(s)) continue;
        if (/^[\d()]+建议/.test(s)) continue;          // 尺码建议行, 不是标签
        if (/^[A-Za-z0-9\-]+(\([^)]*\))?$/.test(s)) continue; // 纯色号
        if (TAG_RE_STRICT.test(s) || (s.includes('人') && (s.includes('买')||s.includes('说')||s.includes('付款')))
            || s.includes('一周内') || s.includes('回头客') || s.includes('又买')
            || s.includes('可退') || s.includes('包退') || s.includes('保障')) {
            if (!tags.includes(s)) tags.push(s);
        }
    }
    // 只保留前 10 个防止噪声
    tags.splice(10);

    // 规格 SKU: 按分组 (颜色 / 尺码 / 规格 / 版本 / 套餐 等) 识别
    // 启发: 某行文字是「颜色」, 其后连续多行为色号/短描述, 直到遇到下一个「尺码」或下一段
    const SKU_GROUP_HEAD = ['颜色','尺码','规格','版本','套餐','类别','口味','香型','款式','产地','容量','大小','尺寸'];
    // 商品详情参数表里的属性名: 这些词绝不可能是 SKU 选项 (黑/白/M/L/套餐一 才是选项)。
    // 扫描 SKU 选项时命中这些词, 说明误闯进了「商品详情」参数表, 必须停止。
    const SKU_ATTR_NAMES = new Set(['主材质','材质','面料','里料','里料材质','版型','腰型','裤腰型','裤长','裙长',
        '袖长','袖型','裤型','领型','衣长','风格','厚度','厚薄','弹力','弹性','流行元素','图案','花型','花色',
        '适用季节','适用年龄','适用性别','适用场景','上市年份季节','上市年份','上市时间','功能','裤门襟','衣门襟','门襟',
        '品牌','货号','产地','成分含量','成分','填充物','织法','工艺','服饰工艺','重量','净重','毛重',
        '服装款式细节','服装口袋样式','口袋样式','是否连帽','是否外贸','系列']);
    // 参数表/页面底部 UI 文本, 也不是选项
    const SKU_UI_STOP = new Set(['收起','展开','查看全部','查看更多','价格说明','评价','店铺','进店逛逛']);
    const specs = [];
    function readSpecGroup(startIdx, headWord) {
        const options = [];
        // 最多向后看 40 行, 或直到下一个分组头 / 数量 / 库存 / 属性名(参数表)
        for (let i = startIdx; i < Math.min(bt.length, startIdx + 40); i++) {
            const s = bt[i].trim();
            if (!s) continue;
            if (i > startIdx) {
                if (SKU_GROUP_HEAD.includes(s) || s === '数量' || /^数量[:：\s]*\d/.test(s)
                    || /库存[:：\s]*\d+/.test(s) || s.indexOf('已选') > -1
                    || s === '立即购买' || s === '加入购物车' || s === '立即秒杀' || s === '限时秒杀' || s === '秒杀购买' || s === '秒杀') break;
                // 命中商品详情属性名或 UI 文本 → 误入参数表, 停止
                if (SKU_ATTR_NAMES.has(s) || SKU_UI_STOP.has(s)) break;
                // 疑似尺码/色号/版本: 2~40 字, 非￥/纯数字, 非操作按钮
                if (s.length > 40) continue;
                if (/^[¥￥]/.test(s) || /^\d+(\.\d+)?$/.test(s)) continue;
                if (tagBlacklist.has(s)) continue;
                options.push(s);
            }
        }
        return options;
    }
    for (let i = 0; i < bt.length; i++) {
        const s = bt[i].trim();
        if (SKU_GROUP_HEAD.includes(s)) {
            const opts = readSpecGroup(i, s);
            if (opts.length >= 2) specs.push({group: s, options: opts});
        }
    }

    return {
        title: title,
        page_url: location.href,
        images: Array.from(urls),
        blob_count: blobCount,
        body_text: bodyText,
        price: price,
        sales: sales,
        stock: stock,
        selected_desc: selectedDesc,
        quantity: quantity,
        tags: tags,
        specs: specs,
    };
})()
"""


# ========================= 文本格式化 (与快手脚本一致) =========================
_PROP_HEADER_KEYWORDS = ("商品详情", "商品属性", "产品参数", "规格参数",
                        "产品介绍", "商品介绍", "参数", "属性")


def format_body_text(body_text: str) -> str:
    """把页面正文格式化成清晰可读的文本: 属性表对齐、分区空行、去连续空行。"""
    if not body_text:
        return "(空)"
    raw_lines = [l.strip() for l in body_text.split("\n")]
    lines, last_empty = [], False
    for l in raw_lines:
        if not l:
            if not last_empty:
                lines.append("")
            last_empty = True
        else:
            lines.append(l)
            last_empty = False

    formatted, i = [], 0
    in_section = False
    blacklist = ("收起", "展开", "进店", "立即购买", "加入购物车", "立即秒杀", "限时秒杀", "秒杀购买", "点击", "购物",
                 "进入", "下载", "打开", "关闭", "查看", "更多", "全部", "详情",
                 "介绍", "参数", "属性", "商品", "服务", "物流", "客服", "券后",
                 "价格说明", "讲解回放", "品牌", "爆款", "热卖", "首页", "购物车")
    while i < len(lines):
        line = lines[i]
        if any(kw in line for kw in _PROP_HEADER_KEYWORDS) and len(line) <= 20:
            if formatted and formatted[-1] != "":
                formatted.append("")
            formatted.append(f"【{line}】")
            formatted.append("")
            in_section = True
            i += 1
            continue
        if not line:
            formatted.append("")
            i += 1
            continue
        if in_section and i + 1 < len(lines) and lines[i + 1]:
            label, value = line, lines[i + 1]
            is_label = (2 <= len(label) <= 8
                        and not label.endswith(("。", "！", "？", "…", ".", "!", "?", ":"))
                        and not label.startswith(("¥", "￥"))
                        and not label.replace("/", "").isdigit()
                        and "  " not in label
                        and not any(b in label for b in blacklist))
            is_value = (1 <= len(value) <= 25
                        and not value.endswith(("。", "！", "？", "…"))
                        and not any(b in value for b in ("展开", "收起", "点击", "立即购买", "立即秒杀", "限时秒杀", "秒杀购买")))
            if is_label and is_value:
                pad = " " * max(0, 8 - len(label))
                formatted.append(f"  {label}{pad} : {value}")
                i += 2
                continue
        formatted.append(line)
        i += 1

    out, last_e = [], False
    for l in formatted:
        if not l:
            if not last_e and out:
                out.append("")
            last_e = True
        else:
            out.append(l)
            last_e = False
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


# ========================= 图片下载 =========================
def _normalize_img_url(u):
    """去掉 CDN 的实时转 webp 参数 (x-oss-process=image/format,webp), 让 CDN 返回原始 JPG/PNG。"""
    if "?" not in u:
        return u
    path, _, query = u.partition("?")
    kept_params = [p for p in query.split("&") if p and not p.lower().startswith("x-oss-process=")]
    return path + ("?" + "&".join(kept_params) if kept_params else "")


def _detect_img_ext(raw, url=""):
    """按文件魔数判断真实图片格式 (比 content-type 和 URL 后缀可靠)。"""
    if len(raw) >= 3 and raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if len(raw) >= 8 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if len(raw) >= 4 and raw[:4] == b"GIF8":
        return ".gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    low = (url or "").lower().split("?")[0]
    for e in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        if low.endswith(e):
            return ".jpg" if e == ".jpeg" else e
    return ".jpg"


def _webp_to_jpg(raw):
    """有 Pillow 时把 webp 字节转成 jpg; 没装则返回 None (原样保存 webp)。

    快手 CDN 对所有 .jpg URL 都返回 webp 字节流（HTTP 头撒谎说 image/jpeg，
    实际内容前 12 字节是 RIFF...WEBP），剥 OSS 参数也无效；
    必须装 Pillow 才能转回 jpg。无 Pillow 时给一次性提示。
    """
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(raw)).convert("RGB")
        out = BytesIO()
        im.save(out, "JPEG", quality=92)
        return out.getvalue()
    except ImportError:
        global _PILLOW_WARNED
        if not _PILLOW_WARNED:
            _PILLOW_WARNED = True
            print("\n⚠️  未安装 Pillow，无法把 webp 转 jpg，图片会保留为 .webp 格式。")
            print("   修复：pip install Pillow（或 pip install -r requirements.txt）\n")
        return None
    except Exception:
        return None


async def download_images(urls, out_dir: Path, referer=None):
    """下载图片, 文件名 图片_01.jpg ...; 返回 (成功数, 失败数)。
    只保存 jpg/png/jpeg (webp 内容尽量转 jpg)。
    """
    ok = fail = 0
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    data_blobs = []
    http_urls = []
    for u in urls:
        if u.startswith("data:image/"):
            import base64 as _b64
            _, _, payload = u.partition(",")
            try:
                raw = _b64.b64decode(payload)
                if len(raw) >= 512:
                    data_blobs.append(raw)
            except Exception:
                pass
        elif u.startswith(("http://", "https://")):
            http_urls.append(u)

    print(f"    其中 {len(data_blobs)} 张来自 blob fetch, {len(http_urls)} 张走 HTTP 下载")

    for idx, raw in enumerate(data_blobs, 1):
        try:
            ext = _detect_img_ext(raw)
            if ext == ".webp":
                conv = _webp_to_jpg(raw)
                if conv is not None:
                    raw, ext = conv, ".jpg"
            name = f"图片_{idx:02d}{ext}"
            (out_dir / name).write_bytes(raw)
            print(f"  OK   {name}  {len(raw):,} bytes  (来自 blob)")
            ok += 1
        except Exception as e:
            print(f"  失败  data:image  ({e})")
            fail += 1

    if http_urls:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
            remaining_idx = len(data_blobs) + 1
            for idx, url in enumerate(http_urls, remaining_idx):
                try:
                    url = _normalize_img_url(url)
                    r = await client.get(url)
                    r.raise_for_status()
                    ctype = r.headers.get("content-type", "")
                    if "image" not in ctype and "octet-stream" not in ctype:
                        print(f"  跳过(非图片): {url[:80]}")
                        continue
                    raw = r.content
                    ext = _detect_img_ext(raw, url)
                    if ext == ".webp":
                        conv = _webp_to_jpg(raw)
                        if conv is not None:
                            raw, ext = conv, ".jpg"
                    name = f"图片_{idx:02d}{ext}"
                    (out_dir / name).write_bytes(raw)
                    print(f"  OK   {name}  {len(raw):,} bytes")
                    ok += 1
                except Exception as e:
                    print(f"  失败  {url[:80]}  ({e})")
                    fail += 1
                await asyncio.sleep(random.uniform(0.4, 1.2))
    return ok, fail


# ========================= 图片白名单过滤 =========================
GOOD_IMG_KEYWORDS = (
    "ITEM_IMAGE",           # 商品主图
    "ITEM_IMAGE-",
    "SKU_IMAGE",            # SKU 颜色图
    "SKU_IMAGE-",
    "ITEM_DETAIL_IMAGE",    # 详情页长图
    "ITEM_DETAIL_IMAGE-",
    "RECOGNIZE_SIZE_CHART", # 尺码表
)

def _filter_product_images(urls):
    """白名单过滤: 只保留明确是商品图的 URL。"""
    kept = []
    for u in urls:
        if not u or not isinstance(u, str):
            continue
        if u.startswith("data:image/svg"):
            continue
        if u.startswith("data:image/"):
            kept.append(u)
            continue
        if not u.startswith(("http://", "https://")):
            continue
        path_lower = _normalize_img_url(u).split("?")[0].lower()
        if any(k.lower() in path_lower for k in GOOD_IMG_KEYWORDS):
            kept.append(_normalize_img_url(u))
    seen = set(); dedup = []
    for u in kept:
        if u not in seen:
            seen.add(u); dedup.append(u)
    return dedup


# ========================= 商品详情参数提取 =========================
PARAM_NAME_BLACKLIST = (
    "电商服务", "店铺", "店铺体验", "粉丝", "总销量", "好评率",
    "物流速度", "客服表现", "平均", "发货保障", "资质保障",
    "已交保证金", "现货", "预计", "承诺", "发出",
    "破损包退", "极速退款", "闪电退货", "7天无理由",
    "商品质量", "进店逛逛", "店铺名",
)

PARAM_NAME_KEYWORDS = (
    "主材质", "材质", "面料", "面料俗称", "里料", "里料材质", "版型", "型", "腰型", "裤腰型",
    "裤长", "裙长", "袖长", "袖型", "裤型", "领型", "衣长", "款式", "风格",
    "厚度", "厚薄", "弹力", "弹性", "流行元素", "图案", "花型", "花色",
    "适用季节", "适用年龄", "适用性别", "适用场景",
    "上市年份季节", "上市年份", "上市时间", "功能", "裤门襟", "衣门襟", "门襟",
    "品牌", "货号", "产地", "颜色", "尺码",
    "规格", "净重", "毛重", "重量", "尺寸",
    "成分含量", "成分", "填充物", "织法", "工艺", "服饰工艺", "系列",
    "服装款式细节", "服装口袋样式", "口袋样式", "是否连帽", "是否外贸",
)

def _extract_detail_params(body_text):
    """从 body_text 里提取【商品详情】的结构化属性, 返回 [(key, value), ...]。"""
    if not body_text:
        return []
    lines = [l.strip() for l in body_text.splitlines() if l.strip()]
    params = []
    seen_keys = set()

    start_idx = 0
    for idx, line in enumerate(lines):
        if any(h in line for h in ("商品详情", "商品参数", "规格参数", "产品参数", "商品属性")):
            start_idx = idx + 1
            break

    def is_end(line):
        return line in ("收起", "查看更多", "展开", "展开更多", "查看全部", "价格说明")

    i = start_idx
    while i < len(lines):
        line = lines[i]
        if is_end(line):
            break

        matched_keyword = None
        for kw in sorted(PARAM_NAME_KEYWORDS, key=len, reverse=True):
            if line == kw:
                matched_keyword = kw
                break
            if line.startswith(kw):
                matched_keyword = kw
                break

        if matched_keyword:
            key = matched_keyword
            if i + 1 < len(lines) and not is_end(lines[i + 1]):
                next_line = lines[i + 1]
                next_is_key = any(next_line == kw or next_line.startswith(kw) for kw in PARAM_NAME_KEYWORDS)
                if next_is_key:
                    for split_at in range(len(matched_keyword) + 1, len(line) + 1):
                        k2 = line[:split_at]
                        v2 = line[split_at:]
                        if k2 in PARAM_NAME_KEYWORDS and v2 and not any(
                                other.startswith(k2) and other != k2 for other in PARAM_NAME_KEYWORDS):
                            if k2 not in seen_keys:
                                params.append((k2, v2))
                                seen_keys.add(k2)
                            break
                    i += 1
                    continue

                value_parts = [next_line]
                j = i + 2
                while j < len(lines) and not is_end(lines[j]):
                    if any(lines[j] == kw or lines[j].startswith(kw) for kw in PARAM_NAME_KEYWORDS):
                        break
                    value_parts.append(lines[j])
                    j += 1
                if key not in seen_keys:
                    params.append((key, " ".join(value_parts)))
                    seen_keys.add(key)
                i = j
                continue
            i += 1
            continue

        if (2 <= len(line) <= 8 and
                not line.endswith(("。", "！", "？", "!", "?", ".")) and
                not line.startswith(("¥", "￥", "+", "-")) and
                not line.isdigit() and
                not any(b in line for b in PARAM_NAME_BLACKLIST) and
                any("\u4e00" <= c <= "\u9fff" for c in line)):
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                next_is_key = (2 <= len(next_line) <= 8 and
                               any("\u4e00" <= c <= "\u9fff" for c in next_line) and
                               not any(b in next_line for b in PARAM_NAME_BLACKLIST))
                if not next_is_key and line not in seen_keys:
                    params.append((line, next_line))
                    seen_keys.add(line)
                    i += 2
                    continue
        i += 1

    return params


# ========================= 网络层兜底 (抓商品 JSON 接口) =========================
# 为什么需要: 快手小店小程序部分页面使用 Skyline 渲染或延迟渲染, DOM 路线拿不到文字.
# 但是只要 CDP 代理连上, 小程序发的所有 HTTP 请求必然会走 Network 事件.
# 这里监听 Network.responseReceived (记录元信息) + loadingFinished (取 body),
# 把 content-type=json 的响应都存下来, 再按字段特征筛出"商品详情接口"的响应.

# "这响应看起来像商品详情"的关键字段组合: 命中 3 个以上就认定是商品 JSON
PRODUCT_HINT_KEYS = (
    "title", "name", "price", "sale_price", "priceInfo", "detail",
    "spec", "specs", "sku", "skus", "stock", "inventory",
    "images", "picture", "cover", "main_img", "thumb",
    "sold", "sales", "sales_count", "sellCount",
    "shop", "store", "merchant",
)
# URL 里只要出现任意一个, 就算"高度疑似商品接口"
PRODUCT_URL_HINTS = (
    "/detail", "/product", "/goods", "/sku", "/item",
    "/detailV2", "/getDetail", "/info", "/show", "/spec",
)


def _looks_like_product(data, url=""):
    """返回 (是否疑似商品详情, 命中原因简述)。"""
    if isinstance(data, dict):
        # 有时商品数据在 data.result / data.data / data.data.result 里, 递归
        for sub_key in ("result", "data", "payload"):
            if sub_key in data and isinstance(data[sub_key], (dict, list)):
                sub_hit, sub_reason = _looks_like_product(data[sub_key], url)
                if sub_hit:
                    return True, "嵌套在 .%s → %s" % (sub_key, sub_reason)
        keys_flat = set()

        def _walk(obj, depth=0):
            if depth > 5:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    keys_flat.add(str(k))
                    _walk(v, depth + 1)
            elif isinstance(obj, list):
                for it in obj:
                    _walk(it, depth + 1)
        _walk(data)
        hits = [k for k in PRODUCT_HINT_KEYS if k in keys_flat]
        if len(hits) >= 3:
            return True, "关键字段命中 %d 个 (%s)" % (len(hits), ", ".join(hits[:6]))
    if isinstance(data, list) and data:
        return _looks_like_product(data[0], url)
    url_lower = (url or "").lower()
    if any(h in url_lower for h in PRODUCT_URL_HINTS):
        return True, "URL 关键字命中"
    return False, ""


class NetworkSniffer:
    """后台消费 CDP events 队列, 抓 JSON 响应。典型用法:
        sniffer = NetworkSniffer(cdp, target_session_id)
        await sniffer.start()
        ... 做各种页面操作触发 API ...
        await sniffer.stop()
        for api in sniffer.product_apis: print(api.url, api.data_keys)
    """

    def __init__(self, cdp: CDPClient, session_id: str):
        self.cdp = cdp
        self.session_id = session_id
        self._pending = {}      # requestId -> {url, mimeType, status}
        self.all_json = []      # [(url, body_text, hit_reason or None)]
        self.product_apis = []  # [(url, data, hit_reason)] —— 疑似商品
        self._task = None
        self._stop = False

    async def _consume_loop(self):
        while not self._stop:
            try:
                ev = await asyncio.wait_for(self.cdp.events.get(), timeout=0.3)
            except asyncio.TimeoutError:
                continue
            method, params, sid = ev
            # 只关心当前 target session 的网络事件
            if sid != self.session_id:
                continue
            if method == "Network.responseReceived":
                resp = params.get("response") or {}
                url = resp.get("url") or ""
                ctype = (resp.get("mimeType") or resp.get("headers", {}).get("content-type", "") or "").lower()
                rid = params.get("requestId") or ""
                status = resp.get("status") or 0
                self._pending[rid] = {"url": url, "ctype": ctype, "status": status}
            elif method == "Network.loadingFinished":
                rid = params.get("requestId") or ""
                meta = self._pending.pop(rid, None)
                if not meta:
                    continue
                url = meta["url"]
                # 非 2xx 跳过
                if meta["status"] and not (200 <= meta["status"] < 300):
                    continue
                is_json_like = (
                    "json" in meta["ctype"]
                    or "/javascript" in meta["ctype"]
                    or url.endswith((".json", ".jsonp"))
                )
                if not is_json_like:
                    continue
                # 拉响应体
                try:
                    body_res = await self.cdp.call(
                        "Network.getResponseBody",
                        {"requestId": rid},
                        session_id=self.session_id,
                        timeout=8,
                    )
                except Exception:
                    continue
                body_text = body_res.get("body", "")
                if body_res.get("base64Encoded"):
                    try:
                        import base64
                        body_text = base64.b64decode(body_text).decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                if not body_text:
                    continue
                # 尝试 json
                parsed = None
                try:
                    parsed = json.loads(body_text)
                except Exception:
                    # 可能是 jsonp, 剥掉回调括号
                    try:
                        m2 = re.match(r"^[^(=]*\((.+)\)\s*;?\s*$", body_text.strip(), re.S)
                        if m2:
                            parsed = json.loads(m2.group(1))
                    except Exception:
                        pass
                hit, reason = (False, None)
                if parsed is not None:
                    hit, reason = _looks_like_product(parsed, url)
                    self.all_json.append((url, body_text[:10000], reason if hit else None))
                    if hit:
                        self.product_apis.append((url, parsed, reason))
                else:
                    # 存未解析成功的前 2k 字做调试
                    self.all_json.append((url, body_text[:2000], None))
            # 其他事件忽略

    async def start(self):
        # 启用 Network domain
        await self.cdp.call("Network.enable", session_id=self.session_id, timeout=5)
        self._stop = False
        self._task = asyncio.create_task(self._consume_loop())

    async def stop(self):
        self._stop = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=1.5)
            except Exception:
                self._task.cancel()
                try:
                    await self._task
                except Exception:
                    pass


def _str_clean(v):
    """把各种类型 (int/str/None/float) 安全转成去掉首尾空白的字符串"""
    if v is None:
        return ""
    s = str(v).strip()
    if s == "None":
        return ""
    return s


def _find_first_mapping(obj, keys):
    """在 dict/嵌套结构里找任一 key 的值, 返回第一个命中的 (非空)"""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] is not None:
                if isinstance(obj[k], (list, dict)) and not obj[k]:
                    continue
                if isinstance(obj[k], str) and not obj[k].strip():
                    continue
                return obj[k]
        for v in obj.values():
            found = _find_first_mapping(v, keys)
            if found is not None and found != "" and found != []:
                return found
    elif isinstance(obj, list):
        for it in obj:
            found = _find_first_mapping(it, keys)
            if found is not None and found != "" and found != []:
                return found
    return None


def _collect_strings(obj, depth=0, max_depth=6):
    """递归收集所有字符串 (用于从 JSON 生成"兜底正文文本"和图片 URL)"""
    out = []
    if depth > max_depth:
        return out
    if isinstance(obj, str):
        s = obj.strip()
        if len(s) >= 2:
            out.append(s)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_collect_strings(v, depth + 1, max_depth))
    elif isinstance(obj, (list, tuple)):
        for it in obj:
            out.extend(_collect_strings(it, depth + 1, max_depth))
    return out


def _collect_image_urls(obj, depth=0, max_depth=8):
    """递归收集所有像图片 URL 的字符串"""
    urls = set()
    if depth > max_depth:
        return list(urls)
    IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("http"):
            low = s.lower().split("?")[0]
            if any(low.endswith(ext) for ext in IMG_EXT) or "mmbiz.qpic.cn" in s \
                    or "/image/" in s or "/img/" in s or "alicdn" in s \
                    or "cdn" in s and ("pic" in s or "img" in s or "photo" in s):
                urls.add(s)
    elif isinstance(obj, dict):
        for v in obj.values():
            for u in _collect_image_urls(v, depth + 1, max_depth):
                urls.add(u)
    elif isinstance(obj, (list, tuple)):
        for it in obj:
            for u in _collect_image_urls(it, depth + 1, max_depth):
                urls.add(u)
    return list(urls)


def _extract_from_product_json(parsed_json, url=""):
    """从商品详情 API 的 JSON 响应中再提取结构化字段.
    返回一个 dict (格式和 EXTRACT_JS 返回的结构对齐, 方便 merge)。
    """
    result = {
        "title": "",
        "price": "",
        "sales": "",
        "stock": "",
        "selected": "",
        "quantity": "",
        "tags": [],
        "specs": [],
        "images": [],
        "body_text": "",
    }
    # 有时商品内容被包在 data/result/data.data 里, 解一层
    p = parsed_json
    if isinstance(p, dict):
        for k in ("data", "result", "payload"):
            if isinstance(p.get(k), dict) and len(p[k]) >= 2:
                p = p[k]
                break
            if isinstance(p.get(k), list) and p[k]:
                p = p[k][0]
                break

    title_val = _find_first_mapping(p, ("title", "goodsName", "goods_name", "name",
                                        "productName", "itemName", "subject",
                                        "item_title", "product_title", "spuName"))
    result["title"] = _str_clean(title_val)

    # 价格: 数字型 (int/float) 或字符串, 加 "¥"
    price_raw = _find_first_mapping(p, ("price", "salePrice", "sale_price", "priceInfo",
                                         "displayPrice", "minPrice", "realPrice",
                                         "nowPrice", "originPrice", "currentPrice",
                                         "priceYuan"))
    if price_raw is not None:
        if isinstance(price_raw, (int, float)):
            p_str = "%g" % price_raw
            # 快手有时候用分 (整数 * 100), 对于 > 1000 的整数加个点
            if isinstance(price_raw, int) and price_raw > 100 and price_raw < 9999999 and not result["price"]:
                # 两种候选都写下来 (让用户一眼看哪个是合理的)
                yuan = price_raw / 100.0
                if 5 <= yuan < 99999:
                    result["price"] = "¥%.2f (或 ¥%s 原始值为分)" % (yuan, p_str)
                else:
                    result["price"] = "¥%s" % p_str
            else:
                result["price"] = "¥%s" % p_str
        elif isinstance(price_raw, dict):
            # priceInfo: {minPrice:xx, maxPrice:xx}
            result["price"] = _str_clean(
                price_raw.get("minPrice")
                or price_raw.get("price")
                or next(iter(price_raw.values()), "")
            )
            if result["price"]:
                try:
                    fv = float(result["price"])
                    if 50 <= fv < 9999999:
                        result["price"] = "¥%.2f (原值可能为分)" % (fv / 100.0)
                    else:
                        result["price"] = "¥%s" % result["price"]
                except Exception:
                    result["price"] = _str_clean(result["price"])
        else:
            s = _str_clean(price_raw)
            if s and not s.startswith("¥"):
                s = "¥" + s
            result["price"] = s

    sales_val = _find_first_mapping(p, ("sales", "sold", "soldCount", "sellCount",
                                         "salesCount", "sales_count", "payCount",
                                         "orderNum", "total_sold", "favCount"))
    if sales_val is not None:
        result["sales"] = _str_clean(sales_val)
        # 纯数字加个"件/次"
        if result["sales"].isdigit():
            result["sales"] = "已售 %s 件" % result["sales"]

    stock_val = _find_first_mapping(p, ("stock", "inventory", "skuStock", "quantity",
                                         "totalStock", "remainCount", "leftCount",
                                         "availableStock"))
    if stock_val is not None:
        result["stock"] = _str_clean(stock_val)

    # 规格: 找 "规格列表" 常见结构
    #   [ {group:颜色, options:[...]} ] 或
    #   {attrs:[{name:颜色, values:[...]}]} 或
    #   skus:[{specs:[{name:颜色, value:黑色}], ...}] (这种需要重新聚合)
    attrs_raw = _find_first_mapping(p, ("specs", "spec", "attributes", "attrs",
                                         "skuInfoList", "skuAttrs", "propList"))
    group_map = {}  # groupName -> set(选项值)
    if isinstance(attrs_raw, list):
        for a in attrs_raw:
            if isinstance(a, dict):
                g = _str_clean(a.get("group") or a.get("name") or a.get("type") or a.get("attr_name"))
                vs = a.get("options") or a.get("values") or a.get("valueList") or a.get("detail")
                if not isinstance(vs, list):
                    if vs is not None:
                        vs = [vs]
                    else:
                        vs = []
                if not g:
                    continue
                if g not in group_map:
                    group_map[g] = set()
                for v in vs:
                    sv = _str_clean(v) if not isinstance(v, dict) else _str_clean(
                        v.get("name") or v.get("value") or v.get("title")
                    )
                    if sv:
                        group_map[g].add(sv)
    # sku-based: skus:[{specs:[{name,value}]}]
    skus_val = _find_first_mapping(p, ("skus", "sku_list", "skuList", "items"))
    if isinstance(skus_val, list):
        for s in skus_val:
            if not isinstance(s, dict):
                continue
            specs_in_sku = s.get("specs") or s.get("attr") or s.get("attributes") or s.get("properties") or s
            if isinstance(specs_in_sku, list):
                for sp in specs_in_sku:
                    if isinstance(sp, dict):
                        g = _str_clean(sp.get("name") or sp.get("k") or sp.get("attr_name"))
                        v = _str_clean(sp.get("value") or sp.get("v") or sp.get("value_name"))
                        if g and v:
                            if g not in group_map:
                                group_map[g] = set()
                            group_map[g].add(v)
            elif isinstance(specs_in_sku, dict):
                for k, v in specs_in_sku.items():
                    g = _str_clean(k)
                    sv = _str_clean(v) if not isinstance(v, (dict, list)) else ""
                    if g and sv and len(g) < 12:
                        if g not in group_map:
                            group_map[g] = set()
                        group_map[g].add(sv)
    for g, vset in group_map.items():
        if len(vset) >= 2:
            result["specs"].append({"group": g, "options": sorted(list(vset))})

    # 服务标签
    tag_val = _find_first_mapping(p, ("tags", "services", "serviceTags", "service_tag",
                                       "promotion_tags", "serviceIcons", "badges"))
    if isinstance(tag_val, list):
        for t in tag_val:
            if isinstance(t, dict):
                s = _str_clean(t.get("name") or t.get("text") or t.get("desc") or t.get("title"))
            else:
                s = _str_clean(t)
            if s and 1 < len(s) < 22:
                result["tags"].append(s)
                if len(result["tags"]) >= 15:
                    break

    # 图片
    img_fields = ("images", "imageList", "image_list", "imgList", "picList",
                  "img_urls", "pics", "detail_pics", "sku_img", "carousel_pics",
                  "sliderImages", "main_img", "coverImg")
    # 显式字段
    for key in img_fields:
        val = _find_first_mapping(p, (key,))
        if isinstance(val, list):
            for item in val:
                u = item if isinstance(item, str) else (
                    (item.get("url") or item.get("src") or item.get("image") if isinstance(item, dict) else "")
                )
                u = _str_clean(u)
                if u.startswith("http"):
                    if u not in result["images"]:
                        result["images"].append(u)
        elif isinstance(val, str):
            val = _str_clean(val)
            if val.startswith("http") and val not in result["images"]:
                result["images"].append(val)
    # 兜底: 递归扫描
    for u in _collect_image_urls(p):
        if u not in result["images"]:
            result["images"].append(u)
    # 限制 500 张, 避免详情长图巨量
    result["images"] = result["images"][:500]

    # 正文: 把所有能收集到的字符串(>=2字) 按出现顺序拼接 (去重保留顺序)
    seen = set()
    body_parts = []
    for s in _collect_strings(p):
        if s not in seen:
            seen.add(s)
            body_parts.append(s)
    result["body_text"] = "\n".join(body_parts[:300])

    return result


# ========================= 主流程 =========================
# 为指定 target 临时 attach → 读真实 DOM 内容（document.title + body.innerText 前 N 字）→ 关闭
PREVIEW_JS = r"""
(function(){
    try {
        const d = typeof document !== 'undefined' ? document : null;
        if (!d) return {title:'', preview:'(无 DOM, Skyline 或未加载)'};
        const title = (d.title || '').trim();
        const body = (d.body && d.body.innerText || '').trim();
        const preview = body.replace(/\s+/g, ' ').slice(0, 120);
        return {title: title, preview: preview, bodyLen: body.length};
    } catch (e) {
        return {title:'', preview: '(读取失败: '+e.message+')'};
    }
})();
"""


async def _preview_one_target(cdp, target):
    """Attach → 读 PREVIEW_JS → detach, 返回 (title, preview, body_len) 或失败信息。"""
    u = target.get("url", "")
    try:
        att = await cdp.call(
            "Target.attachToTarget",
            {"targetId": target["targetId"], "flatten": True},
            timeout=5,
        )
        sid = att["sessionId"]
        dom = await cdp.call(
            "Runtime.evaluate",
            {"expression": PREVIEW_JS, "returnByValue": True, "awaitPromise": False},
            session_id=sid,
            timeout=6,
        )
        try:
            await cdp.call("Target.detachFromTarget", {"sessionId": sid}, timeout=3)
        except Exception:
            pass
        res = (dom.get("result") or {}).get("value") or {}
        title = (res.get("title") or target.get("title") or "").strip() or "(无标题)"
        preview = (res.get("preview") or "").strip() or "(空/壳)"
        body_len = int(res.get("bodyLen") or 0)
        return (title, preview, body_len)
    except Exception as e:
        return (target.get("title") or "(读失败)",
                "(attach 失败: %s)" % str(e)[:50], 0)


def _pick_badge(u, body_len, ttype="page"):
    is_pre = "preload-" in u
    if is_pre:
        return "⚠️preload壳 "
    if ttype == "webview":
        if body_len >= 100:
            return "🟩webview "
        return "🟩webview "
    if body_len >= 500:
        return "✅候选  "
    if body_len >= 100:
        return "🟨有内容  "
    return "  ⚪空/小   "


async def pick_target(cdp: CDPClient, keyword: str, list_only: bool):
    """枚举页面目标 → 持续刷新预览 10 秒 (每 1 秒一次) → 用户按回车锁定选择。
    说明: 微信小程序 target 的 title 都是 AppIndex, URL 只有 appid 哈希,
          根本搜不到"快手"字样, 所以禁用 keyword 自动匹配。
          且真实内容页通常在小程序加载 2~3 秒后才注册新 Target,
          所以这里轮询刷新, 让用户在微信里真正进商品详情后能看到新候选出现。
    """
    await cdp.call("Target.setDiscoverTargets", {"discover": True})

    MAX_WAIT = 12   # 最多刷新 N 轮 (每轮约 1 秒)
    INTERVAL = 1.0  # 秒

    print()
    print("=" * 76)
    print("选择要采集的小程序页面")
    print("  规则: 脚本每隔 1 秒刷新一次预览列表, 最多 %d 轮。" % MAX_WAIT)
    print("  你现在可以在微信里: 打开小程序 → 点击进入商品详情页")
    print("  等列表里出现 badge=✅候选 / 预览里出现『¥价格/商品标题/已售』等文字时,")
    print("  在终端按回车进入选择, 或输入序号 + 回车直接选择。")
    print("=" * 76)

    last_seen_ids = set()
    candidates_latest = []

    for round_idx in range(1, MAX_WAIT + 1):
        result = await cdp.call("Target.getTargets", timeout=10)
        raw_pages = [t for t in result.get("targetInfos", [])
                     if t.get("type") in ("page", "webview")]
        # 只保留 servicewechat.com 开头
        current = []
        GOOD_WEBVIEW_HINTS = ("kwaixiaodian", "kwaishop", "eckwai", "ecukwai")
        for t in raw_pages:
            u = t.get("url", "")
            if "servicewechat.com" in u:
                current.append(t)
            elif t.get("type") == "webview" and any(h in u.lower() for h in GOOD_WEBVIEW_HINTS):
                current.append(t)
        current_ids = {t["targetId"] for t in current}
        new_ones = current_ids - last_seen_ids

        if not last_seen_ids or new_ones or round_idx == MAX_WAIT:
            # 做 preview (每个 candidate), 然后打印
            rows = []
            for i, t in enumerate(current):
                title, preview, body_len = await _preview_one_target(cdp, t)
                u = t.get("url", "")
                badge = _pick_badge(u, body_len, t.get("type", "page"))
                rows.append((t, title, preview, body_len, badge))
            candidates_latest = rows

            header_txt = "── 第 %d/%d 轮刷新 ── 候选 %d 个%s ──" % (
                round_idx, MAX_WAIT, len(rows),
                ("  + NEW! %d 个新 target 出现" % len(new_ones)) if new_ones else "",
            )
            print("\n" + header_txt)
            has_good = any(b.startswith("✅") for _, _, _, _, b in rows)
            if has_good:
                print("  ⭐ 本轮已发现 ✅候选 target (正文较长, 可能是真正商品页)")
            for i, (t, title, preview, body_len, badge) in enumerate(rows):
                u = t.get("url", "")
                shown_url = u[:78]
                print("[%2d] %s | title=%-28.28s | bodyLen=%-6d | %s"
                      % (i, badge, title, body_len, shown_url))
                print("      预览: " + preview)
                print()

            last_seen_ids = current_ids

            # 若有 ✅ 候选 (body>=500), 提示用户可以回车选了
            hit = [j for j, x in enumerate(rows) if x[4].startswith("✅")]
            if hit:
                print("  ✨ 检测到业务页候选 (索引 %s), 随时按回车进入选择或直接输入其序号 + 回车"
                      % ", ".join(str(j) for j in hit))

        # 最后一轮不 sleep, 直接进入选择
        if round_idx >= MAX_WAIT:
            break

        # 用一个可打断的 sleep: 如果 stdin 有输入用户想提前回车选, 这里立刻终止轮询
        try:
            await asyncio.wait_for(asyncio.sleep(INTERVAL), INTERVAL)
        except asyncio.TimeoutError:
            pass

    if not candidates_latest:
        print("[!] 没找到任何候选页面。")
        return None

    print("-" * 76)
    # 默认推荐: webview + body_len>=100 远优先 (快手商品详情在 webview target 里)
    # 否则: 非 preload 且 body_len 最大的
    best_idx = None
    best_score = -1
    for j, (t, title, preview, body_len, badge) in enumerate(candidates_latest):
        if "preload-" in t.get("url", ""):
            continue
        score = body_len
        if t.get("type") == "webview" and body_len >= 100:
            score += 10000  # webview 里的商品详情页, 加 10000 分远优先
        if score > best_score:
            best_score = score
            best_idx = j

    if best_idx is not None:
        print("  ✅ 脚本推荐: [%d] %s (bodyLen=%d, score=%d)"
              % (best_idx, candidates_latest[best_idx][4].strip(),
                 candidates_latest[best_idx][3], best_score))
        if candidates_latest[best_idx][0].get("type") == "webview":
            print("     (🟩webview 是快手商品详情所在的 target 类型, 自动优先)")
    else:
        print("  未能自动推荐 (全是 preload 壳), 请手动选择: 如果上面全是 preload 壳,")
        print("  → 很可能是 小程序实际用 Skyline 渲染, DOM 路线取不到文字, 请使用网络层兜底方案")
    raw = input("  输入序号" + (" (回车默认 %d)" % best_idx if best_idx is not None else "") + ": ").strip()
    if not raw and best_idx is not None:
        idx = best_idx
    else:
        try:
            idx = int(raw)
        except Exception:
            print("[!] 输入不是数字")
            return None
    if not (0 <= idx < len(candidates_latest)):
        print("[!] 序号超出范围")
        return None
    t = candidates_latest[idx][0]
    att = await cdp.call("Target.attachToTarget",
                         {"targetId": t["targetId"], "flatten": True}, timeout=10)
    return t["targetId"], att["sessionId"], t


async def run(args):
    url = CDP_URL_TMPL.format(port=args.port)
    print("[*] 连接 CDP 代理: %s" % url)
    try:
        ws = await websockets.connect(url, max_size=128 * 1024 * 1024, open_timeout=5)
    except Exception as e:
        print("[!] 连不上 CDP 代理: %s" % e)
        print("    请确认: ① WMPFDebugger 已启动 (npx ts-node src/index.ts)")
        print("            ② 看到 'frida script loaded' 后才打开小程序")
        print("            ③ 先跑 python probe_cdp.py 体检")
        return 1

    cdp = CDPClient(ws)
    await cdp.start()
    sniffer = None
    try:
        picked = await pick_target(cdp, args.keyword, args.list)
        if not picked:
            print("[!] 没有找到可采集的页面, 请先在微信里打开小程序。")
            return 1
        _, sid, target = picked
        title_ = target.get("title", "") or "(无标题)"
        print("[*] 已绑定页面: %s" % title_[:40])
        url_ = target.get("url", "") or ""
        print("    %s" % url_[:90])

        # ====== 启动网络嗅探 (DOM 路线失败 / Skyline 兜底) ======
        sniffer = NetworkSniffer(cdp, sid)
        try:
            await sniffer.start()
            print("[*] 已启用 Network 兜底监听")
        except Exception as e:
            sniffer = None
            print("[*] Network.enable 失败, 跳过兜底: %s" % str(e)[:60])

        print()
        print("=" * 60)
        print("请在微信里操作: 进入要采集的【商品详情页】, 等页面加载完成。")
        print("(可选) 点一下『立即购买』弹开规格, 脚本会再自动点一次以触发 API。")
        print("准备好后回到本终端, 按回车开始采集。")
        print("=" * 60)
        input()

        # 0. 展开规格弹层
        print("[*] 打开规格选择弹层 (点立即购买)...")
        sku_r = await cdp.evaluate(sid, OPEN_SKU_JS, await_promise=True, timeout=30)
        if not sku_r or not sku_r.get("opened"):
            reason = sku_r.get("reason", "unknown") if sku_r else "n/a"
            print("    未自动点击到规格按钮 (原因: %s)" % reason)
            print("    如果采不到颜色/尺码, 请在微信里手动点『立即购买』保持弹层打开再重跑")
        else:
            btn_text = sku_r.get("btnText", "立即购买")
            print("    已点击: %s (等待弹层渲染 + API 响应收集)" % btn_text)
            await asyncio.sleep(1.6)

        # 1. 滚动加载
        print("[*] 滚动页面加载全部内容...")
        r = await cdp.evaluate(sid, SCROLL_JS, await_promise=True, timeout=90)
        if r and r.get("error"):
            print("[!] 页面无 DOM (疑似 Skyline 原生渲染), 继续尝试 Network 兜底: %s" % str(r)[:60])
            # 不 return, 继续因为 Network 兜底可能已经抓到了数据
        height = r.get("height") if r else None
        print("    页面高度: %s" % height)
        if height is not None and int(height) < 200:
            print("[!] 页面几乎为空 (高度 < 200px)。")
            print("    通常原因: ① 选错了 target (选到了微信小程序的预加载壳 preload-xx/xx)")
            print("              ② 微信里还没进入商品详情页, 页面还在白屏/加载中")
            print("              ③ 小程序用 Skyline 渲染, DOM 方案不适用")
            if not sniffer or not sniffer.product_apis:
                print("    且 Network 兜底也没抓到商品 API → 先 Ctrl+C 退出重跑, 选 ✅/🟨 target")
                return 2
            print("    → Network 兜底有抓到疑似商品 API, 继续用接口数据")

        # 2. 展开折叠区
        print("[*] 展开折叠内容...")
        try:
            n = await cdp.evaluate(sid, EXPAND_JS)
        except Exception as e:
            n = 0
            print("    展开失败 (DOM 可能不可用): %s" % str(e)[:60])
        print("    点击了 %d 个展开按钮" % n)
        if n:
            await asyncio.sleep(1.0)
            try:
                await cdp.evaluate(sid, SCROLL_JS, await_promise=True, timeout=60)
            except Exception:
                pass
        await asyncio.sleep(1.2)

        # 3. DOM 提取 + sniffer 收尾
        print("[*] 提取图片与文本 (DOM 路线)...")
        data = {}
        try:
            data = await cdp.evaluate(sid, EXTRACT_JS) or {}
            if data.get("error"):
                print("    DOM 提取报错: %s" % str(data["error"])[:60])
                data = {}
        except Exception as e:
            print("    DOM 提取失败 (DOM 不可用或 session 失效): %s" % str(e)[:70])
            data = {}

        if sniffer:
            await asyncio.sleep(0.8)
            await sniffer.stop()
            print("[*] Network: %d 个 JSON / %d 个疑似商品 API"
                  % (len(sniffer.all_json), len(sniffer.product_apis)))
            for i, (u, parsed, reason) in enumerate(sniffer.product_apis[:5], 1):
                print("    · API#%d: %s\n      命中原因: %s" % (i, u[:110], reason))

        dom_title = data.get("title", "") or ""
        dom_price = data.get("price", "") or ""
        dom_sales = data.get("sales", "") or ""
        dom_stock = data.get("stock", "") or ""
        dom_sel = data.get("selected_desc", "") or ""
        dom_qty = data.get("quantity", "") or ""
        dom_tags = list(data.get("tags", []) or [])
        dom_specs = list(data.get("specs", []) or [])
        dom_images = list(data.get("images", []) or [])
        dom_body = data.get("body_text", "") or ""
        page_url = data.get("page_url", "") or target.get("url", "")

        net_fb = None
        if sniffer and sniffer.product_apis:
            net_fb = _extract_from_product_json(sniffer.product_apis[0][1], sniffer.product_apis[0][0])

        def _coa(dv, nv):
            s = _str_clean(dv)
            if s and s not in ("AppIndex", "(未提取到标题)"):
                return dv
            return nv if nv else dv

        title = _coa(dom_title, net_fb["title"] if net_fb else "") or "(未提取到标题)"
        price = _coa(dom_price, net_fb["price"] if net_fb else "")
        sales = _coa(dom_sales, net_fb["sales"] if net_fb else "")
        stock = _coa(dom_stock, net_fb["stock"] if net_fb else "")
        selected_desc = _coa(dom_sel, net_fb["selected"] if net_fb else "")
        quantity = dom_qty
        tags = list(dom_tags)
        if net_fb:
            for t in net_fb.get("tags", []):
                if t and t not in tags:
                    tags.append(t)
        specs = list(dom_specs)
        if not specs and net_fb and net_fb.get("specs"):
            specs = list(net_fb["specs"])
        images = list(dom_images)
        if net_fb:
            for u in net_fb.get("images", []):
                if u and u not in images:
                    images.append(u)
        body_text = dom_body if dom_body else (net_fb["body_text"] if net_fb else "")

        data_source = "DOM"
        if sniffer and sniffer.product_apis:
            data_source += " + Network 兜底 (%d 个 API)" % len(sniffer.product_apis)

        print("    数据来源: %s" % data_source)
        print("    标题: %s" % (str(title or "(空)")[:50]))
        if price: print("    价格: %s" % price)
        if sales: print("    销量: %s" % sales)
        if stock: print("    库存: %s" % stock)
        if selected_desc: print("    已选: %s" % selected_desc[:70])
        if quantity: print("    数量: %s" % quantity)
        if tags: print("    标签: %s" % " / ".join(tags))
        if specs:
            for s in specs:
                gn = s.get("group", "") if isinstance(s, dict) else str(s)
                opts = s.get("options", []) if isinstance(s, dict) else []
                shown = " / ".join([str(o) for o in opts[:4]])
                more = " ..." if len(opts) > 4 else ""
                print("    %s: %d 款  ·  %s%s" % (gn, len(opts), shown, more))
        img_line = "    图片: %d 个 URL" % len(images)
        bc = data.get("blob_count", 0) or 0
        if bc:
            img_line += " (另有 %d 个 blob/data 图跳过)" % bc
        print(img_line)
        print("    正文: %d 字符" % len(body_text or ""))

        has_content = bool(images or (body_text and len(body_text) >= 100) or specs or price or net_fb)
        if not has_content:
            print("[!] DOM 和 Network 都没抓到有价值的内容。")
            print("    ① 确认微信里进入了商品详情 (能看到标题/图/价格)")
            print("    ② 选 target 时选 ✅/🟨 且预览看得到文字的项")
            print("    ③ 如果全是 preload 壳, 关掉小程序重开再跑")
            return 2

        # 4. 图片白名单过滤 (去掉图标/头像/店铺素材等垃圾图)
        raw_images = images
        images = _filter_product_images(images)
        if len(raw_images) != len(images):
            filtered = len(raw_images) - len(images)
            print(f"    ✨ 图片白名单过滤: {len(raw_images)} → {len(images)} (丢弃 {filtered} 张图标/素材图)")

        # 5. 输出目录: 用商品标题命名
        folder = str(title or "")
        for ch in '\\/:*?"<>|':
            folder = folder.replace(ch, " ")
        folder = " ".join(folder.split()).rstrip(". ")
        if len(folder) > 50:
            folder = folder[:50].rstrip()
        stamp = time.strftime("%m%d_%H%M")
        if not folder:
            folder = "wx_采集_%s" % stamp
        out_dir = Path(args.out) / folder
        if out_dir.exists():
            out_dir = Path(args.out) / ("%s_%s" % (folder, stamp))
        out_dir.mkdir(parents=True, exist_ok=True)
        print("    输出目录: %s" % out_dir.name)

        # 5. 下载图片
        ok = fail = 0
        if images:
            print("[*] 下载图片 (%d 张, 每张间隔约 0.5~1.2 秒)..." % len(images))
            print("    使用 Referer: %s" % (page_url or "")[:100])
            ok, fail = await download_images(images, out_dir, referer=page_url)

        # 6. 保存商品信息
        info = []
        info.append("微信小程序商品信息")
        info.append("=" * 50)
        info.append("")
        info.append("采集页面  : %s" % (page_url or ""))
        info.append("采集时间  : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        info.append("")
        detail_params = _extract_detail_params(body_text)

        info.append("【商品标题】")
        info.append(str(title or "(未提取到标题)"))
        info.append("")
        info.append("【核心信息】")
        if price: info.append("  价格       : %s" % price)
        if sales: info.append("  销量       : %s" % sales)
        if stock: info.append("  库存       : %s 件" % stock)
        if selected_desc: info.append("  默认已选   : %s" % selected_desc)
        if quantity: info.append("  购买数量   : %s" % quantity)
        if tags:
            info.append("  服务/标签  : " + "  ·  ".join(tags))
        info.append("")
        if specs:
            info.append("【规格选项 (SKU)】")
            for s in specs:
                g = s.get("group", "") if isinstance(s, dict) else str(s)
                opts = list(s.get("options", [])) if isinstance(s, dict) else []
                info.append("  %s  (%d 款):" % (g, len(opts)))
                for _i, opt in enumerate(opts, 1):
                    info.append("    [%02d] %s" % (_i, opt))
            info.append("")
        if detail_params:
            info.append("【商品详情】")
            for k, v in detail_params:
                pad = " " * max(0, 10 - len(k))
                info.append(f"  {k}{pad}: {v}")
            info.append("")
        (out_dir / "商品信息.txt").write_text("\n".join(info), encoding="utf-8")

        print()
        print("=" * 50)
        print("采集完成!")
        print("  图片   : %d 成功 / %d 失败 (共 %d 张商品图)" % (ok, fail, len(images)))
        if specs:
            sku_summary_bits = []
            for s in specs:
                g = s.get("group", "") if isinstance(s, dict) else str(s)
                n_opts = len(s.get("options", [])) if isinstance(s, dict) else 0
                sku_summary_bits.append("%s%d款" % (g, n_opts))
            print("  SKU    : %d 个分组 (%s)" % (len(specs), ", ".join(sku_summary_bits)))
        if sniffer and sniffer.product_apis:
            print("  接口   : %d 个疑似商品 API (详见 商品信息.txt)" % len(sniffer.product_apis))
        print("  输出目录:", out_dir)
        print("=" * 50)
        return 0
    finally:
        if sniffer:
            try:
                await sniffer.stop()
            except Exception:
                pass
        await cdp.close()
        try:
            await ws.close()
        except Exception:
            pass


def main():
    # 启动诊断: 打印当前执行的脚本文件和字节数, 用来排查"改了代码但执行还是旧版"的缓存问题
    import os as _os
    _me = _os.path.abspath(__file__)
    _size = _os.path.getsize(_me)
    print("脚本路径:", _me)
    print("脚本版本: v%s" % __version__)
    print("脚本大小: %d bytes (如果和你磁盘上对不上, 就是 IDE/系统有旧缓存, 关 IDE tab 再开)" % _size)
    ap = argparse.ArgumentParser(description="微信小程序商品页图片/文本采集器")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="CDP 代理端口 (默认 62000)")
    ap.add_argument("--keyword", default="快手", help="(已废弃) 不再按关键词匹配, 改为 DOM 预览选")
    ap.add_argument("--list", action="store_true", help="(已废弃) 默认始终列出所有候选 + DOM 预览")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "output"),
                    help="输出根目录 (默认项目 output/ 目录)")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
