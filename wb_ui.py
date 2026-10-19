#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 任务管家 · 图形界面（点击操作，不用记编号）
================================================================
双击「界面.vbs」（或桌面「WorkBuddy 任务管家」快捷方式）弹出本界面；
「界面.bat」是同一件事的排障入口 —— 它会把错误明明白白打在黑窗里，但会闪一下黑窗。
（为什么不用 .bat 当门面，见说明书第十章第 5 条。）

设计原则
  · **不复制任何业务逻辑**：菜单结构 / 状态摘要 / 要跑的命令，全部从同目录的
    wb_menu.py 读 —— 保证「界面点一下」与「终端按编号」永远得到同一个结果，
    以后加编号只改 wb_menu.py 一处，界面自动跟着变。
    ★ 读的是**磁盘上的当前版本**：服务按 wb_menu.py 的 mtime 判断，改过就
    importlib.reload 一次（见 menu_module）。所以改完菜单**刷新页面即生效**，
    不需要点「关闭服务」重开。
  · 纯标准库，零第三方依赖（本机托管 Python 没有 tkinter，所以不走原生窗口）。
  · 窗口是一个独立的浏览器「应用窗口」（没有地址栏，看着就是个独立小软件）：
      Windows —— msedge.exe / chrome.exe 的 `--app=` 模式；
      macOS   —— **本机没有装 Edge / Chrome**，所以改用自带的外壳 app
                 「/Applications/WorkBuddy任务管家.app」（Swift + WKWebView，
                 用系统 WebKit，零安装）。见 _open_shell_app()。
    都没有时退回系统默认浏览器。

运行方式
    python wb_ui.py            起服务 + 打开窗口（已在跑则只开窗口）
    python wb_ui.py --launch   同上，但**立刻返回**（给入口脚本用，见 launch）
    python wb_ui.py --ensure   只保证服务在跑（不起窗口），给外壳 app 调用
    python wb_ui.py --serve    只起服务，不开窗口
    python wb_ui.py --port N   换端口（默认 49631）

⚠️ 本文件常在无控制台的解释器下运行（Windows 的 pythonw / macOS 的
   `--serve` 后台进程），此时 sys.stdout 可能是 None：
   所以**一律不要 print**，所有诊断信息写 _界面.log。
