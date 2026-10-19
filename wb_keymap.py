#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_keymap.py —— 「键盘」栏目：配置模型 + 从 Karabiner-Elements 一次性导入。

背景（2026-10-01 第卅二轮 · 用户原话）：
    用户在设置之前、滚动之后，要再加一个「键盘」栏目，把
      · Karabiner-Elements 对本机做的**全部键盘映射**
      · Command X-V 对 Finder 的剪切/粘贴增强
    都收进任务管家，做到**不装那两个软件也能用**。

设计要点（为什么长这样）：
  1. **规则体原样照抄 Karabiner 的 JSON 形状**（`from` / `to` / `to_if_alone` /
     `conditions`），不另造一套 DSL。
     理由：Karabiner 的用户态那层（complex_modifications）本来就是
     「事件进来 → 匹配 from → 改写为 to」；原样搬运，Swift 侧一个翻译器就够，
     而且以后用户改了 Karabiner 配置，再 `--import` 一次永远对得上。
  2. **分组只是「呈现层」**：6 个功能包（见 GROUPS），包内逐条给开关 + 改键。
     分组按 Karabiner 规则序号决定，序号认不出来时退回关键字匹配。
  3. **用户的改动要活得下去**：重新导入时按 `sig`（规则指纹）回填
     `enabled` 与用户改过的键 —— 不能一导入就把人调好的全冲掉。

CLI：
    python3 wb_keymap.py --import      # 从 karabiner.json 导入/合并
    python3 wb_keymap.py --show        # 人话打印当前配置
    python3 wb_keymap.py --json        # 喂给界面 / 引擎
    python3 wb_keymap.py --check       # 结构自检，非 0 退出即有问题
    python3 wb_keymap.py --off         # 总开关关掉（应急：键盘出问题时一键停）
