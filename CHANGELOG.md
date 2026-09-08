# 更新日志（Changelog）

本项目所有重要变更都会记录在本文件中。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，
版本号遵循 [语义化版本 SemVer](https://semver.org/lang/zh-CN/)（修 bug → PATCH，加功能 → MINOR，大改版 → MAJOR）。

分类说明：**新增** 功能 / **变更** 现有功能 / **弃用** 即将移除 / **移除** 已删除 / **修复** Bug / **安全** 漏洞修复。

> 维护方式：每次迭代/发布时，在最上方新增一个 `## [v版本号] - YYYY-MM-DD` 区块，
> 把本次变更按分类列在下面；最新版本始终排在最上面。

## [v1.0.3] - 2026-09-08

### 修复

- 修复采集图片主图保留为 webp 格式无法正常打开的问题。根因是快手 CDN 对所有 `.jpg` URL 都返回 webp 字节流（HTTP 头 `content-type` 撒谎说 `image/jpeg`，但实际内容前 12 字节是 `RIFF...WEBP`），剥 OSS 参数、改 Accept/Referer 都无效，唯一可行方案是下载后用 Pillow 转换。本次修复：① `requirements.txt` 新增 `Pillow>=10.0.0` 依赖；② `_webp_to_jpg` 函数在 Pillow 未安装时给一次性友好提示，不再静默失败；③ 新增 `_PILLOW_WARNED` 全局标志位避免重复提示。已实测转换工作正常：47KB webp → 130KB jpg。（`wx_scraper.py`、`requirements.txt`）

## [v1.0.2] - 2026-09-08

### 变更

- 规格弹层按钮识别：SKU 点击正则新增「立即秒杀/限时秒杀/秒杀购买」，秒杀商品现在能正确点到底部秒杀按钮弹出规格层并提取颜色尺码；同时避免误点价格标签"秒杀价"。（`wx_scraper.py`）
- 商品标题提取全面重写（三层启发式 + 商品关键词词典 + 非标题特征排除），修复两个致命 bug：① 年份开头的标题（如 "2026春秋季..."）被旧规则的数字开头检查挡掉；② 最长行兜底被"已选择规格状态"/尺码描述（含体重/身高）/参数值逗号多值抢占。（`wx_scraper.py`）

### 修复

- Python 侧 `format_body_text` 黑名单和值过滤规则同步加入秒杀类按钮文字，避免秒杀按钮被当成正文内容。（`wx_scraper.py`）

## [v1.0.1] - 2026-09-07

### 修复

- 修复从微信「聊天记录」聚合页（群聊/单聊的历史消息里）点开小程序卡片时无法采集的问题：Frida 场景码白名单新增 `1096`，该入口的小程序现在能正常进入调试模式。（`third_party/WMPFDebugger/frida/hook.js`）
- 修复商品详情参数表两列网格中相邻属性被粘连到同一行的问题（例如「货号」的值里混入「衣门襟」、「成分含量」混入「服饰工艺」、「腰型」混入「里料」），现在各属性独立成行。（`wx_scraper.py`）

## [v1.0.0] - 2026-09-07

### 新增

- 首个正式版本：通过 PC 微信内置远程调试通道（CDP）采集微信小程序商品页的图片、文本与规格信息。
- 一键入口 `开始采集.py`：检测微信 → 自动注入调试 → 等待商品页 → 自动调起采集器，全程持久 WebSocket 连接。
- 商品页自动识别：枚举 CDP 目标并按特征打分，WebView 商品页自动加权推荐，支持回车默认选择。
- 图片采集：白名单过滤图标/无关图片、自动剥离 CDN 的 webp 实时转码参数、webp 自动检测并转为 JPG。
- 规格（SKU）抓取：自动点击「立即购买」弹起规格层，提取颜色/尺码等分组及选项，含三重防护避免把参数表误判为规格。
- 商品详情参数结构化提取（主材质/版型/厚度/品牌/货号等键值对）。
- 输出目录以商品标题命名（重名自动追加时间戳），图片与 `商品信息.txt` 统一输出到 `output/`。
- 体检脚本 `probe_cdp.py`：5 项检查（WMPF 版本、CDP 端口、握手枚举等），首次使用或排查问题时运行。
- 已适配入口：群聊转发、搜索、最近使用等场景（发现页 scene 1000 会导致进程崩溃，已排除）。
- 合规：自定义《非商业学习许可协议》、代码版权头、README 与使用说明中的免责声明。

[v1.0.3]: https://github.com/Stranges77/wxminiapp-scraper/compare/v1.0.2...v1.0.3
[v1.0.2]: https://github.com/Stranges77/wxminiapp-scraper/compare/v1.0.1...v1.0.2
[v1.0.1]: https://github.com/Stranges77/wxminiapp-scraper/compare/v1.0.0...v1.0.1
[v1.0.0]: https://github.com/Stranges77/wxminiapp-scraper/releases/tag/v1.0.0