"""
import os
import sys
import io
import json
import time
import socket
import threading
import subprocess
import tempfile
import webbrowser
import platform

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

HOST = '127.0.0.1'
DEFAULT_PORT = 49632
PAGE_FILE = os.path.join(HERE, '_ui.html')
LOG_FILE = os.path.join(HERE, '_界面.log')
CONF_FILE = os.path.join(HERE, '_界面.json')

# ★ 界面窗口的图标（2026-09-26 加，同日二改）——就是桌面快捷方式用的那个黄色 .ico。
#
# 为什么需要它：本界面是「Edge --app= 起一个应用窗口」跑起来的。这种窗口的
# 标题栏与**任务栏**图标取的是**页面 favicon**；页面没声明 favicon 时，Edge
# 就退回它自带的「地球」占位图 —— 用户看到任务栏挂着一个地球，与桌面快捷
# 方式的黄色图标对不上，就是这个原因。
# 所以这里把同一个 .ico 由本服务在 /favicon.ico 上喂给页面，
# 再由 _ui.html 的 <link rel="icon"> 指过来，两处图标从此同一张脸。
#
# ★ 但这还不够（2026-09-26 深夜实测）：**favicon 这条路只给到 16×16**。
#   受控实验（同一张 .ico 喂进去、再把窗口图标抠回来逐像素比，30px 采样）：
#       刚开窗口        与「ico 第16档放大」几乎一致   MAD 6.6
#       补一次 WM_SETICON 后  与「ico 第48档缩小」几乎一致   MAD 3.0
#   → 任务栏那个 ~30px 的图标，本质是 16px 被拉大 = 糊。所以窗口起来之后
#     还要由 _apply_window_icon() 补一次 WM_SETICON，换成 .ico 里的高清档。
#   实测：只发 WM_SETICON 就够；改窗口类图标（SetClassLongPtrW）与抖动
#   WS_EX_TOOLWINDOW 都是多余的（数字一模一样），不要做——前者会连累同进程
#   的其它 Edge 窗口。
ICON_FILE = os.path.join(HERE, 'TimeGuardian.ico')

# ★ 本进程启动那一刻，wb_ui.py 自己的 mtime（2026-09-26 加）。
#
# 为什么需要：服务是**常驻进程**，而「关窗口 ≠ 关服务」—— 用户改完代码后关掉窗口
# 再双击，--launch 一看端口还被占着，就只是把旧窗口叫出来，**新代码永远不生效**。
# 用户为此白跑过一趟（改了任务栏图标，重启后没变化，一查服务还在跑旧代码）。
#
# 所以这里记下启动时刻，前端把它和磁盘上的当前 mtime 一比，就能一眼看出
# 「该点多出来的『关闭服务』了」，不必再靠猜。
try:
    BOOT_MTIME = os.path.getmtime(os.path.abspath(__file__))
except OSError:
    BOOT_MTIME = None

IS_WIN = (os.name == 'nt')
IS_MAC = (sys.platform == 'darwin')

# macOS 的窗口外壳：本机没装 Edge / Chrome（只有 Safari，而 Safari 不支持
# `--app=`），所以窗口不用浏览器，改用一个自带的小 app —— 它是一个
# Swift + WKWebView 的壳，只做三件事：保证后台服务在跑、开一个没地址栏的
# 窗口、把窗口尺寸记进 _界面.json。用系统 WebKit，零第三方安装。
#   ★ 图标就来自这个 app 自己的 bundle，所以 Dock / ⌘-Tab 里和 Windows 端同一张脸。
SHELL_APP_NAME = 'WorkBuddy任务管家'
SHELL_APP = os.path.join('/Applications', SHELL_APP_NAME + '.app')

# 这些项必须在**独立终端窗口**里跑 —— 它们自带交互（子面板 / 要你输入 / 跑很久）。
# 其余项跑完就结束，输出直接内嵌到界面里显示。
#   Windows 端：{'5','8','X','11'}（Win 的编号体系没动）
#   ★ macOS 端（2026-09-29 第三次重排后逐条重算）：判据仍只有一个 ——
#     **「它会不会问你要输入」**，会就放进来。逐条注明原因：
#       macos1  更多工具 · 菜单命令是 `wb_sync_ctl.py --panel`（带循环的子界面）
#               —— 黑窗菜单敲它要开终端才对；图形界面里另走「视图」不弹终端。
#       macos2  合并两条会话 —— 菜单里是 `--ask`（先列清单再挑两条）
#               —— 同上，图形界面走「合并两条会话」视图。
#       power5  运行监测 · 跑完自动关机 —— `--shutdown` 不带值时是交互面板，要问 y/N
#       power6  运行监测 · 到点自动结束 —— `--autostop` 不带值时要在里面挑一个时长
#       space1  空间回收 —— 它自己就是「按编号挑着清」的交互界面
#     ★ 为什么必须放进来：这些脚本的 input() 都写了 `except EOFError: ans = ''`，
#       意思是 EOF 时**默认「否」**。内嵌执行没有 stdin，于是卡片点下去
#       会被静默取消 —— 用户看到的是「点了这个清理，它说没执行」。
#     ★★ 2026-09-29 又挪走一项：`f`（删除共享文件）**已从本表移到 UI_EXTRA_ARGS**，
#       配 `--yes` 让它在内嵌路线上不再问 —— 因为用户这一轮的要求是
#       「尽量都不用终端界面了，点击按钮自动打开右边的终端界面也不要了」。
#       判据没变，只是「这条命令还会不会问你要输入」的答案从「会」变成了「不会」
#       （命令末多了 --yes）。改这一项要**同时**改 UI_EXTRA_ARGS，否则它就变成
#       「点了没反应」—— `_chk_buttons.py` 会把这种搭配直接报出来。
#     ★ 反例（**刻意不放**）：
#       power1~power4（`--toggle …`）与 power7（`--stop`）全程不问输入；
#       local3（执行全局单份）虽然会改数据，但脚本里一个 input() 都没有
#         —— 它走界面的「会改数据」确认弹窗（needConfirm）；
#       cross2（恢复被误删的会话）：菜单命令是只读的 `--list`，图形界面走视图；
#       界面上的「跑完自动关机」也不走 power5：它走 key=shutdown_on/off 那条
#         非交互专用路（见 do_run）—— power5 只是**另一个入口**（黑窗菜单也用它）。
#     ⚠️ macos1 / macos2 / space1 这三项**图形界面里压根不会走到这里** ——
#       它们在 VIEW_KEYS 里，do_run 一开始就拦下来开视图了。留在本表是因为
#       它们对应的**菜单命令**确实是交互式的（黑窗菜单敲它就该开终端），
#       而且 build_menu 的 mode 会优先判 VIEW_KEYS（见那里的写法）。
#   ⚠️ 键的写法必须与 wb_menu.py 的唯一键**逐字一致**（板块前缀 + 板块内序号）。
#   ★★ 2026-09-29 键名变更（同一次重排）：power1→已取消、power2~5→power1~4、
#      power6→power5、power7→power6、power8→power7、cross3/cross4 合并、
#      原 macos3~8 → 收进 macos1 的「更多工具…」面板。
TERM_NUMS = ({'macos1', 'macos2', 'power5', 'power6', 'space1'} if IS_MAC
             else {'5', '8', 'X', '11'})

# ------------------------------------------------------------------ 界面视图键
#
# ★★ 2026-09-29（用户要求「尽量就都不用终端界面了」）：这几个菜单项在**图形界面**里
#    不再开终端、也不再内嵌跑脚本，而是**打开一个独立视图**（页面里的整屏界面）。
#       · local1 本机 · 任务折叠                    → 「先摊现状、再动手」面板（fold）
#       · cross2 跨机 · 恢复误删会话                → 勾选窗口（标题 + 多选 + 恢复）
#       · macos1 更多工具…                          → 「更多工具」选择面板
#       · macos2 合并会话                           → 挑两条会话的窗口
#       · space1 空间回收                           → 体检 + 分项清理
#    ⚠️ 这些键**仍然是正式菜单键**（黑窗菜单照旧、TERM_NUMS 里该留的还留着）——
#      视图只是图形界面这一侧的走法。do_run 里在**任何命令执行之前**拦下来。
#   ★★★ 2026-10-05（对齐 Windows 端）：**补上 local1 → fold**。
#      原来点「本机同步 · 立即跑一次」是直接跑命令、什么都不给你看；
#      现在点开第一层是**只读的账号与会话全景**（原 local2 那颗独立按钮的活），
#      看完你再按「开始折叠」—— 这正是用户要的「先给我看结果」。
VIEW_KEYS = {
    'local1': 'fold',
    'cross2': 'undelete',
    'macos1': 'tools',
    'macos2': 'merge',
    'space1': 'space',
}

# ★★ 2026-09-29（用户第 7 条）：**收起来的板块**（wb_menu.GROUP_PRESENT 里
#   标 'collapsed' 的那几个）→ 点开后那个「第二层界面」的名字（前端 VIEWS.<name>）。
#   首页上那排按钮就是照这个字段开视图的，**前端一个名字都不写死**。
#   ⚠️ 名字必须在前端 `_ui.html` 里有对应实现，否则点了只弹一句「不认识的视图」——
#      _chk_renumber.py 第 [2] 段会同时核这两边。
GROUP_VIEW = {
    # ★ 2026-10-05（对齐 Win 端）：同步板块由 6 项减到 4 项（local2/local3 并入
    #   local1）；前端 VIEWS.sync 还会**再拦掉 macos2**（它已收进「任务与工作空间」
    #   管理器那一层），所以这一层实际只画 3 张卡 + 1 行管理入口。
    'sync':  'sync',
    'space': 'space',    # 与 space1 那张卡同一个视图
    'macos': 'tools',    # 与 macos1 那张卡同一个视图
}

# 「更多工具…」面板里每一项 → 要跑的 argv。**界面专用键**（不是菜单项）。
#   ★ 为什么单独一张表而不是塞进 wb_menu.py：这些是"图形界面里的细粒度出口"，
#     多数在菜单里**故意没有**编号（否则菜单又会被撑回几十项 —— 那正是本次要治的）。
#   ⚠️ 改了这里要同步 _排障与验证脚本/_chk_renumber.py 的 UI_ONLY_KEYS。
# ★★★ 2026-10-11（拆家）：`TOOL_KEYS` / `TOOL_LABEL` / `TOOL_RISKY` 已删 ——
#   它们指向的全是**任务管家**的脚本（wb_sync_ctl.py / wb_fix_cwd.py / wb_hook…），
#   那些文件在本程序目录里根本不存在（点下去只会报错）。

# 内嵌执行时要额外补的实参（界面是点击触发的，没有输入口 —— 见下面 do_run 的说明）
#   ★ 2026-09-27 编号重排后：macOS 的「一键整合」键是 one1，Windows 仍是 '0'。
#     两个键都列上，谁在跑谁命中（多余的那条不会有害）。
UI_EXTRA_ARGS = {
    # ★★ 2026-09-29：'f'（跨机任务线 · 删除共享文件）—— 原来它在 TERM_NUMS 里，
    #   点一下会另开一个黑色终端问 y/N。用户要求「尽量都不用终端界面了」，而
    #   `wb_sessync.py` 恰好有 `--yes`（只为 --clear 准备的），于是这里替它回答掉，
    #   'f' 也就**从 TERM_NUMS 挪走**（判据：「这条命令还会不会问你要输入」）。
    #   ⚠️ 删共享文件不可逆 —— 它的「会改数据」标志（risky）仍由 wb_menu.EXTRA 的
    #      第 4 个字段控制，界面点它会先弹一次确认，别把那个标志去掉。
}

# 界面专用的补充说明（黑窗菜单不显示）。行为在两套 UI 里不一样时，写出来，
# 免得用户疑惑「为什么界面里不问、黑窗里问」。★ 键一律用**菜单里的正式编号**。
UI_HINTS = {
    # ★ 键 = wb_menu.py 的唯一键。macOS 用板块前缀那套，Windows 仍用旧数字。
    # ★ 下面这些项**都会 input()**，但 2026-09-29 起图形界面里改了走法：
    #   会开视图的走视图（见 VIEW_KEYS），其余在终端里问。
    #   ⚠️ 过时条目必须删掉：留着会显示**错误**的说明（比不显示更糟）。
    'env1':   '新机 / 旧机都点它：探测 → 写配置 → 补界面 → 查快照 → 重装守护 → '
              '重置运行监测 → 修图标 → 归位工作空间（不碰你的会话与记忆）',
    'cross2': '点开会列出被同步误删的会话，勾选后点「恢复」即可；只写库，正文不动',
    'macos1': '点开是一个独立的面板，在里面挑同步线总控 / 工作空间 / 图标 / 守护 / 跨机任务线应急',
    'macos2': '点开是一个整屏视图：全部存活会话**按工作空间分组**列出来，挑两条或更多 → '
              '弹窗给合并后的会话起个名 → 合并（也可以只预览不写盘）；'
              '会写库并改正文，务必先完全退出 WorkBuddy',
    'f':      '点一下直接删共享文件（界面已替你确认过一次）；各机已有的会话不受影响',
    'power1': '界面上「防休眠」那个主开关等同这一项；点一下开、再点一下关',
    'power2': '界面上「屏幕保持常亮」子开关等同这一项；主开关关着时点它会自动把主开关打开',
    'power3': '界面上「电量不足时自动停」子开关等同这一项（用电池且低于 10% 时结束）',
    'power4': '界面上「合盖后保持唤醒」子开关等同这一项；仅笔记本；已装免密不弹密码框；'
              '合盖检测走 IOKit 事件通知（与 PommeToys 同款），不再每 2 秒轮询',
    'power5': '界面上「跑完自动关机」按钮等同这一项（开启时会先弹一次确认 —— 真的会关机；关掉时不问）',
    'power6': '界面上「到点自动结束」按钮等同这一项；点开会让你在终端里挑一个时长（界面上是直接点时长块）',
    'power7': '界面上「停止」按钮等同这一项；立即恢复系统默认电源行为',
    'space1': '点开先体检（先看每项多大、有没有用），再按项点着清 —— 全在界面里完成，不弹终端',
}

# ================================================================== ⓘ 详细介绍
#
# ★★ 2026-09-29（用户第 3 条）：「把界面上所有描述性文字都删掉 …… 每个按钮后面放一个
#    小的感叹号，点击之后就是具体的介绍，**具体的介绍你还可以写得更详细一些**」。
#
# 所以这张表是**界面干净**的代价买单方：卡片上只留一个名字 + 一个 ⓘ，
# 说明全挪到这儿，而且写得比原来长得多。
#   · 键 = wb_menu.py 的唯一键（与卡片一一对应）；
#   · 值是 HTML 片段（前端 showHelp 对 help 字段**不做转义**，见 _ui.html），
#     所以可以用 <b> <br> <code>；
#   · 与 UI_HINTS 的分工：UI_HINTS 是「界面行为与黑窗不一样」的短提示，
#     这里是「这条是干嘛的」的完整说明。两者会被合并显示。
UI_HELP = {
    # ---------------- 环境 ----------------
    'env1': '<b>新机、旧机、哪里出问题，都点它。</b>一条 10 步的流水线，'
            '每一步都是幂等的 —— 跑第二遍不会把东西弄坏，所以放心多点几次。<br><br>'
            '① <b>本机环境</b> 探测本机环境并写入配置<br>'
            '② <b>工具包</b> 拍照快照 + 完整性校验<br>'
            '③ <b>工具包</b> 从快照补回缺失 / 被改坏的文件<br>'
            '④ <b>界面</b> 补齐界面三件套与外壳 app<br>'
            '⑤ <b>守护</b> 重装「跟随 WorkBuddy 启动」（本机同步守护）<br>'
            '⑥ <b>同步线</b> 三条线重新注册（清暂停 → 注册 → 立刻跑一次）<br>'
            '⑦ <b>运行监测</b> 收掉上次开机的一次性残留 + 按当前开关对齐守护'
            '（<b>不动</b>你的「防休眠」开关）<br>'
            '⑧ <b>图标</b> 重新注册 app + 刷新访达<br>'
            '⑨ <b>工作空间</b> 安全归位（只修分隔符写法不同的那一类）<br>'
            '⑩ <b>收尾</b> 环境自检与结论<br><br>'
            '<b>不会碰</b>：你的会话正文、记忆文件、技能库。'
            '唯一要写库的第 ⑨ 步刻意只做「安全的那一档」—— WorkBuddy 正在跑就整步跳过；'
            '要全量修，去「更多工具…」里单独点。',

    # ---------------- ★ 一键 ----------------
    'one1': '<b>拿不准点哪个就用它。</b>把「本机同步」和「跨机同步」一次跑完，'
            '顺带把工作空间归位。等于黑窗菜单里连着点好几项，但只按一下。<br><br>'
            '它<b>不</b>做需要你确认的事（比如写库的折叠、删共享文件）—— 那些还得单独点，'
            '因为不该由「一键」替你做决定。<br><br>'
            '执行过程会在右边的「执行日志」里一行行刷出来，并且<b>带步骤进度条</b>'
            '（这是全工具唯一有进度显示的一条）。',
    # ★★★ 2026-10-05（对齐 Windows 端）：新增「★ 同步账号」—— 文案与 Win 端同源，
    #   只把 Windows 的反斜杠路径 `_backup\账号同步\` 换成 macOS 的 `/`。
    'one2': '<b>账号（登录态）不在一键整合那四条线里 —— 新机器跑完它仍然要手工导账号才能登录。'
            '这一项就是把这件手工活自动化。</b><br><br>'
            '它用的是 <b>WorkDaddy 自带的</b>导出 / 导入接口（就是界面右上角那颗'
            '「导出账号」按出来的那两个），所以：<br>'
            '· 产出的文件与界面上导出的<b>完全同构</b>，哪天要手工导入，WorkDaddy 自己也认；<br>'
            '· 哪天 WorkDaddy 换了信封格式，本项自动跟着走，不用改代码。<br><br>'
            '一次跑三步（<b>并集收敛，不会互相顶掉</b>）：<br>'
            '① 把共享文件里<b>别的机器</b>导出的账号并进本机；<br>'
            '② <b>择优保护</b> —— 本机比共享文件里<b>新</b>的账号原样保留，不会被退回旧版'
            '（WorkDaddy 自带的导入是「覆盖」语义，两台机器都往一个文件导出会互相顶掉，'
            '所以中间加了这一层）；<br>'
            '③ 把本机现在的<b>全部</b>账号导出回共享文件，供其他电脑取用。<br><br>'
            '共享文件：共享目录里的 <code>WorkDaddy-账号同步.json</code>'
            '（共享目录默认沿用「同步」那边 <code>_会话同步.json</code> 的设置，'
            '要单独指定就改本目录的 <code>_账号同步.json</code>；各机密码必须一致）。<br><br>'
            '<b>★ 跑之前请确认 WorkDaddy 在运行</b> —— 它要连 WorkDaddy 的本地接口；'
            '连不上会明确提示你先启动 WorkDaddy（或跑一次「部署和修复」），不会静默失败。<br>'
            '<b>★ 每次动账号前会自动备份</b>到 <code>_backup/账号同步/</code>，保留最近 5 份。',

    # ---------------- 同步 · 本机 ----------------
    # ★★★ 2026-10-05（对齐 Windows 端）：
    #   · 原 local2（账号与会话全景，只读）**整条删** —— 它并进 local1 当前置检查；
    #   · 原 local3（执行全局单份）**并入 local1** —— 它和 local1 是同一件事的
    #     两条实现路径（local1 走常驻引擎 wb_autosync，local3 直接 wb_sync_global
    #     写库），用户一眼看穿：「这俩是一个功能」。
    'local1': '<b>同一台电脑上，多个账号的任务折叠成「全局唯一一份」。</b><br><br>'
              'WorkBuddy 的任务是按账号存的 —— 你切了号，同一个任务在列表里就会'
              '出现第二条。这一条跑一次，把它们并成一条。<br><br>'
              '<b>点开先摊现状、再动手</b>：这一层会先跑一遍<b>只读</b>的「账号与会话全景」'
              '（有几个账号、各多少条会话、哪些是重复的），看完再决定折不折 —— '
              '原来那颗独立的只读按钮已按你的要求撤掉，它现在就在这里当前置检查。<br><br>'
              '<b>运行中也能跑</b>（走的是 autosync 引擎，只动数据库里的归属与软删标记，'
              '不碰会话正文）。真要绕过它的运行检查，用「更多工具…」里的那颗'
              '「强制执行『全局单份』」——它会先备份数据库再写。',

    # ---------------- 跨机同步 ----------------
    'cross1': '<b>几台电脑之间搬东西</b>：记忆、技能、会话三条线各跑一次。<br><br>'
              '判定规则是「<b>双读单写</b>」—— 本机读共享目录、也读移动盘，'
              '但<b>只往本机这一处写</b>；移动盘是你自己用来搬运的，脚本从不写它。<br><br>'
              '想连着本机同步一起跑，用「★ 一键整合」。',
    'cross2': '<b>用来善后</b>：某次同步把本机上有、共享目录里没有的会话删掉了，'
              '这里能把它们捞回来。<br><br>'
              '点开是一张清单（只列<b>标题</b>，看着清爽）：勾一个或多个 → 点「恢复」。'
              '只写库，<b>正文文件不动</b>，所以不会把内容改坏。<br><br>'
              '平时用不到；真误删了再来。',

    # ---------------- 运行监测 ----------------
    'power1': '<b>总开关。</b>开着 = 跑任务期间不睡眠、不休眠，任务一结束立刻撒手，'
              '回到你自己原本的电源设置，和从没开过一样。<br><br>'
              '它是<b>进程级</b>申请（Mac 上是 caffeinate 子进程），<b>不改系统设置</b>，'
              '所以退出即失效、不会有残留。<br><br>'
              '它一关，下面几个子开关全部不生效（状态会记住，重新打开还是原样）。',
    'power2': '<b>子开关：屏幕要不要一直亮着。</b><br><br>'
              '关着 = 只防睡眠，屏幕该黑还是黑（后台照跑，最省电）。<br>'
              '开着 = 屏幕也不黑，适合你在旁边盯着看进度。<br><br>'
              '★ 主开关关着时点它会<b>自动把主开关一起打开</b> —— 因为单开子开关没有意义。',
    'power3': '<b>子开关：用电池且电量低于 10% 时，主动结束监测。</b><br><br>'
              '目的是<b>让睡眠保住电量</b> —— 与其把电耗干，不如让电脑正常睡下去。<br><br>'
              '插着电源时这条不生效。',
    'power4': '<b>子开关：合上盖子也不睡。</b>仅笔记本有意义。<br><br>'
              '它<b>会改系统设置</b>（<code>pmset -a disablesleep</code>，需要管理员权限），'
              '原值记在 <code>_运行监测.json</code> 里，'
              '关掉它 / 停止监测 / 守护退出时都会<b>原样写回</b>。<br><br>'
              '本机已经装了免密白名单，不会再弹密码框。'
              '合盖检测走 IOKit 事件通知（与 PommeToys 同款），不再每 2 秒轮询。',
    'power5': '<b>收尾开关①：任务全部结束后把电脑关掉。</b><br><br>'
              '⚠️ 真的会关机 —— 开启时<b>会先弹一次确认</b>；关掉时不问。<br><br>'
              '它和「到点自动结束」<b>彼此互斥</b>：开一个会自动关掉另一个。'
              '两个都是「跑完之后怎么收尾」的答案，同时开着谁也说不清电脑最后会怎么走。<br><br>'
              '它<b>完全独立于</b>上面那组开关 —— 不开防休眠也照常跑。',
    'power6': '<b>收尾开关②：设一个时长，时间一到就撤掉防休眠并停止监测。</b><br><br>'
              '点一下展开时长选择条，挑一个（或自己填分钟数）。<br><br>'
              '与「跑完自动关机」<b>互斥</b>（见那一条的说明）。<br>'
              '它也<b>完全独立于</b>上面那组开关。',
    'power7': '<b>立即停手。</b>马上恢复系统默认的电源行为 —— '
              '该睡就睡、屏幕该黑就黑，跟从来没开过这个工具一样。<br><br>'
              '「合盖后保持唤醒」改过的系统设置也会在这一刻写回原值。<br>'
              '下面的收尾开关（跑完自动关机 / 到点自动结束）也一并撤掉。',

    # ---------------- 空间回收 ----------------
    'space1': '<b>本机磁盘清理。</b>点开是一张体检表：每一项多大、有多少文件、'
              '有没有用、为什么，<b>先看清楚再动手</b>。<br><br>'
              '列出来的都是本工具自己产生的中间物：死副本、日志、工具包缓存、'
              '过期备份、超期软删会话、回滚备份。<br><br>'
              '两个安全设计：<b>①</b> 删之前先体检，不体检不给删；'
              '<b>②</b> 删是<b>移到废纸篓</b>，不是直接抹掉 —— 后悔了还能捞回来。',

    # ---------------- 更多工具 ----------------
    'macos1': '<b>这里装的是「不方便占主界面一格、但偶尔要用」的东西。</b><br><br>'
              '· 三条同步线的总控（逐条暂停 / 恢复 / 彻底停用 / 体检自愈）<br>'
              '· 同步项开关（记忆 / 技能 / 会话 各要不要同步）<br>'
              '· 工作空间：合并同义目录、安全归位、全量修复归属<br>'
              '· 图标：恢复默认<br>'
              '· 守护：卸载「跟随 WorkBuddy 启动」<br>'
              '· 跨机任务线应急：只收取 / 强制重写共享文件 / 删除共享文件<br><br>'
              '点开是一个独立面板，在里面挑 —— 不弹终端窗口。'
              '已被「★ 一键整合」或「部署和修复」覆盖的操作，这里<b>不再重复列</b>。',
    'macos2': '<b>同一件事聊成了几条会话，把它们并回一条 —— 几条都行，不限两条。</b><br><br>'
              'WorkDaddy 的自动复制、或跨机同步，都可能让一条会话变成几条。<br><br>'
              '点开是<b>一整屏</b>：本机全部存活会话<b>按工作空间分组</b>列出'
              '（一组一个工作空间，可折叠；上面还有搜索框），'
              '在里面<b>挑两条或更多</b>（选几条都行，选第一条时方块就亮）→ '
              '弹出一个窗口给合并后的会话<b>起个名字</b> → 合并。'
              '创建最早的一条自动当「底」，其余按时间接在后面；想换底就点某行的「设为底」。<br><br>'
              '效果上等于「这几条不要了，合成一条新的、名字是我起的」——'
              '但底层做法是<b>把内容并进其中一条、并把它改名</b>'
              '（新建会话会换掉会话身份，跨机同步会把它当新会话发到别的电脑，'
              '反而又多出一条）。<br><br>'
              '⚠️ <b>必须先完全退出 WorkBuddy</b> —— 合并会连正文（jsonl）一起重写，'
              '热写会被内存里的状态覆盖。<br><br>'
              '合并只<b>追加</b>、绝不重排：重排文件头会导致「身份漂移」，'
              '结果又变回两条。<br>'
              '改名只写在<b>显示名</b>上（<code>custom_title</code>），原始标题留着不动，'
              '随时能还原。',

    # ---------------- EXTRA（跨机任务线应急）----------------
    'a': '<b>跨机任务线 · 只收取。</b>把别的电脑上已经并进共享文件的会话，'
         '收到本机来，<b>不往共享文件里写</b>。<br><br>'
         '典型场景：你在另一台机器上干了活，只想把成果拿过来，'
         '不希望本机这边反过来影响它。',
    'b': '<b>跨机任务线 · 强制重写共享文件。</b>⚠️ 应急用 —— '
         '只有共享文件本身坏掉（读不出来、结构乱了）才用它，'
         '它会用本机的状态把共享文件整个重写一遍。<br><br>'
         '点之前先想想：如果本机这边也不全，重写会把共享文件里更全的版本盖掉。'
         '点它会先弹一次确认。',
    'f': '<b>跨机任务线 · 删除共享文件。</b>⚠️ 不可逆。<br><br>'
         '删的是共享目录里那份中转文件，<b>各台电脑上已有的会话不受影响</b>'
         '（它们各自在本机存了一份）。<br><br>'
         '什么时候用：共享文件已经乱到没法收拾，想从零重新开始收。'
         '点它会先弹一次确认。',
}

# 界面文案里要提到的「桌面入口」名字 —— 各平台不一样，前端不该硬编码。
#   页面文案里凡是「重开界面」「看报错」的地方都引用这两个名字，
#   免得 macOS 上写着「请双击界面.vbs」（本机根本没有那个文件）。
#   ★ 由 /api/menu 带给前端（见 do_GET）。
ENTRY_NAME = ('TimeGuardian.app' if IS_MAC
              else ('界面.vbs' if IS_WIN else '界面.sh'))
DEBUG_ENTRY = ('菜单.command' if IS_MAC
               else ('界面.bat' if IS_WIN else '界面.sh'))

# ------------------------------------------------------------------ 日志


_LOG_LOCK = threading.Lock()


def log(msg):
    line = '%s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        with _LOG_LOCK:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1024 * 1024:
                try:
                    os.replace(LOG_FILE, LOG_FILE + '.1')
                except Exception:
                    pass
            with io.open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line)
    except Exception:
        pass


# ------------------------------------------------------------------ Python 定位

_MAC_PY = {'v': None}


def _mac_best_python():
    """macOS：挑一个「装了本工具包要用的第三方包」的解释器。

    ★ 为什么不能直接用 `sys.executable`：界面外壳 app 是用**系统解释器**
      （/usr/bin/python3）把服务拉起来的（系统解释器随叫随到、不用找路径），
      而它会**被复用去跑菜单里的子脚本** —— 那些脚本要 pymupdf / Pillow /
      openpyxl 等，系统解释器里一概没有。所以这里显式换成本机那套通用环境
      （`~/.workbuddy/binaries/python/envs/default`，见项目记忆第 2 节）。
    顺序：$WB_PY → 通用环境 → wb_paths 找到的托管解释器 → 当前解释器。
    """
    if _MAC_PY['v']:
        return _MAC_PY['v']
    cands = [os.environ.get('WB_PY') or '',
             os.path.join(HERE, '_runtime', 'python', 'bin', 'python3'),   # ★ 随包自带运行时优先
             os.path.expanduser('~/.workbuddy/binaries/python/envs/default/bin/python')]
    try:
        import wb_paths as _P                      # 只在 Mac 这条路上用，失败不影响
        cands.append((_P.find_python() or [''])[0])
    except Exception:
        pass
    cands.append(sys.executable or '')
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            _MAC_PY['v'] = c
            return c
    return sys.executable


def find_pythonw():
    """找 pythonw.exe（无控制台）；找不到就退回 python.exe。

    macOS 没有 pythonw（也不存在「控制台窗口」这个概念），统一返回普通解释器。
    """
    if IS_MAC:
        return _mac_best_python()
    exe = sys.executable or ''
    if exe.lower().endswith('python.exe'):
        w = exe[:-len('python.exe')] + 'pythonw.exe'
        if os.path.exists(w):
            return w
    return exe


def find_python():
    if IS_MAC:
        return _mac_best_python()
    return sys.executable


def find_python_console():
    """找 python.exe（**控制台子系统**）—— 内嵌任务的宿主必须用它，不能用 pythonw.exe。

    ★ 为什么（2026-09-26 实测定位，用户报「运行 0 号一直弹窗、跑完还弹」）：
      pythonw.exe 属 GUI 子系统，**根本没有控制台**；而 `CREATE_NO_WINDOW` 对
      非控制台程序是**无效**的（文档明说该标志对 GUI 程序被忽略）。
      于是任务进程没有控制台 → 它下面每一个控制台程序（powershell / python /
      tasklist）都要**各自新建一个控制台**；本机默认终端是 Windows Terminal，
      新建控制台就弹一个终端窗口 → 四步就弹四个，最后一步（归位工作空间里的
      tasklist）刚好落在四条线跑完之后，于是"跑完了还弹"。
    ★ 换成 python.exe 之后：`CREATE_NO_WINDOW` 生效 = 任务进程有一个**无窗口的
      控制台**，整棵子进程树都继承它 → 一个窗口都不会再冒出来。
      （实测：python.exe 宿主 + CREATE_NO_WINDOW，跑 mem+sess 两条线全程 0 个窗口；
        pythonw 宿主则每步都冒出 CASCADIA_HOSTING_WINDOW_CLASS = Windows Terminal。）
    """
    exe = sys.executable or ''
    if exe.lower().endswith('pythonw.exe'):
        c = exe[:-len('pythonw.exe')] + 'python.exe'
        if os.path.exists(c):
            return c
    return _mac_best_python() if IS_MAC else exe


# ------------------------------------------------------------------ 端口 / 单实例

def _port_busy(port, timeout=0.6):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((HOST, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


# ★ 第卅七轮（2026-10-01 用户第 3 条）：栏目由「滚动」改名「鼠标」后，`_界面.json`
#   里旧名留下的键得清掉。外壳（WBShell.swift）在写盘时也清同一份清单 —— **两处一份事实**，
#   加/删旧键名要同时改（`LEGACY_CONF_KEYS` / `LEGACY_CONF_KEYS`）。
LEGACY_CONF_KEYS = ('scrollPerm', 'scrollEngine')


def load_conf():
    try:
        with io.open(CONF_FILE, encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {}
    except Exception:
        return {}
    # ★ 在**读**就把旧键摘掉：本模块每个写盘路径都是「先 load 再 save」，
    #   读时清干净 ⇒ 写回去的也干净（不用在每个 save 处再清一遍）。
    hit = [k for k in LEGACY_CONF_KEYS if k in d]
    if hit:
        for k in hit:
            d.pop(k, None)
        log('清掉改名前的旧键：%s' % ' / '.join(hit))
    return d


def save_conf(d):
    try:
        with io.open(CONF_FILE, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log('写配置失败：%r' % (e,))


# ------------------------------------------------------------------ 打开窗口

def _shell_app_path():
    """macOS 外壳 app 的位置（可用环境变量覆盖，便于换机或测试）。"""
    return os.environ.get('WB_SHELL_APP') or SHELL_APP


def _open_shell_app():
    """macOS：直接把这个窗口外壳 app 叫起来。

    ★ 为什么不用 Edge/Chrome 的 `--app=`：本机**没装** Edge / Chrome（只有 Safari，
      而 Safari 不支持 `--app=`）。用 `open -a` 起自带的外壳，等价且更稳 ——
      它用系统 WebKit，不依赖任何第三方浏览器；窗口的尺寸还原、图标都由它自己管。
    ★ `open -a` 会复用已在运行的实例（同一个 app 只开一个窗口），这正是我们要的。
    """
    p = _shell_app_path()
    if not os.path.isdir(p):
        log('找不到外壳 app：%s' % p)
        return False
    try:
        subprocess.Popen(['open', '-a', p])
        log('已用外壳 app 打开界面：%s' % p)
        return True
    except Exception as e:
        log('打开外壳 app 失败：%r' % (e,))
        return False


def _browser_candidates():
    if IS_MAC:
        return [['open', '-a', 'Microsoft Edge', '--args'],
                ['open', '-a', 'Google Chrome', '--args']]
    if not IS_WIN:
        return []
    pf = os.environ.get('ProgramFiles', r'C:\Program Files')
    pf86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    la = os.environ.get('LOCALAPPDATA', '')
    out = []
    for base in (pf86, pf, la):
        for rel in (r'Microsoft\Edge\Application\msedge.exe',
                    r'Google\Chrome\Application\chrome.exe'):
            if base:
                p = os.path.join(base, rel)
                if os.path.exists(p):
                    out.append([p])
    return out


WIN_DEFAULT = (1180, 820)

# 合理的窗口尺寸区间。低于/高过这个范围的值一律不采纳 —— 某些环境下
# outerWidth/outerHeight 会返回 0 或屏幕外的怪值，存进去下次就成了畸形窗口。
WIN_MIN_W, WIN_MAX_W = 600, 8000
WIN_MIN_H, WIN_MAX_H = 400, 6000


def _win_geometry():
    """取上次的窗口尺寸/位置，拼成浏览器的命令行参数。

    ★ 为什么需要这个：`--app` 每次打开都是一个**全新的顶层窗口**，Chromium
      **不会**自己记住它的宽高与位置（跟普通浏览器窗口不同，那些会存在 profile
      里）。所以「用户调过大小、下次却回到默认」不是 bug 而是默认行为 ——
      只能由页面把尺寸报回来、我们在下次启动时再传一次。
      尺寸由 _ui.html 每 2 秒采样一次（见 /api/winsize）。
    """
    conf = load_conf()
    try:
        w = int(conf.get('winW') or 0)
        h = int(conf.get('winH') or 0)
    except Exception:
        w = h = 0
    if not (WIN_MIN_W <= w <= WIN_MAX_W and WIN_MIN_H <= h <= WIN_MAX_H):
        w, h = WIN_DEFAULT

    args = ['--window-size=%d,%d' % (w, h)]
    try:
        x, y = conf.get('winX'), conf.get('winY')
        if x is not None and y is not None:
            x, y = int(x), int(y)
            if -20000 <= x <= 20000 and -20000 <= y <= 20000:
                args.append('--window-position=%d,%d' % (x, y))
    except Exception:
        pass
    return args


def save_win_size(d):
    """记下页面报上来的窗口尺寸/位置，供下次 open_window 复用。只改配置，很轻。"""
    try:
        w = int(d.get('w') or 0)
        h = int(d.get('h') or 0)
    except Exception:
        return False
    if not (WIN_MIN_W <= w <= WIN_MAX_W and WIN_MIN_H <= h <= WIN_MAX_H):
        return False

    conf = load_conf()
    changed = (conf.get('winW') != w) or (conf.get('winH') != h)
    conf['winW'], conf['winH'] = w, h
    try:
        x, y = d.get('x'), d.get('y')
        if x is not None and y is not None:
            xi, yi = int(x), int(y)
            if -20000 <= xi <= 20000 and -20000 <= yi <= 20000:
                if conf.get('winX') != xi or conf.get('winY') != yi:
                    changed = True
                conf['winX'], conf['winY'] = xi, yi
    except Exception:
        pass
    if changed:
        save_conf(conf)
        log('窗口几何已记：%dx%d @ %s,%s' % (w, h, conf.get('winX'), conf.get('winY')))
    return True


def open_window(port, block_icon=False):
    """用「应用窗口」模式打开界面 —— 没有地址栏，看着像独立软件。

    窗口尺寸/位置沿用上次用户调过的值（见 _win_geometry）。

    ★ URL 上的 `?appwin=1` 是个标记：页面只在这个标记存在时才回报窗口尺寸。
      不然用户从普通浏览器打开一次，那个标签页的大小就会被当成「窗口尺寸」
      记下来，下次 --app 窗口就以那个尺寸开 —— 越修越乱。

    ★ `block_icon`（2026-09-27 加，踩过坑）：
      `_apply_window_icon()` 要等窗口出现（几秒）才能设图标。**短命进程**（`--launch`、
      `main()` 里「已有实例 → 只开窗口」那两条路）**返回后立刻退出**，而 `daemon=True`
      的线程会随进程一起被杀 —— 于是图标**永远设不上**（用户报「你明明生成过清晰的、
      实际还是不清晰」，就是这个）。所以这两条路必须**同步等待**（block_icon=True）；
      只有常驻的服务进程（`threading.Timer` 那条）才用后台线程。
    """
    url = 'http://%s:%d/?appwin=1' % (HOST, port)
    geom = _win_geometry()
    # ★ macOS 优先走自带的窗口外壳（本机没有 Edge/Chrome，见文件头说明）。
    #   外壳自己会从 _界面.json 还原尺寸，所以这里不必再传 --window-size。
    if IS_MAC and _open_shell_app():
        return True
    for cmd in _browser_candidates():
        try:
            subprocess.Popen(cmd + ['--app=' + url] + geom)
            log('已用应用窗口打开：%s  %s' % (' '.join(cmd), ' '.join(geom)))
            # ★ 窗口起来后再补一次 WM_SETICON，把任务栏图标从「favicon 16px 放大」
            #   换成 .ico 的原生高清档（见 _apply_window_icon 的说明）。
            if block_icon:
                _apply_window_icon(hold=WIN_ICON_HOLD)
            else:
                # 常驻服务：看得久一点，窗口活着就一直管着它
                threading.Thread(target=_apply_window_icon,
                                 kwargs={'hold': WIN_ICON_HOLD_SVC},
                                 daemon=True).start()
            return True
        except Exception as e:
            log('尝试 %s 失败：%r' % (cmd, e))
    try:
        webbrowser.open(url)
        log('已用默认浏览器打开：%s' % url)
        return True
    except Exception as e:
        log('打开浏览器失败：%r' % (e,))
        return False


# ------------------------------------------------------ 给窗口换高清图标

# 任务栏那个图标只有 ~30px，但 Chromium 从 favicon 只给到 16×16。
# 窗口建好之后补一次 WM_SETICON，就能换成 .ico 里的原生档。
#
# ★★ 但**只设一次不够**（2026-09-27 查出来，用户报「还是糊」）：
#   刚开窗口时 Chromium 还没拿到 favicon，我们把图标设上；等 favicon 载入，它又拿那
#   16px 档把窗口图标**覆盖回去** → 任务栏仍旧从 16px 放大。
#   证据：整条流程跑完（日志写着"已换成高清档"）之后再读 `WM_GETICON`，拿到的**不是**
#   我们那个句柄；而在 favicon 载完之后**重设一次**，屏幕上的图标立刻从
#   「最像 ico16 / MAD 6.4」变成「最像 ico32 / MAD 2.5」，肉眼也明显变清楚。
#   这也解释了回归脚本 `_chk_ui_icon.py` 为什么**假阳性通过** —— 它等 12 秒才设，
#   那时 favicon 早载完了，永远撞不上这个覆盖。
#   → 所以设完必须**继续看住** `hold` 秒。
#
# ★★ 第三轮（2026-09-27，用户报「每次刚打开会模糊一下，过两秒才变清晰」）：
#   上一轮的对策是"**等 favicon 装好再设**"（`SETTLE = 6` 秒）。它确实治住了"被盖回去"，
#   但代价是——**糊的时间正好等于这 6 秒**，把"刚打开"这段最显眼的时间全糊上了。
#   `_tmp_pair2.py` 的实测把这点钉死了：开窗后 2 秒，任务栏最像 **ico16 / MAD 6.81**。
#   正确做法不是"躲"，而是"**抢**"：
#     ① `SETTLE` 压到 0.5 秒 —— 窗口一出现就首发；
#     ② 头 `BURST` 秒**密集重发**（每 `BURST_GAP` 秒一次）。Chromium 就算把图标盖回去，
#        0.35 秒内又被我们盖回来 —— 视觉上根本看不到糊。
#   实测：窗口出现后 ≤0.5 秒即为高清档（最像 ico64 / MAD ~2.0），此后一直保持。
#   代价只是头 8 秒多几百次 `SendMessageW`（本地消息，开销可忽略）。
#
# 这一步纯属「锦上添花」，任何环节出问题都必须静默跳过，绝不能连累开窗口。
WIN_ICON_TITLE_KEY = '任务管家'
WIN_ICON_WAIT = 25.0          # 最多等这么久（秒），等不到就算了
WIN_ICON_SMALL, WIN_ICON_BIG = 64, 256
# ★ 别再"等 favicon 装好"—— 那等于把图标糊着放 6 秒。见上面第三轮说明。
WIN_ICON_SETTLE = 0.5         # 窗口出现后只等这么久就首发（等窗体初始化，不等 favicon）
WIN_ICON_BURST = 8.0          # ★ 头 8 秒「密集重发」，专治「刚打开模糊一下」
WIN_ICON_BURST_GAP = 0.35     # 密集期重发间隔：Chromium 盖回来也能在 0.35 秒内改回
WIN_ICON_RESEND = 2.0         # 密集期之后放宽（别让任务栏一直闪），但仍继续看住
WIN_ICON_POLL = 0.15          # 找窗口的轮询间隔（原来 0.5 秒，白等最久 0.5 秒）
WIN_ICON_HOLD = 25.0          # 短命进程（--launch 等）：看一小会儿，反正进程一会儿就退
WIN_ICON_HOLD_SVC = 180.0     # 常驻服务：窗口活着就管着


def _apply_window_icon(wait=None, hold=None):
    """给刚打开的界面窗口补设高清图标，并在 hold 秒内看住它别被 Chromium 盖回去。

    返回 True/False（仅用于日志）。
    """
    if not IS_WIN or not os.path.exists(ICON_FILE):
        return False
    wait = WIN_ICON_WAIT if wait is None else wait
    hold = WIN_ICON_HOLD if hold is None else hold
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32

        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        u.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                               ctypes.POINTER(wintypes.DWORD)]
        u.SendMessageW.restype = ctypes.c_void_p
        u.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.c_void_p]
        u.LoadImageW.restype = ctypes.c_void_p
        u.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.c_wchar_p,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
    except Exception as e:
        log('设窗口图标：ctypes 初始化失败 %r' % (e,))
        return False

    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
    BROWSER_EXES = ('msedge.exe', 'chrome.exe')

    def _proc_exe(pid):
        h = k.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ''
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(1024)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return os.path.basename(buf.value).lower()
        except Exception:
            pass
        finally:
            k.CloseHandle(h)
        return ''

    def _find():
        hits = []

        def _cb(hwnd, lp):
            try:
                if not u.IsWindowVisible(hwnd):
                    return True
                n = u.GetWindowTextLengthW(hwnd)
                if not n:
                    return True
                b = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, b, n + 1)
                if WIN_ICON_TITLE_KEY not in b.value:
                    return True
                pid = wintypes.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if _proc_exe(pid.value) in BROWSER_EXES:
                    hits.append(hwnd)
            except Exception:
                pass
            return True

        u.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND,
                                         wintypes.LPARAM)(_cb), 0)
        return hits

    # 先按尺寸把 HICON 载好，避免每轮重复 LoadImage
    handles = []
    for size in (WIN_ICON_SMALL, WIN_ICON_BIG):
        try:
            h = u.LoadImageW(None, ICON_FILE, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        except Exception:
            h = None
        if h:
            handles.append((size, h))
    if not handles:
        log('设窗口图标：LoadImageW 失败，跳过')
        return False

    start = time.time()
    deadline = start + wait          # 第一阶段：等窗口出现
    seen = None                      # 第一次看到窗口的时刻
    first_send = None                # 首次设上的时刻（密集重发从这一刻算起）
    next_send = 0.0                  # 下次允许重发的时刻（限流，免得任务栏一直闪）
    done = False
    while True:
        now = time.time()
        if seen is None:
            if now >= deadline:
                break
        elif now >= seen + WIN_ICON_SETTLE + hold:
            break                    # 看护期结束
        try:
            hwnds = _find()
            if hwnds and seen is None:
                seen = now
            # ★「抢」而不是「躲」（第三轮结论）：窗口一出现就设（SETTLE 仅 0.5 秒），
            #   然后头 BURST 秒**密集重发**。Chromium 拿到 favicon 后确实会把窗口图标
            #   盖回 16px 档，但 0.35 秒后又被我们盖回来 —— 用户视觉上就看不到糊。
            #   也别想用 WM_GETICON 判断"设上了没有"：Chromium 自己拦了 WM_GETICON，
            #   恒不返回我们那个句柄；按时间重发是唯一可行、且实测有效的办法。
            if (seen is not None and hwnds
                    and now >= seen + WIN_ICON_SETTLE and now >= next_send):
                for hwnd in hwnds:
                    for size, h in handles:
                        idx = ICON_SMALL if size == WIN_ICON_SMALL else ICON_BIG
                        u.SendMessageW(hwnd, WM_SETICON, idx, h)
                if first_send is None:
                    first_send = now
                    log('界面窗口图标已换成高清档（hwnd=%s，%d 档；窗口出现后 %.1f 秒即设，'
                        '头 %.0f 秒每 %.2f 秒重发防 Chromium 盖回，此后每 %.0f 秒一次，'
                        '共看住 %.0f 秒）'
                        % (hex(hwnds[0]), len(handles), now - seen,
                           WIN_ICON_BURST, WIN_ICON_BURST_GAP, WIN_ICON_RESEND, hold))
                    done = True
                if now - first_send < WIN_ICON_BURST:
                    next_send = now + WIN_ICON_BURST_GAP
                else:
                    next_send = now + WIN_ICON_RESEND
        except Exception as e:
            log('设窗口图标出错：%r' % (e,))
        time.sleep(WIN_ICON_POLL)

    if not done:
        log('设窗口图标：%.0f 秒内没等到界面窗口，跳过' % wait)
    else:
        log('界面窗口图标看护结束（共 %.0f 秒）' % (time.time() - start))
    return done


# ------------------------------------------------------------------ 菜单数据

# 菜单结构的返回值缓存。★ 必须连 mtime 一起记：菜单定义改过就立刻作废，
# 否则「改完菜单 → 刷新页面」最前面那 5 秒还会看到老的，很容易被当成"没生效"。
MENU_CACHE = {'t': 0, 'data': None, 'mtime': None}

_MENU_MOD = {'mtime': None, 'mod': None}


def menu_mtime():
    """wb_menu.py 的修改时间；读不到（文件被删/权限）返回 None。"""
    try:
        return os.path.getmtime(os.path.join(HERE, 'wb_menu.py'))
    except OSError:
        return None


def _file_fp(path):
    """文件的指纹：`<mtime 秒>-<字节数>`；读不到返回空串。"""
    try:
        st = os.stat(path)
        return '%d-%d' % (int(st.st_mtime), st.st_size)
    except OSError:
        return ''


def page_ver():
    """界面页 `_ui.html` 的指纹 —— 页面靠它发现「我这版已经过期了」。

    ★ 为什么必须放进 /api/status（2026-09-29 晚）：窗口里的页面是**开窗那一刻**
      加载进来的，服务端换了新页面它自己不会知道 —— 外壳只在 `webView.url == nil`
      时才 load（见 WBShell.swift::showWindowEnsuringService），所以「我已经改完」
      和「用户屏幕上是什么」可以差好几个小时。用户当场截图指着一颗**早就删掉**的
      按钮说「这个按钮也不需要存在了」，就是踩在这里。
      现在页面每 3 秒轮询时顺带比一下指纹，不一致就自己 reload。
    """
    return _file_fp(PAGE_FILE)


def menu_ver():
    """菜单表 `wb_menu.py` 的指纹 —— 页面靠它决定「要不要重拉一次 /api/menu」。

    ★ 菜单是**开窗时拉一次**、之后刻意不轮询的；改了板块名/编号之后，用户不重开
      窗口就永远看不到（这条坑在说明书里记了很久）。指纹一变就让前端重拉。
    """
    return _file_fp(os.path.join(HERE, 'wb_menu.py'))


def code_stale():
    """磁盘上的 wb_ui.py 是否比**本进程启动时**更新 —— 即「该点『关闭服务』重开了」。

    ★ 只用来**提示**，绝不自动重启：服务内存里可能挂着用户正在跑的内嵌任务，
      擅自退出会把它一起断掉（见说明书第 10 节）。
    +0.5 秒余量，躲开「同一秒内改完又启动」的边界。
    """
    try:
        return (BOOT_MTIME is not None
                and os.path.getmtime(os.path.abspath(__file__)) > BOOT_MTIME + 0.5)
    except OSError:
        return False


def menu_module():
    """拿 wb_menu 模块；它的源文件被改过就**热重载**。

    ★ 为什么需要：本服务是常驻进程，`import wb_menu` 一次之后模块就进了
      `sys.modules`。此后改 MENU / GROUPS / EXTRA / 描述文字，进程里看到的
      还是老代码 —— 表现就是「改完界面没变化，必须点『关闭服务』再重开」。
      （5 秒缓存只缓存**返回值**，解决不了模块缓存。）
      这里按源文件 mtime 判断：改过就 reload，于是**改完刷新页面即可**，
      因为前端每次加载都会重新拉一次 /api/menu。
    ★ 为什么 reload 是安全的：wb_menu 模块级只有 import 与常量，没有需要保留的
      运行时状态；`import wb_sync_global as G` 走的是 sys.modules 缓存，
      不会重跑它的 stdout 包装（那件事重跑两遍会关掉 fd 1，见 wb_menu 里的注释）。
    ★ 界面里跑动作是**起子进程**（`python xxx.py`），子进程每次都读磁盘上的新代码，
      所以只有「进程内 import 的菜单数据」需要这套热重载。
    """
    import importlib
    import sys
    fresh = 'wb_menu' not in sys.modules     # 本次调用才第一次导入（模块里就是磁盘上的新代码）
    import wb_menu as M
    mt = menu_mtime()
    if mt is None:
        return M
    prev = _MENU_MOD['mtime']
    if prev is None:
        # 首次调用：模块要么是本次刚导入的（就是磁盘上的新代码，不必白 reload 一次），
        # 要么是别的代码先导入的（那就不能假设它新，强制对齐一次）。
        prev = mt if fresh else -1
    if prev != mt:
        old = _MENU_MOD['mod'] or M
        try:
            M = importlib.reload(M)
            log('wb_menu.py 已变动，热重载成功（mtime=%s）' % mt)
        except Exception as e:
            # 改坏了（语法错等）就继续用旧模块，让界面不至于打不开；
            # ★ 仍记下这次 mtime，免得每次请求都刷一条同样的失败日志 ——
            #   用户把文件改好之后 mtime 会再变，那时自然会重试。
            log('热重载 wb_menu 失败，继续用旧模块：%r' % (e,))
            M = old
    # ★ 登记要**无条件**做：不能只在 reload 分支里写 _MENU_MOD['mod']，
    #   否则首次调用（prev 已对齐、跳过 reload）会返回 None，/api/menu 直接 500。
    _MENU_MOD['mtime'] = mt
    _MENU_MOD['mod'] = M
    return M


def _split_desc(desc):
    """把 '运行监测 · 档位一：不睡 + 不休眠（任务结束恢复默认）' 拆成
    组名 / 主标题 / 副标题。拆不出来就整条当主标题。"""
    group, rest = '', desc
    if '·' in desc:
        head, tail = desc.split('·', 1)
        head, tail = head.strip(), tail.strip()
        if len(head) <= 8 and tail:
            group, rest = head, tail
    sub = ''
    if rest.endswith('）') and '（' in rest:
        i = rest.rfind('（')
        # 只在括号包住尾部时拆分，避免拆掉正文里的括号
        if i > 0:
            sub = rest[i + 1:-1].strip()
            rest = rest[:i].strip()
    return group, rest, sub


# ★★ 2026-09-29（用户第 3 条）：「把软件界面上所有描述性文字都删掉」。
#   卡片上只剩一个**极简名字**，全貌搬进右上角那个 ⓘ。两处要砍：
#     ① **板块前缀**（「本机同步 · 立即跑一次」）—— 卡片本来就画在「本机同步」这个
#        tab 底下，再写一遍是冗余；
#     ② **冒号后面的描述**（「运行监测 · 防休眠：总开关」→「防休眠」）。
#   ⚠️ 只动**发给图形界面**的那个名字。wb_menu.py 里的完整标题一个字不改 ——
#      黑窗编号菜单是平铺列表，没有 tab 这层上下文，那里正需要这些前缀。
#   ★★ 2026-09-29 晚（用户第 3 条「本机同步与跨机同步……合并成一个『同步』」）：
#     两块并进同一个 tab 之后，「本机同步 · 立即跑一次」与「跨机同步 · 立即跑一次」
#     剥完前缀会**双双只剩「立即跑一次」**，同一张卡面分不清是哪条线 ——
#     所以这两条前缀要**缩写后保留**（→「本机 · 立即跑一次」「跨机 · 立即跑一次」），
#     其余前缀照旧剥掉（卡片就画在那个 tab 底下，再写一遍板块名是冗余）。
_UI_PREFIXES = ('本机同步', '跨机同步', '同步', '运行监测', '空间回收', 'macOS 专属')
_UI_KEEP_PREFIX = {'本机同步': '本机', '跨机同步': '跨机',
                   # ★★★ 2026-10-05（对齐 Windows 端）：新体例把「本机 / 跨机」
                   #   挪到 `·` 前面当**板块前缀**（「本机 · 任务折叠」），
                   #   所以这两个词自己也得登记一条「缩写后保留」——
                   #   否则 _split_desc 会把它当板块前缀剥掉，卡片上只剩「任务折叠」。
                   '本机': '本机', '跨机': '跨机'}


def _ui_name(num, name, group=''):
    """把菜单标题压成「卡片上该显示的那几个字」。压不出来就原样返回。

    `group` = `_split_desc` 拆出来的板块前缀（多数标题都带一个，如「运行监测」）；
    它自己会被 `_split_desc` 摘掉，所以「要不要把前缀接回去」只能靠这里补。
    """
    n = (name or '').strip()
    # ① 需要保留的前缀：缩写后接回去
    short = _UI_KEEP_PREFIX.get((group or '').strip())
    if short and n:
        n = '%s · %s' % (short, n)
    # ② 兜底：name 里若还带着板块前缀（`_split_desc` 没拆出来时），剥掉 / 缩写
    for pre in _UI_PREFIXES:
        cut = ''
        for sep in (' · ', '·'):
            if n.startswith(pre + sep) and len(n) > len(pre + sep):
                cut = n[len(pre) + len(sep):].strip()
                break
        if cut:
            n = ('%s · %s' % (_UI_KEEP_PREFIX[pre], cut)
                 if pre in _UI_KEEP_PREFIX else cut)
            break
    # ③ 砍掉冒号后面的描述（只留冒号前那截）
    for ch in ('：', ':'):
        if ch in n:
            head = n.split(ch, 1)[0].strip()
            if head:
                n = head
                break
    return n or (name or '').strip()


def build_menu():
    """从 wb_menu.py 读菜单，转成界面用的结构。带 5 秒缓存（按源文件 mtime 失效）。"""
    now = time.time()
    mt = menu_mtime()
    if (MENU_CACHE['data'] and MENU_CACHE['mtime'] == mt
            and now - MENU_CACHE['t'] < 5):
        return MENU_CACHE['data']

    M = menu_module()                                    # 改了 wb_menu.py 会自动热重载

    index = {num: (desc, cmd, risky) for num, desc, cmd, risky in M.MENU + M.EXTRA}

    # ★★ 2026-09-29（用户第 3 条）：顺手把每个 item 按 key 收一份，
    #   作为 /api/menu 的 help —— 前端 ⓘ 直接照 key 取，不用再跑一趟接口。
    HELP_MAP = {}

    def item(num):
        desc, cmd, risky = index[num]
        group, name, sub = _split_desc(desc)
        it = {
            # ★ key = wb_menu.py 里的唯一键（板块前缀 + 板块内序号）。
            #   前端点卡片时**回传的就是它** —— 不要再回传「显示的序号」：
            #   编号重排后每个板块都有 1，单独一个数字分不清是哪个板块的。
            'key': num,
            'num': num,                       # 兼容旧前端：与 key 相同
            'n': M.show_num(num),             # ★ 卡片徽标显示这个（板块内序号）
            'group': group,
            # ★★ 2026-09-29（用户第 3 条）：卡片上只写**精简名**（无板块前缀、无冒号描述）。
            #   ★ 2026-09-29 晚：「同步」板块里那两条**缩写前缀**要留着才分得清本机/跨机
            #     —— 详见 _ui_name 的说明。
            'name': _ui_name(num, name, group),
            'nameFull': name,                 # 完整标题（ⓘ 弹层与排障要用）
            'sub': sub,
            'hint': UI_HINTS.get(num, ''),
            # ★★ 2026-09-29（用户第 3 条）：详细介绍，给卡片右上角那个 ⓘ 用
            'help': UI_HELP.get(num, ''),
            'risky': bool(risky),
            'mode': ('view' if num in VIEW_KEYS
                     else ('term' if num in TERM_NUMS else 'inline')),
            'script': cmd[0] if cmd else '',
        }
        # ⓘ 弹层用的是**完整标题** —— 那里空间大，「跨机同步 · 恢复被同步误删的会话」
        # 这种完整说法反而更清楚。
        rec = dict(it)
        rec['name'] = name
        HELP_MAP[num] = rec
        return it

    groups = []
    # ★ 2026-09-27：GROUPS 由二元组变成了三元组 —— 多出第一个「板块 key」（gkey）。
    #   界面按板块分组渲染，编号又是「板块内序号」，所以 gkey 要一路带到前端，
    #   免得点击时只知道「第 3 项」而不知道是哪个板块的第 3 项。
    for gkey, title, nums in M.GROUPS:
        items = [item(n) for n in nums if n in index]
        if items:
            groups.append({
                'gkey': gkey, 'title': title, 'items': items,
                # ★★ 2026-09-29（用户第 7 条）：这一组是「铺在首页」还是「收成一个按钮」。
                #   'flat'      → 前端把内容直接画出来（不再有分类 tab 这一层）；
                #   'collapsed' → 前端只画一个按钮，点开 view 那个第二层界面。
                #   真值源在 wb_menu.py 的 GROUP_PRESENT（这里不另立一份）。
                'present': getattr(M, 'GROUP_PRESENT', {}).get(gkey, 'flat'),
                'view': GROUP_VIEW.get(gkey, ''),
                # tab 上的短名（砍掉标题里那截括号说明）。后端算好、前端照用 ——
                # 用户第 3 条要的「图标叫：同步」就是这里的 title 首段。
                'tab': (title.split('（')[0].strip() or title),
            })

    extra = [item(num) for num, _d, _c, _r in M.EXTRA]

    # ★★ 2026-09-29（用户要求）：图形界面**不再单列「界面内直达」那一区** ——
    #   a / b / f 三条跨机任务线应急口被收进「更多工具…」面板的「跨机任务线应急」一段
    #   （见 build_tools()，那一段直接读 wb_menu.EXTRA，不另抄命令，所以不会漂移）。
    #   黑窗菜单里 a / b / f 三个字母键**照旧可用**。
    extra_gui = []

    # 界面上「无入口 bat、界面内直达」的快捷操作。
    # ★ 2026-09-28：原来这里唯一的一项 'r'（启动切号守护）**已挪进 wb_menu.py 的 EXTRA** ——
    #   因为黑窗菜单里压根没登记它，在菜单里敲 r 会报「没有这个快捷键」，是个悬空引用。
    # ★ 2026-09-29：'r' 又随 EXTRA 精简一并**取消**（「部署和修复」会重装并拉起该守护，
    #   纯属重复）；这块列表继续留空。
    shortcuts = []

    data = {'groups': groups, 'extra': extra_gui,
            'extraAll': extra,          # 仍下发（排障/以后要用），但前端本轮不画
            'shortcuts': shortcuts,
            # ★★ 2026-09-29：卡片 ⓘ 的详细介绍（键与卡片一致，含 name/group/sub/hint/help）
            'help': HELP_MAP}
    MENU_CACHE.update(t=now, data=data, mtime=mt)
    return data


# ------------------------------------------------------------------ 状态数据

_ANSI = None


def strip_ansi(s):
    global _ANSI
    if _ANSI is None:
        import re
        _ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
    return _ANSI.sub('', s or '')


def _level_of(text):
    """按状态行里的**符号 + 关键字**猜语义，只用于上色。

    ★ 2026-09-27 修 bug（状态条的灯）：
      以前只认符号（● ✓ ○ ◐ ✗）。于是那些**不带符号的状态句**一律落进兜底
      'info' = 灰灯 —— 守护明明在跑，「同步守护」那一格却是灰的（用户当场发现：
      「同步守护在运行中怎么灯不是绿色的」）。同类的还有「切号触发 · 已装」
      （已装也是灰的）、「未装（…）」「读不出来（…）」也分不出颜色。
      现在符号与关键字两路都认。

    ★ 判定顺序是「先坏后好」，不能反：反了的话「未运行」里的「运行」
      会被下面第③条的「运行中」抢先命中，把**停摆**的灯点成绿的。
    """
    t = text or ''
    # ① 坏了：报错 / 读不出来
    if any(k in t for k in ('✗', '失败', '错误', '读不出来', '异常')):
        return 'err'
    # ② 不全好：暂停 / 未部署 / 未装 / 停用 / 判不出
    if any(k in t for k in ('◐', '◑', '⚠', '未部署', '已暂停', '未装', '停用', '判不出')):
        return 'warn'
    # ③ 好的
    if any(k in t for k in ('●', '✓', '运行中', '已装')):
        return 'ok'
    # ④ 关着的（「未运行」与「运行中」互不包含，但必须排在③之后判）
    if any(k in t for k in ('○', '未开启', '未运行', '空（')):
        return 'off'
    return 'info'


# ---- 运行环境自检（2026-09-28 新增）----------------------------------------
# ★ 2026-10-01（第卅一轮）：加 `_ENV_DETAIL` —— 受限时要说清**差在哪**
#   （rename / mkdir / find），否则界面上只有一句「受限」，还得回头翻日志猜。
_ENV_OK = [None]        # 缓存；用 list 当可变容器，省掉一堆 global 声明
_ENV_DETAIL = ['']      # 受限时：被拦住的项（'、' 分隔）


def env_probe():
    """本服务 fork 出的子进程，**能不能干工具真正要干的活**？

    为什么要问这个：这棵进程树**有时**会被系统塞进「代理程序策略」
    （brokered program policy）里 —— 历史发生过（见 _界面.log 里那次
    `--ensure：拉起后台服务`）。那种树里 mkdir / find / grep / cp 会被**直接
    拦掉**，而系统的报错方式极其隐蔽：只在 stderr 留一句
    `Brokered program policy check unavailable`，退出码非 0，但没有任何人
    知道原因在哪。实测代价：
      · 记忆线把 grep 的失败误判成「不在白名单」→ 打印「非法同步项（已拒绝）」
        → exit 3（看着像配置坏了）；
      · 跨机任务线报 `Operation not permitted`，250MB 的导出白跑一趟；
      · 最坏的一种是「假成功」：退出码 0、日志照打「同步完成」，一个文件没搬。

    ★★ 2026-10-01（第卅一轮）**判据升级 —— 血泪教训**：
      原来这里只拿 `/usr/bin/true` 打一枪。实测证明**这一枪打得太轻**：
      那棵坏树里 `/usr/bin/true` 跑得动（于是自检一路报「正常」），
      而工具真正依赖的两件事全被拦，用户看到的就是「点按钮每次都失败」：
        · 起子进程跑 `mkdir` / `find` → `Brokered program policy check unavailable`
          → 「一键整合」第 2 步记忆线 exit 5、总结写「成功 2 / 失败 1」；
        · Python 的 `os.replace`（`rename(2)`）→ `Operation not permitted`
          → 「部署和修复」第 5 步退出码 1、第 6 步退出码 2；
          顺带每次失败白留一份 250MB 的 `.tmp`（实测攒了 988MB）。
      所以现在**按「真活」逐项试**，任何一项不过就判「受限」并把**差在哪**记下来。

    ★ 修法：`_排障与验证脚本/_restart_ui_service.py` 重启 = 换一棵干净的树；
      最稳的是**让外壳 app 拉服务**（`pkill -f WorkBuddyShell` 后 `open -a
      "WorkBuddy任务管家.app"` —— 外壳由 LaunchServices 拉起，树是干净的）。
    ★ 只测一次并缓存：/api/status 是每 3 秒轮询一次的，不能每轮都 fork 子进程。
    """
    if IS_WIN:
        return True
    if _ENV_OK[0] is None:
        bad = []
        # ① 基础：起得起子进程（老判据，保留）
        try:
            if subprocess.call(['/usr/bin/true'],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) != 0:
                bad.append('起子进程')
        except Exception:
            bad.append('起子进程')
        # ② 真活一：工具目录里能不能**改名**（所有「先写 .tmp 再 os.replace」都靠它）
        #   ⚠️ 故意**不叫 .tmp**：受限树里 rename 失败后连 remove 也可能失败，
        #     留一个叫 .tmp 的探针文件会跟真正的「失败残留」混在一起，看日志时误导。
        probe = os.path.join(HERE, '_envprobe_%d.a' % os.getpid())
        try:
            with open(probe, 'w') as f:
                f.write('x')
            os.replace(probe, probe + '.b')
            os.remove(probe + '.b')
        except Exception:
            bad.append('rename')
            for p in (probe, probe + '.b'):
                try:
                    os.remove(p)
                except OSError:
                    pass
        # ③ 真活二：外部命令 mkdir / find（记忆线的搬运全靠它们）
        tdir = os.path.join(HERE, '_envprobe_dir_%d' % os.getpid())
        for exe, args, label in (('/bin/mkdir', [tdir], 'mkdir'),
                                 ('/usr/bin/find', [HERE, '-maxdepth', '0'], 'find')):
            if not os.path.exists(exe):
                continue
            try:
                if subprocess.call([exe] + args,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL) != 0:
                    bad.append(label)
            except Exception:
                bad.append(label)
        try:
            os.rmdir(tdir)
        except OSError:
            pass

        _ENV_DETAIL[0] = '、'.join(bad)
        _ENV_OK[0] = not bad
        if bad:
            log('⚠ 运行环境自检失败：%s 被系统策略拦住了 —— 同步/搬运类任务会'
                '「假成功」或报 Operation not permitted；'
                '跑 _排障与验证脚本/_restart_ui_service.py 重启服务可修'
                '（最稳：先 pkill -f WorkBuddyShell，再 open -a "WorkBuddy任务管家.app"）'
                % _ENV_DETAIL[0])
    return _ENV_OK[0]


def env_state():
    """给界面/状态接口用的一句话结论。

    ★ 2026-09-30（第廿七轮）：多带一个 `short` —— 「运行环境」这一格这一轮起
      进了首页状态板（板改成两列网格，正好六格），格子里只放得下两三个字。
    ★ 2026-10-01（第卅一轮）：受限时**把「差在哪」写出来**（rename / mkdir / find …）——
      只说「受限」还是会让人去猜，而这次的原因恰恰很具体。
    """
    if env_probe():
        return {'text': '正常', 'short': '正常', 'level': 'ok'}
    return {'text': '⚠ 受限环境（%s 被拦）：同步/部署类按钮会失败 —— '
                    '重启服务可修' % (_ENV_DETAIL[0] or '外部命令'),
            'short': '受限', 'level': 'warn'}


# ---------------------------------------------------------------- 状态快照预热
#
# ★★ 2026-09-29（用户：「所有按钮点下去都要等半秒才反应，不够丝滑」）
#
# 病根：`/api/status` 实测每次要 **86~113 ms**（中位 93 ms）。拆开看，钱都花在
#   「起子进程问系统状态」上：`wb_state()` → `pgrep -f <长路径>` 15ms；
#   `sync_summary()` → 三条线各一次 `launchctl print` 共 ~11ms；合计 4~7 个子进程。
#   而这个接口被**每 3 秒轮询一次**，用户点「状态与设置」「刷新」时又各拉一次 ——
#   于是每次点击都要现算 90ms，叠上轮询就是「点完等半秒」。
#
# 试过的错路（留个记号，别再走）：`pgrep -c` 看着只要 2ms，其实是 macOS 不认这个
#   选项、**直接报 usage 退出**（rc=2）—— 用它判「有没有在跑」会永远得到「没跑」。
#   `pgrep -q` 一样要 14ms：慢在**匹配**本身，不在格式化输出。
#
# 修法：后台线程把状态**预先算好**放进快照，请求直接读快照（≈0ms）。状态是秒级
#   变化的东西，慢一两秒对用户完全无感；但点击的响应从 90ms 变成 ~5ms。
#   ⚠️ 刻意**只**缓存 build_status()：`build_power()`（5ms）和 `jobs`（0ms）留在
#      请求里现算 —— 倒计时、运行指示这类要实时，不能被缓存拖住。
#   ⚠️ 只在「最近有人看」时才预热：窗口关掉十分钟后就不烧 CPU 了（见 _status_warmer）。
_STATUS_SNAP = {'v': None, 't': 0.0, 'hit': 0.0}
_STATUS_FRESH = 10.0      # 快照超过这么久没人碰就作废；也是「有没有人在看」的窗口
_STATUS_WARM_SEC = 2.0    # 热态期间的续命周期（比页面 3 秒轮询稍快，保证每次都命中）
# ★★★ 2026-10-01（自检轮 · 用户第 1 条「不要一直轮巡监测」）：
#   上面那个预热线程原来是 `while True: … time.sleep(2.0)` —— **没人看也每 2 秒醒一次**，
#   一天 43200 次，只为了「万一有人来问，快一点」。现在改成**事件驱动**：
#   `build_status()`（每次真有人来问）会 `_STATUS_WAKE.set()`，线程只在「最近 10 秒
#   内确实有人问过」的那段时间里每 2 秒续一次热；一旦没人问，它 `wait()` **无限期睡着**
#   —— 空闲时**零唤醒、零 CPU**。功能一模一样，热态照旧。
_STATUS_WAKE = threading.Event()


def _snap_refresh():
    _STATUS_SNAP['v'] = _build_status_direct()
    _STATUS_SNAP['t'] = time.time()


def _status_warmer():
    """预热线程：**有人来问才醒**，把状态快照保持在热态；没人问就无限期睡着。

    ★ 原来这里是「每 2 秒醒一次、顺便看一眼有没有人在看」—— 用户明确反感这种
      「一直挂着的监测」。现在是纯事件驱动：
        · 每次 `/api/status` 请求都会 `_STATUS_WAKE.set()` 把它叫醒；
        · 它醒来后，只要 `_STATUS_FRESH`（10 秒）窗口内还有人问，就每 2 秒续一次热；
        · 窗口一过（没人看了）→ 回到 `wait()`，**一次都不再醒**。
      也就是「页面关掉之后，这里一觉睡到下次有人点开」。
    """
    while True:
        try:
            _STATUS_WAKE.wait()          # ← 没人来问就在这里睡着，零唤醒
            _STATUS_WAKE.clear()
            # 热态续命：只在「刚才确实有人看」的窗口期内才继续烧这一段
            while time.time() - _STATUS_SNAP['hit'] <= _STATUS_FRESH:
                _snap_refresh()
                time.sleep(_STATUS_WARM_SEC)
        except Exception as e:
            log('状态预热失败：%r' % (e,))
            time.sleep(_STATUS_WARM_SEC)


def build_status():
    """读预热好的状态快照。快照过期／还没建好就现算一次（并顺手填上）。"""
    _STATUS_SNAP['hit'] = time.time()
    _STATUS_WAKE.set()          # ★ 叫醒预热线程：有人在看，把这份快照续热
    v = _STATUS_SNAP['v']
    if v is None or (time.time() - _STATUS_SNAP['t']) > _STATUS_FRESH:
        _snap_refresh()
        v = _STATUS_SNAP['v']
    return v


def _short_status(t, n=9):
    """状态板那一格用的**短值**（只给界面顶那块板用，`text` 一个字不动）。

    ★★ 2026-09-30（第廿七轮 · 用户第 5 条）：状态板从「一串宽窄不一的胶囊」改成
      **两列对齐的网格**（每格等宽、上下对齐），于是值必须短。规则按顺序：
        ① 砍掉第一层括号及其内容 —— 「运行中（端口 17803）」→「运行中」、
           「已装（切号后同步任务（全局单份））」→「已装」；
        ② 砍掉逗号/分号后面的尾巴 —— 「229.1 MB，导出于 09-30 22:03」→「229.1 MB」；
        ③ 仍超 n 个字 → 截断加省略号。
    ⚠️ 只影响状态板；「状态与设置」页与菜单栏面板显示的还是**全文**。
    """
    t = (t or '').strip()
    for a, b in (('（', '）'), ('(', ')')):
        i = t.find(a)
        if 0 < i < len(t) - 1:
            # ⚠️ 必须**按层配对**找右括号。第一版用的是 `t.find(b, i+1)`，
            #   于是「已装（切号后同步任务（全局单份））」只砍到内层那一个
            #   `）`，留下尾巴「已装）」—— 当场被这条自检抓出来。
            depth, j = 0, -1
            for k in range(i, len(t)):
                if t[k] == a:
                    depth += 1
                elif t[k] == b:
                    depth -= 1
                    if depth == 0:
                        j = k
                        break
            if j > i:
                head, inner = t[:i].strip(), t[i + 1:j].strip()
                # 「空（还没有待同步的会话）」→ 只剩一个「空」字没人看得懂，
                # 这种**头太短**的反而要留括号里的说明；其余一律以头为准。
                t = inner if (head in ('空', '无') and inner) else head
                break
    for ch in ('，', ',', '；', ';'):
        i = t.find(ch)
        if i > 0:
            t = t[:i].strip()
            break
    if len(t) > n:
        t = t[:n] + '…'
    return t


def _trigger_short(t):
    """「切号触发」那一格：短，但仍看得出**装的是哪条任务**。

    ★ 用户给的样例就是这一条：「可以让类似这种长描述的状态显示，字数少一些，
      比如『切号同步单份任务』」。所以这里不是简单砍括号 ——
      已装 + 名字里有「单份」→「已装单份任务」（6 字，绿灯照样由「已装」点亮）。
    """
    t = (t or '').strip()
    if '已装但被停用' in t:
        return '已装但停用'
    if t.startswith('已装'):
        if '单份' in t:
            return '已装单份任务'
        if '全局' in t:
            return '已装全局任务'
        return '已装'
    if t.startswith('未装'):
        return '未装'
    return _short_status(t)


def _build_status_direct():
    M = menu_module()                                     # noqa

    def mk(fn, *a):
        try:
            t = strip_ansi(fn(*a)).strip()
        except Exception as e:
            t = '读不出来（%s）' % e
        # ★ 廿七轮：多带一个 `short`（状态板格子用）。前端 bdItem() 优先用它。
        return {'text': t, 'level': _level_of(t), 'short': _short_status(t)}

    out = {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        # ★ 2026-09-27：原来只有「运行中 → ok，其余一律 off」，于是「判不出」
        #   （读不到 WorkBuddy 进程）也点灰灯，看着就像「没在跑」。改成三态：
        #   运行中=绿、未运行=灰、判不出=黄（不知道 ≠ 没开，别让用户看错）。
        # ★★★ 2026-10-11（拆家）：wb / guard / trigger / lines / linesArr / share
        #   这几项（WorkBuddy 状态、切号同步守护、同步线…）全属任务管家，已删。
        'uiVer': page_ver(),
        'menuVer': menu_ver(),
        # ★ 2026-09-28：本服务的进程树能不能执行外部程序（见 env_probe 的说明）。
        #   受限时一键整合里的记忆线会误报「非法同步项」或干脆假成功，
        #   这里把它摆到状态里，界面/排障脚本一眼能看见。
        'env': env_state(),
        # ★★ 2026-09-29：正在跑的内嵌任务 —— 前端拿它恢复卡片右侧的运行指示
        #   （窗口重载后前端内存会丢，靠这条 3 秒一次的轮询自己续上）。
        'jobs': running_jobs_snapshot(),
    }
    try:
        out['paths'] = {'here': HERE}
    except Exception:
        out['paths'] = {'here': HERE}
    return out


# ★ 开机自愈只做一次（2026-09-28）。
#   用户要求：重启之后不要还记着上次「跑完自动关机」是开着的。
#   放在 /api/status 里做「惰性自愈」——服务一被打开界面就会调它，
#   比塞进启动流程更可靠（服务可能是被 launchd / 外壳 app 拉起来的）。
#   用内存变量节流：同一个服务进程里只真跑一次 `--heal`。
_HEALED_BOOT = [None]


def heal_once():
    """打开界面后的第一次轮询：把「上次开机留下的」状态收干净。

    ★ 为什么不每次轮询都调：`heal_boot_state` 要读 kern.boottime（起一个
      sysctl 子进程），3 秒一次太浪费；而且每次开机只需要判断一次。
    """
    try:
        import wb_powerguard as PG
        bt = PG.boot_time()
        if not bt:
            return
        if _HEALED_BOOT[0] == bt:
            return                      # 这次开机已经做过了
        _HEALED_BOOT[0] = bt
        conf = PG.load_conf()
        changed, what = PG.heal_boot_state(conf, full=False)
        if changed:
            log('开机自愈（打开界面时触发）：%s' % '；'.join(what))
    except Exception as e:
        log('开机自愈失败：%r' % (e,))


def _features_payload(F, conf, batt=False):
    """把「5 个开关」变成前端能直接画的列表。每项自带 `key`（点它执行哪条菜单项）。

    ★ 顺序 = 界面上从上到下；`sub=True` 的往右缩进一格；`child=True` 的再缩一格
      （PommeToys 的「合盖后内建显示器保持点亮」就是挂在「合盖后保持唤醒」下面的子项）。
    ★ `key` 从 POWER_GUI 的 toggles 取（菜单唯一键，不许前端写死）；第 5 个开关
      没有菜单编号，用界面专用键 `toggle_lid_display`。
    ⚠️ `batt` 由调用方传进来（`build_power` 里才有 `PG` 这个局部名）——
      这里**不要**自作主张去 `import wb_powerguard`：第一版就是这么写的，
      结果 `PG` 不在作用域、被下面的 except 静默吞掉，标签一直显示「需要电池」。
    """
    # ★★ 2026-09-29 修：原来这里写的是 `gui()`，而本模块**没有** `gui` 这个名
    #   字（真名是 `build_gui()`，见下面）。于是每次调用都抛 NameError，被下面
    #   那个宽 except 一口吞掉 → `tog` 永远是空的 → 一直走**硬编码退路**
    #   `['power1'..'power4']`。后果很隐蔽：上面那段 docstring 说「key 从
    #   POWER_GUI 的 toggles 取，不许前端写死」，而事实上它从来没读过 POWER_GUI；
    #   哪天再动一次编号，界面上的开关就会**静静地点到旧键**上去。
    #   （本项目的老坑：同一条信息存了两份 = 迟早漂移。这次由 _e2e_switches 的
    #     A3d 项暴露 —— 它拿 `wb_ui.gui()` 去核对，才发现这个名字不存在。）
    #   ⚠️ 异常仍然兜住，但**必须留痕**：静默退路是这类 bug 的温床。
    try:
        tog = [str(x) for x in (build_gui().get('toggles') or [])]
    except Exception as e:
        log('读 POWER_GUI 的 toggles 失败，退回内置键序：%r' % (e,))
        tog = []
    while len(tog) < 4:
        # 退路（gui() 没给够时）—— ★ 2026-09-29 重排后是 power1~power4
        tog.append(['power1', 'power2', 'power3', 'power4'][len(tog)])
    thr = int(conf.get('lowBattPct') or 10)
    # ★★★ 2026-10-01（第卅五轮 · 用户）：新增 `panel` 一栏 —— **这个开关要不要
    #   出现在菜单栏面板里**。用户原话「你在菜单栏的小界面上同时放了『防休眠』
    #   『合盖后保持唤醒』『屏幕保持常亮』以及『完成后自动关机』这几个开关，
    #   不必把它收进去了」→ 面板只留**常拨的这几个**；其余（电量不足时自动停、
    #   合盖后内建显示器保持点亮）只在大界面的监测页里。
    #   ⚠️ 这个判断**必须放在后端**：前端有一条老规矩 —— 它不认识任何开关的名字
    #      （加减开关不用改 _ui.html）。所以只能在数据里带标记，不能在界面里按名字筛。
    # ★★★ 2026-10-05（用户第 8 条）：门槛判据**只在 `wb_powerguard.feat_gates()` 一处**，
    #   这里只取结果。两项门槛：① 没有电池；② **本机已经合着盖**（事件驱动）。
    #   ⚠️ 取不到时**退回「不置灰」**（宁可让它可点，也别误禁）。
    try:
        import wb_powerguard as _PGg
        _gates = _PGg.feat_gates(batt)
    except Exception as e:
        log('读「开关门槛」（feat_gates）失败，退回不置灰：%r' % (e,))
        _gates = {}
    no_batt, na_why = _gates.get('lowBattStop', (False, ''))
    lid_na, lid_why = _gates.get('lidClose', (False, ''))
    lis_na, lis_why = _gates.get('lidDisplayOn', (False, ''))
    return [
        {'id': 'awake', 'key': tog[0], 'on': bool(F.get('awake')), 'sub': False,
         'panel': True,
         'label': '防休眠', 'tag': '总开关',
         'desc': '开着时，Mac 不会自行进入睡眠。'},
        {'id': 'display', 'key': tog[1], 'on': bool(F.get('keepDisplayOn')), 'sub': True,
         'panel': True,
         'label': '屏幕保持常亮', 'tag': '子开关',
         'desc': '显示器保持唤醒状态。'},
        {'id': 'lowbat', 'key': tog[2], 'on': bool(F.get('lowBattStop')), 'sub': True,
         'panel': False, 'na': no_batt, 'naWhy': na_why,
         'label': '电量不足时自动停', 'tag': ('子开关' if not no_batt else '本机无电池'),
         'desc': '用电池且低于 %d%% 时结束，让睡眠保住电量。' % thr},
        {'id': 'lid', 'key': tog[3], 'on': bool(F.get('lidClose')), 'sub': True,
         'panel': True, 'na': lid_na, 'naWhy': lid_why,
         'label': '合盖后保持唤醒',
         'tag': ('本机无电池' if no_batt else ('轻薄本' if batt else '需要电池')),
         'desc': '合盖后 Mac 不进入睡眠。请注意散热。'},
        {'id': 'lid_display', 'key': 'toggle_lid_display',
         'on': bool(F.get('lidDisplayOn')), 'sub': True, 'child': True,
         'panel': False, 'na': lis_na, 'naWhy': lis_why,
         'label': '合盖后内建显示器保持点亮', 'tag': '子项',
         'desc': '合盖后不再关闭内建显示器。⚠️ 本机给不了"亮度自动调至最低"'
                 '（老亮度接口在 Apple Silicon 上已失效），所以亮度保持不变。'},
    ]


def _feat_short(F):
    """状态板那一格用的**极短**版开关组合（见 feat_desc）。

    ★ 2026-09-30（第廿七轮）：首页状态板改成一格一格的等宽网格，放不下
      「防休眠 + 屏幕常亮 + 合盖也不睡 + 合盖保持点亮 + 电量低于 10% 自动停」那种长句。
      这一格只需要回答「现在挂着哪几条」：一律以「防休眠」起头，后面最多带一项
      （完整那句仍在 levelDesc 里，卡片和设置页照常显示）。
    """
    if not F or not F.get('awake'):
        return '未开'
    if F.get('lidClose'):
        return '防休眠＋合盖'
    if F.get('keepDisplayOn'):
        return '防休眠＋常亮'
    return '防休眠'


def build_power():
    """运行监测的详细状态（给界面上的档位按钮用）。纯只读。

    ★ macOS（2026-09-27 起）：本机**也有**这一组了。
      早先这里对 Mac 直接返回 `unsupported`，理由是「Windows 版 wb_powerguard
      是纯 Win32（SetThreadExecutionState + 改电源方案），Mac 跑不了」。
      现在补了平台层的 Mac 实现（`caffeinate` / `pmset` / `osascript`，见
      wb_powerguard.py 的 docstring 对照表），**字段与 Win 版逐字一致**，
      所以这一份代码可以原样给两端用 —— 不再需要分叉。
    """
    try:
        import wb_powerguard as PG
        conf = PG.load_conf()
        st = PG.read_state()
        alive = PG.state_alive(st)
        F = PG.feats(conf)
        lvl = F['level']
        d = {
            'enabled': bool(conf.get('enabled')),
            'alive': bool(alive),
            'level': lvl,
            'levelDesc': PG.feat_desc(F),
            # ★ 廿七轮：状态板那一格用的极短版（见 _feat_short）。
            'levelShort': _feat_short(F),
            # ★★ 2026-09-29：把 5 个开关的**真值**和一份「开关描述表」一起下发。
            #   为什么给描述表：一套前端服务两端，且"开关有哪些、叫什么、谁是子项"
            #   属于**后端知识**（它决定点下去执行哪条命令）。前端照着画就行，
            #   以后加减开关**一行前端都不用改** —— 写死就会点错东西，
            #   这个教训在本项目已经吃过一次（档位按钮按占位编号绑事件）。
            'features': _features_payload(F, conf, PG.has_battery()),
            'feat': {k: bool(F.get(k)) for k in PG._FEAT_KEYS},
            'lowBattPct': int(conf.get('lowBattPct') or 10),
            'busy': bool(st.get('busy')),
            'pid': st.get('pid'),
            'autoShutdown': bool(conf.get('autoShutdown')),
            'shutdownPending': bool(st.get('shutdownPending')),
            'shutdownLeft': max(0, int((st.get('shutdownAt') or 0) - time.time()))
                            if st.get('shutdownPending') else 0,
            # ↓ 附加二「到点自动结束」（与 autoShutdown 互斥，见 wb_powerguard.cmd_autostop_set）
            'autoStopAt': float(conf.get('autoStopAt') or 0),
            'autoStopSetAt': float(conf.get('autoStopSetAt') or 0),
            'autoStopDurationSec': int(conf.get('autoStopDurationSec') or 0),
            'idleConfirmSec': conf.get('idleConfirmSec'),
            # ↓ 给界面上的「跑完自动关机」状态灯用。
            #   界面 3 秒才轮询一次，光靠 shutdownLeft 会一跳一跳；
            #   给绝对时间戳（now / lastBusyAt / shutdownAt）让前端本地走秒。
            'activityWindowSec': conf.get('activityWindowSec'),
            'shutdownDelaySec': conf.get('shutdownDelaySec'),
            # ★ 2026-10-11（A6）：灯面要说「信号读不到多久后兜底关机」。
            'unknownShutdownSec': conf.get('unknownShutdownSec'),
            'now': time.time(),
            'lastBusyAt': float(st.get('lastBusyAt') or 0),
            'busySince': float(st.get('busySince') or 0),
            'shutdownAt': float(st.get('shutdownAt') or 0),
            'everBusy': bool(st.get('everBusy')),
            'lastSignals': st.get('signals') or st.get('sig') or '',
            # ★ 平台标识：界面上有几句文案要分平台（例：档位三「合上盖子」只对笔记本；
            #   Mac 的关机走 AppleScript、首次要授一次权）。给前端，不让它去猜 UA。
            'platform': 'mac' if IS_MAC else ('win' if IS_WIN else 'other'),
            # ★ 写配置/状态失败的原因（空串 = 一切正常）。
            #   2026-09-28 加：这类失败原来只写进日志，界面完全不知道 ——
            #   用户看到的是「按了没反应、开关关不掉」，而日志里其实写得很清楚。
            'writeError': (PG.last_write_err() if hasattr(PG, 'last_write_err') else ''),
        }
        try:
            d['hasBattery'] = PG.has_battery()
        except Exception:
            d['hasBattery'] = None
        return d
    except Exception as e:
        log('读运行监测状态失败：%r' % (e,))
        return {'error': str(e)}


# ------------------------------------------------------------------ 界面编号映射

# 界面上那张「运行监测」卡片里的按钮，点下去等于执行某个**菜单编号**。
# ★ 为什么由后端下发、不在前端写死：一套前端服务两端，编号一旦分叉就会点错东西。
# ★ 2026-09-27 编号重排后：macOS 的值已换成新键（power1~power7），
#   Windows 仍是 11~16。两端**不再相同**，但机制不变 —— 仍然后端下发、
#   前端不写死，所以以后哪一端再调整，都不用回头动 _ui.html。
#   ⚠️ 这里给的是「菜单唯一键」，不是显示序号。前端拿它当 data-key 回传。
POWER_GUI = {
    'win':   {'panel': '11', 'levels': ['12', '13', '14'], 'stop': '16'},
    # ★ 2026-09-29：`levels`（档位 1/2/3）退役 → `toggles`（那排开关的菜单键）。
    #   前端**不认这两个字段名**了 —— 它画的是 build_power() 下发的 `features`
    #   （每项自带 key），所以这里保留 toggles 只为排障时一眼看出对应关系。
    #   ⚠️ 第 5 个开关「合盖后内建显示器保持点亮」**没有菜单编号**（是个嵌套子项，
    #      只在图形界面里），它走界面专用键 `toggle_lid_display`。
    # ★★ 2026-09-29 二次重排：原来那条「状态与设置面板」的菜单项（power1）取消了，
    #   所以开关的菜单键整体上移一位（power2~5 → power1~4），停止从 power8 → power7。
    #   `panel` 这个键**一起删掉**：它原本指向 power1（交互式文字面板），而那个面板
    #   已经让位给窗口右上角的「状态与设置」视图（见 build_settings）。
    'mac':   {'toggles': ['power1', 'power2', 'power3', 'power4'],
              'stop': 'power7'},
}

# ★ 2026-09-28：Mac 上「打开文字面板」**不再弹终端**，改成把完整状态内嵌在
#   界面里显示。原因（用户实测报障）：
#     power1 走的是 job_in_terminal（另开终端跑 `wb_powerguard.py --panel` 那个
#     交互菜单）。实测下来 open 确实成功、`.command` 也正确，但只要窗口没落到
#     用户眼前（Terminal 冷启动时的「恢复窗口」、或落在别的 Space），用户看到的
#     就是「点了不让打开」。而这个按钮的**唯一目的就是看状态**，为此去开一个
#     终端本来就不划算。
#   现在：点它 = 内嵌跑 `--status --full`（只读、非交互），结果直接显示在下面的
#   输出面板里，一定能看到。真要键盘菜单，右上角有「编号菜单」。
#   ⚠️ 它是一个**界面专用键**（不是菜单项），在 do_run 里有专门分支。


def build_gui():
    """界面按钮 → 菜单编号的映射（见 POWER_GUI 的说明）。"""
    g = dict(POWER_GUI['mac' if IS_MAC else ('win' if IS_WIN else 'win')])
    return g


# ---------------------------------------------------- 音效（三套 + 静音开关）

# ★★ 2026-09-29（用户要求：「增加选择音效的功能，三个音效都要……要加静音开关」）。
#   音效文件是我们**自己用纯标准库合成**的（_gen_sounds.py 生成 → _assets/sounds/），
#   不引用任何第三方 App 的资源 —— 只是"风格上"照 PommeToys 那种 UI 提示音的做法：
#   每个交互动作一声很短的"哒"，三套可选、可静音。
# ★★★ 2026-10-01（第卅五轮）：音色整套重做。用户说「提示音能做成和 POMMETOYS
#   一样的吗 我觉得他的提示音好听」—— 把 PommeToys 那 45 个明文 wav 量了一遍，
#   病根是**我们的音拖得太长、又太闷**（旧版可闻尾 44/69/110ms、频谱重心
#   2.7k/4.7k/0.4kHz；PommeToys 是 4~12ms 就收干净、重心 4.8~6.5kHz）。
#   现在三套的指标是对着 PommeToys 的实测值逐项对齐的，判据见
#   `_gen_sounds.py --report`（超区间会报 ✗）。文案也跟着改准。
SOUND_DIR = os.path.join(HERE, '_assets', 'sounds')
# ★★ 2026-09-29：界面字体 —— PommeToys 用的那一款（Instrument Sans，SIL OFL，
#   许可允许自由使用与嵌入）。整站都是它，中文回退苹方（见 _ui.html 的 --sans）。
FONT_DIR = os.path.join(HERE, '_assets', 'fonts')
SOUND_SETS = [
    ('1', '清脆', '一声极短的「嗒」，干脆利落 —— 最接近系统按钮声，默认'),
    ('2', '机械', '继电器 / 棘轮的「咔」，短促有质感'),
    ('3', '柔和', '短而温和的「咚」，夜里安静时用'),
]


def sound_files(sid):
    return {'click': 'set%s-click.wav' % sid, 'done': 'set%s-done.wav' % sid}


def sound_list():
    out = []
    for sid, name, desc in SOUND_SETS:
        f = sound_files(sid)
        out.append({
            'id': sid, 'name': name, 'desc': desc,
            'click': '/sounds/%s' % f['click'],
            'done': '/sounds/%s' % f['done'],
            'ok': os.path.exists(os.path.join(SOUND_DIR, f['click'])),
        })
    return out


def sound_prefs():
    """音效偏好（存 `_界面.json`，与窗口几何同一个文件）。"""
    c = load_conf()
    sid = str(c.get('soundSet') or '1')
    if sid not in [s[0] for s in SOUND_SETS]:
        sid = '1'
    return {'on': bool(c.get('soundOn', True)), 'set': sid}


def save_sound_prefs(d):
    c = load_conf()
    if 'soundOn' in d:
        c['soundOn'] = bool(d.get('soundOn'))
    if 'soundSet' in d:
        sid = str(d.get('soundSet'))
        if sid in [s[0] for s in SOUND_SETS]:
            c['soundSet'] = sid
    save_conf(c)
    return sound_prefs()


# ------------------------------------------------------------------ 「鼠标」
#
# ★★ 2026-09-30（用户第 2 条）：首页新增一个「鼠标」板块，照 PommeToys 的样子：
#   两个开关（平滑滚动 / 反转方向）+ 一条拉动式速度滑块。
#
# 为什么**单独一个文件**（`_鼠标.json`）而不是塞进 `_界面.json`：
#   真正干活的是**外壳**（WBShell.swift 里的 CGEventTap 平滑滚动引擎）——
#   它需要一个「整份读完就能直接用」的配置，而且要能**只盯这一个文件**做事件通知
#   （kqueue/DispatchSource，不轮询）。`_界面.json` 里混着窗口几何，外壳自己每次
#   拖窗都会写它 → 拿它当配置源会白白误触发。分开就干净了。
#
# ⚠️ 这个文件**就地覆盖写**（先写 tmp 再 rename 会换 inode，外壳那边的 kqueue
#   就盯不住了）。文件很小、一次 write 就落完，读一半的风险可忽略。
MOUSE_FILE = '_鼠标.json'
# 速度档位：条状滑块就地取这几档。★ 2026-09-30（用户第 3 条）：格子从 0.25 改细到
# **0.05**，范围仍是 0.5×~3.0× → 51 档。默认 1.5×，与 PommeToys 的
# `smoothScroll.tuning.v1 → speedSlider` 现值一致。
# ⚠️ 改这里**必须同步** `_ui.html` 的 `MS_STEPS` —— 两处一份事实，`_chk_mouse.py`
#    第 [2] 段逐项比对，漂了会当场报错。
SMOOTH_SPEEDS = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05,
                 1.1, 1.15, 1.2, 1.25, 1.3, 1.35, 1.4, 1.45, 1.5, 1.55, 1.6, 1.65,
                 1.7, 1.75, 1.8, 1.85, 1.9, 1.95, 2.0, 2.05, 2.1, 2.15, 2.2, 2.25,
                 2.3, 2.35, 2.4, 2.45, 2.5, 2.55, 2.6, 2.65, 2.7, 2.75, 2.8, 2.85,
                 2.9, 2.95, 3.0]
SMOOTH_DEFAULT_SPEED = 1.5


def mouse_path():
    return os.path.join(HERE, MOUSE_FILE)


def _near_speed(v):
    """把任意数值吸附到最近的一档（认不出来就回默认档）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return SMOOTH_DEFAULT_SPEED
    return min(SMOOTH_SPEEDS, key=lambda s: abs(s - f))


