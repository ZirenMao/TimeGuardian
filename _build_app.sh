#!/bin/bash
# _build_app.sh —— 重建 /Applications/WorkBuddy任务管家.app（macOS 窗口外壳）
# ============================================================================
# 这个 app 是「图形界面的壳子」：双击它 → 保证后台小服务在跑 → 弹一个没有地址栏的
# 窗口（Swift + WKWebView，用系统 WebKit，零第三方安装）。
#
# 为什么不是「浏览器 --app 窗口」：本机**没装** Edge / Chrome，只有 Safari，
# 而 Safari 不支持 --app=。详见 wb_ui.py 文件头与 WBShell.swift 的注释。
#
# 用法：
#   bash _build_app.sh              # 编译 + 装 app + 注册到 LaunchServices
#   bash _build_app.sh --no-icon    # 不动图标（图标单独由 _gen_icon.py 管）
#
# ⚠️ 编译需要 Xcode Command Line Tools（`xcode-select -p` 有输出即可）。
# ============================================================================
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/TimeGuardian.app"
NAME="TimeGuardian"
EXE="TimeGuardianShell"
BID="com.wb.timeguardian-mac"
VER="1.0"    # 只是**默认值**；真值在 ② 段从现有 Info.plist 继承（见那里的注释）
LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "✗ 找不到 swiftc。先装 Xcode Command Line Tools："
  echo "    xcode-select --install"
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

echo "① 编译外壳（swiftc -O）..."
# ★ 2026-10-01（第卅二轮）：多了 `-framework Carbon` —— 键映射里判断「当前输入法」
#   用的 `TISCopyCurrentKeyboardInputSource` / `kTISPropertyInputSourceID` 在 Carbon
#   （HIToolbox）里。少这个 framework，`import Carbon` 会链接失败。
# ★ 2026-10-02（第六十六轮）：又多了 `-framework IOKit` —— 修饰键映射要**按设备**生效
#   （等价 Karabiner 的 `devices[].simple_modifications`），靠
#   `CGEventCopyIOHIDEvent` + `IOHIDEventGetSenderID` + `IORegistryEntryIDMatching`
#   反查「这事件是哪把键盘发的」。少这个 framework，`import IOKit` 会链接失败。
swiftc -O -o "${WORK}/${EXE}" "${HERE}/WBShell.swift" \
  -framework Cocoa -framework WebKit -framework Carbon -framework IOKit
echo "  ✓ ${WORK}/${EXE}（$(du -h "${WORK}/${EXE}" | cut -f1)）"

echo "② 组装 app 包 ..."
# ★ 2026-10-01（第卅八轮）：版本号**从现有 plist 继承**。
#   为什么：`make_icon.py` 换图标时会把小版本号 +0.1（`bump_version()`，专门用来逼
#   系统重建图标缓存）。本脚本若写死 VER="2.0"，重编一次就把人家 bump 出来的版本号
#   打回去 —— 又一处「两处一份事实」漂了（当天实测 Info.plist 是 2.4、脚本里是 2.0）。
#   现在：有旧值就沿用，没有才用默认 2.0。
if [ -f "${APP}/Contents/Info.plist" ] && [ -x /usr/libexec/PlistBuddy ]; then
  OLDVER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
            "${APP}/Contents/Info.plist" 2>/dev/null || true)"
  if [ -n "${OLDVER}" ]; then VER="${OLDVER}"; fi
fi
echo "  · 版本号 ${VER}"
rm -rf "${APP}"
mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources"
cp "${WORK}/${EXE}" "${APP}/Contents/MacOS/${EXE}"
chmod 755 "${APP}/Contents/MacOS/${EXE}"

# 图标：优先用 _gen_icon.py 生成的那颗。
# ★★ 2026-10-01（第卅四轮）：**mac 几何必须排第一**。
#   本脚本上面有 `rm -rf "${APP}"`，每次重建都会把图标重新拷一遍 ——
#   原来这里写的是 AppIcon_win.icns 在前，于是「重建一次外壳，图标就被打回满幅」，
#   与「换完颜色图标又变回去」是同一类病（改一处、另一处没跟着改）。
#   mac 那颗＝Apple 图标网格（内容 820/1024），与 WorkBuddy 官方图标轮廓一致；
#   win 那颗只留给 Windows 端用，缺 mac 时才退而求其次。
ICON_SRC=""
for c in "${HERE}/_assets/AppIcon_mac.icns" "${HERE}/_assets/AppIcon_win.icns" "${HERE}/_assets/AppIcon_yellow.icns"; do
  [ -f "${c}" ] && ICON_SRC="${c}" && break
