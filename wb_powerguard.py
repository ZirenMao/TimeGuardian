#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 运行监测（电源守护）· macOS 版 —— wb_powerguard.py
================================================================

在 WorkBuddy **正在跑任务**期间，让这台 Mac「不因空闲而入睡」
（可选：屏幕常亮 / 笔记本合盖也不睡）；任务一结束就撒手，
让系统回到它原本的电源行为 —— 也就是「没人动它，到点自己就睡」。

为什么要有它
----------------------------------------------------------------
WorkBuddy 跑长任务时（批量 OCR、跑脚本、长回答），人常常离开电脑。
默认电源设置在几分钟无操作后就睡眠，一睡，任务就断了。
手动去改电源设置又麻烦，而且多半会忘了改回来。

★ 与 Windows 版的对应关系（这才是本文件存在的意义）
----------------------------------------------------------------
Windows 版用的是 Win32 的 `SetThreadExecutionState`（内核级、**按进程**生效的
「先别睡」申请）和 `powercfg`（改合盖动作）。这两样 macOS 都没有，
所以**做法不同、行为一致**：

    能力               Windows 版                        macOS 版（本文件）
    ───────────────    ────────────────────────────      ──────────────────────────
    不睡眠    SetThreadExecutionState           IOPM 电源断言
    屏幕常亮           ES_DISPLAY_REQUIRED               ＋PreventUserIdleDisplaySleep 断言
    合盖也不睡         powercfg LIDACTION=0              pmset -a disablesleep 1（需管理员）
    跑完自动关机       shutdown /s /t N                  osascript 请 System Events 关机
    判「在不在干活」   会话文件 / DB status / 子进程       同左（纯 Python + 文件/进程探针）

★★ 2026-10-01（第六十二轮）：防睡眠从 `caffeinate` 子进程换成 **IOKit 电源断言**
  （`IOPMAssertionCreateWithName`），学的是 PommeToys —— 理由见下面「电源断言」那一节。
  **caffeinate 保留为回退**：断言这条路建不起来时自动走老路，行为一个字不变。

★ 最要紧的共同点：**都不修改系统设置本身**。
  Win 的 `SetThreadExecutionState` 与 Mac 的电源断言是同一件事：内核级、**绑在
  「进程」上**的「先别睡」申请 —— 进程一退出（正常退出、被强杀、崩溃、注销、
  重启都算）申请自动失效，**不需要恢复任何设置**。需求里那句
  「任务结束之后电脑和默认状态一样」在 Mac 上同样成立。
  （`caffeinate` 子进程也满足这条，但它是**独立进程** —— 父进程被强杀时它不会
   跟着死，会变成没人认领的孤儿；这正是换断言的原因之一。）
  唯一的例外是**合盖**的 `pmset -a disablesleep`（它会持久化），所以：
    · 启用前把原值记进 `_运行监测.json` 的 `lidOriginal`；
    · 停止监测 / 守护退出时写回原值。

一个主开关 + 4 个子开关 + 2 个收尾开关（2026-09-29 起，按 PommeToys 的功能树重做）
----------------------------------------------------------------
    防休眠（主开关）         不睡眠
      ├ 屏幕保持常亮         连显示器一起顶住（PreventUserIdleDisplaySleep）
      ├ 电量不足时自动停     在用电池且电量低于阈值时**暂停**（开关保留，插电自动恢复）
      └ 合盖后保持唤醒       合上盖子也不睡（仅笔记本；需 root，已装免密）
          └ 合盖后内建屏保持点亮   照 PommeToys：合盖后**不再关闭**内建屏；
                               **亮度自动调至最低**，开盖后恢复。默认关 = 合盖后熄屏
    跑完自动关机             任务全部结束后把电脑关掉        ⚡ 与「到点自动结束」互斥
    到点自动结束             值守满一个时长后自动收尾         ⚡ 与「跑完自动关机」互斥

★ 2026-10-12 照 PommeToys 对齐（详见本文件「PommeToys 口径」一节）：合盖期间**只下一次
  「补偿熄屏」**、有别的程序按着屏就不插手、合盖那一刻核对「合盖不睡」是否被别处改动、
  温度过高就收手；开盖主动把屏叫回来。

★★ 用户 2026-09-29 拍的板：「档位一 / 档位二」这种**捆绑销售**没必要存在 ——
   它们本来就是「主开关 + 屏幕常亮」的两种组合，拆成独立开关后是冗余概念。
   所以现在：
     · **真值源** = 上面那 5 个布尔（`awake` / `keepDisplayOn` / `lidClose` /
       `lidDisplayOn` / `lowBattStop`），存 `_运行监测.json`；
     · `level`（0/1/2/3）退化成**派生视图**，任何时候由 `feats()` 现算 ——
       只为兼容老菜单、老文档、老状态文件，**不要再手写它**。
     · 老命令 `--level 0|1|2|3` 保留且行为不变（内部就是映射成一组开关）。

主开关与子开关的关系（照 PommeToys）：**主开关一关，子项全部不生效，但状态被记住**；
点了某个子开关而主开关是关的 → 自动把主开关打开（不然「点了没反应」，最气人）。
两个「收尾开关」与上面这组**完全独立**（不开防休眠也照常跑），且**彼此互斥**：
开一个会自动关掉另一个 —— 它们都是「这一轮跑完之后怎么收尾」的答案，同时开着
用户自己都说不清电脑最后会怎么走。两者都是**一次性**的：本次开机里设的只对本次
开机有效，机器一重启就恢复（`autoShutdownBootTs` / `autoStopBootTs` 记的就是
「设定那一刻所处的那次开机」）。

★ 「合盖也不睡」为什么**不用**「屏幕常亮 + 亮度调最低」这套（用户 2026-09-28 问过）：
  `caffeinate -d` 管的是「显示器空闲睡眠」那个**软件计时器**，对合盖无效；
  合盖那半边只能靠 `pmset disablesleep`。「屏幕常亮 + 亮度压低」真正适用的场景是
  **不盖盖子**跑 —— 那正是档位一。

★★ 但 `disablesleep` **只短路了「合盖 → 系统睡眠」这一步**（2026-09-28 用户实测）：
  正常合盖时，由同一套合盖处理顺手做的「内建屏熄灭」也一起没了 → 合盖后**屏留在亮着**。
  这是 `disablesleep` 的固有副作用（改成 `caffeinate -d` 也没用，那是反方向的
  「别熄屏」）。而 macOS 在「没有外接屏」时不提供任何「合盖不睡 + 熄屏」的开关，
  所以本工具自己补一刀 —— 见 `lid_screen_guard()`：
    · 合盖期间下发 `pmset displaysleepnow`（**只熄屏、不睡觉**，后台照跑），
      并用 `CGDisplayIsAsleep` 复核；被唤醒就按退让间隔再压一次；
    · 开盖即停；检测到外接屏 / 假负载头就**让位**（那已经是真·合盖模式，
      系统自己会把内建屏断电，我们插手只会把人家正在看的屏幕也黑掉）。
  想要**物理级**的内建屏断电（更省电、更凉），走「假负载头 + 电源」的硬件方案 ——
  见同工作空间的《合盖模式指南_WorkBuddy持续运行.md》。

怎么判「在不在干活」（三路信号，与 Win 版同源）
----------------------------------------------------------------
    ① WorkBuddy 自己的会话状态   `~/.workbuddy/workbuddy.db` 里 `status='working'`
       ← **最权威、最实时**（应用在每轮对话的边界改写它）
    ② 会话文件在写               `~/.workbuddy/projects/**/*.jsonl` 有新写入
    ③ 起了工具子进程             WorkBuddy 进程树里出现非 WorkBuddy 的后代

    （Win 版还有一路「进程 CPU 在涨」，默认**关闭** —— 见 DEFAULTS 里的说明：
      WorkBuddy 是 Electron，十几个进程在「人没动、任务也没跑」时也有常驻 CPU
      开销，按阈值判会一直为「忙」→ 电脑永远不睡，正好与需求相反。）

判「任务结束」比判「忙」更谨慎：**全部信号静默**满 `idleConfirmSec` 才算结束。
宁可多守一会儿，也不能在长任务跑到一半时把申请撤了、让电脑睡过去。

用法（与 Windows 版逐字一致，方便对照与脚本复用）
----------------------------------------------------------------
    python wb_powerguard.py --status              一行状态（菜单首屏用）
    python wb_powerguard.py --panel               交互面板（推荐入口）
    python wb_powerguard.py --level 1|2|3         切到某一档，并确保守护在跑
    python wb_powerguard.py --level 0             只挂监测 / 自动关机，不做抑制
    python wb_powerguard.py --level-toggle 1|2|3  按一下开、再按一下关（界面档位按钮用）
    python wb_powerguard.py --shutdown            交互式开关「任务结束自动关机」
    python wb_powerguard.py --cancel-shutdown     取消正在倒计时的自动关机
    python wb_powerguard.py --autostart on|off    是否随登录自启（写 LaunchAgent）
    python wb_powerguard.py --stop                停止监测，立即恢复默认
    python wb_powerguard.py --heal                只收「上次开机留下的」两个附加项（不动开关）
    python wb_powerguard.py --reconcile           收附加项 + 按当前开关对齐守护（不动开关）
    python wb_powerguard.py --reset               彻底还原：连 5 个「防休眠」开关一起归零
    python wb_powerguard.py --daemon              守护本体（内部用，勿手工跑）
    python wb_powerguard.py --selftest            只读自检（给排障用）
    python wb_powerguard.py --selftest-lid        实测「合盖熄屏」链路（会真的熄一次屏）

设计约束
----------------------------------------------------------------
  · 纯标准库，零第三方依赖；
  · **绝不**在模块级包装 / 使用 `sys.stdout`：守护可能被没有控制台的解释器拉起
    （那时 sys.stdout 是 None），取 `.buffer` 会在 import 期就炸、异常还被吞掉 ——
    Windows 版 2026-09-25 在这上面栽过一次（守护因此「从未起来过」但面板显示正常）。
    本文件所有诊断都写 `_运行监测.log`；
  · 本文件可独立运行，只借用同目录 wb_paths 的数据根探测。
"""
import argparse
import ctypes
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))

IS_MAC = (sys.platform == 'darwin')
IS_WIN = (os.name == 'nt')

CONF_FILE = os.path.join(HERE, '_运行监测.json')
STATE_FILE = os.path.join(HERE, '_运行监测状态.json')
LOG_FILE = os.path.join(HERE, '_运行监测.log')
STOP_FLAG = os.path.join(HERE, '_运行监测.stop')
# ★ 取消标记（2026-09-28 加）：一个「请你立刻放弃这次关机倒计时」的信号文件。
#   为什么不能只改配置 / 只清状态文件（都是踩过的坑）：
#     `shutdown_pending` 是**守护进程的内存变量**，主循环只在「进入倒计时那一轮」
#     读一次 conf；此后哪怕用户把 conf['autoShutdown'] 关掉、或把状态文件里的
#     shutdownPending 清掉，守护看都不看 —— 到点照样关机。
#     症状就是用户说的：「关机倒计时的时候，再点击跑完自动关机没用」。
#   两个兜底一起上：① 主循环每轮复查 conf['autoShutdown']；② 这个显式标记文件
#   （给「只想取消这一次、开关留着」的场合，也就是 --cancel-shutdown）。
CANCEL_FLAG = os.path.join(HERE, '_运行监测.cancel')
INST_FILE = os.path.join(HERE, '_运行监测.instance')
# 演练（--selftest-shutdown）的旁车备份：抗强杀，见 heal_selftest_leftover()
CONF_SELFTEST_BAK = CONF_FILE + '.selftestbak'
STATE_SELFTEST_BAK = STATE_FILE + '.selftestbak'

# ★ 最近一次「写配置/状态失败」的原因（2026-09-28 加）。
#   以前这类失败只写进日志、界面完全不知道 → 用户看到的永远是旧值，
#   于是「按了没反应」。现在它会被 --status 带出去，界面上红字直说。
_LAST_WRITE_ERR = ['', 0.0]
# ★ 2026-10-11（A6）：第三态「未知」的起点时刻（0 = 现在读得到）。
#   放在模块级是为了跨轮次记住「从什么时候开始读不到」。
_UNKNOWN_SINCE = [0.0]

BAR = '=' * 74
LOG_MAX = 1024 * 1024          # 日志超过 1MB 就滚动一次
POLL_SEC = 15                  # 守护轮询周期（秒）—— 现在是「忙时」的兜底，见下面两个常量
TOOL_VERSION = 2

# ★★★ 2026-10-01（第四十八轮 · 用户第 4 条「这个应该是看任务在没在跑的事件驱动」）：
#   **闲时不再每 15 秒醒一次。**
#
#   改之前的节奏是死的：`sleep(15)` 一天醒 5760 次，只为问一句「有没有任务在跑」——
#   而电脑上绝大多数时间压根没任务。
#
#   WorkBuddy 在跑任务时，**有两处东西必然在变**（本轮实测确认）：
#     ① `~/.workbuddy/workbuddy.db-wal`
#        每轮对话边界写 `sessions.status`（working / completed）都落在 WAL 上。
#        ★ 这是最权威的一路：**「开始跑」和「跑完了」都是它写出来的** ——
#        也就是说「任务结束」**本身就是一个事件**，不需要靠超时去猜。
#     ② `~/.workbuddy/projects/*/*.jsonl`
#        会话正文，干活时每 10~30 秒追加一次（补充信号，C 盘/长任务时更密）。
#
#   于是主循环改成：**有动静才醒**（见 start_task_event_watch）。
#   下面两个数**只是兜底**（保险丝：万一事件源失效，别让守护变成瞎子），
#   不是「轮询周期」：
TASK_IDLE_SEC = 300            # 闲时兜底（秒）—— 一天最多醒 288 次（原 5760 次）
TASK_ACTIVE_SEC = 15           # 忙时兜底（秒）—— 与改前的 POLL_SEC 完全一致，手感不变

# ---- 档位三的「合盖熄屏」（见 lid_screen_guard；2026-09-28 加）----
#   ★ 为什么需要这几个数：主循环一轮 15 秒，合盖后要等满一轮才熄屏，用户会觉得
#     「怎么没反应」。所以守候时把那一轮 sleep 切成 LID_SCREEN_TICK 的小片，
#     合盖后最多 3 秒就熄屏（切片只改「怎么睡」，不改主逻辑的判定节奏）。
LID_SCREEN_TICK = 2            # 切片长度（秒）—— 2 秒：合盖后最多 2 秒就开始熄屏
LID_SCREEN_GAP = 8             # 两次「强制熄屏」之间至少隔这么久（秒）
LID_SCREEN_BACKOFF = 45        # 被反复唤醒后的退让间隔（秒）—— 别整夜跟某个程序对拉
LID_SCREEN_RETRY_AFTER = 3     # 前几次用 GAP，超过这个次数改用 BACKOFF
# ★★ 合盖那一两秒必须「连压」，不能只压一次（2026-09-28 实测抓到的真瑕疵）：
#   合盖瞬间系统先自己关屏，紧接着 `Entering DarkWake state due to 'Clamshell Sleep'`，
#   然后被一条 HID 活动（`DarkWake to FullWake ... due to HID Activity`）拽回全醒 →
#   **屏又亮**。只压一次的话，压完正好被这一下顶开，用户看到的就是「合盖了屏还亮」。
#   所以刚合盖的那几秒要连压若干次，把这段抖动窗口整个压平；稳定后再回到低频维持。
LID_SCREEN_BURST = 8           # 刚合盖时连压几次（★ 2026-10-12 起只在 LID_COMPENSATE_PRESSES 范围内用）
LID_SCREEN_BURST_GAP = 0.45    # 连压之间的间隔（秒）
# ★★ 2026-10-12（照 PommeToys 收窄）：上面「连压 8 次 + 每 8/45 秒再压」是**跟别人对拉**的写法，
#   也正是「合盖后再开盖叫不回屏」的土壤（屏被按进一个系统不知道的熄灭状态）。
#   PommeToys 那边只下一次「补偿熄屏」(compensating sleep)，并且**有别的程序按着屏就不插手**
#   （它自己的日志原话 "display left lit at a holder's request"）。我们现在按它收窄：
LID_COMPENSATE_PRESSES = 2     # 刚合盖时最多补按几次（只为止住「熄→亮→熄」那一下抖动）
LID_SCREEN_RECHECK = 300       # 补偿之后的重查间隔（秒）：只在「屏又被点亮、且没人按着」时补一次
LID_DIM_BRIGHTNESS = 0.02      # 「合盖后内建显示器保持点亮」用的最低亮度（照它：压到最低，不是关屏）

# ★ 空跑模式（与 Win 版同一个环境变量名）：会改变系统状态的命令一律只记日志、不真执行。
#   目前只有 `--selftest-shutdown` 演练会打开它 —— 演练要跑的是**真的守护主循环、
#   真的判定逻辑**，唯独不能真的把用户的电脑关掉。
_DRY = (os.environ.get('WB_POWERGUARD_DRY') == '1')

# macOS 自带的命令（都在 /usr/bin，不用第三方）
CAFFEINATE = '/usr/bin/caffeinate'
PMSET = '/usr/bin/pmset'
OSASCRIPT = '/usr/bin/osascript'
IOREG = '/usr/sbin/ioreg'        # 读「合盖了吗」（AppleClamshellState）
PS = '/bin/ps'

# 自启（LaunchAgent）—— 只在 --autostart 时写，且**不自动 bootstrap**
# （本工具所在会话写不了 launchd 域，必须用户自己在终端里执行一条命令）
LA_LABEL = 'com.wb.workbuddy-powerguard'
LA_PLIST = os.path.join(os.path.expanduser('~'), 'Library', 'LaunchAgents',
                        LA_LABEL + '.plist')


# ------------------------------------------------------------------ 配置

# ★ 字段与 Windows 版**逐字一致** —— 这样两端的「状态/配置」能互相读懂，
#   将来要合并成一份跨平台实现时，只要换掉下面的平台实现层即可。
DEFAULTS = {
    'schemaVersion': 1,
    'enabled': False,           # 守护是否应当在跑（False = 整套功能关）
    # ★ 2026-09-29（用户要求，按 PommeToys 的功能树重做）：档位 1/2/3 退役，
    #   改成「1 个主开关 + 4 个子开关」。这几个才是**真值源**；`level` 只是派生视图。
    #   PommeToys 的键名照抄它的语义，便于以后两边对照：
    #     awake          防休眠            ← 原「档位二」的核心（不睡）
    #     keepDisplayOn  屏幕保持常亮      ← 原「档位一」的独有项（caffeinate -d）
    #     lidClose       合盖后保持唤醒    ← 原「档位三」（pmset disablesleep，免密）
    #     lidDisplayOn   合盖后内建屏保持点亮 ← PommeToys 的子项（默认关 = 合盖熄屏）
    #     lowBattStop    电量不足时自动停  ← PommeToys 的 lowBatteryStop
    'awake': False,             # 主开关：防休眠（不睡眠）
    'keepDisplayOn': False,     # 子项：屏幕保持常亮
    'lidClose': False,          # 子项：合盖后保持唤醒（笔记本；需 root，已装免密）
    'lidDisplayOn': False,      # 子项之下的子项：合盖后内建显示器保持点亮
    'lowBattStop': False,       # 子项：电量不足时自动停
    'lowBattPct': 10,           # 「电量不足」的阈值（%）
    'lowBattCheckSec': 60,      # 电量多久看一次（秒）—— 只在主开关 + 本开关都开着时才读
    'level': 0,                 # 【派生】0 不抑制 / 1 防休眠+常亮 / 2 防休眠 / 3 +合盖不睡
    'autoShutdown': False,      # 附加：任务全部结束后自动关机
    # ★ 2026-10-11：**唯一的倒计时**，固定 3 分钟（不再与「5 分钟确认窗口」叠加）。
    #   ★ 不要做界面档位：用户明确「直接按默认 3 分钟，不用调 UI」。
    'shutdownDelaySec': 180,
    # ★ 2026-10-09（对齐 Win 端）：发令之后的「宽限 + 强制」
    #   shutdownGraceSec      —— 已发出关机命令后，先等这么久看它到底关没关
    #                            （真关机的话本进程会被系统结束，走不到下面 → 活着＝没关成）。
    #   shutdownForceAfterSec —— 宽限期过了机器还没关（多半有程序顶着不让关），
    #                            再等这么久就改用**强制**关机；设 0 = 不强制，如实上报失败。
    # ★★ 2026-10-10（夜间事故后）：默认从 0 改成 **120** —— 那晚就是因为「不强制」，
    #    关机请求卡住后只能等 10 分钟冷却再试，循环了一整夜。
    'shutdownGraceSec': 60,
    'shutdownForceAfterSec': 120,
    # ★ 2026-10-11：语义从「静默多久算结束」改成「**结束去抖**」——
    #   忙→闲之后先静默这么久，躲开「一轮刚完、下一轮马上来」的缝。
    #   60 秒的来由：本机实测 WorkBuddy 任务内间隔 99% ≤60s、Hermes 97% ≤60s。
    'idleConfirmSec': 60,
    # ★ 2026-10-11（A6）：任务信号**持续读不到**多久 → 走兜底关机。
    #   为什么要有它：读不到时一律判忙 → 会「永远不关机」且界面上看不出来。
    #   设 0 = 关掉这个兜底。
    'unknownShutdownSec': 1200, # 信号持续读不到 → 兜底关机（默认 20 分钟）
    'activityWindowSec': 60,    # 会话文件多新算「有动静」
    'useCpu': False,            # 见 docstring：Electron 常驻开销会让它一直判「忙」
    'cpuBusyRatio': 0.60,
    'useChildProcess': True,    # 把「起了工具子进程」当忙信号
    'useWbDbSignal': True,      # 读 workbuddy.db 的 sessions.status（权威、实时）
    # ★ 双监测（对齐 Win 端）：任务结束自动关机要**WorkBuddy 与 Hermes 都跑完**才动。
    'useHermesSignal': True,       # 同时监测 Hermes 的活动
    'hermesActiveWindowSec': 120,  # Hermes「正在跑」窗口（秒）
    'hermesHome': '',              # 留空 = 自动探测 ~/.hermes 或 $HERMES_HOME
    'wbWorkingRecentSec': 3600,  # 只认「近 1 小时内还活跃」的 working 会话（防僵尸）
    'wbRunningWindowSec': 90,   # 仅诊断用：更短的窗口，回答「现在这一刻还在跑吗」
    'childFreshSec': 180,       # 子进程创建多久之内算「刚起来的」＝正在干活
    'childCpuEps': 0.05,        # 累计 CPU 涨过这么多秒，算「在烧 CPU」
    'childStaleGraceSec': 600,  # 不新建也不烧 CPU 的，要安静够这么久才不算忙
    'childRateMin': 0.005,      # 安静者平均速率 ≥ 这么多核，才算「还在慢慢干活」
    'childRateWinSec': 60,
    'autostart': False,
    'lidOriginal': None,        # 档位三用：改动前的 SleepDisabled 原值（0/1）
    'lidChanged': False,        # 档位三用：我们是否改过它
    'autoShutdownBootTs': 0,    # 见 docstring：自动关机只对「那次开机」有效
    # ---- 附加二：「到点自动结束」（★与「跑完自动关机」互斥，见 cmd_autostop_set）----
    #   与 autoShutdown 一样是**一次性**的：只对「设定那一刻所处的那次开机」有效，
    #   重启即失效 —— 防「设了 2 小时忘了，下次开机莫名其妙被结束」。
    'autoStopAt': 0,            # 到点时刻（unix 秒；0 = 没设）
    'autoStopSetAt': 0,         # 设定那一刻的时间（界面算「还剩多少」用）
    'autoStopDurationSec': 0,   # 用户设的时长（回显「共 X」用）
    'autoStopBootTs': 0,        # 同 autoShutdownBootTs：只对那次开机的设定有效
}

LEVEL_DESC = {
    0: '不抑制（仅监测 / 只挂自动关机）',
    1: '档位一 · 不睡眠 + 屏幕常亮',
    2: '档位二 · 不睡眠 + 允许息屏',
    3: '档位三 · 不睡眠 + 合盖也不睡 + 合盖自动熄屏（笔记本）',
}

# 允许息屏的档位（2、3）；档位 1 额外要求屏幕常亮
LEVEL_KEEP_DISPLAY = (1,)

# 5 个「先别睡」开关（真值源）。顺序 = 界面上从上到下的顺序。
_FEAT_KEYS = ('awake', 'keepDisplayOn', 'lidClose', 'lidDisplayOn', 'lowBattStop')

_FEAT_CN = {
    'awake': '防休眠',
    'keepDisplayOn': '屏幕保持常亮',
    'lidClose': '合盖后保持唤醒',
    'lidDisplayOn': '合盖后内建显示器保持点亮',
    'lowBattStop': '电量不足时自动停',
}


def feats(conf):
    """把配置归一成「一组开关」。向后兼容老配置（只有 level 时按老档位推）。

    返回 dict：5 个布尔 + 派生出来的 `level`（0~3）。
    ★ 真值永远是那 5 个布尔；`level` 只在「写状态 / 老文档 / 老菜单提示」里出现。
    """
    c = conf or {}
    if any(k in c for k in _FEAT_KEYS):
        f = {k: bool(c.get(k)) for k in _FEAT_KEYS}
    else:
        lvl = int(c.get('level') or 0)
        f = {'awake': lvl > 0,
             'keepDisplayOn': (lvl == 1),
             'lidClose': (lvl == 3),
             'lidDisplayOn': False,
             'lowBattStop': False}
    f['level'] = 0 if not f['awake'] else (
        3 if f['lidClose'] else (1 if f['keepDisplayOn'] else 2))
    return f


def feat_desc(f):
    """开关组合 → 一句人话（状态行 / 日志 / 界面都调它，别各处自己拼）。"""
    if not f or not f.get('awake'):
        return '不抑制（仅监测 / 只挂自动关机）'
    parts = ['防休眠']
    if f.get('keepDisplayOn'):
        parts.append('屏幕常亮')
    if f.get('lidClose'):
        parts.append('合盖也不睡')
        parts.append('合盖保持点亮' if f.get('lidDisplayOn') else '合盖熄屏')
    if f.get('lowBattStop'):
        parts.append('电量低于 %d%% 自动停' % int(f.get('lowBattPct') or 10))
    return ' + '.join(parts)


def write_feats(conf, f):
    """把一组开关写回 conf（含派生 level）。就地改，返回 conf。"""
    for k in _FEAT_KEYS:
        conf[k] = bool(f.get(k))
    conf['level'] = 0 if not conf['awake'] else (
        3 if conf['lidClose'] else (1 if conf['keepDisplayOn'] else 2))
    return conf


def clear_feats(conf):
    """把所有「先别睡」开关归零（只有「停止监测」和「彻底还原」该调它）。

    ★★★ 2026-10-01（第廿九轮）：**不要**再在「开机自愈 / 关闭服务 / 部署和修复」
      里调它。那三条都是**日常动作**，而 5 个开关是**用户的长期姿势** ——
      顺手归零＝用户点一次「关闭服务」防休眠就没了（用户投诉的原现象）。
      判据很简单：**只有用户明确说「恢复原样 / 彻底重来」时才允许走这里。**

    ★ 为什么不能像以前那样只写 `conf['level'] = 0`：现在 `feats()` 优先认那 5 个布尔，
      只把 level 归零**关不掉任何东西** —— 这是改造里最容易漏、且症状最隐蔽的一处
      （界面显示「已停止」，实际照旧不睡）。所以统一走这里。

    ★ 2026-09-30：顺手把「到点自动结束」一并作废（见 drop_autostop_no_awake）。
      语义是连着的 —— 这函数就是「不值守了」，那还留着一个到点把一切停掉的时间戳，
      只会让用户下次打开时莫名其妙。调用方记得 save_conf。
    """
    write_feats(conf, {k: False for k in _FEAT_KEYS})
    drop_autostop_no_awake(conf)
    return conf


# ------------------------------------------------------------------ 日志

_LOG_FH = None


def log(msg, console=True):
    """写日志文件（滚动 1MB），必要时也打到控制台。

    ★ 绝不依赖 sys.stdout：守护可能跑在无控制台的解释器下。
    """
    global _LOG_FH
    line = '%s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        if _LOG_FH is None:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX:
                try:
                    if os.path.exists(LOG_FILE + '.1'):
                        os.remove(LOG_FILE + '.1')
                    os.replace(LOG_FILE, LOG_FILE + '.1')
                except Exception:
                    pass
            _LOG_FH = open(LOG_FILE, 'a', encoding='utf-8')
        _LOG_FH.write(line)
        _LOG_FH.flush()
    except Exception:
        pass
    if console:
        try:
            if sys.stdout is not None:
                sys.stdout.write(line)
                sys.stdout.flush()
        except Exception:
            pass


def _out(s=''):
    """往控制台打一行（没有控制台就静默丢弃）。"""
    try:
        if sys.stdout is not None:
            sys.stdout.write(str(s) + '\n')
            sys.stdout.flush()
    except Exception:
        pass


def _run(cmd, timeout=25):
    """跑一条命令，返回 (rc, stdout+stderr 文本)。绝不抛异常。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except Exception as e:
        return -1, '%s: %s' % (type(e).__name__, e)


# ------------------------------------------------------------------ 数据根

def wb_home_of():
    """定位 ~/.workbuddy（借用 wb_paths；它探测失败就退回默认路径）。"""
    try:
        sys.path.insert(0, HERE)
        import wb_paths as P
        p = getattr(P, 'WB_HOME', None)
        if p and os.path.isdir(p):
            return p
    except Exception:
        pass
    return os.path.join(os.path.expanduser('~'), '.workbuddy')


# ------------------------------------------------------------------ 配置读写