def mouse_prefs():
    """读「鼠标」的各项。文件不在 / 读坏了 → 回一套默认（不报错、不抛出）。"""
    d = {'smooth': False, 'reverse': False, 'speed': SMOOTH_DEFAULT_SPEED,
         'dockMin': False}
    try:
        with open(mouse_path(), encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            d['smooth'] = bool(raw.get('smooth', d['smooth']))
            d['reverse'] = bool(raw.get('reverse', d['reverse']))
            d['speed'] = _near_speed(raw.get('speed', d['speed']))
            # ★ 第卅七轮（2026-10-01 用户第 3 条）：点 Dock 图标最小化 —— 鼠标栏目下第二个功能。
            d['dockMin'] = bool(raw.get('dockMin', d['dockMin']))
    except Exception:
        pass
    return d


def save_mouse_prefs(part):
    """只覆盖传进来的字段（读-改-写整份），返回存好之后的全量。"""
    d = mouse_prefs()
    if 'smooth' in part:
        d['smooth'] = bool(part.get('smooth'))
    if 'reverse' in part:
        d['reverse'] = bool(part.get('reverse'))
    if 'speed' in part:
        d['speed'] = _near_speed(part.get('speed'))
    if 'dockMin' in part:                    # ★ 第卅七轮：点 Dock 图标最小化
        d['dockMin'] = bool(part.get('dockMin'))
    try:
        with open(mouse_path(), 'w', encoding='utf-8') as f:   # ← 就地写，别 rename
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log('写 %s 失败：%r' % (MOUSE_FILE, e))
        return d
    return d


def mouse_status():
    """`mouse_prefs()` + 外壳回报的**实况**（权限有没有、引擎开没开）。

    ★ 那两个值由 WBShell.swift 写在 `_界面.json` 里（`mousePerm` / `mouseEngine`）——
      只有外壳知道自己有没有拿到「辅助功能」权限。**没这个键就回 None**（＝还不知道），
      不要回 False：那会让界面在「外壳压根没跑」时冤枉地说「缺权限」。
    """
    d = mouse_prefs()
    c = load_conf()
    d['perm'] = c.get('mousePerm') if 'mousePerm' in c else None
    d['engine'] = c.get('mouseEngine') if 'mouseEngine' in c else None
    # ★ 第卅七轮：点 Dock 图标最小化是**另一个独立引擎**（各挂各的事件 tap），
    #   所以实况要另报一个键 —— 它和平滑滚动可以一个开一个关。
    d['dockMinEngine'] = c.get('dockMinEngine') if 'dockMinEngine' in c else None
    return d


# ------------------------------------------------------------ 「键盘」（第卅二轮）
#
# ★★ 2026-10-01（用户第 1 条）：把 Karabiner-Elements 对本机做的**全部**键盘映射
#   与 Command X-V 的 Finder 剪切增强，收进「键盘」栏目。
#
# 分工（与「鼠标」一字不差）：
#   · 本进程只做**配置的读写与翻译**（`wb_keymap.py`）—— 它把 `~/.config/karabiner/
#     karabiner.json` 里的 simple_modifications + 55 条 complex rules 一次性搬过来，
#     规则体**照抄 Karabiner 的 JSON 形状**；
#   · 真正干活的是外壳里的 `KeyEngine`（CGEventTap），它盯着 `_键盘.json`、
#     文件一改就重挂引擎 —— 与 MouseEngine 同一套基建。
#
# ⚠️ 落盘走 `wb_keymap.save()`（**原子写**）。外壳那边的 DispatchSource 对 rename
#   是**会重挂**的（`watchKeyConf()` 里 `.delete/.rename` 那一支），所以原子写没问题。
#   这一点与 `_鼠标.json`（必须就地写）**不同**，别照抄错了。
#
# ⚠️ 引擎默认是**关**的。绝不能因为用户点开一次「键盘」页就把他的键盘改了 ——
#   `blank()` 里 `enabled: False`，`readKeyConf()` 读不到文件也回「全关」。


def _keymap_mod():
    """懒加载 wb_keymap（与 `import wb_menu as M` 同一套做法，见上面第 995 行）。"""
    if 'wb_keymap' not in sys.modules:
        import wb_keymap                                        # noqa: F401
    return sys.modules['wb_keymap']


def _apply_key_item(it, patch):
    """按 id 就地改一条：开关 / 触发键 / 目标键 / 触发键的修饰键。

    ⚠️ 只改传进来的字段 —— 界面每点一下就回一小段，不能整条覆盖。

    ★★ 2026-10-01（第卅三轮）：新增 `fromMods`。界面上改键不再是「从清单里挑一个键」，
    而是**点一下、直接在键盘上按出新的组合**（如 `Ctrl+Shift+V`）—— 捕获到的是
    「键 + 修饰键」两样，所以必须能一起写回去。只改 key_code 不改 mandatory 的话，
    用户按出来的组合里那几个修饰键会被无声吞掉：
    界面显示 `Ctrl+Shift+V`、实际引擎仍按原来的 `Ctrl+V` 去匹配。
    ★ 2026-10-18 晚（用户：「这俩功能给改成默认的按键，我改不回来了」）：
    新增 `resetDefault`。为什么必须有个**不用按键**的路径：锁屏那条的默认值就是
    `Ctrl+Cmd+Q` —— 在录制态里按它，系统直接锁屏，永远录不进去；睡眠那条同理
    （`Ctrl+Shift+电源键`）。所以「恢复默认」＝后端直接把 `from` 抄成 `to[0]`，
    并**顺手停用这一条**（回到系统原生行为，不留一条空转规则）。
    """
    if patch.get('resetDefault'):
        to = it.get('to') or []
        t0 = to[0] if to and isinstance(to[0], dict) else {}
        if t0.get('key_code'):
            md = {}
            if t0.get('modifiers'):
                md['mandatory'] = [str(x) for x in t0['modifiers']]
            md['optional'] = ['any']
            it['from'] = {'key_code': str(t0['key_code']), 'modifiers': md}
        it['enabled'] = False
        return it
    if 'enabled' in patch:
        it['enabled'] = bool(patch.get('enabled'))
    fk = patch.get('fromKey')
    if fk:
        it.setdefault('from', {})['key_code'] = str(fk)
    # ⚠️ 必须用 `in` 判、不能用真值判：空列表 ＝「把修饰键全去掉」，
    #    写成 `if patch.get('fromMods'):` 的话「去掉修饰键」这个动作永远落不了盘。
    if 'fromMods' in patch and isinstance(patch.get('fromMods'), list):
        f = it.setdefault('from', {})
        md = dict(f.get('modifiers') or {})      # ★ 保留 optional:["any"] 之类
        mods = [str(x) for x in patch['fromMods'] if x]
        if mods:
            md['mandatory'] = mods
        else:
            md.pop('mandatory', None)
        if md:
            f['modifiers'] = md
        else:
            f.pop('modifiers', None)
    tk = patch.get('toKey')
    if tk:
        to = it.get('to') or []
        if not to:
            to = [{}]
        to[0]['key_code'] = str(tk)
        it['to'] = to
    return it


def keymap_status():
    """给界面的全量：配置 + 人话标签 + **外壳回报的实况**。

    ★ `perm` / `engine` 由外壳写在 `_界面.json` 里（`keyPerm` / `keyEngine`）——
      只有外壳知道自己有没有拿到「辅助功能」权限。**没这个键就回 None**（＝还不知道），
      不要回 False：那会让界面在「外壳压根没跑」时冤枉地说「缺权限」。
    """
    d = _keymap_mod().for_ui()
    c = load_conf()
    d['live'] = {
        'perm': c.get('keyPerm') if 'keyPerm' in c else None,
        'engine': c.get('keyEngine') if 'keyEngine' in c else None,
        'text': c.get('keyStatus') or '',
    }
    d['ok'] = True
    return d


# ★★ 2026-10-01（第卅二轮）：键盘配置的**读-改-写必须串行**。
#   服务端是 `ThreadingHTTPServer`（见文件末尾 `srv = ThreadingHTTPServer(...)`），
#   而 `apply_keymap_patch()` 干的是「load() 读盘 → 改**一个**字段 → save() 写盘」。
#   两个请求重叠时就变成「后落盘的那份把前一份整个盖掉」——
#   实测复现过：同一个 tick 里连点「总开关」与「Command X-V」，最后只有总开关落盘，
#   Command X-V 那个开关在界面上翻过去了、配置里却没变（用户以为点了没用）。
#   ⚠️ 锁必须罩住**整个读-改-写**：只在 `wb_keymap.save()` 里加是没用的，
#      两个请求各自读到的是同一份旧数据，谁后写谁赢。
#   ⚠️ 这里**只护写**；`keymap_status()` 那类纯读不进锁（读了也不会写坏）。
_KEYMAP_LOCK = threading.Lock()


def keymap_live():
    """只读外壳回报的那三个实况值（**给 3 秒轮询用**）。

    ★ 为什么单拎出来：`keymap_status()` 要把整份 `_键盘.json`（几十 KB、含 57 条规则）
      读出来翻译一遍，那是「进这一页时拉一次」的量级；塞进 3 秒轮询白烧 IO。
      这三个值全在 `_界面.json` 里（很小），且只有外壳知道。
    """
    c = load_conf()
    return {
        'perm': c.get('keyPerm') if 'keyPerm' in c else None,
        'engine': c.get('keyEngine') if 'keyEngine' in c else None,
        'text': c.get('keyStatus') or '',
    }


def apply_keymap_patch(part):
    """界面回传的小改动 → 合并进 `_键盘.json` → 回全量（外壳会自己重挂）。

    ★★ 2026-10-01（第卅二轮）：整段读-改-写罩在 `_KEYMAP_LOCK` 里 ——
       服务端是 `ThreadingHTTPServer`，两个补丁重叠时会「后写覆盖前写」
       （实测：同一 tick 里连点两个开关，只有一个落盘）。理由详见上面锁的定义。

    ⚠️ 这个函数**只有一份**（就在 `_KEYMAP_LOCK` 下面）。原来这里还留着一份
       没有锁的旧定义 —— Python 里**后定义的那个生效**，等于锁白加了，
       改动时务必确认全文只有一处 `def apply_keymap_patch`。
    """
    K = _keymap_mod()
    with _KEYMAP_LOCK:
        cfg = K.load()
        if isinstance(part.get('engine'), dict) and 'enabled' in part['engine']:
            cfg['engine']['enabled'] = bool(part['engine']['enabled'])
        if isinstance(part.get('commandXV'), dict) and 'enabled' in part['commandXV']:
            cfg['commandXV']['enabled'] = bool(part['commandXV']['enabled'])
        md = part.get('modifiers')
        if isinstance(md, dict):
            if 'enabled' in md:
                cfg['modifiers']['enabled'] = bool(md['enabled'])
            # ★ 2026-10-02：「修饰键映射对谁生效」。只认 all / external，
            #   别的值一律当 external（默认），不给「悄悄全键盘」这种惊吓。
            if 'scope' in md:
                cfg['modifiers']['scope'] = 'all' if str(md['scope']) == 'all' else 'external'
            byid = {x.get('id'): x for x in cfg['modifiers']['items']}
            for p in (md.get('items') or []):
                it = byid.get(p.get('id'))
                if it:
                    _apply_key_item(it, p)
        for gp in (part.get('groups') or []):
            # ⚠️ 2026-10-18（用户：「修饰映射键也是，不保留」）：原来这里有个
            #   `key == 'modifier'` 的分支，用来写 `cfg['modifiers']`（那一组不在
            #   `cfg['groups']` 里）。现在那一组已从界面与 `for_ui()` 里删掉，
            #   分支一并去掉 —— 界面上再没有能发 "modifier" 补丁的地方。
            g = next((x for x in cfg['groups'] if x.get('key') == gp.get('key')), None)
            if g is None:
                continue
            if 'enabled' in gp:
                g['enabled'] = bool(gp['enabled'])
            byid = {x.get('id'): x for x in g['rules']}
            for p in (gp.get('rules') or []):
                it = byid.get(p.get('id'))
                if it:
                    _apply_key_item(it, p)
        ok, how = K.save(cfg)
    d = keymap_status()
    d['saved'] = ok
    d['how'] = how
    return d


# ------------------------------------------------------------ 「开机自启」
#
# ★★ 2026-09-30（用户第 2 条）：照 PommeToys 的 `app.launchAtLogin.enabled`
#   在「状态与设置」里加一个开机自启开关。
#
# 做法＝写/删一个用户级 LaunchAgent（`~/Library/LaunchAgents/<label>.plist`）。
#   · 这一步是**纯文件操作**，本进程就能干（无需提权、无需沙箱外的上下文）；
#   · 落到磁盘上就已经保证了「下次登录自动运行」—— 这是这个开关的**本义**；
#   · 想让它**立刻**在 launchd 里生效（而不是等下次登录），再走一次提权桥让
#     外壳代跑 `launchctl bootstrap`（工具进程里 bootstrap 恒报 5: I/O error，
#     见 §18）。桥没回也不影响本义，只是下次登录才生效 —— 所以只提示、不报错。
AUTOSTART_LABEL = 'com.wb.timeguardian-mac'
SHELL_APP_PATH = '/Applications/TimeGuardian.app'

# ★★★ 2026-10-02（第六十六轮 · 用户第 1 条）：「开机自动打开，但是**不要弹出来窗口**」。
#   `--args --silent` 就是为这一条加的：登录时拉起＝**静默**（只在菜单栏留图标，
#   窗口不上屏、不抢前台），手动双击 / 点 Dock 图标照旧弹窗口。
#   ⚠️ 为什么不是「外壳启动后再把窗口收起来」：`open -g` 只做到「不主动前置」，
#      把窗口端上来的其实是外壳自己那两句 `makeKeyAndOrderFront` + `activate`
#      （见 WBShell.swift 的 SILENT_LAUNCH）—— 必须在源头分叉才有用。
#   ⚠️ `--args` 后面那串**只在 app 尚未运行时**才送达（登录场景正是这一种）。
#      已跑着的实例被 `open` 拉一次只会被激活，不会带参数 —— 这条路我们不依赖它。
#   ⚠️ 门禁 `_排障与验证脚本/_chk_mouse.py` 的 [4] 只断言「有 /usr/bin/open」
#      「有 .app 结尾的参数」，多这两项不冲突（已核）。
AUTOSTART_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>/usr/bin/open</string>
\t\t<string>-g</string>
\t\t<string>{app}</string>{extra}
\t</array>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>ProcessType</key>
\t<string>Interactive</string>
</dict>
</plist>
"""


def autostart_plist():
    return os.path.expanduser('~/Library/LaunchAgents/%s.plist' % AUTOSTART_LABEL)


def autostart_silent():
    """开机自启是否「后台启动、不弹主界面」（默认 True）。存在 _界面.json。"""
    try:
        return bool(load_conf().get('autostartSilent', True))
    except Exception:
        return True


def _autostart_extra(silent):
    """plist 里 open 的附加参数：静默＝带 `--args --silent`，否则不带（照常弹窗口）。"""
    return ("\n\t\t<string>--args</string>\n\t\t<string>--silent</string>"
            if silent else "")


def autostart_state():
    p = autostart_plist()
    return {'on': os.path.exists(p), 'plist': p, 'label': AUTOSTART_LABEL,
            'app': SHELL_APP_PATH, 'appExists': os.path.exists(SHELL_APP_PATH),
            'silent': autostart_silent()}


def set_autostart(on, silent=None):
    """开/关开机自启；`silent` 决定登录时是否弹主界面。返回 `(state, warn)`。"""
    if silent is None:
        silent = autostart_silent()
    else:
        try:
            c = load_conf()
            c['autostartSilent'] = bool(silent)
            save_conf(c)
        except Exception:
            pass
    p = autostart_plist()
    warn = ''
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if on:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(AUTOSTART_XML.format(label=AUTOSTART_LABEL, app=SHELL_APP_PATH,
                                             extra=_autostart_extra(silent)))
        elif os.path.exists(p):
            os.remove(p)
    except Exception as e:
        return autostart_state(), '写 %s 失败：%r' % (p, e)
    ok, why = bridge_call('autostart', 'on' if on else 'off')
    if not ok:
        warn = ('已%s（下次登录生效），但让 launchd 立刻生效没成：%s'
                % ('打开' if on else '关闭', why))
        log('开机自启：%s' % warn)
    return autostart_state(), warn


def bridge_call(kind, val='', wait=6.0):
    """走「任务管家.app」的请求文件桥，代跑一件本进程干不了的事。返回 (ok, 原因)。

    与 wb_powerguard._lid_via_shell_bridge 同一套协议（见 WBShell.swift 的
    「提权请求文件」段）—— 这里只是把 kind/val 泛化，不再写死 disablesleep：
      · 在本进程（沙箱里）`launchctl bootstrap` 恒报 `5: I/O error`；
      · 而 app 由 launchd 拉起、在**干净上下文**，bootout/bootstrap 都能成。
    节奏：有常驻实例就安心等 `wait` 秒；没有就短等 2.5 秒再冷启一个临时实例。
    """
    req = os.path.join(HERE, '_sudo_req.json')
    inv = os.path.join(HERE, '_sudo_invite')
    res = os.path.join(HERE, '_sudo_res.%d-%d.json'
                             % (os.getpid(), int(time.time() * 1000) % 100000))

    def _shell_running():
        try:
            r = subprocess.run(['/usr/bin/pgrep', '-f', 'WorkBuddyShell'],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    def _poll(seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                with open(res, encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
            time.sleep(0.15)
        return None

    def _write_req():
        tmp = req + '.tmp.%d' % os.getpid()
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'seq': int(time.time() * 1000), 'kind': kind,
                       'val': str(val), 'res': res, 'at': time.time()}, f)
        os.replace(tmp, req)

    try:
        os.remove(res)
    except OSError:
        pass

    resident = _shell_running()
    d = None
    try:
        for attempt in (1, 2):
            try:
                _write_req()
            except Exception as e:
                return False, '写请求文件失败：%r' % e
            d = _poll(wait if (resident and attempt == 1) else 2.5)
            if d is not None:
                break
            if not os.path.exists(SHELL_APP_PATH):
                return False, '找不到 %s，桥不可用' % SHELL_APP_PATH
            try:
                with open(inv, 'w', encoding='utf-8') as f:
                    f.write(str(time.time()))
                subprocess.run(['/usr/bin/open', '-n', '-g', '-a',
                                os.path.basename(SHELL_APP_PATH).replace('.app', '')],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=15)
            except Exception as e:
                return False, '拉起外壳失败：%r' % e
            d = _poll(8.0)
            if d is not None:
                break
    finally:
        for _p in (inv, res):
            try:
                os.remove(_p)
            except OSError:
                pass

    if d is None:
        return False, '桥没回结果（超时）'
    if int(d.get('rc', -1)) == 0:
        return True, str(d.get('msg') or '')
    return False, '桥里非 0（rc=%s）：%s' % (d.get('rc'), str(d.get('msg'))[:120])


# ---------------------------------------------------- 更多工具 / 状态与设置

def _run_res(argv, timeout=60):
    """同步跑一个子脚本，返回 `(rc, 输出文本)`。**只给视图内「点了就有结果」用。**

    ★★ 2026-09-29：为什么需要「同步」而不是一律走 job_start：
      用户要求「尽量都不用终端界面了，点击按钮自动打开右边的终端界面也不要了」。
      新视图里的动作（恢复会话 / 合并会话 / 清空间）结果**直接在视图里显示一行**，
      不值得为它打开右侧输出面板。HTTP 处理跑在独立线程里，同步等一会儿没关系。
    ⚠️ `stdin=DEVNULL`：脚本里那些 y/N 会立刻吃到 EOF、按脚本自己的 `EOFError`
      兜底走「否」—— 所以这里**不能**跑那些「不问就不干活」的交互式子命令。
    """
    script = os.path.join(HERE, argv[0])
    if not os.path.exists(script):
        return 127, '（找不到 %s）' % argv[0]
    try:
        r = subprocess.run([find_python(), script] + [str(x) for x in argv[1:]],
                           cwd=HERE, env=_env_for_child(),
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return (r.returncode,
                strip_ansi((r.stdout or b'').decode('utf-8', 'replace')))
    except subprocess.TimeoutExpired:
        return 124, '（超时：%d 秒还没跑完）' % timeout
    except Exception as e:
        return 1, '（跑不起来：%r）' % (e,)


def _run_text(argv, timeout=20):
    """同上，只要文本。"""
    return _run_res(argv, timeout=timeout)[1]


def _json_script(argv, timeout=40):
    """跑一个带 `--json` 的脚本，把它那**一行纯 JSON** 解析出来。

    ⚠️ 只在**用户主动打开**的视图里用（/api/undelete、/api/merge、/api/space），
      绝不放进 3 秒轮询 —— 每轮 fork 一个进程太浪费。
    取「最后一条能解析成功的行」而不是「整段 stdout」：脚本哪天多打一行人话，
      视图也不至于整片空白（这是最容易被后手改动打破的地方）。
    """
    rc, txt = _run_res(argv, timeout=timeout)
    for line in reversed(txt.splitlines()):
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict):
                d.setdefault('rc', rc)
                return d
    return {'ok': False, 'rc': rc,
            'msg': '脚本没有输出可解析的 JSON（%s）' % argv[0],
            'raw': txt[-1200:]}


# ------------------------------------------------- 右键助手（系统「快速操作」）
#
# ★★ 2026-10-01（第四十八轮 · 用户）：把右键助手专业版（MouseBoost Pro）里那几项
#    搬出来、收进「鼠标」栏目。用户原话：「把 word模板创建 / 图片转PDF / 合并PDF /
#    PDF加密解密 / 文档转PDF 从 MouseBoost Pro 搬出来，卸载 MouseBoost 后仍可用，
#    并集成到任务管家；且图标不要一律用 Word 图标」。
#
# ⚠️ 这一层只干三件事：读状态 / 装卸单项 / 让右键菜单生效。
#    **真正干活的仍是 com.wb.pdfquick 守护**（pdf_daemon.py），一行没动。
# ⚠️ 绝不放进 /api/status 的 3 秒轮询 —— 每次都要 fork 一个子进程。
def quickactions_status():
    """跑 `wb_quickactions.py --json` 拿快照（人话说明也在里面，前端不自己拼）。"""
    return _json_script(['wb_quickactions.py', '--json'], timeout=25)


def quickactions_apply(name, on):
    """装 / 卸**单个**快速操作。返回 (ok, 一行结论)。"""
    argv = ['wb_quickactions.py', '--install' if on else '--uninstall', str(name)]
    rc, txt = _run_res(argv, timeout=45)
    tail = [ln for ln in (txt or '').splitlines() if ln.strip()]
    return (rc == 0), (tail[-1].strip() if tail else ('rc=%d' % rc))


# ★★ 2026-10-01（第四十九轮 · 用户）：给「word模板创建」挑模板。
#
#  用户原话：「在 **Word 模板创建**这个 UI 图标的右半边，加一个按钮，即可以选择
#            word 模板文件，默认的 Word 模板文件应该是 Normal.dotm（共用模板）」。
#
#  ⚠️ 弹选文件框这件事**必须由本进程干**（而不是丢给外壳桥）：
#     · `<input type="file">` 在自建 WKWebView 里点不动 —— 要用得先在 Swift 里实现
#       `runOpenPanelWith`，为一个按钮改外壳太贵；
#     · 而 `osascript … choose file` 用的是 Standard Additions 自带的那个框，
#       **不需要辅助功能权限、也不去控制别的 App**，普通用户态进程就能弹。
#     本进程由外壳/launchd 拉起，不在 WorkBuddy 的工具沙箱里，所以弹得出来。
#  ⚠️ 它会一直等到用户选完或取消 —— 超时给 5 分钟；服务端是 ThreadingHTTPServer
#     （daemon_threads=True），这一个请求挂着**不影响**别的接口。
def quickactions_pick_template():
    """返回 (ok, msg, cancel)。cancel=True 表示用户自己按了取消，界面别报红。"""
    rc, txt = _run_res(['wb_quickactions.py', '--pick-template'], timeout=300)
    tail = [ln for ln in (txt or '').splitlines() if ln.strip()]
    msg = tail[-1].strip() if tail else ('rc=%d' % rc)
    if rc == 0:
        return True, msg, False
    if rc == 3:                      # 脚本里约定的「用户取消」
        return False, msg, True
    return False, msg, False


def quickactions_set_template(path):
    """直接指定模板路径（目前界面只用 pick 那条；这条留给脚本/以后用）。"""
    rc, txt = _run_res(['wb_quickactions.py', '--set-template', str(path)], timeout=30)
    tail = [ln for ln in (txt or '').splitlines() if ln.strip()]
    return (rc == 0), (tail[-1].strip() if tail else ('rc=%d' % rc))
# ★★★ 2026-10-11（拆家）：`_lines_payload()` / `_lines_arr()` 已删 ——
#   它们读的是任务管家的 `wb_sync_ctl`（四条同步线），本程序没有这个模块。

# ★ 2026-10-11（拆家）：build_tools()（任务管家的「更多工具」面板数据）已删。

def build_settings():
    """「状态与设置」视图的全部数据（只读）。打开时才调，不进轮询。"""
    M = menu_module()
    ui = load_conf()

    def mk(label, text):
        t = strip_ansi(str(text)).strip()
        return {'k': label, 'v': t, 'level': _level_of(t)}

    # ★★★ 2026-10-11（拆家）：原来这里还有 WorkBuddy 运行状态 / 切号后同步守护 /
    #   切号触发 / 会话共享文件 四行 + 四条同步线 —— 全属任务管家，已删。
    #   本程序只报它自己那一件事：运行监测。
    misc = []
    try:
        misc = [mk('运行监测（一行）', M.powerguard_state())]
    except Exception as e:
        misc = [{'k': '状态', 'v': '读不出来（%s）' % e, 'level': 'err'}]

    return {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'env': env_state(),
        'stale': code_stale(),
        'entry': ENTRY_NAME,
        'debugEntry': DEBUG_ENTRY,
        'counts': {'menu': len(M.MENU), 'groups': len(M.GROUPS), 'extra': len(M.EXTRA)},
        'paths': {'here': HERE},
        'win': {k: ui.get(k) for k in ('port', 'winW', 'winH', 'winX', 'winY')
                if ui.get(k) is not None},
        'prefs': sound_prefs(),
        'sounds': sound_list(),
        # ★★ 2026-09-30（用户第 2 条）：开机自启的状态（开关在「状态与设置」里）。
        #   与「鼠标」不同，它不跟着 3 秒轮询走 —— 只在这一页打开时读一次。
        'autostart': autostart_state(),
        'misc': misc,
        # 运行监测的完整状态：直接把 `--status --full` 的文字拿过来
        # （原来这是菜单 power1 那个交互面板的内容；面板取消后挪到这儿）
        'powerText': _run_text(['wb_powerguard.py', '--status', '--full']),
    }


# ------------------------------------------------------------------ 任务执行

JOBS = {}
JOB_LOCK = threading.Lock()
JOB_SEQ = [0]


def running_jobs_snapshot():
    """正在跑的内嵌任务清单（给 /api/status → 前端恢复卡片上的运行指示）。

    ★ 为什么放在状态轮询里：窗口重载（页面过期自动刷新 / 外壳重启）后
      前端内存里的「job ↔ 卡片」对应关系全丢 —— 但每 3 秒一次的 /api/status
      还在跑，顺路把 running 的 job 带回去，卡片右侧的进度指示就能自己续上。
    """
    with JOB_LOCK:
        return [{'id': j['id'], 'key': j.get('key', ''), 'num': j['num'],
                 'desc': j['desc'], 'elapsed': round(time.time() - j['t0'], 1)}
                for j in JOBS.values() if j['state'] == 'running']


def _shq(s):
    """把一段字符串安全地包成 shell 单引号形式（macOS 起终端才用得到）。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _write_term_cmd(argv, title='WorkBuddy 任务管家'):
    """写一个临时 .command（macOS 用「终端」打开它来跑 argv），返回文件路径。

    为什么写文件而不是 `osascript -e 'tell app "Terminal" to do script ...'`：
    后者要「自动化 → 终端」授权，没授权时报 -10004 且**不弹窗**，症状是
    「点了卡片什么都没发生」。`open -a Terminal <文件>` 不需要那项授权。
    文件落在工具目录的 `_终端任务/`（不是 /tmp），便于事后回看跑了什么；
    这些小文件在菜单 17「清理本工具包目录的备份/缓存」里会被一并收走。
    """
    tdir = os.path.join(HERE, '_终端任务')
    try:
        os.makedirs(tdir, exist_ok=True)
    except Exception:
        tdir = None
    fd, tmp = tempfile.mkstemp(suffix='.command', dir=tdir)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        # ★ 2026-09-28：末尾不再用 `exec`，改成「跑完停一下，按任意键关闭」。
        #   起因：用户点「打开文字面板」后什么都没看到。查下来 open 其实成功了
        #   （Terminal 进程的启动时刻正是点击那一刻），但**只要子脚本以任何理由
        #   立刻退出，Terminal 就会按默认设置把窗口一起关掉** —— 用户看到的就是
        #   「一闪而过 / 不让打开」，连报错都来不及看。
        #   加这一段的收益：无论成功失败，窗口都留着并把退出码显示出来。
        f.write('#!/bin/bash\n'
                '# %s\n'
                '# 本文件由「界面」按需生成：图形界面里点了「要另开窗口操作」的项、\n'
                '# 或点了右上角「编号菜单」时，会走这儿。\n'
                'cd %s || { echo "进不去目录 %s"; read -n 1 -s -p "按任意键关闭…"; exit 1; }\n'
                '%s\n'
                '__rc=$?\n'
                'echo\n'
                'echo "———— 上面这条已经跑完（退出码 $__rc）————"\n'
                'read -n 1 -s -p "按任意键关闭这个窗口…"\n'
                'exit $__rc\n'
                % (title, _shq(HERE), HERE,
                   ' '.join(_shq(c) for c in argv)))
    os.chmod(tmp, 0o755)
    return tmp


def open_menu_terminal():
    """在独立终端里打开「黑窗编号菜单」—— 界面右上角那个按钮走这条。

    ★ 这是用户定的口径（2026-09-27）：「与 Windows 一致」= 桌面入口开**图形界面**，
      黑窗菜单收进界面里，不再另占一个桌面图标。所以菜单得在界面里点得到，
      否则老用户会以为黑窗菜单没了。
    """
    script = os.path.join(HERE, 'wb_menu.py')
    if not os.path.exists(script):
        return {'ok': False, 'msg': '找不到 %s' % script}
    try:
        if IS_WIN:
            exe = os.path.join(HERE, '_run.bat')
            if not os.path.exists(exe):
                return {'ok': False, 'msg': '找不到 %s' % exe}
            subprocess.Popen(['cmd', '/k', exe, 'wb_menu.py'], cwd=HERE,
                             env=_env_for_child(), creationflags=0x00000010)
        elif IS_MAC:
            tmp = _write_term_cmd([find_python(), script],
                                  'WorkBuddy 任务管家 · 编号菜单')
            subprocess.Popen(['open', '-a', 'Terminal', tmp], cwd=HERE,
                             env=_env_for_child())
        else:
            subprocess.Popen(['x-terminal-emulator', '-e', find_python(), script],
                             cwd=HERE, env=_env_for_child())
    except Exception as e:
        log('打开编号菜单终端失败：%r' % (e,))
        return {'ok': False, 'msg': repr(e)}
    log('已在终端打开编号菜单')
    return {'ok': True}


def _env_for_child():
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    env['PYTHONUTF8'] = '1'
    return env


def _reader(job, proc):
    """后台线程：把子进程输出逐行收进 job['out']。

    用 readline（而不是 read(4096)）是为了实时 —— read 会一直攒到缓冲区满
    才返回，长任务就看不到进度；universal newlines 已把 \\r / \\r\\n / \\n
    都当行结束，所以用 \\r 刷新的进度条也能一行行收到。
    """
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            job['out'].append(line)
            if len(job['out']) > 200000:
                del job['out'][:50000]
    except Exception as e:
        job['out'].append('\n[读取输出出错：%r]\n' % (e,))
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        job['rc'] = proc.wait()
        job['state'] = 'done'
        job['t1'] = time.time()


def job_start(num, desc, cmd, key=''):
    script = os.path.join(HERE, cmd[0])
    if not os.path.exists(script):
        return {'error': '找不到脚本：%s' % script}
    # ★ 宿主用 find_python_console()（python.exe）而**不是** find_python()：
    #   pythonw 没有控制台，CREATE_NO_WINDOW 又对它无效 → 整条链上每个子进程
    #   都会各弹一个终端窗口（详见 find_python_console 的说明）。
    full = [find_python_console(), script] + list(cmd[1:])

    with JOB_LOCK:
        JOB_SEQ[0] += 1
        jid = '%d-%d' % (int(time.time()), JOB_SEQ[0])
        job = {
            'id': jid, 'num': num, 'desc': desc, 'cmd': full,
            # ★ 2026-09-29：前端点卡片时用的**界面键**（如 env1 / one1）。
            #   卡片右侧的迷你进度指示靠它把「哪条 job 属于哪张卡」对上号 ——
            #   num 是菜单里的正式写法（可能与界面键不同），key 才是卡片上的 data-key。
            'key': str(key or ''),
            'out': [], 'state': 'running', 'rc': None,
            't0': time.time(), 't1': None, 'mode': 'inline', 'proc': None,
        }
        JOBS[jid] = job
        # 只留最近 12 条，避免内存一直涨
        if len(JOBS) > 12:
            for k in sorted(JOBS, key=lambda k: JOBS[k]['t0'])[:len(JOBS) - 12]:
                if JOBS[k]['state'] == 'done':
                    JOBS.pop(k, None)

    job['out'].append('$ %s %s\n' % (os.path.basename(cmd[0]),
                                     ' '.join(cmd[1:]) if len(cmd) > 1 else ''))
    job['out'].append('-' * 70 + '\n')
    try:
        proc = subprocess.Popen(
            full, cwd=HERE, env=_env_for_child(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, universal_newlines=True,
            encoding='utf-8', errors='replace',
            creationflags=(0x08000000 if IS_WIN else 0),   # CREATE_NO_WINDOW
        )
        job['proc'] = proc
    except Exception as e:
        job['out'].append('\n✗ 启动失败：%r\n' % (e,))
        job['state'] = 'done'
        job['rc'] = -1
        job['t1'] = time.time()
        return {'id': jid, 'mode': 'inline'}

    t = threading.Thread(target=_reader, args=(job, proc), daemon=True)
    t.start()
    log('内嵌执行 #%s %s' % (num, ' '.join(cmd)))
    return {'id': jid, 'mode': 'inline'}


def job_in_terminal(num, desc, cmd):
    """在独立终端窗口里跑（交互式脚本）—— 界面只负责把它叫起来。

    ★ 这里刻意**不自己拼命令行**：直接让 cmd 跑同目录的 _run.bat。
      _run.bat 已经处理好了两件麻烦事 —— 自己找 Python、以及 chcp 65001
      （新控制台默认是 936/GBK，而我们所有脚本都吐 UTF-8，不切码页必然乱码）。
      绕开它另拼一条命令，等于把这两件事重做一遍，还容易在引号上翻车。
    ★ CREATE_NEW_CONSOLE(0x10) 直接开一个独立控制台窗口，不需要再套 start，
      少一层 cmd 解析就少一类引号陷阱。
    """
    script = os.path.join(HERE, cmd[0])
    if not os.path.exists(script):
        return {'error': '找不到脚本：%s' % script}

    try:
        if IS_WIN:
            exe = os.path.join(HERE, '_run.bat')
            if not os.path.exists(exe):
                return {'error': '找不到 %s' % exe}
            # 参数里不能出现空格，否则 list2cmdline 会给它加引号、cmd 解析就乱；
            # 现有菜单项的实参都是 --level / 1 这种，天然安全。
            subprocess.Popen(['cmd', '/k', exe] + [cmd[0]] + list(cmd[1:]),
                             cwd=HERE, env=_env_for_child(),
                             creationflags=0x00000010)       # CREATE_NEW_CONSOLE
        elif IS_MAC:
            # ★ macOS 走「写一个临时 .command → 交给「终端」打开」这条路，
            #   不用 osascript 的 `tell app "Terminal" to do script ...`：
            #   后者要求「自动化 → 终端」授权，未授权时报 -10004 权限违例，
            #   而且**不弹窗提示**，表现就是「点了卡片什么都没发生」。
            #   `open -a Terminal <file.command>` 不需要那项授权。
            py = find_python()
            tmp = _write_term_cmd([py, script] + list(cmd[1:]),
                                  'WorkBuddy 任务管家 · %s' % desc)
            # ★ 2026-09-28：必须看 open 的返回码。
            #   原来用 Popen(...) 不看结果 —— `open` 失败时**返回非 0 就结束了**
            #   （不是抛异常），日志却照记「终端执行」，于是界面上弹出「请到那个
            #   窗口里继续」，而根本没有窗口。用 run() 拿返回码，失败就明说。
            try:
                r = subprocess.run(['open', '-a', 'Terminal', tmp], cwd=HERE,
                                   env=_env_for_child(),
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=25)
                rc = r.returncode
                msg = (r.stdout or b'').decode('utf-8', 'replace').strip()
            except Exception as e:
                rc, msg = -1, repr(e)
            if rc != 0:
                log('弹终端失败 #%s（open rc=%s）：%s' % (num, rc, msg))
                return {'error': '打不开「终端」窗口（open 返回 %s）：%s。'
                                 '可以点右上角「编号菜单」，或自己在终端里跑 %s'
                                 % (rc, msg or '无输出', os.path.basename(cmd[0]))}
            log('终端执行 #%s（%s）' % (num, tmp))
        else:
            subprocess.Popen(['x-terminal-emulator', '-e', find_python(), script]
                             + list(cmd[1:]), cwd=HERE, env=_env_for_child())
    except Exception as e:
        log('弹终端失败：%r' % (e,))
        return {'error': '打不开终端窗口：%r' % (e,)}
    log('终端执行 #%s %s' % (num, ' '.join(cmd)))
    return {'mode': 'term'}


def job_stop(jid):
    with JOB_LOCK:
        job = JOBS.get(jid)
    if not job or job['state'] != 'running' or not job['proc']:
        return {'ok': False, 'msg': '任务已经结束了'}
    try:
        if IS_WIN:
            subprocess.call(['taskkill', '/F', '/T', '/PID', str(job['proc'].pid)],
                            creationflags=0x08000000,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            job['proc'].terminate()
        job['out'].append('\n[已请求终止]\n')
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'msg': repr(e)}


def do_run(payload):
    num = str(payload.get('num') or '').strip()
    M = menu_module()                                     # noqa
    # ★ 一律按小写比、并把「正式写法」从表里取回来：菜单里既有小写字母键
    #   （a / b / c / j），又有大写编号（X = 空间回收）。前端传过来的就是
    #   菜单里那个原样字符串，但将来若有人在别处小写化过，这里仍能对上，
    #   不会出现「卡片点了说没有这一项」。取回 num 是为了下面 TERM_NUMS
    #   与 UI_EXTRA_ARGS 用的是菜单里的正式写法。
    # ★ 2026-09-27 加防线：原来这里是**按小写折叠**建索引的（`str(n).lower()`），
    #   于是 'x' 和 'X' 会互相覆盖、谁在后面谁赢 —— 而界面上的「取消这次自动关机」
    #   按钮传的正是旧编号 'x'，菜单里 'x' 却早已让位给 'X'（空间回收），
    #   结果点「取消关机」打开了空间回收。这是**第二次**踩同一个坑（第一次见
    #   wb_menu.py 第 225 行的 09-26 变更记录），所以在这里显式检测、不再静默取一个：
    #   一旦发现两项折叠后同键，两个都判为「分不清」，直接报错让人去改菜单。
    index = {}
    _dups = {}
    for _n, _d, _c, _r in M.MENU + M.EXTRA:
        _k = str(_n).lower()
        if _k in index:
            _dups.setdefault(_k, [index[_k][0]]).append(str(_n))
        else:
            index[_k] = (str(_n), _d, _c, _r)
    # 快捷项
    key = str(payload.get('key') or '').strip()

    # ★★ 2026-09-29：图形界面里的「视图」键 —— **在任何命令执行之前**拦下来。
    #   它们对应的菜单项本身是交互式的（黑窗菜单敲它要开终端才是对的），
    #   但在界面里我们改成开一个独立视图（见 VIEW_KEYS 的说明）。
    #   前端收到 {'view': …} 就切到那个界面；后续取数据 / 执行各走专用接口。
    if key in VIEW_KEYS:
        return {'view': VIEW_KEYS[key]}

    if key == 'z':
        num = 'one1'

    # ★★ 2026-09-29：「更多工具…」面板里的每一项，全是**界面专用键**（不是菜单项）。
    #   为什么不在菜单里：菜单要精简（用户要求「别再单列」），这些细粒度出口
    #   只该在面板里出现。三种形态：
    #     ① TOOL_KEYS 表里的固定项；
    #     ② tool_line_<线>_<动作>：单独控制某条线（up/pause/resume/down/now/log）；
    #     ③ tool_icon_<色相> / tool_items_set：换图标颜色 / 设置同步项开关。
    # ★★★ 2026-10-11（拆家）：同步线 / 工作空间 / 空间回收 那几组界面专用键全删
    #   （tool_line_* / tool_items_set / tool_space_* / TOOL_KEYS 固定项）——
    #   它们背后的脚本属任务管家，本程序没有。

    if key.startswith('tool_icon_'):
        tail = key[len('tool_icon_'):]
        argv = ['make_icon.py']
        if tail.isdigit():
            argv.append(tail)          # 位置参数就是色相角度（与黑窗菜单「macos5 45」一致）
        return job_start('tool', '图标 · 换颜色', argv)

    # ★★★ 2026-10-11（拆家）：tool_items_set（记忆线同步项开关）与
    #   tool_space_*（空间回收）都属任务管家，已删（它们背后的脚本本程序没有）。

    # 运行监测卡上那排开关（PommeToys 式）。
    #   走 `--toggle <name>`（非交互），名字用下划线传，后端换成中划线。
    #   为什么不像档位那样复用菜单编号：第 5 个开关「合盖后内建显示器保持点亮」
    #   **没有菜单编号**（它是嵌套子项），所以统一走这条专用路，四个有编号的也一起。
    if key in ('toggle_awake', 'toggle_display', 'toggle_lowbat',
               'toggle_lid', 'toggle_lid_display'):
        nm = key[len('toggle_'):].replace('_', '-')
        return job_start('power2', '运行监测 · 切换开关（%s）' % nm,
                         ['wb_powerguard.py', '--toggle', nm])

    # 运行监测卡上的「取消这次关机」：倒计时进行中那个按钮变成它。
    #   与「关掉跑完自动关机」的区别：这个**只取消这一次**，开关保持原样
    #   （走 wb_powerguard.py --cancel-shutdown，会落 CANCEL_FLAG 让守护立刻撒手）。
    if key == 'shutdown_cancel':
        return job_start('power6', '取消这次的关机倒计时',
                         ['wb_powerguard.py', '--cancel-shutdown'])

    # 运行监测卡上的「跑完自动关机」按钮：必须给一条**非交互**的路。
    # 原来它是去调 15 号的交互式子面板，而内嵌执行的子进程 stdin 是 DEVNULL ——
    # 那句 input() 立刻吃到 EOF，按钮点了等于没点（面板文字刷过去而已）。
    if key in ('shutdown_on', 'shutdown_off'):
        on = (key == 'shutdown_on')
        desc = '%s「任务结束自动关机」' % ('开启' if on else '关闭')
        # ★★★ 2026-10-01（第五十一轮 · 用户第 2 条）：**开启时不再回一条确认**。
        #   用户原话：「跑完自动关机按理讲应该不用修改数据啊，点击这个按钮的时候
        #   无论哪个界面都不需要弹窗」。
        #   想想确实多余：这颗开关本身就是一次**显式**的意思表示 —— 点一下翻过去，
        #   界面当场变绿、悬停也写着「任务全部结束后会自动关机」，再拦一道
        #   「这个操作会改动数据，确认执行吗？」只是让人多点一次；
        #   而且它是**非交互**命令（`wb_powerguard.py --shutdown on`），
        #   根本不存在「脚本里还留着一句 input() 要替它回答」那类问题
        #   （那套「先回一条确认、再由前端复跑」的机制见下面 num 那条分支）。
        #   ⚠️ 只撤这一处。`num` 那条路上的那套确认是给「破坏性菜单项」兜底的，
        #      与这里无关，别顺手一起删。
        #   ⚠️ 上面这几行注释里**不许**写出那个字段的字面量 —— 门禁是拿它当
        #      关键词扫这段源码的（连注释一起扫），写了就会误报「又出现了」。
        return job_start('power6', desc,
                         ['wb_powerguard.py', '--shutdown', 'on' if on else 'off'])

    # 运行监测卡上的「到点自动结束」：同样必须给一条**非交互**的路
    #   （原因同上：菜单 6 号是交互式面板，会问时长；内嵌执行时 stdin=DEVNULL，
    #    那句 input() 立刻吃到 EOF → 按钮点了等于没点）。
    #   键的写法：`autostop_off` = 取消；`autostop_<分钟>` = 设这么多分钟。
    if key == 'autostop_off':
        return job_start('power7', '取消「到点自动结束」',
                         ['wb_powerguard.py', '--autostop', 'off'])
    if key.startswith('autostop_'):
        tail = key[len('autostop_'):]
        if tail.isdigit():
            mins = int(tail)
            return job_start('power7', '设置「到点自动结束」（%d 分钟后）' % mins,
                             ['wb_powerguard.py', '--autostop', '%dm' % mins])

    # ★ 2026-09-27 编号重排：新版前端点卡片时只回传**唯一键**（key，如 local3），
    #   不再带 num（重排后每个板块都有「1」，单发一个数字服务端分不清是哪个板块的）。
    #   老前端仍回传 num。两个都认，谁有就用谁。
    if not num:
        num = key

    if num and num.lower() in _dups:
        return {'error': '菜单编号有歧义：%s 折叠成小写后是同一个键（%s）—— '
                         '必须改掉其中一个，否则界面/命令行分不清点的是哪个'
                         % (num, '、'.join(_dups[num.lower()]))}
    hit = index.get(num.lower())
    if not hit:
        return {'error': '没有这一项：%s' % num}
    num, desc, cmd, risky = hit      # ← num 换成菜单里的正式写法（如 'X'）
    if risky and not payload.get('confirm'):
        return {'needConfirm': True, 'desc': desc}

    # 界面上点一下 = 已经确认过了（点击本身就是那个「确认」动作）。
    # 但脚本里可能还留着一句 y/N 在等输入，而内嵌执行时 stdin=DEVNULL ——
    # 那句 input() 会立刻吃到 EOF、被脚本按「用户取消」处理，
    # 表现就是「点了没反应」。所以这里替用户把它回答掉（见 UI_EXTRA_ARGS）。
    extra = UI_EXTRA_ARGS.get(num)
    if extra:
        cmd = list(cmd) + list(extra)

    if num in TERM_NUMS:
        return job_in_terminal(num, desc, cmd)
    # ★ 2026-09-29：把界面键带给 job —— 前端卡片右侧的进度指示靠它对号（见 job_start）。
    return job_start(num, desc, cmd, key=key)


# ------------------------------------------------------------------ HTTP

# ═════════════════════════════════════ 事件推送（SSE）· 2026-10-01（第四十八轮）
#
# ★★★ 为什么加它（用户第 1 条：「页面刷新为什么需要轮询？为什么不能映射到各个功能，
#   掉了才亮，改为事件驱动」）：
#
#   页面显示的状态，**每一次变化都有原因**，而且原因只有两类：
#     ① 你自己点了什么 —— 开关、滑块、执行、取消。请求回来的那一刻就有新状态了，
#        根本不用等下一次轮询；原来是「点了 → 等最多 3 秒才看到」。
#     ② 后台自己发生了变化 —— 任务开跑 / 跑完、倒计时到点。这些是**别的进程**
#        干的事，页面不知道，所以过去只能靠每 3 秒问一次。
#
#   第 ② 类现在也有事件了：运行监测守护每轮都会写 `_运行监测状态.json`，
#   用内核文件事件盯住它即可（见 start_state_watch）。
#
#   于是：**两条事件源 → 一个进程内广播台 → 所有 `/api/events` 长连接**。
#   前端收到就重画；3 秒轮询降级成「很久一次的兜底」（防推送链路彻底失效）。
#
#   ⚠️ 为什么能开长连接：服务端是 `ThreadingHTTPServer` 且 `daemon_threads=True`
#      —— 每个连接独占一个线程，一条长连接**不会**挡住别的请求。
_EVT_LOCK = threading.Lock()
_EVT_SUBS = []                       # 每个订阅者一个 queue.Queue
_EVT_STAT = {'n': 0, 'lastAt': 0.0, 'why': '', 'peak': 0}


def sse_publish(why=''):
    """往所有订阅者投一帧。**永不阻塞、永不抛** —— 推送失败绝不能影响主流程。"""
    _EVT_STAT['n'] += 1
    _EVT_STAT['lastAt'] = time.time()
    _EVT_STAT['why'] = why
    with _EVT_LOCK:
        subs = list(_EVT_SUBS)
        if len(subs) > _EVT_STAT['peak']:
            _EVT_STAT['peak'] = len(subs)
    for q in subs:
        try:
            q.put_nowait({'why': why, 't': time.time()})
        except Exception:
            pass                     # 队列满 = 这个客户端跟不上，丢一帧无所谓


def sse_state():
    """给自检 / 排障看：订户数、推过多少帧、最近一次为什么推。"""
    with _EVT_LOCK:
        n = len(_EVT_SUBS)
    return {'subs': n, 'pushed': _EVT_STAT['n'], 'why': _EVT_STAT['why'],
            'lastAt': _EVT_STAT['lastAt'], 'peak': _EVT_STAT['peak']}


def start_state_watch():
    """盯「运行监测守护」的状态文件：它一变就推一帧。

    ★ 这是事件驱动里「**别人干的事**」那一半：
        · 你自己点按钮那一半 → `_send` 里的钩子（POST 返回前推）；
        · 后台任务开跑 / 跑完 / 倒计时变化 → 是**守护进程**写状态文件反映出来的，
          页面没法靠「自己点过什么」知道，以前只能每 3 秒问一次。
    ★ 用内核文件事件（kqueue），**空闲时零唤醒**（阻塞在 kq.control 上）。
    ⚠️ 守护写状态文件是**原地覆盖**（原子替换会被沙箱文件策略拦，见守护日志），
      所以 inode 一般不变；但 DELETE/RENAME 仍按老规矩重挂 ——
      「哪天它改成原子替换」这件事随时可能发生，而那时不重挂会**静默失聪**。
    """
    try:
        import wb_powerguard as PG
        path = PG.STATE_FILE
    except Exception as e:
        log('状态文件监听没起来（拿不到路径）：%r' % (e,))
        return False

    # ★★★ 关键的一道闸：**只推「真正变了」的状态**。
    #
    #   为什么非要有它：守护几乎**每一轮**都会重写状态文件（心跳、忙碌时长、
    #   倒计时读数……都在里面）。照「文件一动就推」推下去，页面会被推得
    #   比原来 3 秒轮询还勤 —— 那不叫优化，叫倒退。
    #
    #   判据 = 只取**离散**字段做摘要（pid / busy / 档位 / 是否在倒计时 / 上次忙的时间……）。
    #   心跳时刻、空闲秒数这类**连续**值一律不看：它们本来就该由前端自己走秒
    #   （页面上那两盏倒计时就是这么做的），不需要服务端推。
    _DIGEST_KEYS = ('pid', 'busy', 'running', 'level', 'applied',
                    'shutdownPending', 'shutdownAt', 'lastBusyAt', 'everBusy',
                    'enabled', 'autoShutdown', 'autoStopAt')

    def _digest():
        try:
            with io.open(path, encoding='utf-8') as f:
                st = json.load(f) or {}
        except Exception:
            return None
        return json.dumps({k: st.get(k) for k in _DIGEST_KEYS}, sort_keys=True)

    last_digest = [None]

    def _run():
        import select as _sel
        ff = (_sel.KQ_NOTE_WRITE | _sel.KQ_NOTE_EXTEND
              | _sel.KQ_NOTE_DELETE | _sel.KQ_NOTE_RENAME)
        while True:
            kq = None
            fd = None
            try:
                if not os.path.exists(path):
                    time.sleep(5)              # 守护还没写过 → 过会儿再看
                    continue
                fd = os.open(path, os.O_RDONLY)
                kq = _sel.kqueue()
                ev = _sel.kevent(fd, filter=_sel.KQ_FILTER_VNODE,
                                 flags=(_sel.KQ_EV_ADD | _sel.KQ_EV_ENABLE
                                        | _sel.KQ_EV_CLEAR),
                                 fflags=ff)
                kq.control([ev], 0, 0)         # 注册（与等待分两段，学 pdf_daemon）
                last_digest[0] = _digest()     # 挂上时先记一份基线，别把「首读」当变化
                while True:
                    got = kq.control(None, 8, 600)      # 超时只是保险丝
                    if not got:
                        continue
                    d = _digest()
                    if d is not None and d != last_digest[0]:
                        last_digest[0] = d
                        sse_publish('state')       # ← 只有真变了才推
                    if any(getattr(e, 'fflags', 0) & (_sel.KQ_NOTE_DELETE
                                                      | _sel.KQ_NOTE_RENAME)
                           for e in got):
                        break                  # inode 换了 → 重挂
            except Exception as e:
                log('状态文件监听异常（5 秒后重挂）：%r' % (e,))
                time.sleep(5)
            finally:
                try:
                    if kq is not None:
                        kq.close()
                except Exception:
                    pass
                try:
                    if fd is not None:
                        os.close(fd)
                except Exception:
                    pass

    threading.Thread(target=_run, name='wbui-state-watch', daemon=True).start()
    return True


class Handler:
    """用组合而非继承，避免类体里写一大堆 self。"""

    def __init__(self, server):
        self.server = server
        self.wfile = None
        self._is_write = False       # ★ 由 handle() 每请求设置，见 _send 里的推送钩子

    # ---- 工具
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        head = ('HTTP/1.0 %d %s\r\n'
                'Content-Type: %s\r\n'
                'Content-Length: %d\r\n'
                'Cache-Control: no-store\r\n'
                '\r\n') % (
                    code, 'OK' if code == 200 else 'ERR', ctype, len(body))
        try:
            self.wfile.write(head.encode('ascii') + body)
            self.wfile.flush()
        except Exception:
            pass
        # ★★★（第四十八轮）**写操作成功返回前，顺手推一帧**给所有长连接 ——
        #   这样「点开关 / 拖滑块 / 执行任务」的结果**当场**出现在页面与面板上，
        #   不用等下一次（原来的 3 秒）轮询。
        #   ⚠️ 判据只认 `_is_write`（＝ POST）：GET 全是读接口（/api/status、
        #      /api/menu……），推它们等于「页面刷新 → 又推 → 又刷新」的死循环。
        if code == 200 and self._is_write:
            try:
                sse_publish('api')
            except Exception:
                pass
            # ★ 2026-10-09：写操作成功 → 顺手喊一声看门狗当场复核（与 Win 一致）。
            #   挂点只此一处：多喊一次的代价只是一次极便宜的复核。
            try:
                power_wd_nudge('api')
            except Exception:
                pass

    # ---- 事件长连接（SSE）
    def _sse_chunk(self, data):
        """按 chunked 编码写一块 —— HTTP/1.1 流式响应的标准写法。

        ⚠️ 不能用 `_send`：那个函数一次性写完整响应（还带 Content-Length），
           而长连接要的是「先给头、之后一块一块地写、长度事先未知」。
        """
        self.wfile.write(('%x\r\n' % len(data)).encode('ascii') + data + b'\r\n')
        self.wfile.flush()

    def _sse(self):
        """`/api/events` —— 长连接推「状态变了」。

        · 客户端一接上先收到一个 `hello`，紧接着会收到服务端补的一帧真值
          （前端收到任何帧都只是「去拉一次 /api/status」，所以顺序无所谓）；
        · 空闲时每 20 秒发一个注释行当心跳 —— 防止中间层（代理 / 内核）把
          长期没数据的连接掐掉；
        · 队列满 16 帧说明这个客户端已经跟不上了，**丢帧不阻塞**（见 sse_publish）。
        """
        import queue as _q
        try:
            self.wfile.write(
                b'HTTP/1.1 200 OK\r\n'
                b'Content-Type: text/event-stream; charset=utf-8\r\n'
                b'Cache-Control: no-cache\r\n'
                b'Connection: keep-alive\r\n'
                b'X-Accel-Buffering: no\r\n'
                b'Transfer-Encoding: chunked\r\n'
                b'\r\n')
            self.wfile.flush()
        except Exception:
            return
        box = _q.Queue(maxsize=16)
        with _EVT_LOCK:
            _EVT_SUBS.append(box)
            nsub = len(_EVT_SUBS)
        log('SSE 已连接（当前 %d 个）' % nsub)
        try:
            self._sse_chunk(b'event: hello\ndata: {}\n\n')
            while True:
                try:
                    item = box.get(timeout=20)
                    self._sse_chunk(
                        ('data: %s\n\n' % json.dumps(item, ensure_ascii=False)).encode('utf-8'))
                except _q.Empty:
                    self._sse_chunk(b': ping\n\n')       # 心跳
        except Exception:
            pass                                        # 客户端走了（页面关/切走）
        finally:
            with _EVT_LOCK:
                try:
                    _EVT_SUBS.remove(box)
                except ValueError:
                    pass
                nsub = len(_EVT_SUBS)
            log('SSE 断开（还剩 %d 个）' % nsub)

    # ---- 路由
    def handle(self, method, path, query, body):
        self._is_write = (method == 'POST')     # ★ 见 _send 里的推送钩子
        try:
            # ★★★（第四十八轮）事件长连接：页面/面板接上它之后就不再需要 3 秒轮询。
            #   必须放在最前面 —— 这个分支自己管着连接，直到客户端断开才返回。
            if path == '/api/events':
                return self._sse()

            if path in ('/', '/index.html'):
                # ★ 记一笔「页面什么时候被谁取走的」——窗口重新加载页面是件稀罕事
                #   （外壳只在首次 load / 服务重启后才取），记一条换来「界面到底
                #   有没有刷新」可查：`tail _界面.log`。页面**自己**发现界面过期后
                #   的 location.reload() 也走这里（见 page_ver / _ui.html::checkVer）。
                log('界面页送达（uiVer=%s）' % page_ver())
                with io.open(PAGE_FILE, encoding='utf-8') as f:
                    return self._send(200, f.read(), 'text/html; charset=utf-8')

            # 页面 <link rel="icon"> 指过来的图标（见 ICON_FILE 的说明）。
            # body 直接传 bytes：_send 对非 str/dict/list 的原样发出，不编码。
            if path in ('/favicon.ico', '/icon.ico'):
                try:
                    with open(ICON_FILE, 'rb') as f:
                        return self._send(200, f.read(), 'image/x-icon')
                except OSError:
                    log('图标文件读不到：%s' % ICON_FILE)
                    return self._send(404, {'error': 'no icon'})

            if path == '/api/menu':
                return self._send(200, {'ok': True, 'menu': build_menu(),
                                        'stale': code_stale(),
                                        # 版本指纹：页面把第一次收到的当基线（见 page_ver 说明）
                                        'uiVer': page_ver(),
                                        'menuVer': menu_ver(),
                                        # 文案里要用到的平台名（前端不硬编码）
                                        'entry': ENTRY_NAME,
                                        'debugEntry': DEBUG_ENTRY,
                                        # 界面按钮 → 菜单编号的映射（见 POWER_GUI 的说明：
                                        # 两端的编号不一样，前端写死会在 Mac 上点错东西）
                                        'gui': build_gui(),
                                        'termMenu': not IS_WIN or os.path.exists(
                                            os.path.join(HERE, '_run.bat'))})

            if path == '/api/status':
                heal_once()
                # ★★ 2026-09-29（用户第 10 条）：顶层也带版本指纹。
                #   之前只有 /api/menu 带 —— 事故：用户窗口开着跨过服务重启，
                #   /api/menu 失败一次 → 首页永远空白，且「页面过期自动刷新」
                #   也跟着死（checkVer 只能从 menu 的响应里拿到版本号）。
                #   现在状态轮询（3 秒一次）自己就能发现「页面过期」并 reload。
                return self._send(200, {'ok': True, 'status': build_status(),
                                        'power': build_power(),
                                        # ★★ 2026-09-30（用户第 2 条）：首页「鼠标」板块
                                        #   的三个值。跟着 3 秒轮询走 —— 别的窗口/别处改了
                                        #   也能自己收敛回来（开关的写入口只有这一个）。
                                        #   ⚠️ 必须放在**这个响应字典**里：塞进 build_status()
                                        #   返回的 dict 里＝静默失效（本项目踩过）。
                                        'mouse': mouse_status(),
                                        # ★★ 2026-10-01（第卅二轮 · 用户第 1 条）：「键盘」
                                        #   页的实况（有没有辅助功能权限 / 引擎跑没跑）。
                                        #   ⚠️ 这里**只回三个小值**（keymap_live），
                                        #   不是整份键盘配置 —— 那是「进这一页拉一次」的量级，
                                        #   塞进 3 秒轮询白烧 IO。同样必须放在**这个响应字典**里。
                                        'keymap': keymap_live(),
                                        'uiVer': page_ver(),
                                        'menuVer': menu_ver(),
                                        # ★★ 2026-09-29（用户第 11 条）：正在跑的内嵌任务
                                        #   放**顶层**（与 uiVer/menuVer 同级）——
                                        #   前端窗口重载后靠它恢复卡片右侧的运行指示。
                                        'jobs': running_jobs_snapshot()})

            if path == '/api/power':
                heal_once()
                return self._send(200, {'ok': True, 'power': build_power()})

            # 页面每隔几秒把窗口尺寸报回来，下次启动才能按它复原
            if path == '/api/winsize':
                data = json.loads(body) if body else {}
                return self._send(200, {'ok': save_win_size(data)})

            if path == '/api/run':
                data = json.loads(body) if body else {}
                return self._send(200, do_run(data))

            if path == '/api/job':
                jid = query.get('id', [''])[0]
                with JOB_LOCK:
                    job = JOBS.get(jid)
                if not job:
                    return self._send(404, {'error': 'no such job'})
                return self._send(200, {
                    'id': job['id'], 'num': job['num'], 'desc': job['desc'],
                    # ★ 2026-09-29：界面键（卡片 data-key）。页面重载后靠它恢复
                    #   卡片右侧的运行指示（见 /api/status 的 jobs 快照）。
                    'key': job.get('key', ''),
                    'cmd': ' '.join(os.path.basename(c) if i == 0 else c
                                    for i, c in enumerate(job['cmd'])),
                    'state': job['state'], 'rc': job['rc'],
                    'elapsed': round((job['t1'] or time.time()) - job['t0'], 1),
                    'out': ''.join(job['out']),
                })

            if path == '/api/stop':
                data = json.loads(body) if body else {}
                return self._send(200, job_stop(data.get('id')))

            if path == '/api/openhere':
                try:
                    if IS_WIN:
                        subprocess.Popen(['explorer', HERE])
                    elif IS_MAC:
                        subprocess.Popen(['open', HERE])
                    else:
                        subprocess.Popen(['xdg-open', HERE])
                    return self._send(200, {'ok': True})
                except Exception as e:
                    return self._send(200, {'ok': False, 'msg': repr(e)})

            # —— 在访达里打开「快速操作」所在目录（右键助手那张卡的「在访达里看」）——
            #   ⚠️ `~/Library` 在访达里默认是隐藏的，但 `open` 点名那个目录没问题。
            if path == '/api/qa-open':
                try:
                    if not IS_MAC:
                        return self._send(200, {'ok': False, 'msg': '只有 macOS 有「快速操作」'})
                    d = os.path.expanduser('~/Library/Services')
                    os.makedirs(d, exist_ok=True)
                    subprocess.Popen(['/usr/bin/open', d])
                    return self._send(200, {'ok': True, 'path': d})
                except Exception as e:
                    return self._send(200, {'ok': False, 'msg': repr(e)})

            # 在终端里打开黑窗编号菜单（界面上「编号菜单」按钮走这条）
            if path == '/api/term-menu':
                return self._send(200, open_menu_terminal())

            # ================= 2026-09-29 新增：独立视图的接口 =================
            #
            # ★★ 这一组是用户第 5/6 条要求的落地：「点开之后出现 ui 界面，让我选择
            #    具体里面的其他功能」「单独做一个打开的 ui 界面……选中，之后点击
            #    一个恢复按钮就可以」。全部**只读或同步一次**，结果直接回给视图，
            #    **不打开右侧终端/输出面板**（第 7 条）。
            #
            # ⚠️ 一律**不放进** /api/status 的 3 秒轮询 —— 这里每个都 fork 子进程。

            # —— 「鼠标」偏好（GET 读 / POST 存）——
            #   ★★ 2026-09-30（用户第 2 条）：真正干活的是外壳里的平滑滚动引擎，
            #   这里只负责把三个值落到 `_鼠标.json`；外壳盯着这个文件、事件驱动地重读。
            if path == '/api/mouse':
                if method == 'POST':
                    data = json.loads(body) if body else {}
                    return self._send(200, {'ok': True, 'mouse': save_mouse_prefs(data)})
                return self._send(200, {'ok': True, 'mouse': mouse_status(),
                                        'speeds': SMOOTH_SPEEDS})

            # —— 「键盘」映射（GET 读全量 / POST 存一小段）——
            #   ★★ 2026-10-01（用户第 1 条）：把 Karabiner-Elements 的全部键盘映射
            #   与 Command X-V 的 Finder 剪切增强收进「键盘」栏目。这里只读写配置，
            #   真正干活的是外壳里的 KeyEngine（CGEventTap）。
            if path == '/api/keymap':
                if method == 'POST':
                    data = json.loads(body) if body else {}
                    return self._send(200, apply_keymap_patch(data))
                return self._send(200, keymap_status())

            # —— 重新从 Karabiner 导一次（用户改了 karabiner.json 之后想同步过来）——
            #   ★ 合并而不覆盖：同一 `sig` 的条目沿用用户调过的开关与键位。
            if path == '/api/keymap-import':
                if method == 'POST':
                    K = _keymap_mod()
                    with _KEYMAP_LOCK:          # ★ 与 apply_keymap_patch 共用一把锁，
                        cfg = K.import_from_karabiner(write=True)   # 免得与补丁互相覆盖
                    d = keymap_status()
                    d['imported'] = cfg is not None
                    return self._send(200, d)
                return self._send(200, {'ok': False, 'error': '只支持 POST'})

            # —— 打开「辅助功能」设置页（平滑滚动要的权限就在那一栏）——
            #   ★ 只能由用户自己去勾：TCC 的授权框不给程序代劳。这里只负责把那一页
            #     替他翻出来，省得让他在系统设置里一层层找（PommeToys 也是这么做的）。
            if path == '/api/mouse-perm' or path == '/api/keymap-perm':
                url = ('x-apple.systempreferences:com.apple.preference.security'
                       '?Privacy_Accessibility')
                try:
                    if IS_MAC:
                        subprocess.Popen(['/usr/bin/open', url])
                        return self._send(200, {'ok': True, 'opened': url})
                    return self._send(200, {'ok': False, 'error': '只有 macOS 有这个设置页'})
                except Exception as e:
                    return self._send(200, {'ok': False, 'error': repr(e)})

            # —— 右键助手（系统「快速操作」）——
            #   GET 读状态 / POST 装卸单项 / POST{refresh:1} 让右键菜单立刻生效 /
            #   POST{pick:1} 弹原生选文件框挑「word模板创建」用的模板 /
            #   POST{template:"…"} 直接指定模板。
            #   ★ 只在**用户主动打开鼠标页**时拉一次，不进 3 秒轮询（它 fork 子进程）。
            if path == '/api/quickactions':
                if method == 'POST':
                    data = json.loads(body) if body else {}
                    # ★ 先认「挑模板 / 指定模板」——它们与装卸是同一张卡的三个动作。
                    #   顺序要紧：`pick` / `template` 都不带 `name`，排在装卸前面最清楚。
                    if data.get('pick'):
                        ok, msg, cancel = quickactions_pick_template()
                        d = quickactions_status()
                        d['pick'] = {'ok': ok, 'cancel': cancel, 'msg': msg}
                        return self._send(200, {'ok': True, 'quick': d})
                    if data.get('template') is not None:
                        ok, msg = quickactions_set_template(data.get('template'))
                        d = quickactions_status()
                        d['setTemplate'] = {'ok': ok, 'msg': msg}
                        return self._send(200, {'ok': True, 'quick': d})
                    if data.get('refresh'):
                        # ★★ 走**外壳桥**：`pbs -update` 在工具进程里恒报
                        #   `XPC message error: Connection interrupted`（它要连的是
                        #   用户的图形会话），而外壳由 launchd 在那个会话里拉起、
                        #   不在沙箱 → 只有它跑得动（与 launchctl bootstrap 同一个道理）。
                        ok, why = bridge_call('pbs', '', wait=25.0)
                        d = quickactions_status()
                        d['refresh'] = {'ok': bool(ok), 'msg': why}
                        return self._send(200, {'ok': True, 'quick': d})
                    name = str(data.get('name') or '')
                    if not name:
                        return self._send(200, {'ok': False, 'error': '缺少 name'})
                    ok, msg = quickactions_apply(name, bool(data.get('on')))
                    d = quickactions_status()
                    d['apply'] = {'ok': ok, 'name': name,
                                  'on': bool(data.get('on')), 'msg': msg}
                    return self._send(200, {'ok': True, 'quick': d})
                return self._send(200, {'ok': True, 'quick': quickactions_status()})

            # —— 开机自启（GET 读 / POST 开关）——
            if path == '/api/autostart':
                if method == 'POST':
                    data = json.loads(body) if body else {}
                    if 'silent' in data and 'on' not in data:
                        # 只改「静默」这一项：保持当前开关状态，重写 plist
                        st, warn = set_autostart(autostart_state()['on'], data.get('silent'))
                    else:
                        st, warn = set_autostart(bool(data.get('on')), data.get('silent'))
                    return self._send(200, {'ok': True, 'autostart': st, 'warn': warn})
                return self._send(200, {'ok': True, 'autostart': autostart_state()})

            # —— 音效偏好（GET 读 / POST 存）——
            if path == '/api/sound':
                if method == 'POST':
                    data = json.loads(body) if body else {}
                    return self._send(200, {'ok': True, 'prefs': save_sound_prefs(data)})
                return self._send(200, {'ok': True, 'prefs': sound_prefs(),
                                        'sets': sound_list()})

            # —— 音效文件本体（页面用 <audio> 拉）——
            #   只从 SOUND_DIR 里取，且**必须**是 .wav、名字不含路径分隔符
            #   （`os.path.basename` 已经挡掉 ../ 这类穿越）。
            if path.startswith('/sounds/'):
                name = os.path.basename(path[len('/sounds/'):])
                fp = os.path.join(SOUND_DIR, name)
                if not name.endswith('.wav') or not os.path.exists(fp):
                    return self._send(404, {'error': 'no such sound'})
                with open(fp, 'rb') as f:
                    return self._send(200, f.read(), 'audio/wav')

            # —— 界面字体（Instrument Sans，随包一份）——
            #   ★★ 2026-09-29：与 /sounds/ 同一套防护 —— 只认白名单后缀，
            #   basename 已经挡掉 ../ 这类穿越。
            if path.startswith('/fonts/'):
                name = os.path.basename(path[len('/fonts/'):])
                fp = os.path.join(FONT_DIR, name)
                if not name.endswith(('.ttf', '.otf')) or not os.path.exists(fp):
                    return self._send(404, {'error': 'no such font'})
                with open(fp, 'rb') as f:
                    return self._send(200, f.read(), 'font/ttf')

            # —— 「状态与设置」（窗口右上角那个按钮）——
            if path == '/api/settings':
                d = build_settings()
                d['ok'] = True
                return self._send(200, d)

            if path == '/api/quit':
                # ★ 2026-09-28（用户要求）：关闭服务 = 这一轮结束，运行监测要**还原成默认**。
                #   ★ 2026-10-01 补注：这里的「还原成默认」**只指两个一次性的附加项**，
                #     不含 5 个「先别睡」开关 —— 见下面第廿九轮的说明。
                #   原话：「关闭服务或者关闭重启（包括系统重启）之后，要还原成默认状态，
                #          不要让『跑完自动关机』还是打开状态」。
                #   为什么放在这里而不是退出时的 finally：/api/quit 是唯一的「用户主动关闭」
                #   入口（点窗口右上角 ✕ 只是关窗口、服务仍在，不该归零）。放这里语义最准。
                #
                # ★★★ 2026-10-01（第廿九轮）**改走 `--reconcile`，不许再走 `--reset`**：
                #   上面那句「还原成默认」指的只是**两个一次性的附加项**（跑完自动关机 /
                #   到点自动结束）——它们本来就只活在那一次开机里。而 `--reset` 是个
                #   「彻底还原」档，会**连 5 个「先别睡」开关一起归零** → 用户点一次
                #   「关闭服务」，防休眠就被静默关掉（界面上只留一行「运行监测 → 关闭」，
                #   看不出是谁干的；`_运行监测.log` 里这条自愈文案出现过 24 次）。
                #   用户 2026-10-01 原话：「防休眠这个功能本身就是要保持后台的……
                #   除了重启以外，任何睡眠或者合盖唤醒之后，它上次是什么样还是什么样」。
                #   → 日常动作一律用 `--reconcile`：收掉附加项 ＋ 按用户现在的开关把
                #     守护**对齐**（开着就继续守，关着就退出），**绝不碰开关的姿势**。
                def _reset_then_quit():
                    try:
                        subprocess.run([find_python(),
                                        os.path.join(HERE, 'wb_powerguard.py'),
                                        '--reconcile'],
                                       cwd=HERE, env=_env_for_child(),
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=60)
                        log('关闭服务：已收掉上次开机的一次性附加项；'
                            '「防休眠」等开关的姿势**保持不动**（守护按需继续值守）')
                    except Exception as e:
                        log('关闭服务时收拾运行监测状态失败：%r' % (e,))
                    try:
                        self.server.shutdown()
                    except Exception:
                        pass

                threading.Thread(target=_reset_then_quit, daemon=True).start()
                return self._send(200, {'ok': True})

            if path == '/api/restart':
                # ★★ 2026-09-29（用户第 17 条）：**原地重启**服务。
                #
                # 为什么要它：原来页面发现「服务代码比我的新」时，是提示用户去点
                #   「关闭服务」再重新打开界面。而「关闭服务」是 `/api/quit` —— 它会把
                #   服务**退出**（连带外壳一起退）→ 用户一照做，**界面整个死掉**，
                #   非重新双击 app 不能恢复。实测里这套动作连着把好端端的服务关了两次
                #   （`_界面.log` 有「关闭服务：运行监测已还原成默认…服务退出」）。
                #   现在页面改成**自己**请求这里：服务原地重启、页面自己 reload，
                #   用户什么都不用做，也不会有人把服务关死了。
                #
                # 为什么用 `os.execv`：它是**替换当前进程映像**，不 fork 新进程，
                #   所以在受限沙箱里同样可用；端口能立刻重绑（HTTPServer 开了
                #   allow_reuse_address，不吃 TIME_WAIT）。
                # ⚠️ 先睡 0.4 秒，把这一个 HTTP 响应送出去再换新。
                def _restart_self():
                    time.sleep(0.4)
                    try:
                        log('页面请求换新代码：服务原地重启')
                        os.execv(sys.executable, [sys.executable] + sys.argv)
                    except Exception as e:
                        log('原地重启失败：%r（退化为直接退出，请重开一次界面）' % (e,))
                        os._exit(3)

                threading.Thread(target=_restart_self, daemon=True).start()
                return self._send(200, {'ok': True})

            return self._send(404, {'error': 'not found'})

        except Exception as e:
            log('处理 %s 出错：%r' % (path, e))
            try:
                return self._send(500, {'error': repr(e)})
            except Exception:
                pass


# ═══════════════ 运行监测守护的「启动对齐」与「事件驱动看门狗」 · 2026-10-09
#   （从 Windows 版移植；Mac 用 kqueue EVFILT_PROC 等守护退出，零轮询）
#
# ★ 为什么加：配置(_运行监测.json)是持久的，守护进程不是 —— 它只在
#   ① 手拨开关(--toggle) ② 改档位 ③ 关闭服务(--reconcile) 时被拉起。
#   除此之外没人管：登录自启只跑 --serve、服务重启也不拉它。
#   ⇒ 守护被谁杀掉 / 电脑重启后，就落进「配置写着已开、实际没人守」的状态。
#   两层修法（都不动开关姿势）：① 启动对齐 ② 看门狗（守护一退就当场上拉）。
POWER_WD_FUSE_SEC = 600.0      # 看门狗保险丝（秒）——只为兜「事件没送到」
_PWR_RECON_LOCK = threading.Lock()
_PWR_RECON_BUSY = [False]      # 一次只跑一个 --reconcile（启动对齐与看门狗可能撞上）
_PWR_WD_LAST_PULL = [0.0]      # 上一次「补拉」的时刻
_PWR_WD_PULL_GAP = 30.0        # 两次补拉之间至少隔这么久


class _WakeSignal(object):
    """跨线程「立刻醒」信号（看门狗事件驱动的那一半）。

    Mac 实现 = 一个**自管道**（self-pipe）：`notify()` 往写端写一个字节，
    看门狗的 kqueue 同时盯「守护进程退出」与「这个管道可读」——
    两者谁先来醒谁，**真事件驱动、零轮询**。
    （Windows 版用 CreateEventW + WaitForMultipleObjects，语义等价。）
    """
    def __init__(self):
        self._r, self._w = os.pipe()
        try:
            os.set_blocking(self._r, False)
            os.set_blocking(self._w, False)
        except Exception:
            pass
        self._ev = threading.Event()

    def rfd(self):
        return self._r

    def notify(self):
        """叫醒等待中的看门狗。可从任何线程调，**永不抛**。"""
        try:
            os.write(self._w, b'x')
        except Exception:
            pass
        try:
            self._ev.set()
        except Exception:
            pass

    def drain(self):
        try:
            while True:
                if not os.read(self._r, 4096):
                    break
        except Exception:
            pass
        try:
            self._ev.clear()
        except Exception:
            pass

    clear = drain

    def wait(self, timeout):
        """阻塞等到「有人叫」或超时。返回 True = 被叫醒。"""
        try:
            return self._ev.wait(max(0.0, float(timeout)))
        except Exception:
            return False


_PWR_WD_WAKE = _WakeSignal()


def power_wd_nudge(why=''):
    """喊一声看门狗：「值守状态可能变了，当场复核一次」。"""
    _PWR_WD_WAKE.notify()


def _power_reconcile_bg(why):
    """后台跑一次 `wb_powerguard.py --reconcile`（收附加项 ＋ 按开关对齐守护）。

    ★ 照搬 /api/quit 那条命令，不在这里重写判断（同一件事存两份迟早漂移）。
    ★ 放线程里：--reconcile 内部可能 start_daemon()，最多等 12 秒，不能卡启动路径。
    ★ 返回线程对象（忙时 None）—— 看门狗要 join 它，等补拉落地再往下走。
    """
    with _PWR_RECON_LOCK:
        if _PWR_RECON_BUSY[0]:
            return None
        _PWR_RECON_BUSY[0] = True

    def _go():
        try:
            p = subprocess.run(
                [find_python(), os.path.join(HERE, 'wb_powerguard.py'), '--reconcile'],
                cwd=HERE, env=_env_for_child(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=90)
            log('守护对齐（%s）：--reconcile 返回 %s' % (why, p.returncode))
        except Exception as e:
            log('守护对齐失败（%s）：%r' % (why, e))
        finally:
            _PWR_RECON_BUSY[0] = False
            _PWR_WD_WAKE.notify()      # 对齐完了 → 让看门狗立刻复核一次

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t


def _power_need_but_absent():
    """「还有东西该由守护去守、守护却不在跑」吗？在跑/不需要就返回 None。"""
    import wb_powerguard as PG
    conf = PG.load_conf()
    if not PG.need_daemon(conf):
        return None                     # 没有需要值守的项 → 本就不该有守护
    if PG.state_alive(PG.read_state()):
        return None                     # 在跑
    try:
        return PG.feat_desc(PG.feats(conf))
    except Exception:
        return '（读不出开关名）'


def _power_wait_daemon(pid, timeout):
    """阻塞等：守护退出 / 有人喊 / 保险丝到点。

    返回 'exited' / 'wake' / 'fuse' / 'gone'。
    ★ Mac 主路径：kqueue 注册 EVFILT_PROC(NOTE_EXIT) 盯守护 pid，再挂上
      self-pipe 的 EVFILT_READ —— 守护活着时这个线程挂在内核里，零轮询。
    ⚠️ 注册失败（pid 已没了 / 权限）→ 'gone'/'exited'，交给下一轮 state_alive 复核。
    """
    try:
        pid = int(pid)
    except Exception:
        return 'gone'
    if pid <= 0:
        return 'gone'
    end = time.time() + max(1.0, float(timeout))
    kq = None
    try:
        import select
        if hasattr(select, 'kqueue'):
            kq = select.kqueue()
            evs = [
                select.kevent(pid, filter=select.KQ_FILTER_PROC,
                              flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                              fflags=select.KQ_NOTE_EXIT),
                select.kevent(_PWR_WD_WAKE.rfd(), filter=select.KQ_FILTER_READ,
                              flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE),
            ]
            kq.control(evs, 0, 0)
    except Exception:
        try:
            if kq:
                kq.close()
        except Exception:
            pass
        kq = None

    if kq is None:
        # 退回轮询（无 kqueue / 注册失败）
        import wb_powerguard as PG
        while time.time() < end:
            if _PWR_WD_WAKE.wait(min(2.0, max(0.2, end - time.time()))):
                return 'wake'
            try:
                if not PG._pid_alive(pid):
                    return 'exited'
            except Exception:
                pass
        return 'fuse'

    try:
        while True:
            left = end - time.time()
            if left <= 0:
                return 'fuse'
            try:
                got = kq.control(None, 4, left)
            except Exception:
                got = []
            for e in got:
                if getattr(e, 'filter', None) == getattr(select, 'KQ_FILTER_PROC', 0xFFFFFFFF):
                    return 'exited'
                if e.ident == _PWR_WD_WAKE.rfd():
                    return 'wake'
    finally:
        try:
            kq.close()
        except Exception:
            pass


def _power_wd_loop():
    """看门狗主循环（事件驱动）—— 每轮只做两件事。
      ① 复核：还有东西该守、守护却不在 → 补拉（30 秒防抖）；
      ② 守候：拿得到 pid → 阻塞等它退出；拿不到 → 睡到「界面喊我」或保险丝。
    """
    while True:
        try:
            desc = _power_need_but_absent()
            if desc is not None:
                now = time.time()
                if now - _PWR_WD_LAST_PULL[0] >= _PWR_WD_PULL_GAP:
                    _PWR_WD_LAST_PULL[0] = now
                    log('看门狗：还需要值守（%s），但守护不在跑 → 补拉' % desc)
                    t = _power_reconcile_bg('看门狗')
                    if t is not None:
                        t.join(25)      # 等补拉落地，免得紧接着又判"没人守"
            pid = 0
            try:
                import wb_powerguard as PG
                st = PG.read_state()
                if PG.state_alive(st):
                    pid = int(st.get('pid') or 0)
            except Exception:
                pid = 0
            if pid:
                why = _power_wait_daemon(pid, POWER_WD_FUSE_SEC)
                if why == 'exited':
                    log('看门狗：守护 %s 已退出 → 立即复核' % pid)
            else:
                _PWR_WD_WAKE.wait(POWER_WD_FUSE_SEC)
            _PWR_WD_WAKE.clear()
        except Exception as e:
            log('看门狗异常：%r' % (e,))
            time.sleep(30.0)


def start_power_guard():
    """服务就绪时调用**一次**：把运行监测守护对齐，并从此长期盯着它。

    ⚠️ 只做「按 need_daemon() 对齐」这件事 —— 不写任何开关、不动配置姿势。
    """
    _power_reconcile_bg('服务启动')
    threading.Thread(target=_power_wd_loop, name='wb-ui-power-wd', daemon=True).start()
    log('运行监测守护：已挂上「启动对齐 ＋ 事件驱动看门狗（kqueue，保险丝 %g 秒）」'
        % POWER_WD_FUSE_SEC)


def make_server(port):
    """标准库 http.server —— 不自造协议解析（少一类 bug）。

    用 HTTP/1.0：每个请求一条连接、回答完就关。本地回环上这点开销可以忽略，
    换来的是「绝不会有连接复用/超时导致的挂死」。
    """
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse, parse_qs

    class H(BaseHTTPRequestHandler):
        server_version = 'WBUI/1.0'

        def log_message(self, fmt, *args):
            pass                      # 不打到 stderr（pythonw 下它可能是 None）

        def _go(self):
            u = urlparse(self.path)
            try:
                n = int(self.headers.get('Content-Length') or 0)
            except Exception:
                n = 0
            body = ''
            if n:
                try:
                    body = self.rfile.read(n).decode('utf-8', 'replace')
                except Exception:
                    body = ''
            h = Handler(self.server)
            h.wfile = self.wfile
            h.handle(self.command, u.path, parse_qs(u.query), body)

        do_GET = _go
        do_POST = _go

    srv = ThreadingHTTPServer((HOST, port), H)
    srv.daemon_threads = True
    return srv


# ------------------------------------------------------------------ 启动

def _spawn_server(port):
    """后台拉起一个服务进程（脱离调用者，黑窗/父进程关掉也照跑）。

    Windows：DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP，让它彻底脱离那个黑窗。
    macOS  ：**必须 `start_new_session=True`（= setsid 另开一个会话）**。
      ⚠️ 这是本项目踩过的实锤坑（见技能 macos-port-win-script-tool 第四节）：
         `nohup … &` 只挡 SIGHUP、**不新建会话**，发起它的进程组一被回收
         （launchd 收作业、终端关窗、外壳 app 退出），服务会被**连带杀掉**。
         症状极具迷惑性：日志里有正常启动记录、过一会儿进程却没了、**且没有任何
         退出日志**。用 start_new_session 另起会话才是正解。
    """
    pyw = find_pythonw()
    flags = 0x00000008 | 0x00000200
    subprocess.Popen(
        [pyw, os.path.abspath(__file__), '--serve', '--port', str(port)],
        cwd=HERE, env=_env_for_child(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=(flags if IS_WIN else 0),
        start_new_session=IS_MAC,
        close_fds=True,
    )


def ensure_service(open_win=False):
    """保证后台服务在跑。返回 0 = 已就绪。

    两个调用者：
      · macOS 的窗口外壳 app（Swift）—— 它先跑 `--ensure`，再来开窗口，
        所以「双击 app」这一条路完全不依赖任何 Python 查找逻辑；
      · 下面的 launch()。
    已经在跑就什么都不做（单实例靠端口探测，与 main() 同一套判据）。
    """
    conf = load_conf()
    port = int(conf.get('port') or DEFAULT_PORT)

    if _port_busy(port):
        log('--ensure：端口 %d 已在服务' % port)
        if open_win:
            open_window(port, block_icon=True)
        return 0

    if not os.path.exists(PAGE_FILE):
        log('✗ 找不到界面文件 %s' % PAGE_FILE)
        return 2

    log('--ensure：拉起后台服务（%s）' % find_pythonw())
    try:
        _spawn_server(port)
    except Exception as e:
        log('--ensure 拉服务失败：%r' % (e,))
        return 3

    # 等它就绪（最多 ~6 秒）；起来了再开窗口，免得窗口先弹一个「无法访问」
    for _ in range(40):
        if _port_busy(port, timeout=0.4):
            break
        time.sleep(0.15)
    else:
        log('--ensure：等待服务就绪超时（%d 秒）' % 6)
        return 4

    log('--ensure：服务就绪 http://%s:%d/' % (HOST, port))
    if open_win:
        open_window(port, block_icon=True)
    return 0


def launch():
    """给入口脚本用的入口：**立刻返回**，不留黑窗。

    已经在跑 → 直接把窗口叫出来；没跑 → 后台起一个服务再把窗口叫出来。
    Windows 的「界面.bat」只写一句 `call _run.bat wb_ui.py --launch`，
    「找一个 Python」那套逻辑复用现成的 _run.bat，不重写一遍。
    macOS 对应的是「界面.command」/ 直接双击 app（app 自己会跑 --ensure）。
    """
    rc = ensure_service(open_win=True)
    if rc == 4:
        log('--launch：等待超时，改用带控制台的方式起服务以便看到错误')
        try:
            subprocess.Popen([find_python(), os.path.abspath(__file__), '--serve'],
                             cwd=HERE, env=_env_for_child(),
                             creationflags=(0x00000010 if IS_WIN else 0))
        except Exception as e:
            log('兜底启动也失败：%r' % (e,))
        return 4

    open_window(port, block_icon=True)
    return 0


# ------------------------------------------------------------------ main

def main():
    argv = sys.argv[1:]
    port = DEFAULT_PORT
    serve_only = False
    force_port = None

    for i, a in enumerate(argv):
        if a == '--launch':
            return launch()
        # ★ macOS 的窗口外壳 app 先跑这个：只保证服务在跑，不动窗口。
        if a == '--ensure':
            return ensure_service(open_win=False)
        # ★ 补救用（2026-09-27 加）：把**当前已经开着**的界面窗口图标就地换成高清档。
        #   窗口图标只在创建时定，改了 .ico 或修好代码后，老窗口不会自己变 ——
        #   要么重开窗口，要么跑一次这条。
        #   macOS 不适用（窗口图标由 app bundle 决定），直接返回 0。
        if a == '--fix-icon':
            return 0 if (not IS_WIN or _apply_window_icon()) else 1
        if a == '--serve':
            serve_only = True
        elif a == '--port' and i + 1 < len(argv):
            force_port = argv[i + 1]

    conf = load_conf()
    if force_port:
        try:
            port = int(force_port)
        except Exception:
            port = DEFAULT_PORT
    else:
        port = int(conf.get('port') or DEFAULT_PORT)

    log('=== 启动 %s (port=%d, serve_only=%s) ===' % (sys.executable, port, serve_only))

    # 已经在跑 → 只把窗口叫出来，本进程退出
    if _port_busy(port):
        log('端口 %d 已被占用，判定已有实例在跑 → 只开窗口' % port)
        if not serve_only:
            open_window(port, block_icon=True)
        return 0

    if not os.path.exists(PAGE_FILE):
        log('✗ 找不到界面文件 %s' % PAGE_FILE)
        return 2

    try:
        srv = make_server(port)
    except OSError as e:
        log('✗ 端口 %d 起不来：%r' % (port, e))
        # 换一个随机高位端口再试
        try:
            port = port + 1 if port < 65000 else 49640
            srv = make_server(port)
            conf['port'] = port
            save_conf(conf)
            log('改用端口 %d' % port)
        except Exception as e2:
            log('✗ 还是起不来：%r' % (e2,))
            return 3

    # ★★★ 2026-10-01（第四十八轮 · 用户第 3 条「这个我不懂是干嘛用的」）：
    #   **状态预热线程撤掉了。**
    #   它原来的唯一职责：页面每 3 秒问一次 /api/status，而 build_status() 现算要
    #   ~90ms —— 预热着，点按钮那一下才能 5ms 出结果、不觉得卡。
    #   现在页面改成**事件推送**（/api/events），请求次数从「每 3 秒一次」降到
    #   「状态真的变了才一次」，这点开销完全不值得再养一个**常驻 2 秒线程**。
    #   ⚠️ 缓存机制本身留着（build_status 仍会读/填 _STATUS_SNAP），
    #      只是不再有人常驻为它续热 —— 即「没人问，就不烧 CPU」。
    #   ⚠️ `_status_warmer()` 的函数定义也留着：它不是死代码，
    #      万一 SSE 出问题，把下面这行取消注释就退回旧行为（最省事的应急手段）。
    # threading.Thread(target=_status_warmer, daemon=True).start()

    # ★ 2026-10-09：服务一启动就把「运行监测守护」按当前开关**对齐拉起**。
    #   背景：守护原来只在「改开关 / 切档位 / 关闭服务(--reconcile)」时对齐；
    #   而「关闭服务 → 重新打开 TimeGuardian」这条日常路径里，服务重启这段
    #   **没人管守护** → 开关还开着、守护却没回来，界面一直显示
    #   「配置已开，但守护没在跑」。用户 2026-10-01 明确：「防休眠……
    #   除了重启以外，上次什么样还是什么样」—— 启动即对齐，开关的姿势说了算
    #   （全关着 → sync_enabled 什么都不做）。
    # ★ 2026-10-09：服务一就绪就把「运行监测守护」对齐，并挂上事件驱动看门狗。
    #   （移植自 Windows 版 2026-10-03 那套；Mac 版此前缺了这块，
    #     于是「关闭服务 / 重启 → 开关还开着、守护却没回来」。）
    try:
        start_power_guard()
    except Exception as _e:
        log('挂载运行监测守护（启动对齐＋看门狗）失败：%r' % (_e,))

    # ★ 新起的是它：盯守护的状态文件，一变就推给所有长连接。
    start_state_watch()

    if not serve_only:
        threading.Timer(0.8, lambda: open_window(port)).start()

    log('服务就绪：http://%s:%d/' % (HOST, port))
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        log('服务退出')
    return 0


if __name__ == '__main__':
    sys.exit(main())
