# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号任务工具 —— 环境探测与路径解析（wb_paths.py）
================================================================
设计目标
----------------------------------------------------------------
整套工具要能在**任意一台 Windows 机器**上直接跑，绝不写死：
    用户名、盘符、Python 版本号均不写死，运行时现探。

所有路径都在运行时现探，探到的结果可缓存到同目录 `_本机配置.json`，
缓存失效（换机、升级、路径变更）时自动重新探测 —— 缓存只是加速，不是事实源。

探测对象
----------------------------------------------------------------
  · WorkBuddy 数据根  ~/.workbuddy          （内含 workbuddy.db / projects / tasks）
  · WorkDaddy 数据根  %APPDATA%/WorkDaddy   （内含 meta.json / automations.json / ui-port.json）
  · Python 解释器     托管 → PATH → py 启动器 → 常见安装位置
  · 同步服务端口      17801 起找空闲端口，结果记入本机配置并被守护/触发任务共享

两级兜底
----------------------------------------------------------------
  ① _run.bat          在 CMD 层把 Python 找出来（不依赖任何绝对路径）
  ② 本模块            在 Python 层把数据目录找出来（不依赖任何绝对路径）
两层互相独立，任一层命中即可跑通。

用法
----------------------------------------------------------------
    import wb_paths
    wb_paths.WB_HOME          # WorkBuddy 数据根
    wb_paths.DB               # workbuddy.db
    wb_paths.WD_HOME          # WorkDaddy 数据根
    wb_paths.SYNC_PORT        # 本机同步端口
    print(wb_paths.describe())   # 全套体检信息（供自检/排障）
