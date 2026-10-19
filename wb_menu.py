#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 任务管家 —— 编号菜单（macOS）
================================================================
双击 /Applications/WorkBuddy任务管家.app 即打开**图形界面**（点击操作，不用记编号）；
本文件是同一件事的「黑窗版」，在图形界面里点「打开编号菜单」或在终端跑
同目录的 菜单.command 都能到这儿。

菜单编号与 Windows 端的 0_ ～ 9_、00_ 入口一一对应，用法不变。
所有实际动作都交给同目录下的 .py 脚本执行，这里只负责"选编号 + 跑 + 回显"。

编号体系（2026-09-27 重排 · 用户要求「每个板块都从 1 开始」）：
    ★ 显示编号 = 「板块内序号」，每个板块都从 1 重新开始数。
      板块顺序：环境 → ★一键 → 本机同步 → 跨机同步 → 运行监测 → 空间回收 → macOS 专属
      ⚠️ 唯一的例外是『★ 一键』—— 它显示 **0**（与 Windows 端同号，2026-09-27 用户指定），
         见下面的 SEQ_OVERRIDE；其余条目一律是板块内序号。
      系统内部认的是「板块前缀 + 序号」的唯一键，例如 local3（本机同步·第 3 项）、
      cross1（跨机同步·第 1 项）、power1（运行监测·防休眠总开关）。
      ⚠️ 为什么不能只用纯数字：重排后每个板块都有「1」，
         光看数字分不清是哪个板块的那一条（输入「1」没法判断要跑什么）。
    · 黑窗菜单：**先输板块号（1~7），再输该板块里的条目号**；
      也可以直接敲完整键（如 local3）一步到位。
    · 图形界面：卡片徽标显示板块内序号，点击即执行 —— 不用记编号。
    ⚠️ 本编号与 Windows 端**不再逐号对应**（Win 端仍是 00/01/0/1~9/X/11~16）。
      这是 2026-09-27 用户明确要求的、只针对 Mac 侧的改动，两端对照表见《使用说明_macOS.md》。
    ★★ 2026-09-29（用户要求「对任务管家做减法和修改」）又精简过一轮：
      · 「环境自检与部署」＋「拍照快照」→ 合成一条 **「部署和修复」**（env1）；
      · 删掉「预览全局单份折叠」「查看删除传播队列」两条；
      · macOS 专属整片 + 跨机同步总控 → **收成一张卡「更多工具…」**（macos1），
        面板里再挑具体功能；
      · 运行监测的「状态与设置面板」取消（改到窗口**右上角**的「状态与设置」）；
      · EXTRA 里的 `r` 取消（部署和修复已覆盖）。**现为 MENU 17 项 / EXTRA 3 项。**
    ★★★ 2026-10-05（**对齐 Windows 端**，用户要求「Mac 端和 Windows 端的所有按钮和
      所有功能都要和 Windows 端看齐，以 Mac 端的方式去实现」）：
      · 新增 **one2「★ 同步账号」**（排在「一键整合」下面，复用 WorkDaddy 自带的
        导出/导入账号接口，中间加一层「择优保护」）；
      · **local2（账号与会话全景，只读）删除** —— 它的能力并进 local1 的**前置检查**
        （界面里点「本机 · 任务折叠」会先把这份只读报告摊出来，再让你决定折不折）；
      · **local3（执行全局单份）并入 local1** —— 同一件事的两条实现路径，保留走常驻
        引擎那条（运行中也能跑、更安全），local3 那道保险挪到「更多工具」面板里的
        tool_global_force；
      · local1 / cross1 / cross2 **改名**成「本机 · 任务折叠 / 跨机 · 中转同步 /
        跨机 · 恢复误删会话」，三条一律「本机|跨机 · 动作」体例；
      · 「同步」板块因此 **6 项 → 4 项**（local1 / cross1 / cross2 / macos2）。
      ⇒ **现为 MENU 16 项 / EXTRA 3 项 / 6 个板块。**
      ⚠️ macos2 的**键与命令一个字没动**，只是图形界面里不再单列一张卡（收进
         「任务与工作空间…」管理器）；黑窗菜单敲它照旧。