"""

import io
import os
import sys
import json
import time
import hashlib
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import wb_paths as P  # noqa: E402

CONF = os.path.join(HERE, '_键盘.json')
KARABINER = os.path.expanduser('~/.config/karabiner/karabiner.json')
CMDX_V_PREF = os.path.expanduser(
    '~/Library/Preferences/com.writronic.commandxv.plist')

SCHEMA = 1

# ---------------------------------------------------------------- 功能包
# (包 key, 标题, 一句话说明)。顺序＝界面上从上到下的顺序。
# ★★ 2026-10-18 晚（用户第 3 条）：「类似的放到一起，别显得突兀；浏览器、虚拟机也放到最后，
#    但不如 Caps Lock 靠后，Caps Lock 是最后面」。
#    ⇒ 常用的「编辑键 / 中英切换 / 系统键」占前面；只在特定程序里生效的
#      「终端·虚拟机」「浏览器」沉到后面；Caps Lock 单独垫底。
#    改这里就等于改界面顺序 —— `for_ui()` 与 `_normalize()` 都按这个列表排，
#    前端不再自己排（配置里存的旧顺序会被自动纠正）。
GROUPS = [
    # ⚠️ 2026-10-01（第卅二轮）把这条说明从「把**外接键盘**的 Alt 当作 Command 用」改掉了：
    #   那是 Karabiner 的说法（它按 device_if 分设备），而**我们分不了**内建/外接
    #   （实测里装着 Karabiner 时，物理键盘在 hidutil / ioreg 里根本不出现）。
    #   本引擎里这条对内建键盘**也**生效 —— 说明必须如实写，不给假承诺。
    ('modifier', '修饰键映射', '整键翻转：把 Alt 键本身换成 Command 键。开着会连 Option 一起吃掉，平时不用开'),
    # ★ 2026-10-18（用户第 3 条「分类 UI 太乱」）：说明改成人话 —— 这一包里现在既有
    #   Ctrl 组合、也有新加的一批 Alt 组合（左 Alt + 键 → ⌘ + 同一键），还混着 Home/End
    #   这类单键。界面上会按 fromMods 再分三小段，说明文字得把三样都讲到。
    ('edit', '编辑键', 'Windows 习惯：Ctrl 组合、Alt 组合、Home / End / Delete / Enter → macOS 对应键'),
    ('ime', '中英切换', '左右 Shift 单击一下，切换中英文输入法'),
    ('sys', '系统键', '锁屏、睡眠、Win 键这类系统动作'),
    ('term', '终端 · 虚拟机 · 远程桌面', '只在终端、虚拟机、远程桌面里生效的补充映射'),
    ('browser', '浏览器', '只在浏览器里生效的补充映射'),
    # ★★ 2026-10-01（第卅三轮 · 用户第 4 条）：「Caps Lock 好像还需要你加一个设置」。
    #   原来 kb-54 是躺在「系统键」里的一条 `CapsLock → CapsLock`（Karabiner 管它叫
    #   passthrough）—— 摆在那儿毫无意义：**这一条本来就是空转**，界面上既看不出它在
    #   干什么，也没有可调的地方。所以单拎成一个包，只给「按下去做什么」这一个选择。
    #   2026-10-18：按用户要求挪到**最后**（最下面）。
    ('caps', 'Caps Lock', '把 Caps Lock 换成 Esc / Ctrl / Command，或保持大写锁定'),
]
GROUP_TITLE = {k: t for k, t, _d in GROUPS}
GROUP_DESC = {k: d for k, _t, d in GROUPS}
GROUP_ORDER = [k for k, _t, _d in GROUPS]

# ★★ 2026-10-01（第卅三轮）：每个包**允许怎么改** —— 前端按这个决定画什么控件。
#    这是「界面长什么样」的**唯一事实源**：前端不许自己按包名写 if（那样改一处必漏一处）。
#      'keys'   → 逐条给「按键录制」按钮（点一下，直接在键盘上按出新键）
#      'switch' → 逐条只给开关，键位**锁死**（用户第 2 条：「干脆对于编辑键里面就只
#                 设置个开关 不要让它可以修改按键了」）
#      'caps'   → 只给一个「按下去做什么」的按钮组（选项少、且语义特殊，不用录制）
GROUP_KIND = {
    'modifier': 'keys',
    'caps':     'caps',
    'edit':     'switch',
    'term':     'keys',
    'browser':  'keys',
    'ime':      'keys',
    'sys':      'keys',
}

# Caps Lock 这一个包的「按下去做什么」选项：(存进 to.key_code 的值, 界面上写的话)。
# ⚠️ 每一个都必须在外壳 `KeyTable.codes` 里 —— 引擎表里没有的 key_code 会让
#    `parseRule()` 把整条规则丢掉（界面改了、保存也成功，可按下去毫无反应）。
CAPS_CHOICES = [
    ('caps_lock',      '原样（大写锁定）'),
    ('left_control',   '当作 Ctrl'),
    ('escape',         '当作 Esc'),
    ('left_command',   '当作 Cmd'),
]

# ★★ 2026-10-18 晚（用户第 1 条）：「每个功能前面写一个标题，比如『锁屏』『删除』『睡眠』」。
#    原因：界面上原来一行只有「默认 Ctrl+Cmd+Q → [⌨ 左 Ctrl]」，看不出这一行到底管什么事；
#    系统键那四条尤其严重（两条都是英文 desc，被前端当噪音滤掉了）。
#    ⚠️ 这是**纯展示名**，不参与任何匹配；新规则没登记就退回组名，不会报错。
NAME_BY_ID = {
    # ---- 系统键
    'kb-35': '锁屏',
    'kb-36': '睡眠',
    'kb-48': '删除',
    'kb-49': '锁屏',
    # ---- 编辑键：Ctrl 组合
    'kb-18': '全选', 'kb-19': '加粗', 'kb-20': '复制', 'kb-21': '查找',
    'kb-22': '斜体', 'kb-23': '新建', 'kb-24': '打开', 'kb-25': '打印',
    'kb-26': '刷新', 'kb-27': '保存', 'kb-28': '新标签页', 'kb-29': '查看源码',
    'kb-30': '粘贴', 'kb-31': '关闭', 'kb-32': '剪切', 'kb-33': '重做',
    'kb-34': '撤销',
    # ---- 编辑键：Alt 组合（默认关着）
    'kb-55': '剪切·Alt', 'kb-56': '复制·Alt', 'kb-57': '粘贴·Alt',
    'kb-58': '撤销·Alt', 'kb-59': '保存·Alt',
    'kb-60': '切标签 1', 'kb-61': '切标签 2',
    'kb-62': '切标签 3', 'kb-63': '切标签 4',
    'kb-64': '退出·Alt', 'kb-65': '关闭·Alt', 'kb-66': '刷新·Alt',
    # ---- 编辑键：单键与 Insert
    'kb-0': '复制·Insert', 'kb-1': '粘贴·Insert',
    'kb-2': '选中到开头', 'kb-3': '选中到行首', 'kb-4': '跳到开头',
    'kb-5': '跳到行首',
    'kb-6': '选中到结尾', 'kb-7': '选中到行尾', 'kb-8': '跳到结尾',
    'kb-9': '跳到行尾',
    'kb-11': '按词左选', 'kb-13': '按词右选',
    'kb-14': '删前一个词', 'kb-15': '删后一个词',
    'kb-16': '换行', 'kb-17': '换行并上移',
    # ---- 终端 · 虚拟机 · 远程桌面
    'kb-37': '复制·终端', 'kb-38': '粘贴·终端',
    'kb-39': '补全路径',
    'kb-40': '退出程序', 'kb-41': '退出·外置键盘',
    'kb-42': '复制·终端', 'kb-43': '粘贴·终端',
    'kb-47': '切换窗口',
    # ---- 浏览器
    'kb-44': '历史记录', 'kb-45': '地址栏', 'kb-46': '关闭标签页',
    # ---- 中英切换
    'kb-50': '切中英文·左', 'kb-51': '切中英文·左',
    'kb-52': '切中英文·右', 'kb-53': '切中英文·右',
    # ---- Caps Lock
    'kb-54': '大写锁定',
}

# Karabiner 规则序号 → 包。序号是从 2026-10-01 那份 karabiner.json 数出来的；
# 认不出（用户又加了规则）就走 _classify_by_desc()。
_BY_INDEX = {}
for _i in range(0, 35):
    _BY_INDEX[_i] = 'edit'
for _i in (35, 36, 48, 49):
    _BY_INDEX[_i] = 'sys'
# ★ 第卅三轮：54 号（Caps Lock passthrough）从「系统键」挪到它自己的包
_BY_INDEX[54] = 'caps'
for _i in (37, 38, 39, 40, 41, 42, 43, 47):
    _BY_INDEX[_i] = 'term'
for _i in (44, 45, 46):
    _BY_INDEX[_i] = 'browser'
for _i in (50, 51, 52, 53):
    _BY_INDEX[_i] = 'ime'


def _classify_by_desc(desc):
    d = (desc or '').lower()
    if ('shift' in d and ('abc' in d or 'wetype' in d)) or 'input source' in d:
        return 'ime'
    if ('terminal' in d or 'ide' in d or 'remote' in d or 'vm' in d
            or '虚拟机' in d or 'virtualbox' in d or 'parallels' in d):
        return 'term'
    if 'browser' in d or 'chrome' in d or 'firefox' in d or 'safari' in d:
        return 'browser'
    if 'caps' in d:                     # CapsLock 那条（Karabiner 里改了序号时靠它兜底）
        return 'caps'
    if ('lock' in d or 'sleep' in d or 'win ' in d
            or '锁屏' in d or '睡眠' in d):
        return 'sys'
    return 'edit'


def classify(idx, desc):
    if idx in _BY_INDEX:
        return _BY_INDEX[idx]
    return _classify_by_desc(desc)


# ---------------------------------------------------------------- 键名显示
_MOD_CN = {
    'control': 'Ctrl', 'left_control': 'Ctrl', 'right_control': 'Ctrl',
    'option': 'Alt', 'left_option': 'Alt', 'right_option': 'Alt',
    'command': 'Cmd', 'left_command': 'Cmd', 'right_command': 'Cmd',
    'shift': 'Shift', 'left_shift': 'Shift', 'right_shift': 'Shift',
    'any': '任意', 'fn': 'Fn',
}
_KEY_CN = {
    'left_arrow': '←', 'right_arrow': '→', 'up_arrow': '↑', 'down_arrow': '↓',
    # ★ 2026-10-18（用户：「ui 上的表述改成和我现在用的物理按键一样」）：
    #   原来这两个是 ⌫ / ⌦ 两个符号 —— 普通人分不清哪个是退格、哪个是前删，
    #   而键盘上印的是 Delete / Backspace 这样的字。改成键帽上的说法。
    'delete_or_backspace': '退格', 'delete_forward': 'Delete',
    'return_or_enter': '回车', 'escape': 'Esc', 'spacebar': '空格',
    'caps_lock': 'CapsLock', 'power': '电源键', 'slash': '/',
    'backslash': '\\', 'semicolon': ';', 'quote': "'", 'comma': ',',
    'period': '.', 'open_bracket': '[', 'close_bracket': ']',
    'minus': '-', 'equal_sign': '=', 'grave_accent_and_tilde': '`',
    'tab': 'Tab', 'home': 'Home', 'end': 'End', 'insert': 'Insert',
    'page_up': 'PgUp', 'page_down': 'PgDn', 'print_screen': 'PrtSc',
    # 左右修饰键本身也要能显示 —— 修饰键映射那一组用的就是它们
    'left_option': '左 Alt', 'right_option': '右 Alt',
    'left_command': '左 Cmd', 'right_command': '右 Cmd',
    'left_control': '左 Ctrl', 'right_control': '右 Ctrl',
    'left_shift': '左 Shift', 'right_shift': '右 Shift',
    'fn': 'Fn',
}


def key_cn(k):
    if not k:
        return '?'
    k = str(k)
    if k in _KEY_CN:
        return _KEY_CN[k]
    if k.startswith('f') and k[1:].isdigit():
        return 'F' + k[1:]
    if len(k) == 1:
        return k.upper()
    return k


def mods_cn(ms):
    return '+'.join(_MOD_CN.get(m, m) for m in (ms or []))


def combo_cn(key, mods):
    """('l', ['control','option']) → 'Ctrl+Alt+L'（没修饰键就是 'L'）"""
    if not mods:
        return key_cn(key)
    return '%s+%s' % (mods_cn(mods), key_cn(key))


def to_cn(to_list):
    out = []
    for t in (to_list or []):
        k = t.get('key_code')
        if not k and t.get('shell_command'):
            out.append('执行命令')
            continue
        out.append(combo_cn(k, t.get('modifiers')))
    return ' / '.join(out) if out else '—'


def rule_label(rule):
    """给界面用的一行人话：`Ctrl+Alt+L → Cmd+Ctrl+Q`"""
    f = rule.get('from') or {}
    fm = (f.get('modifiers') or {}).get('mandatory') or []
    left = combo_cn(f.get('key_code'), fm)
    alone = rule.get('to_if_alone')
    right = to_cn(rule.get('to'))
    if alone:
        right = '%s（单击）' % to_cn(alone)
    return '%s → %s' % (left, right)


def scope_label(rule):
    """这条规则在哪些 app 里生效（人话）。空串＝全局。"""
    for c in (rule.get('conditions') or []):
        t = c.get('type') or ''
        if t.startswith('frontmost_application'):
            n = len(c.get('bundle_identifiers') or []) + len(c.get('file_paths') or [])
            neg = '除' if t.endswith('unless') else ''
            return '%s%s 个指定程序' % (neg, n)
        if t == 'device_if':
            return '仅外接键盘'
        if t.startswith('input_source'):
            return '按当前输入法'
    return ''


def sig_of(rule):
    """规则指纹：重新导入时靠它认回同一条，保住用户改过的开关与键位。"""
    raw = json.dumps({
        'd': rule.get('description'),
        'f': rule.get('from'),
        't': rule.get('to'),
        'a': rule.get('to_if_alone'),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()[:10]


# ---------------------------------------------------------------- 键盘模式
# ⚠️ 2026-10-18（用户：「把键盘模式这个功能全量删除，不保留」）：整段删除。
#   原来这里有 WIN_ONLY_IDS / set_mode() / external_keyboards()（hidutil 枚举设备）。
#   一并去掉的还有配置里的 `keyboardMode` 字段与界面那张「Win / Mac」两段控件。
#   那 14 条 Win 模式补偿映射本身的开关状态**不变**（现在是关着的），仍在各包里可单独开。

# ---------------------------------------------------------------- 默认配置
def blank():
    return {
        'schemaVersion': SCHEMA,
        'engine': {
            # 引擎总开关。关掉＝事件监听整个卸载，键盘回到系统原样。
            'enabled': False,
            # ⚠️ 2026-10-01 实测：**做不到按设备区分**。Karabiner 能按设备是因为它
            #   有自己的 DriverKit 虚拟 HID 驱动；用户态 CGEventTap 拿不到「这个事件
            #   来自哪把键盘」。而且装着 Karabiner 时，`hidutil list` 里**只看得到
            #   Karabiner 的虚拟键盘**，物理键盘根本不出现 —— 连「外接键盘在不在」
            #   都探不到。所以不留这个开关，免得给用户一个假承诺。
            'importedAt': '',
            'source': 'karabiner',
        },
        # ★★ 2026-10-02（照 Karabiner）：`devices` ＝ **设备白名单** —— 只有列在这里的
        #    [厂商, 产品] 才做 Alt→Cmd。Karabiner 的 `devices[].simple_modifications`
        #    就是这个语义；内建键盘不在名单里，所以它天然不受影响。
        'modifiers': {'enabled': True, 'items': [], 'scope': 'external', 'devices': []},
        'groups': [],
        'commandXV': {
            'enabled': False,
            'targets': ['com.apple.finder'],
            'cutTimeoutSec': 300,
        },
    }


# ---------------------------------------------------------------- 读 / 写
def load(auto_import=True):
    """读 _键盘.json；没有就从 Karabiner 导一份（首次打开键盘页即自动建好）。"""
    if not os.path.exists(CONF):
        if auto_import:
            cfg = import_from_karabiner(write=True)
            if cfg:
                return cfg
        return blank()
    try:
        with io.open(CONF, encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception as e:
        P.log('[键盘] 读 %s 失败，用空配置顶上：%r' % (os.path.basename(CONF), e))
        return blank()
    return _normalize(cfg)


def _normalize(cfg):
    """补齐缺字段（老配置文件 / 手改坏了都要能起来）。"""
    base = blank()
    if not isinstance(cfg, dict):
        return base
    out = base
    eng = cfg.get('engine') or {}
    out['engine']['enabled'] = bool(eng.get('enabled', base['engine']['enabled']))
    out['engine']['importedAt'] = str(eng.get('importedAt') or '')
    out['engine']['source'] = str(eng.get('source') or 'karabiner')

    md = cfg.get('modifiers') or {}
    out['modifiers']['enabled'] = bool(md.get('enabled', True))
    # ★★★ 2026-10-02（用户：「Alt→Cmd 从『只对外接键盘』变成『全键盘』…你最好还是给我
    #   改成一模一样的」）：这一栏决定修饰键映射对谁生效。
    #     'external'（默认）＝只对外接非 Apple 键盘 —— 与 Karabiner 那份
    #                           `devices[].simple_modifications` 的行为一致（内建键盘的
    #                           Option 原样保留，还能打 • ™ 这类特殊符号）；
    #     'all'          ＝全键盘（老行为）。
    #   写歪了/没写一律回默认 external，绝不给「悄悄全键盘」这种惊吓。
    out['modifiers']['scope'] = 'all' if md.get('scope') == 'all' else 'external'
    # ★★ 2026-10-02（照 Karabiner）：**设备白名单** —— 只有 [厂商, 产品] 列在这里的设备
    #    才做 Alt→Cmd（Karabiner `devices[].simple_modifications` 的语义）。
    #    ⚠️ 界面保存配置时会走这里，**必须原样带回去**：漏了的话用户点一下开关，
    #       白名单就被洗掉，行为悄悄退回「内建键盘之外全都映射」。
    _dv = md.get('devices')
    out['modifiers']['devices'] = [
        [int(x[0]), int(x[1])] for x in (_dv or [])
        if isinstance(x, (list, tuple)) and len(x) == 2
    ]
    out['modifiers']['items'] = [
        _norm_item(x) for x in (md.get('items') or []) if isinstance(x, dict)]

    gs = []
    for g in (cfg.get('groups') or []):
        if not isinstance(g, dict):
            continue
        gk = g.get('key') or ''
        if gk not in GROUP_TITLE:
            continue
        rules = [_norm_item(x, with_cond=True) for x in (g.get('rules') or [])
                 if isinstance(x, dict)]
        gs.append({
            'key': gk,
            'title': GROUP_TITLE[gk],
            'desc': GROUP_DESC[gk],
            'enabled': bool(g.get('enabled', True)),
            'rules': rules,
        })
    out['groups'] = gs

    # ---- ★ 迁移（2026-10-01 第卅三轮）：Caps Lock 那一条从「系统键」搬进它自己的包。
    #   为什么不只靠 `--import` 做：`--import` 得再点一次「重新读入」，而这条迁移
    #   纯粹是**换个包**（规则本身一字不动）。不做的话，旧配置里那一条会按
    #   `sys` 的 kind（＝可录键）画出来 —— 用户按一下就会把大写锁定改成别的键。
    moved = []
    for g in out['groups']:
        if g['key'] == 'caps':
            continue
        keep = []
        for r in g['rules']:
            fk = ((r.get('from') or {}).get('key_code') or '')
            if fk == 'caps_lock':
                moved.append(r)
            else:
                keep.append(r)
        g['rules'] = keep
    if moved:
        caps = next((g for g in out['groups'] if g['key'] == 'caps'), None)
        if caps is None:
            caps = {'key': 'caps', 'title': GROUP_TITLE['caps'],
                    'desc': GROUP_DESC['caps'], 'enabled': True, 'rules': []}
            pos = len(out['groups'])          # 兜底：没找到 edit 就摆最后
            for i, g in enumerate(out['groups']):
                if g['key'] == 'edit':
                    pos = i
                    break
            out['groups'].insert(pos, caps)
        caps['rules'] = moved + caps['rules']
    # 空包不留（有些机器上可能根本没有 Caps Lock 那一条规则）
    out['groups'] = [g for g in out['groups']
                     if g['rules'] or g['key'] != 'caps']
    # ★ 2026-10-18：落盘顺序也按 GROUPS 排 —— 否则界面上排好了，存一次盘又按旧序写回去。
    _pos = {k: i for i, k in enumerate(GROUP_ORDER)}
    out['groups'].sort(key=lambda x: _pos.get(x['key'], 99))

    cx = cfg.get('commandXV') or {}
    out['commandXV']['enabled'] = bool(cx.get('enabled', False))
    tg = cx.get('targets')
    out['commandXV']['targets'] = list(tg) if isinstance(tg, list) and tg \
        else list(base['commandXV']['targets'])
    try:
        out['commandXV']['cutTimeoutSec'] = int(
            cx.get('cutTimeoutSec') or base['commandXV']['cutTimeoutSec'])
    except Exception:
        out['commandXV']['cutTimeoutSec'] = base['commandXV']['cutTimeoutSec']
    return out


def _norm_item(x, with_cond=False):
    it = {
        'id': str(x.get('id') or ''),
        'sig': str(x.get('sig') or ''),
        'enabled': bool(x.get('enabled', True)),
        'desc': str(x.get('desc') or ''),
        'from': x.get('from') or {},
        'to': x.get('to') or [],
    }
    if x.get('to_if_alone'):
        it['to_if_alone'] = x['to_if_alone']
    try:
        it['ord'] = int(x.get('ord') or 0)
    except Exception:
        it['ord'] = 0
    try:
        it['ord'] = int(x.get('ord') or 0)
    except Exception:
        it['ord'] = 0
    if with_cond:
        if x.get('conditions'):
            it['conditions'] = x['conditions']
        if x.get('scope'):
            it['scope'] = str(x['scope'])
    return it


def save(cfg):
    ok, how = P.atomic_write_json(CONF, _normalize(cfg))
    if not ok:
        P.log('[键盘] 配置写不进去（原地覆盖也没成）')
    return ok, how


# ---------------------------------------------------------------- 导入
def _read_karabiner():
    if not os.path.exists(KARABINER):
        return None
    try:
        with io.open(KARABINER, encoding='utf-8') as f:
            j = json.load(f)
    except Exception as e:
        P.log('[键盘] 读 karabiner.json 失败：%r' % e)
        return None
    profs = j.get('profiles') or []
    if not profs:
        return None
    # 取「选中」的那个 profile；没有 selected 就取第一个。
    for p in profs:
        if p.get('selected'):
            return p
    return profs[0]


def import_from_karabiner(write=False):
    """把 karabiner.json 里的 simple_modifications + complex rules 全搬过来。

    ★ 合并规则：已有配置里同 `sig` 的条目，
      **沿用用户的 enabled 与改过的键**（不覆盖）—— 见 _merge_item()。
    """
    prof = _read_karabiner()
    if prof is None:
        P.log('[键盘] 没找到可用的 karabiner.json，导入跳过')
        return None

    old = None
    if os.path.exists(CONF):
        try:
            with io.open(CONF, encoding='utf-8') as f:
                old = _normalize(json.load(f))
        except Exception:
            old = None
    cfg = blank()
    cfg['engine']['importedAt'] = _now()
    cfg['engine']['source'] = 'karabiner'

    # ---- 1. simple_modifications（按设备去重：4 个设备是同一套）
    seen = set()
    items = []
    for dev in (prof.get('devices') or []):
        for m in (dev.get('simple_modifications') or []):
            key = json.dumps(m, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            f = m.get('from') or {}
            to = m.get('to') or []
            ri = _sig_src('mod', f, to)
            items.append(_merge_item(old, 'modifier', ri,
                                     'mod-%d' % (len(items) + 1),
                                     '%s → %s' % (key_cn(f.get('key_code')),
                                                  to_cn(to)),
                                     f, to))
    cfg['modifiers']['items'] = items
    if old:
        cfg['modifiers']['enabled'] = old['modifiers']['enabled']
        cfg['modifiers']['scope'] = old['modifiers'].get('scope') or 'external'

    # ★★ 2026-10-02（照 Karabiner）：把 `devices[].identifiers` 记成**设备白名单** ——
    #    这正是 Karabiner「只对这几把键盘做 Alt→Cmd」的原始表达。内建键盘不在名单里，
    #    所以它天然不受影响。（我们原先那套 `Built-In` / `vendor != 2189` 判断是多余的；
    #    而且 `vendor != 2189` 还是错的 —— `2189/1327` 本来就在名单上。）
    if old and old['modifiers'].get('devices'):
        cfg['modifiers']['devices'] = list(old['modifiers']['devices'])
    else:
        devs = []
        for d in (prof.get('devices') or []):
            idf = d.get('identifiers') or {}
            pair = [idf.get('vendor_id'), idf.get('product_id')]
            if pair not in devs:            # 原配置里 13652/64009 出现两次，去重
                devs.append(pair)
        cfg['modifiers']['devices'] = devs

    # ---- 2. complex_modifications
    buckets = {}
    for i, r in enumerate(((prof.get('complex_modifications') or {}).get('rules') or [])):
        mans = r.get('manipulators') or []
        if not mans:
            continue
        m = mans[0]                      # 本机 55 条全是一对一，够用
        gk = classify(i, r.get('description'))
        f = m.get('from') or {}
        to = m.get('to') or []
        alone = m.get('to_if_alone')
        ri = _sig_src('rule', f, to, alone, m.get('conditions'))
        item = _merge_item(old, gk, ri, 'kb-%d' % i,
                           r.get('description') or '',
                           f, to, alone=alone,
                           conditions=m.get('conditions'),
                           scope=scope_label(m))
        item['ord'] = i
        item['ord'] = i
        buckets.setdefault(gk, []).append(item)

    groups = []
    for gk, title, gdesc in GROUPS:
        if gk == 'modifier':
            continue                     # 修饰键那一包走 modifiers 字段
        rules = buckets.get(gk) or []
        oldg = _find_group(old, gk)
        groups.append({
            'key': gk, 'title': title, 'desc': gdesc,
            'enabled': bool(oldg.get('enabled', True)) if oldg else True,
            'rules': rules,
        })
    cfg['groups'] = groups

    # ---- 3. Command X-V
    cx = _read_commandxv(old)
    cfg['commandXV'] = cx
    cfg['engine']['enabled'] = bool(old['engine']['enabled']) if old else False

    if write:
        save(cfg)
    return cfg


def _read_commandxv(old):
    """从 com.writronic.commandxv.plist 读出它现在的开关，作为我们的初值。"""
    cx = {
        'enabled': bool(old['commandXV']['enabled']) if old else False,
        'targets': list(old['commandXV']['targets']) if old else ['com.apple.finder'],
        'cutTimeoutSec': int(old['commandXV']['cutTimeoutSec']) if old else 300,
    }
    p = CMDX_V_PREF
    if not os.path.exists(p):
        return cx
    try:
        import plistlib
        with open(p, 'rb') as f:
            d = plistlib.load(f)
        raw = d.get('CommandXV_Configuration')
        if isinstance(raw, bytes):
            j = json.loads(raw.decode('utf-8'))
        elif isinstance(raw, str):
            j = json.loads(raw)
        else:
            j = {}
        if isinstance(j, dict):
            if 'targetApplications' in j and isinstance(j['targetApplications'], list):
                cx['targets'] = [str(x) for x in j['targetApplications'] if x]
            if 'cutTimeout' in j:
                try:
                    cx['cutTimeoutSec'] = int(j['cutTimeout'])
                except Exception:
                    pass
    except Exception as e:
        P.log('[键盘] 读 Command X-V 偏好失败（不影响其它）：%r' % e)
    return cx


def _sig_src(kind, f, to, alone=None, cond=None):
    raw = json.dumps({'k': kind, 'f': f, 't': to, 'a': alone, 'c': cond},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode('utf-8')).hexdigest()[:10]


def _find_group(cfg, gk):
    if not cfg:
        return None
    for g in (cfg.get('groups') or []):
        if g.get('key') == gk:
            return g
    return None


def _find_old(old, gk, ri):
    """在旧配置里按签名找同一条（先在同一个包里找，再全局找）。"""
    if not old:
        return None
    cands = []
    if gk == 'modifier':
        cands = list((old.get('modifiers') or {}).get('items') or [])
    else:
        g = _find_group(old, gk)
        if g:
            cands = list(g.get('rules') or [])
        for gg in (old.get('groups') or []):
            for r in (gg.get('rules') or []):
                if r not in cands:
                    cands.append(r)
        for r in ((old.get('modifiers') or {}).get('items') or []):
            cands.append(r)
    for c in cands:
        if c.get('sig') and c.get('sig') == ri:
            return c
    return None


def _merge_item(old, gk, ri, rid, desc, f, to, alone=None,
                conditions=None, scope=''):
    """构造一条；旧配置里有同签名的就用**用户那一份**（开关与键位都不冲掉）。

    ★ 关键：`from`/`to` 用**旧的**，只要签名一样 —— 签名是拿旧的 from/to 算的，
      所以「用户把 Ctrl+A 改成 Cmd+A」之后签名就变了，下次导入会认不回来。
      因此签名只用**规则本身**算（描述 + Karabiner 原始的 from/to），
      而「用户改过什么」记在 `from`/`to` 里 —— 见下面 _orig_sig 的兜底。
    """
    prev = _find_old(old, gk, ri)
    if prev:
        it = dict(prev)
        it['id'] = rid
        it['sig'] = ri
        # 描述若变了（用户在 Karabiner 里改了名），以新的为准，其余不动
        if desc:
            it['desc'] = desc
        return _norm_item(it, with_cond=True)
    it = {'id': rid, 'sig': ri, 'enabled': True, 'desc': desc or '',
          'from': f, 'to': to}
    if alone:
        it['to_if_alone'] = alone
    if conditions:
        it['conditions'] = conditions
    if scope:
        it['scope'] = scope
    return _norm_item(it, with_cond=True)


def _now():
    import datetime
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------- 自检 / 打印
def meta(cfg=None):
    """给界面用的一份摘要（不含规则体）。"""
    cfg = cfg or load()
    ngroups = len(cfg['groups'])
    nrules = sum(len(g['rules']) for g in cfg['groups'])
    nmod = len(cfg['modifiers']['items'])
    return {
        'schemaVersion': cfg['schemaVersion'],
        'engine': cfg['engine'],
        'modifierCount': nmod,
        'groupCount': ngroups,
        'ruleCount': nrules,
        'onRuleCount': (sum(1 for r in cfg['modifiers']['items'] if r['enabled'])
                        + sum(1 for g in cfg['groups'] for r in g['rules'] if r['enabled'])),
        'commandXV': cfg['commandXV'],
        'karabinerConfig': os.path.exists(KARABINER),
        'configFile': CONF,
    }


def for_ui(cfg=None):
    """界面要的全部内容（含人话标签），一次给全，前端不再自己拼。"""
    cfg = cfg or load()
    # ⚠️ 2026-10-18（用户：「修饰映射键也是，不保留」）：不再下发 `modifiers` 那一组。
    #   配置结构里它还在（引擎 WBShell 的 KConf 要读这个字段），但 items 已清空，
    #   界面上也没有任何入口了。
    out = {'meta': meta(cfg), 'engine': cfg['engine'],
           'groups': [], 'commandXV': cfg['commandXV'],
           # Caps Lock 那一个包的选项表（存进 to.key_code 的值 + 人话）。
           # ⚠️ 选项只有一份事实（CAPS_CHOICES）；前端不许自己抄一份。
           'capsChoices': [{'key': k, 'text': t} for k, t in CAPS_CHOICES]}
    for g in cfg['groups']:
        gg = {'key': g['key'], 'title': g['title'], 'desc': g['desc'],
              'kind': GROUP_KIND.get(g['key'], 'keys'),
              'enabled': g['enabled'], 'rules': []}
        for r in g['rules']:
            gg['rules'].append(_ui_item(r))
        out['groups'].append(gg)
    # ★ 2026-10-18：界面顺序以 GROUPS 为准（配置里存的旧顺序一并纠正）。
    _pos = {k: i for i, k in enumerate(GROUP_ORDER)}
    out['groups'].sort(key=lambda x: _pos.get(x['key'], 99))
    return out


def _ui_item(it):
    f = it.get('from') or {}
    fm = (f.get('modifiers') or {}).get('mandatory') or []
    to = it.get('to') or []
    t0 = to[0] if to else {}
    return {
        'id': it.get('id'), 'sig': it.get('sig'),
        'enabled': bool(it.get('enabled')),
        'desc': it.get('desc') or '',
        # ★ 2026-10-18：一行中文功能名（「锁屏」「跳行首」…），界面拿它当标题。
        'name': NAME_BY_ID.get(it.get('id'), ''),
        'label': rule_label(it),
        'scope': it.get('scope') or '',
        'fromKey': f.get('key_code') or '',
        'fromMods': list(fm),
        'toKey': (t0.get('key_code') or ''),
        'toMods': list(t0.get('modifiers') or []),
        'alone': bool(it.get('to_if_alone')),
        # ★ 第卅三轮：`to_if_alone` 的输出键（F17）**直接下发** ——
        #   原来是前端从 `label`（`左 Shift → F17（单击）`）里抠箭头右边那一句，
        #   文案里多一个「（单击）」就得跟着改解析逻辑。这一页要把它印成只读文字，
        #   所以给它一个字段，别再抠字符串。
        'aloneKey': (((it.get('to_if_alone') or [{}])[0] or {}).get('key_code') or ''),
        'ord': int(it.get('ord') or 0),
    }


def show(cfg=None):
    cfg = cfg or load()
    m = meta(cfg)
    print('== 键盘映射配置 ==')
    print('  配置文件：%s' % CONF)
    print('  总开关：%s' % ('开' if cfg['engine']['enabled'] else '关'))
    print('  规则：%d 条（%d 条开着）  分组：%d 个'
          % (m['ruleCount'] + m['modifierCount'], m['onRuleCount'], m['groupCount']))
    print('  Command X-V 增强：%s  目标：%s'
          % ('开' if cfg['commandXV']['enabled'] else '关',
             ', '.join(cfg['commandXV']['targets'])))
    print()
    print('  [修饰键映射] %s' % ('开' if cfg['modifiers']['enabled'] else '关'))
    for it in cfg['modifiers']['items']:
        print('    %s %-22s %s' % ('[x]' if it['enabled'] else '[ ]',
                                   it['id'], rule_label(it)))
    for g in cfg['groups']:
        on = sum(1 for r in g['rules'] if r['enabled'])
        print()
        print('  [%s] %s（%d/%d 条开着）'
              % ('x' if g['enabled'] else ' ', g['title'], on, len(g['rules'])))
        for r in g['rules']:
            print('    %s %-8s %-30s %s'
                  % ('[x]' if r['enabled'] else '[ ]', r['id'],
                     rule_label(r), r.get('scope') or ''))


def check(cfg=None):
    """结构自检；有问题返回非 0。"""
    cfg = cfg or load()
    bad = []
    if cfg['schemaVersion'] != SCHEMA:
        bad.append('schemaVersion=%r' % cfg['schemaVersion'])
    if not cfg['modifiers']['items'] and not cfg['groups']:
        bad.append('一条规则都没有（导入失败？）')
    seen = set()
    for it in cfg['modifiers']['items'] + [r for g in cfg['groups'] for r in g['rules']]:
        i = it.get('id') or ''
        if not i:
            bad.append('有条目没有 id')
        if i in seen:
            bad.append('id 重复：%s' % i)
        seen.add(i)
        f = (it.get('from') or {}).get('key_code')
        if not f:
            bad.append('%s 没有 from.key_code' % i)
        if not (it.get('to') or []) and not it.get('to_if_alone'):
            bad.append('%s 既没有 to 也没有 to_if_alone' % i)
    for g in cfg['groups']:
        if g['key'] not in GROUP_TITLE:
            bad.append('不认识的包：%s' % g['key'])
    if bad:
        for b in bad:
            print('  ✗ %s' % b)
        return 1
    print('  ✓ 键盘配置结构正常：%d 组 / %d 条'
          % (len(cfg['groups']),
             sum(len(g['rules']) for g in cfg['groups'])))
    return 0


def main(argv):
    a = argv[1:]
    if '--import' in a:
        cfg = import_from_karabiner(write=True)
        if cfg is None:
            print('✗ 没找到 karabiner.json，导入失败')
            return 2
        print('✓ 已从 Karabiner 导入并合并：')
        show(cfg)
        return 0
    if '--show' in a:
        show()
        return 0
    if '--json' in a:
        print(json.dumps(for_ui(), ensure_ascii=False, indent=2))
        return 0
    if '--check' in a:
        return check()
    if '--off' in a:
        cfg = load()
        cfg['engine']['enabled'] = False
        cfg['commandXV']['enabled'] = False
        save(cfg)
        print('✓ 已关闭键盘引擎与 Finder 剪切增强')
        return 0
    if '--on' in a:
        cfg = load()
        cfg['engine']['enabled'] = True
        save(cfg)
        print('✓ 已开启键盘引擎')
        return 0
    print(__doc__)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
