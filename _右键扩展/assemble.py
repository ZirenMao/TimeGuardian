#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组装两样东西 —— 都**不再产出独立的 /Applications 宿主 app**：

  ① FinderSync 扩展  →  _out/WorkBuddy右键扩展.appex
     由 `_build.sh` 装进 `/Applications/WorkBuddy任务管家.app/Contents/PlugIns/`。
     ★ 为什么必须寄生：macOS 强制 FinderSync 扩展住在一个 .app 的 PlugIns 里
       （`.workflow` 快速操作到不了右键顶层，扩展才行）。既然任务管家也是我们的
       app，就寄到它里面 —— /Applications 里不再多一个图标。

  ② 非沙箱命令执行器  →  _out/.bin/WorkBuddy执行器.app   （构建产物，隐藏目录）
     正式驻地由 `_build.sh` 拷进：/Applications/WorkBuddy任务管家.app/Contents/Resources/
     ★★★ 2026-10-02（第六十五轮 · 用户：「为什么又多了一个名叫执行器的软件？
         不是应该整合到任务管家一个软件里面吗？」）—— 它原来直接躺在 `_out/` 下，
         是个**货真价实的 .app**：被 `open` 过之后 `lsappinfo` 能列到它
         （每次 open 都进一遍系统的"应用"名单）、Spotlight / 访达搜索也搜得到。
         用户看到的正是这个。两处一起改：
           · 构建产物藏进**隐藏目录** `_out/.bin/`（`ls` 看不见、Spotlight 不索引）；
           · **正式驻地搬进任务管家自己的包内**（Contents/Resources/）。
         ⇒ 系统里"看得见的应用"永远只有「WorkBuddy任务管家」一个。
     ★ 为什么仍然是个独立进程，而不是把代码并进外壳：
         `_build.sh` 每轮都要重签宿主，而重签前必须 `pkill -f WorkBuddyShell` ——
         活儿要是挂在宿主进程里，这一刀就把干活的人自己砍了（后面的命令全丢）。
         独立进程 + 住在宿主包内 ⇒ 既隐身、又不挨刀。
     ★ 为什么放 `Contents/Resources/` 而不是 `Contents/PlugIns/`：
         codesign 只把 PlugIns / Frameworks 当**嵌套代码**（要求它先签好）；
         Resources 里的一律当**普通资源**密封。已用 /tmp 副本实测：
         放 Resources 后重签 + `codesign --verify --deep --strict` 全部通过，
         嵌套那份 app 自身的签名也依然有效。
     由 `open` 拉起 ⇒ APP_SANDBOX_CONTAINER_ID=<none>，工具沙箱里被拒的
     `pluginkit` / `log show` / `killall` / `ps` 都能在里面跑。

★ 两个已定论的坑（拿机器上 5 个在跑的同类扩展逐符号对照出来的，改回去必挂）：
  ① 主类必须用 **clang 编 ObjC**：swiftc 会把 `_OBJC_CLASS_$_X` 降级成 local
     符号（`nm` 小写 s）⇒ PluginKit 找不到主类 ⇒ 进程起得来但 `init()` 永不
     调用、且不报任何错。
  ② 入口必须调 **NSExtensionMain**（不是 NSApplicationMain）—— 只挂
     NSApplicationMain 时 main() 能跑，但系统永不来实例化主类，同样静默。

用法：python3 assemble.py <二进制目录> [输出目录]
"""
import os
import plistlib
import shutil
import sys

BIN = sys.argv[1] if len(sys.argv) > 1 else '/tmp/wbqa_build'
OUT = (sys.argv[2] if len(sys.argv) > 2
       else os.path.join(os.path.dirname(os.path.abspath(__file__)), '_out'))

APPEX_BID = 'com.wb.wbqa.rightclick.ext'
APPEX_NAME = 'WorkBuddy右键扩展.appex'
APPEX_EXE = 'WBRCFinderSync'
APPEX_CLASS = 'WBRCFinderSync'

EXEC_BID = 'com.wb.workbuddy-toolkit.exec'
EXEC_NAME = 'WorkBuddy执行器.app'
EXEC_EXE = 'WorkBuddy执行器'


def wr(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        plistlib.dump(obj, f)


def fresh(p):
    if os.path.isdir(p):
        shutil.rmtree(p)
    os.makedirs(p)


# ── ① FinderSync 扩展 bundle ────────────────────────────────────────────────
appex = os.path.join(OUT, APPEX_NAME)
fresh(appex)
os.makedirs(os.path.join(appex, 'Contents', 'MacOS'))
shutil.copy2(os.path.join(BIN, APPEX_EXE),
             os.path.join(appex, 'Contents', 'MacOS', APPEX_EXE))
wr(os.path.join(appex, 'Contents', 'Info.plist'), {
    'CFBundleIdentifier': APPEX_BID,
    'CFBundleExecutable': APPEX_EXE,
    'CFBundleName': 'WorkBuddy右键扩展',
    'CFBundleDisplayName': 'WorkBuddy右键扩展',
    'CFBundlePackageType': 'XPC!',
    'CFBundleSupportedPlatforms': ['MacOSX'],
    'CFBundleDevelopmentRegion': 'en',
    'CFBundleShortVersionString': '1.0',
    'CFBundleVersion': '1',
    'CFBundleInfoDictionaryVersion': '6.0',
    'LSMinimumSystemVersion': '11.0',
    'LSUIElement': True,
    'NSPrincipalClass': 'NSApplication',
    'NSExtension': {
        'NSExtensionAttributes': {},
        'NSExtensionPointIdentifier': 'com.apple.FinderSync',
        'NSExtensionPrincipalClass': APPEX_CLASS,
    },
})
print('✓ 扩展     ：%s' % appex)

# ── ② 非沙箱执行器 app（构建产物，藏在 _out/.bin/；正式驻地在宿主包内） ───────
ex = os.path.join(OUT, '.bin', EXEC_NAME)
fresh(ex)
os.makedirs(os.path.join(ex, 'Contents', 'MacOS'))
shutil.copy2(os.path.join(BIN, 'Host'),
             os.path.join(ex, 'Contents', 'MacOS', EXEC_EXE))
wr(os.path.join(ex, 'Contents', 'Info.plist'), {
    'CFBundleIdentifier': EXEC_BID,
    'CFBundleExecutable': EXEC_EXE,
    'CFBundleName': 'WorkBuddy执行器',
    'CFBundleDisplayName': 'WorkBuddy执行器',
    'CFBundlePackageType': 'APPL',
    'CFBundleShortVersionString': '1.0',
    'CFBundleVersion': '1',
    'CFBundleInfoDictionaryVersion': '6.0',
    'LSMinimumSystemVersion': '11.0',
    'NSPrincipalClass': 'NSApplication',
    'LSUIElement': True,
})
print('✓ 执行器   ：%s' % ex)

# ── 顺手清掉上一版的**可见**位置（`_out/WorkBuddy执行器.app`） ────────────────
# ★ 它是个正经 .app ⇒ 被 open 过就会进 LaunchServices / Spotlight 的记录，
#   用户正是在那儿看见「WorkBuddy执行器」的。目录只要还在，早晚还会被搜到 ⇒ 删。
stale = os.path.join(OUT, EXEC_NAME)
if os.path.isdir(stale):
    shutil.rmtree(stale)
    print('✓ 已清掉旧位置：%s' % stale)
