# wxminiapp-scraper

> PC 微信小程序商品信息采集工具（CDP 方案）
>
> Version: **v1.0.4** ｜ License: **非商业学习许可**（仅限个人学习研究，禁止商业用途）
>
> 更新日志见 [CHANGELOG.md](CHANGELOG.md)。

通过 PC 微信内置的远程调试通道（Chrome DevTools Protocol），像操作网页一样采集微信小程序中展示的商品信息。

**已适配目标**：微信内 WebView 渲染的商品详情页（已在快手小店小程序验证）。其他使用 WebView 渲染商品页的小程序理论上同样可用。

---

## ⚠️ 免责声明 / 合规提示（使用前必读）

1. 本工具**仅供个人学习、技术研究**使用，禁止用于任何商业用途（出售、付费代采集、为企业/商家提供采集服务等），禁止大规模、高频爬取。
2. 本工具通过 Frida 注入解锁**你本人电脑上**微信的调试通道，仅读取你本人账号有权访问的公开商品页面。注入只在内存中进行，重启微信即完全还原，不修改微信文件。
3. 自动化采集可能违反相关平台用户协议，存在账号风控风险（功能限制、封号等），**风险由使用者自行承担**。建议使用微信小号、控制采集频率。
4. 请遵守《网络安全法》《数据安全法》《个人信息保护法》等法律法规，不得采集他人隐私或非公开数据。
5. 作者不对使用本工具造成的任何直接或间接损失负责。详见 [LICENSE](LICENSE)。

---

## 功能

- 🖼️ 商品图片（主图 / SKU 图 / 详情长图 / 尺码表，自动过滤图标头像等素材图，自动转 JPG）
- 📝 商品标题、价格、销量、服务标签
- 🎨 SKU 规格（颜色、尺码等分组选项，自动点开规格弹层提取）
- 📋 商品详情参数表（材质、版型、品牌、适用季节…… 结构化键值对）

输出为 `output/` 下以**商品标题命名**的文件夹，内含按序编号的图片和 `商品信息.txt`。

## 工作原理

```
PC 微信 (WeChatAppEx.exe, Chromium 内核)
        │  小程序运行在 WMPF 中, 内置远程调试但被限制
        ▼
WMPFDebugger (Frida 注入, 开源第三方工具, 位于 third_party/)
        │  解锁调试限制, 本机暴露标准 CDP 端口
        ▼
ws://127.0.0.1:62000  (Chrome DevTools Protocol)
        │
        ▼
wx_scraper.py (Python)
        │  直连 CDP → 选中 webview 目标(商品详情H5)
        │  滚动加载 → 展开折叠 → 提取 DOM → 白名单过滤 → 下载图片
        ▼
output/<商品标题>/ 图片_01.jpg ... 商品信息.txt
```

## 目录结构

```
wxminiapp-scraper/
├── 开始采集.py              # ★ 一键采集入口（推荐）
├── wx_scraper.py           # 采集器核心
├── probe_cdp.py            # 环境体检脚本（5 项检查）
├── requirements.txt        # Python 依赖（httpx + websockets）
├── docs/
│   └── 使用说明.txt         # 详细教程（与本 README 内容同步）
├── third_party/
│   └── WMPFDebugger/       # 第三方调试注入工具（Node/TS 项目，见文末致谢）
│       ├── frida/hook.js   #   场景码白名单（已适配）
│       └── src/            #   注: node_modules 需自行 yarn 安装
├── tools/                  # WMPF 版本适配工具（微信升级后重新适配用）
│   ├── wmpf_adapt.py       #   自动扫描新版本生成地址配置
│   ├── wmpf_dump.py
│   ├── diag_hook.js / diag_agent.js
│   └── requirements.txt    #   capstone + pefile
└── output/                 # 采集结果输出目录（gitignore，不入库）
```