done
if [ -z "${ICON_SRC}" ]; then
  echo "  ! 没找到 icns（先跑一次 python3 _gen_icon.py），app 将没有自定义图标"
else
  cp "${ICON_SRC}" "${APP}/Contents/Resources/AppIcon.icns"
  chmod 644 "${APP}/Contents/Resources/AppIcon.icns"
  echo "  ✓ 图标 ← ${ICON_SRC}"
fi

# ★ CFBundleIconFile 与 CFBundleIconName **两个都要写**：只写前者在 macOS 26 上
#   图标常常不生效（参照物 /Applications/WorkDaddy.app 就是两个都写）。
cat > "${APP}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDisplayName</key>
	<string>${NAME}</string>
	<key>CFBundleName</key>
	<string>${NAME}</string>
	<key>CFBundleExecutable</key>
	<string>${EXE}</string>
	<key>CFBundleIdentifier</key>
	<string>${BID}</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleIconFile</key>
	<string>AppIcon</string>
	<key>CFBundleIconName</key>
	<string>AppIcon</string>
	<key>CFBundleShortVersionString</key>
	<string>${VER}</string>
	<key>CFBundleVersion</key>
	<string>${VER}</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>LSMinimumSystemVersion</key>
	<string>11.0</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>NSPrincipalClass</key>
	<string>NSApplication</string>
</dict>
</plist>
PLIST

plutil -lint "${APP}/Contents/Info.plist" >/dev/null && echo "  ✓ Info.plist 语法 OK"

# ★★★ 2026-10-02（第六十四轮）：把 FinderSync 右键扩展装回 PlugIns。
#   为什么必须在这里做：上面有 `rm -rf "${APP}"` —— 每次重建都会连扩展一起删掉。
#   少了这一段，用户点一次「重建外壳」右键菜单就整条消失，而且**不报任何错**
#   （表现只是右键里那几项没了，极难联想到是重建外壳导致的）。
#   产物由 `_右键扩展/_build.sh` 生成并**已签名**；本脚本只负责拷回去 ——
#   签名仍只有一条路（下面 ③·5 的 wb_appsign.py，它会把嵌套的 appex 一并封印）。
#   ⚠️ 两处必须同时存在：这里（重建外壳时装回）+ `_右键扩展/_build.sh`（改扩展时装进）。
# ★ 2026-10-10：右键扩展归 TimeGuardian。
#   为什么归它：Finder 右键菜单那几项（图片转PDF/合并PDF/PDF加密解密/文档转PDF/
#   新建 Excel/Word/PPT）就是本程序「鼠标」板块的右键助手，扩展必须跟着本程序走，
#   不能寄在任务管家包里 —— 那样一卸任务管家，右键菜单就整条没了。
#   产物由 `_右键扩展/_build.sh` 生成并**已签名**；本脚本只负责拷回来 ——
#   签名仍只有一条路（下面 ③·5 的 wb_appsign.py，它会把嵌套的 appex 一并封印）。
APPEX_SRC="${HERE}/_右键扩展/_out/WorkBuddy右键扩展.appex"
if [ -d "${APPEX_SRC}" ]; then
  mkdir -p "${APP}/Contents/PlugIns"
  /usr/bin/ditto "${APPEX_SRC}" "${APP}/Contents/PlugIns/WorkBuddy右键扩展.appex"
  echo "  ✓ 右键扩展已装入 Contents/PlugIns/"
else
  echo "  ✗ 找不到右键扩展产物：${APPEX_SRC}"
  echo "    （先跑 _右键扩展/_build.sh 生成并签名它）"
fi

# ★★★ 2026-10-02（第六十五轮）：非沙箱执行器（构建期/排障工具）同理装回 **Resources**。
#   为什么必须在这里做：同上面那条 —— `rm -rf "${APP}"` 会连它一起删掉。
#   ★ 为什么住 Resources 而不是 PlugIns：codesign 只把 PlugIns / Frameworks 当**嵌套
#     代码**（要求它先签好）；Resources 里的一律当**普通资源**密封。已用 /tmp 副本实测：
#     放 Resources 后重签 + `codesign --verify --deep --strict` 全过，嵌套那份自身签名
#     也依然有效。
#   ★ 为什么它不该继续待在 _out/ 下当个正经 .app：被 `open` 过就会进 LaunchServices /
#     Spotlight 的记录 —— 用户正是在访达里看见「WorkBuddy执行器」这个"多出来的软件"的。
#   ★ TimeGuardian-Mac：执行器是构建期/排障工具，appex 只丢任务文件不起子进程，
#     所以本程序不需要它。装回步骤留给任务管家那边。
EXEC_SRC=""
if [ -n "${EXEC_SRC}" ] && [ -d "${EXEC_SRC}" ]; then :; fi
echo "  （TimeGuardian-Mac：不需要非沙箱执行器，跳过）"