def load_conf_raw():
    try:
        with open(CONF_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_conf():
    raw = load_conf_raw()
    d = dict(DEFAULTS)
    d.update(raw)
    try:
        d['level'] = int(d.get('level') or 0)
    except Exception:
        d['level'] = 0
    # ★★ 2026-09-29 一次性迁移：老配置里只有 `level`、没有那 5 个开关。
    #   ⚠️ `DEFAULTS` 里已经带上开关（默认 False），所以**绝不能靠"键在不在"判断新旧** ——
    #      那会把一份 `level=3` 的老配置读成「全部关掉」（症状：升级后档位静默失效）。
    #      判据取**原始文件**（load_conf_raw）里有没有 `awake`；没有就按老档位补出来，
    #      并落盘（只落一次）。落盘失败也没关系：每次读都会重新补，行为不受影响。
    if 'awake' not in raw:
        #   ⚠️ 只补在**内存里**，不落盘：`--status` 号称"纯只读"，不该有副作用；
        #      而且这里落盘失败还会点亮界面那条「配置写不进去」的红字，纯属误报。
        #      真需要落盘时（用户点任何开关）自然会由 write_feats + save_conf 写全。
        d = write_feats(d, feats({'level': d['level']}))
    return d


def _dump_json(path, d):
    """把 dict 写成 JSON 文件 —— 先原子替换，失败再退回原地覆盖。

    ★★ 为什么必须有这个回退（2026-09-28 实测定位，用户报「点了按钮没反应」）：
      原实现是 `open(tmp,'w')` + `os.replace(tmp, CONF_FILE)`。在**受限运行环境**里
      （本机实测到两种：WorkBuddy 沙盒的 `Brokered host rename overwrite refused by
      file policy`，以及系统层面的 `PermissionError(1, 'Operation not permitted')`），
      `os.replace` 会被拒 —— 而写 tmp 那一步是成功的。
      结果：配置文件**永远是旧值**（mtime 停住不动），代码却以为写成功了，
      界面轮询读回旧值 → 用户看到「按钮按了没反应」「开关关不掉」。
      日志里留下 41 条 `写配置失败`，而界面**一条都不知道**。

      原地覆盖（open(path,'w')）不涉及 rename，绕开这一整类策略。
      它没有原子性，所以**只在原子替换失败后才退**（正常路径仍然是原子的）。
    返回 (成功?, 失败原因或'')。
    """
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True, ''
    except Exception as e1:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            try:
                os.remove(tmp)
            except Exception:
                pass
            log('原子替换被拒，已改用原地覆盖写成功（%s）：%r'
                % (os.path.basename(path), e1))
            return True, ''
        except Exception as e2:
            log('写 %s 彻底失败：原子=%r 原地=%r' % (os.path.basename(path), e1, e2))
            return False, '%r（原地覆盖也失败：%r）' % (e1, e2)


def last_write_err():
    """最近一次写失败的原因（**不清空**，150 秒后自动过期）。

    ★ 为什么不清空（2026-09-28）：界面 3 秒轮询一次，若这里读一次就清，
      刚好错过那一次轮询的窗口，用户就永远看不到这条警告了。
      而"一直显示到下次写成功"又太吵 —— 所以带个时间戳，只认最近 150 秒的。
    """
    e, at = _LAST_WRITE_ERR[0], _LAST_WRITE_ERR[1]
    if not e:
        return ''
    if at and (time.time() - at) > 150:
        return ''
    return e


def save_conf(d):
    ok, why = _dump_json(CONF_FILE, d)
    if not ok:
        _LAST_WRITE_ERR[0] = why
        _LAST_WRITE_ERR[1] = time.time()
    return ok


def read_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_state(d):
    ok, why = _dump_json(STATE_FILE, d)
    if not ok:
        _LAST_WRITE_ERR[0] = why
        _LAST_WRITE_ERR[1] = time.time()
    return ok


# ============================================================ 「要不要守护」的判据
# ★★ 2026-09-29（用户第二条诉求的落地）：守护**不该无条件常驻**。
#   用户原话：「真正需要轮询监测的只有一个任务忙不忙的功能……这个进程一启动或者
#   关闭就改变判断状态就可以啊」，以及「我点停止监测，点取消这次关机，这个都能
#   瞬时取消，不应该作为轮询」。
#
#   于是把「总开关 enabled」从「谁想置 True 就置 True」改成一个**算出来的派生量**：
#   有活干才有守护。判据只有下面这一个函数，`cmd_toggle` / `cmd_level` /
#   `cmd_shutdown_set` / `cmd_autostop_off` 全部走它 —— 免得哪天又有人漏改一处，
#   留下一个「什么都没开却每 15 秒查一次库」的常驻进程（这次就是实测踩到的）。

def need_daemon(conf):
    """还有没有任何一件事需要守护去做？

    · 5 个「先别睡」开关里有开着的；或
    · 挂着「跑完自动关机」（要盯任务忙不忙）；或
    · 挂着「到点自动结束」（要睡到点）。

    ★ 2026-09-30：「到点自动结束」现在**依附「防休眠」总开关**，所以这一条也带上
      `F['awake']` —— 总开关关着时，它不算一件事（正常路径上设定已被
      drop_autostop_no_awake 清掉，这里是防残值的第二道）。
    """
    # ★★ 2026-10-11（B1）：判据收成两条 —— 用户原话「我把防休眠这个大开关关掉，
    #   守护理应就失效了」。子开关只挂在总开关下面，**不再单独给守护续命**
    #   （它们的开/关状态照旧保留、不丢，见 cmd_toggle；只是不参与"还需不需要值守"）。
    #   · 总开关开着（防休眠 = 要抑制睡眠）→ 需要；
    #   · 挂着「跑完自动关机」→ 需要（它和总开关**同级**，是独立的收尾项）；
    #   · 「到点自动结束」依附总开关，所以被第一条覆盖。
    F = feats(conf)
    return bool(F['awake'] or conf.get('autoShutdown'))


def sync_enabled(conf, quiet=True):
    """按 need_daemon() 把总开关定下来；需要就把守护拉起来，不需要就让它退。

    返回 True = 还需要守护。

    ⚠️ 它**不动任何开关的姿势** —— 「全关掉」不等于「忘掉」（PommeToys 也是：
       主开关一关子项灰掉但不丢，下次开主开关自动恢复）。要归零请显式调 clear_feats。

    ⚠️ 不需要时**不落 STOP_FLAG、也不发 SIGTERM**：只把 enabled 置 false，
       再 `_poke_daemon()` 把守护叫醒，它读到 `enabled=false` 就自己走 cleanup
       （撤抑制 + 写回合盖设置 + 落状态 + 退出）。这样下一轮 start_daemon() 还能
       照常拉起来 —— 落 STOP_FLAG 会反过来挡住下次启动。
    """
    # ★★ 2026-09-30：先把「防休眠关着、却还挂着到点结束」的设定作废掉。
    #   为什么要在这一层兜住：`cmd_toggle('awake')` 走的是 `write_feats` 而**不是**
    #   `clear_feats`（子开关的"姿势"要留着），所以清理挂在 sync_enabled 这个
    #   所有配置变更的必经口上，免得哪天又新开一条改开关的路就漏了。
    if drop_autostop_no_awake(conf):
        log('「防休眠」已关 → 顺带作废「到点自动结束」设定（只在值守期间计时）')
    need = need_daemon(conf)
    conf['enabled'] = bool(need)
    if need and conf.get('autoShutdown') and not conf.get('autoShutdownBootTs'):
        conf['autoShutdownBootTs'] = boot_time()
    save_conf(conf)
    if need:
        start_daemon(quiet=quiet)
    else:
        log('没有任何开关 / 附加项需要守 → 守护退出，不再空转')
    # ★ 收尾一律戳一下（SIGUSR1 → `_LID_WAKE.set()`）：需要时让它立刻按新组合重算，
    #   不需要时让它立刻醒来看到 `enabled=false` 然后退出。
    #   放在这里 = 「谁改了配置谁负责叫醒」，免得每条命令各写一遍、漏一处就变成
    #   「点了没反应，得等下一轮 15 秒」。
    _poke_daemon()
    return need


def _pid_alive(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def state_alive(st=None, max_age=None):
    """守护是否还活着：看状态文件的 pid + 心跳时间。

    ★★★ 2026-10-13（把 Windows 版 2026-10-02 的修法移植过来；Mac 侧当时漏了这一步）：
      「怎么动不动就没在跑」的根因是**判活阈值写死了 120 秒**，而守护闲下来时
      一轮睡 `TASK_IDLE_SEC`（300 秒），心跳自然也是 300 秒才写一次
      ⇒ 每 300 秒里有 180 秒被判「不在跑」，界面徽标忽明忽暗、看门狗反复误补拉。

      修法（与 Win 版同源）：阈值不再写死，改成**由守护自己在状态文件里声明**
      `hbMax` = 「本轮实际睡多久 × 2 + 60 秒余量」，并且下限 120 秒。于是：
        · 忙时（15 秒一轮）→ max(120, 90) = **120 秒，与改前完全一致**；
        · 闲时（300 秒一轮）→ max(120, 660) = 660 秒，再也不会被误判。
      取不到 `hbMax`（老状态文件，或测试桩）就退回 120 —— 向后兼容。

    ⚠️ 不会因此掩盖真死：上面 `_pid_alive` 已先判过 pid，进程没了直接 False。
    """
    st = st if st is not None else read_state()
    if not st:
        return False
    if not _pid_alive(st.get('pid')):
        return False
    if max_age is None:
        try:
            max_age = float(st.get('hbMax') or 0) or 120.0
        except (TypeError, ValueError):
            max_age = 120.0
    hb = float(st.get('heartbeatAt') or 0)
    return (time.time() - hb) <= max_age


def _next_nap(conf, busy, shutdown_pending=False, shutdown_at=0.0):
    """本轮结束后准备睡多久（秒）—— 也**就是心跳的间隔**。

    ★ 2026-10-13：照 Win 版（2026-10-02）抽成函数。原来这段逻辑在主循环里
      **写了两遍**（一遍正常、一遍 except 兜底），而状态文件现在要声明 `hbMax`
      （判活允许的心跳间隔），**必须**在落状态之前就知道本轮要睡多久 ——
      顺着这个需求把它收敛成一处。

    · 忙 → `TASK_ACTIVE_SEC`(15s)；闲 → `TASK_IDLE_SEC`(300s)
    · 「到点自动结束」/「关机倒计时」是**准点的事** → 睡到那个点为止，不空转
    · 下限 1 秒：万一某个时刻已经过去而决策函数还没放行，不至于变成空转
    """
    nap = TASK_ACTIVE_SEC if busy else TASK_IDLE_SEC
    try:
        at = _cfg_num(conf, 'autoStopAt', 0)
        if at > 0:
            nap = min(nap, max(1.0, at - time.time()))
        if shutdown_pending and shutdown_at:
            nap = min(nap, max(1.0, shutdown_at - time.time()))
    except Exception:
        nap = TASK_ACTIVE_SEC if busy else TASK_IDLE_SEC
    return nap


def hb_max_for(nap):
    """由「这一觉睡多久」推出状态文件里该写的 `hbMax`（允许的最大心跳间隔）。

    ★ 下限 120 秒：**忙时的行为和改前一模一样**（老阈值就是 120），
      闲时才放开到 nap×2+60。留 ×2 是因为 `_sleep_loop` 会被事件提前叫醒、
      也可能被系统挂起拖长，给它一倍的余量才不会误判。
    """
    try:
        return int(max(120.0, float(nap) * 2 + 60))
    except Exception:
        return 120


# ------------------------------------------------------------------ 单实例

def _pid_is_guard(pid):
    """那个 pid 是不是「本程序的守护」（防 pid 被复用后误锁）。

    ★ 2026-10-10：原来的 lock 靠 `read_state()` 可读来判断前任还活着 —— 受限环境里
      状态文件读不到就**放行**（fail-open），实测跑出过两个守护同时守（多一份防休眠
      断言 + 两套关机判定）。改成直接认「那个 pid 的进程是不是本守护」，
      与状态文件、与环境是否受限都无关。
    """
    try:
        r = subprocess.run(['/bin/ps', '-o', 'command=', '-p', str(int(pid))],
                           capture_output=True, text=True, timeout=5)
        return 'wb_powerguard' in (r.stdout or '')
    except Exception:
        return True         # 查不动就保守当成「是自己人」：宁可自己退出，也不要双守护


def _take_instance_lock(wait_sec=8.0, step=0.4):
    """抢单实例锁（pid 文件 + 存活探测），抢到返回 True。

    与 Win 版同思路：不依赖 flock（跨平台行为不一致），
    只写自己的 pid，看到别人活着就不抢。
    """
    deadline = time.time() + wait_sec
    while True:
        try:
            if os.path.exists(INST_FILE):
                old = None
                try:
                    old = int(open(INST_FILE, encoding='utf-8').read().strip() or 0)
                except Exception:
                    old = None
                if old and _pid_alive(old) and old != os.getpid():
                    if _pid_is_guard(old):
                        # 真有一个活着的守护 → 让它独占。等一会再判一次（给「前任正在退出」
                        # 留窗口），到点还活着就自己退出。
                        if time.time() >= deadline:
                            return False
                        time.sleep(step)
                        continue
                    # pid 活着但不是本守护（pid 被复用）⇒ 当陈旧锁，直接接管。
            with open(INST_FILE, 'w', encoding='utf-8') as f:
                f.write(str(os.getpid()))
            return True
        except Exception:
            return False


# ======================================================== 平台实现层：防睡眠
#
# 上面的状态机与 Windows 版一致；下面这一层是**唯一**平台相关的部分。

# ------------------------------------------------------------------ 电源断言（IOPM）
#
# ★★ 2026-10-01（第六十二轮 · 用户「我把 PommeToys 装回来了，你参考他是怎么做的」）：
#   它压睡眠用的**不是** caffeinate，是 IOKit 的**电源断言**（power assertion）。
#   二进制里 grep 到的原话（是查出来的，不是猜的）：
#       IOPMAssertionCreateWithName / IOPMAssertionRelease
#       "PreventUserIdleSystemSleep" / "PreventUserIdleDisplaySleep"
#       PowerAssertionMaking(协议) → SystemPowerAssertions.create(type:reason:)
#   换成断言好在哪（三条，都是我们这个工具真会踩的）：
#     ① **进程一死断言自动消失**。断言是内核绑在「任务」上的：守护被 SIGKILL、
#        崩溃、被系统回收 → 断言当场上交，电脑能睡。而 `caffeinate` 是个**独立
#        子进程**，父进程死了它**不会跟着死** —— 于是「配置里明明写着没开防休眠，
#        电脑却整夜不睡」，而且没人看得出是谁干的。这是实打实的隐患。
#     ② **名字能看**。`pmset -g assertions` 里按名字找得到（见 ASSERT_REASON），
#        排障不用再靠 pid。
#     ③ 切开关是**纯内存操作**：不起进程、不记 pid、不杀进程。
#   ★ 仍然保留 caffeinate 回退（断言这条路不可用时走老路，行为一个字不变）。
#   ★★ 2026-10-01（第六十二轮 b · 学 PommeToys）：**只用两条**
#       PreventUserIdleSystemSleep  ＝ caffeinate -i   ← 「防休眠」就靠它
#       PreventUserIdleDisplaySleep ＝ caffeinate -d   ← 「屏幕保持常亮」才加
#   ⚠️ **不再用 `PreventSystemSleep`（＝ `caffeinate -s`）** —— 这是本工具上一版
#      自己多加的一条，是个错。Apple 官方文档两句话把它说死了：
#        · `PreventUserIdleSystemSleep`："…sleeping **automatically due to a lack of
#          user activity**"、"The system **may still sleep for lid close**, Apple menu,
#          low battery, or other sleep reasons."（该用的）
#        · `PreventSystemSleep`："Prevents the system from sleeping and allows the
#          system to reside in **Dark Wake** … rather than go to sleep."（过强）
#      ⇒ 后果很具体：**合上盖子系统也不睡**，而系统那套「合盖流程」是一整条链
#        （熄屏 + 睡眠）—— 被拦住之后**连熄屏也一起没了**，屏幕一直亮着。
#        它还会拦住用户从苹果菜单点「睡眠」（很气人），而这对「防休眠」毫无必要。
#      ⇒ PommeToys 的做法是**分开管**：防休眠只顶「空闲」，合盖另有开关
#        （`features.keepAwake.lidClose`）。我们照做 —— 见 `feats()` 的 `lidClose`。

# ★★ 断言名**必须是 ASCII** —— 实测（2026-10-01）：
#   传中文名「WorkBuddy 任务管家：防休眠」时，CFString 本身建得好好的
#   （`CFStringGetLength` = 18），断言也建成功，但 `pmset -g assertions`
#   打出来是 `named: ""` —— powerd 打印名字那一步过不了非 ASCII。
#   ⇒ 名字一旦是中文，「名字能看」这个好处就没了，断言也认不出来。
#   所以这里用纯 ASCII；中文说明留在日志与界面上（那是我们自己的地盘）。
#   判据：`pmset -g assertions | grep TimeGuardian`
ASSERT_REASON = 'TimeGuardian-keep-awake'
ASSERT_SYS = 'PreventUserIdleSystemSleep'
ASSERT_DISP = 'PreventUserIdleDisplaySleep'
# ⚠️ 这里**故意没有** `PreventSystemSleep`（旧版叫 `ASSERT_ALL`）—— 见上面那段说明。
#   一句话：它会让「合盖」整条链（熄屏 + 睡眠）都失效 ⇒ 合盖后屏幕一直亮着。
#   真要「合盖也不睡」请开「合盖后保持唤醒」，那才是它的开关（与 PommeToys 一致）。
_K_IOPM_LEVEL_ON = 255          # kIOPMAssertionLevelOn

_IOPM = None                    # None=没试 / False=这条进程内永久不用 / (cf, io)
_IOPM_LOCK = threading.Lock()
_IOPM_WHY = ['']
_ASSERT_OK = [None]             # None=没试过 / True=能用 / False=建过但失败 → 永久回退
_ASSERT_VERIFIED = [False]      # 只复核一次（复核要起一次 pmset）


def _iopm():
    """(CoreFoundation, IOKit) 句柄，且断言函数已绑好；拿不到返回 None。

    ★ 与 `_iokit()` 同套路：懒加载一次、失败就**永久降级**（回退 caffeinate 老路），
      绝不让守护因为拿不到断言而崩掉。
    """
    global _IOPM
    with _IOPM_LOCK:
        if _IOPM is False:
            return None
        if _IOPM is None:
            try:
                cf = ctypes.CDLL('/System/Library/Frameworks/'
                                 'CoreFoundation.framework/CoreFoundation')
                io = ctypes.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
                cf.CFStringCreateWithCString.restype = ctypes.c_void_p
                cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                         ctypes.c_uint32]
                cf.CFRelease.argtypes = [ctypes.c_void_p]
                io.IOPMAssertionCreateWithName.restype = ctypes.c_int32
                io.IOPMAssertionCreateWithName.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_uint32)]
                io.IOPMAssertionRelease.restype = ctypes.c_int32
                io.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
                # ★ 真调一次「释放一个不存在的 id」当探针：函数在不在、能不能调，
                #   当场就知道（返回非 0 是正常的，我们只关心没抛异常）。
                io.IOPMAssertionRelease(0)
                _IOPM = (cf, io)
            except Exception as e:
                _IOPM_WHY[0] = repr(e)
                _IOPM = False
        return _IOPM or None


def assert_available():
    """电源断言这条路现在可用吗。"""
    return _ASSERT_OK[0] is not False and _iopm() is not None


class PowerAssertion(object):
    """一组电源断言的持有者。

    ★ 故意做成「像个子进程」：`poll()` 非 None 表示已经松手。
      这样守护心跳日志里那句 `proc_holder[0].poll()` 一个字都不用改。
    """

    def __init__(self, ids, kinds, why=ASSERT_REASON):
        self.ids = list(ids)
        self.kinds = list(kinds)
        self.why = why
        self._held = True

    def poll(self):
        return None if self._held else 0

    def alive(self):
        return self._held and bool(self.ids)

    def stop(self):
        self._held = False
        ids, self.ids = self.ids, []
        h = _iopm()
        if h is None or not ids:
            return
        cf, io = h
        for i in ids:
            try:
                io.IOPMAssertionRelease(i)
            except Exception:
                pass


def assert_take(keep_display=False):
    """按当前开关建一组断言。成功返回 (PowerAssertion, '')，失败返回 (None, 原因)。

    ★ 一门失败就把已经拿到的全还回去 —— **绝不半吊子**（半吊子最坏：
      以为「屏幕常亮 + 防休眠」都顶上了，其实只顶了一层，还查不出来）。
    """
    h = _iopm()
    if h is None:
        return None, (_IOPM_WHY[0] or 'IOKit / IOPMAssertion 不可用')
    cf, io = h
    kinds = [ASSERT_SYS] + ([ASSERT_DISP] if keep_display else [])
    ids = []
    for k in kinds:
        key = name = None
        try:
            key = cf.CFStringCreateWithCString(None, k.encode(), _KCFSTRING_UTF8)
            name = cf.CFStringCreateWithCString(None, ASSERT_REASON.encode(), _KCFSTRING_UTF8)
            if not key or not name:
                raise RuntimeError('CFString 建不出来')
            aid = ctypes.c_uint32(0)
            rc = io.IOPMAssertionCreateWithName(key, _K_IOPM_LEVEL_ON, name,
                                                ctypes.byref(aid))
            if rc != 0:
                raise RuntimeError('IOPMAssertionCreateWithName 返回 %d' % rc)
            ids.append(aid.value)
        except Exception as e:
            for i in ids:
                try:
                    io.IOPMAssertionRelease(i)
                except Exception:
                    pass
            return None, '%s 建不起来（%s）' % (k, e)
        finally:
            for obj in (key, name):
                if obj:
                    try:
                        cf.CFRelease(obj)
                    except Exception:
                        pass
    return PowerAssertion(ids, kinds), ''


def assert_list():
    """系统里现在挂着几条我们的断言（读 `pmset -g assertions`）。

    ★ **唯一判据**：断言到底建上没建上，只有这里说了算。
      返回 (条数, 命中的原文行列表)；跑不起来返回 (None, [])。
    """
    try:
        rc, out = _run([PMSET, '-g', 'assertions'], timeout=10)
        if rc != 0:
            return None, []
        lines = [ln.strip() for ln in (out or '').splitlines() if ASSERT_REASON in ln]
        return len(lines), lines
    except Exception:
        return None, []


def _awake_kind(keep_display):
    """「怎么顶住睡眠」的计划标识 —— 换后端、换开关都要能被察觉。"""
    if assert_available():
        return ('assert', bool(keep_display))
    return ('proc', bool(keep_display))


def _awake_backend():
    """给状态文件 / --status 看的一行：现在用的是哪条后端。"""
    if _ASSERT_OK[0] is False:
        return 'caffeinate（断言不可用，已回退）'
    return '电源断言' if assert_available() else 'caffeinate'


def _caffeinate_args(keep_display=False):
    """给出 caffeinate 的参数（2026-09-29 起按「开关」而不是「档位」）。

    man caffeinate 的开关：
        -d  防**显示器**睡眠（屏幕常亮）
        -i  防系统**空闲**睡眠
        -m  防磁盘睡眠
        -s  防系统睡眠（**仅在接电源时有效**；电池供电时自动忽略，不报错）

    档位一（不睡 + 屏幕常亮）→ `-d -i`
    档位二（不睡，允许息屏）→ `-i`
    档位三（在档位二基础上合盖也不睡）→ `-i`（合盖那部分另由 pmset 负责）

    ⚠️ **不再带 `-s`**（2026-10-01 第六十二轮 b · 学 PommeToys）。
      `-s` ＝ `PreventSystemSleep` 断言，它**连合盖睡眠一起挡**，而合盖是「熄屏 + 睡眠」
      连在一起的一整条链 —— 拦住之后**屏幕也不灭了**（合盖后一直亮着）。
      它还会拦住用户从苹果菜单点「睡眠」。而「防休眠」要的只是「别因为闲着而睡」，
      `-i` 一条就够。想「合盖也不睡」请开「合盖后保持唤醒」（另由 pmset 负责）。
    """
    args = [CAFFEINATE]
    if keep_display:
        args.append('-d')
    args.append('-i')
    return args


def suppress(level, proc_holder, keep_display=False):
    """施加「先别睡」申请。proc_holder 是 [持有者|None] 容器，便于跨轮复用。

    `keep_display` = 要不要连显示器一起顶住。它与 level **正交** ——
    「合盖也不睡 + 屏幕常亮」这种组合在老的档位体系里根本表达不出来（老 `level`
    是四选一），现在可以了。见 `feats()`。

    ★★ 2026-10-01：持有者现在是**两种东西之一** ——
      · `PowerAssertion`（首选，学 PommeToys）：进程死了一并消失，系统里看得见名字；
      · `Popen`（caffeinate，回退）：断言这条路不可用时照旧。
      两者都有 `.poll()`（None = 还顶着），所以下游代码不用分支。
    """
    if level <= 0:
        release(proc_holder)
        return 0
    if _DRY:
        return level          # 空跑：记成「已施加」，但不起 caffeinate、也不建断言
    wd = bool(keep_display)
    kind = _awake_kind(wd)
    # 已经按同一套在顶着 → 不动它（避免每轮重建）
    old = proc_holder[0]
    if old is not None and old.poll() is None and getattr(old, '_wb_kind', None) == repr(kind):
        return level
    release(proc_holder)

    if kind[0] == 'assert':
        h, why = assert_take(wd)
        if h is not None:
            h._wb_kind = repr(kind)
            proc_holder[0] = h
            _why = '不睡' + (' + 屏幕常亮' if wd else '（允许息屏）')
            log('防休眠生效：%s（电源断言 %s，共 %d 条）'
                % (_why, '+'.join(h.kinds), len(h.ids)))
            # ★ 建完**当场用 `pmset -g assertions` 复核一次**（每个守护进程只做一次）：
            #   断言建了但没登记上去，是这条路唯一的失败模式 —— 不复核就查不出来。
            if not _ASSERT_VERIFIED[0]:
                _ASSERT_VERIFIED[0] = True
                n, lines = assert_list()
                if n:
                    log('电源断言已在系统里登记（pmset 数到 %d 条「%s」）'
                        % (n, ASSERT_REASON))
                else:
                    log('⚠️ 断言建了但 `pmset -g assertions` 数不到「%s」'
                        '（返回 %r）—— 请跑 --status --full 核对' % (ASSERT_REASON, n))
            return level
        _ASSERT_OK[0] = False        # 本次进程内永久回退，别每轮重试刷日志
        log('电源断言没建起来（%s）→ 本条进程改用 caffeinate 子进程（行为不变）' % why)

    argv = _caffeinate_args(wd)
    try:
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p._wb_argv = argv
        p._wb_kind = repr(kind)
        proc_holder[0] = p
        # ★ 2026-09-29：文案跟着开关走（原来印「已开启第 N 档」——
        #   档位这个概念已经从界面上撤掉了，日志再印会让排障的人对不上号）。
        _why = '不睡' + (' + 屏幕常亮' if wd else '（允许息屏）')
        log('防休眠生效：%s（caffeinate %s，pid=%d）'
            % (_why, ' '.join(argv[1:]), p.pid))
        return level
    except Exception as e:
        log('开启 caffeinate 失败：%r' % (e,))
        proc_holder[0] = None
        return 0


def release(proc_holder, quiet=False):
    """撤掉「先别睡」申请 —— 还掉断言 / 杀掉 caffeinate，系统立刻回到自己的电源行为。

    ★★ 2026-10-01：断言只要**还回去**就完事（纯内存调用）；caffeinate 才需要
      杀进程。进程被强杀时前者由内核自动收拾，后者会变成孤儿 —— 这正是换过去的理由。
    """
    h = proc_holder[0]
    proc_holder[0] = None
    if h is None:
        return
    stop = getattr(h, 'stop', None)
    if callable(stop):                       # ★ 断言这条路
        try:
            stop()
        except Exception as e:
            log('还断言出错：%r' % (e,))
        if not quiet:
            log('已撤掉「先别睡」申请（电源断言已上交，系统回到默认电源行为）')
        return
    p = h                                    # 老路：caffeinate 子进程
    try:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        if not quiet:
            log('已撤掉「先别睡」申请（caffeinate 退出，系统回到默认电源行为）')
    except Exception as e:
        log('停 caffeinate 出错：%r' % (e,))


def lid_read():
    """读当前 `SleepDisabled`（0/1）。读不到返回 None。

    ★ 2026-09-28：首选 ctypes 直读 IORegistry（0.0078 ms）。
      原来是 `pmset -g` —— 起进程、约 10 ms，而主循环**每轮要读两次**
      （档位三复查一次 + 写状态一次）→ 0.13% 的常驻开销，就这么来的。
    ⚠️ 两条路的「没有」语义不同，要处理干净：
        `pmset -g` 是「不显示这一行 = 0」；IORegistry 这边是「**属性消失 = 0**」
        → 所以 missing 传 False。若传默认的 None，「没开」会被当成
        「读不到」→ 每轮白走一次回退路，等于没优化。
    """
    v = iokit_bool('SleepDisabled', missing=False)
    if v is not None:
        return 1 if v else 0

    # ---- 回退：pmset -g（IOKit 这条路不可用时才走）----
    rc, out = _run([PMSET, '-g'], timeout=10)
    if rc != 0:
        return None
    for ln in out.splitlines():
        m = re.match(r'\s*SleepDisabled\s+(\d+)', ln)
        if m:
            return int(m.group(1))
    # 有些系统版本不显示这一行 = 默认 0
    return 0


# ★★ 2026-10-01（学 PommeToys 的 `POMMETOYS_FAKE_BATTERY`）：测试钩子。
#   为什么需要：这台机器接电源满电时，「用电池 + 电量低于阈值」这条路**根本走不到**——
#   而它偏偏是「电量不足时自动停」唯一的触发条件。没有钩子就只能靠嘴说「应该没问题」。
#   PommeToys 里对应的是 `SystemBatteryState.fake`（读 `POMMETOYS_FAKE_BATTERY`），
#   连日志格式都一样（`fake battery armed percent=… onBattery=…`）。
#   用法：`WB_FAKE_BATTERY=1,5` = 假装「在用电池 + 电量 5%」；`0,80` = 接电源 + 80%。
#   ⚠️ 只为排障 / 回归；不设时**零开销**（一次 os.environ.get）。
_FAKE_BATT_ENV = 'WB_FAKE_BATTERY'


def fake_battery():
    """解析 `WB_FAKE_BATTERY`。返回 (True, on_batt, pct, charging) 或 None。"""
    v = os.environ.get(_FAKE_BATT_ENV)
    if not v:
        return None
    try:
        a, b = (v.split(',') + [''])[:2]
        on = bool(int(a.strip()))
        pct = int(b.strip())
        return (True, on, pct, not on)
    except Exception:
        return None


def battery_state():
    """读电池。返回 (有没有电池, 是否在用电池, 电量%, 是否在充电)。读不到返回 4 个 None。

    ★ 2026-09-29 加（PommeToys 的「电量不足时自动停」）。
    ★★ 2026-09-29 再改：**先走 IOPS 直读**（`iops_read()`，0.0445 ms、不起进程），
      读不到才退回下面的 `pmset -g batt`（~6 ms，起进程）。
      理由就是原来那句权衡被推翻了：起进程是唯一贵的地方，而这条路
      现在被界面**每 3 秒**问一次、还要给「电量不足时自动停」看阈值 ——
      能用 0.04 ms 拿到的读数，没有理由再去起进程。
    ★★ 2026-10-01：最前面加 `WB_FAKE_BATTERY` 测试钩子（学 PommeToys），见上。
    """
    fk = fake_battery()
    if fk is not None:
        return fk

    r = iops_read()
    if r is not None:
        return r

    # ---- 回退：pmset -g batt（IOPS 这条路不可用时才走）----
    rc, out = _run([PMSET, '-g', 'batt'], timeout=10)
    if rc != 0 or not out:
        return (None, None, None, None)
    txt = out
    if 'InternalBattery' not in txt:
        return (False, None, None, None)
    on_batt = "'Battery Power'" in txt
    pct = None
    m = re.search(r'(\d+)%', txt)
    if m:
        pct = int(m.group(1))
    charging = ('charging' in txt) and ('discharging' not in txt)
    return (True, on_batt, pct, charging)


# 「电量不足」的读数缓存 —— 让「什么时候真读一次」这件事只有一处实现。
_LOWBATT = {'at': 0.0, 'hit': '', 'saidAt': 0.0}

# ★★ 2026-10-01（学 PommeToys 的 `KeepAwakeEndReason` + `endNotice(for:)`）：
#   「上一次**为什么**停」要能查。PommeToys 有个结束原因枚举，界面能直说
#   「因为电量低于 10% 结束了」。我们原来只有日志里一句中文，界面上看不出来 ——
#   用户问「昨晚防休眠是谁关的」时无从回答。
#   kind：'' 没停过 / 'lowBattery' 电量不足 / 'deadline' 到点自动结束
_END = {'kind': '', 'why': '', 'at': 0.0}


def set_end_reason(kind, why):
    _END['kind'] = kind
    _END['why'] = why or ''
    _END['at'] = time.time()


def clear_end_reason():
    if _END['kind']:
        _END.update({'kind': '', 'why': '', 'at': 0.0})


def end_reason():
    return dict(_END)


# ★ 事件驱动的防抖下限（秒）：IOPS 事件偶尔会连着来两条，
#   间隔小于它就沿用上次结果、把 `_IOPS_DIRTY` 留着，下一轮再真读。
_LOWBATT_MIN_GAP = 1.0


def low_batt_reason(conf, now=None):
    """到了「用电池 + 低于阈值」就返回一句原因，否则返回空串。

    ★★ 2026-09-29 改成**事件驱动**（学 PommeToys 的 IOPS 通知）：
      原来：每 `lowBattCheckSec`（默认 60 秒）才真读一次 → **最多晚 60 秒**。
      现在：IOPS 推来事件就把 `_IOPS_DIRTY[0]` 置 True（见 start_pwr_event_watch），
            主循环的下一轮**立刻真读一次** —— 掉到阈值 / 插拔电源当场发现。
      `lowBattCheckSec` 降级成两件事：① 事件不可用时的兜底间隔；
      ② 事件可用时的最长间隔（防「系统一直不来事件」）。
    """
    now = now or time.time()
    iv = max(10.0, float(_cfg_num(conf, 'lowBattCheckSec', 60)))
    age = now - float(_LOWBATT.get('at') or 0)
    if _IOPS_DIRTY[0]:
        # 有新事件 → 想立刻读；但刚读过就别被一串事件打成空转（下一轮再读）
        if age < _LOWBATT_MIN_GAP:
            return _LOWBATT.get('hit') or ''
    elif age < iv:
        return _LOWBATT.get('hit') or ''
    _IOPS_DIRTY[0] = False
    _LOWBATT['at'] = now
    _LOWBATT['hit'] = ''
    try:
        has, on_batt, pct, _charging = battery_state()
    except Exception as e:
        log('读电池状态失败（忽略这一环）：%r' % (e,))
        return ''
    if has and on_batt and pct is not None:
        thr = int(_cfg_num(conf, 'lowBattPct', 10))
        if pct < thr:
            _LOWBATT['hit'] = '用电池 + 电量 %d%%（低于 %d%%）' % (pct, thr)
    return _LOWBATT['hit']


# 「这台机器有没有电池」的读数缓存（只在 pmset 回退路上用得上）。
_BATT_CACHE = {'at': 0.0, 'v': None}


def has_battery():
    """这台机器有没有电池（笔记本）。

    ★ 与 Win 版同名同义 —— 界面上「合盖保持唤醒」只对笔记本有意义，
      台式机/iMac/Mac mini 上要把那个开关解释清楚（不是坏了，是这台机器没盖子）。
      `pmset -g batt` 在没电池的机器上只有一行「Now drawing from 'AC Power'」，
      有电池时会多出 `InternalBattery-0`。探不到返回 None（界面按「未知」显示）。

    ★★ 2026-09-29：**先走 IOPS 直读**（0.0445 ms）——
      界面**每 3 秒**问一次 `/api/status`，原来每次都真起一次 `pmset -g batt`
      （~6 ms）＝ 0.2% 常驻，正是用户说的「这种监测比较吃性能」。
      换成直读之后，60 秒缓存只留给 pmset 回退路（那条路上它仍然有用）。
    """
    r = iops_read()
    if r is not None:
        return r[0]
    now = time.time()
    if _BATT_CACHE['v'] is not None and now - _BATT_CACHE['at'] < 60:
        return _BATT_CACHE['v']
    rc, out = _run([PMSET, '-g', 'batt'], timeout=10)
    if rc != 0:
        return None
    v = ('InternalBattery' in (out or ''))
    _BATT_CACHE['at'] = now
    _BATT_CACHE['v'] = v
    return v


def _lid_write_sudo(value):
    """走免密 sudo 设 SleepDisabled。装了白名单才通。返回 (ok, 原因)。

    判据（可自己复核）：`/usr/bin/su <用户名> -c 'sudo -n /usr/bin/pmset -a disablesleep 0'`
    返回 0 即为通。白名单由 `_排障与验证脚本/_install_lid_sudo.py` 安装，
    内容只有一条：`<用户> ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 0, … 1`
    —— **参数写死**，拿不到任意 root 命令。
    """
    argv = ['/usr/bin/sudo', '-n', PMSET, '-a', 'disablesleep', str(int(value))]
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except Exception as e:                     # 沙盒拦 exec sudo 会走这里
        # ★★ 2026-09-29 用户反复报障：界面服务那棵「受限进程树」拦 setuid 二进制 sudo，
        #   抛 `PermissionError(1, 'Operation not permitted')` —— 白名单装得好好的、
        #   `env_probe` 还报「正常」（它只拿 /usr/bin/true 打枪，测不出 setuid）。
        #   后果：每次点「合盖后保持唤醒」都掉进 osascript 授权框，逼用户输密码。
        #   绕法（按可靠性排序）：
        #     ② **提权桥** —— 让「任务管家.app」（由 launchd 拉起、在干净上下文）
        #        代跑这一条 sudo（见 _lid_via_shell_bridge）。实测最稳，零用户操作。
        #     ③ `launchctl submit` —— 旧路，环境支持时可用，如今多数树里已失声。
        #   两条都断才认栽退回授权框。
        ok2, why2 = _lid_via_shell_bridge(value)
        if ok2:
            return True, ''
        ok3, why3 = _lid_via_launchd(value)
        if ok3:
            return True, ''
        return False, '%s；提权桥也失败（%s）；launchctl 也失败（%s）' % (
            repr(e), why2, why3)
    if r.returncode == 0:
        return True, ''
    # sudo -n 跑起来了但非 0：多半是白名单没装 / 参数不匹配（要密码）。
    # 先走提权桥（干净树里白名单直接生效），再走 launchctl；都非 0 才把真实
    # stderr 抛回去（此时才轮到 osascript 授权框兜底）。
    ok2, why2 = _lid_via_shell_bridge(value)
    if ok2:
        return True, ''
    ok3, why3 = _lid_via_launchd(value)
    if ok3:
        return True, ''
    return False, ((r.stderr or b'').decode('utf-8', 'replace').strip()[:120]
                   or '%s；%s' % (why2, why3))


SHELL_APP = '/Applications/WorkBuddy任务管家.app'
SHELL_APP_NAME = 'WorkBuddy任务管家'


def _shell_running():
    """有没有常驻的「任务管家」实例在跑。

    决定「要不要先等它轮询」：有实例就安心等（它每秒看一次请求）；
    没有就**别白等**、直接冷启一个临时实例 —— 否则每次点按钮都得先干等几秒
    （2026-09-29 晚踩过：e2e 里桥因此拖到 9 秒，把 C 组 8 秒的等待窗撑爆）。
    """
    try:
        r = subprocess.run(['/usr/bin/pgrep', '-f', 'WorkBuddyShell'],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8)
        return bool((r.stdout or b'').strip())
    except Exception:
        return True          # 探不到就当「有」（保守：宁可多等，也别乱冷启）


def _lid_via_shell_bridge(value, wait=6.0):
    """通过「任务管家.app」的**提权桥**执行 pmset（★ 2026-09-29 新增，主通道）。

    为什么需要它（用户反复报「一点合盖后保持唤醒就弹密码框」的根治）：
      界面服务 / 守护可能跑在 **seatbelt 沙箱**里 —— 那棵树里
        · exec /usr/bin/sudo 被内核直接拒（PermissionError: Operation not permitted）；
        · `launchctl submit` / `bootstrap` 全不可用（5: I/O error）。
      而 **WorkBuddy任务管家.app** 由 LaunchServices/launchd 拉起，在**干净上下文**
      （不继承调用方的沙箱），它的 `sudo -n` 能正常命中
      /etc/sudoers.d/workbuddy-lid 的免密白名单 → **全程零密码框**。

    交接走**请求文件**而不是命令行参数：实测 `open -n -a App --args …` 对
    **已在运行**的同 bundle id app **不传参**（新实例的 CommandLine 里没有它们）。
    app 侧实现见 WBShell.swift 的「提权请求文件」段。

    节奏（2026-09-29 晚按实测调到这样）：
      · 有常驻实例 → 等 `wait` 秒（正常情况下 0.3 秒就回）；
      · 没有 → 只等 2.5 秒就冷启（临时实例 ~1 秒办完），**别让用户干等**；
      · 整轮失败 → **再试一轮**（可能撞上外壳正在重启/落盘）。
    返回 (ok, 原因)。
    """
    req = os.path.join(HERE, '_sudo_req.json')
    inv = os.path.join(HERE, '_sudo_invite')
    res = os.path.join(HERE, '_sudo_res.%d-%d.json'
                             % (os.getpid(), int(time.time() * 1000) % 100000))

    def _poll(seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                with open(res, encoding='utf-8') as f:
                    d = json.load(f)
            except Exception:
                d = None
            if d is not None:
                return d
            time.sleep(0.15)
        return None

    def _write_req():
        # ★ 原子写（先 .tmp 再 rename）：外壳那边每秒都在读，读到半截 JSON 会被
        #   当成「正在写入」跳过 —— 不致命，但白白多等一轮。
        tmp = req + '.tmp.%d' % os.getpid()
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'seq': int(time.time() * 1000), 'kind': 'disablesleep',
                       'val': str(int(value)), 'res': res, 'at': time.time()}, f)
        os.replace(tmp, req)

    def _cold_start():
        # ★ 先放「邀请」再 open：外壳靠它认出「我是被拉来干一次活的」，
        #   活儿办完了也**不开窗口**。否则每超时一次就多留一个常驻实例
        #   —— 实测会涨到 7 个、把桥拖成 20 秒超时、最后又掉回密码框。
        with open(inv, 'w', encoding='utf-8') as f:
            f.write(str(time.time()))
        subprocess.run(['/usr/bin/open', '-n', '-g', '-a', SHELL_APP_NAME],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)

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
                return False, '写提权请求失败：%r' % e
            # 有常驻实例 → 安心等；没有 → 短等一下就冷启，别干等
            d = _poll(wait if (resident and attempt == 1) else 2.5)
            if d is not None:
                break
            if not os.path.exists(SHELL_APP):
                return False, '找不到 %s，提权桥不可用' % SHELL_APP
            try:
                _cold_start()
            except Exception as e:
                return False, '拉起提权桥失败：%r' % e
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
        return False, '提权桥没回结果（超时）'
    if int(d.get('rc', -1)) == 0:
        return True, ''
    return False, '提权桥里 sudo 非 0：%s' % str(d.get('msg'))[:80]