## 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Windows | 10/11 | 仅支持 Windows |
| PC 微信 | 4.x 最新版 | [下载](https://pc.weixin.qq.com/)，必须是 PC 版 |
| Python | 3.10+ | `python --version` 验证 |
| Node.js | LTS v22 | [下载](https://nodejs.org/)，`node --version` 验证 |
| yarn | 任意新版 | `npm install -g yarn` |

## 快速开始

以下命令均在**项目根目录**执行。

### 首次：装依赖（每台电脑一次）

```powershell
pip install -r requirements.txt
cd third_party/WMPFDebugger
yarn
cd ..\..
```

> 国内 pip 慢可先换清华源：
> `pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`

### 方式一：一键采集（推荐）

```powershell
python 开始采集.py
```

脚本自动完成 4 步：① 检测微信 → ② 新窗口启动注入（自动等 `frida script loaded`）→ ③ 轮询等待商品页 → ④ 自动调起采集器。按提示在微信里打开商品详情页即可。

### 方式二：手动分步（排障时用）

```powershell
# 窗口 A：启动注入（保持窗口不关）
cd third_party/WMPFDebugger
npx ts-node src/index.ts --debug-frida --debug-main

# 看到 "[frida] you can now open any miniapps" 后, 在微信里打开小程序

# 窗口 B：体检（首次必做）
python probe_cdp.py

# 窗口 B：采集
python wx_scraper.py
```

### 打开小程序的入口（重要）

- ✅ **安全**：聊天里点开分享的小程序卡片（含群聊聊天记录转发）、微信顶部搜索进入、下拉「最近使用」进入
- ❌ **会崩**：「发现页 → 小程序」入口（scene 1000 会导致小程序进程崩溃，上游已知问题）

注入窗口出现 `[hook] hook scene condition -> 1101` 且 `[miniapp] miniapp client connected` 即表示成功。

## 采集结果

```
output/
└── <商品标题>/
    ├── 图片_01.jpg          # 商品图按序编号（自动转 JPG）
    ├── 图片_02.jpg
    └── 商品信息.txt
```

`商品信息.txt` 包含：采集页面/时间、商品标题、核心信息（价格/销量/标签）、**规格选项 SKU（颜色/尺码）**、商品详情参数表。

## 常见问题

| 现象 | 解决 |
|---|---|
| `npx` / `yarn` 不是命令 | 没装 Node.js / yarn，见环境要求 |
| 看不到 `frida script loaded` | 微信未登录、版本太旧，或杀软拦截 Frida（加白名单） |
| 注入成功但连不上 | 顺序反了：必须先看到 loaded 再打开小程序；重启注入重开小程序 |
| 小程序反复闪退 | 用了发现页入口（scene 1000），换聊天卡片/搜索入口 |
| `version config not found: xxxxx` | 微信自动升级了运行时，用 `tools/wmpf_adapt.py` 重新适配（见 docs/使用说明.txt） |
| 采不到颜色/尺码 | 脚本会自动点「立即购买」弹开规格层；如仍失败，手动点开规格弹层再回车采集 |

更多细节见 [docs/使用说明.txt](docs/使用说明.txt)。

## 第三方致谢

- **[WMPFDebugger](https://github.com/evi0s/WMPFDebugger)**（evi0s）— 本工具的调试注入基础，代码位于 `third_party/WMPFDebugger/`，遵循其自带许可协议；本项目对 `frida/hook.js` 场景白名单做了适配修改
- **[Frida](https://frida.re/)** — 动态插桩框架

## 许可证

本项目采用 **非商业学习许可协议** —— 个人学习研究免费，**禁止商业用途与大规模爬取**，转载/二次分发需保留版权声明。完整条款见 [LICENSE](LICENSE)。

Copyright (c) 2026 Stranges77

## 仓库地址

- GitHub：<https://github.com/Stranges77/wxminiapp-scraper>
- Gitee（国内镜像）：<https://gitee.com/strange77/wxminiapp-scraper>