"""
import os
import re
import sys
import io
import glob
import json
import shutil
import socket
import platform
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
CONF_FILE = os.path.join(HERE, '_本机配置.json')
# 注意：这个文件名必须是纯 ASCII —— _run.bat 要在 CMD 里读它，
# 而 _run.bat 运行时会 chcp 65001，非 ASCII 文件名会让解析出错。
PY_CACHE = os.path.join(HERE, '_python_path.txt')

IS_WIN = (os.name == 'nt')
IS_MAC = (sys.platform == 'darwin')

HOME = os.path.expanduser('~')
APPDATA = os.environ.get('APPDATA', '')
LOCALAPPDATA = os.environ.get('LOCALAPPDATA', '')
PROGRAMFILES = os.environ.get('ProgramFiles', '' if IS_MAC else r'C:\Program Files')
PROGRAMFILES86 = os.environ.get('ProgramFiles(x86)', '' if IS_MAC else r'C:\Program Files (x86)')

# macOS 固定位置（与 Windows 端文件名完全一致，只是根目录不同）
MAC_SUPPORT = os.path.join(HOME, 'Library', 'Application Support')
MAC_WB_HOME = os.path.join(HOME, '.workbuddy')
MAC_WD_HOME = os.path.join(MAC_SUPPORT, 'WorkDaddy')

LAUNCH_AGENTS = os.path.join(HOME, 'Library', 'LaunchAgents')

# WorkBuddy 主进程（Electron 壳）。macOS 上 pgrep 必须用全路径匹配：
# 进程名是 "Electron"，`pgrep -x WorkBuddy` 永远匹配不到。
WB_APP_BIN = os.environ.get(
    'WB_APP_BIN', '/Applications/WorkBuddy.app/Contents/MacOS/Electron')

# 本工具自己的 LaunchAgent（跟随 WorkBuddy 启动，不随开机自启）
LA_LABEL = 'com.wb.workbuddy-task-sync'
LA_PLIST = os.path.join(LAUNCH_AGENTS, LA_LABEL + '.plist')

DEFAULT_SYNC_PORT = 17801

# 需要主动规避的假 python（微软商店占位符，存在但一跑就弹应用商店）
_BAD_PY_MARKERS = ('windowsapps',)


def log(m):
    try:
        print(m, flush=True)
    except Exception:
        pass


# ================================================================ 小工具
def _is_ascii(s):
    try:
        s.encode('ascii')
        return True
    except Exception:
        return False


def short_path(p):
    """把含中文等非 ASCII 的路径转成 8.3 短路径，规避 CMD 编码问题。

    中文用户名（如 C:\\Users\\张三）在 bat 里读取/比较时容易乱码，
    转成 C:\\Users\\XXXXXX~1 后全 ASCII，彻底免疫。失败则原样返回。
    """
    if not p or _is_ascii(p):
        return p
    if os.name != 'nt':
        return p
    try:
        import ctypes
        fn = ctypes.windll.kernel32.GetShortPathNameW
        buf = ctypes.create_unicode_buffer(1024)
        n = fn(p, buf, 1024)
        if n and buf.value and os.path.exists(buf.value):
            return buf.value
    except Exception:
        pass
    return p


def _verkey(name):
    """'3.13.12' -> (3,13,12)；非数字段记 -1，保证纯数字版本排前面。"""
    out = []
    for seg in re.split(r'[.\-_+]', name):
        out.append(int(seg) if seg.isdigit() else -1)
    return out


def _managed_py(verdir):
    """WorkBuddy 托管 Python 目录 → 可执行文件绝对路径（找不到返回 None）。

    Windows：<ver>/python.exe
    macOS  ：<ver>/bin/python3（有的包只有 python3.13）
    """
    if not os.path.isdir(verdir):
        return None
    rels = (('python.exe',) if IS_WIN
            else ('bin/python3', 'bin/python3.13', 'bin/python3.12', 'bin/python3.11'))
    for rel in rels:
        p = os.path.join(verdir, *rel.split('/'))
        if os.path.isfile(p):
            return p
    return None


def _osdesc():
    """一行系统描述（供体检打印）。"""
    try:
        if IS_MAC:
            return 'macOS %s (%s)' % (platform.mac_ver()[0], platform.machine())
        if IS_WIN:
            return 'Windows %s' % (platform.version(),)
        return '%s %s' % (platform.system(), platform.release())
    except Exception:
        return platform.platform()


def _run_quiet(cmd, timeout=20):
    """跑一条命令，返回 (ok, stdout)。窗口隐藏、异常一律吞掉。"""
    try:
        kw = {}
        if os.name == 'nt':
            kw['creationflags'] = 0x08000000      # CREATE_NO_WINDOW
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding='utf-8', errors='replace', **kw)
        return r.returncode == 0, (r.stdout or '')
    except Exception:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                               encoding='gbk', errors='replace', **kw)
            return r.returncode == 0, (r.stdout or '')
        except Exception:
            return False, ''


# ================================================================ 本机配置缓存
def load_conf():
    try:
        d = json.load(io.open(CONF_FILE, encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- 落盘：原子写 + 原地回退
#
# ★★★ 2026-10-01（第卅一轮实锤）—— **本机这个进程树里 `rename(2)` 被策略拒绝**。
#
#   现象：`[Errno 1] Operation not permitted: 'x.tmp' -> 'x'`。
#   而 `open(path,'w')`（原地覆盖）是**允许**的 —— 所以凡是「先写 .tmp 再 os.replace」
#   的写法，在这棵树里**全部失效**，而且是**静默**失效。
#
#   实测证据（2026-10-01 10:33 / 10:36 的两次「部署和修复」）：
#     · `~/Library/LaunchAgents/com.wb.workbuddy-task-sync.plist.tmp` 写成了，
#       正式 plist 没换成 → 「部署和修复」第 5 步退出码 1；
#     · `_sudo_req.json.tmp` 写成了、`_sudo_req.json` 没生成
#       → 守护自愈桥发不出请求 → 第 6 步三条线齐报 `Bootstrap failed: 5`；
#     · `_运行监测.json.tmp` 写成了 → 第 7 步每次都打一行红色「原子替换被拒」
#       （那条路径当时已有回退，所以第 7 步是绿的）。
#
#   所以这里给一个**统一**的落盘函数，全工具共用：先按原子写走，被拒就退原地覆盖。
#   原地覆盖没有原子性 —— 但「写不进去」比「不原子」坏得多；而且读方要么容忍半截
#   （外壳的提权桥会等下一轮），要么本来就先读后写（配置类）。
#
#   返回 (ok: bool, how: str)：how ∈ {'replace', 'inplace', 'fail'}。
#   ★ 不抛异常：调用方多数只关心成没成；真正失败也**返回**出来，别吞。
#   ★ 退回原地覆盖时**打一行日志**（不静默）—— 这样「这棵树是受限的」当场可见，
#     而不是等到某个按钮报错才回头猜。


def atomic_write(path, text, encoding='utf-8'):
    """原子写文本文件；本机策略禁止 rename 时自动退回原地覆盖。

    ⚠️ 只认 `rename` 一种失败：写 .tmp 本身就失败的话，原地覆盖多半也失败，
       直接返回 ('fail')；写 .tmp 成功、rename 被拒 → 原地覆盖（通常能成）。
    """
    tmp = path + '.tmp'
    try:
        with io.open(tmp, 'w', encoding=encoding, newline='') as f:
            f.write(text)
    except Exception as e:
        log('[落盘] 写 %s 的 .tmp 就失败了：%r' % (os.path.basename(path), e))
        # 还有一条路：直接原地写（有的策略只拦新文件、不拦覆盖）
        try:
            with io.open(path, 'w', encoding=encoding, newline='') as f:
                f.write(text)
            return True, 'inplace'
        except Exception as e2:
            log('[落盘] %s 彻底写不进去：%r' % (os.path.basename(path), e2))
            return False, 'fail'
    try:
        os.replace(tmp, path)
        return True, 'replace'
    except OSError as e:
        # ★ 这就是「受限树」的指纹：rename 被拒，但覆盖写可以。
        try:
            with io.open(path, 'w', encoding=encoding, newline='') as f:
                f.write(text)
        except Exception as e2:
            log('[落盘] %s 原子替换被拒且原地覆盖也失败：%r / %r'
                % (os.path.basename(path), e, e2))
            return False, 'fail'
        log('[落盘] %s：rename 被策略拒绝（%s），已改用原地覆盖'
            % (os.path.basename(path), e.strerror or e))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return True, 'inplace'
    except Exception as e:
        log('[落盘] %s 落盘异常：%r' % (os.path.basename(path), e))
        return False, 'fail'


def atomic_write_json(path, obj, indent=2):
    """`atomic_write` 的 JSON 版。返回 (ok, how)。"""
    return atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=indent))


def save_conf(d):
    """写 _本机配置.json（UTF-8）与 _本机Python.txt（供 _run.bat 读）。"""
    conf = load_conf()
    conf.update(d or {})
    conf['schemaVersion'] = 1
    conf['toolDir'] = HERE
    ok, how = atomic_write_json(CONF_FILE, conf)
    if not ok:
        # ★ 以前这里只打一行 [WARN] 就过去了 —— 那正是「假成功」：
        #   调用方以为配置写好了，实际还是旧值。现在明确报出来。
        log('[WARN] 写 _本机配置.json 失败（原地覆盖也没成）')

    py = conf.get('python')
    if py:
        try:
            # 尽量写 ASCII 短路径：_run.bat 在 CMD 里读，非 ASCII 易乱码
            with io.open(PY_CACHE, 'w', encoding='utf-8', newline='') as f:
                f.write(short_path(py) + '\n')
        except Exception as e:
            log('[WARN] 写 _本机Python.txt 失败: %s' % e)
    return conf


# ================================================================ WorkBuddy 数据根
def _wb_candidates():
    """按可信度排列的候选目录。"""
    c = [
        os.environ.get('WORKBUDDY_HOME', ''),
        os.environ.get('WB_HOME', ''),
        MAC_WB_HOME,
        os.path.join(MAC_SUPPORT, 'WorkBuddy'),
    ]
    for base in (APPDATA, LOCALAPPDATA):
        if base:
            c += [os.path.join(base, 'WorkBuddy'), os.path.join(base, 'workbuddy')]
    seen, out = set(), []
    for p in c:
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(os.path.normpath(p))
    return out


# WorkBuddy 的 sessions 表必须具备的列（用于确认「这个 db 确实是对的」）
REQUIRED_COLS = ('id', 'user_id', 'deleted_at', 'cwd')


def db_ok(path):
    """这个 workbuddy.db 是不是真的。

    判定顺序：
      ① 必须先是一个 SQLite 文件（读 magic header）；
      ② 能查就查 sessions 表在不在、关键列齐不齐 —— 查到就是权威结论；
      ③ 查不了（WorkBuddy 正在写、库被锁）不算否决，只要文件确实像数据库就接受，
         否则会出现「WorkBuddy 开着 → 探测不到数据根」的假故障。
    """
    if not path or not os.path.isfile(path):
        return False

    head = None
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except Exception:
        pass

    if head is not None and not head.startswith(b'SQLite format 3'):
        return False
    if head is None:
        # 连头部都读不出来：只能是独占锁。大文件 + 名字对，就当它是。
        try:
            return os.path.getsize(path) > 1024 * 512
        except Exception:
            return False

    try:
        import sqlite3
        uri = 'file:%s?mode=ro' % path.replace('\\', '/').replace('?', '%3f')
        con = sqlite3.connect(uri, uri=True, timeout=3)
        try:
            cols = {r[1] for r in con.execute('PRAGMA table_info(sessions)')}
        finally:
            con.close()
        return set(REQUIRED_COLS) <= cols
    except Exception:
        # 是真 SQLite 但当前查不动（被锁/权限）→ 接受
        return True


def find_workbuddy(fresh=False):
    """定位 WorkBuddy 数据根。返回 (home, db, 依据说明)。"""
    conf = {} if fresh else load_conf()
    c = conf.get('workbuddyHome'), conf.get('workbuddyDb')

    # ① 缓存命中
    if c[1] and db_ok(c[1]) and os.path.isdir(c[0] or ''):
        return os.path.normpath(c[0]), os.path.normpath(c[1]), '本机配置缓存'

    # ② 现探：谁有可用的 workbuddy.db 谁就是根
    for root in _wb_candidates():
        db = os.path.join(root, 'workbuddy.db')
        if db_ok(db):
            return root, db, '自动探测（含可用数据库）'
        for sub in ('data', 'Data'):
            db2 = os.path.join(root, sub, 'workbuddy.db')
            if db_ok(db2):
                return os.path.normpath(os.path.join(root, sub)), db2, '自动探测（子目录）'

    # ③ 退而求其次：有 projects/tasks 目录的
    for root in _wb_candidates():
        if os.path.isdir(os.path.join(root, 'projects')) or \
           os.path.isdir(os.path.join(root, 'tasks')):
            return root, os.path.join(root, 'workbuddy.db'), '自动探测（含配置目录）'

    # ④ 默认（首次安装、还没数据）
    root = os.path.join(HOME, '.workbuddy')
    return root, os.path.join(root, 'workbuddy.db'), '默认位置（未找到既有数据）'


# ================================================================ WorkDaddy 数据根
def _wd_candidates():
    c = [
        os.environ.get('WORKDADDY_HOME', ''),
        MAC_WD_HOME,                                   # macOS：~/Library/Application Support/WorkDaddy
        os.path.join(HOME, 'AppData', 'Roaming', 'WorkDaddy'),
        os.path.join(HOME, 'AppData', 'Local', 'WorkDaddy'),
    ]
    for base in (APPDATA, LOCALAPPDATA):
        if base:
            c.append(os.path.join(base, 'WorkDaddy'))
    seen, out = set(), []
    for p in c:
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(os.path.normpath(p))
    return out


def find_workdaddy(fresh=False):
    """定位 WorkDaddy 数据根。返回 (home, 依据说明)。"""
    conf = {} if fresh else load_conf()
    h = conf.get('workdaddyHome')
    if h and os.path.isdir(h) and os.path.isfile(os.path.join(h, 'meta.json')):
        return os.path.normpath(h), '本机配置缓存'

    marks = ('meta.json', 'automations.json', 'ui-port.json', 'accounts')
    for root in _wd_candidates():
        if not os.path.isdir(root):
            continue
        if any(os.path.exists(os.path.join(root, m)) for m in marks):
            return root, '自动探测（含 WorkDaddy 数据文件）'

    for root in _wd_candidates():
        if os.path.isdir(root):
            return root, '自动探测（目录存在）'

    return _wd_candidates()[1], '默认位置（未安装 WorkDaddy？）'


# ================================================================ Python 解释器
def _py_candidates():
    """按优先级排列的 Python 解释器候选（Win: python.exe / Mac: bin/python3）。"""
    out = []

    def add(p, front=False):
        if p and p not in out:
            if front:
                out.insert(0, p)
            else:
                out.append(p)

    # ★★ 2026-10-01（自检轮）：**托管 venv 排在最前**。
    #
    #   病根（实测，不是猜）：托管目录下面有两套东西 ——
    #     · `versions/<ver>/bin/python3`  ← **裸解释器**，第三方包一个都没有；
    #     · `envs/default`                 ← venv，本工具包要的 Pillow /
    #                                        cryptography / pymupdf 全装在这里。
    #   老顺序按「版本号最高」从 `versions/*` 里挑，于是**永远**挑中裸解释器；
    #   `--write` 再把它写进 `_本机配置.json` 与 `_python_path.txt`，
    #   接着 `wb_menu.PY` 拿它当**终端菜单里所有命令的宿主** ——
    #   于是「图标」要 Pillow、「会话同步」要 cryptography，在终端那条路上缺库。
    #   ⚠️ 界面上点按钮**不受影响**：`wb_ui._mac_best_python()` 有自己的候选表，
    #      venv 一直排第一。这次改动就是把两处的优先级**对齐成一份事实**。
    #   ⚠️ venv 不在（新机 / 还没建）就自然跳过，老机器行为不变。
    for root in _wb_candidates():
        venv = os.path.join(root, 'binaries', 'python', 'envs', 'default',
                            'Scripts', 'python.exe') if IS_WIN else \
               os.path.join(root, 'binaries', 'python', 'envs', 'default',
                            'bin', 'python')
        if os.path.isfile(venv):
            add(venv, front=True)

    # 当前解释器
    if sys.executable and os.path.basename(sys.executable).lower().startswith('python'):
        add(sys.executable)

    # WorkBuddy 托管 Python（多版本，取版本号最高）
    bases = []
    for root in _wb_candidates():
        bases.append(os.path.join(root, 'binaries', 'python', 'versions'))
    ver_dirs = []
    for b in bases:
        if os.path.isdir(b):
            for d in glob.glob(os.path.join(b, '*')):
                exe = _managed_py(d)
                if exe:
                    ver_dirs.append((d, exe))
    ver_dirs.sort(key=lambda t: _verkey(os.path.basename(t[0])), reverse=True)
    for _d, exe in ver_dirs:
        add(exe)

    if IS_WIN:
        # 用户级 / 系统级常见安装
        for base in (os.path.join(LOCALAPPDATA, 'Programs', 'Python'),
                     os.path.join(APPDATA, 'Python'),
                     PROGRAMFILES, PROGRAMFILES86,
                     'C:\\'):
            if not base or not os.path.isdir(base):
                continue
            try:
                subs = glob.glob(os.path.join(base, 'Python3*'))
            except Exception:
                subs = []
            subs.sort(key=lambda p: _verkey(os.path.basename(p)), reverse=True)
            for d in subs:
                add(os.path.join(d, 'python.exe'))
            add(os.path.join(base, 'python.exe'))

        # PATH
        ok, so = _run_quiet(['where', 'python'])
        if ok:
            for line in so.splitlines():
                p = line.strip()
                if p and os.path.isfile(p):
                    add(p)

        # py 启动器
        ok, so = _run_quiet(['py', '-3', '-c', 'import sys;print(sys.executable)'])
        if ok and so.strip():
            add(so.strip().splitlines()[-1].strip())

        # conda
        for base in (os.path.join(HOME, 'anaconda3'), os.path.join(HOME, 'miniconda3'),
                     os.path.join(LOCALAPPDATA, 'anaconda3'),
                     os.path.join(LOCALAPPDATA, 'miniconda3'),
                     r'C:\ProgramData\anaconda3', r'C:\ProgramData\miniconda3'):
            add(os.path.join(base, 'python.exe'))
    else:
        # macOS / Linux：PATH → 常见安装位置（Homebrew / 系统 / pyenv / conda）
        for name in ('python3', 'python3.13', 'python3.12', 'python3.11', 'python'):
            w = shutil.which(name)
            if w:
                add(w)
        for base in ('/opt/homebrew/bin', '/usr/local/bin', '/usr/bin',
                     os.path.join(HOME, '.pyenv', 'shims'),
                     os.path.join(HOME, 'miniconda3', 'bin'),
                     os.path.join(HOME, 'anaconda3', 'bin')):
            for name in ('python3', 'python'):
                p = os.path.join(base, name)
                if os.path.isfile(p):
                    add(p)

    return out


def _py_usable(exe):
    """真的能跑吗？能 import sqlite3 吗？（挡住商店占位符与损坏安装）"""
    if not exe or not os.path.isfile(exe):
        return False
    low = exe.lower()
    if any(m in low for m in _BAD_PY_MARKERS):
        return False
    ok, so = _run_quiet([exe, '-c',
                         'import sys,sqlite3;print(sys.version_info[0]*100+sys.version_info[1])'],
                        timeout=25)
    if not ok:
        return False
    try:
        return int((so or '0').strip().splitlines()[-1]) >= 308
    except Exception:
        return False


_PY_CACHE_RESULT = {}


def _bundled_python():
    """随包自带的运行时（自包含便携版）：`_runtime/python/bin/python3`。

    在就返回路径，不在返回 ''。让「随包的」永远优先于「本机装的那套」，
    这样把整个目录拷到别的 Mac 上也能跑（不再依赖 ~/.workbuddy）。
    """
    p = os.path.join(HERE, '_runtime', 'python', 'bin', 'python3')
    return p if os.path.isfile(p) else ''


def find_python(fresh=False):
    """定位可用 python。返回 (路径, 依据说明)；找不到返回 (None, 原因)。"""
    if not fresh and _PY_CACHE_RESULT:
        return _PY_CACHE_RESULT['exe'], _PY_CACHE_RESULT['why']

    # ★ 2026-10-09：随包自带的运行时优先（自包含便携版）
    b = _bundled_python()
    if b and _py_usable(b):
        _PY_CACHE_RESULT.update(exe=b, why='随包自带运行时')
        return b, '随包自带运行时'

    conf = {} if fresh else load_conf()
    cand = conf.get('python')
    if cand and _py_usable(cand):
        _PY_CACHE_RESULT.update(exe=cand, why='本机配置缓存')
        return cand, '本机配置缓存'

    for p in _py_candidates():
        if _py_usable(p):
            _PY_CACHE_RESULT.update(exe=p, why='自动探测')
            return p, '自动探测'

    _PY_CACHE_RESULT.update(exe=None, why='未找到可用 Python')
    return None, '未找到可用 Python'


def find_pythonw(py=None):
    """同目录的 pythonw.exe（无窗口），没有就退回 python.exe。"""
    py = py or find_python()[0]
    if not py:
        return None
    cand = os.path.join(os.path.dirname(py), 'pythonw.exe')
    return cand if os.path.exists(cand) else py


# ================================================================ 端口
def port_free(port, host='127.0.0.1'):
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def free_port(start=DEFAULT_SYNC_PORT, tries=60):
    for p in range(start, start + tries):
        if port_free(p):
            return p
    return start


def resolve_port(fresh=False, prefer_free=False):
    """本机同步端口：优先沿用配置里的（守护在跑时端口是被占的，不能当冲突处理）。"""
    conf = {} if fresh else load_conf()
    p = conf.get('syncPort')
    if isinstance(p, int) and 1024 <= p <= 65535:
        if not prefer_free or port_free(p):
            return p
    return free_port(DEFAULT_SYNC_PORT)


# ==================================================== 进程退出通知（kqueue）
#
# ★★ 2026-09-29 立。两个守护（跨机任务线 / 本机任务线）都需要同一件事：
#      **「WorkBuddy 关掉了没有」—— 关了才收尾、才下班。**
#   原来两边都是「每 15 秒 pgrep 一次」；用户拍板把轮询去掉后，
#   统一改成让**内核通知**：拿一次 pid → 注册 `EVFILT_PROC` + `NOTE_EXIT` →
#   进程一退，`kevent()` 立刻返回。零轮询、不起子进程。
#
# ⚠️ 本机实测（2026-09-29；判据脚本 `_排障与验证脚本/_chk_sessync_evt.py`）：
#     · 对**不是自己子进程**的目标（已被 init 收养、`ppid=1`）同样能收到 NOTE_EXIT
#       —— 这正是 WorkBuddy 的情形（launchd 拉起来的）。实测用时 3.00 秒＝
#       目标活多久等多久，没有半点额外延迟；
#     · 目标还活着时带超时的 `kevent()` **返回 0 并按超时返回**（实测 1.00 秒）
#       → 既不忙等也不空转；
#   所以「等一个进程退出」这件事可以完全交给内核，一行 sleep 都不用。
#
# ⚠️ 三个坑：
#   ① 64 位 macOS 上 `struct kevent` 是 **32 字节**：
#      ident 8 / filter 2 / flags 2 / fflags 4 / data 8 / udata 8
#      —— ctypes 必须用 `c_size_t` / `c_ssize_t`（`c_uint` 只 4 字节，
#      结构体对不上 = 「注册成功但永远收不到事件」这种最难查的病）。
#   ② pid 复用：knote 在**注册那一刻挂在 proc 结构上**，不是挂在 pid 数字上，
#      注册之后 pid 号被别的进程拿去也不会误报；
#      但「注册时进程已经没了」会让 kevent 返回 ESRCH → 当成「它已经退了」。
#   ③ 非 macOS / 系统调用失败 → 一律返回 None，调用方**必须**退回轮询。
EVFILT_PROC = -5
EV_ADD = 0x0001
EV_ENABLE = 0x0004
NOTE_EXIT = 0x80000000
NOTE_EXITSTATUS = 0x04000000

_KQ_LIB = None


if IS_MAC:
    import ctypes

    class _Kevent(ctypes.Structure):
        """`struct kevent`（64 位 macOS，32 字节）。"""
        _fields_ = [('ident', ctypes.c_size_t),
                    ('filter', ctypes.c_int16),
                    ('flags', ctypes.c_uint16),
                    ('fflags', ctypes.c_uint32),
                    ('data', ctypes.c_ssize_t),
                    ('udata', ctypes.c_void_p)]

    class _Timespec(ctypes.Structure):
        _fields_ = [('tv_sec', ctypes.c_long), ('tv_nsec', ctypes.c_long)]
else:                     # Windows 没有 kqueue；给个占位，让 import * 不至于炸
    _Kevent = None
    _Timespec = None

KEVENT_SIZE = 32


def _kq_lib():
    """libc 句柄（懒加载一次，带 errno）；拿不到返回 None。"""
    global _KQ_LIB
    if not IS_MAC:
        return None
    if _KQ_LIB is False:
        return None
    if _KQ_LIB is None:
        try:
            import ctypes.util as _cu
            lib = ctypes.CDLL(_cu.find_library('c') or 'libc.dylib', use_errno=True)
            lib.kqueue.restype = ctypes.c_int
            lib.kqueue.argtypes = []
            lib.kevent.restype = ctypes.c_int
            lib.kevent.argtypes = [ctypes.c_int, ctypes.POINTER(_Kevent), ctypes.c_int,
                                   ctypes.POINTER(_Kevent), ctypes.c_int, ctypes.c_void_p]
            _KQ_LIB = lib
        except Exception:
            _KQ_LIB = False
    return _KQ_LIB or None


def kq_available():
    """kqueue 这条路现在能不能用（决定走「内核通知」还是退回「轮询」）。"""
    lib = _kq_lib()
    if lib is None:
        return False
    try:
        fd = lib.kqueue()
        if fd < 0:
            return False
        os.close(fd)
        return True
    except Exception:
        return False


def wait_pids_exit(pids, timeout=None):
    """阻塞等这些 pid **全部**退出（内核通知，不轮询、不起进程）。

    返回 True=全退了 / False=超时还没退（只在给了 timeout 时可能出现）
        / None=这条路不可用（调用方**必须**退回轮询）。
    """
    lib = _kq_lib()
    if lib is None or not pids:
        return None
    try:
        kq = lib.kqueue()
    except Exception:
        return None
    if kq < 0:
        return None
    registered = []
    try:
        for pid in pids:
            ch = _Kevent(ident=int(pid), filter=EVFILT_PROC, flags=EV_ADD | EV_ENABLE,
                         fflags=NOTE_EXIT | NOTE_EXITSTATUS, data=0, udata=None)
            if lib.kevent(kq, ctypes.byref(ch), 1, None, 0, None) == 0:
                registered.append(pid)
            # 注册失败（多半 ESRCH = 这个进程已经没了）→ 就当它已经退出
        if not registered:
            return True                       # 一个都没注册上 = 全都已经退了
        left = len(registered)
        ev = _Kevent()
        ts = None
        if timeout is not None:
            ts = _Timespec(int(timeout), int(round((timeout - int(timeout)) * 1e9)))
        while left > 0:
            n = lib.kevent(kq, None, 0, ctypes.byref(ev), 1,
                           ctypes.byref(ts) if ts is not None else None)
            if n == 0:
                return False                  # 超时
            if n < 0:
                return None                   # 系统调用出错 → 让调用方退回轮询
            left -= 1
        return True
    except Exception:
        return None
    finally:
        try:
            os.close(kq)
        except Exception:
            pass


# ==================================================== 进程父 pid（libproc）
#
# ★★ 2026-09-29 加，起因是**自检脚本被判失败**（不是软件坏了）：
#   `_chk_sessync_evt.py` 校验「kqueue 能等到**不是自己子进程**的目标退出」，
#   为此要确认目标已被 init 收养（`ppid == 1`）。原来用
#   `subprocess.run(['ps', '-o', 'ppid=', '-p', N])` 取父 pid ——
#   而 **WorkBuddy 会话沙盒会拦掉 exec `ps`**（`PermissionError: Operation
#   not permitted`），于是一条**只读**的自检报「退出码 1」。
#
#   `ps` 在这类进程树里本来就不可靠（同样是为什么 wb_powerguard.py 枚举进程
#   时首选 libproc、`ps` 只当退路），所以这里改成**直接问内核**：
#   `proc_pidinfo(pid, PROC_PIDTBSDINFO, …)` 填的是 `struct proc_bsdinfo`
#   （`sys/proc_info.h`），头 5 个字段都是 `uint32_t`：
#       flags / status / xstatus / pbi_pid / **pbi_ppid** ← 偏移 16
#   取满 ≥ 20 字节即可安全读第 5 个 uint32。
PROC_PIDTBSDINFO = 3

_LP_LIB = None


def _lp_lib():
    """libproc 句柄（懒加载一次）；拿不到返回 None。"""
    global _LP_LIB
    if not IS_MAC:
        return None
    if _LP_LIB is False:
        return None
    if _LP_LIB is None:
        try:
            import ctypes
            lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            lib.proc_pidinfo.restype = ctypes.c_int
            lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                         ctypes.c_void_p, ctypes.c_int]
            _LP_LIB = lib
        except Exception:
            _LP_LIB = False
    return _LP_LIB or None


def ppid_of(pid):
    """某个进程的父 pid；取不到返回 None（进程不存在 / 这条路不可用）。

    ⚠️ 走的是 libproc，**不起子进程**（见上面那段说明：`ps` 会被沙盒拦）。
       `ps` 留作兜底，只为「万一这台机器 libproc 句柄拿不到」。
    """
    try:
        pid = int(pid)
    except Exception:
        return None
    if pid <= 0:
        return None
    lib = _lp_lib()
    if lib is not None:
        try:
            import ctypes
            buf = ctypes.create_string_buffer(256)
            n = lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, ctypes.c_uint64(0),
                                 buf, ctypes.c_int(256))
            if n >= 20:
                return int(ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32))[4])
        except Exception:
            pass
    ok, out = _run_quiet(['/bin/ps', '-o', 'ppid=', '-p', str(pid)], timeout=5)
    txt = (out or '').strip()
    if ok and txt.isdigit():
        return int(txt)
    return None


# ================================================================ 全局常量（导入即定位）
WB_HOME, DB, _WB_WHY = find_workbuddy()
PROJ = os.path.join(WB_HOME, 'projects')
TASKS = os.path.join(WB_HOME, 'tasks')

WD_HOME, _WD_WHY = find_workdaddy()
WD_META = os.path.join(WD_HOME, 'meta.json')
WD_AUTO = os.path.join(WD_HOME, 'automations.json')
WD_PORT_FILE = os.path.join(WD_HOME, 'ui-port.json')
WD_TOKEN_FILE = os.path.join(WD_HOME, '.api-token')

SYNC_PORT = resolve_port()

BACKUP_DIR = os.path.join(HERE, '_backup')
LOG_FILE = os.path.join(HERE, '_autosync.log')


# ================================================================ 移入废纸篓
#
# ★★ 2026-09-29 新增，起因是一次**真的挂死**：
#   图形界面点「部署和修复」→ wb_repair 第 2 步拍照快照 → 轮换旧快照时请
#   Finder 删除 → 那个 AppleEvent **再也没回来**。而当时各脚本写的超时是
#   `timeout=600`（整批）+ `timeout=180`（逐项重试），于是一份旧快照就能把
#   整条流水线按住十几分钟；`_n 份` 就是几个小时 —— 用户看到的就是
#   「点了一下没反应，一直转」。实测卡了 7 分 25 秒还在 `select_poll_poll`。
#
#   为什么 Finder 会不回：`tell application "Finder" to delete …` 是**跨进程
#   AppleEvent**，要 Finder 那边把请求处理完才返回。Finder 没启动 / 正忙 /
#   弹了自动化授权框没人点 —— 都表现为「发出去就石沉大海」。这个工具大量
#   场景是在**没有前台会话**的进程树里跑（界面服务 fork 出来的子进程），
#   这类调用本来就不该**无限等**。
#
#   所以这里定三条：
#     ① 单一实现。原来 wb_clean_dead / wb_clean_logs / wb_clean_pkg / wb_space
#        各抄了一份（超时值还各不相同：600/180/60），改一处漏三处。
#     ② 超时必须**短**。Finder 处理一条删除请求正常在一秒内；给 15 秒已经很宽容。
#     ③ Finder 不给办就**本地兜底**：Python 直接把它挪进 ~/.Trash —— 效果相同
#        （文件进废纸篓、可从「废纸篓」还原），只是少一层 Finder 的「放回原处」
#        记录。宁可少一层可撤销性，也不能让整个按钮挂死。
TRASH_TIMEOUT = 15

# 「Finder 到底还能不能用」的进程内记忆。
#   None = 还没试过；True = 能用；False = 本进程别再用它了（见下面 to_trash）。
#   ★ 为什么要有它：Finder 不回 AppleEvent 时，**每一项**都要白等一轮超时。
#     实测这台机器 Finder 完全没响应 —— 一件文件就要等 15×2 = 30 秒，
#     快照轮换 3 份就是 90 秒。第一次不响应就把它记住，后面直接走本地兜底，
#     整批的耗时就降到「一次超时 + 其余瞬间」。
_FINDER_STATE = {'ok': None}


def finder_ok():
    """None=还没试过 / True=能用 / False=本进程已判定不可用。"""
    return _FINDER_STATE['ok']


def trash_dir():
    return os.path.expanduser('~/.Trash')


def _as_applescript_str(s):
    return '"%s"' % s.replace('\\', '\\\\').replace('"', '\\"')


def _ask_finder(items, timeout):
    """请 Finder 删一批。返回 True=这次调用没超时（不代表删成功）。

    ⚠️ 只看「有没有超时」来判 Finder 死活：Finder 明确**拒绝**某个文件
      （被占用、名字怪）会立刻返回非 0，那是**这一项**的问题，不该把
      Finder 整个判死 —— 下一项它可能就办成了。
    """
    try:
        subprocess.run(['osascript', '-e',
                        'tell application "Finder" to delete {%s}' % items],
                       capture_output=True, timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        if _FINDER_STATE['ok'] is not False:
            log('Finder 对删除请求没有响应（等了 %ss）—— 本进程内改用本地挪入 ~/.Trash'
                % timeout)
        _FINDER_STATE['ok'] = False
        return False
    except Exception:
        return True          # osascript 不在、脚本语法错之类：不判 Finder 死刑


def _move_to_trash_py(p):
    """Finder 不管时的退路：直接挪进 ~/.Trash（同名自动加序号）。"""
    d = trash_dir()
    try:
        os.makedirs(d, exist_ok=True)
        base = os.path.basename(p.rstrip(os.sep)) or 'item'
        dst = os.path.join(d, base)
        n = 2
        while os.path.exists(dst):
            root, ext = os.path.splitext(base)
            dst = os.path.join(d, '%s %d%s' % (root, n, ext))
            n += 1
        shutil.move(p, dst)
    except Exception as e:
        log('移入废纸篓失败 %s：%r' % (p, e))
        return False
    return not os.path.exists(p)


def to_trash(paths, chunk=40, timeout=TRASH_TIMEOUT):
    """把一批路径移入废纸篓（可还原）。返回 `(成功数, 失败列表)`。

    先请 Finder（用户在访达里能「放回原处」），**它不响应/拒绝就本地兜底**。
    判「成功」一律以「文件还在不在」为准，不看 osascript 的退出码 ——
    AppleEvent 有时返回 0 却什么都没干。
    """
    paths = [p for p in (paths or []) if p and os.path.exists(p)]
    if not paths:
        return 0, []
    if not IS_MAC:
        return _to_trash_win(paths)

    good, bad = 0, []
    for i in range(0, len(paths), chunk):
        part = paths[i:i + chunk]
        if _FINDER_STATE['ok'] is not False:       # Finder 已判死就别再等它
            items = ', '.join('POSIX file %s as alias' % _as_applescript_str(p)
                              for p in part)
            _ask_finder(items, timeout)
        for p in part:
            if not os.path.exists(p):
                good += 1
                continue
            # 逐项再来一次（Finder 对个别项会挑剔），还是短超时
            if _FINDER_STATE['ok'] is not False:
                _ask_finder('POSIX file %s as alias' % _as_applescript_str(p), timeout)
            if not os.path.exists(p):
                good += 1
            elif _move_to_trash_py(p):
                good += 1
            else:
                bad.append(p)
    return good, bad


def _to_trash_win(paths):
    """Windows：SHFileOperationW + FOF_ALLOWUNDO（进回收站）。返回 (成功数, 失败列表)。"""
    import ctypes
    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400
    FOF_NOCONFIRMMKDIR = 0x0200

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [('hwnd', ctypes.c_void_p),
                    ('wFunc', ctypes.c_uint),
                    ('pFrom', ctypes.c_wchar_p),
                    ('pTo', ctypes.c_wchar_p),
                    ('fFlags', ctypes.c_uint),
                    ('fAnyOperationsAborted', ctypes.c_int),
                    ('hNameMappings', ctypes.c_void_p),
                    ('lpszProgressTitle', ctypes.c_wchar_p)]

    good, bad = 0, []
    for p in paths:
        buf = os.path.abspath(p) + '\0\0'
        op = SHFILEOPSTRUCTW(None, FO_DELETE, buf, None,
                             FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT |
                             FOF_NOERRORUI | FOF_NOCONFIRMMKDIR,
                             0, None, None)
        try:
            ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        except Exception as e:
            log('SHFileOperationW 失败 %s：%r' % (p, e))
        if os.path.exists(p):
            bad.append(p)
        else:
            good += 1
    return good, bad



# ================================================================ 体检
def describe():
    """返回一份完整的环境体检结果（供 wb_setup.py 与排障使用）。"""
    py, pywhy = find_python()
    return {
        '工具目录': HERE,
        '本机用户': os.environ.get('USERNAME') or os.environ.get('USER') or '',
        '计算机名': os.environ.get('COMPUTERNAME') or socket.gethostname(),
        'os': _osdesc(),
        'Python': py or '（未找到）',
        'Python 依据': pywhy,
        'Python 版本': sys.version.split()[0] if py else '',
        'pythonw': find_pythonw(py) or '',
        'WorkBuddy 数据根': WB_HOME,
        'WorkBuddy 依据': _WB_WHY,
        'workbuddy.db': DB,
        '数据库可用': db_ok(DB),
        'WorkDaddy 数据根': WD_HOME,
        'WorkDaddy 依据': _WD_WHY,
        'WorkDaddy 已安装': os.path.isdir(WD_HOME) and
                            os.path.isfile(os.path.join(WD_HOME, 'meta.json')),
        '同步端口': SYNC_PORT,
        '配置文件': CONF_FILE if os.path.exists(CONF_FILE) else '（未生成）',
        'Python 缓存': PY_CACHE if os.path.exists(PY_CACHE) else '（未生成）',
    }


def main():
    import argparse
    ap = argparse.ArgumentParser(description='WorkBuddy 工具包 —— 环境探测')
    ap.add_argument('--write', action='store_true', help='把探测结果写成本机配置')
    ap.add_argument('--fresh', action='store_true', help='忽略缓存，强制重新探测')
    a = ap.parse_args()

    py, pywhy = find_python(fresh=a.fresh)
    wb, db, wbwhy = find_workbuddy(fresh=a.fresh)
    wd, wdwhy = find_workdaddy(fresh=a.fresh)
    port = resolve_port(fresh=a.fresh, prefer_free=not a.write)

    if a.write:
        if not py:
            log('✗ 未找到可用的 Python，无法写入配置。')
            return 2
        save_conf({'python': py, 'pythonw': find_pythonw(py),
                   'workbuddyHome': wb, 'workbuddyDb': db,
                   'workdaddyHome': wd, 'syncPort': port,
                   'machine': '%s@%s' % (os.environ.get('USERNAME')
                                         or os.environ.get('USER') or '',
                                         os.environ.get('COMPUTERNAME')
                                         or socket.gethostname()),
                   'platform': sys.platform})
        log('✓ 已写入 %s' % CONF_FILE)
        log('✓ 已写入 %s' % PY_CACHE)

    for k, v in describe().items():
        log('  %-18s %s' % (k + ':', v))
    return 0 if py else 2


if __name__ == '__main__':
    sys.exit(main())