图形界面（2026-09-27 移植 Windows 端）
    同目录的 wb_ui.py + _ui.html 起一个本地小服务，再开一个**没有地址栏**的窗口
    给用户点。它**不复制任何业务逻辑** —— 菜单结构、状态摘要、要跑的命令都从
    本文件读，所以「界面点一下」与「终端按编号」永远一致；以后加编号只改本文件一处。
    ★ macOS 的窗口外壳是 /Applications/WorkBuddy任务管家.app（Swift + WKWebView）——
      本机没装 Edge / Chrome（只有 Safari，而 Safari 不支持 --app=），所以不用
      浏览器的应用窗口模式，改用自带外壳，图标也与 Windows 端同一张脸。
    菜单里按 u 也能开界面。

设计约束
  · 纯标准库，零第三方依赖
  · 只调用本目录内的脚本，绝不写死绝对路径
  · 会改数据的动作（执行全局单份 / 清理死副本）先二次确认
"""
import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

IS_WIN = (os.name == 'nt')

# ⚠️ 这里只能用 reconfigure，**不能** sys.stdout = io.TextIOWrapper(sys.stdout.buffer)。
#    原因：wb_sync_global 在 import 时也会重新包装一次 sys.stdout；
#    若我们先包一层，随后它再包一层，先前那层会被回收并连带关掉 fd 1，
#    之后任何 print 都报 "I/O operation on closed file"（2026-09-12 踩过）。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import wb_paths as P                                          # noqa: E402
# TimeGuardian-Mac：不再依赖 wb_sync_global（同步属于任务管家）。守卫导入，
# 缺了也不影响本程序启动（下面 wb_state 会退化为「判不出」）。
try:
    import wb_sync_global as G                                # noqa: E402
except Exception:                                            # pragma: no cover
    G = None

PY = P.find_python()[0] or sys.executable
BAR = '=' * 78

# (唯一键, 说明, 脚本+参数, 是否需二次确认)
# ★ 唯一键 = 板块前缀 + 该板块内序号（从 1 起），例如 local3 / cross4 / power2。
#   前缀只用于「唯一化」，**不参与显示** —— 界面与黑窗菜单显示的都是板块内序号。
#   加条目 / 调顺序：改本表 + GROUPS 的成员列表即可，显示序号会自己跟着位置走
#   （所以永远不会出现「加了条目忘了改号」或「两个板块撞号」）。
MENU = [
    ('power1', '运行监测 · 防休眠：总开关（开 = 跑任务期间不睡眠、不休眠）',
                                                              ['wb_powerguard.py', '--toggle', 'awake'],     False),
    ('power2', '运行监测 · 屏幕保持常亮（子开关 · 主开关关着时会自动把它打开）',
                                                              ['wb_powerguard.py', '--toggle', 'display'],   False),
    ('power3', '运行监测 · 电量不足时自动停（子开关 · 用电池且低于 10% 时结束，让睡眠保住电量）',
                                                              ['wb_powerguard.py', '--toggle', 'lowbat'],    False),
    ('power4', '运行监测 · 合盖后保持唤醒（子开关 · 笔记本 · 免密 · 合盖靠事件通知）',
                                                              ['wb_powerguard.py', '--toggle', 'lid'],       False),
    ('power5', '运行监测 · 跑完自动关机（任务全部结束后关掉电脑 · 与「到点自动结束」互斥）',
                                                              ['wb_powerguard.py', '--shutdown'],            False),
    ('power6', '运行监测 · 到点自动结束（选一个时长，到点撤防休眠并停止监测 · 与「跑完自动关机」互斥）',
                                                              ['wb_powerguard.py', '--autostop'],            False),
    ('power7', '运行监测 · 停止（立即恢复系统默认电源行为，回到「什么都没干」）',
                                                              ['wb_powerguard.py', '--stop'],                False),
]

# 菜单分组：决定「有哪些板块、板块怎么排、每个板块里有哪些条目」。
# ★ 板块内的显示序号 = 条目在这个列表里的位置（从 1 起）—— 不额外维护数字，
#   所以永远不会出现「加了条目忘了改号」或「两个板块撞号」。
# ★ 顺序即界面顺序；板块号（黑窗菜单第一级输入的 1 / 2 / 3…）也按这个顺序。
# ★★ 2026-09-29 晚（用户第 3、4、5 条）：**七板块收成六板块** ——
#   ① 「本机同步」＋「跨机同步」两个板块**合并成一个「同步」**（用户原话：「本机同步
#      与跨机同步，把里面的图标都合并了，然后上级只放一个。图标叫：同步」），
#      并接收从 macOS 板块挪来的「合并会话」；
#   ② 「运行监测」提到**第三位**（用户原话：「运行监测放到部署和一键这两个图标的
#      后面放到第三位置」）；
#   ③ macOS 板块只剩「更多工具…」（用户原话：「更多工具这里就不用折叠了」）。
#   ⚠️ 各条目的**唯一键一个字没改**（local1 / cross1 / macos2 …）—— 键名是「当初
#      从哪来」的历史标记，归属一律看 KEY2GKEY（由本表派生）。改键名会牵动
#      TERM_NUMS / VIEW_KEYS / UI_HINTS / 检查脚本 / 文档十几处，收益只是好看。
#   ⚠️ 板块个数改了要同步 `_chk_renumber.py` 第 [2] 段的期望值（那里断言板块数）。
GROUPS = [
    # TimeGuardian-Mac：只保留「运行监测」一块 —— 时间守护的全部职能。
    # 任务/同步/空间回收/更多工具 属于「任务管家」，本程序一概不提供。
    ('power', '运行监测（WorkBuddy 跑任务时不让电脑睡）',
     ['power1', 'power2', 'power3', 'power4', 'power5', 'power6', 'power7']),
]

# ★★ 2026-09-29（用户第 7 条）：**板块在图形界面上怎么呈现**。
#   用户原话：「后 3 个按键不用调整，因为它们内容比较多，需要收起来。对于前 3 个按键，
#   我觉得还不如都放在前台 —— ① 取消这前 3 个按键，把其中的内容直接铺开；
#   ② 后 3 个按键再单独列出来，作为这种收起来的、点击后出现第 2 个 UI 界面的按键。」
#     'flat'      = 内容**直接铺在首页**（不再套「分类 tab」那一层）；
#     'collapsed' = 首页上只留一个按钮，点开是**第二层界面**。
#   ⚠️ 只影响图形界面。黑窗编号菜单照样一级级列全，不看这张表。
#   ⚠️ 每个板块都必须表态；漏了 / 写错会被 _chk_renumber.py 第 [2] 段当场抓住。
#   ⚠️ 判据不是「条目多少」而是「铺在首页会不会淹掉主界面」：
#      空间回收只有 1 项，但它要读磁盘、要挑文件，本身就是一整个界面 → 照样收起来。
GROUP_PRESENT = {
    'power': 'flat',        # 运行监测（一排开关，天天要看着）
}

# ---- 由上面两张表派生的查表结构（界面与黑窗菜单**共用这一份**，别在别处另建）----
KEY2ITEM = {k: (desc, cmd, risky) for k, desc, cmd, risky in MENU}

# ★ 显示序号覆盖表（2026-09-27 用户要求：「★ 一键」前面的序号改成 0）。
#   默认「显示的号 = 条目在板块里的位置」（1..n）；个别条目可以用这里改成别的号。
#   ⚠️ 只改**显示出来的号**与**能不能直接敲**，唯一键（one1）和它跑的命令一个字不动。
#   ⚠️ 覆盖号请挑**不在 1..n 里**的号（如 0）—— 否则会和同板块其它条目撞脸、分不清。
SEQ_OVERRIDE = {
    'one1':   '0',    # ★ 一键：显示 0（沿用 Windows 端的号），不是 1
    # ★ 空间回收：显示 X（用户 2026-09-28 指定）。它整组只有这一项，
    #   显示「1」会跟别的板块的 1 号看着像同一件事；X 一眼就知道是"那个清理的"。
    #   作用：黑窗菜单里敲 `X`（大小写都认）一步直达；界面卡片徽标也显示 X。
    'space1': 'X',
}

KEY2SEQ = {}       # 'local3' -> '3'     显示的号（默认=板块内位置，可被上面覆盖）
KEY2GKEY = {}      # 'local3' -> 'local' 所属板块
GKEY2TITLE = {}    # 'sync'   -> '同步（…）'
GKEY2KEYS = {}     # 'sync'   -> ['local1', 'local2', 'local3', 'cross1', 'cross2', 'macos2']
GORDER = []        # ['env', 'one', 'power', 'sync', 'space', 'macos']

for _gk, _title, _keys in GROUPS:
    GORDER.append(_gk)
    GKEY2TITLE[_gk] = _title
    GKEY2KEYS[_gk] = list(_keys)
    for _i, _k in enumerate(_keys, 1):
        KEY2SEQ[_k] = SEQ_OVERRIDE.get(_k, str(_i))
        KEY2GKEY[_k] = _gk

# 板块号（黑窗菜单第一级输入）→ 板块 key
GKEY_BY_NO = {str(_i): _gk for _i, _gk in enumerate(GORDER, 1)}

# 覆盖号 → 唯一键（**只**登记 SEQ_OVERRIDE 里的号）。
# 为什么只登记覆盖号：板块内的 1..n 每个板块都有（7 个板块里 5 个都有「1」），光看数字
# 分不清是哪个板块的第几项，那套只能「先选板块、再选条目」；而覆盖号是刻意挑出来的
# 全局唯一号（0 谁都没用），可以放心一步直达 —— 敲 0 就是「★ 一键」。
OVERRIDE2KEY = {v: k for k, v in SEQ_OVERRIDE.items()}


# 旧编号 → 新位置的提示（2026-09-27 重排后，给用惯了老编号的人一句话）。
# ⚠️ 只登记**不会与新编号撞车**的旧编号：旧 1~4（本机同步）和旧 5~7（跨机同步）
#    现在的 1~6 是板块号，输进去会直接进板块，压根走不到「没找到」那条分支 ——
#    登记了也是死代码，反而误导。
# ⚠️ 旧 `0`（★一键）**不登记**：它现在仍然是实打实的 0 号（见 SEQ_OVERRIDE），
#    输进去会直接执行，也走不到「没找到」那条分支。
# ⚠️ 旧 `x`（小写）**2026-09-28 起也不再登记**：空间回收现在就是实打实的 **X**
#    （见 SEQ_OVERRIDE），敲 x / X 都会直达（解析里有大小写宽容），
#    登记成「旧编号提示」反而多一层误导。
LEGACY_HINT = {
    '00': '环境 · 1 号（部署和修复）',
    '01': '已并入「环境 · 1 号 部署和修复」',
    # ★★ 2026-09-29 晚（板块合并）：原来「本机同步」「跨机同步」是两个板块，
    #   现在合成一个「同步」，所以旧跨机同步的 1~2 号顺延成**同步 · 4~5 号**
    #   （本机同步的 1~3 号位置没动）。
    # ★★★ 2026-10-05（对齐 Windows 端）：「同步」板块又从 6 项减到 **4 项**
    #   （local2 / local3 并入 local1），顺序变成：
    #     1 = 本机 · 任务折叠（local1）   2 = 跨机 · 中转同步（cross1）
    #     3 = 跨机 · 恢复误删会话（cross2）  4 = 合并会话（macos2）
    #   ⇒ 恢复误删会话由**第 5 号**变**第 3 号**，这行跟着改（Win 端那份忘了跟，
    #      本端按实际情况写对）。
    '8':  '同步 · 3 号（跨机 · 恢复误删会话）',
    '9':  '已并入「更多工具 · 1 号 更多工具…」（工作空间那一段）',
    # ★ 2026-09-29：运行监测板块又动过一次 —— 「状态与设置面板」取消（改到窗口右上角
    #   「状态与设置」），其余整体上移一位（旧 12→新 1、旧 16→新 7）。
    #   旧号的含义**变了**，这里只能指到"最接近的那个新条目"；组合类（例如
    #   "防休眠 + 屏幕常亮"）现在要**点两个开关**，没有单个编号能表示。
    '11': '已取消（改到窗口右上角「状态与设置」里看）',
    '12': '运行监测 · 1 号 + 2 号（防休眠 + 屏幕保持常亮）',
    '13': '运行监测 · 1 号（防休眠；屏幕允许自己黑）',
    '14': '运行监测 · 1 号 + 4 号（防休眠 + 合盖后保持唤醒）',
    '15': '运行监测 · 5 号（跑完自动关机）',
    '16': '运行监测 · 7 号（停止）',
    # ★ 20~25 原指 macOS 专属 1~8 号；2026-09-29 那一整片收进了「更多工具…」面板，
    #   所以现在一律指向那张卡。
    '20': '更多工具 · 1 号（更多工具… → 工作空间：预览）',
    '21': '更多工具 · 1 号（更多工具… → 工作空间：执行）',
    '22': '更多工具 · 1 号（更多工具… → 图标：换颜色）',
    '23': '已并入「环境 · 1 号 部署和修复」（修图标不显示）',
    '24': '已并入「环境 · 1 号 部署和修复」（安装跟随启动）',
    '25': '更多工具 · 1 号（更多工具… → 守护：卸载跟随启动）',
}


def show_num(key):
    """某条目该显示几号（板块内序号）。查不到就退回它自己的键，便于排查。"""
    return KEY2SEQ.get(key, key)



# 跨机任务线应急口：**没有对应的编号入口、不属于任何板块**，黑窗菜单里按字母直达。
#   ⚠️ 正因为"不属于任何板块"，名字里必须自带归属前缀 —— 否则用户看不出它到底是
#      「跨机同步」还是「本机同步」的事。
# 字母避开 s（旧跨机面板）/ d（访达打开本目录）/ q（退出）。
# ★★ 2026-09-29（用户要求）：
#    · 图形界面里**不再单列出这一区** —— 它们被收进「更多工具…」面板的
#      「跨机任务线应急」一段（见 wb_ui.py 的 build_tools()，那一段直接读本表，
#      不另抄一份命令）。黑窗菜单里 a / b / f 三个字母键照旧。
#    · 原 'r'（启动 / 重启「切号后同步」服务）**已删** —— 用户要求「一键整合或
#      部署和修复里能做到的就不再单列」，而「部署和修复」本来就会重装并拉起这个
#      守护（wb_install_hook.py --install 内部含 --start），它纯属重复。
#   ⚠️ 改本表要三处同步：wb_menu.py::EXTRA、wb_ui.py::UI_HINTS/TERM_NUMS、
#      _排障与验证脚本/_chk_mac_ui.py::PROMPT_EVIDENCE（'f' 那条）。
EXTRA = []   # TimeGuardian-Mac：跨机任务线属于任务管家，本程序不提供


def clear():
    os.system('clear')


def open_here():
    """在访达里打开本目录（找日志 / 备份用）。

    Mac 原版只调用了这个函数却没定义它，按 d 会直接 NameError（2026-09-15 补）。
    """
    try:
        subprocess.Popen(['open', HERE] if IS_WIN is False else ['explorer', HERE])
        print('\n  已在文件管理器中打开：%s' % HERE)
    except Exception as e:
        print('\n  ✗ 打不开，请手工进入：%s（%s）' % (HERE, e))


def wb_state():
    """一行状态：WorkBuddy 是否在跑、守护是否在跑。纯只读，失败不报错。"""
    try:
        running = G.is_wb_running()
    except Exception:
        running = None
    tag = {True: '运行中', False: '未运行', None: '判不出'}.get(running, '?')

    port = P.load_conf().get('syncPort') or P.SYNC_PORT
    guard = '未运行'
    try:
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open('http://127.0.0.1:%d/health' % port, timeout=1.5) as r:
            guard = '运行中（端口 %d）' % port if r.status == 200 else '异常'
    except Exception:
        pass
    return tag, guard


def sessync_state():
    """一行摘要：共享队列文件多大、什么时候导出的。纯只读。

    ⚠️ 2026-09-15 修正（同 Windows 端）：旧版读信封里的 `wbsSync` 元数据块
    （count / machine），但 wb_sessync.py 从来不写这个块 —— 它写的是 WorkDaddy
    官方 v3 信封（wbsExport/version/exportType/createdAt/kdf/...），所以旧版永远
    显示「0 条待同步（...来自 ?）」，是条假的。这里改成读**真实存在**的
    createdAt + 文件大小；若哪天信封里真有 wbsSync 块，再把条数带上。
    只读文件头 4KB，不碰几百 MB 正文。
    """
    try:
        import json as _json
        import time as _time
        conf = os.path.join(HERE, '_会话同步.json')
        with open(conf, 'r', encoding='utf-8') as f:
            cfg = _json.load(f)
        share = os.path.join(cfg.get('shareDir') or '', cfg.get('fileName') or '')
        if not os.path.exists(share):
            return '空（还没有待同步的会话）'
        size = os.path.getsize(share) / 1048576.0
        when = ''
        try:
            import re as _re
            import calendar as _cal
            with open(share, 'r', encoding='utf-8') as f:
                head = f.read(4096)
            m = _re.search(r'"createdAt"\s*:\s*"([^"]+)"', head)
            if m:
                try:
                    t = _time.strptime(m.group(1)[:19], '%Y-%m-%dT%H:%M:%S')
                    when = '，导出于 %s' % _time.strftime(
                        '%m-%d %H:%M', _time.localtime(_cal.timegm(t)))
                except Exception:
                    when = ''
            m2 = _re.search(r'"wbsSync"\s*:\s*\{[^}]*"count"\s*:\s*(\d+)', head)
            if m2:
                when += '，%s 条' % m2.group(1)
        except Exception:
            pass
        return '%.1f MB%s' % (size, when)
    except Exception:
        return '读不出来（跑 17 看详情）'

def task_trigger_state():
    """一行摘要：WorkDaddy 里那条「切号后自动同步」的触发任务装了没有。

    为什么要显示：切号自动同步要**两个都在**才会真的生效 ——
      ① 本机同步守护在跑（见上面「同步守护」一行）；
      ② WorkDaddy 里挂着一条 accountSwitched 触发器（本行的来源）。
    缺哪个一眼能看出来，不必去猜「为什么我切了号却没同步」。

    ★ 2026-09-27 移植 Windows 端界面时补的：Windows 那边一直有这一行，
      Mac 的 wb_menu 却没有，而图形界面（wb_ui.build_status）会调它 ——
      缺了它状态条上「切号触发」一栏永远是空的。这里按 Mac 的实际落点读：
      WorkDaddy 的自动化存在 `~/Library/Application Support/WorkDaddy/automations.json`。
      只读，读不到不报错（返回一句人话）。
    """
    try:
        import json as _json
        p = os.path.join(P.WD_HOME, 'automations.json')
        if not os.path.exists(p):
            return '未装（找不到 WorkDaddy 自动化文件）'
        with open(p, 'r', encoding='utf-8') as f:
            d = _json.load(f)
        tasks = d.get('tasks') if isinstance(d, dict) else None
        if tasks is None:
            tasks = d if isinstance(d, list) else []
        hits = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            trig = (t.get('trigger') or {})
            if trig.get('type') == 'accountSwitched':
                hits.append(t)
        if not hits:
            return '未装（WorkDaddy 里没有 accountSwitched 任务）'
        on = [t for t in hits if t.get('enabled') is not False]
        if on:
            return '已装（%s）' % ((on[0].get('name') or '切号同步')[:24])
        return '已装但被停用（%s）' % ((hits[0].get('name') or '切号同步')[:24])
    except Exception as e:
        return '读不出来（%s）' % e


def powerguard_state():
    """一行摘要：运行监测开了没、当前哪一档、任务在不在跑。纯只读，失败不报错。

    ★ 2026-09-27 移植 Windows 端界面时补的（Windows 那边一直有这一行）。
      实现只有一句 —— 摘要的写法归 wb_powerguard 一家，菜单不重复造。
    """
    try:
        import wb_powerguard as PG
        return PG.state_line()
    except Exception:
        return '（读不出来，见「运行监测」一组）'


def show_header():
    clear()
    print(BAR)
    print('  WorkBuddy 任务管家 · 本机同步（多账号全局单份）+ 跨机同步（记忆/技能/会话/任务）')
    print('                        + 运行监测（跑任务时不让电脑睡）+ 空间回收')
    print(BAR)
    tag, guard = wb_state()
    print('  运行监测  : %s' % powerguard_state())
    print('  工作目录  : %s' % HERE)
    print('-' * 78)


def show_menu():
    """列出所有板块与条目。

    ★ 2026-09-27 重排：显示的是**板块内序号**（每个板块都从 1 开始重新数）。
      所以这里必须按板块分组、板块内单独数号 —— 不能再「一个编号列到底」。
    ⚠️ 序号一律走 show_num() 取，**不要自己 enumerate 出数字**：唯一的例外
      「★ 一键 = 0」是在 SEQ_OVERRIDE 里定的，自己数会数成 1。
    """
    print('  要做什么？（先输板块号，再输该板块里的条目号；也可直接敲完整键 —— local3、★一键直接敲 0）')
    for gi, gk in enumerate(GORDER, 1):
        print()
        print('  %d. %s' % (gi, GKEY2TITLE[gk]))
        for k in GKEY2KEYS[gk]:
            desc, _cmd, risky = KEY2ITEM[k]
            print('     %-3s %s%s' % (show_num(k), desc, '   ⚠ 会改数据' if risky else ''))
    print()
    print('  -- 界面内直达（字母键，不属于任何板块）')
    for k, desc, _cmd, risky in EXTRA:
        print('   %-4s %s%s' % (k, desc, '   ⚠ 会改数据' if risky else ''))
    print()
    print('   %-4s %s' % ('s', '★ 更多工具…（= 更多工具与 macOS 专属·1，同步线总控就在这里）'))
    print('   %-4s %s' % ('u', '打开图形界面（点击操作，不用记编号）—— 等同双击 app'))
    print('   %-4s %s' % ('d', '在访达中打开本目录（找日志 / 备份）'))
    print('   %-4s %s' % ('q', '退出'))
    print()


def pick_in_group(gk):
    """进入某个板块，让用户挑条目。返回选中的键，返回 None 表示取消。

    也接受直接敲完整键（如 local3）—— 熟手可以少按一次回车。
    """
    keys = GKEY2KEYS.get(gk) or []
    print()
    print('  ── %s ──' % GKEY2TITLE.get(gk, gk))
    for k in keys:
        desc, _cmd, risky = KEY2ITEM[k]
        print('   %-3s %s%s' % (show_num(k), desc, '   ⚠ 会改数据' if risky else ''))
    try:
        ans = input('\n  条目号（直接回车返回） > ').strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not ans:
        return None
    if ans in KEY2ITEM and KEY2GKEY.get(ans) == gk:
        return ans
    # 先按**显示出来的号**匹配（含 SEQ_OVERRIDE，例如 ★一键是 0，不是它在板块里的位置 1）
    for k in keys:
        if show_num(k) == ans:
            return k
    if ans.isdigit() and 1 <= int(ans) <= len(keys):
        return keys[int(ans) - 1]
    print('\n  ✗ 这个板块里没有 %s 号。' % ans)
    pause()
    return None


def confirm(desc):
    try:
        ans = input('  「%s」会改动数据，确认执行？(y/N) ' % desc).strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ''
    return ans in ('y', 'yes')


def run(args):
    script = os.path.join(HERE, args[0])
    if not os.path.exists(script):
        print('\n  ✗ 找不到脚本：%s' % script)
        return
    cmd = [PY, script] + list(args[1:])
    print('\n  运行：%s\n' % ' '.join(os.path.basename(c) if c == script else c for c in cmd))
    print('-' * 78)
    try:
        subprocess.call(cmd, cwd=HERE)
    except KeyboardInterrupt:
        print('\n  （已中断）')
    except Exception as e:
        print('\n  ✗ 执行失败：%s' % e)
    print('-' * 78)


def pause():
    try:
        input('\n  按回车返回菜单 ...')
    except (EOFError, KeyboardInterrupt):
        pass


def main():
    # 首次运行自动补一次本机配置（缺了才写，不打扰）
    if not os.path.exists(P.CONF_FILE):
        try:
            subprocess.call([PY, os.path.join(HERE, 'wb_paths.py'), '--write'],
                            cwd=HERE, stdout=subprocess.DEVNULL)
        except Exception:
            pass

    while True:
        show_header()
        show_menu()
        try:
            raw_in = input('  板块号 > ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        # ★ 2026-09-27 重排后：第一级输「板块号」（1~7），第二级输「条目号」。
        #   为省事也支持一步到位 —— 直接敲完整键（如 local3）或字母键（如 f、P）。
        #   EXTRA 里有**大写**键（P/S/R/H/D/L/W），所以查表用原样的 raw_in，
        #   下面那些 `in ('q', ...)` 判断才用小写的 choice。
        choice = raw_in.lower()

        if not raw_in:
            continue
        if choice in ('q', 'quit', 'exit'):
            print('\n  再见。')
            return 0
        if choice in ('d', 'dir', 'open'):
            open_here()
            continue
        # 跨机同步面板：自己是一个带循环的子界面，跑完直接回主菜单
        if choice in ('s', 'sync'):
            run(['wb_sync_ctl.py', '--panel'])
            continue
        # 打开图形界面（与 Windows 端菜单的 u 键对齐）。
        #   ★ 走 wb_ui.py --launch 而不是直接 open app：launch 会先确保后台服务
        #     在跑、再开窗口，app 那边只负责显示。这样两条入口（菜单 u / 双击 app）
        #     走的是同一条路，不会出现「一个能开、一个连不上」。
        if choice in ('u', 'ui', '界面'):
            run(['wb_ui.py', '--launch'])
            continue

        # 支持「power6 30m」这种带参数的写法：首段是编号/键，其余原样追加
        #   ⚠️ 这个例子原来写的是 `macos5 45`（换图标颜色 + 色相）—— 2026-09-29
        #   那两项收进「更多工具…」面板后 `macos5` 已经不存在了，照抄会敲出
        #   「没有这个板块号 / 快捷键」。现值取自 `power6`（到点自动结束，
        #   追加一个时长）、是**当前真能用**的例子 —— 换例子时请顺手试一遍。
        parts = raw_in.split()
        head = parts[0]
        rest = parts[1:]

        picked = None
        if head in KEY2ITEM:                      # 完整键，原样
            picked = head
        elif head.lower() in KEY2ITEM:            # 完整键，大小写宽容
            picked = head.lower()
        elif head in OVERRIDE2KEY:                # 覆盖号一步直达（0 = ★一键、X = 空间回收）
            picked = OVERRIDE2KEY[head]
        elif head.upper() in OVERRIDE2KEY:        # ★ 大小写宽容：敲小写 x 也直达 X
            picked = OVERRIDE2KEY[head.upper()]
        elif head in GKEY_BY_NO:                  # 第一级：板块号 → 进去挑条目
            picked = pick_in_group(GKEY_BY_NO[head])
            if picked is None:
                continue
            rest = []                             # 走板块挑的时候不带参数

        if picked is not None:
            desc0, cmd0, risky0 = KEY2ITEM[picked]
            desc, cmd, risky = desc0, list(cmd0) + rest, risky0
        else:
            # 第二类：字母键（EXTRA）。原样 → 小写两步找。
            hit = None
            for k, d, c, r in EXTRA:
                if k == raw_in or k.lower() == choice:
                    hit = (d, c, r)
                    break
            if hit is None:
                print('\n  ✗ 没有这个板块号 / 快捷键：%s' % raw_in)
                tip = LEGACY_HINT.get(raw_in) or LEGACY_HINT.get(choice)
                if tip:
                    print('     （这是**重排前的旧编号**，已作废）它现在是：%s' % tip)
                    print('     先输板块号再输条目号，或直接敲完整键，例如 local3。')
                pause()
                continue
            desc, cmd, risky = hit[0], list(hit[1]) + rest, hit[2]

        if risky and not confirm(desc):
            print('\n  已取消。')
            pause()
            continue

        run(cmd)
        pause()


if __name__ == '__main__':
    sys.exit(main())