def _lid_via_launchd(value):
    """通过 launchctl 的干净进程树执行 `sudo -n pmset -a disablesleep <value>`。

    ★ 为什么存在：界面服务/守护可能跑在一棵被系统塞进「代理程序策略」的受限树里，
      那棵树拦 setuid 二进制（sudo exec 不起来，抛 Operation not permitted），
      但 launchd 拉起的任务树是干净的，白名单 `/etc/sudoers.d/workbuddy-lid`
      直接生效。用 `launchctl submit` 起一次性任务把命令丢到干净树跑，
      结果写进临时文件再读回。返回 (ok, 原因)。

    ⚠️ `launchctl submit` 是异步的、不吐退出码，所以命令自己把 `rc` 写进结果文件，
       本函数轮询那个文件（最多等 ~4 秒）。任务标签带 pid + 随机后缀，避免撞名。
    """
    label = 'com.wb.wb-lid-%d-%d' % (os.getpid(), int(time.time() * 1000) % 100000)
    fd, out_path = tempfile.mkstemp(prefix='wb-lid-', suffix='.out')
    os.close(fd)
    try:
        # 干净树里跑：sudo -n 免密设值；把退出码写进结果文件（echo 追加，最后一行即 rc）。
        cmd = ('/usr/bin/sudo -n /usr/bin/pmset -a disablesleep %d; '
               'echo "RC=$?" > "%s"' % (int(value), out_path))
        rc_submit, _ = _run(['/bin/launchctl', 'submit', '-l', label, '--',
                             '/bin/bash', '-c', cmd], timeout=15)
        if rc_submit != 0:
            return False, 'launchctl submit 失败'
        # 轮询结果文件（submit 后任务可能延迟几十毫秒才起）。
        deadline = time.time() + 4.0
        result = None
        while time.time() < deadline:
            if os.path.exists(out_path):
                try:
                    with open(out_path, encoding='utf-8', errors='replace') as f:
                        result = f.read().strip()
                except Exception:
                    result = None
                if result:
                    break
            time.sleep(0.1)
        if not result:
            return False, '干净树任务没回结果（超时）'
        m = re.search(r'RC=(\d+)', result)
        if not m:
            return False, '结果文件读不出 RC（内容：%s）' % result[:60]
        if int(m.group(1)) == 0:
            return True, ''
        return False, '干净树里 sudo 仍非 0（RC=%s）' % m.group(1)
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass
        try:
            _run(['/bin/launchctl', 'remove', label], timeout=10)
        except Exception:
            pass


def lid_write(value):
    """把 SleepDisabled 设成 value（0/1）。

    ★ 三条路，按顺序试（2026-09-29 又一次改造 —— 治「点合盖后保持唤醒反复弹
      密码框」；2026-09-28 那次只治了「切一次档位三要输一次密码」）：

      ① `sudo -n` —— 装了 /etc/sudoers.d/workbuddy-lid 就秒过、**不弹框**。
         `-n` = 不交互：需要密码时**立刻失败**，绝不会把流程挂住。
      ② **提权桥**（`_lid_via_shell_bridge`）—— 当前进程树**禁 exec sudo**
         （WorkBuddy 会话沙箱，以及从它派生的界面服务/守护那棵树，都会拦 setuid
         二进制、报 `Operation not permitted`）时的**主路**：把请求交给
         「任务管家.app」（由 launchd 拉起、在干净上下文）代跑，白名单在那边
         直接生效 → **仍然零弹窗、零用户操作**。
      ③ `launchctl submit`（`_lid_via_launchd`）—— 旧路，环境支持时可用。
      ④ 兜底 `osascript … with administrator privileges` —— 只有 ①②③ 全断才会走到，
         由**系统自己**弹标准密码框（用户看得见、可取消）。正常情况下永远到不了这里。
    """
    if _DRY:
        log('（空跑）本应执行：pmset -a disablesleep %d' % int(value))
        return True, ''
    ok, why = _lid_write_sudo(value)
    if ok:
        return True, ''
    if why:
        log('免密通道不可用（%s）→ 改用系统授权框' % why[:100])
    script = 'do shell script "%s -a disablesleep %d" with administrator privileges' % (
        PMSET, int(value))
    rc, out = _run([OSASCRIPT, '-e', script], timeout=90)
    if rc == 0:
        return True, ''
    return False, (out or '').strip()[:200]


def lid_apply_off(conf, proc_holder=None):
    """档位三：关掉「合盖睡眠」。原值记进配置，便于恢复。

    ★★ 记账规则（2026-09-28 修 claim bug）：**只在真的写入过时**才记原值、才认领所有权。
      起因：原实现里 `if cur == 1: conf['lidChanged'] = True` —— 自己**一个字没写**
      （SleepDisabled 本来就是 1，多半是 PommeToys / 用户自己开的），却认领成
      「我改的」，还把**当前值 1** 当成「原值」记进 lidOriginal。后果：停止监测时
      lid_restore_conf 见 `cur == orig` → 一笔不写、直接放弃追踪 → 等 PommeToys
      也退出后，SleepDisabled 会**永久停在 1**，这台 Mac 再也不自动睡。
      本机 2026-09-28 01:03 实测就是这个状态（lidOriginal 记成 1，真原值是 0）。

    现在的口径：
      · cur == 1 → 如实报「本来就是关的」，**不认领、不记原值**（除非我们自己早就改过）；
      · cur == 0 → 真的写 1；**写成功之后**才记 lidOriginal=0、才置 lidChanged。
    """
    cur = lid_read()
    if cur is None:
        return False, '读不出当前合盖设置（pmset 不可用？）'
    if cur == 1:
        if conf.get('lidChanged'):
            # 我们自己之前改过、现在仍是 1 → 账还在，保持现状（不动 lidOriginal）
            return True, '（已经是关的）'
        return True, '（本来就是关的 —— 不是我们改的，停止监测时不会写回）'
    ok, err = lid_write(1)
    if ok:
        conf['lidOriginal'] = cur      # ★ 写成功后才记；此刻 cur 一定是 0
        conf['lidChanged'] = True
        save_conf(conf)
        _GATE['stopped'] = ''          # ★ 重新设回去 → 清掉「被谁弄停」的记录
        log('「合盖后保持唤醒」：已关闭「合盖睡眠」（原值 %s 记在 lidOriginal）' % cur)
        return True, ''
    return False, err


def lid_restore_conf(quiet=False):
    """把「合盖睡眠」写回原值。停止监测 / 守护退出时调用。"""
    conf = load_conf()
    if not conf.get('lidChanged'):
        return True, ''
    orig = conf.get('lidOriginal')
    try:
        orig = int(orig)
    except Exception:
        orig = 0
    cur = lid_read()
    if cur == orig:
        conf['lidChanged'] = False
        save_conf(conf)
        return True, ''
    ok, err = lid_write(orig)
    if ok:
        conf['lidChanged'] = False
        save_conf(conf)
        if not quiet:
            log('已把「合盖睡眠」恢复成原值 %d' % orig)
        return True, ''
    if not quiet:
        log('恢复合盖设置失败（可能用户取消了授权）：%s' % err)
    return False, err


# =========================================== 平台实现层：档位三「合盖熄屏」（2026-09-28 加）
#
# ★★ 为什么要有这一块（用户 2026-09-28 实测报障：开了档位三合盖，屏幕还亮着）：
#    `pmset -a disablesleep 1` 短路的是「合盖 → 系统睡眠」这一步；而**正常**合盖时，
#    「把内建屏熄灭」是同一套合盖处理顺手做的 —— 被短路之后这一步也没了。
#    这是 disablesleep 的固有副作用（换成 `caffeinate -d` 也没用，那是反方向的
#    「别熄屏」）。macOS 在「没有外接屏」时不提供任何「合盖不睡 + 熄屏」的开关，
#    所以本工具自己补一刀：**合盖期间反复 `pmset displaysleepnow`**（只熄屏、不睡觉）。
#
#    三个判据全部走 **公共 API**（CoreGraphics），不 exec 也不碰私有 IOKit：
#      · 熄没熄    `CGDisplayIsAsleep(CGMainDisplayID())`
#      · 有没有外接屏 `CGGetActiveDisplayList` + `CGDisplayIsBuiltin`
#      · 合盖没合盖 `ioreg -r -k AppleClamshellState`（挂在 IOPMrootDomain 上）
#
#    ⚠️ 这是**软件补丁**：内建屏的背光关掉了，但屏还是"通着电"的（比硬件方案费电、发热）。
#       想要物理级断电，走「假负载头 + 电源」进真·合盖模式 —— 见《合盖模式指南》。
_CG = None


# ==================================================================== IOKit 直读
#
# ★★ 为什么要有这一层（2026-09-28，用户问「这些轮询是不是吃性能」时查出来的）
#
#   原来两个读数都是**起一个新进程**（fork+exec）：
#       ioreg -n IOPMrootDomain -r -d 1     单次 10.56 ms（其中 CPU 9.02 ms）
#       pmset -g                            单次约 10 ms
#   而档位三开着时：每 2 秒读一次合盖、每 15 秒读两次电源设置 ——
#       → 0.45% + 0.13% 的**持续 CPU，整夜不停**，是全套守护里唯一有实感的一笔。
#   换成 ctypes 直接读 IORegistry 属性后：**0.0078 ms / 次**（实测 500 次均值），
#   快约 1350 倍，且**不起进程**。⇒ 「判合盖状态」本身根本不要钱，
#   贵的从来是「起一个进程」。
#
# ★ 语义（2026-09-28 用 ioreg 交叉核对，两路读数逐字一致）
#       AppleClamshellState         Yes = 盖子合上了（开盖为 No）
#       SleepDisabled               Yes = 「合盖也不睡」已开；**属性消失 = 0**
#       AppleClamshellCausesSleep   Yes = 合盖会导致睡眠（这是**配置**不是状态）
#
# ⚠️ 规矩：读不到一律返回 None（或调用方指定的 missing 值），
#    **绝不**把「读不到」默认成 False —— 那会把合盖误判成开盖，合盖了反而不熄屏。
_IOKIT = None
_IOKIT_LOCK = threading.Lock()
_IOKIT_SERVICE = 'IOPMrootDomain'
_KCFSTRING_UTF8 = 0x08000100


def _iokit():
    """(IOKit, CoreFoundation) 句柄；拿不到返回 None。

    ★ 与 _cg() 同套路：懒加载一次、失败就**永久降级**（回退 ioreg / pmset 老路），
      绝不让守护因为读不到属性而崩掉。
    """
    global _IOKIT
    with _IOKIT_LOCK:
        if _IOKIT is False:
            return None
        if _IOKIT is None:
            try:
                cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
                io = ctypes.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
                cf.CFStringCreateWithCString.restype = ctypes.c_void_p
                cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                         ctypes.c_uint32]
                cf.CFBooleanGetValue.restype = ctypes.c_bool
                cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
                cf.CFGetTypeID.restype = ctypes.c_ulong
                cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
                cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
                cf.CFRelease.argtypes = [ctypes.c_void_p]
                io.IOServiceMatching.restype = ctypes.c_void_p
                io.IOServiceMatching.argtypes = [ctypes.c_char_p]
                io.IOServiceGetMatchingService.restype = ctypes.c_uint32
                io.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
                io.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
                io.IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_uint32, ctypes.c_void_p,
                                                               ctypes.c_void_p, ctypes.c_uint32]
                io.IOObjectRelease.argtypes = [ctypes.c_uint32]
                # ★ 同时校验一次「真能读到」，读不到就当不可用（免得以后每次调用白试）
                if not io.IOServiceGetMatchingService(0, io.IOServiceMatching(_IOKIT_SERVICE.encode())):
                    raise RuntimeError('匹配不到 %s' % _IOKIT_SERVICE)
                _IOKIT = (cf, io)
            except Exception:
                _IOKIT = False
        return _IOKIT or None


def iokit_available():
    """IOKit 直读这条路现在可用吗（给自检 / --status 用）。"""
    return _iokit() is not None


def iokit_bool(name, service=_IOKIT_SERVICE, missing=None):
    """读 IORegistry 上的布尔属性。True / False / None（读不到）。

    missing：**服务拿到了、但这个属性不存在**时返回什么（默认 None）。
      例：`SleepDisabled` 属性消失就等于「没开」，所以调用方传 missing=False。
      「库/服务本身就不可用」则一律返回 None，让调用方走回退路。

    ★ 每次重新取一次服务句柄（实测只多 0.0036 ms，0.0042 → 0.0078）——
      换来的是**线程安全**：合盖事件线程与主循环可能同时读，
      共用一个句柄不放心，出问题会很难查。
    """
    h = _iokit()
    if h is None:
        return None
    cf, io = h
    svc = 0
    key = None
    val = None
    try:
        svc = io.IOServiceGetMatchingService(0, io.IOServiceMatching(service.encode()))
        if not svc:
            return None                      # 服务都没了 → 这条路暂时不通
        key = cf.CFStringCreateWithCString(None, name.encode(), _KCFSTRING_UTF8)
        if not key:
            return None
        val = io.IORegistryEntryCreateCFProperty(svc, key, None, 0)
        if not val:
            return missing                   # 服务在、属性不在 → 由调用方解释
        if cf.CFGetTypeID(val) != cf.CFBooleanGetTypeID():
            return missing                   # 不是布尔（罕见）→ 同「没有」
        return bool(cf.CFBooleanGetValue(val))
    except Exception:
        return None
    finally:
        for obj, release in ((val, cf.CFRelease), (key, cf.CFRelease), (svc, io.IOObjectRelease)):
            if obj:
                try:
                    release(obj)
                except Exception:
                    pass


# ============================================== 电源 / 电量变化事件监听（IOPS）
#
# ★★ 2026-09-29 学 PommeToys（用户点名要学的那一条）。
#   PommeToys 二进制里逐个 grep 到的符号（是查出来的，不是猜的）：
#       IOPSNotificationCreateRunLoopSource      ← 注册回调，电源状态一变系统就推消息
#       IOPSCopyPowerSourcesInfo / IOPSGetPowerSourceDescription
#       kIOPSCurrentCapacityKey / kIOPSPowerSourceStateKey / kIOPSIsChargingKey
#       IOPSGetTimeRemainingEstimate
#   ⇒ 它**一身事件驱动，没有一处「每 N 秒去问一次」**。
#
#   我们原来那套：`pmset -g batt`（起进程 ~6 ms）+ **60 秒缓存** ——
#   意味着「电量掉到阈值」或「拔掉电源」**最多晚 60 秒**才被察觉。
#   用户问过「电量不足时自动停为什么不立刻停」，根因就是这条缓存。
#
#   现在：系统推事件 → 回调只做三件小事（计数 / 标脏 / 叫醒主循环）
#   → 主循环这一轮立刻真读一次 → 该停就停。**零轮询，而且更准。**
#
# ⚠️ 本机实测（2026-09-29，可随时用 `--selftest-pwr` 复现）：
#     · `IOPSCopyPowerSourcesInfo()` 返回的是 **CFArray**（每项一个 CFDictionary），
#       **不是** CFDictionary —— 按字典写会当场抛
#       `NSInvalidArgumentException: -[__NSCFArray objectForKey:]`（第一版就踩了）；
#     · 「接的是交流电还是电池」的键叫 **`Power Source State`**
#       （值 'AC Power' / 'Battery Power'），在**每一项的字典里**；
#     · 读数 **0.0445 ms/次**（pmset 是 ~6 ms 且要起进程）→ 快约 135 倍；
#     · `IOPSNotificationCreateRunLoopSource` 返回非 0 = 建得起来；
#     · ⚠️ **启动后来不了一次初始回调**（静候 3 秒，0 次）→ 必须自己主动读一次初值。
#
# ⚠️ 三条坑（合盖那条全踩过，照抄经验）：
#   ① `CFUNCTYPE` 回调对象必须挂**模块级全局**，否则被 GC → 野指针；
#   ② 启动后**必须主动读一次**做初值（见上）；
#   ③ **回退必须留** —— IOPS 不可用时降回 `pmset -g batt`（老路原样保留）。
_IOPS = None
_IOPS_LOCK = threading.Lock()
_IOPS_WHY = ['']
# 事件账本（给 --status / 自检看：到底有没有收到过推送）
_IOPS_EVENT = {'ok': None, 'why': '', 'count': 0, 'lastAt': 0.0}
_IOPS_CB = None                 # ★ C 回调的函数指针，必须常驻（见 start_pwr_event_watch）
# ★ 缓存是否作废：True = 下一轮必须真读一次电量。
#   初值就是 True —— 对应「启动后没有初始回调，要自己读一次」。
_IOPS_DIRTY = [True]

_IOPS_KEYS = {
    'state': b'Power Source State',
    'cur':   b'Current Capacity',
    'max':   b'Max Capacity',
    'chg':   b'Is Charging',
    'type':  b'Type',
}


def _iops():
    """(cf, io, keys) 句柄；拿不到返回 None（调用方走 `pmset -g batt` 回退）。

    ★ 与 _iokit() / _cg() 同套路：懒加载一次、失败就**永久降级**。
      复用 _iokit() 已经 dlopen 好的那两个句柄，不再重复加载。
    """
    global _IOPS
    with _IOPS_LOCK:
        if _IOPS is False:
            return None
        if _IOPS is None:
            try:
                h = _iokit()
                if h is None:
                    raise RuntimeError('IOKit 不可用')
                cf, io = h
                cf.CFStringGetCString.restype = ctypes.c_bool
                cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                  ctypes.c_long, ctypes.c_uint32]
                cf.CFStringGetTypeID.restype = ctypes.c_ulong
                cf.CFArrayGetCount.restype = ctypes.c_long
                cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
                cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
                cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
                cf.CFArrayGetTypeID.restype = ctypes.c_ulong
                cf.CFDictionaryGetTypeID.restype = ctypes.c_ulong
                cf.CFDictionaryGetValue.restype = ctypes.c_void_p
                cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                cf.CFNumberGetTypeID.restype = ctypes.c_ulong
                cf.CFNumberGetValue.restype = ctypes.c_bool
                cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
                io.IOPSCopyPowerSourcesInfo.restype = ctypes.c_void_p
                io.IOPSCopyPowerSourcesInfo.argtypes = []
                if not io.IOPSCopyPowerSourcesInfo():
                    raise RuntimeError('IOPSCopyPowerSourcesInfo 返回空')
                keys = {k: cf.CFStringCreateWithCString(None, v, _KCFSTRING_UTF8)
                        for k, v in _IOPS_KEYS.items()}
                # ⚠️ 任何一个键没建出来，后面 CFDictionaryGetValue 就会收到 NULL 键 → 危险。
                if not all(keys.values()):
                    raise RuntimeError('CFStringCreateWithCString 建键失败')
                _IOPS = (cf, io, keys)
            except Exception as e:
                _IOPS_WHY[0] = repr(e)
                _IOPS = False
        return _IOPS or None


def _iops_s(cf, v):
    """CFString → str（不是字符串就返回 None）。"""
    if not v or cf.CFGetTypeID(v) != cf.CFStringGetTypeID():
        return None
    buf = ctypes.create_string_buffer(256)
    if cf.CFStringGetCString(v, buf, 256, _KCFSTRING_UTF8):
        return buf.value.decode('utf-8', 'replace')
    return None


def _iops_n(cf, v):
    """CFNumber → int（不是数字就返回 None）。"""
    if not v or cf.CFGetTypeID(v) != cf.CFNumberGetTypeID():
        return None
    c = ctypes.c_int(0)
    return c.value if cf.CFNumberGetValue(v, 9, ctypes.byref(c)) else None


def _iops_desc(cf, keys, d):
    """从「一份电源描述字典」里取 (有电池, 在不在用电池, 电量%, 充电中)。"""
    typ = _iops_s(cf, cf.CFDictionaryGetValue(d, keys['type']))
    st = _iops_s(cf, cf.CFDictionaryGetValue(d, keys['state']))
    cur = _iops_n(cf, cf.CFDictionaryGetValue(d, keys['cur']))
    mx = _iops_n(cf, cf.CFDictionaryGetValue(d, keys['max'])) or 100
    cg = cf.CFDictionaryGetValue(d, keys['chg'])
    # 有电池的判据：类型写着 InternalBattery；老系统没有 Type 键时，
    # 只要报得出容量就当它有电池（台式机/iMac 上这两条都不成立）。
    has = (typ == 'InternalBattery') or (typ is None and cur is not None)
    pct = None if cur is None else (int(cur) if mx == 100 else int(round(100.0 * cur / mx)))
    charging = (bool(cf.CFBooleanGetValue(cg))
                if (cg and cf.CFGetTypeID(cg) == cf.CFBooleanGetTypeID()) else None)
    return (has, (st == 'Battery Power') if st else None, pct, charging)


def iops_read():
    """ctypes 直读电量/电源。返回 (有电池, 用电池, 电量%, 充电中)；不可用返回 None。

    ★ 不起进程、0.0445 ms —— 这是「界面每 3 秒问一次有没有电池」和
      「电量不足时自动停」共用的读值入口。
    """
    h = _iops()
    if h is None:
        return None
    cf, io, keys = h
    blob = None
    try:
        blob = io.IOPSCopyPowerSourcesInfo()
        if not blob:
            return None
        # ★ 实测这台机器给的是 CFArray（每项一个描述字典）；老系统可能给 CFDictionary。
        if cf.CFGetTypeID(blob) == cf.CFArrayGetTypeID():
            arr = blob
        elif cf.CFGetTypeID(blob) == cf.CFDictionaryGetTypeID():
            return _iops_desc(cf, keys, blob)
        else:
            return None
        rows = []
        for i in range(cf.CFArrayGetCount(arr)):
            e = cf.CFArrayGetValueAtIndex(arr, i)
            if e and cf.CFGetTypeID(e) == cf.CFDictionaryGetTypeID():
                rows.append(_iops_desc(cf, keys, e))
        if not rows:
            return (False, None, None, None)        # 台式机：没有任何电源项
        has = any(r[0] for r in rows)
        for r in rows:                              # 优先采用「报了电源状态」的那一项
            if r[1] is not None:
                return (has, r[1], r[2], r[3])
        return (has, None, rows[0][2], rows[0][3])
    except Exception:
        return None
    finally:
        if blob:
            try:
                cf.CFRelease(blob)
            except Exception:
                pass


def pwr_event_status():
    """返回 (订阅是否成功, 说明, 收到次数, 最后一次的时刻)。"""
    return (_IOPS_EVENT['ok'], _IOPS_EVENT['why'], _IOPS_EVENT['count'],
            _IOPS_EVENT['lastAt'])


def start_pwr_event_watch(quiet=False):
    """起一个后台线程订阅 IOPS 的「电源 / 电量变化」事件。返回是否订阅成功（幂等）。

    ★ 回调里**只做三件事**：计数 + 记时间戳 + 标脏并叫醒主循环。
      绝不在回调里起进程/读文件 —— 它在 CFRunLoop 线程上跑，
      做重活会拖慢整个系统电源事件的分发，而且出问题极难查。
      真正的「够不够低、要不要停」仍由主循环（自己的线程、自己的节奏）决定。

    ★ 为什么复用 `_LID_WAKE`：主循环只等**一个** Event。合盖、电量、命令行点名，
      三者都是「有事了，回去重算一轮」—— 共用同一个事件最省事，
      限流闸（`_WAKE_MIN_GAP`）本来也只针对自动事件、且电源事件本就低频。
    """
    if not IS_MAC:
        _IOPS_EVENT['ok'] = False
        _IOPS_EVENT['why'] = '非 macOS'
        return False
    if _IOPS_EVENT['ok'] is not None:
        return bool(_IOPS_EVENT['ok'])
    h = _iops()
    if h is None:
        _IOPS_EVENT['ok'] = False
        _IOPS_EVENT['why'] = 'IOPS 不可用（%s）' % (_IOPS_WHY[0] or '未知')
        return False
    cf, io, _keys = h
    try:
        io.IOPSNotificationCreateRunLoopSource.restype = ctypes.c_void_p
        io.IOPSNotificationCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRunLoopRun.argtypes = []

        def _cb(_ctx):
            # ★ 只有这几行允许跑在回调线程上。
            _IOPS_EVENT['count'] += 1
            _IOPS_EVENT['lastAt'] = time.time()
            _IOPS_DIRTY[0] = True    # 把读数缓存作废 → 下一轮真读一次
            _LID_WAKE.set()          # 把主循环叫醒，让它自己去读、去做决定

        # ★ 必须把 CFUNCTYPE 对象**挂在全局**：局部变量被 GC 掉之后，
        #   C 那边还留着函数指针 → 下一次事件进来就是野指针。
        global _IOPS_CB
        _IOPS_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(_cb)

        src = io.IOPSNotificationCreateRunLoopSource(_IOPS_CB, None)
        if not src:
            raise RuntimeError('IOPSNotificationCreateRunLoopSource 返回空')

        def _loop():
            try:
                mode = ctypes.c_void_p.in_dll(cf, 'kCFRunLoopDefaultMode')
                cf.CFRunLoopAddSource(cf.CFRunLoopGetCurrent(), src, mode)
                cf.CFRunLoopRun()          # 阻塞在这个线程上，等系统推消息
            except Exception as e:
                _IOPS_EVENT['why'] = '事件线程异常：%r' % (e,)

        threading.Thread(target=_loop, name='wb-pwr-event', daemon=True).start()
        # ★ 启动后系统**不会**补一次初始回调（实测 3 秒 0 次）→ 自己读一次初值。
        _IOPS_DIRTY[0] = True
        _IOPS_EVENT['ok'] = True
        _IOPS_EVENT['why'] = 'IOPS 事件订阅已就绪'
        if not quiet:
            log('电量/电源事件监听已启动（IOPS 通知，不再靠每 60 秒问一次）')
        return True
    except Exception as e:
        _IOPS_EVENT['ok'] = False
        _IOPS_EVENT['why'] = repr(e)
        if not quiet:
            log('电源事件监听没能启动（%r）→ 退回定时问一次，功能不受影响' % (e,))
        return False


# ============================================== 合盖事件监听（IOKit 通知）
#
# ★★ 这一节就是用户说的「做成和 PommeToys 一模一样」的那一半。
#
#   PommeToys 的做法（2026-09-28 从其二进制破译）：`PommeToysLidGuard.ClamshellWatch`
#   用 IOKit 的 **interest notification** 订阅 `IOPMrootDomain` —— 合盖时被系统
#   **推**一条消息，而不是每 2 秒主动去问一次。我们原来那套是「每 2 秒 spawn
#   一次 ioreg」，一分钟 30 个进程 —— 这正是用户觉得「很多监测、吃性能」的地方。
#
#   本机实测（2026-09-28 23:17，kr=0 订阅成功）：
#     · `IOServiceAddInterestNotification(port, IOPMrootDomain,
#         b'IOGeneralInterest', cb, NULL, &notify)` → 返回 0 = KERN_SUCCESS
#     · 执行 `pmset displaysleepnow` 后**立刻**收到 `type=0xe0034150`
#   ⇒ 机制可用。
#
# ⚠️ 但**没有**把兜底轮询整个删掉，理由写在这里（免得以后有人以为忘了删）：
#   ① 「合盖」这一动作本身没能在本机直接验证（总不能替用户合盖子），
#      而 `displaysleepnow` 触发的消息类型未必等同于合盖事件；
#   ② 直读一次只要 **0.0092 ms**（见上面 IOKit 直读层），5 秒兜底一次
#      的开销是 **0.0002%** —— 等于免费。
#   所以取「事件负责快、轮询负责稳」：收到事件立刻处理 + 低频兜底。
LID_EVENT_FALLBACK_SEC = 5     # 有事件监听时的兜底轮询间隔（秒）
_LID_WAKE = threading.Event()  # 事件到来时 set 它 → 主循环的 sleep 立刻返回
# ★ 早退的限流闸：IOKit 那一路是**故意不按消息类型过滤**的（见 _cb 里的长注释），
#   万一电源域来一串消息，每次都早退就等于把主循环打成空转（probe 要查库、扫会话文件）。
#   限到 2 秒一次：既能「合盖 2 秒内熄屏」，又不会被消息风暴拖垮。
_WAKE_MIN_GAP = 2.0
_LAST_WAKE_AT = [0.0]
# ★★ 但「命令行点了一下」是**人**在等结果（点开关 / 点停止 / 点取消这次关机），
#   这种必须**立刻**回去重读配置，一次都不能漏 —— 限流只针对自动事件。
#   于是单开一个旗标：SIGUSR1 处理器同时置它，`_sleep_loop` 见到就无条件早退。
#   （原来只有一个 Event，限流一开就把「点一下」也一起压掉了：实测点了开关
#     要等 8 ~ 15 秒才生效，症状和「点了没反应」一样。）
_CLI_POKE = [False]

# 事件监听的运行时账本（给 --status 看：到底有没有收到过事件）
_LID_EVENT = {'ok': None, 'why': '', 'count': 0, 'lastAt': 0.0, 'msgs': 0, 'lastMsg': 0}
_LID_CB = None                 # ★ C 回调的函数指针，必须常驻（见 start_lid_event_watch）


def lid_event_status():
    """返回 (订阅是否成功, 说明, 收到次数, 最后一次的时刻)。

    ★ 专门留这个给界面/自检用：如果 `count` 一直是 0 而用户确实合过盖，
      就说明这条路在本机不成立 —— 那是**关键现场**，比「猜」强得多。
    """
    return (_LID_EVENT['ok'], _LID_EVENT['why'], _LID_EVENT['count'], _LID_EVENT['lastAt'])


def start_lid_event_watch(quiet=False):
    """起一个后台线程订阅 `IOPMrootDomain` 的事件。返回是否订阅成功（幂等）。

    ★ 回调里**只做两件事**：计数 + `set()` 一个 Event。
      绝不在回调里读写文件/起进程 —— 它在 CFRunLoop 线程上跑，
      做重活会拖慢整个系统电源事件的分发，而且出问题极难查。
      真正的「合盖了怎么办」仍由主循环（自己的线程、自己的节奏）决定。
    """
    if not IS_MAC:
        _LID_EVENT['ok'] = False
        _LID_EVENT['why'] = '非 macOS'
        return False
    if _LID_EVENT['ok'] is not None:
        return bool(_LID_EVENT['ok'])

    h = _iokit()
    if h is None:
        _LID_EVENT['ok'] = False
        _LID_EVENT['why'] = 'IOKit 不可用'
        return False
    cf, io = h

    try:
        io.IONotificationPortCreate.restype = ctypes.c_void_p
        io.IONotificationPortCreate.argtypes = [ctypes.c_uint32]
        io.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
        io.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]
        io.IOServiceAddInterestNotification.restype = ctypes.c_int
        io.IOServiceAddInterestNotification.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRunLoopRun.argtypes = []

        def _cb(_refcon, _service, msg_type, _msg_arg):
            # ★ 只有这几行允许跑在回调线程上。
            #
            # ⚠️ **故意不按消息类型过滤**（2026-09-28 定）：实测 `displaysleepnow`
            #    推来的是 `0xe0034150`，但「合盖」推来的类型没法在本机确证 ——
            #    一旦白名单写错，事件就被自己过滤掉了，症状还是「合盖没反应」，
            #    而且比没有事件更难查。所以**任何**电源域消息都当成线索：
            #    叫醒主循环去读一次状态（0.0092 ms），状态没变就什么都不做。
            _LID_EVENT['msgs'] += 1
            _LID_EVENT['lastMsg'] = msg_type
            _LID_EVENT['count'] += 1
            _LID_EVENT['lastAt'] = time.time()
            _LID_WAKE.set()          # 把主循环叫醒，让它自己去读状态、做事

        # ★ 必须把 CFUNCTYPE 对象**挂在全局**：局部变量被 GC 掉之后，
        #   C 那边还留着函数指针 → 下一次事件进来就是野指针。
        global _LID_CB
        _LID_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32,
                                   ctypes.c_uint32, ctypes.c_void_p)(_cb)

        port = io.IONotificationPortCreate(0)
        svc = io.IOServiceGetMatchingService(0, io.IOServiceMatching(_IOKIT_SERVICE.encode()))
        if not port or not svc:
            raise RuntimeError('IONotificationPortCreate/IOServiceGetMatchingService 失败')
        notify = ctypes.c_uint32(0)
        kr = io.IOServiceAddInterestNotification(port, svc, b'IOGeneralInterest',
                                                 _LID_CB, None, ctypes.byref(notify))
        if kr != 0:
            raise RuntimeError('IOServiceAddInterestNotification 返回 %d' % kr)

        def _loop():
            try:
                src = io.IONotificationPortGetRunLoopSource(port)
                mode = ctypes.c_void_p.in_dll(cf, 'kCFRunLoopDefaultMode')
                cf.CFRunLoopAddSource(cf.CFRunLoopGetCurrent(), src, mode)
                cf.CFRunLoopRun()          # 阻塞在这个线程上，等系统推消息
            except Exception as e:
                _LID_EVENT['why'] = '事件线程异常：%r' % (e,)

        threading.Thread(target=_loop, name='wb-lid-event', daemon=True).start()
        _LID_EVENT['ok'] = True
        _LID_EVENT['why'] = 'IOKit 事件订阅已就绪（kr=0）'
        if not quiet:
            log('合盖事件监听已启动（IOKit 通知，不再靠每 2 秒问一次）')
        return True
    except Exception as e:
        _LID_EVENT['ok'] = False
        _LID_EVENT['why'] = repr(e)
        if not quiet:
            log('合盖事件监听没能启动（%r）→ 退回低频轮询，功能不受影响' % (e,))
        return False


def _cg():
    """CoreGraphics 句柄（懒加载一次，拿不到返回 None）。

    ★ 读屏状态是「锦上添花」，绝不能让守护因此崩掉；也不能靠 exec 外部程序 ——
      所以用 ctypes 直接吃框架（纯标准库，无第三方依赖）。
    """
    global _CG
    if _CG is False:
        return None
    if _CG is None:
        try:
            lib = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
            lib.CGMainDisplayID.restype = ctypes.c_uint32
            lib.CGDisplayIsAsleep.restype = ctypes.c_bool
            lib.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
            lib.CGDisplayIsBuiltin.restype = ctypes.c_bool
            lib.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
            lib.CGGetActiveDisplayList.restype = ctypes.c_int32
            lib.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32,
                                                   ctypes.POINTER(ctypes.c_uint32),
                                                   ctypes.POINTER(ctypes.c_uint32)]
            _CG = lib
        except Exception:
            _CG = False
    return _CG or None


