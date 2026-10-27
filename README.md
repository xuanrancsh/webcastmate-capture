# 抓取直播伴侣画面 · WebcastMate Capture

> 一键把 **抖音直播伴侣** 正在推流的合成画面抓到本地 —— 不开可见窗口、不装 NDI、不占用副屏。
> Capture the final composed stream canvas of 抖音直播伴侣 (Douyin Webcast Mate) without any visible window, NDI, or extra monitor.

![platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-0078D4)
![build](https://github.com/xuanrancsh/webcastmate-capture/actions/workflows/build.yml/badge.svg)
![license](https://img.shields.io/badge/license-MIT-green)

---

## 它解决什么问题

直播伴侣的「输出画面 / 投影窗口」是**所有图层合成之后、推流之前的最终画面**（含摄像头、游戏、贴纸、字幕、弹幕）。想拿它做二次处理（录屏、AI 分析、推流到别处、送虚拟摄像头），传统办法是：

- **OBS + NDI** → 要装两个软件，还得在直播伴侣里点开关；
- **窗口捕获** → 必须让那个投影窗口一直摆在桌面上，挡视线；
- **截全屏** → 拿到的是被其它窗口遮挡的画面。

本工具的做法是：**给直播伴侣主进程挂一个钩子**，让它自己开一个输出投影窗、**瞬间移到屏幕外**，然后用 Windows Graphics Capture 抓这个窗口。你在桌面上看不到任何东西，画面却是完整的合成结果。

## 功能

| | |
|---|---|
| 🎯 **自动识别画布** | 横屏 1920×1080 / 竖屏 1080×1920 / 4:3 / 21:9 全部自适应，不写死分辨率 |
| 👻 **零可见窗口** | 投影窗直接落在 `-32000,-32000`，不进任务栏、不抢焦点 |
| 🔍 **实时预览** | 内置 MJPEG 预览，随时确认抓的是不是你想要的画面 |
| 📸 **原始分辨率截图** | 一键保存 PNG 到 `图片\WebcastMate Capture` |
| ♻️ **可完全还原** | 退出/点「停止并还原」即恢复 `index.js` 原样，程序自动留 `.bak` |
| 🛡 **不破坏软件** | 注入行用 `try/catch` 包裹 —— 即使钩子文件丢失，直播伴侣照样正常启动 |

## 系统要求

- Windows 10 1809+ 或 Windows 11，x64
- 已安装 **抖音直播伴侣**（12.x 及以上）
- 管理员权限（首次运行会弹 UAC，程序需要写入直播伴侣安装目录）
- WebView2 运行时（Win11 自带；Win10 多数已通过 Edge 更新安装。缺失时程序会自动改用默认浏览器打开界面）

## 快速开始

1. 到 [Releases](../../releases/latest) 下载 `WebcastMateCapture.exe`；
2. **先打开直播伴侣**，进入直播画面（确认预览区有画面）；
3. 双击 `WebcastMateCapture.exe`，UAC 弹窗点「是」；
4. 点 **开始抓取** —— 程序会自动关闭直播伴侣 → 注入钩子 → 重新启动它 → 等画面就绪；
5. 预览区出现画面即可使用（截图 / 二次处理）；
6. 用完点 **停止并还原**，或直接关掉程序（退出时自动还原）。

> 程序关闭直播伴侣是为了换掉 `index.js`（Electron 只在启动时读一次）。所有工作都在本机完成，不联网。

## 工作原理

```
直播伴侣/直播伴侣.exe            ← 正常启动
  └─ resources/app/index.js      ← 首行被插入一行 require()（有 .bak 备份）
       └─ hook_main.js           ← 注入的钩子，运行在 Electron 主进程
            ├─ 读 GetVideoParameter → 真实画布分辨率
            ├─ Projector.Open      → 由宿主自己开一个 bOutput 输出窗
            ├─ SetPositionForProjector → 把窗口挪到屏幕外
            └─ 文件命令通道 (cmd.txt / res.txt)

WebcastMateCapture.exe
  ├─ Windows.Graphics.Capture 抓那个窗口（客户区，去掉标题栏）
  ├─ 本地 127.0.0.1 上的 HTTP + MJPEG 预览
  └─ 退出时剥离注入行 / 从 .bak 还原
```

- 钩子文件和配置存放于 `%PROGRAMDATA%\WebcastMateCapture\hook\`（**纯 ASCII 路径**，避免中文用户名导致的 `require` 失败）；
- 注入行示例：
  ```js
  try{require("C:/ProgramData/WebcastMateCapture/hook/hook_main.js")}catch(e){}; // WebcastMateCapture-hook
  ```
- 还原策略三重保险：① 删掉注入行 ② 从 `.bak` 恢复 ③ 两者都失效时提示手动处理。

## 常见问题

**Q: 提示「没有找到直播伴侣」？**
先手动打开一次直播伴侣（程序会从运行中的进程读取安装路径），或者在「设置 → 安装位置」里把 `index.js` 的完整路径粘进去。

**Q: 卡在「等待画面」？**
通常是直播伴侣还没进入直播界面、或没有添加任何画面源（此时它拿不到输出画布）。进入直播画面后点「停止」再「开始抓取」即可。

**Q: 竖屏 1080×1920 抓出来是缩小版？**
投影窗不能超过桌面工作区高度，超了就等比缩放（不会拉伸变形）。窗口里的「窗口缩放 ×0.7xx」就是这个比例。要 1:1 竖屏需要 4K 竖屏显示器。

**Q: 直播伴侣升级后失效？**
升级会覆盖 `index.js`（钩子丢失）。重新点一次「开始抓取」即可重新注入；旧 `.bak` 会被自动识别并重建。

**Q: 杀毒软件报警？**
程序做的事是「修改另一个程序的文件」，行为上和恶意软件相似，会被启发式引擎误报。代码完全公开，可自行审计或用源码构建。

## 从源码构建

```bash
git clone https://github.com/xuanrancsh/webcastmate-capture.git
cd webcastmate-capture
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean WebcastMateCapture.spec
# 产物: dist/WebcastMateCapture.exe
```

也可以直接跑源码（需要管理员终端）：

```bash
python src/main.py
```

推送 `v*` 标签会自动触发 GitHub Actions 构建并发布 Release。

## 免责声明

本项目通过修改**你自己电脑上**已安装软件的一个入口文件来实现画面捕获，仅在本机生效，不修改、不上传、不分发任何第三方软件本体。使用前请确认符合你所在地区的法律法规与软件许可协议。**风险自负** —— 建议使用前备份 `resources\app\index.js`（程序也会自动备份为 `.bak`）。

本项目与抖音 / 字节跳动无任何关联。

## License

MIT

---

## English

**WebcastMate Capture** grabs the final composited canvas of Douyin's 直播伴侣 (Webcast Mate) on Windows.

It prepends a `require()` to the app's Electron entry (`resources/app/index.js`), which asks the host to open an output projector window and instantly moves it off-screen. The tool then captures that window with Windows.Graphics.Capture — no visible window, no NDI, no extra monitor, no screen recording overlay.

- Auto-detects canvas size (landscape / portrait / 4:3 / 21:9), never stretches.
- Fully reversible: injected line is stripped or restored from `.bak` on exit.
- The injected line is wrapped in `try/catch`, so a missing hook can never break the host app.
- Requires Windows 10 1809+ x64, WebView2 runtime, and the 直播伴侣 app installed.

Download the latest `WebcastMateCapture.exe` from [Releases](../../releases/latest), run it as administrator, and hit **开始抓取 (Start)**.

> Not affiliated with Douyin or ByteDance. Use at your own risk; the tool only modifies files on your own machine and keeps a backup.
