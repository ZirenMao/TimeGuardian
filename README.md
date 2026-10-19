# TimeGuardian（时间守护）· macOS

macOS 桌面端「防休眠 / 屏幕常亮 / 合盖不睡眠」守护工具，带图形面板、看门狗自愈与一组键盘映射能力。纯系统框架实现：后台守护与界面服务是 Python 标准库，窗口外壳是 Swift + WKWebView，零第三方 Python 依赖。

> 界面与文档均为中文。Windows 版后续发布。

## 功能

- **防休眠 + 屏幕常亮**：按档位组合电源断言（`pmset` / IOKit），面板上开关一目了然，进程在断言就在，不存在「面板说没跑、屏幕却亮着」的错位。
- **合盖不睡眠**：写 `pmset -a disablesleep`（root 操作走免弹框的 sudo 白名单，失败回退授权框）。
- **看门狗自愈**：守护按被监测方自己声明的心跳阈值判活（不写死 120 秒），需要值守而守护不在时自动补拉。
- **事件驱动的跟随启动**：LaunchAgent 只盯「被跟随方的活标记」，不做常驻轮询。
- **图形面板**：本地 HTTP 服务（`wb_ui.py`）+ 单文件界面（`_ui.html`），窗口外壳（`WBShell.swift`）用系统 WebKit 开一个无地址栏窗口，不依赖浏览器。
- **键盘映射引擎**（外壳内建）：用户态 CGEventTap，Karabiner 形状的改写规则 + 修饰键映射，配置文件热重载，支持按设备白名单。
- **音效 / 图标 / 开机自启**等桌面化细节。

## 目录结构

```
wb_powerguard.py      守护进程：电源断言、合盖控制、看门狗、心跳
wb_ui.py              界面服务：本地 HTTP + API，LaunchAgent 自启管理
_ui.html              单文件前端界面
WBShell.swift         窗口外壳（Swift + WKWebView + 键盘引擎）
wb_menu.py            菜单入口（菜单.command 调它）
wb_paths.py           环境探测与路径解析（不写死用户名/盘符）
wb_keymap.py          键盘映射配置模型
wb_quickactions.py    PDF 等右键快捷动作（需配合任务管家的守护端，见下）
wb_appsign.py         代码签名 / TCC 权限保持工具（自签证书方案）
wb_selfsign.py        自签证书与独立钥匙串生成
_build_app.sh         构建 /Applications/TimeGuardian.app 外壳
_install_shutdown_sudo.sh  安装 shutdown 免密 sudo 白名单
菜单.command           菜单入口启动器
使用说明_macOS.md      完整使用与实现说明（含踩坑记录）
右键扩展/              Finder Sync 扩展 + 执行器（源码）
_assets/              图标、音效、字体（Instrument Sans，OFL 许可）
```

## 快速开始

```bash
# 方式一：直接跑菜单（系统 Python 即可）
./菜单.command

# 方式二：构建 app 外壳（需要 Xcode Command Line Tools）
bash _build_app.sh
```

启动后面板会常驻：守护进程负责电源断言，界面服务提供本地网页面板。

### 便携运行时（可选）

工具支持「自带解释器」的便携形态：把一个 Python 放到 `_runtime/python/` 即可被 `菜单.command` 与构建脚本优先使用；没有就用系统 Python。本仓库不带任何二进制运行时。

## 已知边界

- **右键扩展 / PDF 快捷动作**是另一套「任务管家」工具的功能面：本仓库含其扩展与界面侧源码，但守护端（`pdf_daemon.py` 及队列）不在本仓库，这部分功能单独装跑不全。
- **签名与权限**：外壳经重签名后 TCC（辅助功能等）授权会失效，`wb_appsign.py` / `wb_selfsign.py` 提供「自签证书 + 独立钥匙串」方案保持授权稳定；`_自签证书/` 目录属本机私密，不入库，重建时需自备证书放回该目录。
- `pmset` 相关操作需要管理员授权，工具会明确提示，不会静默提权。

## 隐私说明

本仓库只含源码与静态资源，不含任何配置、日志、状态文件、证书或个人信息；首次运行时这些文件在本机自动生成。

## License

[MIT](LICENSE)