def lid_closed():
    """笔记本**合盖**了吗？True / False；读不到返回 None。

    判据：`IOPMrootDomain` 上那条 `"AppleClamshellState" = Yes/No`。

    ★ 2026-09-28 复核与更正：本机开盖时实测**一直是 No**（多次），而 20:13:53 /
      20:14:56 / 20:33:42 三次日志都认出了「合盖了」—— 当时读的就是这个属性，
      所以**判据本身是准的**。一度怀疑「disablesleep 会把这个标志位冻住」，
      那是个**误判**：22:51:06 读到 No 的那一刻，档位还是 0、盖也还没合上
      （用户 22:51:10 才点档位三），No 是对的。
    ⚠️ 但**没有在「合盖 + 档位三」状态下直接读过一次值**，所以别当已确证 ——
      真出现「合盖了却读不到」，日志会给出现场（见下面 closed is None 那条）。
    ⚠️ 读不到时**返回 None 而不是 False** —— 上层只在 `is True` 时才去熄屏，
      读不到就等于「这一段什么也不做」，绝不会因一次读失败就误判成开盖。
    """
    # ---- 首选：ctypes 直读（0.0078 ms，不起进程）2026-09-28 改 ----
    v = iokit_bool('AppleClamshellState')
    if v is not None:
        return v

    # ---- 回退：起 ioreg 子进程（IOKit 这条路过不去时才走）----
    rc, out = _run([IOREG, '-n', 'IOPMrootDomain', '-r', '-d', '1'], timeout=8)
    if rc != 0:
        return None
    m = re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)', out or '')
    if not m:
        return None
    return m.group(1) == 'Yes'


def feat_gates(batt=None):
    """「电相关」三个开关**各自**能不能点 —— 全项目唯一判据（与 Windows 版同名同义）。

    返回 `{开关名: (置灰?, 置灰的理由)}`，键就是 `_FEAT_KEYS` 里的正式名：
    `lowBattStop` / `lidClose` / `lidDisplayOn`。

    ★ 为什么要有它：三项的**门槛并不相同**，写成字面量散在界面里迟早漂移
      （界面上说"不会灰"、检测里说"灰"，用户照着错的那份去排查）。
      收成这一个函数后，界面（`wb_ui._features_payload`）只取结果。
      （Windows 版同一函数见 `wb_powerguard.feat_gates`，两端结构保持一致。）

    ★★★ 2026-10-05（用户第 8 条）新增第二条门槛：**本机已经合着盖**。
      用户原话：「你加一个事件驱动的监测，如果笔记本合盖，就自动置灰这个选项」，
      并明确「既然上一级『合盖后保持唤醒』置灰了，自然『合盖后内建显示器保持点亮』
      也置灰」。
      · 判据用 `lid_closed()` —— macOS 走 `IOPMrootDomain` 上的 `AppleClamshellState`
        （ctypes 直读，0.008 ms；读不到返回 None）。
      · ⚠️ 它**只是界面层的门槛**：已经写进系统的 `disablesleep` **照旧保留** ——
        合盖期间后台长任务绝不能因为界面变灰就被中断。这里只改"能不能点"。
      · ⚠️ `lid_closed()` 返回 None（读不到）时**不置灰**，沿用本项目
        「宁可让它可点，也别误禁」那条老原则。
      · ⚠️ 接外接屏、macOS 自己进了真·合盖模式时，`AppleClamshellState` 同样是 Yes
        ⇒ 也会置灰，正好对上「盖合上、只用外接屏」那种用法。

    ★ 「事件驱动」在两端各自的落法（不必一样）：
      · Windows：真订阅了 `GUID_LIDSWITCH_STATE_CHANGE`（见 `start_win_lid_callback`）
        —— 合盖那一刻就能翻，界面下一次轮询即见；
      · macOS：**没有**等价的廉价系统通知（合盖状态只能问 IOKit），所以走既有那条
        3 秒状态轮询 —— `wb_ui.build_power()` **刻意不缓存**（见其上方注释），
        每次都重新调本函数 ⇒ 合盖后**最迟 3 秒**自动置灰，开盖后同样自动恢复。
        `lid_closed()` 一次 0.008 ms，这点开销可以忽略。
    """
    if batt is None:
        batt = has_battery()
    no_batt = (batt is False)
    why_batt = '这台机器没有电池（台式机），这一项用不上'
    g = {'lowBattStop': (no_batt, why_batt if no_batt else ''),
         'lidClose': (no_batt, why_batt if no_batt else ''),
         'lidDisplayOn': (no_batt, why_batt if no_batt else '')}

    why_closed = ('笔记本已合盖（在用外接屏）—— 这一项此刻不适用，开盖后自动恢复可选')
    try:
        if lid_closed() is True:
            for _k in ('lidClose', 'lidDisplayOn'):
                g[_k] = (True, why_closed)
    except Exception:
        pass
    return g


def display_asleep():
    """主显示器现在是**熄屏**状态吗？True / False；读不到返回 None。"""
    lib = _cg()
    if lib is None:
        return None
    try:
        return bool(lib.CGDisplayIsAsleep(lib.CGMainDisplayID()))
    except Exception:
        return None


def external_display():
    """有没有除内建屏以外的**活动显示器**（真显示器或假负载头）？True/False/None。

    ★ 有外接屏时**不要**去强制熄屏：那种情况 macOS 会自己进真·合盖模式、把内建屏
      断电，我们插手只会把用户正在看的那块屏也黑掉。
    """
    lib = _cg()
    if lib is None:
        return None
    try:
        arr = (ctypes.c_uint32 * 8)()
        n = ctypes.c_uint32(0)
        if lib.CGGetActiveDisplayList(8, arr, ctypes.byref(n)) != 0:
            return None
        for i in range(min(n.value, 8)):
            if not lib.CGDisplayIsBuiltin(arr[i]):
                return True
        return False
    except Exception:
        return None


def display_off_now():
    """立刻熄屏（**只熄屏，不睡觉**）。返回 (ok, 原因)。"""
    if _DRY:
        log('（空跑）本应执行：pmset displaysleepnow')
        return True, ''
    rc, out = _run([PMSET, 'displaysleepnow'], timeout=15)
    if rc == 0:
        return True, ''
    return False, (out or '').strip()[:160]


# ══════════════════════════════════════════════════════════════════════════
# PommeToys 口径（2026-10-12：对 /Applications/PommeToys.app 反汇编 + 把它二进制里的
# 中英文案抠出来读了一遍；它本机装了但用户会员受限用不了，只当参考实现看待）
#
# 它的分工（从符号名读出来的）：KeepAwakeController（顶睡眠 + 时长/倒计时）、
# LidGuardCoordinator ＋ 一个**特权 helper**（LaunchDaemon `app.pommetoys.LidHelper`，
# 走 IOPMSetSystemPowerSetting 设 SleepDisabled，带 EngagePlan/RestorePlan 和持久记录
# /Library/Application Support/PommeToys/lid-helper.state）、**LidBrightnessGuard**
# （合盖时把内建屏亮度压到最低、开盖恢复）、ClamshellWatch（订阅合盖通知）。
#
# 它自己的文案（zh-Hans，直接从二进制里读出来的）：
#   · 「合盖后保持唤醒」：合盖后 Mac 不进入睡眠。请注意散热。开启期间「睡眠」菜单不可用。
#     仅在 PommeToys 运行期间有效，退出即恢复正常睡眠。
#   · 「合盖后内建显示器保持点亮」：合盖后不再关闭内建显示器；亮度自动调至最低，开盖后恢复。
#   · 「系统的睡眠设置在别处被改动，合盖后 Mac 将恢复睡眠。」
#   · 「另一个程序反复改回系统的睡眠设置，PommeToys 已停止重设。」
#   · 「机器温度过高，合盖后 Mac 将恢复睡眠。」「用电池且低于 10% 时结束，让睡眠保住电量。」
#
# 对我们最关键的三条（也正是我们那个「合盖再开盖黑屏」的土壤）：
#   ① 它**不反复按灭**内建屏：只下一次「补偿熄屏」，而且**先看有没有别的程序按着屏**
#      （displayKeepers），有人按着就不插手 —— 不对拉。
#   ② 「保持点亮」那一项它做的不是"屏别灭"，而是**亮度压到最低 + 开盖恢复**（DisplayServices）。
#   ③ 它盯着自己设的系统设置：被别处改动 / 被反复改回 / 过热 / 低电 → **收手并恢复正常睡眠**。
# ══════════════════════════════════════════════════════════════════════════
DS_FRAMEWORK = '/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices'
CG_FRAMEWORK = '/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics'
_DS_CG = [None]
_KEEPER_CACHE = {'at': 0.0, 'v': []}
_THERM_CACHE = {'at': 0.0, 'v': None}
# 「合盖不睡」被谁弄停的（照 PommeToys 的两条文案）：'' / 'gate'（别处改了设置）/ 'overheat'
_GATE = {'stopped': ''}


def _dscg():
    """按需加载 DisplayServices + CoreGraphics（私有框架，取不到就返回 (None, None)）。"""
    if _DS_CG[0] is None:
        try:
            import ctypes
            ds = ctypes.CDLL(DS_FRAMEWORK)
            cg = ctypes.CDLL(CG_FRAMEWORK)
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32,
                                                  ctypes.POINTER(ctypes.c_uint32),
                                                  ctypes.POINTER(ctypes.c_uint32)]
            cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
            ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32,
                                                        ctypes.POINTER(ctypes.c_float)]
            ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
            _DS_CG[0] = (ds, cg)
        except Exception:
            _DS_CG[0] = False
    return (None, None) if _DS_CG[0] is False else _DS_CG[0]


def builtin_display_id():
    """内建屏的 display id；没有内建屏/读不到 → None。"""
    _ds, cg = _dscg()
    if cg is None:
        return None
    try:
        import ctypes
        arr = (ctypes.c_uint32 * 8)()
        n = ctypes.c_uint32(0)
        if cg.CGGetActiveDisplayList(8, arr, ctypes.byref(n)) != 0:
            return None
        for i in range(min(int(n.value), 8)):
            if cg.CGDisplayIsBuiltin(arr[i]):
                return int(arr[i])
    except Exception:
        return None
    return None


def display_brightness():
    """当前内建屏亮度 0.0~1.0；读不到 → None。"""
    ds, cg = _dscg()
    if ds is None:
        return None
    try:
        import ctypes
        did = builtin_display_id()
        if did is None:
            did = int(cg.CGMainDisplayID())
        v = ctypes.c_float(0)
        if ds.DisplayServicesGetBrightness(ctypes.c_uint32(did), ctypes.byref(v)) != 0:
            return None
        return float(v.value)
    except Exception:
        return None


def set_brightness(value):
    """把内建屏亮度设成 value（0.0~1.0）。成功 True。"""
    ds, cg = _dscg()
    if ds is None:
        return False
    try:
        import ctypes
        did = builtin_display_id()
        if did is None:
            did = int(cg.CGMainDisplayID())
        return ds.DisplayServicesSetBrightness(ctypes.c_uint32(did),
                                               ctypes.c_float(float(value))) == 0
    except Exception:
        return False


def display_keepers(me_pid=None, ttl=5.0):
    """除我们自己之外，**谁还按着「显示器别睡」**？（对应 PommeToys 的 displayKeepers）

    返回 [(pid, 进程名), ...]。有别人按着，我们就不去按灭屏 —— 照它的口径不对拉。
    """
    now = time.time()
    if now - _KEEPER_CACHE['at'] < ttl:
        return _KEEPER_CACHE['v']
    me = int(me_pid or os.getpid())
    out = []
    try:
        rc, txt = _run([PMSET, '-g', 'assertions'], timeout=10)
        for line in (txt or '').splitlines():
            if ('NoDisplaySleepAssertion' not in line
                    and 'PreventUserIdleDisplaySleep' not in line):
                continue
            m = re.match(r'\s*pid\s+(\d+)\(([^)]*)\)', line)
            if not m:
                continue
            try:
                pid = int(m.group(1))
            except ValueError:
                continue
            if pid == me:
                continue                    # 我们自己持有的不算
            nm = m.group(2)
            mm = re.search(r'named:\s*"([^"]*)"', line)
            if mm and mm.group(1):
                nm = '%s/%s' % (nm, mm.group(1))
            out.append((pid, nm or '?'))
    except Exception:
        out = []
    _KEEPER_CACHE['at'] = now
    _KEEPER_CACHE['v'] = out
    return out


def thermal_state(ttl=60.0):
    """热量状态：0 正常 / 1 偏热 / 2 过热 / 3 极热；读不到 → None。

    走 JXA 问 `NSProcessInfo.thermalState`（PommeToys 用的同一个读数），
    不需要 root、也不起常驻进程。照它的口径：过热就收手、恢复睡眠。
    """
    now = time.time()
    if _THERM_CACHE['v'] is not None and now - _THERM_CACHE['at'] < ttl:
        return _THERM_CACHE['v']
    v = None
    try:
        rc, out = _run([OSASCRIPT, '-l', 'JavaScript', '-e',
                        'ObjC.import("Foundation");'
                        ' $.NSProcessInfo.processInfo.thermalState + ""'], timeout=10)
        s = (out or '').strip()
        if rc == 0 and s.isdigit():
            v = int(s)
    except Exception:
        v = None
    _THERM_CACHE['at'] = now
    _THERM_CACHE['v'] = v
    return v


def _restore_brightness_after_lid(ctx, log):
    """开盖：把「保持点亮」那一项压下去的亮度恢复（照 PommeToys 的 LidBrightnessGuard）。"""
    if not ctx.get('dimmed'):
        return
    want = ctx.get('brightness_saved')
    ok = False if want is None else set_brightness(want)
    ctx['dimmed'] = False
    ctx['brightness_saved'] = None
    if ok:
        log('开盖：内建屏亮度已恢复成 %s' % round(want, 3))
    else:
        log('开盖：内建屏亮度没恢复成（DisplayServices 不可用）—— 手动按一下亮度键即可')


def lid_screen_guard(ctx):
    """档位三专用：**合盖后把内建屏熄掉，但不动系统睡眠**（后台照跑）。

    ctx 是守护内存里的一个小字典，用来记住「上一片看到了什么」：
      lid      上一片看到的合盖状态（靠它的变化识别「合盖 / 开盖」这一刻）
      ext      这一段合盖里探到的「有没有外接屏」（合盖状态一变就重探）
      off_at   上次下发熄屏的时刻（限流）
      off_n    这一段合盖里下发了几次（前几次按 GAP，之后退让成 BACKOFF）
      fail_n   连续失败次数（只在头几次记日志，不刷屏）
    """
    closed = lid_closed()
    was = ctx.get('lid')
    if closed != was:
        ctx['lid'] = closed
        ctx['ext'] = None
        ctx['off_at'] = 0.0
        ctx['off_n'] = 0
        ctx['fail_n'] = 0
        ctx['compensated'] = False     # ★ 新的一段合盖 → 允许再下一次「补偿熄屏」
        ctx['keeperLogged'] = False
        ctx['gateChecked'] = False     # 每段合盖都重新核一次「合盖不睡还在不在」+ 温度
        ctx['hotLogged'] = False
        ctx['displayOnLogged'] = False  # 「保持点亮」那句日志，每段合盖只说一次
        if closed is True:
            log('合盖了（合盖后保持唤醒）：系统不睡，接下来把内建屏熄掉（后台照跑）')
        elif closed is False and was is True:
            # ★★ 2026-10-12 修（用户报「合盖后再打开 → 黑屏 + 鼠标转圈」的恶性 bug）：
            #   这里原来只写「不再强制熄屏」，而**没有任何一句去把屏叫回来**。
            #   档位三开着 `pmset -a disablesleep 1`，合盖**不是睡眠**，只是我们自己
            #   `displaysleepnow` 把屏按灭了 —— 系统因此不会走「唤醒显示」那套
            #   （它压根不认为睡过）。于是开盖后屏留在灭的状态：WindowServer 还活着、
            #   还能画鼠标（所以是「黑屏 + 一个转圈光标」），画面出不来、前台全像卡死。
            #   必须主动发一次用户活动把屏点亮，见 _resume_display()。
            log('开盖了（合盖后保持唤醒）：正在把屏幕叫回来…')
            if _resume_display(log):
                _mark_lid_screen(False)
            _restore_brightness_after_lid(ctx, log)
        elif closed is None:
            # 只记一次（连续多轮都是 None 时不会再进这个分支）—— 目的是「不再静默失败」
            log('合盖状态读不到（ioreg 没给出 AppleClamshellState）→ 这一段不强制熄屏。'
                '若你正合着盖而屏幕还亮着，把这行连同 `--status --full` 一起反馈')
    if closed is not True:
        return
    # ★★ 2026-10-12 照 PommeToys：**在合盖这一刻**核对两件事，都得能「收手」——
    #   ① 我们设的「合盖不睡」还在不在？被别处改动 → 如实报出来，**并且不再往下按屏**
    #      （机器既然会正常睡眠，我们再强按屏只会把它按进一个没人管的熄灭状态，
    #       那正是黑屏 bug 的土壤）。它的原话：
    #       「系统的睡眠设置在别处被改动，合盖后 Mac 将恢复睡眠。」
    #      ⚠️ 只读一次、**绝不写回** —— 用户 2026-09-29 明确点名删掉了「每轮自愈重写」那段，
    #         所以这里只做「知情 + 不添乱」，不跟别的程序对拉。
    #   ② 机器是不是过热？→ 收手（照它：「机器温度过高，合盖后 Mac 将恢复睡眠」）。
    if not ctx.get('gateChecked'):
        ctx['gateChecked'] = True
        if lid_read() == 0:
            _GATE['stopped'] = 'gate'
            log('系统的睡眠设置在别处被改动（SleepDisabled=0）→ 照 PommeToys：'
                '合盖后 Mac 将恢复睡眠；本次不再强制熄屏。想再设回去，'
                '重新点一下「合盖后保持唤醒」即可。')
            return
    _t = thermal_state(ttl=300)
    if _t is not None and _t >= 2:
        if not ctx.get('hotLogged'):
            ctx['hotLogged'] = True
            _GATE['stopped'] = 'overheat'
            lid_restore_conf(quiet=True)
            log('机器温度过高（thermalState=%d）→ 照 PommeToys：合盖后 Mac 将恢复睡眠'
                '（已把「合盖睡眠」恢复原值）' % _t)
        return
    # ★★ 2026-10-12 照 PommeToys 重做「合盖后内建显示器保持点亮」：
    #   它这一项做的**不是「屏别灭」这么粗**，而是「不再关闭内建显示器；**亮度自动调至
    #   最低**，开盖后恢复」（它的 LidBrightnessGuard + DisplayServices 私有框架）。
    #   我们原来只做了一半（不熄屏、亮度不变 → 合盖塞包里一直全亮，烫）。
    #   现在照它：记住原亮度 → 压到最低 → 开盖恢复。取不到 DisplayServices 就退回老行为并记日志。
    if ctx.get('displayOn'):
        if not ctx.get('displayOnLogged'):
            ctx['displayOnLogged'] = True
            b0 = display_brightness()
            if b0 is not None and not ctx.get('dimmed') and set_brightness(LID_DIM_BRIGHTNESS):
                ctx['dimmed'] = True
                ctx['brightness_saved'] = b0
                log('合盖后内建显示器保持点亮（照 PommeToys）：不关屏，亮度 %s → 最低 %s（开盖恢复）'
                    % (round(b0, 3), LID_DIM_BRIGHTNESS))
            else:
                log('合盖后内建显示器保持点亮（照 PommeToys）：不关屏 —— 请注意散热')
        ctx['off_n'] = 0
        return
    if ctx.get('ext') is None:
        ext = external_display()
        if ext is None:
            # 读不到（守护这棵树里 CoreGraphics 有时拿不到）→ 按「没有外接屏」处理。
            ext = False
            log('读不到外接屏信息（CoreGraphics 不可用）→ 按「没有外接屏」处理，照常熄屏')
        ctx['ext'] = ext
        if ext:
            log('合盖后检测到外接显示器/假负载头 → 交给系统自己管，工具不抢着熄屏')
    if ctx['ext']:
        return

    # ★★ 照 PommeToys：**有别的程序按着屏就不插手**（它的 displayKeepers，日志原话
    #    "display left lit at a holder's request"）。别人正拿显示断言顶着，我们去按灭只会
    #    两边对拉 —— 它退让，我们也退让，并如实说清是谁按着。
    keepers = display_keepers()
    if keepers:
        if not ctx.get('keeperLogged'):
            ctx['keeperLogged'] = True
            log('合盖熄屏：另有程序按着显示器（%s）→ 照 PommeToys 口径**不插手**，屏留给它'
                '（不影响后台任务）'
                % '、'.join('%s(pid %d)' % (n, p) for p, n in keepers[:3]))
        return

    # ★★ 照 PommeToys：**只下一次「补偿熄屏」，之后不再反复压**。
    #    我们原来是「连压 8 次 + 每 8/45 秒继续压」—— 那正是「开盖叫不回来」的土壤：
    #    屏被我们按进了一个**系统不知道的熄灭状态**，而反复按并不会让它更黑，只会跟别人对拉。
    #    它那边只补偿一次（compensating sleep），然后站住不动。我们保留「头两下」（合盖瞬间
    #    系统会「熄→亮→熄」抖一下，需要压平），但**到此为止**，不再按 8 秒/45 秒反复压。
    now = time.time()
    if not ctx.get('compensated'):
        hit = 0
        for i in range(LID_COMPENSATE_PRESSES):
            ok, err = display_off_now()
            ctx['off_at'] = time.time()
            if ok:
                hit += 1
            else:
                ctx['fail_n'] = ctx.get('fail_n', 0) + 1
                if ctx['fail_n'] == 1:
                    log('合盖熄屏没成功：%s' % err)
            if i + 1 < LID_COMPENSATE_PRESSES:
                time.sleep(LID_SCREEN_BURST_GAP)
            if i >= 1 and display_asleep() is True:
                break
        ctx['compensated'] = True
        if hit:
            _mark_lid_screen(True)
        asleep = display_asleep()
        log('合盖熄屏：下了 %d 次「补偿熄屏」（照 PommeToys：只补偿、不反复压）（现在%s）'
            % (hit, '屏已熄' if asleep is True
               else ('屏还亮着' if asleep is False else '读不到屏状态，按已下发算')))
        return

    # 补偿之后：**只在「屏又被点亮、且没人按着」时补一次**，间隔拉到 5 分钟。
    #   PommeToys 干脆不补；我们留这一手只为对付合盖瞬间系统自身那次 DarkWake 抖动，
    #   代价是每 5 分钟最多 1 次 —— 不再是原来的 8 秒一次 / 被顶开就 45 秒一次。
    if ctx.get('off_at') and now - ctx['off_at'] < LID_SCREEN_RECHECK:
        return
    if display_asleep() is True:          # 已经熄着 —— 不用再点
        return
    ok, err = display_off_now()
    ctx['off_at'] = now
    if ok:
        ctx['off_n'] = ctx.get('off_n', 0) + 1
        _mark_lid_screen(True)
        log('合盖熄屏：屏又被点亮了（没人按着）→ 再补一次（每 %d 秒至多一次）'
            % LID_SCREEN_RECHECK)
    else:
        ctx['fail_n'] = ctx.get('fail_n', 0) + 1
        if ctx['fail_n'] in (1, 3):
            log('合盖熄屏没成功：%s' % err)


def _sleep_loop(sec, tick_fn=None, wake=None, step=None):
    """睡 sec 秒。三种用法：

      · 都不给              → 就是 `time.sleep(sec)`
      · 给了 tick_fn        → 切成 step（默认 LID_SCREEN_TICK）的小片，每片后调一次
      · 再给了 wake         → 每片改成 `wake.wait(片长)`：系统推来合盖事件时
                              **立刻返回**，不用等满这一片

    ★ 只改「怎么睡」，不改主循环的判定节奏（probe 仍是一轮一次）。
    ★ 2026-09-28：加 wake —— 这是「和 PommeToys 一样靠事件驱动」的落地处。
      PommeToys 的合盖响应是**事件推**出来的；我们现在是「事件推 + 低频兜底」：
      事件把这一觉叫醒 → 主循环立刻跑一轮 → `lid_screen_guard` 读到合盖 → 熄屏。
      兜底那一路保证「万一事件没来」也不会瞎。

    ★★ 2026-09-29 修一个**语义 bug**（实测踩到，症状很误导人）：
      原来 `wake.wait(piece)` 提前返回后，只把**这一片**提前结束，`while` 接着
      把剩下的时间睡满 —— 于是「靠事件叫醒主循环」这件事**根本没发生**。
      对合盖熄屏看不出来（因为 `tick_fn` 当场就跑了一次），但对
      「点开关 / 点停止 / 点取消要秒级生效」是致命的：SIGUSR1 发得再准，
      守护也要等这一觉**自然睡满**才动。实测：点一下开关 → 8 秒后才生效，
      而 8 秒正好是「上一轮结束 + nap」的余数，跟信号半点关系没有。
      现在：`wake.wait()` 返回 True（＝有人 set 了）就**结束整觉直接回去**，
      主循环立刻重读配置与标记。
    """
    if tick_fn is None and wake is None:
        time.sleep(sec)
        return
    end = time.time() + sec
    piece_len = step if step else (LID_SCREEN_TICK if tick_fn else sec)
    while True:
        left = end - time.time()
        if left <= 0:
            return
        piece = min(piece_len, left)
        fired = False
        if wake is not None:
            try:
                fired = bool(wake.wait(piece))
                wake.clear()          # 先清掉，免得下一轮 wait 又立刻返回（空转）
            except Exception:
                time.sleep(piece)
        else:
            time.sleep(piece)
        if tick_fn is not None:
            try:
                tick_fn()
            except Exception as e:
                log('合盖熄屏看门狗出错（忽略这一片）：%r' % (e,))
        if fired:
            # 有人专门把我们叫醒 → 这一觉别睡了，回主循环重读配置与标记
            #（那里才知道要熄屏还是要换抑制、还是要退出）。
            urgent = _CLI_POKE[0]
            _CLI_POKE[0] = False
            _now = time.time()
            if urgent or (_now - _LAST_WAKE_AT[0] >= _WAKE_MIN_GAP):
                _LAST_WAKE_AT[0] = _now
                return
            # 自动事件太密 → 这一轮不早退，按普通切片继续睡（下一片还会再看一次）


# ======================================================== 平台实现层：判忙

def wb_running():
    """WorkBuddy 主进程在不在跑。macOS 上要按全路径匹配 ——
    进程名是 "Electron"，`pgrep -x WorkBuddy` 永远匹配不到。"""
    if IS_MAC:
        rc, _ = _run(['/usr/bin/pgrep', '-f', '/Applications/WorkBuddy.app/Contents/MacOS/Electron'],
                     timeout=8)
        return rc == 0
    if IS_WIN:
        rc, out = _run(['tasklist', '/FI', 'IMAGENAME eq WorkBuddy.exe'], timeout=10)
        return 'WorkBuddy.exe' in (out or '')
    return False


def scan_projects_mtime(wb_home, max_depth=2):
    """扫 ~/.workbuddy/projects/*/*.jsonl 的最近修改时间。

    返回 (最新文件年龄秒 或 None, 文件数)。
    ★ 与 Win 版同实现：它回答的是「这一轮对话还在不在写」，
      是「正在跑」最直接的痕迹（实测干活时每 10~30 秒写一次）。
    """
    root = os.path.join(wb_home, 'projects')
    if not os.path.isdir(root):
        return None, 0
    newest = 0.0
    n = 0
    try:
        for d1 in os.listdir(root):
            p1 = os.path.join(root, d1)
            if not os.path.isdir(p1):
                continue
            try:
                subs = os.listdir(p1)
            except Exception:
                continue
            for f in subs:
                if not f.endswith('.jsonl'):
                    continue
                p2 = os.path.join(p1, f)
                try:
                    mt = os.path.getmtime(p2)
                except Exception:
                    continue
                n += 1
                if mt > newest:
                    newest = mt
        if newest <= 0:
            return None, n
        return max(0.0, time.time() - newest), n
    except Exception:
        return None, n


# ══════════════════════════════════════════ 「任务在跑」的事件源（2026-10-01 加）
#
# ★ 为什么加：见 TASK_IDLE_SEC 上面那段长注释。一句话 —— 主循环原来每 15 秒
#   醒一次去「问」有没有任务在跑；现在改成盯着那个会变的文件，**有动静才醒**。
#
# ⚠️ 三个必须注意的坑（都是别的模块踩过、这里会原样踩一遍的）：
#   ① **「盯内容」只能盯文件，不能盯目录** —— kqueue 的目录事件只覆盖
#      「目录项变化（新建/删除/改名）」，**文件内容被追加是不触发的**。
#      ⚠️ 但反过来，「**发现新文件**」这件事只有目录能盯 —— 见下面 ③。
#      （这与 pdf_daemon 盯「队列目录」同理：那里要的正是一个个新出现的 .job。）
#   ② **WAL 会被 SQLite checkpoint 删掉重建** → inode 换掉，老 fd 就此失效。
#      必须处理 NOTE_DELETE / NOTE_RENAME 并**重挂**（同外壳 sudoWatch 的教训）。
#   ③ **新开的会话文件要能挂上** —— 原来靠 `TASK_REARM_SEC = 60 秒` 定期重挂，
#      **那是一条真正的定时器（一天 1440 次）**。
#      ★★ 2026-10-01（第五十七轮 · 用户第 3 条）改成**事件驱动**：
#         把 `projects/` 及其下各 slug 目录也挂上 kqueue，
#         目录里一有新文件出现，内核立刻叫醒我们 → 重挂 → 新文件被纳入。
#         于是「什么时候重挂」不再由时间决定，而由「**真的开了新会话**」决定。
_TASK_EVENT = {'ok': None, 'why': '', 'count': 0, 'lastAt': 0.0, 'rearm': 0}
TASK_WATCH_MAX_FILES = 8       # 最多盯几个会话文件（别把 100 个 fd 全挂上）
TASK_WATCH_MAX_DIRS = 64       # 最多盯几个目录（2026-10-01：本机实测只有 23 个）


def task_watch_files(wb_home=None, limit=TASK_WATCH_MAX_FILES):
    """要盯的文件清单（**顺序即重要性**）：

      1. `workbuddy.db-wal` —— 权威信号：`sessions.status` 的每次改写都落在这儿，
         「开始跑」与「跑完了」两端都在这里，所以结束也是事件。
      2. `workbuddy.db`     —— 没有 WAL（非 WAL 模式 / 刚 checkpoint 完）时的退路。
      3. 最近活跃的若干个会话 `.jsonl` —— 长任务期间的那种「高频小写入」补充信号。
    """
    H = wb_home or wb_home_of() or os.path.expanduser('~/.workbuddy')
    out = []
    for n in ('workbuddy.db-wal', 'workbuddy.db'):
        p = os.path.join(H, n)
        if os.path.exists(p):
            out.append(p)
    cand = []
    try:
        root = os.path.join(H, 'projects')
        now = time.time()
        for d1 in os.listdir(root):
            p1 = os.path.join(root, d1)
            if not os.path.isdir(p1):
                continue
            for f in os.listdir(p1):
                if not f.endswith('.jsonl'):
                    continue
                p2 = os.path.join(p1, f)
                try:
                    mt = os.path.getmtime(p2)
                except Exception:
                    continue
                if now - mt <= 7200:          # 只挂「最近 2 小时写过的」
                    cand.append((mt, p2))
    except Exception:
        pass
    cand.sort(reverse=True)
    out.extend(p for _, p in cand[:max(0, limit)])
    return out


def task_watch_dirs(wb_home=None, limit=TASK_WATCH_MAX_DIRS):
    """要盯的**目录**清单 —— 只为一件事：**发现新出现的会话文件**。

    ★ 为什么需要（第五十七轮）：`task_watch_files()` 给的是一份**固定快照**
      （db-wal ＋ 当时最近 2 小时写过的 8 个会话）。用户新开一个会话时，
      新文件不在那份快照里，它的写入就**盯不到** —— 原来靠每 `TASK_REARM_SEC`
      （60 秒）重挂一次来弥补，而那是一条真正的定时器。
      现在改成：目录里一有新文件出现 → 内核叫醒 → 重挂 → 新文件进清单。

    盯两层：
      · `projects/`         —— 新工作区（slug）目录出现
      · `projects/<slug>/`  —— 新会话 `.jsonl` 出现（最常发生的一种）

    ⚠️ **刻意不盯 `~/.workbuddy` 根目录**：那里太活跃（日志、db-wal、临时文件
      一分钟能变好几次），挂上它等于把刚省掉的定时器换个形式加回来。
      根目录里真正关心的 `workbuddy.db-wal` / `workbuddy.db` 已经在
      **文件级**被盯住了 —— 文件级比目录级精确得多。
    """
    H = wb_home or wb_home_of() or os.path.expanduser('~/.workbuddy')
    out = []
    root = os.path.join(H, 'projects')
    if not os.path.isdir(root):
        return out
    out.append(root)                    # ① 新工作区（slug）目录出现
    # ② 各工作区目录（新会话 .jsonl 出现）—— 这是最常发生的一种
    #
    # ★★ 判据**故意不按活跃度筛**（第一版按「目录自身的 mtime」筛，是错的，实测只挂到
    #    1 个目录）：目录的 mtime 只在**目录项变化**（新建 / 删除 / 改名）时更新，
    #    往一个已存在的会话文件里追加内容**根本不会动它** —— 于是最活跃的那个工作区
    #    反倒会被判成「不活跃」而漏挂，恰恰丢掉最该盯的那个。
    #    改用「目录下最新 .jsonl 的 mtime」也可以修好，但实测本机一共只有 23 个
    #    工作区目录，全挂上不过 23 个 fd，还省掉一次全目录扫描 —— 直接全挂更简单。
    try:
        subs = [d for d in os.listdir(root)
                if os.path.isdir(os.path.join(root, d))]
    except Exception:
        subs = []
    subs.sort()
    out.extend(os.path.join(root, d) for d in subs[:max(0, limit)])
    return out


def task_event_state():
    """给 `--status` / 自检用：(ok, why, count, lastAt)。"""
    return (_TASK_EVENT['ok'], _TASK_EVENT['why'],
            _TASK_EVENT['count'], _TASK_EVENT['lastAt'])


def _task_watch_loop():
    """挂上文件事件 → 阻塞等 → 有动静就 `_LID_WAKE.set()` 叫醒主循环。

    ★ 主循环**只等一个 Event**（`_LID_WAKE`）—— 合盖、电源、SIGUSR1 都往它 set，
      这里复用同一个（见 `_LID_WAKE` 定义处的说明）。不要新开第二个 Event，
      否则 `_sleep_loop(wake=...)` 只认一个，另一个的唤醒会被吞掉。
    """
    import select as _sel

    paths = task_watch_files()
    dirs = task_watch_dirs()
    if not paths and not dirs:
        _TASK_EVENT.update({'ok': False,
                            'why': '一个可盯的文件都没有（WorkBuddy 还没建库？）'})
        _LID_WAKE.set()
        time.sleep(TASK_IDLE_SEC)
        return

    ff = (_sel.KQ_NOTE_WRITE | _sel.KQ_NOTE_EXTEND | _sel.KQ_NOTE_DELETE
          | _sel.KQ_NOTE_RENAME | _sel.KQ_NOTE_ATTRIB)
    kq = _sel.kqueue()
    fds = []
    dir_fds = set()          # ★ 目录 fd：命中它 = 「有新文件出现」→ 重挂
    n_file = 0
    n_dir = 0
    evs = []
    try:
        for p in paths:
            try:
                # ★ 用 O_RDONLY 而不是 O_EVTONLY：`os.O_EVTONLY` 在部分 Python
                #   构建里**根本没有这个常量**（实测系统 3.9 就 AttributeError）；
                #   pdf_daemon 一直用 O_RDONLY，跨版本都稳（这些文件都是可读的）。
                fd = os.open(p, os.O_RDONLY)
            except Exception:
                continue
            fds.append(fd)
            n_file += 1
            evs.append(_sel.kevent(fd, filter=_sel.KQ_FILTER_VNODE,
                                   flags=(_sel.KQ_EV_ADD | _sel.KQ_EV_ENABLE
                                          | _sel.KQ_EV_CLEAR),
                                   fflags=ff))
        # ★★ 第五十七轮（用户第 3 条）：目录也挂上 —— **只为发现新会话文件**。
        #   分工：**内容**靠上面那些文件 fd（目录事件看不到内容追加），
        #        **新文件**只有目录看得见。两路合起来，就不需要「每 60 秒重挂」了。
        for d in dirs:
            try:
                fd = os.open(d, os.O_RDONLY)
            except Exception:
                continue
            fds.append(fd)
            dir_fds.add(fd)
            n_dir += 1
            evs.append(_sel.kevent(fd, filter=_sel.KQ_FILTER_VNODE,
                                   flags=(_sel.KQ_EV_ADD | _sel.KQ_EV_ENABLE
                                          | _sel.KQ_EV_CLEAR),
                                   fflags=ff))
        if not evs:
            _TASK_EVENT.update({'ok': False, 'why': '文件一个都打不开（权限 / 已删除）'})
            _LID_WAKE.set()
            time.sleep(TASK_IDLE_SEC)
            return
        # ★ 注册与等待必须**分两段**（学 pdf_daemon）：注册这一次只投递变更
        #   （timeout=0、nodeliver），之后循环里一律 `control(None, …)` 只等事件。
        #   绝不能把同一份 changelist 反复传进去 —— 那等于每轮重新 KQ_EV_ADD 一次。
        kq.control(evs, 0, 0)

        first = (_TASK_EVENT['ok'] is not True)
        _TASK_EVENT.update({'ok': True,
                            'why': '盯着 %d 个文件 + %d 个目录（db-wal＋活跃会话＋会话目录）'
                                   % (n_file, n_dir)})
        if first:
            log('任务事件监听已就位：%s —— 空闲时不再按秒唤醒' % _TASK_EVENT['why'])

        while True:
            got = kq.control(None, 64, TASK_IDLE_SEC)
            if not got:
                # 超时 = 兜底保险丝（TASK_IDLE_SEC）—— 也叫醒一次主循环。
                # ⚠️ 这是**保险丝**，不是「每 60 秒重挂」那种定时器：
                #    正常情况下一整夜都走不到这里（任务起止都各有事件）。
                _LID_WAKE.set()
                return
            _TASK_EVENT['count'] += len(got)
            _TASK_EVENT['lastAt'] = time.time()
            _LID_WAKE.set()                      # ★ 叫醒主循环：可能刚开跑 / 刚跑完
            for e in got:
                fl = getattr(e, 'fflags', 0)
                if fl & (_sel.KQ_NOTE_DELETE | _sel.KQ_NOTE_RENAME):
                    return                       # inode 换了 → 必须重挂
                # ★★ 目录里多了/少了东西（多半是新开了一个会话）→ 重挂，
                #    把新文件纳入清单。**这一条取代了原来的 60 秒定期重挂。**
                if e.ident in dir_fds and (fl & _sel.KQ_NOTE_WRITE):
                    _TASK_EVENT['rearm'] = _TASK_EVENT.get('rearm', 0) + 1
                    return
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            kq.close()
        except Exception:
            pass