# ★★★ 2026-10-01（第卅八轮）：签名这件事**已收归 `wb_appsign.py` 一处**。
#   为什么要 ad-hoc 重签：swiftc 出来的二进制是「linker-signed」，Identifier 是二进制名
#   （WorkBuddyShell）—— 清单里只会出现一个莫名其妙的 WorkBuddyShell。用 bundle id
#   重签之后，清单里显示的是 com.wb.workbuddy-toolkit，排障时一眼能对上本工具。
#
#   ★ 而当天的真事故是另一半：`make_icon.py` 换图标时会**重写 Info.plist**（补图标键
#     + bump 版本号）**却不重签** → 签名封印里的 Info.plist 哈希对不上 →
#     `codesign --verify` 报 `invalid Info.plist` → TCC 校验不了授权 →
#     `AXIsProcessTrusted()` 恒 false。而系统设置那栏按名字列行、不校验签名，
#     开关照样是蓝的 ⇒ 用户看到「我明明授权了，怎么还说没权限」。
#   现在：装 app（本脚本）与换图标（make_icon.py）走**同一个签名入口**，且签完必须
#   自校验；校验不过就**中止** —— 装上去权限也一直被拒，那是废的。
#   ★★★ 2026-10-01（第卅九轮，用户要求「根治每次重编都掉权限」）：
#     上面那条「ad-hoc 签名下 TCC 记的是 cdhash ⇒ 重编必掉权限」**已经根治**——
#     `wb_appsign.py` 现在**优先用本地自签证书**（`wb_selfsign.py` 建的），
#     指定要求从 `cdhash H"…"` 变成 `certificate root = H"…"`，与内容无关 ⇒
#     重编 / 换图标都**不再掉权限**，用户不用再重勾。
#
#   ⚠️ 本脚本**不要再自己调 codesign**（历史上这么做过，出现了「一处改了
#      另一处没跟着改」的漂移）。签名只有一条路：`wb_appsign.py --sign`。
echo "③·5 签名 + 校验（唯一实现：wb_appsign.py；优先证书签名）..."
PY_SIGN="$(command -v python3 || true)"
if [ -z "${PY_SIGN}" ]; then
  echo "  ✗ 找不到 python3，没法签名。先装命令行工具：xcode-select --install"
  exit 1
fi
if ! "${PY_SIGN}" "${HERE}/wb_appsign.py" --sign; then
  echo "  ✗ 签名/校验失败 —— 装上去权限也会一直被拒，已中止。"
  exit 1
fi

echo "③ 注册到 LaunchServices（否则访达可能显示白底通用图标）..."
if [ -x "${LSREG}" ]; then
  "${LSREG}" -f "${APP}" || true
  if "${LSREG}" -dump 2>/dev/null | grep -q "${BID}"; then
    echo "  ✓ 已登记 ${BID}"
  else
    echo "  ! 没查到这个 bundle id 的记录 —— 换菜单 23「修复图标不显示」再试"
  fi
else
  echo "  （找不到 lsregister，跳过）"
fi

touch "${APP}"
# ★ 2026-10-01（第卅八轮）：原来这里那段「cdhash 变了 → 权限可能已失效」的提示，
#   已**搬进 `wb_appsign.py`**（`--sign` 末尾比对 `_外壳.cdhash`、变了就打印同一段话）。
#   为什么搬：换图标（`make_icon.py`）也会重签、也会掉权限 —— 两条路合用一个提示
#   才不会一处改了另一处没改（这正是当天出事的模式：改一处、另一处忘了跟）。

echo "④ 刷新访达/程序坞图标缓存 ..."
killall Finder >/dev/null 2>&1 || true
killall Dock >/dev/null 2>&1 || true
echo "✓ 完成：${APP}"
echo "  双击它即弹出界面窗口；黑窗编号菜单在图形界面里点「打开编号菜单」。"