def start_task_event_watch(quiet=False):
    """起一个后台线程盯「任务在跑」的事件源。返回能不能用。"""
    try:
        import select as _sel
        if not hasattr(_sel, 'kqueue'):
            raise RuntimeError('没有 kqueue（不是 macOS）')
    except Exception as e:
        _TASK_EVENT.update({'ok': False, 'why': '不可用：%r' % (e,)})
        if not quiet:
            log('任务事件监听起不来（%r）→ 退回低频兜底，功能不受影响' % (e,))
        return False

    def _run():
        while True:
            try:
                _task_watch_loop()
            except Exception as e:
                _TASK_EVENT.update({'ok': False, 'why': '监听异常：%r' % (e,)})
                log('任务事件监听异常（10 秒后重挂）：%r' % (e,))
                time.sleep(10)

    threading.Thread(target=_run, name='wb-task-watch', daemon=True).start()
    return True


def probe_wb_tasks(conf, wb_home):
    """读 WorkBuddy 自己的会话状态。返回 (working, running, ok, why)。

    ★ 这是最权威的一路信号：`sessions.status='working'` 由应用在每轮对话的
      边界改写，比「猜文件有没有在写」准得多。
    窗口 `wbWorkingRecentSec`（默认 1 小时）**不是**用来让灯面早点变
      「已停止运行」的 —— 它唯一的任务是**排除僵尸行**（被强杀后 status 卡在
      working）。灯面的翻转靠 status 本身。
    """
    if not conf.get('useWbDbSignal', True):
        return 0, 0, False, '配置里关掉了（useWbDbSignal=false）'
    if not wb_home:
        return 0, 0, False, '不知道 ~/.workbuddy 在哪'
    db = os.path.join(wb_home, 'workbuddy.db')
    if not os.path.exists(db):
        return 0, 0, False, '找不到 workbuddy.db'
    recent = _cfg_num(conf, 'wbWorkingRecentSec', 3600)
    now_ms = int(time.time() * 1000)

    def _cnt(c, win_sec):
        return c.execute(
            "select count(*) from sessions"
            " where status='working' and coalesce(deleted_at, 0)=0 and updated_at >= ?",
            (now_ms - int(max(1.0, win_sec) * 1000),)).fetchone()[0]

    try:
        uri = 'file:///' + urllib.parse.quote(db.replace('\\', '/'), safe='/:')
        con = sqlite3.connect(uri + '?mode=ro', uri=True, timeout=2.0)
        try:
            c = con.cursor()
            working = _cnt(c, recent)
            try:
                running = c.execute(
                    'select count(*) from automation_runtime_state'
                    ' where running=1').fetchone()[0]
            except Exception:
                running = 0        # 这个表不一定存在（版本差异），不算错
            return int(working or 0), int(running or 0), True, ''
        finally:
            con.close()
    except Exception as e:
        return 0, 0, False, '%s: %s' % (type(e).__name__, e)


NOISE_PROC = {'grep', 'pgrep', 'ps', 'sh', 'bash', 'zsh', 'login', 'sudo',
              'caffeinate', 'osascript', 'python3', 'python',
              # macOS 自己拉起来写 os_log 的小工具（`/usr/bin/log`）——
              # WorkBuddy 只要往系统日志写东西就会带上它，不代表团伙在干活
              'log'}

# WorkBuddy（Electron）自己的常驻 helper：它们「一直都在」，不代表团伙在干活。
# ★ 2026-09-27 实测：这份名单是**必要**的 —— WorkBuddy 常年挂着
#   sandbox-cli-gc / sandbox-center / crashpad_handler，若把它们算作「起了工具子进程」，
#   判忙会一直被钉死成 True，电脑反而永远不睡（与需求正好相反）。
WB_IDLE_HELPERS = {
    'chrome_crashpad_handler', 'sandbox-cli-gc', 'sandbox-center',
    'sandbox-cli', 'editor_sdk', 'zsh-macos',
}


# ---------------------------------------------- 进程枚举（libproc 优先）

# ★ 为什么不用 `ps`
#   本来用 `ps -Ao pid=,ppid=,comm=,command=`，2026-09-27 在 Mac 上踩到一个坑：
#   通过 WorkBuddy 自带的工具跑 `ps` 会被它的沙盒直接拒掉（`Operation not permitted`），
#   连 `dangerouslyDisableSandbox` 都救不回来 —— 因为拦截发生在 **exec** 这一步。
#   `ps` 一旦探不到，「起了工具子进程」这一路信号就静默失踪：不报错、只是永远不忙。
#   于是改成**直接问内核**：libproc 的 proc_listpids / proc_pidinfo / proc_pidpath
#   走的是系统调用，不 exec 任何东西，沙盒内外都能用，而且比 `ps` 快（一次扫全表 ~30ms）。
#   `ps` 保留为兜底（比如将来别的机器上 libproc 结构对不上）。

class _BsdInfo(ctypes.Structure):
    """proc_bsdinfo（libproc.h）—— 只要 pid / ppid / comm，其余按尺寸占位。"""
    _fields_ = [('pbi_flags', ctypes.c_uint32), ('pbi_status', ctypes.c_uint32),
                ('pbi_xstatus', ctypes.c_uint32), ('pbi_pid', ctypes.c_uint32),
                ('pbi_ppid', ctypes.c_uint32), ('pbi_uid', ctypes.c_uint32),
                ('pbi_gid', ctypes.c_uint32), ('pbi_ruid', ctypes.c_uint32),
                ('pbi_rgid', ctypes.c_uint32), ('pbi_svuid', ctypes.c_uint32),
                ('pbi_svgid', ctypes.c_uint32), ('rfu_1', ctypes.c_uint32),
                ('pbi_comm', ctypes.c_char * 16), ('pbi_name', ctypes.c_char * 32),
                ('pbi_nfiles', ctypes.c_uint32), ('pbi_pgid', ctypes.c_uint32),
                ('pbi_pjobc', ctypes.c_uint32), ('e_tdev', ctypes.c_uint32),
                ('e_tpgid', ctypes.c_uint32), ('pbi_nice', ctypes.c_int32),
                ('pbi_start_tvsec', ctypes.c_uint64), ('pbi_start_tvusec', ctypes.c_uint64)]


class _TaskInfo(ctypes.Structure):
    """proc_taskinfo（libproc.h）—— 前两个 u64 是 CPU 累计纳秒，用来算「烧不烧 CPU」。"""
    _fields_ = [('pti_virtual_size', ctypes.c_uint64), ('pti_resident_size', ctypes.c_uint64),
                ('pti_total_user', ctypes.c_uint64), ('pti_total_system', ctypes.c_uint64),
                ('pti_threads_user', ctypes.c_uint64), ('pti_threads_system', ctypes.c_uint64),
                ('pti_policy', ctypes.c_int32), ('pti_faults', ctypes.c_int32),
                ('pti_pageins', ctypes.c_int32), ('pti_cow_faults', ctypes.c_int32),
                ('pti_messages_sent', ctypes.c_int32), ('pti_messages_received', ctypes.c_int32),
                ('pti_syscalls_mach', ctypes.c_int32), ('pti_syscalls_unix', ctypes.c_int32),
                ('pti_csw', ctypes.c_int32), ('pti_threadnum', ctypes.c_int32),
                ('pti_numrunning', ctypes.c_int32), ('pti_priority', ctypes.c_int32)]


PROC_ALL_PIDS = 1
PROC_PIDTBSDINFO = 3
PROC_PIDTASKINFO = 4

_LIBC = None
_LIBC_TRIED = False


def _libc():
    """惰性加载 libSystem 并把三个函数的签名钉死；不可用就返回 None。"""
    global _LIBC, _LIBC_TRIED
    if _LIBC_TRIED:
        return _LIBC
    _LIBC_TRIED = True
    if not IS_MAC:
        return None
    try:
        lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        lib.proc_listpids.restype = ctypes.c_int
        lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidinfo.restype = ctypes.c_int
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                     ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidpath.restype = ctypes.c_int
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        _LIBC = lib
    except Exception:
        _LIBC = None
    return _LIBC


def all_pids():
    """当前所有 pid（libproc）；失败返回 []。"""
    lib = _libc()
    if lib is None:
        return []
    try:
        need = lib.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
        if need <= 0:
            return []
        # ★ +32 个槽位的余量：两次调用之间进程数会变，缓冲开小了会截断
        buf = (ctypes.c_int * (need // 4 + 32))()
        got = lib.proc_listpids(PROC_ALL_PIDS, 0, buf, ctypes.sizeof(buf))
        if got <= 0:
            return []
        return [buf[i] for i in range(got // 4) if buf[i] > 0]
    except Exception:
        return []


def pid_bsd(pid):
    """返回 (ppid, comm)；读不到返回 None（别人的进程会 EPERM，属正常）。"""
    lib = _libc()
    if lib is None:
        return None
    try:
        bi = _BsdInfo()
        if lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0,
                            ctypes.byref(bi), ctypes.sizeof(bi)) != ctypes.sizeof(bi):
            return None
        return int(bi.pbi_ppid), bi.pbi_comm.decode('utf-8', 'replace')
    except Exception:
        return None


def pid_path(pid):
    """进程可执行文件的完整路径（识别「这是不是 WorkBuddy 的进程」靠它）。"""
    lib = _libc()
    if lib is None:
        return None
    try:
        b = ctypes.create_string_buffer(4096)
        r = lib.proc_pidpath(pid, b, 4096)
        return b.value.decode('utf-8', 'replace') if r > 0 else None
    except Exception:
        return None


def list_processes():
    """返回 [(pid, ppid, 命令名, 完整命令行/可执行路径)]；探不到返回 None（与「空列表」区分）。

    两条路，先 libproc（快、不 exec、沙盒里也能跑），不行再退回 `ps`。
    """
    if not IS_MAC:
        return None

    # ---- 路一：libproc
    pids = all_pids()
    if pids:
        rows = []
        for pid in pids:
            bsd = pid_bsd(pid)
            if bsd is None:
                continue          # 别人的进程，读不到就跳过（我们只关心自己的）
            ppid, comm = bsd
            rows.append((pid, ppid, comm, pid_path(pid) or comm))
        if rows:
            return rows

    # ---- 路二：`ps` 兜底
    rc, out = _run([PS, '-Ao', 'pid=,ppid=,comm=,command='], timeout=12)
    if rc != 0:
        return None
    rows = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except Exception:
            continue
        rows.append((pid, ppid, parts[2], parts[3] if len(parts) > 3 else parts[2]))
    return rows or None


def wb_pids(rows):
    """WorkBuddy 本体（含渲染 / GPU 等 helper）的 pid 集合。"""
    out = set()
    for pid, _ppid, comm, cmdline in rows:
        if 'WorkBuddy.app' in cmdline or comm.endswith('Electron'):
            out.add(pid)
    return out


def _is_noise_proc(base, path):
    """噪音进程？—— shell、我们自己的探针、以及 WorkBuddy 的常驻 helper。

    ★ 用**前缀**而不是全等匹配：libproc 的 `pbi_comm` 只有 16 字节，
      `chrome_crashpad_handler` 到这里就截成 `chrome_crashpad`；解释器的 comm
      是带版本的 `python3.13`。全等匹配会全部漏掉。
    """
    cands = set()
    for c in (base or '', os.path.basename(path or '').lower()):
        if c:
            cands.add(c)
    for c in cands:
        if c in NOISE_PROC:
            return True
        # 解释器：python / python3 / python3.13 / pythonw …
        if c.startswith('python') or c.startswith('node'):
            return True
        # WorkBuddy 的常驻 helper（一直在，不代表团伙在干活）
        for h in WB_IDLE_HELPERS:
            if c.startswith(h):
                return True
        # Electron 自己的 Renderer / GPU / Plugin 等
        if c.startswith('workbuddy helper') or c.startswith('electron helper'):
            return True
    return False


def _cfg_num(conf, key, default):
    """从配置里取一个数；**只在「缺失 / 为空 / 解析不了」时**才回退到 default。

    ★ 为什么不能用 `conf.get(k) or default`：那样 **0 会被当成「没设」**。
      踩过的实例（2026-09-27）：`activityWindowSec: 0` 本意是「关掉会话文件这条信号」
      （`--selftest-shutdown` 演练就靠它 —— 演练自己一直在写会话文件），
      结果 `0 or 60` 回落到 60，信号压根没关掉，演练全凭「碰巧 60 秒没写文件」才过。
      配置项里凡是「0 有明确含义」的，都必须走这个函数。
    """
    v = conf.get(key)
    if v is None or v == '':
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def child_probe(rows, prev, conf, now=None):
    """WorkBuddy 起了工具子进程没有 —— 判「正在干活」的第三路。

    与 Win 版同思路，但用 libproc / `ps` 代替 Win32 的进程快照：
      · 只看 WorkBuddy 的**后代**（避免把系统进程算进来）；
      · 噪音进程（shell / 我们自己的解释器 / WorkBuddy 常驻 helper）不算；
      · 「正在干活」= **CPU 在涨**，而不是「存在」——
        残留的常驻 helper 不该把 busy 钉死（Win 版为此踩过坑）。
    返回 (kids 描述列表, 是否忙, 新的 prev 采样表)。
    """
    if not conf.get('useChildProcess', True):
        return [], False, prev
    now = now or time.time()
    if rows is None:
        return [], False, prev
    roots = wb_pids(rows)
    if not roots:
        return [], False, prev
    by_parent = {}
    for pid, ppid, comm, cmdline in rows:
        by_parent.setdefault(ppid, []).append((pid, comm, cmdline))

    kids = []
    # ★ seen 必须先装进 roots：WorkBuddy 的好几个进程（主进程 / 渲染 / GPU）
    #   既满足 wb_pids 的条件（自己的根），又是别的根的**子**进程。
    #   不预装的话它们会被当成「起了的工具子进程」重复列进来，面板上看着莫名其妙。
    seen = set(roots)
    stack = list(roots)
    while stack:
        p = stack.pop()
        for pid, comm, cmdline in by_parent.get(p, []):
            if pid in seen:
                continue
            seen.add(pid)
            stack.append(pid)
            base = os.path.basename(comm or '').lower()
            if _is_noise_proc(base, cmdline):
                continue
            kids.append((pid, base, cmdline))

    cpu_eps = _cfg_num(conf, 'childCpuEps', 0.05)
    rate_min = _cfg_num(conf, 'childRateMin', 0.005)
    rate_win = _cfg_num(conf, 'childRateWinSec', 60)
    # ⚠️ childFreshSec / childStaleGraceSec 两项目**保留在配置里但不使用**（跨平台字段对齐）。
    #    Win 版拿它们做「刚起来就算忙 / 安静够久才算停」，macOS 上刻意不做：
    #    WorkBuddy（Electron）会**例行**拉起一堆短期 helper（sandbox-cli、crashpad…），
    #    按「新建」判忙会把电脑钉在「一直忙」上 —— 正好与需求相反。
    #    这里只认一条最不容易误伤的：**CPU 速率在涨**。漏掉的短任务由库信号兜住。

    busy = False
    desc = []
    newprev = {}
    for pid, base, cmdline in kids:
        cpu = _proc_cpu_sec(pid)
        old = prev.get(pid)
        delta = None
        if old is not None and cpu is not None:
            delta = max(0.0, cpu - old)
        if cpu is not None:
            newprev[pid] = cpu
        tag = []
        hot = False
        if old is None:
            # 第一次见：只登记，不判忙 —— 否则守护刚起来就会把满屏常驻 helper 算成「在干活」
            tag.append('新见到')
        if delta is not None and delta >= cpu_eps:
            hot = True
            tag.append('在烧 CPU %.3f 秒' % delta)
        if delta is not None and delta / max(1.0, rate_win) >= rate_min:
            hot = True
            tag.append('速率 %.5f 核' % (delta / max(1.0, rate_win)))
        if hot:
            busy = True
            tag.append('干活中')
        else:
            tag.append('常驻·没在干活')
        desc.append('%s（%s）' % (base, '·'.join(tag)))

    # 保留少量现场，别让 prev 无限长
    if len(newprev) > 200:
        newprev = dict(list(newprev.items())[-200:])
    return desc, busy, newprev


def _proc_cpu_sec(pid, timeout=8):
    """某个进程累计吃掉的 CPU 秒数。读不到返回 None。

    首选 libproc 的 proc_taskinfo（pti_total_user + pti_total_system，单位纳秒），
    读不到再退回 `ps -o time=`（同 list_processes 的取舍说明）。
    """
    lib = _libc()
    if lib is not None:
        try:
            ti = _TaskInfo()
            if lib.proc_pidinfo(pid, PROC_PIDTASKINFO, 0,
                                ctypes.byref(ti), ctypes.sizeof(ti)) == ctypes.sizeof(ti):
                return (ti.pti_total_user + ti.pti_total_system) / 1e9
        except Exception:
            pass

    rc, out = _run([PS, '-o', 'time=', '-p', str(pid)], timeout=timeout)
    if rc != 0:
        return None
    s = (out or '').strip()
    if not s:
        return None
    # 形如 00:00:03 / 1-02:03:04 / 3:04.56
    days = 0
    if '-' in s:
        d, s = s.split('-', 1)
        try:
            days = int(d)
        except Exception:
            days = 0
    parts = s.split(':')
    try:
        parts = [float(x) for x in parts]
    except Exception:
        return None
    sec = 0.0
    for x in parts:
        sec = sec * 60 + x
    return days * 86400 + sec


def _hermes_home(conf):
    """定位 Hermes 主目录：conf.hermesHome → $HERMES_HOME → ~/.hermes。"""
    home = conf.get('hermesHome') or ''
    cands = []
    if home:
        cands.append(home)
    he = os.environ.get('HERMES_HOME') or ''
    if he:
        cands.append(he)
    cands.append(os.path.join(os.path.expanduser('~'), '.hermes'))
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return ''


def probe_hermes_tasks(conf):
    """Hermes 是否**正在跑一轮**（对应界面上的「蓝色」）。返回 (age_sec, ok, why)。

    ★ 判据（2026-10-09 用户要求）：Hermes 跑一轮对话/工具调用时会**持有一条
      `session_turn_leases` 租约**（Desktop / CLI / 网关都一样），轮次一结束就 release。
      所以「有没有未过期的租约」＝「是不是蓝色正在运行」。
      ⇒ **只有蓝色才算「在跑」**；运行完毕(绿)/需要人工操作(黄)/已读(灰) 都不算，
        不挡「跑完自动关机」的倒计时。
    ★ 老库没有该表时，退回 state.db 的写入新鲜度（`hermesActiveWindowSec`）。
    """
    if not conf.get('useHermesSignal', True):
        return None, False, '配置里关掉了（useHermesSignal=false）'
    root = _hermes_home(conf)
    if not root:
        return None, False, '找不到 Hermes 主目录'
    db = os.path.join(root, 'state.db')
    if not os.path.exists(db):
        return None, False, 'Hermes 主目录里没有 state.db（没跑过？）'
    now = time.time()
    try:
        import sqlite3
        con = sqlite3.connect('file:%s?mode=ro' % db, uri=True, timeout=2)
        try:
            row = con.execute(
                'SELECT MAX(expires_at) FROM session_turn_leases WHERE expires_at > ?',
                (now,)).fetchone()
        finally:
            con.close()
        if row and row[0] and row[0] > now:
            return 0.0, True, ''                       # 有活跃租约 → 蓝色 = 正在运行
        return None, False, '不在运行（没有未过期的轮次租约）'
    except Exception as e:
        if 'no such table' not in str(e).lower():
            # ★ 读不到 ≠ 空闲：保守判「在跑」，绝不因一次读失败就把机器关了
            #   （项目一贯的取向：宁可多守一会儿，也不误判成“已经结束”）。
            return 0.0, True, '读租约失败（保守判忙）：%s' % e
    # ---- 退回：老库无租约表 —— 用 state.db 写入新鲜度 ----
    newest = None
    for name in ('state.db', 'state.db-wal', 'state.db-shm'):
        f = os.path.join(root, name)
        try:
            m = os.path.getmtime(f)
        except Exception:
            continue
        if newest is None or m > newest:
            newest = m
    if newest is None:
        return None, False, '读不到 state.db 的写入时间'
    return max(0.0, now - newest), True, ''


def probe(wb_home, conf, prev):
    """一轮探测。返回 (signals, busy, prev)。

    busy = 有没有任务在跑（用于灯面与关机判定）。
    """
    now = time.time()
    sig = {}

    # ① WorkBuddy 自己的状态（最权威）
    working, running, ok, why = probe_wb_tasks(conf, wb_home)
    sig['wbWorking'] = working
    sig['wbRunning'] = running
    sig['wbSig'] = 'db' if ok else ''
    sig['wbWhy'] = why

    # ①b Hermes 自己的活动新鲜度（双监测）
    hage, hok, hwhy = probe_hermes_tasks(conf)
    hwin = _cfg_num(conf, 'hermesActiveWindowSec', 120)
    sig['hermesAge'] = hage
    sig['hermesSig'] = 'db' if hok else ''
    sig['hermesWhy'] = hwhy
    sig['hermesBusy'] = bool(hok and hage is not None and hage <= hwin)

    # ★ 2026-10-11（A6）：**第三态「未知」** —— 任务信号读不到时，既不算忙也不算闲。
    #   为什么必须有它：读不到一律判忙 → 会「永远不关机」、而且界面上看不出来（事故型）。
    #   判据：Hermes 租约读失败（probe_hermes_tasks 里那句「读租约失败（保守判忙）」），
    #        或 WorkBuddy 那路给的是"读失败"型原因。
    #   持续读不到 → 交给 shutdown_decision 走兜底关机（`unknownShutdownSec`）。
    _unk = ('读租约失败' in (hwhy or '')) or \
           ('读' in (sig.get('wbWhy') or '') and '失败' in (sig.get('wbWhy') or ''))
    sig['unknown'] = bool(_unk)
    if _unk:
        if not _UNKNOWN_SINCE[0]:
            _UNKNOWN_SINCE[0] = now
    else:
        _UNKNOWN_SINCE[0] = 0.0
    sig['unknownFor'] = round((now - _UNKNOWN_SINCE[0]) if _UNKNOWN_SINCE[0] else 0.0, 1)

    # ② 会话文件在写
    age, n = scan_projects_mtime(wb_home)
    sig['fileAge'] = age
    sig['files'] = n
    win = _cfg_num(conf, 'activityWindowSec', 60)
    sig['file'] = bool(age is not None and age <= win)

    # ③ 工具子进程
    rows = list_processes()
    sig['procEnumOk'] = rows is not None
    sig['wbProcs'] = len(wb_pids(rows)) if rows else 0
    kids, child_busy, prev = child_probe(rows, prev, conf, now)
    sig['children'] = kids[:12]
    sig['childBusy'] = bool(child_busy)

    # ④ 兜底：三路都探不到（比如没有会话目录）时，进程在跑就算忙
    fallback = not ok and age is None and not (rows or [])
    sig['fallback'] = bool(fallback)
    wb_on = wb_running() if fallback else (sig['wbProcs'] > 0)

    # ★ 双监测：拆两路，好让界面说清「到底是谁在跑」。
    _wb_only = bool(sig['file'] or working > 0 or running > 0 or child_busy or fallback)
    sig['wbBusy'] = _wb_only
    _src = []
    if _wb_only:
        _src.append('WorkBuddy')
    if sig['hermesBusy']:
        _src.append('Hermes')
    sig['busySrc'] = '+'.join(_src)
    busy = bool(_wb_only or sig['hermesBusy'])
    sig['running'] = busy
    sig['runWhy'] = ('Hermes %s前刚活动' % ('%.0f 秒' % hage if hage is not None else '?')) if sig['hermesBusy'] else (
        '库里有 %d 个 working 会话' % working) if working > 0 else (
        '会话文件 %s 前刚写过' % ('%.0f 秒' % age if age is not None else '?') if sig['file']
        else ('起了工具子进程' if child_busy else
              ('兜底：WorkBuddy 在跑' if fallback else '')))
    sig['keepAwake'] = busy
    sig['keepWhy'] = sig['runWhy'] or '没有额外动静'
    sig['wbOn'] = bool(wb_on)

    # ⑤ ★ 显式钩子（演练 / 排障用，与 Win 版同一个环境变量名）：
    #    `WB_POWERGUARD_FORCE_BUSY=<unix 秒>` —— 在该时刻之前**强制判忙**。
    #    为什么需要它：判据改成「看 CPU 速率」以后，安静的常驻 helper 不再算忙，
    #    而 `--selftest-shutdown` 原来正是靠「那批安静 helper 先算忙、宽容期后过期」
    #    来把 ever_busy 置真的 —— 那条路没了。给演练一个**确定**的「忙」来源，
    #    比让它去猜进程树可靠（演练未必跑在 WorkBuddy 的进程树下）。
    #    ⚠️ 只影响守护自己的判定；不写配置文件、不跨进程继承。
    try:
        _fb = float(os.environ.get('WB_POWERGUARD_FORCE_BUSY') or 0)
    except Exception:
        _fb = 0.0
    if _fb and now < _fb:
        busy = True
        sig['forcedBusy'] = round(_fb - now, 1)
        sig['runWhy'] = sig['runWhy'] or '演练钩子：强制判忙'
        sig['running'] = True
        sig['keepAwake'] = True
        sig['keepWhy'] = sig['runWhy']
    else:
        sig.pop('forcedBusy', None)

    return sig, busy, prev


# ======================================================== 关机

def _dur(sec):
    """把秒数说成人话：< 90 秒就说秒，否则说分钟。日志里别出现「0 分钟」。"""
    try:
        sec = float(sec)
    except Exception:
        return '%s 秒' % sec
    return ('%.0f 秒' % sec) if sec < 90 else ('%.1f 分钟' % (sec / 60.0))


# 「到点自动结束」允许的时长上下限（秒）。下限防「设成 0 秒当场结束」；
# 上限 24 小时防手滑（多打一个 0 变成 20 小时）。
AUTOSTOP_MIN_SEC = 60
AUTOSTOP_MAX_SEC = 24 * 3600


def parse_duration(s):
    """把「2h」/「90m」/「1h30m」/「120」（纯数字＝分钟）解析成秒。

    返回 (秒数, '') 或 (None, 原因)。写法与 Win 版同一套，方便两端参数描述逐字对齐。
    """
    raw = str(s or '').strip().lower()      # str() 兜底：万一被人传了 int（正常路径不会）
    if not raw:
        return None, '没给时长'
    t = (raw.replace('小时', 'h').replace('分钟', 'm').replace('个', '')
            .replace('钟', '').replace('分', 'm').replace('时', 'h'))
    parts = re.findall(r'(\d+(?:\.\d+)?)\s*([hm]?)', t)
    if not parts:
        return None, '认不出这个时长：%s（写法示例：30m / 2h / 1h30m / 90）' % s
    total = 0.0
    for val, unit in parts:
        try:
            v = float(val)
        except Exception:
            return None, '认不出这个数字：%s' % val
        total += v * (3600.0 if unit == 'h' else 60.0)   # 没单位 → 当分钟
    total = int(round(total))
    if total < AUTOSTOP_MIN_SEC:
        return None, '太短了（最少 1 分钟）'
    if total > AUTOSTOP_MAX_SEC:
        return None, '太长了（最多 24 小时，防手滑）'
    return total, ''


def clear_expired_autostop(conf, now=None):
    """把「已经过期、却没被清掉」的到点结束设定清干净。返回是否真清过。

    ★ 什么情况下会留下这种残值：do_autostop 靠 save_conf 把 autoStopAt 归零；
      万一那次写盘失败（只读盘 / 权限 / 安全软件拦），配置里就留着一个**过期**的
      时间戳。之后每次拉起守护都会立刻「到点结束」一次 —— 用户看到的现象是
      「刚点完档位，防休眠马上又被撤掉」，而且会一次一次重复。
    ★ 判据严格：**只清已经过去的**。将来的设定（用户正等着它的）一律不动。
    """
    at = _cfg_num(conf, 'autoStopAt', 0)
    if not at:
        return False
    if at > (time.time() if now is None else now):
        return False                     # 还没到点 —— 绝不能动
    conf['autoStopAt'] = 0
    conf['autoStopSetAt'] = 0
    conf['autoStopDurationSec'] = 0
    conf['autoStopBootTs'] = 0
    return True


def drop_autostop_no_awake(conf):
    """「防休眠」总开关没开 → 把「到点自动结束」设定作废。返回是否真作废了一个。

    ★★ 语义（用户 2026-09-30 定，原话：「我把这个防休眠关了之后，应该就不再计时了
       才对。只有防休眠打开的时候，才会开始计时」）：
       到点结束是**值守期间**的功能 —— 依附「防休眠」总开关。总开关一关，设定直接
       作废（界面滑块弹回最右端「无限」），重新打开要重新选时长。

    ★ 为什么必须有这道闸（原来没有，症状很隐蔽）：`autoStopAt` 是个**绝对时间戳**，
       与开关状态毫无关系 —— 用户关掉防休眠后，界面上的倒计时照走（`tickAutoStop`
       只看 `autoStopAt > 0`），到点 `do_autostop()` 还会真执行一次：撤抑制 + 关掉
       总开关 + **顺手把 `autoShutdown` 也清了**。用户完全不知道它在倒计时。

    ★ 只管 autoStop 那四个字段，**不动 autoShutdown** —— 那是独立附加项
      （「跑完自动关机」不依附任何开关，用户 2026-09-28 明确过）。
    ★ 判据用 `autoStopAt` **或** `autoStopDurationSec`：正常路径下两者同生同灭，
      但万一写盘失败只落下了一个，也要能收干净。
    """
    if feats(conf).get('awake'):
        return False
    if not _cfg_num(conf, 'autoStopAt', 0) and not _cfg_num(conf, 'autoStopDurationSec', 0):
        return False
    conf['autoStopAt'] = 0
    conf['autoStopSetAt'] = 0
    conf['autoStopDurationSec'] = 0
    conf['autoStopBootTs'] = 0
    return True


def autostop_decision(conf, now):
    """要不要现在执行「到点自动结束」？返回 (bool, 未触发的原因)。

    ★ 从守护主循环里抽出来（照 shutdown_decision 的先例）：能写回归，
      也能把「差在哪」原样写进日志 —— 用户「设了到点结束却没结束」这类
      事故最缺的就是这行原因。
    ★ 「到点自动结束」与「跑完自动关机」**互斥**（开一个会自动关掉另一个，
      见 cmd_autostop_set / cmd_shutdown_set）—— 所以这里不必考虑两者的先后关系。
    """
    at = _cfg_num(conf, 'autoStopAt', 0)
    if not at:
        return False, '没设「到点自动结束」'
    # ★★ 2026-09-30：最后一道闸 —— 没在值守就绝不动手。正常路径上
    #   drop_autostop_no_awake() 已经把设定清了，走到这儿说明配置是残值
    #   （写盘失败 / 手改 / 旧版本留下的）；此时宁可不触发，也不能在用户
    #   已经把防休眠关掉的情况下、突然把一切停掉。
    if not feats(conf).get('awake'):
        return False, '「防休眠」没开（到点结束只在值守期间生效）'
    # 只对「设定那一刻所处的那次开机」有效 —— 重启后自动变「没设」（同 autoShutdown）
    bts = _cfg_num(conf, 'autoStopBootTs', 0)
    if bts and (time.time() - boot_time()) > 60 and abs(bts - boot_time()) > 120:
        return False, '这是上一次开机时设的（重启后自动失效）'
    if now < at:
        return False, '还没到点（还剩 %s）' % _dur(at - now)
    return True, ''


def shutdown_decision(conf, idle_for, now, last_shutdown_try, shutdown_pending,
                      unknown_for=0.0, last_busy_at=0.0):
    """要不要现在触发自动关机？返回 (bool, 未触发的原因, 触发类型)。

    触发类型：'finish' = 任务跑完；'unknown' = 信号持续读不到走兜底。

    ★ 从守护主循环里抽出来（照 Win 版的做法），这样：
      · 能写回归 —— 原来埋在 while 里只能靠人眼读；
      · 能把「差在哪」原样写进日志 —— 用户「明明开了跑完自动关机，电脑却整夜没关」
        这类事故最缺的就是这行原因。
    """
    if not conf.get('autoShutdown'):
        return False, '「跑完自动关机」开关是关着的', ''
    # ★★ 安全闸：配置里若带着演练残留标记（selftestRun），**绝不允许**真关机。
    #    为什么必须有它（2026-09-27 踩到）：演练要把 idleConfirmSec 压到 8 秒、
    #    autoShutdown 打开，才能十几秒跑完。要是演练进程被强杀（超时、终端被关、
    #    用户 Cmd+C），配置就留在「开关开着 + 只等 8 秒」这个状态。
    #    此时用户随手点一下别的档位 → 新守护起来，_DRY 却是 False → 电脑
    #    **在 8 秒判闲 + 倒计时后真的关机**，而用户完全不知道发生了什么。
    #    所以宁可少关一次机，也不能让演练的残留把机器关掉。
    if conf.get('selftestRun') and not _DRY:
        return False, ('配置里有演练残留标记 selftestRun —— 为安全起见不执行关机。'
                       '跑一次 --selftest-shutdown 或 --stop 会把配置恢复干净'), ''
    if shutdown_pending:
        return False, '已经在关机倒计时里了', ''
    # 只对「本次开机」有效 —— 重启后自动变「关」（用户 2026-09-27 明确要求）
    bts = _cfg_num(conf, 'autoShutdownBootTs', 0)
    if bts and (time.time() - boot_time()) > 60 and abs(bts - boot_time()) > 120:
        return False, '这是上一次开机时打开的开关（重启后自动失效）', ''
    # ---- A6 兜底：任务信号持续读不到 → 到点就关（与"跑完"无关）----
    unk_need = _cfg_num(conf, 'unknownShutdownSec', 1200)
    if unk_need > 0 and unknown_for >= unk_need:
        return True, ('任务信号已持续 %s 读不到（上限 %s）'
                      % (_dur(unknown_for), _dur(unk_need))), 'unknown'
    if unknown_for > 0:
        return False, ('任务信号读不到（已 %s；满 %s 走兜底关机）'
                       % (_dur(unknown_for), _dur(unk_need))), ''
    # ---- 正常路：**本次开机要真的跑完过一次**（触发是事件，不是状态）----
    #   ★ 2026-10-11（A1）：原来靠"静默够久"这个**状态**触发，才需要 10 分钟冷却去兜。
    #     现在要求「结束时刻晚于上次触发/取消时刻」＝ 必须有一次新的"跑完"。
    #     于是：取消/关机失败之后不会自己重来（要等你下次跑完），**冷却整条删掉**。
    if last_busy_at <= last_shutdown_try:
        return False, '自上次触发/取消以来还没有新的「任务跑完」', ''
    deb = _cfg_num(conf, 'idleConfirmSec', 60)
    if idle_for < deb:
        return False, ('距上次「忙」才 %s，还不够去抖的 %s'
                       % (_dur(idle_for), _dur(deb))), ''
    return True, '', 'finish'


def boot_time():
    """本机开机时刻（unix 秒）。"""
    try:
        rc, out = _run(['/usr/sbin/sysctl', '-n', 'kern.boottime'], timeout=8)
        m = re.search(r'sec\s*=\s*(\d+)', out or '')
        if m:
            return float(m.group(1))
    except Exception:
        pass
    # 退路：用 uptime 秒数倒推
    try:
        rc, out = _run(['/usr/sbin/sysctl', '-n', 'kern.boottime'], timeout=8)
    except Exception:
        pass
    try:
        with open('/proc/uptime') as f:      # 非 macOS 才有
            return time.time() - float(f.read().split()[0])
    except Exception:
        return 0.0


def _sleep_with_heartbeat(sec, step=10.0):
    """睡 sec 秒，但每 step 秒往状态文件刷一次心跳。

    ★ 为什么需要它：关机命令发出去之后要等宽限期，而这段时间主循环是**停住**的
      —— 状态文件的 `heartbeatAt` 不更新，判活阈值（120 秒）一到，界面那盏
      「运行监测」徽标就会翻成「守护没在跑」。用户最容易截图的时刻恰恰就是
      「任务跑完了、机器怎么还不关」，不该让他同时看到一个假的"没在跑"。
      所以边等边报平安。（照 Win 版的同名函数。）
    """
    end = time.time() + max(0.0, float(sec))
    while True:
        left = end - time.time()
        if left <= 0:
            return
        try:
            st = read_state() or {}
            st.update({'pid': os.getpid(), 'heartbeatAt': time.time(),
                       'hbMax': hb_max_for(step + 60.0)})
            write_state(st)
        except Exception:
            pass
        time.sleep(min(step, left))


def _wake_display():
    """发关机请求前先把屏幕点亮。

    ★ 为什么（2026-10-10 夜间事故）：夜里「合盖熄屏」把屏幕熄着，守护这时请求关机；
      一旦系统弹出授权框（或某个程序顶着不让关），屏幕上一片黑 —— 用户开盖只看到
      「黑屏 + 转圈」，只能强按电源。先把屏幕点亮，弹框才有机会被看见/处理。
    `/usr/bin/caffeinate -u -t 1` = 模拟一次用户活动（唤醒屏幕），1 秒后自己退。
    """
    try:
        _run(['/usr/bin/caffeinate', '-u', '-t', '1'], timeout=8)
    except Exception:
        pass


def _mark_lid_screen(forced, log=None):
    """记下「内建屏是被我们按灭的」这个事实（落 `_运行监测状态.json`）。

    ★ 为什么要落盘（2026-10-12）：守护若在合盖期间被强杀/重启/换档位，
      屏还灭着而它自己不知道 → 开盖时没人去点灯，用户看到的就是黑屏 + 转圈。
      落个标记，下一轮（或下次启动）读到「标记还在 + 盖子已打开」就主动把屏叫回来。
    """
    try:
        st = read_state() or {}
        if bool(st.get('lidScreenForced')) == bool(forced):
            return
        st['lidScreenForced'] = bool(forced)
        write_state(st)
    except Exception:
        pass


def _resume_display(log=None, tries=4):
    """开盖 / 启动自愈时把屏**主动叫回来**（档位三专用）。返回是否确认亮起。

    ★★ 2026-10-12（用户报的恶性 bug）：档位三开着 `pmset -a disablesleep 1` 时，
      合盖不再触发系统睡眠，而是我们自己 `pmset displaysleepnow` 把内建屏按灭。
      系统因此**不会**在开盖时执行「唤醒显示器」——它根本不认为曾经睡过。
      所以「不再压熄屏」远远不够，必须自己发一次用户活动
      （`caffeinate -u -t 1`，等价于动一下鼠标/敲一下键）把屏点亮，并核对结果。
      读不到屏状态（CG 不可用）时不硬撑：试两次就收手，别在这儿空转。
    """
    for i in range(max(1, tries)):
        try:
            _run([CAFFEINATE, '-u', '-t', '1'], timeout=8)
        except Exception as e:
            if log:
                log('唤醒屏幕：调用 caffeinate 失败（%s）' % e)
        time.sleep(0.6)
        st = display_asleep()
        if st is False:
            if log:
                log('唤醒屏幕：已点亮（第 %d 次）' % (i + 1))
            return True
        if st is None and i >= 1:
            if log:
                log('唤醒屏幕：读不到屏状态（CoreGraphics 不可用），按已下发算')
            return True
    if log:
        log('唤醒屏幕：发了 %d 次用户活动仍读不到「已点亮」——'
            '动一下触控板/敲一下键即可；若长期如此把这行反馈给工具作者' % tries)
    return False


def lid_screen_selfheal(log=None):
    """兜底自愈：**只要「屏是我们按灭的」标记还在、而盖子已经打开，就把屏叫回来。**

    ★ 2026-10-12（用户报的恶性 bug「合盖后再打开 → 黑屏 + 鼠标转圈」的第二道保险）：
      主路径是 `lid_screen_guard()` 里「开盖那一刻」调 `_resume_display()`。但下面这些
      情形主路径够不着，屏会一直灭着：
        · 守护在合盖期间被强杀 / 重启（它自己不知道屏还灭着）；
        · 用户在合盖状态下关掉「合盖后保持唤醒」（guard 从此不再被调用）；
        · 合盖事件那一路失灵，状态变化没被看到。
      所以每轮（和启动时）都读一次这个标记来兜底。代价只是一个小 JSON 的读取。
    """
    try:
        if not (read_state() or {}).get('lidScreenForced'):
            return False
        if lid_closed() is True:              # 还合着盖 → 屏本来就该灭着
            return False
        if log:
            log('自愈：屏是我们按灭的、盖子已经打开 → 主动唤醒屏幕')
        if _resume_display(log):
            _mark_lid_screen(False)
            return True
    except Exception as e:
        if log:
            log('自愈检查失败（%s）—— 不影响守护' % e)
    return False


def _shutdown_argv_sudo():
    """免密关机命令：装了 /etc/sudoers.d/workbuddy-shutdown 就秒过、**不弹框**。"""
    return ['/usr/bin/sudo', '-n', '/sbin/shutdown', '-h', 'now']


def do_shutdown():
    """真的关机。返回 (成功?, 说明)。

    ★★ 2026-10-11 重排顺序（用户要求：正常情况别把屏幕无故点亮）：
        ① `sudo -n /sbin/shutdown -h now`（免密白名单 /etc/sudoers.d/workbuddy-shutdown）
           —— **主路**：不弹框、不卡、**不点灯**；
        ② 只有 ① 不通了，才 `_wake_display()` **点亮屏幕** → 走
           `osascript … tell application "System Events" to shut down`（它要 GUI／授权框，
           夜里屏幕熄着时正是卡在这条路上 —— 那晚「黑屏 + 转圈」的根因）。
      两条都不通就如实上报失败；**不再有 10 分钟冷却自动重试**（那是旧的状态型触发的补丁）——
      失败后要等下一次「任务跑完」或用户手动处理。
    ★ 发令之后要**等一等再下结论** —— 真关机的话本进程会被系统结束，走不到下一行；
      所以「宽限期内我还活着」就是"没关成"最直接的证据。
    """
    conf = load_conf()
    grace = int(_cfg_num(conf, 'shutdownGraceSec', 60))
    grace = max(0, min(grace, 3600))
    force_after = int(_cfg_num(conf, 'shutdownForceAfterSec', 0))
    force_after = max(0, min(force_after, 24 * 3600))

    if _DRY:
        log('【演练】本应执行关机（空跑，未真的关机）')
        return True, ''

    used = ''
    err = ''
    # ★★ 2026-10-11（A3 · 用户要求）：**先走免密 sudo，不点灯**。
    #   关不关得掉跟屏幕亮不亮无关；点灯只是为了下面那条 AppleScript 退路
    #   （它要 GUI／授权框 —— 夜里屏幕熄着时正是卡在这条路上）。
    rc, out = _run(_shutdown_argv_sudo(), timeout=60)      # ① 免密 sudo（主路）
    if rc == 0:
        used = 'sudo -n /sbin/shutdown'
    else:
        err = (out or '').strip()[:200]
        _wake_display()                  # ② 只有 sudo 不通了，才点亮屏幕
        rc2, out2 = _run([OSASCRIPT, '-e',
                          'tell application "System Events" to shut down'], timeout=30)
        if rc2 == 0:
            used = 'osascript（System Events）'
        else:
            err2 = (out2 or '').strip()[:200]
            log('关机命令失败：免密 sudo rc=%s（%s）；osascript rc=%s（%s）'
                % (rc, err, rc2, err2))
            return False, ('关机两条路都没成功。可在「终端」执行：sudo shutdown -h now'
                           '（sudo: %s ／ osascript: %s）' % (err, err2))

    log('已发出关机命令（%s）%s'
        % (used, ('；%d 秒内仍可取消' % grace) if grace > 0 else ''))
    wait = (grace + force_after) if force_after > 0 else (min(grace, 120) + 45)
    if wait <= 0:
        return True, ''
    _sleep_with_heartbeat(wait)
    # 走到这里 = 宽限期过了机器还没关（真关机的话进程早没了）
    if force_after > 0:
        log('宽限期（%d 秒）过了机器还没关 → 再强制关一次' % wait)
        rc3, out3 = _run(_shutdown_argv_sudo(), timeout=60)
        if rc3 == 0:
            _sleep_with_heartbeat(min(grace, 120) + 45)
            return True, ''
        log('强制关机失败：rc=%s %s' % (rc3, (out3 or '').strip()[:200]))
        return False, ('强制关机也没成（rc=%s）：可在「终端」执行 sudo shutdown -h now' % rc3)
    log('关机命令已发出，但 %d 秒后机器仍未关（多半有程序顶着不让关）' % wait)
    return False, ('关机命令已发出但机器没关（可能有程序顶着）。'
                   '可在「终端」执行 sudo shutdown -h now；'
                   '想让它无论如何都关，把配置里的 shutdownForceAfterSec 设成非 0')


def do_autostop(conf, idle_for=0.0, busy=False, reason='deadline'):
    """「到点自动结束」到点时做的事：改配置 + 记账；随后由调用方走 cleanup()。

    ★ 就是「停止监测」那一套（与 cmd_stop 同一语义）：撤抑制 → 写回合盖设置 →
      关掉总开关 → 守护退出。所以「结束」的字面直觉成立 —— 回到什么都没干。
    ★ 硬切约定（用户 2026-09-28 定）：到点就撒手，**不等**任务跑完；
      但到点时若仍有任务在跑，日志必须写清楚，免得事后看不出是它断的。
    ★ 顺手把 autoShutdown 也关掉：两者互斥，能走到这里它本来就该是关的，这里是兜底。
    ★★ 2026-10-01：多一个 `reason`（学 PommeToys 的 `stop(reason:)`）——
      「为什么停」要留痕，见 set_end_reason / `--status --full`。
      ⚠️ 现在只剩「到点」一条路会调它；**电量不足那条不再走这儿**（见主循环）。
    """
    at = _cfg_num(conf, 'autoStopAt', 0)
    stamp = time.strftime('%H:%M', time.localtime(at)) if at else '?'
    set_end_reason(reason, '到点自动结束（%s）' % stamp)
    conf = load_conf()
    conf['enabled'] = False
    clear_feats(conf)
    conf['autoStopAt'] = 0
    conf['autoStopSetAt'] = 0
    conf['autoStopDurationSec'] = 0
    conf['autoStopBootTs'] = 0
    conf['autoShutdown'] = False
    _saved = save_conf(conf)
    if not _saved:
        # ★ 配置写不进去 → 这件事就「记不住」：下一个守护拉起时会读到同一个过期
        #   时间戳、再触发一次、再撤一遍防休眠 → 无限循环。补一道停止标记：
        #   它一存在，后续任何守护都会立刻正常退出，不会被反复拉起来干同一件事。
        try:
            open(STOP_FLAG, 'w', encoding='utf-8').write(str(time.time()))
        except Exception:
            pass
        log('★ 配置写不进去 → 已写停止标记（_运行监测.stop），'
            '避免「到点结束」被反复触发；请检查工具目录是否可写')
    if busy:
        log('★ 到点自动结束（%s）：**到点时仍有任务在跑**（已静默 %s）'
            '—— 按「硬切」约定撒手，不再继续顶住睡眠'
            % (stamp, _dur(idle_for)))
    else:
        log('到点自动结束（%s）：任务已空闲 %s' % (stamp, _dur(idle_for)))
    return True, ''


# ======================================================== 守护体

def _python_exe():
    """起守护 / 跑子脚本用的解释器（借 wb_paths 的探测，Mac 上优先托管环境）。"""
    try:
        sys.path.insert(0, HERE)
        import wb_paths as P
        p = P.find_python()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    return sys.executable


def daemon_main():
    conf = load_conf()
    if not os.path.exists(STOP_FLAG) and not load_conf_raw().get('enabled'):
        d = load_conf()
        d['enabled'] = True
        save_conf(d)
        conf = d

    if not _take_instance_lock():
        log('已有守护在跑 —— 本进程退出')
        return 0

    log('%s' % BAR)
    log('运行监测守护启动（macOS 版 · pid=%d）' % os.getpid())
    log('%s' % BAR)

    proc_holder = [None]
    prev = {}
    applied = 0
    last_busy_at = time.time()
    busy_since = 0.0
    ever_busy = False
    last_shutdown_try = 0.0
    shutdown_at = 0.0
    shutdown_pending = False
    wb_home = wb_home_of()
    cycles = 0
    # ★ 第四十八轮：日志节流从「按轮数」改成「**按时间**」。
    #   原因：一轮的长度不再是固定 15 秒（闲时 300 秒），按轮数会让心跳从
    #   5 分钟一行变成 100 分钟一行 —— 事后根本看不出它还在不在岗。
    beat_at = 0.0        # 「在守」心跳
    why_at = 0.0         # 「自动关机暂未触发」原因
    # 「合盖熄屏」的记事本（见 lid_screen_guard —— 按合盖/开盖两段各清一次）
    lid_ctx = {}
    # 这一轮真正用的 caffeinate 参数 —— 光看 level 不够：「合盖也不睡 + 屏幕常亮」
    # 与「合盖也不睡」的 level 都是 3，但参数不同（差一个 -d）。
    _applied_argv = [None]
    F = {'level': 0}

    # ★ 合盖事件监听（2026-09-28 加，学 PommeToys）：订阅 IOPMrootDomain 的通知，
    #   合盖那一刻把主循环叫醒，而不是靠每 2 秒问一次。
    #   订阅失败也没关系 —— 主循环会自动退回 2 秒轮询，功能一样。
    start_lid_event_watch()

    # ★★ 2026-10-12：启动自愈 —— 上一次若是在合盖期间退出/被强杀，内建屏可能还灭着。
    #   读到「屏是我们按灭的」标记且盖子已打开，就先把屏叫回来（见 lid_screen_selfheal）。
    lid_screen_selfheal(log)

    # ★ 电量/电源变化事件监听（2026-09-29 加，同样学 PommeToys 的 IOPS）：
    #   插拔电源 / 电量变化那一刻系统推消息，主循环被叫醒后立刻真读一次电量。
    #   原来靠 `pmset -g batt` + 60 秒缓存 → 最多晚 60 秒才发现「电量不足」。
    #   订阅失败也没关系 —— `low_batt_reason` 会自动退回按 lowBattCheckSec 问一次。
    start_pwr_event_watch()

    # ★★★ 2026-10-01（第四十八轮 · 用户第 4 条）：**「任务在不在跑」改成事件驱动**。
    #   原来的节奏是死的 `sleep(15)` —— 一天 5760 次，只为问一句有没有任务。
    #   现在盯两处**必然在变**的东西：
    #     · `workbuddy.db-wal` —— `sessions.status`（working/completed）每次改写都落这儿；
    #     · 最近活跃的会话 `.jsonl` —— 干活时每 10~30 秒追加一次。
    #   ★ 关键认识：「任务跑完了」**本身就是一个事件**（status 写回 completed →
    #     WAL 必然被 touch），所以**两端都由事件驱动**，不靠超时去猜 ——
    #     灯面翻转反而比原来 15 秒采样更快。
    #   起不来也没关系：主循环的兜底周期还在，只是退回到「隔一阵问一次」。
    start_task_event_watch()

    # ★ SIGUSR1 = 「别睡了，立刻跑一轮」。CLI 改完配置 / 落完取消标记就发它
    #   （见 _poke_daemon）。处理器只做一件事：置 `_CLI_POKE`（＝这是人在等结果，
    #   不许被限流吃掉）+ set 那个 Event 把这一觉叫醒。
    #   ⚠️ 只允许做「赋值 / set」这种原子小动作 —— 信号处理器跑在主线程的任意指令之间。
    def _on_cli_poke(*_a):
        _CLI_POKE[0] = True
        _LID_WAKE.set()

    try:
        signal.signal(signal.SIGUSR1, _on_cli_poke)
    except Exception as e:
        log('装 SIGUSR1 唤醒处理器失败（退回"按轮询发现"，功能不受影响）：%r' % (e,))

    def cleanup(*_a):
        try:
            release(proc_holder, quiet=True)
            lid_restore_conf(quiet=False)
            # ★★ 2026-10-12：退出前若屏还是**我们按灭的**、盖子又开着 → 先叫回来再走。
            #   否则用户点「停止监测」之后再开盖，看到的还是「黑屏 + 转圈」——
            #   这跟合盖那条是同源的恶性 bug，只是触发方式不同。
            #   盖子还合着就不动（屏本来就该灭着），标记留着，下次启动/下一轮自愈。
            lid_screen_selfheal(log)
        finally:
            st = read_state()
            st.update({'pid': 0, 'heartbeatAt': 0, 'busy': False,
                       'shutdownPending': False, 'shutdownAt': 0,
                       'level': 0, 'applied': 0})
            write_state(st)
            log('守护退出（已撤掉所有抑制，系统回到默认电源行为）')
            sys.exit(0)

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(s, cleanup)
        except Exception:
            pass

    while True:
        try:
            if os.path.exists(STOP_FLAG):
                log('看到停止标记（_运行监测.stop），正常退出')
                cleanup()

            conf = load_conf()
            # ★ 取消标记只在「正在倒计时」时有意义（见下面 shutdown_pending 那一段）。
            #   没在倒计时却看到它 = 上一次留下的残骸（用户点过取消，可那一刻并没有
            #   倒计时在跑，于是没人在倒计时分支里消费它）。
            #   ⚠️ 必须清掉：否则**下一次真的进入倒计时会被这个陈旧标记立刻取消** ——
            #   症状变成「跑完自动关机怎么都不触发」，比拦不下更气人。
            #   （2026-09-28 实测踩到，日志与文件都留下了证据。）
            if not shutdown_pending and os.path.exists(CANCEL_FLAG):
                try:
                    os.remove(CANCEL_FLAG)
                    log('清掉一个陈旧的「取消这次关机」标记（当时并没有倒计时在跑）')
                except Exception:
                    pass
            if not conf.get('enabled'):
                log('配置里 enabled=false → 退出守护')
                cleanup()

            now = time.time()
            cycles += 1
            sig, busy, prev = probe(wb_home, conf, prev)

            if busy:
                last_busy_at = now
                if busy_since <= 0:
                    busy_since = now
                ever_busy = True
            else:
                busy_since = 0.0

            # ---- 「先别睡」申请：开关里任何一项变了就重来一次 ----
            #   ★ 2026-09-29：真值源换成 5 个开关（见 feats()）；level 只是派生视图。
            F = feats(conf)
            lvl = F['level']
            lid_ctx['displayOn'] = bool(F.get('lidDisplayOn'))
            # 「没在忙」时也维持申请：这是**整个会话周期**的意图，
            # 不是「这一刻忙」才顶 —— 否则任务间隙（几十秒）就会放开、睡过去。
            want = lvl

            # ★★ 2026-10-01 学 PommeToys 的 `KeepAwakePolicy.shouldRefuseStart(battery:
            #   ruleEnabled:)`：电量**已经**低于阈值（在用电池）→ 这一刻**根本不顶**，
            #   连断言都不建；但**绝不改配置** —— 插上电源时 IOPS 会推事件
            #   （start_pwr_event_watch）→ 下一轮 want 自己回到 1，断言自动回来。
            #   PommeToys 的原话：「Battery is too low to start.」
            refuse = ''
            if want > 0 and F.get('awake') and F.get('lowBattStop'):
                refuse = low_batt_reason(conf, now)
                if refuse:
                    want = 0
            want_kind = _awake_kind(bool(F.get('keepDisplayOn'))) if want > 0 else None
            # ⚠️ 判据必须带上「计划」，不能只看 level（见上面 _applied_argv 的说明）：
            #   「合盖也不睡 + 屏幕常亮」与「合盖也不睡」的 level 都是 3，计划却不同。
            if want != applied or want_kind != _applied_argv[0]:
                if want > 0:
                    applied = suppress(want, proc_holder, bool(F.get('keepDisplayOn')))
                    _applied_argv[0] = want_kind if applied else None
                else:
                    release(proc_holder, quiet=True)
                    applied = 0
                    _applied_argv[0] = None
                if F.get('lidClose') and applied:
                    okk, err = lid_apply_off(conf)
                    if not okk:
                        log('「合盖也不睡」没改成（%s）。其余仍然生效（不睡）。' % err)
                elif not F.get('lidClose') or not applied:
                    # ★ 2026-10-01：`or not applied` 这一支是为「低电量暂停」补的 ——
                    #   原来只有「关掉合盖开关」时才会写回；现在撤断言也可能发生在
                    #   合盖开关**还开着**的时候（电量低）→ 不写回的话 `SleepDisabled`
                    #   会留在 1，合盖照样不睡，暂停就白暂停了。
                    lid_restore_conf(quiet=bool(F.get('lidClose')))
                    lid_ctx.clear()      # 离开「合盖也不睡」→ 合盖熄屏的记事本也清掉
                    lid_ctx['displayOn'] = bool(F.get('lidDisplayOn'))
            else:
                applied = lvl if want > 0 else 0

            # ★★ 2026-09-29 删掉一段「合盖设置漂移自愈」（用户点名要删）：
            #   原来是每轮读一次 `lid_read()`，发现 `SleepDisabled` 被改回 0
            #   就重新写 1 —— 为了防「用户中途自己把它改回去」。
            #   用户判断：这个不需要，删掉，痕迹也删掉。
            #   ⇒ 现在守护在跑期间 **不再每轮读睡眠设置**；`lid_read()` 只剩
            #     「决定要不要记原值 / 停止监测时要不要写回」这一个用途（见 lid_apply_off）。
            #   ⚠️ 知情副作用：守护在跑时若 `SleepDisabled` 被别的程序改回 0，
            #     工具**不再自动修复**，合盖就不保活 —— 直到你重新点一次那个开关。

            # ---- 电量不足时自动停（学 PommeToys 的 `lowBatteryStop`）----
            #   ★★ 2026-10-01 改（用户点名「参考他是怎么做的」）：
            #     原来这里是 `do_autostop()` + `cleanup()` —— **把开关也关掉、守护直接退出**。
            #     那不是 PommeToys 的行为：它是 `shouldAutoStop` ⇒ 结束**这一程**，
            #     设置留着；插上电源 `revalidate` 一跑，自己就回来了。
            #     原来的坏处很具体：夜里电量掉到阈值以下 → 开关被悄悄关掉、守护退出；
            #     早上插上电接着跑长任务，电脑睡了、任务断了，而界面上看不出是谁关的。
            #   ⇒ 现在：want 已在上面被拒（断言已撤、合盖设置已还），**配置一个字不动**，
            #     守护继续守着；插上电源 → IOPS 事件 → 下一轮自动恢复。全程不用你动手。
            if refuse:
                set_end_reason('lowBattery', refuse)
                if now - float(_LOWBATT.get('saidAt') or 0) >= 300:
                    _LOWBATT['saidAt'] = now
                    log('电量不足（%s）→ 暂停顶住睡眠；开关保持开着，插上电源自动恢复'
                        % refuse)
            elif _END.get('kind') == 'lowBattery':
                clear_end_reason()
                log('电量已回到阈值以上 → 恢复顶住睡眠（开关一直是开着的）')

            # ---- 到点自动结束（与「跑完自动关机」互斥）----
            #   ★ 放在关机之前：「结束」在语义上优先于「关机」；两者互斥，
            #     能走到这里顶多只有一个是开的。
            stop_now, stop_why = autostop_decision(conf, now)
            if stop_now:
                do_autostop(conf, idle_for=now - last_busy_at, busy=bool(busy))
                cleanup()      # 内部：撤抑制 + 写回合盖设置 + 落状态 + sys.exit
            elif stop_why and conf.get('autoStopAt') and (cycles % 40 == 0):
                log('到点自动结束暂未触发：%s' % stop_why)

            # ---- 关机：倒计时管理 ----
            idle_for = now - last_busy_at
            if shutdown_pending:
                # ★★ 三道复查（2026-09-28 加，用户报「倒计时时再点跑完自动关机没用」）。
                #   根因：shutdown_pending 是**内存变量**，进了倒计时就再也不看配置，
                #   于是「关开关」「点取消」全都拦不下，到点照样关机。
                #   顺序有意为之：先看用户的显式动作，再看任务是否又开跑。
                if os.path.exists(CANCEL_FLAG):
                    try:
                        os.remove(CANCEL_FLAG)
                    except Exception:
                        pass
                    log('倒计时期间收到「取消这次关机」标记 → 已取消（开关未动）')
                    shutdown_pending = False
                    shutdown_at = 0.0
                    last_shutdown_try = now
                elif not conf.get('autoShutdown'):
                    log('倒计时期间检测到「跑完自动关机」已被关掉 → 取消这次自动关机')
                    shutdown_pending = False
                    shutdown_at = 0.0
                    last_shutdown_try = now
                elif busy:
                    # ★ 倒计时期间又有任务开跑 → 取消这次关机。
                    #   这是安全兜底：用户完全可能在倒计时里点开一个新任务。
                    log('倒计时期间又检测到任务在跑 → 取消这次自动关机')
                    shutdown_pending = False
                    shutdown_at = 0.0
                    last_shutdown_try = now
                elif now >= shutdown_at:
                    okk, why = do_shutdown()
                    shutdown_pending = False
                    shutdown_at = 0.0
                    last_shutdown_try = now
                    if not okk:
                        log('关机没能执行：%s' % why)
                        st = read_state()
                        st['shutdownError'] = why
                        write_state(st)
                    else:
                        log('已按「跑完自动关机」执行关机倒计时到点')
            else:
                unknown_for = float(sig.get('unknownFor') or 0.0)
                fire, why, kind = shutdown_decision(
                    conf, idle_for, now, last_shutdown_try, shutdown_pending,
                    unknown_for=unknown_for, last_busy_at=last_busy_at)
                if fire:
                    delay = _cfg_num(conf, 'shutdownDelaySec', 180)
                    shutdown_pending = True
                    shutdown_at = now + delay
                    if kind == 'unknown':
                        log('任务信号已持续 %s 读不到 → 兜底：%s 后自动关机'
                            % (_dur(unknown_for), _dur(delay)))
                    elif _DRY:
                        log('任务已结束（去抖后）→ 【演练】本应触发自动关机（%s 后）'
                            % _dur(delay))
                    else:
                        log('任务已结束（去抖后）→ %s 后自动关机' % _dur(delay))
                elif why and (now - why_at >= 300):
                    # 按时间节流（见初始化处 beat_at 的说明）
                    why_at = now
                    log('自动关机暂未触发：%s' % why)

            # ---- 落状态（界面读这个文件） ----
            # ★ 第四十八轮：每轮读一次「任务事件源」的健康状况（纯内存读，零开销），
            #   带进状态文件 → --status 与界面都能看到它到底在不在盯、叫醒过多少次。
            _tok, _twy, _tcnt, _tat = task_event_state()
            st = {
                'pid': os.getpid(),
                'heartbeatAt': now,
                # ★★★ 2026-10-13：声明「本轮心跳最多允许隔多久」（见 state_alive）——
                #   睡多久就报多大：忙时 120（与改前一致）、闲时 660，闲时那 300 秒
                #   一轮不会再被判成「守护没在跑」。
                'hbMax': hb_max_for(_next_nap(conf, busy, shutdown_pending, shutdown_at)),
                'startedAt': st_started(),
                'level': lvl,
                'applied': applied,
                # ★ 2026-09-29：5 个开关的真值（界面按它们画开关；level 只是派生）
                'feat': {k: bool(F.get(k)) for k in _FEAT_KEYS},
                'awake': bool(F.get('awake')),
                'keepDisplayOn': bool(F.get('keepDisplayOn')),
                'lidClose': bool(F.get('lidClose')),
                'lidDisplayOn': bool(F.get('lidDisplayOn')),
                'lowBattStop': bool(F.get('lowBattStop')),
                'admin': os.geteuid() == 0 if hasattr(os, 'geteuid') else False,
                'busy': bool(busy),
                'idleFor': round(idle_for, 1),
                'lastBusyAt': last_busy_at,
                'busySince': busy_since,
                'everBusy': bool(ever_busy),
                'signals': sig,
                'autoShutdown': bool(conf.get('autoShutdown')),
                'shutdownPending': bool(shutdown_pending),
                'shutdownAt': shutdown_at,
                # ★ 2026-10-11（A6）：第三态「未知」——与忙/闲并列，界面据此报警。
                'unknown': bool(sig.get('unknown')),
                'unknownFor': round(float(sig.get('unknownFor') or 0.0), 1),
                'unknownWhy': ((sig.get('hermesWhy') or '') if sig.get('unknown') else ''),
                'autoStopAt': _cfg_num(conf, 'autoStopAt', 0),
                'lidChanged': bool(conf.get('lidChanged')),
                'lidClosed': lid_ctx.get('lid'),
                'lidScreenOff': int(lid_ctx.get('off_n') or 0),
                'extDisplay': lid_ctx.get('ext'),
                # 合盖事件监听（2026-09-28）：用的是事件还是兜底轮询，一眼可辨。
                # 若 lidEvents 长期是 0 而用户确实合过盖 → 这条路在本机不成立，
                # 那是关键现场，比「猜」强。
                'lidEventOk': bool(_LID_EVENT.get('ok')),
                'lidEvents': int(_LID_EVENT.get('count') or 0),
                'lidEventAt': float(_LID_EVENT.get('lastAt') or 0),
                # 事件线程是否还活着 —— 「订阅成功」和「线程真的在跑」是两件事：
                # CFRunLoop 线程要是早就退了，订阅对象还在，但永远不会有回调。
                # 有这个字段就能一眼分清（不然只能靠猜）。
                'lidEventThread': any(t.name == 'wb-lid-event'
                                      for t in threading.enumerate()),
                'lidEventWhy': (_LID_EVENT.get('why') or '')[:120],
                # ★ 2026-09-29：电量/电源事件（IOPS）—— 用法同上面那组 lidEvent*：
                #   pwrEvents 长期是 0、而用户确实插拔过电源 → 这条路在本机不成立。
                'pwrEventOk': bool(_IOPS_EVENT.get('ok')),
                'pwrEvents': int(_IOPS_EVENT.get('count') or 0),
                'pwrEventAt': float(_IOPS_EVENT.get('lastAt') or 0),
                # 事件线程是否还活着（同 lidEventThread 的理由：订阅成功 ≠ 线程还在）
                'pwrEventThread': any(t.name == 'wb-pwr-event'
                                      for t in threading.enumerate()),
                'pwrEventWhy': (_IOPS_EVENT.get('why') or '')[:120],
                # 这个进程能不能调 CoreGraphics（读屏状态 / 外接屏）。守护如果在受限
                # 进程树里，这里会是 false —— 熄屏本身不受影响，只是复核那一步失灵。
                'cgOk': _cg() is not None,
                'cycles': cycles,
                'toolVersion': TOOL_VERSION,
                'platform': 'mac',
                # ★ 第四十八轮：「任务在不在跑」的事件源状况（见 start_task_event_watch）
                'taskEvent': {'ok': _tok, 'why': _twy, 'n': _tcnt, 'at': _tat},
                # ★★ 第六十二轮（学 PommeToys）：
                #   · awakeBackend —— 顶住睡眠走的是电源断言还是 caffeinate 子进程；
                #   · endKind/endWhy/endAt —— 上一次「为什么停」（电量不足 / 到点）。
                'awakeBackend': _awake_backend(),
                'endKind': _END.get('kind') or '',
                'endWhy': (_END.get('why') or '')[:120],
                'endAt': float(_END.get('at') or 0),
            }
            write_state(st)

            # 心跳日志（**按时间** 5 分钟一行，便于事后回看它在不在岗）
            # ★ 改前是「每 20 轮」——一轮固定 15 秒时正好 5 分钟；现在闲时一轮
            #   300 秒，按轮数会变成 100 分钟一行，等于没有。所以按时间判。
            if now - beat_at >= 300:
                beat_at = now
                log('在守：%s 忙=%s（%s）idle=%s 顶睡眠=%s（%s）任务事件=%s'
                    % (feat_desc(F), busy, sig.get('runWhy') or '-', _dur(idle_for),
                       'on' if (proc_holder[0] and proc_holder[0].poll() is None) else 'off',
                       _awake_backend(),
                       ('已就位·累计 %d 次' % _tcnt) if _tok else ('不可用（%s）' % (_twy or '?'))))
        except Exception as e:
            log('守护主循环出错（继续守）：%r' % (e,))

        # ★ 档位三守候时把这一轮的 sleep 切成小片：合盖后 3 秒内就熄屏，
        #   而不是等满 15 秒那一轮（见 lid_screen_guard）。其它档位就是普通 sleep。
        # ★ 档位三守候时把这一轮的 sleep 交给「合盖事件 + 低频兜底」：
        #     · 事件就绪 → 兜底间隔放到 LID_EVENT_FALLBACK_SEC（5 秒），
        #       合盖那一瞬间系统会把这一觉叫醒，所以体感是「瞬间」；
        #     · 事件没起来 → 回到 LID_SCREEN_TICK（2 秒）保底，功能不受影响。
        #   其它档位（0/1/2）不用管合盖，就是一次普通 sleep —— 不切片、不等待事件。
        # ★★ 2026-09-29：这一觉不再固定 15 秒 —— 「到点自动结束」和「关机倒计时」
        #   都是**准点的事**，睡到那个点就醒比「每 15 秒才发现到点了」准得多。
        #   （用户问「到点为什么还要轮询」：以前确实白等，一秒不差不可能，但要睡够。）
        #   下限 1 秒：万一某个时刻已经过去而决策函数还没放行，不至于变成空转。
        # ★★★ 2026-10-01（第四十八轮 · 用户第 4 条）：底数从「固定 15 秒」改成**分档**：
        #     忙 → TASK_ACTIVE_SEC（15s，与改前一致，手感不变）；
        #     闲 → TASK_IDLE_SEC（300s，一天从 5760 次醒降到 288 次）。
        #   ★ 而且这两个数**都只是兜底** —— 真正的唤醒来自 `_LID_WAKE`：
        #     db-wal / 会话文件一被写、点开关（SIGUSR1）、合盖、插拔电源，都会立刻叫醒。
        #   ★ 所以「闲时 300 秒」**不等于**「要等 300 秒才发现任务跑完了」：
        #     任务一结束，status 写回 → WAL 事件 → **当场**醒。
        nap = _next_nap(conf, busy, shutdown_pending, shutdown_at)
        # ★ 始终挂 wake：这样「点停止 / 点取消」的 SIGUSR1 能立刻把这一觉叫醒。
        _lc = bool(F.get('lidClose') and applied)
        _step = (LID_EVENT_FALLBACK_SEC if _LID_EVENT.get('ok') else LID_SCREEN_TICK) if _lc else None
        _sleep_loop(nap, (lambda: lid_screen_guard(lid_ctx)) if _lc else None,
                    wake=_LID_WAKE, step=_step)
        # ★★ 2026-10-12：兜底自愈（见 lid_screen_selfheal 的说明）——
        #   管「被强杀重启 / 合盖期间关掉该功能 / 事件那路失灵」这几种主路径够不着的情形。
        lid_screen_selfheal(log)


_STARTED = [0.0]


def st_started():
    if not _STARTED[0]:
        _STARTED[0] = time.time()
    return _STARTED[0]


def start_daemon(quiet=False):
    """确保守护在跑（幂等）。"""
    st = read_state()
    if state_alive(st):
        if not quiet:
            log('守护已在跑（pid=%s）' % st.get('pid'))
        return True
    if os.path.exists(STOP_FLAG):
        try:
            os.remove(STOP_FLAG)
        except Exception:
            pass
    py = _python_exe()
    me = os.path.abspath(__file__)
    try:
        env = dict(os.environ)
        env['PYTHONUNBUFFERED'] = '1'
        subprocess.Popen([py, me, '--daemon'],
                         cwd=HERE, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
        log('拉起守护失败：%r' % (e,))
        return False
    for _ in range(30):
        time.sleep(0.4)
        if state_alive(read_state()):
            if not quiet:
                log('守护已拉起（pid=%s）' % read_state().get('pid'))
            return True
    log('守护拉起后 12 秒内没看到心跳')
    return False


# ======================================================== 命令

def _menu_hint():
    """未开启时那句「去哪儿打开」的提示。

    ★★ 2026-09-29：这里原来写死「8 个条目」。这一轮运行监测重排成 7 项之后，
      那半句话就成了**错的**（而且它不报错 —— 本项目的教训：「人话里的数字」
      最容易漏改，体检脚本②组专抓这个）。
      现在从 `wb_menu.GROUPS` 里现数；数不出来就只说板块名，**绝不留错数字**。
      判据：凡是「菜单里 × 项 / 编号 X~Y」这种句子，一律现算，不许手写。
    """
    try:
        import wb_menu as M
        for gkey, title, keys in M.GROUPS:
            if gkey == 'power':
                return '菜单里的「%s」板块（%d 个条目）可以打开' % (
                    title.split('（')[0].strip(), len(keys))
    except Exception:
        pass
    return '菜单里的「运行监测」板块可以打开'


def state_line():
    """一行摘要（给菜单首屏）。纯只读。

    ★ 与 Win 版同款，只把「去哪儿打开」那句提示换成 Mac 的说法 ——
      两端的运行监测**编号不同**（Win 是 11~16，Mac 是板块前缀那套 power1~power7），
      这是**唯一**该分叉的地方。
      ⚠️ 2026-09-29 修：那句提示里的条目数原来写死成 8，重排后成了错的；
        现在由 _menu_hint() 从菜单表现数（见那里的说明）。
    """
    conf = load_conf()
    st = read_state()
    if not conf.get('enabled'):
        return '未开启  ← %s' % _menu_hint()
    alive = state_alive(st)
    F = feats(conf)
    fd = feat_desc(F)
    if not alive:
        return ('配置为「%s」，但守护没在跑 ← 点一下「防休眠」开关，'
                '或打开「跑完自动关机」再关一次，它会自己把守护拉起来' % fd)
    sig0 = st.get('signals') or {}
    # 一行摘要也只给两态 —— 正在运行 / 已停止运行（用户 2026-09-27 明确）。
    # （旧状态文件没有 running 字段时退回看 busy，不至于报错。）
    if sig0.get('running', st.get('busy')):
        busy = '正在运行'
    else:
        busy = '已停止运行'
    extra = []
    _at = _cfg_num(conf, 'autoStopAt', 0)
    if _at:
        extra.append('到点结束 %s（还剩 %s）'
                     % (time.strftime('%H:%M', time.localtime(_at)),
                        _dur(max(0, _at - time.time()))))
    if conf.get('autoShutdown'):
        if st.get('shutdownPending'):
            left = max(0, int((st.get('shutdownAt') or 0) - time.time()))
            extra.append('关机倒计时 %d 秒（可以取消）' % left)
        else:
            extra.append('跑完自动关机')
    return '● %s · %s%s' % (fd, busy,
                            (' · ' + ' / '.join(extra)) if extra else '')


def cmd_status(full=False):
    """--status = 一行摘要（与 Win 版一致，菜单首屏用）；--status --full = 详细面板。"""
    if not full:
        _out(state_line())
        return 0
    conf = load_conf()
    st = read_state()
    alive = state_alive(st)
    F = feats(conf)
    lvl = F['level']
    _out('%s' % BAR)
    _out('  WorkBuddy 运行监测 · 状态（macOS）')
    _out('%s' % BAR)
    _out('  总开关        : %s' % ('开' if conf.get('enabled') else '关'))
    _out('  守护          : %s' % ('运行中 · pid %s' % st.get('pid') if alive
                                   else '没在跑'))
    _out('  当前组合      : %s' % feat_desc(F))
    _out('  开关明细      : %s' % ' '.join(
        '%s=%s' % (_FEAT_CN[k], '开' if F.get(k) else '关') for k in _FEAT_KEYS))
    _app = int(st.get('applied') or 0)
    _out('  实际已施加    : %s' % (feat_desc(feats({'level': _app})) if _app > 0 else '无'))
    # ★★ 2026-10-01（学 PommeToys）：
    #   · 顶睡眠的后端 —— 断言 / caffeinate 回退，一眼可辨；
    #   · 上一次「为什么停」—— 回答「昨晚防休眠是谁关的」这个问题。
    _out('  顶睡眠的后端  : %s' % (st.get('awakeBackend') or _awake_backend()))
    _n, _lines = assert_list()
    if _n is None:
        _out('  电源断言      : 数不到（`pmset -g assertions` 用不了）')
    elif _n:
        _out('  电源断言      : 系统里挂着 %d 条「%s」' % (_n, ASSERT_REASON))
    elif alive and F.get('awake'):
        _out('  电源断言      : ⚠️ 一条都没有，但「防休眠」是开的 —— 见 %s' % LOG_FILE)
    else:
        _out('  电源断言      : 没挂（现在没在顶睡眠）')
    if alive:
        _ek = st.get('endKind') or ''
        if _ek:
            _out('  上次为什么停  : %s' % (st.get('endWhy') or _ek))
            if _ek == 'lowBattery':
                _out('                  （开关还开着 —— 插上电源会自动恢复，不用手动点）')
    if alive:
        _out('  任务          : %s%s' % (
            '在跑' if st.get('busy') else '空闲',
            ('（%s）' % (st.get('signals') or {}).get('runWhy')) if st.get('busy') else
            ('（已静默 %s）' % _dur(st.get('idleFor') or 0))))
        _out('  会话文件      : %s 个，最近 %s 前写过'
             % ((st.get('signals') or {}).get('files'),
                ('%.0f 秒' % (st.get('signals') or {}).get('fileAge'))
                if (st.get('signals') or {}).get('fileAge') is not None else '?'))
        _out('  库里的 working: %s 个' % (st.get('signals') or {}).get('wbWorking'))
    _out('  跑完自动关机  : %s%s' % ('已开' if conf.get('autoShutdown') else '关',
                                   ('（倒计时中，还有 %.0f 秒）'
                                    % max(0, float(st.get('shutdownAt') or 0) - time.time()))
                                   if st.get('shutdownPending') else ''))
    _at = _cfg_num(conf, 'autoStopAt', 0)
    _out('  到点自动结束  : %s' % (
        ('已设 %s（还剩 %s）' % (time.strftime('%Y-%m-%d %H:%M', time.localtime(_at)),
                                 _dur(max(0, _at - time.time()))))
        if _at else '没设'))
    # ★ 2026-09-28 改文案：原来一律写「已关闭（档位三）」，但实测会误导 ——
    #   开关都关了、`SleepDisabled` 还是 1（那是 PommeToys 之类的程序设的），
    #   界面却说「档位三关的」，用户会以为「点回不抑制就该恢复」。
    #   现在按「是不是本工具设的」分开说（`lidChanged` 就是本工具的账本）。
    # ★★ 2026-09-29：这一行改成**现读一次**（`lid_read()`，0.0078 ms）。
    #   原来是读状态文件里的 `lidOff` 字段 —— 那个字段由主循环**每轮**写，
    #   也就是「每 15 秒读一次睡眠设置」的产物；用户点名把这个轮询删掉，
    #   字段连同一起删了。`--status` 是**一次性命令**，当场读一次最准，
    #   也顺带避免「状态文件是旧的、显示跟事实不符」。
    if lid_read() == 1:
        if st.get('lidChanged'):
            _out('  合盖睡眠      : 已关闭（合盖也不睡）· 本工具所设，停止监测时会写回')
        else:
            _out('  合盖睡眠      : 已关闭（合盖也不睡）· ⚠️ 不是本工具设的'
                 '（PommeToys 之类的程序），停监测不会把它关回来')
    else:
        _out('  合盖睡眠      : 正常（合盖会睡）')
    if F.get('lidClose'):
        if F.get('lidDisplayOn'):
            _out('  合盖熄屏      : 已改为「保持点亮」（PommeToys 子项开着）—— 注意散热')
        elif st.get('lidClosed') is True:
            _out('  合盖熄屏      : 合盖中 · 已下发 %s 次熄屏（后台照跑）'
                 % (st.get('lidScreenOff') or 0))
        else:
            _out('  合盖熄屏      : 待命（盖一合就熄屏；检测到外接屏会让位）')
    else:
        _out('  合盖熄屏      : 未启用（要开「合盖后保持唤醒」才轮到它）')
    # ★ 合盖检测靠的是「事件」还是「兜底轮询」——2026-09-28 加。
    #   这一行是排障的关键：lidEvents 一直是 0 而用户确实合过盖，
    #   就说明 IOKit 事件在本机不成立，得回头看兜底那一路。
    #   ⚠️ 数据一律从**状态文件**读（那是守护写的），别读本进程的 `_LID_EVENT` ——
    #      `--status` 是个刚起来的新进程，自己当然没有订阅，会把「已就绪」
    #      误报成「未启用」（这个错第一版就犯了，`--status` 跑完显示
    #      「事件监听未启用：未知」而状态文件里明明是 kr=0）。
    _ev_ok = st.get('lidEventOk')
    _ev_n = st.get('lidEvents') or 0
    _ev_why = st.get('lidEventWhy') or ''
    _ev_at = st.get('lidEventAt') or 0
    if _ev_ok:
        _th = ('事件线程在跑' if st.get('lidEventThread') else
               '⚠️ 事件线程不在（收不到通知，只能靠兜底轮询）')
        _out('  合盖检测      : 事件监听 + %d 秒兜底（%s；已收到 %s 次通知%s）'
             % (LID_EVENT_FALLBACK_SEC, _th, _ev_n,
                ('，最近 %s' % time.strftime('%H:%M:%S', time.localtime(_ev_at))) if _ev_at else ''))
    elif alive:
        _out('  合盖检测      : 每 %d 秒轮询（事件监听未启用：%s）'
             % (LID_SCREEN_TICK, _ev_why or '未知'))
    else:
        _out('  合盖检测      : 守护没在跑')
    # ★ 2026-09-29：电量/电源事件（IOPS）—— 同上面那一行的用法。
    #   这一行回答的是「电量掉到阈值 / 插拔电源，是**当场**知道还是最多晚 60 秒」。
    _p_ok = st.get('pwrEventOk')
    _p_n = st.get('pwrEvents') or 0
    _p_at = st.get('pwrEventAt') or 0
    if _p_ok:
        _pth = ('事件线程在跑' if st.get('pwrEventThread') else '⚠️ 事件线程不在')
        _out('  电源/电量检测 : IOPS 事件推送（%s；已收到 %s 次%s）'
             % (_pth, _p_n,
                ('，最近 %s' % time.strftime('%H:%M:%S', time.localtime(_p_at)))
                if _p_at else ''))
    elif alive:
        _out('  电源/电量检测 : 每 %s 秒问一次（事件订阅未启用：%s）'
             % (_cfg_num(conf, 'lowBattCheckSec', 60),
                (st.get('pwrEventWhy') or '未知')[:60]))
    else:
        _out('  电源/电量检测 : 守护没在跑')
    _out('  日志          : %s' % LOG_FILE)
    if full and alive:
        sig = st.get('signals') or {}
        _out('%s' % ('-' * 74))
        _out('  信号明细     : 文件=%s(%.0fs) working=%s running=%s 子进程忙=%s 兜底=%s'
             % (sig.get('file'), sig.get('fileAge') or 0, sig.get('wbWorking'),
                sig.get('wbRunning'), sig.get('childBusy'), sig.get('fallback')))
        for k in (sig.get('children') or [])[:8]:
            _out('      · %s' % k)
        # ★ 第四十八轮：这一行回答「它到底是按秒问，还是在等事件」
        _te = st.get('taskEvent') or {}
        if _te.get('ok'):
            _out('  任务事件源   : 已就位 —— %s（累计叫醒 %d 次）'
                 % (_te.get('why') or '', _te.get('n') or 0))
        else:
            _out('  任务事件源   : ✗ %s'
                 % (_te.get('why') or '守护还没上报（旧版本 / 刚启动）'))
    _out('%s' % BAR)
    return 0


def _read_bytes(path):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except Exception:
        return None


def heal_selftest_leftover(quiet=False):
    """把上一次「演练被强杀」留下的配置/状态还原回去（幂等，可随时调用）。

    ★ 为什么用**旁车文件**而不是把原内容存在内存里：演练可能被 SIGTERM / SIGKILL /
      终端关闭打断，那时 `finally` 根本不执行，内存里的备份随进程一起没了。
      先把原字节写进 `*.selftestbak`，下次开跑（或用户点任何入口）就能自愈。
      这也是「宁可不关机，也不能乱关机」的另一半 —— 闸门在 shutdown_decision。
    """
    healed = []
    for path, bak in ((CONF_FILE, CONF_SELFTEST_BAK), (STATE_FILE, STATE_SELFTEST_BAK)):
        blob = _read_bytes(bak)
        if blob is None:
            continue
        try:
            if blob == b'':
                if os.path.exists(path):
                    os.remove(path)
            else:
                with open(path, 'wb') as f:
                    f.write(blob)
            os.remove(bak)
            healed.append(os.path.basename(path))
        except Exception as e:
            if not quiet:
                _out('  ! 还原 %s 失败：%r' % (os.path.basename(path), e))
    # 万一旁车文件本身没了（被人手工删掉），只要看到标记就至少把开关收掉
    try:
        raw = load_conf_raw()
        if raw.get('selftestRun'):
            raw['selftestRun'] = False
            raw['autoShutdown'] = False
            raw['enabled'] = False
            clear_feats(raw)
            save_conf(raw)
            if 'selftestRun' not in ' '.join(healed):
                healed.append('（就地收掉演练残留标记）')
    except Exception:
        pass
    if healed and not quiet:
        _out('  已自愈上一次演练的残留：%s' % '、'.join(healed))
    return healed


def cmd_selftest_shutdown(wait_sec=180):
    """端到端演练「任务结束 → 自动关机」整条链路（**空跑，不会真关机**）。

    ★ 照 Win 版原样搬过来的，理由也一样值得记：
      用户「明明开了跑完自动关机，电脑却整夜没关」，而当时日志里**一行相关记录都没有**
      —— 只能靠翻系统日志 + 手工查进程树定位，花了一整轮。
      这条命令把当时缺的东西补齐：用**真守护主循环、真 probe()、真判定**跑一遍，
      只把「真的关机」换成空跑，并把宽容期 / 确认时长压小，十几秒就能看到结论。

    实现上有个反直觉的选择：**不另起进程，而是在本进程里用一个线程跑 daemon_main()**。
    照 start_daemon 那样 spawn 一个 `python --daemon` 的话，它起来几秒就会被连带清掉
    （宿主/沙盒对子进程的清理），演练必然假失败。放线程里跑既躲开这件事，
    又一样走的是真循环（只是 pid 记成本进程）。

    演练期间要临时关掉「会话文件在写」这条信号 —— **演练自己就在写会话文件**，
    不关的话 busy 恒为真，永远等不到「任务结束」。所以 activityWindowSec 临时设 0。

    判定标准（与 Win 一字不差）：日志里必须同时出现
    `【演练】本应触发自动关机` —— 即「任务结束 → 触发自动关机」这条链路的完整序列。
    跑完配置与状态逐字节还原。
    """
    global _DRY, POLL_SEC, TASK_IDLE_SEC, TASK_ACTIVE_SEC
    _out(BAR)
    _out('  演练：任务结束 → 自动关机（空跑，不会真的关机）')
    _out(BAR)

    if state_alive():
        _out('  ✗ 已有守护在跑 —— 先「停止监测」，再跑演练，免得两个守护互相打扰。')
        return 1

    # ★ 开跑前先自愈：上一次演练如果被强杀，磁盘上会留着「开关开着 + 只等 8 秒」的
    #   危险配置。不先收干净，这一轮的「备份」就会把脏配置当成原件存下来。
    heal_selftest_leftover(quiet=True)

    # ★ 清掉可能残留的停止标记：它与 start_daemon 的口径一致（「显式启动」即清除）。
    #   不清的话演练守护一进主循环就「看到停止标记 → 正常退出」，
    #   表现是「刚启动就退出」，非常容易被误读成代码坏了。
    if os.path.exists(STOP_FLAG):
        try:
            os.remove(STOP_FLAG)
        except Exception:
            pass

    bak_conf = _read_bytes(CONF_FILE)
    bak_state = _read_bytes(STATE_FILE)
    # 旁车落盘（不是只记在内存里）—— 见 heal_selftest_leftover 的说明
    for bak_path, blob in ((CONF_SELFTEST_BAK, bak_conf),
                           (STATE_SELFTEST_BAK, bak_state)):
        try:
            with open(bak_path, 'wb') as f:
                f.write(blob if blob is not None else b'')
        except Exception as e:
            _out('  ! 写演练备份失败：%r' % (e,))
    log_mark = len(_read_bytes(LOG_FILE) or b'')
    old_dry, old_poll = _DRY, POLL_SEC
    # ★ 第四十八轮：主循环的兜底底数改成 TASK_IDLE_SEC / TASK_ACTIVE_SEC 了，
    #   演练要一起压到 1 秒，否则整轮演练会从 ~15 秒变成 ~300 秒（必挂）。
    old_idle, old_active = TASK_IDLE_SEC, TASK_ACTIVE_SEC

    # 整份替换（我的 save_conf 是**整份覆盖**语义，不能像 Win 那样逐个键写）
    tmp_conf = dict(DEFAULTS)
    tmp_conf.update({
        'enabled': True,
        'level': 0,                 # ★ 演练不动电源状态（0 = 只监测、不抑制睡眠）
        'autoShutdown': True,
        'autoShutdownBootTs': boot_time(),   # 让「本次开机」那条检查通过
        # ★★ 危险标记：`shutdown_decision` 看到它就**拒绝真关机**。
        #    防的是「演练被强杀 → 配置留在只等 8 秒的状态 → 新守护真关机」。
        #    收尾时连同旁车备份一起清掉；真被留下也会被 heal_selftest_leftover 收走。
        'selftestRun': True,
        'idleConfirmSec': 8,        # 压实：正常 300 秒
        'shutdownDelaySec': 60,
        'activityWindowSec': 0,     # ★ 演练自己就在写会话文件，这条必须临时关掉
        # ★★ 库信号也必须临时关掉 —— 这是 Mac 上跑出来的新认识（2026-09-27）：
        #    `sessions.status='working'` 说的是「**这个会话**正在干活」，而演练
        #    恰恰是从会话里点出来的 → 库信号恒为忙 → idleFor 永远是 0 → 等不到
        #    「任务结束」，演练必然假失败（实测卡了 2 分钟一行都没等到）。
        #    Win 版没这一条，是因为它多半是在**没有活跃会话**的终端里跑的；
        #    我们的入口在界面/菜单上，永远带一个活跃会话，所以必须显式排除。
        #    演练要验的是「判闲之后 → 先整合 → 再关机」这条链路，
        #    不是「库信号好不好用」（那个用 --status 看「库里的 working」即可）。
        'useWbDbSignal': False,
        # ★ 2026-10-11：**同理关掉 Hermes 那一路** —— 演练是从 Hermes 里发起的，
        #   本会话自身就持着一条未过期的轮次租约 → hermesBusy 恒为真 → idle 永远 0
        #   → 永远到不了触发点（实测日志：「忙=True（Hermes 0 秒前刚活动）」）。
        #   2026-10-09 加 Hermes 信号时漏了这里 ⇒ 演练从那天起就跑不通。
        'useHermesSignal': False,
        'useCpu': False,
        'useChildProcess': True,
        'childFreshSec': 0.01,      # 不靠「新建」（本进程自己就是新起的）
        'childCpuEps': 1000,        # 不靠 CPU（免得某个 helper 偶发占点 CPU 干扰）
        'childStaleGraceSec': 4,    # 只留宽容期这条：4 秒后残留子进程即被甩掉
        'childRateMin': 1e9,        # 不靠子进程速率（改由 FORCE_BUSY 钩子给「忙」）
        # ★ 2026-10-10：「关机前整合」已删（那是任务管家的功能，时间守护不该有）。
    })

    ok = False
    th = None
    old_fb = os.environ.get('WB_POWERGUARD_FORCE_BUSY')
    try:
        save_conf(tmp_conf)
        _DRY = True                 # 关机 / pmset / caffeinate 只记日志、不真执行
        POLL_SEC = 1                # 把轮询压到 1 秒：整轮演练约 15 秒
        # ★ 第四十八轮：真正生效的是这两个（POLL_SEC 已降级为「忙时兜底」的旧名）。
        #   两个都压到 1 秒，演练节奏与改前完全一致。
        TASK_IDLE_SEC = 1
        TASK_ACTIVE_SEC = 1
        # 判据改成「速率」后，安静的常驻 helper 不再算忙，演练就没有了「先忙起来」的
        # 来源 → 用显式钩子强制判忙 8 秒（够把 ever_busy 那个循环状态走一遍），
        # 之后自动过期 → 转闲 → 攒满 idleConfirmSec 就触发关机。
        os.environ['WB_POWERGUARD_FORCE_BUSY'] = str(time.time() + 8)
        th = threading.Thread(target=daemon_main, name='pg-selftest', daemon=True)
        th.start()

        _out('  已在进程内启动真守护循环（每 1 秒探一次，空跑模式）...')
        _out('  并强制判忙 8 秒，之后自动转闲、进入确认倒计时。')
        _out('  下面这些带「|」前缀的行，是守护自己写进日志的原话：')

        deadline = time.time() + wait_sec
        while time.time() < deadline:
            time.sleep(0.5)
            txt = ((_read_bytes(LOG_FILE) or b'')[log_mark:]).decode('utf-8', 'replace')
            if '【演练】本应触发自动关机' in txt:
                ok = True
                break
            if not th.is_alive():
                _out('  ! 演练守护循环自己退出了，日志见下。')
                break

        txt = ((_read_bytes(LOG_FILE) or b'')[log_mark:]).decode('utf-8', 'replace')
        _out(BAR)
        for ln in txt.strip().splitlines()[-20:]:
            _out('  | ' + ln)
        _out(BAR)
    finally:
        if old_fb is None:
            os.environ.pop('WB_POWERGUARD_FORCE_BUSY', None)
        else:
            os.environ['WB_POWERGUARD_FORCE_BUSY'] = old_fb
        try:
            with open(STOP_FLAG, 'w', encoding='utf-8') as f:
                f.write('selftest')
        except Exception:
            pass
        # ★ 先把 conf 里的 enabled 收掉，避免线程退出时 cleanup 又写一遍状态文件
        try:
            c = load_conf_raw()
            c['enabled'] = False
            clear_feats(c)
            save_conf(c)
        except Exception:
            pass
        if th is not None:
            th.join(timeout=POLL_SEC + 8)
            if th.is_alive():
                _out('  ! 演练守护线程没能停下来 —— 仅本进程内，退出即结束。')
        try:
            os.remove(STOP_FLAG)
        except Exception:
            pass
        release([None], quiet=True)
        _DRY, POLL_SEC = old_dry, old_poll
        TASK_IDLE_SEC, TASK_ACTIVE_SEC = old_idle, old_active
        # 逐字节还原配置与状态，并删掉旁车备份（顺序反过来：先还原再删备份）
        for path, blob, bak_path in ((CONF_FILE, bak_conf, CONF_SELFTEST_BAK),
                                     (STATE_FILE, bak_state, STATE_SELFTEST_BAK)):
            try:
                if blob is None:
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    with open(path, 'wb') as f:
                        f.write(blob)
                if os.path.exists(bak_path):
                    os.remove(bak_path)
            except Exception as e:
                _out('  ! 还原 %s 失败：%r' % (os.path.basename(path), e))
        try:
            os.remove(INST_FILE)
        except Exception:
            pass

    if ok:
        _out('  ✓ 通过：任务结束 → 触发自动关机')
    else:
        _out('  ✗ 未通过：%d 秒内没看到「【演练】本应触发自动关机」这一行，'
             '把上面这段日志发出来' % wait_sec)
    return 0 if ok else 1


def cmd_selftest():
    """只读自检：把每一条依赖都跑一遍，报「能不能用」。"""
    _out('%s' % BAR)
    _out('  运行监测 · 自检（只读，不改任何东西）')
    _out('%s' % BAR)
    for name, path in (('caffeinate', CAFFEINATE), ('pmset', PMSET),
                       ('osascript', OSASCRIPT), ('ps', PS)):
        _out('  %-12s %s' % (name, ('有 · %s' % path) if os.path.exists(path) else '✗ 找不到'))
    _out('  %-12s %s' % ('libproc', '可用 · proc_listpids/proc_pidinfo'
                         if _libc() is not None else '✗ 不可用（会退回 ps）'))
    wh = wb_home_of()
    _out('  %-12s %s' % ('~/.workbuddy', wh if os.path.isdir(wh) else '✗ 找不到'))
    w, r, ok, why = probe_wb_tasks(load_conf(), wh)
    _out('  %-12s %s' % ('读库 status', ('working=%s running=%s' % (w, r)) if ok else ('✗ ' + why)))
    age, n = scan_projects_mtime(wh)
    _out('  %-12s %s' % ('会话文件', ('%s 个，最近 %s 前' % (n, '%.0fs' % age if age is not None else '?'))))
    rows = list_processes()
    how = ('libproc' if _libc() is not None and all_pids() else 'ps')
    _out('  %-12s %s' % ('进程枚举', ('OK(%s) · 共 %d 个进程，WorkBuddy 相关 %d'
                                      % (how, len(rows), len(wb_pids(rows))))
                         if rows else '✗ 失败（这一路信号会用不上）'))
    # 后代里「可能算在干活」的，列出来给用户过目 —— 常驻 helper 不该出现在这里
    kids_desc = []
    if rows:
        try:
            kids_desc, _busy0, _p = child_probe(rows, {}, load_conf())
        except Exception:
            kids_desc = []
        if kids_desc:
            _out('  %-12s %d 个 → %s' % ('候选子进程', len(kids_desc),
                                         '、'.join(kids_desc[:4]) + ('…' if len(kids_desc) > 4 else '')))
        else:
            _out('  %-12s %s' % ('候选子进程', '无（说明 WorkBuddy 常驻 helper 已被正确排除）'))
    _out('  %-12s %s' % ('WorkBuddy', '在跑' if wb_running() else '没在跑'))
    # ★ 2026-09-29：原来这里还有一行 `合盖设置 SleepDisabled=` ——
    #   它是「每 15 秒读一次睡眠设置」那套的最后一处痕迹，用户点名删掉。
    #   要看这个值请走 `--selftest-lid`（那里是**合盖链路的专属自检**，
    #   读的是同一个值，但语义明确：它属于合盖那一件事）。
    _out('%s' % BAR)
    return 0


def cmd_selftest_lid(apply=False):
    """实测「合盖熄屏」这条链路的每一环（默认只读，加 --apply 才真熄一次屏）。

    为什么要能单独测：这条链路的真验收场景是「合上盖子」，人不在跟前没法随时验；
    这里把四环拆开逐环打印判据 —— 哪一环坏了，一眼看出是谁：
      ① 读合盖状态（IOKit 直读，2026-09-28 起不再起 ioreg 进程）
      ② 有没有外接屏（CoreGraphics）
      ③ 熄屏命令能不能让主屏真的 asleep
      ④ 熄屏后能不能保持（有人碰键盘/触控板时会被唤醒，那是正常的）
      ⑤ 合盖事件监听在不在（2026-09-28 加，学 PommeToys）—— 读守护写的状态文件
    """
    _out('%s' % BAR)
    _out('  档位三 · 「合盖熄屏」链路实测（macOS）')
    _out('%s' % BAR)
    lc = lid_closed()
    _out('  ① 合盖状态    : %s' % ('合盖' if lc is True else
                                  ('开盖' if lc is False else '读不到（IOKit 与 ioreg 都不通？）')))
    # ④ 睡眠设置（原在 `--selftest` 里，2026-09-29 挪到这条合盖专属链路里）
    _sd = lid_read()
    _out('  ④ 睡眠设置    : SleepDisabled=%s %s' % (
        _sd, '（合盖也不睡已生效）' if _sd == 1 else
        ('（合盖会睡 —— 正常）' if _sd == 0 else '（读不到）')))
    ex = external_display()
    _out('  ② 外接显示器  : %s' % ('有（会主动让位，不去熄屏）' if ex else
                                  ('没有（内建屏自己扛）' if ex is False else '读不到')))
    # ⑤ 合盖事件监听（2026-09-28 加，学 PommeToys）—— 决定「合盖那一瞬间」
    #   能不能被系统**推**醒，而不是靠每 2 秒问一次。
    #   ⚠️ 数据读**守护写的状态文件**：本进程刚起来、自己当然没订阅。
    _st = read_state()
    if state_alive(_st):
        _ev_ok = _st.get('lidEventOk')
        _ev_n = _st.get('lidEvents') or 0
        _ev_at = _st.get('lidEventAt') or 0
        if _ev_ok:
            _out('  ⑤ 合盖事件    : 已订阅（累计收到 %s 次%s）'
                 % (_ev_n, ('，最近 %s' % time.strftime('%H:%M:%S', time.localtime(_ev_at)))
                    if _ev_at else ''))
            _out('     ↳ 要验它对「合盖」灵不灵：合一下盖子再看这一行 —— ')
            _out('        计数 +1 就说明合盖能推醒它（那 5 秒兜底就只是保险）')
        else:
            _out('  ⑤ 合盖事件    : 未订阅（%s）→ 退回每 %d 秒轮询，功能不受影响'
                 % ((_st.get('lidEventWhy') or '未知')[:60], LID_SCREEN_TICK))
    else:
        _out('  ⑤ 合盖事件    : 守护没在跑，看不出（先在菜单里点一个档位）')
    before = display_asleep()
    _out('  ③ 现在熄屏吗  : %s' % ('熄着' if before else
                                  ('亮着' if before is False else '读不到')))
    if not apply:
        _out('     （只读模式。要真测熄屏，加 --apply：屏幕会黑一下，动鼠标即恢复）')
        _out('%s' % BAR)
        return 0
    ok, err = display_off_now()
    if not ok:
        _out('  ✗ 熄屏命令失败：%s' % err)
        _out('%s' % BAR)
        return 4
    time.sleep(1.0)
    a1 = display_asleep()
    _out('  ✓ 已下发 pmset displaysleepnow；1 秒后主屏 = %s'
         % ('熄着（生效 ✓）' if a1 is True else
            ('又被点亮（有输入事件在发生）' if a1 is False else '读不到')))
    time.sleep(4.0)
    a2 = display_asleep()
    _out('    5 秒后主屏 = %s' % ('仍熄着（能保持 → 合盖后就稳）' if a2 is True else
                                  ('又被点亮（本机有东西在产生输入事件）' if a2 is False
                                   else '读不到')))
    _out('  判据：合盖之后没人碰键盘/触控板，这条链路就能一直保持熄屏；')
    _out('        想物理级断电（更凉更省电）→ 接假负载头，走《合盖模式指南》。')
    _out('%s' % BAR)
    return 0


def cmd_selftest_pwr():
    """实测「电量 / 电源变化事件（IOPS）」这条路：读数 + 事件订阅 + 耗时对比。只读。

    为什么要能单独测：这条路的真验收场景是「拔掉电源 / 电量掉到阈值」，
    人不在跟前没法随时验。这里把每一环拆开逐环打印判据：
      ① IOPS 直读出不出数（有电池 / 在用电池吗 / 电量% / 充电吗）
      ② 与 `pmset -g batt` 交叉核对（两路读数应当一致）
      ③ 耗时对比（直读 vs 起进程）—— 回答「为什么值得为它写一层 ctypes」
      ④ 事件源建不建得起来、事件线程在不在
      ⑤ ⚠️ 启动后**来不来初始回调**（实测不来 → 所以必须自己主动读一次）
      ⑥ 守护写的状态文件里，累计收到过多少次推送
    """
    _out('%s' % BAR)
    _out('电量 / 电源事件（IOPS）自检 · 只读 · 学 PommeToys')
    _out('%s' % BAR)
    if not IS_MAC:
        _out('  非 macOS —— 这条路不适用。')
        _out('%s' % BAR)
        return 0

    # ①② 直读 + 计时
    n = 200
    t0 = time.time()
    r = None
    for _ in range(n):
        r = iops_read()
    ms = (time.time() - t0) / n * 1000
    if r is None:
        _out('  ① IOPS 直读    : ✗ 不可用（%s）→ 正在用 pmset 回退，功能不受影响'
             % (_IOPS_WHY[0] or '未知'))
    else:
        _out('  ① IOPS 直读    : 有电池=%s 用电池=%s 电量=%s%% 充电中=%s'
             % (r[0], r[1], r[2], r[3]))
    _out('  ② 单次耗时     : %.4f ms / 次（%d 次均值）' % (ms, n))

    # ③ 与 pmset 交叉核对 + 起进程的代价
    rc, out = _run([PMSET, '-g', 'batt'], timeout=10)
    if rc == 0 and out:
        _out('  ③ 交叉核对     : pmset 说「%s」'
             % ' / '.join([x.strip() for x in out.splitlines() if x.strip()][:2]))
        t0 = time.time()
        for _ in range(5):
            _run([PMSET, '-g', 'batt'], timeout=10)
        pms = (time.time() - t0) / 5 * 1000
        _out('  ④ 起进程耗时   : %.2f ms / 次 → 直读快约 %.0f 倍、且不起进程'
             % (pms, (pms / ms) if ms > 0 else 0))
    else:
        _out('  ③ 交叉核对     : pmset 读不到（不影响功能）')

    # ⑤ 事件源
    ok = start_pwr_event_watch(quiet=True)
    _out('  ⑤ 事件订阅     : %s（%s）'
         % ('已就绪' if ok else '未启用', (_IOPS_EVENT.get('why') or '')[:70]))
    if ok:
        time.sleep(2.0)
        alive = any(t.name == 'wb-pwr-event' for t in threading.enumerate())
        _out('     · 事件线程   : %s' % ('在跑' if alive else '⚠️ 不在（收不到推送）'))
        _out('     · 启动 2 秒内收到 %d 次推送 —— %s'
             % (_IOPS_EVENT['count'],
                '正常：系统启动时**不补**初始回调，所以实现里自己读了初值'
                if _IOPS_EVENT['count'] == 0 else '（本机居然补了一次，也不影响）'))
        _out('     判据：插入/拔掉电源线，再跑一次这条命令 —— 计数应当 +1。')

    # ⑥ 守护那边收了多少
    st = read_state()
    if state_alive(st):
        _out('  ⑥ 守护侧       : 订阅=%s 收到=%s 次%s'
             % (st.get('pwrEventOk'), st.get('pwrEvents') or 0,
                ('，最近 %s' % time.strftime('%H:%M:%S',
                                             time.localtime(st.get('pwrEventAt') or 0)))
                if st.get('pwrEventAt') else ''))
        _out('     线程=%s' % ('在跑' if st.get('pwrEventThread') else '⚠️ 不在'))
    else:
        _out('  ⑥ 守护侧       : 守护没在跑，看不出（先在菜单里点一个开关）')
    _out('%s' % BAR)
    return 0


def cmd_level(level, toggle=False):
    conf = load_conf()
    # 「点档位 → 防休眠刚顶上又被撤掉」的元凶就是这个残留值，先清掉再说
    if clear_expired_autostop(conf):
        log('清掉一个「已过期却没清掉」的到点结束设定（上次写盘可能失败了）')
        _out('（顺带清掉一个过期的「到点自动结束」设定 —— 上次它没能写进配置）')
    # ★ 2026-09-29：不再无条件置 True。切到档位 0（＝一个开关都不开）而又没挂
    #   「跑完自动关机 / 到点自动结束」时，守护就该退 —— 见 need_daemon()。
    if toggle:
        cur = int(conf.get('level') or 0)
        level = 0 if cur == level else level
    level = max(0, min(3, int(level)))
    # ★ 2026-09-29：老命令 `--level 0|1|2|3` 保留且**行为不变**，内部就是映射成一组开关。
    #   档位一 = 主开关 + 屏幕常亮；档位二 = 只要主开关；档位三 = 再加「合盖也不睡」。
    write_feats(conf, {'awake': level > 0,
                       'keepDisplayOn': level == 1,
                       'lidClose': level == 3,
                       'lidDisplayOn': bool(conf.get('lidDisplayOn')),
                       'lowBattStop': bool(conf.get('lowBattStop'))})
    sync_enabled(conf)
    if level == 0:
        log('档位 → 不抑制（仅监测）')
    else:
        log('档位 → %s' % LEVEL_DESC.get(level, level))
    # 立即把合盖设置跟上（档位三的提权要在**前台**弹，用户才看得见）
    if level == 3:
        okk, err = lid_apply_off(load_conf())
        _out('合盖不睡：%s' % ('已生效' if okk else ('没改成 —— %s' % err)))
    elif level in (0, 1, 2):
        lid_restore_conf(quiet=True)
    _out('已切到：%s' % LEVEL_DESC.get(level, level))
    return 0


def cmd_toggle(name):
    """单项开关（PommeToys 那排开关）：界面与编号菜单都走这条。非交互。

    name ∈ awake / display / lowbat / lid / lid-display
    ★ 语义照 PommeToys：
      · 子项（display/lowbat/lid）点了而主开关是关的 → **自动把主开关打开**
        （不然点了没反应，用户会以为坏了）；
      · 关掉主开关时**不动子项** —— 子项状态被记住，下次开主开关自动恢复
        （PommeToys 也是：主开关一关，子项灰掉但不丢）。
    """
    conf = load_conf()
    if clear_expired_autostop(conf):
        log('（顺带清掉一个过期的「到点自动结束」设定）')
    key = {'awake': 'awake', 'display': 'keepDisplayOn', 'lowbat': 'lowBattStop',
           'lid': 'lidClose', 'lid-display': 'lidDisplayOn'}.get(name)
    if key is None:
        _out('不认识的开关：%s（可选 awake / display / lowbat / lid / lid-display）' % name)
        return 2
    F = feats(conf)
    F[key] = not F[key]
    auto = False
    if key != 'awake' and F[key] and not F['awake']:
        F['awake'] = True
        auto = True
    # ★★ 2026-09-29：总开关（enabled）改成**算出来的**，不再无脑置 True。
    #   判据 = 还有没有任何一件事需要守护去做（见 need_daemon / sync_enabled）。
    #   一件都没有 = 用户把开关全关了 → **让守护退出**，别挂着空转。
    #   （用户原话：「真正需要轮询监测的只有一个任务忙不忙的功能」——
    #     那这个功能都没人要了，就更不该占着一个常驻进程每 15 秒去查库、扫会话文件。
    #     实测踩到过：Smoke 往返「防休眠 开→关」之后，守护还在跑、心跳照写。）
    #   ⚠️ 开关的「姿势」要留着（PommeToys 也是灰掉不丢，下次开主开关自动恢复），
    #      所以这里**绝不能**顺手 clear_feats —— 那是「停止监测」才该干的。
    #   先把这一下点出来的新组合写回 conf，sync_enabled 才读得到（顺序不能反）。
    write_feats(conf, F)
    # ★★ 2026-09-30（用户：关了防休眠就不该再计时）：总开关一关，「到点自动结束」
    #   一并作废。这里**显式调一次是为了能告诉用户**（sync_enabled 里那道只是兜底、
    #   不吭声）—— 不然用户会以为自己的时长设定被吞了。
    dropped_as = drop_autostop_no_awake(conf)
    need = sync_enabled(conf)
    log('开关「%s」→ %s%s%s'
        % (_FEAT_CN.get(key, key), '开' if F[key] else '关',
           '' if need else '（已无事要守 → 停掉守护）',
           '；「到点自动结束」一并作废' if dropped_as else ''))
    # 合盖那一路要**在前台**把系统设置跟上（提权/写回都发生在这里，用户看得见）
    F2 = feats(load_conf())
    if F2.get('lidClose'):
        okk, err = lid_apply_off(load_conf())
        _out('合盖不睡：%s' % ('已生效' if okk else ('没改成 —— %s' % err)))
    else:
        lid_restore_conf(quiet=True)
    _out('「%s」已%s。当前组合：%s'
         % (_FEAT_CN.get(key, key), '打开' if F[key] else '关闭',
            feat_desc(feats(load_conf()))))
    if dropped_as:
        _out('  到点结束是「值守期间」的功能 —— 总开关一关，它已一并作废'
             '（时长滑块回到最右端「无限」）。')
    if not need:
        _out('  所有开关都已关闭、也没挂「跑完自动关机 / 到点自动结束」')
        _out('  → 运行监测**已自动停止**（不再常驻后台）。要用时点任一开关即可。')
    if auto:
        _out('（顺带把「防休眠」总开关打开了 —— 子项要先有主开关才生效）')
    if key == 'lowBattStop' and F[key]:
        _out('  生效条件：在用电池、且电量低于 %d%%（每 %d 秒看一次）。'
             % (int(_cfg_num(conf, 'lowBattPct', 10)), int(_cfg_num(conf, 'lowBattCheckSec', 60))))
    if key == 'lidDisplayOn' and F[key]:
        _out('  ⚠️ 合盖后屏幕**不会熄**（这是你要的语义），但笔记本合着盖还亮屏会发热。')
    if key == 'lidClose' and F[key]:
        _out('  仅笔记本有效；已装免密，不再弹密码框。')
    return 0


def cmd_shutdown_interactive():
    conf = load_conf()
    cur = bool(conf.get('autoShutdown'))
    _out('「任务全部结束后自动关机」现在是：%s' % ('已开' if cur else '关'))
    _out('  说明：它和档位**完全独立** —— 不选档位也照常跑完就关机。')
    _out('        本次开机里打开只对本次开机有效，机器一重启就是「关」。')
    try:
        ans = input('  %s？（y/N） ' % ('要关掉它' if cur else '要打开它')).strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ''
    if ans not in ('y', 'yes'):
        _out('  已取消。')
        return 0
    return cmd_shutdown_set(not cur)


def cmd_shutdown_set(on):
    conf = load_conf()
    clear_expired_autostop(conf)
    conf['autoShutdown'] = bool(on)
    mutex_off = False
    if on:
        conf['autoShutdownBootTs'] = boot_time()
        # ★ 互斥（用户 2026-09-28 定）：开「跑完自动关机」就自动关掉「到点自动结束」。
        if _cfg_num(conf, 'autoStopAt', 0):
            conf['autoStopAt'] = 0
            conf['autoStopSetAt'] = 0
            conf['autoStopDurationSec'] = 0
            conf['autoStopBootTs'] = 0
            mutex_off = True
    # ★ 2026-09-29：关掉它之后若一个开关都没开、也没有「到点自动结束」，
    #   守护就该退 —— 别再留一个只为了「盯着任务忙不忙」却根本没挂关机的进程。
    need = sync_enabled(conf)
    st = read_state()
    if not on:
        # ★ 关开关 = 用户不想关机了。必须同时告诉正在跑的守护（见 CANCEL_FLAG）——
        #   否则它内存里的 shutdown_pending 还在，到点照样关机。
        _signal_cancel()
        if st.get('shutdownPending'):
            st['shutdownPending'] = False
            st['shutdownAt'] = 0
            write_state(st)
    log('「跑完自动关机」→ %s%s' % ('开' if on else '关',
        '；已自动取消「到点自动结束」（两者互斥）' if mutex_off else ''))
    # 叫醒守护不用在这里做：sync_enabled() 已经戳过了；关的那一路里
    # `_signal_cancel()` 落完 CANCEL_FLAG 还会再戳一次（取消要秒级生效）。
    _out('「跑完自动关机」已%s。' % ('打开' if on else '关闭'))
    if not need:
        _out('  开关都没开、也没挂别的事 → 运行监测**已自动停止**（不再常驻后台）。')
    if on:
        # ★ 2026-10-11：不再是「静默满 X 再倒计时 Y」两段式。
        _out('  生效条件：任务跑完后先静默 %s（去抖），再倒计时 %s。'
             % (_dur(conf.get('idleConfirmSec') or 60),
                _dur(conf.get('shutdownDelaySec') or 180)))
        _unk0 = _cfg_num(conf, 'unknownShutdownSec', 1200)
        if _unk0 > 0:
            _out('  另外：任务信号若持续 %s 读不到，也会走兜底关机（与「跑完」无关）。'
                 % _dur(_unk0))
        if mutex_off:
            _out('  ⚠️ 两者互斥 —— 已自动取消「到点自动结束」。')
    return 0


def _poke_daemon():
    """把正在跑的守护**立刻叫醒**，不让它把这一觉睡完。

    ★ 2026-09-29 加，正面回答用户的质问：「点停止监测 / 取消这次关机，应该瞬时生效，
      不该算轮询」。原来是「落一个标记文件 → 等下一轮（≤15 秒）才发现」，
      用户体感就是"点了没反应"。现在落完标记再发 SIGUSR1：守护那个
      `_LID_WAKE`（threading.Event）立刻返回 → 主循环马上重读配置与标记 → 秒级生效。

    ⚠️ 为什么必须用 Event 而不是"信号打断 sleep"：CPython 按 PEP 475，被信号打断的
      `time.sleep()` 会**自动续睡剩余时间**，光发信号叫不醒它。Event.wait 则是
      「处理器里 set() → wait 立刻返回」，这条路才通。
    ⚠️ 守护没在跑 / 读不到 pid → 静默跳过（标记文件还留着，它下次起来照样认）。
    """
    try:
        pid = int((read_state() or {}).get('pid') or 0)
    except Exception:
        pid = 0
    if not pid or not _pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
        return True
    except Exception:
        return False


def _signal_cancel():
    """给正在跑的守护留一个「立刻取消这次倒计时」的标记文件。"""
    try:
        with open(CANCEL_FLAG, 'w', encoding='utf-8') as f:
            f.write(str(time.time()))
        _poke_daemon()      # ★ 叫醒守护 → 「取消这次关机」秒级生效，不等下一轮
        return True
    except Exception as e:
        log('写取消标记失败：%r' % (e,))
        return False


def cmd_cancel_shutdown():
    """只取消**这一次**倒计时，总开关不动。"""
    st = read_state()
    if not st.get('shutdownPending'):
        _out('当前没有正在倒计时的自动关机。')
        return 0
    # ★ 关键：光清状态文件**拦不住正在跑的守护**（它的 shutdown_pending 在内存里），
    #   必须同时落这个标记文件 —— 守护每轮都会看它。见 CANCEL_FLAG 的说明。
    _signal_cancel()
    st['shutdownPending'] = False
    st['shutdownAt'] = 0
    write_state(st)
    log('已取消这次的自动关机倒计时（总开关未动）')
    _out('已取消这次倒计时（「跑完自动关机」开关仍是开着状态）。')
    return 0


def cmd_autostop_set(sec):
    """打开「到点自动结束」，sec 秒后执行。

    ★★ 与「跑完自动关机」**互斥**（用户 2026-09-28 定）：开这个就自动关掉那个。
      理由：两者都是「这一轮跑完之后怎么收尾」的答案，同时开着用户自己都说不清
      电脑最后会怎么走；互斥之后语义唯一。
    """
    sec = int(sec)
    conf = load_conf()
    clear_expired_autostop(conf)
    had_shutdown = bool(conf.get('autoShutdown'))
    # ★★ 2026-09-30：到点结束**依附「防休眠」总开关** —— 总开关关着就先替用户打开。
    #   照 cmd_toggle 里子开关的老规矩（点了子项而主开关关着 → 自动开主开关）。
    #   非这样做不可：不打开的话，下面刚写下的 autoStopAt 会被 sync_enabled 里那道
    #   兜底立刻作废，用户看到的现象是「设了时长却什么都没发生」。
    F = feats(conf)
    auto_awake = not F['awake']
    if auto_awake:
        F['awake'] = True
        write_feats(conf, F)
    now = time.time()
    conf['autoStopAt'] = now + sec
    conf['autoStopSetAt'] = now
    conf['autoStopDurationSec'] = sec
    conf['autoStopBootTs'] = boot_time()
    conf['autoShutdown'] = False          # ★ 互斥
    # 挂了「到点自动结束」= 有事要守（要睡到点），sync_enabled 必然置 true。
    sync_enabled(conf)
    st = read_state()
    if st.get('shutdownPending'):
        # 开了「到点结束」也就等于「别再关机了」，一并把在跑的倒计时撤掉
        _signal_cancel()
        st['shutdownPending'] = False
        st['shutdownAt'] = 0
        write_state(st)
    log('「到点自动结束」→ 开（%s 后，即 %s）%s%s'
        % (_dur(sec), time.strftime('%H:%M', time.localtime(now + sec)),
           '；已自动关掉「跑完自动关机」（两者互斥）' if had_shutdown else '',
           '；已顺带打开「防休眠」' if auto_awake else ''))
    _out('「到点自动结束」已打开。')
    _out('  到点时刻：%s（%s 之后）'
         % (time.strftime('%Y-%m-%d %H:%M', time.localtime(now + sec)), _dur(sec)))
    _out('  到点时：撤掉「先别睡」→ 把合盖睡眠写回原值 → 停止监测，回到系统默认电源行为。')
    _out('  到点时若任务还在跑：按「硬切」约定撒手，日志里会写明。')
    if had_shutdown:
        _out('  ⚠️ 两者互斥 —— 已自动关掉「跑完自动关机」。')
    if auto_awake:
        _out('  （顺带打开了「防休眠」总开关 —— 到点结束只在**值守期间**计时，'
             '关掉它这个设定就作废。）')
    _out('  本次开机内有效，机器一重启就恢复成「没设」。')
    return 0


def cmd_autostop_off():
    """取消「到点自动结束」（档位与「跑完自动关机」都不动）。"""
    conf = load_conf()
    had = _cfg_num(conf, 'autoStopAt', 0)
    conf['autoStopAt'] = 0
    conf['autoStopSetAt'] = 0
    conf['autoStopDurationSec'] = 0
    conf['autoStopBootTs'] = 0
    # ★ 2026-09-29：取消之后若一个开关都没开、也没挂「跑完自动关机」，
    #   守护就没活干了 —— 让它退（sync_enabled 内部会 `_poke_daemon()`，
    #   所以「取消」依旧是秒级生效，不必等下一轮）。
    need = sync_enabled(conf)
    log('「到点自动结束」→ 关（原定 %s）'
        % (time.strftime('%H:%M', time.localtime(had)) if had else '未设'))
    _out('「到点自动结束」已取消（档位和「跑完自动关机」都没动）。')
    if not need:
        _out('  开关都没开、也没挂别的事 → 运行监测**已自动停止**（不再常驻后台）。')
    return 0


def cmd_autostop_interactive():
    """黑窗菜单里的交互入口：给几个快捷档。"""
    conf = load_conf()
    at = _cfg_num(conf, 'autoStopAt', 0)
    if at:
        _out('「到点自动结束」现在是：开（%s，还剩 %s）'
             % (time.strftime('%H:%M', time.localtime(at)),
                _dur(max(0, at - time.time()))))
    else:
        _out('「到点自动结束」现在是：没设')
    _out('  它和「跑完自动关机」**互斥** —— 设了它就会自动关掉那个。')
    _out('  到点后：撤掉「先别睡」、把合盖睡眠写回原值、停止监测（回到系统默认）。')
    _out('')
    _out('   [1] 30 分钟     [2] 1 小时     [3] 2 小时     [4] 4 小时')
    _out('   [c] 自定义（分钟，如 90）       [0] 取消        [q] 返回')
    try:
        ch = input('  选择 > ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        return 0
    table = {'1': 30 * 60, '2': 3600, '3': 2 * 3600, '4': 4 * 3600}
    if ch == '0':
        return cmd_autostop_off()
    if ch in ('q', ''):
        _out('  已取消。')
        return 0
    if ch == 'c':
        try:
            mins = input('  多少分钟？ > ').strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        sec, why = parse_duration(mins)
        if not sec:
            _out('  %s' % why)
            return 0
        return cmd_autostop_set(sec)
    if ch in table:
        return cmd_autostop_set(table[ch])
    _out('  不认识：%s' % ch)
    return 0


def cmd_stop():
    """停止监测：撤掉所有抑制，把合盖设置写回去。"""
    # 顺手把演练残留一起收干净 —— 「停止监测」就是用户心里那个「恢复原样」，
    # 不该还留着一个拒绝关机的标记让他事后困惑。
    heal_selftest_leftover(quiet=True)
    conf = load_conf()
    conf['enabled'] = False
    clear_feats(conf)
    save_conf(conf)
    try:
        open(STOP_FLAG, 'w', encoding='utf-8').write(str(time.time()))
    except Exception:
        pass
    st = read_state()
    pid = st.get('pid')
    if _pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass
        for _ in range(20):
            time.sleep(0.3)
            if not _pid_alive(pid):
                break
    lid_restore_conf(quiet=True)
    st.update({'pid': 0, 'heartbeatAt': 0, 'busy': False, 'level': 0, 'applied': 0,
               'shutdownPending': False, 'shutdownAt': 0})
    write_state(st)
    log('已停止监测，恢复系统默认电源行为')
    _out('已停止监测 —— 系统回到你自己的电源设置，跟没开过一样。')
    return 0


def heal_boot_state(conf, full=False, feats=False):
    """把「只该活在那一次开机里」的状态收干净，返回 (是否改动过, 说明列表)。

    两类状态，**生命周期完全不同**，别再混在一起收：

    ┌── A 一次性附加项（`autoShutdown` / `autoStopAt`）────────────────────────┐
    │ ★ 用户 2026-09-28 明确要求：                                              │
    │   「关闭任务管家之后、重启之后，不要记录上次的运行监测状态 ——              │
    │     不要让『跑完自动关机』还是打开状态。」                                 │
    │ 它们本来就设计成一次性的（`autoShutdownBootTs` = 设定那一刻的开机时刻），   │
    │ 但那个判断只挡住「真去关机」，配置与界面仍显示「已开」→ 这里在配置层也归零。│
    └──────────────────────────────────────────────────────────────────────────┘
    ┌── B 5 个「先别睡」开关（`awake` / `keepDisplayOn` / `lidClose` …）────────┐
    │ ★★★ 2026-10-01（第廿九轮，用户要求）：                                    │
    │   「防休眠这个功能本身就是要保持后台的。除了重启以外，任何睡眠或者合盖      │
    │     唤醒之后，它上次是什么样还是什么样。」                                 │
    │ → **B 是用户的长期姿势，不属于「一次性」，不许被任何自动路径顺手清掉。**   │
    │ 只有 `feats=True`（＝用户显式跑 `--reset`）才归零。                        │
    │                                                                           │
    │ 为什么单独拎出来（这一条踩了很久才看清）：`--reset` 同时干了两件事 ——      │
    │ 「附加项归零」＋「5 个开关归零」，而它被**复用**在两个日常动作上：          │
    │   · 界面「关闭服务」/ 外壳 ⌘Q → `POST /api/quit` → `--reset`              │
    │   · 「部署和修复」（那个「哪儿出问题都点这一个」的万能按钮）→ 第 7 步      │
    │ 于是用户点一次这两个里的任何一个，**「防休眠」就被静默关掉**，            │
    │ 而界面上只留下一行「运行监测 → 关闭」，看不出是它干的。                    │
    │ → 修法：日常动作一律**不碰 B**；要彻底重来请显式 `--reset`。               │
    └──────────────────────────────────────────────────────────────────────────┘

    full=True ：收 A ＋（由 cmd_heal 决定）重算/停守护 —— 「关闭服务」那一档。
    feats=True：连 B 一起归零 —— **只有 `--reset` 走这条**（彻底还原）。
    full=False：只收 A —— 服务启动时 + 状态轮询的惰性自愈。
    """
    changed = []
    bt = boot_time()
    # 开机不足 60 秒时 kern.boottime 可能还没稳定，不动手（免得把本次开机误判成上次）
    if bt <= 0 or (time.time() - bt) < 60:
        return False, []

    bts = _cfg_num(conf, 'autoShutdownBootTs', 0)
    if conf.get('autoShutdown') and bts and abs(bts - bt) > 120:
        conf['autoShutdown'] = False
        conf['autoShutdownBootTs'] = 0
        changed.append('「跑完自动关机」是上次开机时打开的 → 已还原成「关」')

    ats = _cfg_num(conf, 'autoStopBootTs', 0)
    if _cfg_num(conf, 'autoStopAt', 0) and ats and abs(ats - bt) > 120:
        conf['autoStopAt'] = 0
        conf['autoStopSetAt'] = 0
        conf['autoStopDurationSec'] = 0
        conf['autoStopBootTs'] = 0
        changed.append('「到点自动结束」是上次开机时设的 → 已清掉')

    # ★★ full 只表示「这次是『关闭服务』那一档」——**不在这里写 enabled=False**，
    #   也不在这里动 B 层。见 cmd_heal：它收完 A 之后要按 need_daemon() 重算，
    #   否则「关了服务但防休眠还开着」会把本该继续守的守护一起停掉。
    #   （这里不往 changed 里塞话，免得每次关闭服务都打一句「已收干净」的空话。）

    # ★★★ B 层：只有显式彻底还原才动。判据用「真有开关开着」，避免空写一条日志。
    if feats and any(bool(conf.get(k)) for k in _FEAT_KEYS):
        clear_feats(conf)
        changed.append('5 个「先别睡」开关 → 归零（用户显式要求彻底还原）')

    if changed:
        save_conf(conf)
        log('开机/退出自愈：' + '；'.join(changed))
    return bool(changed), changed


def _stop_daemon_hard():
    """把正在跑的守护停掉（SIGTERM + 等它真的退出）。停到过则返回 True。

    ★ 只做「停进程」这一件事：不写 STOP_FLAG（那会挡住下次 start_daemon），
      也不去动配置 —— 归零/重算由调用方决定。
    """
    try:
        pid = int((read_state() or {}).get('pid') or 0)
    except Exception:
        pid = 0
    if not _pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    for _ in range(20):                     # 最多等 6 秒
        time.sleep(0.3)
        if not _pid_alive(pid):
            break
    return True


def cmd_heal(full=False, feats=False):
    """命令行入口：把该收的收干净。服务启动 / 退出 / 打开界面时都会被叫一次。

    ★★★ 2026-10-01（第廿九轮）**分成三档**，别再混着走 —— 混着走的代价是
      「用户点一次『关闭服务』，防休眠就被静默关掉」（见 heal_boot_state 的长注释）：
        · full=False            → 只收 A → CLI 的 `--heal`。
                                  服务启动 + 状态轮询的惰性自愈走它。
        · full=True             → 收 A ＋ **按 need_daemon() 重算守护** → CLI 的 `--reconcile`。
                                  还开着「防休眠」等开关 → 守护**继续守**（关服务 ≠ 关防休眠）。
                                  界面「关闭服务」/ 外壳 ⌘Q/⌘W、`wb_repair` 第 7 步走它。
        · full=True, feats=True → 收 A ＋ 5 个开关归零 ＋ 停守护 → CLI 的 `--reset`（彻底还原）。
    """
    # 顺手清掉可能残留的取消标记（理由见 daemon 主循环那一段的说明）
    try:
        if os.path.exists(CANCEL_FLAG):
            os.remove(CANCEL_FLAG)
    except Exception:
        pass
    conf = load_conf()
    changed, what = heal_boot_state(conf, full=full, feats=feats)

    if feats:
        # ── 彻底还原：开关都归零了 → 除非本次开机刚设过「跑完自动关机」，否则没活干 ──
        if sync_enabled(conf):      # 把 enabled 定成 need_daemon()，并叫醒守护
            _out('· 5 个开关已归零；本次开机设的「跑完自动关机」还在 → 守护继续守着它')
        else:
            _stop_daemon_hard()     # 补一记 SIGTERM：到点即停，不等轮询
            lid_restore_conf(quiet=True)
            _out('· 已彻底还原成「没开过」的样子（连 5 个开关一起归零）')
    elif full:
        # ── 「关闭服务」：收完 A 之后按 need_daemon() 重算 ────────────────
        # ★★★ 关键：**不许无条件停守护**。用户开着「防休眠」时点「关闭服务」，
        #   要的只是关掉这个界面服务本身，绝不是让机器从此可以睡 —— 那正是
        #   用户这次投诉的现象（合盖唤醒后防休眠变回默认）。
        if sync_enabled(conf):
            _out('· 还有需要值守的项（「先别睡」开关 / 自动关机）→ 守护继续值守'
                 '（关服务不影响它）')
        else:
            _stop_daemon_hard()
            lid_restore_conf(quiet=True)

    for w in what:
        _out('· %s' % w)
    if not what and not full and not feats:
        _out('没有需要收拾的残留（状态本来就是干净的）。')
    return 0


def cmd_autostart(on):
    """是否随登录自启（LaunchAgent）。

    ⚠️ 本工具所在的会话**写不了 launchd 域**，所以只写 plist 文件，
       `bootstrap` 必须由用户在自己的终端里执行（提示会打出来）。
    """
    if not IS_MAC:
        _out('本功能只在 macOS 上有效。')
        return 1
    if on:
        py = _python_exe()
        plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key><array>
    <string>%s</string><string>%s</string><string>--daemon</string>
  </array>
  <key>WorkingDirectory</key><string>%s</string>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>%s</string>
  <key>StandardErrorPath</key><string>%s</string>
</dict></plist>
""" % (LA_LABEL, py, os.path.abspath(__file__), HERE,
            os.path.join(HERE, '_运行监测.out'), os.path.join(HERE, '_运行监测.err'))
        os.makedirs(os.path.dirname(LA_PLIST), exist_ok=True)
        with open(LA_PLIST, 'w', encoding='utf-8') as f:
            f.write(plist)
        conf = load_conf()
        conf['autostart'] = True
        save_conf(conf)
        _out('已写好自启配置：%s' % LA_PLIST)
        _out('最后一步要你自己的终端里执行（本工具所在会话写不了 launchd 域）：')
        _out('    launchctl bootstrap gui/$(id -u) "%s"' % LA_PLIST)
    else:
        try:
            if os.path.exists(LA_PLIST):
                os.remove(LA_PLIST)
        except Exception as e:
            _out('删不掉 %s：%r' % (LA_PLIST, e))
            return 1
        conf = load_conf()
        conf['autostart'] = False
        save_conf(conf)
        _out('已取消自启。若之前装载过，可执行：launchctl bootout gui/$(id -u)/%s' % LA_LABEL)
    return 0


def cmd_panel():
    """交互面板：与 Win 版的 --panel 等价的文字面板。"""
    while True:
        conf = load_conf()
        st = read_state()
        alive = state_alive(st)
        _out('')
        _out('%s' % BAR)
        _out('  WorkBuddy 运行监测 · 面板（macOS）')
        _out('%s' % BAR)
        _out('   [1] 档位一  不睡 + 屏幕常亮      %s'
             % ('● 正在用' if int(conf.get('level') or 0) == 1 else ''))
        _out('   [2] 档位二  不睡（允许息屏）      %s'
             % ('● 正在用' if int(conf.get('level') or 0) == 2 else ''))
        _out('   [3] 档位三  再加：合盖也不睡 + 合盖自动熄屏（笔记本）  %s'
             % ('● 正在用' if int(conf.get('level') or 0) == 3 else ''))
        _out('   [0] 不抑制（只挂监测 / 自动关机）')
        _at = _cfg_num(conf, 'autoStopAt', 0)
        _out('   [s] 跑完自动关机：%s' % ('已开' if conf.get('autoShutdown') else '关'))
        _out('   [t] 到点自动结束：%s'
             % (('已设 %s（还剩 %s）' % (time.strftime('%H:%M', time.localtime(_at)),
                                        _dur(max(0, _at - time.time()))))
                if _at else '没设'))
        _out('       （s 与 t 互斥：开一个会自动关掉另一个）')
        _out('   [x] 停止监测并恢复默认')
        _out('   [d] 详细状态')
        _out('   [q] 退出')
        _out('%s' % BAR)
        try:
            ch = input('  编号 > ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0
        if ch in ('q', ''):
            return 0
        if ch in ('1', '2', '3'):
            cmd_level(int(ch))
        elif ch == '0':
            cmd_level(0)
        elif ch == 's':
            cmd_shutdown_set(not conf.get('autoShutdown'))
        elif ch == 't':
            cmd_autostop_interactive()
        elif ch == 'x':
            cmd_stop()
            return 0
        elif ch == 'd':
            cmd_status(full=True)
        else:
            _out('  不认识：%s' % ch)


def main():
    ap = argparse.ArgumentParser(description='WorkBuddy 运行监测（电源守护）· macOS')
    # ★ 参数表与 Windows 版**逐字一致**（含 help 文案）—— 两端的界面、菜单、
    #   文档都按同一套命令描述来写，改一端就要改另一端。
    ap.add_argument('--status', action='store_true', help='打印一行摘要（菜单首屏用）')
    ap.add_argument('--full', action='store_true', help='配合 --status 打印完整状态')
    ap.add_argument('--panel', action='store_true', help='交互面板')
    ap.add_argument('--level', type=int, default=None, help='切档位 0/1/2/3')
    ap.add_argument('--level-toggle', type=int, default=None, dest='level_toggle',
                    help='按一下开 / 再按一下关（1/2/3）：正用这档 → 停监测，否则切到这档。'
                         '界面上的档位按钮与编号菜单都走这条（非交互）')
    ap.add_argument('--toggle', metavar='NAME', default=None,
                    help='单项开关（PommeToys 式）：awake=防休眠 / display=屏幕保持常亮 / '
                         'lowbat=电量不足时自动停 / lid=合盖后保持唤醒 / '
                         'lid-display=合盖后内建显示器保持点亮。'
                         '按一下开、再按一下关；点某个子项若主开关是关的会自动打开主开关。'
                         '界面上的开关与编号菜单都走这条（非交互）')
    ap.add_argument('--shutdown', nargs='?', const='ask', default=None,
                    choices=['ask', 'on', 'off'],
                    help='开关「任务结束自动关机」；不带值 = 交互面板，'
                         'on / off = 直接开或关（图形界面按钮用这条）')
    ap.add_argument('--cancel-shutdown', action='store_true', dest='cancel_shutdown',
                    help='取消正在倒计时的关机')
    ap.add_argument('--autostop', nargs='?', const='ask', default=None,
                    help='「到点自动结束」（与「跑完自动关机」互斥）：'
                         '值 = 时长（30m / 2h / 1h30m / 纯数字＝分钟），'
                         'off = 取消，不带值 = 交互面板')
    ap.add_argument('--heal', action='store_true',
                    help='收拾「上次开机留下的」运行监测状态（只收两个一次性的附加开关，'
                         '不碰「防休眠」等 5 个开关，也不动守护）')
    ap.add_argument('--reconcile', action='store_true',
                    help='收掉一次性附加项 ＋ 按「你现在的开关」把守护对齐'
                         '（该守就守、该退就退；**不碰任何开关的姿势**）。'
                         '日常的「关闭服务」「部署和修复」走这条')
    ap.add_argument('--reset', action='store_true',
                    help='★彻底还原★：两个附加开关归零 ＋ **5 个「防休眠」开关一起归零** '
                         '＋ 停守护。只在想「当成从没开过」时手动用 —— '
                         '日常的「关闭服务」「部署和修复」都不该走这条（那会把防休眠关掉）')
    ap.add_argument('--autostart', choices=['on', 'off'], default=None, help='开机自启')
    ap.add_argument('--stop', action='store_true', help='停止监测并恢复默认')
    ap.add_argument('--selftest', action='store_true', help='只读自检')
    ap.add_argument('--selftest-shutdown', action='store_true', dest='selftest_shutdown',
                    help='端到端演练「任务结束 → 自动关机」（空跑，不会真关机）')
    ap.add_argument('--selftest-lid', action='store_true', dest='selftest_lid',
                    help='实测「合盖熄屏」链路；默认只读，加 --apply 才真熄一次屏')
    ap.add_argument('--selftest-pwr', action='store_true', dest='selftest_pwr',
                    help='实测「电量/电源事件」（IOPS 推送）：读数 + 订阅 + 耗时对比（只读）')
    ap.add_argument('--apply', action='store_true',
                    help='配合 --selftest-lid：真的执行一次熄屏')
    ap.add_argument('--daemon', action='store_true', help='守护本体（内部用）')
    a = ap.parse_args()

    if a.daemon:
        return daemon_main()
    if a.selftest:
        return cmd_selftest()
    if a.selftest_shutdown:
        return cmd_selftest_shutdown()
    if a.selftest_lid:
        return cmd_selftest_lid(apply=a.apply)
    if a.selftest_pwr:
        return cmd_selftest_pwr()
    if a.status or a.full:
        return cmd_status(full=a.full)
    if a.panel:
        return cmd_panel()
    if a.toggle:
        return cmd_toggle(a.toggle)
    if a.level_toggle is not None:
        return cmd_level(a.level_toggle, toggle=True)
    if a.level is not None:
        return cmd_level(a.level)
    if a.shutdown:
        # 不带值 = 交互面板；on / off = 直接开关（图形界面按钮）
        return (cmd_shutdown_interactive() if a.shutdown == 'ask'
                else cmd_shutdown_set(a.shutdown == 'on'))
    if a.cancel_shutdown:
        return cmd_cancel_shutdown()
    if a.autostop is not None:
        if a.autostop == 'ask':
            return cmd_autostop_interactive()
        if a.autostop.strip().lower() in ('off', 'no', '0'):
            return cmd_autostop_off()
        sec, why = parse_duration(a.autostop)
        if not sec:
            _out('时长没认出来：%s' % why)
            return 2
        return cmd_autostop_set(sec)
    if a.reconcile:
        # 日常档：「关闭服务」「部署和修复」走这条 —— 收 A ＋ 按需重算守护，**不碰开关**。
        return cmd_heal(full=True)
    if a.reset:
        # ★★★ 彻底还原：连 5 个「防休眠」开关一起归零。**只有这条**会动 B 层。
        return cmd_heal(full=True, feats=True)
    if a.heal:
        return cmd_heal(full=False)
    if a.autostart:
        return cmd_autostart(a.autostart == 'on')
    if a.stop:
        return cmd_stop()
    return cmd_status(full=True)


if __name__ == '__main__':
    sys.exit(main())
