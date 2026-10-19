#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""工具包自保快照 / 完整性校验 / 一键复活。

为什么要有它（2026-09-27 的教训）
================================================================
  09-27 09:55:56，**一个本工具包之外的程序**在 30 秒里把
  `D:\APP\WorkBuddy任务同步工具` 整个目录连同 `D:\APP` 下另外四个软件的
  子目录一起「移入回收站」（1510 项 / 约 1.3 GB）。幸亏这机器的删除被
  安全层改道到回收站，才全部捞回来。
  但那是运气：**如果先被清空了回收站，工具包的所有脚本就都没了。**
  本脚本就是给这件事兜底：
    · 把工具包的「源码 + 文档 + 图标 + 快捷方式」打成一个 < 1 MB 的 zip，
      **存在工具包之外**（工作空间的 `_backup\pkg-snapshots\`）——
      因为被删的正是工具包本身，快照放在包里等于一起被删；
    · 每份快照附一份 `manifest`（每个文件的 sha256），可随时校验
      「少了哪个 / 哪个被改了 / 多了什么」；
    · 真出事时一条命令把缺的、坏的补回来（不覆盖你没动过的文件，
      也不删除多出来的文件）。

用法
================================================================
  python _snapshot_pkg.py                 # 拍照 + 顺手校验（默认，最常用）
  python _snapshot_pkg.py --verify        # 只校验，不拍照
  python _snapshot_pkg.py --list          # 列出现有快照（按时间倒序）
  python _snapshot_pkg.py --restore       # 从最新快照补回缺失/损坏的文件
  python _snapshot_pkg.py --restore pkg-20260927-100000.zip
  python _snapshot_pkg.py --keep 5        # 保留最新几份（默认 5）

边界
================================================================
  · 只打包下面白名单里的类型；运行期产物（_backup / __pycache__ /
    *.log / *.instance / *.stop / *状态.json / 会话同步大文件）一律不进快照；
  · 清理旧快照走回收站（可还原），不真删；
  · `--restore` **只补不删**：多出来的文件原样留着，便于事后分析。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import sys
import zipfile

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 快照存放处：**工具包之外**（工作空间 _backup）。可被环境变量覆盖，便于换机。
#   Windows: D:\AI\电脑工具\_backup\pkg-snapshots
#   macOS  : ~/WorkBuddy/电脑工具/_backup/pkg-snapshots
#     ★ 2026-09-27 移植时补：原来只有 Windows 那一行的字面量，Mac 上会去
#       建一个名为「D:\AI\电脑工具\_backup\pkg-snapshots」的怪目录。
def _default_snap_dir():
    if os.name == 'nt':
        return r'D:\AI\电脑工具\_backup\pkg-snapshots'
    return os.path.join(os.path.expanduser('~'), 'WorkBuddy', '电脑工具',
                        '_backup', 'pkg-snapshots')


SNAP_DIR = os.environ.get('WB_PKG_SNAPSHOT_DIR') or _default_snap_dir()

# 进快照的扩展名（源码 / 文档 / 图标 / 快捷方式 / 配置样例）
KEEP_EXT = {'.py', '.pyw', '.bat', '.cmd', '.vbs', '.ps1', '.html', '.htm',
            '.txt', '.md', '.ico', '.png', '.lnk', '.mjs', '.json'}
KEEP_NAMES = {'_运行监测.json'}          # json 里只留这个「有默认值可重建」的配置
# ★★ 2026-10-01（第卅九轮）：`_自签证书` **必须排除**。
#   它是本地代码签名证书的私钥 + 独立钥匙串（`key.pem` / `cert.p12` /
#   `workbuddy-sign.keychain-db` / `keychain.pw`）。快照会被放到工具包**之外**
#   （工作空间的 `_backup/pkg-snapshots/`），私钥跟着快照走 = 泄漏。
#   实测：这几类扩展名本来就不在 KEEP_EXT 里（`.pem` / `.p12` / `.keychain-db`
#   都不打包），但那是**巧合**而不是规则 —— 显式排除掉，别指望巧合。
SKIP_DIRS = {'_backup', '__pycache__', '.workbuddy', '.git', '_自签证书'}
SKIP_SUFFIX = ('.log', '.old')
MANIFEST = 'manifest.json'
KEEP_DEFAULT = 5


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def should_keep(rel):
    name = os.path.basename(rel)
    ext = os.path.splitext(name)[1].lower()
    if ext == '.json':
        return name in KEEP_NAMES
    return ext in KEEP_EXT


def walk_files(root=HERE):
    """产出 (绝对路径, 相对路径)。跳过运行期产物与重解析点。"""
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS
                   and not os.path.islink(os.path.join(base, d))]
        for f in files:
            p = os.path.join(base, f)
            name = f.lower()
            if name.endswith(SKIP_SUFFIX) or '状态.json' in f:
                continue
            if f.endswith(('.instance', '.stop', '.lock')):
                continue
            rel = os.path.relpath(p, root)
            if should_keep(rel):
                out.append((p, rel))
    out.sort(key=lambda x: x[1])
    return out


def snapshots():
    if not os.path.isdir(SNAP_DIR):
        return []
    out = [os.path.join(SNAP_DIR, n) for n in os.listdir(SNAP_DIR)
           if n.startswith('pkg-') and n.endswith('.zip')]
    out.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return out


def do_snapshot(keep):
    files = walk_files()
    man = {'tool': 'WorkBuddy任务同步工具', 'toolDir': HERE,
           'createdAt': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'files': {}}
    for p, rel in files:
        man['files'][rel.replace('\\', '/')] = {
            'sha256': sha256(p), 'size': os.path.getsize(p)}

    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    out = os.path.join(SNAP_DIR, 'pkg-%s.zip' % stamp)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr(MANIFEST, json.dumps(man, ensure_ascii=False, indent=1))
        for p, rel in files:
            z.write(p, rel.replace('\\', '/'))
    print('  ✓ 已拍照：%s' % out)
    print('    收录 %d 个文件，%.1f KB' % (len(files), os.path.getsize(out) / 1024.0))

    # 只留最新 keep 份，更早的进回收站（可还原）
    olds = snapshots()[keep:]
    if olds:
        try:
            import wb_clean_pkg as C
            got, fail, _codes = C.to_recycle_bin(list(olds))
            print('  旧快照 %d 份 → 回收站（成功 %d，失败 %d）'
                  % (len(olds), got, len(fail)))
        except Exception as e:
            print('  ! 旧快照清理跳过（%r）—— 它们仍留在 %s' % (e, SNAP_DIR))
    return out


def latest_manifest():
    zips = snapshots()
    if not zips:
        return None, None
    z = zips[0]
    with zipfile.ZipFile(z) as zf:
        try:
            man = json.loads(zf.read(MANIFEST).decode('utf-8'))
        except Exception:
            return z, None
    return z, man


def do_verify(quiet=False):
    z, man = latest_manifest()
    if man is None:
        print('  ! 还没有任何快照（先跑一次不带参数的 _snapshot_pkg.py）。')
        return 1
    print('  基准快照：%s（%s）' % (os.path.basename(z), man.get('createdAt')))
    miss, chg, extra = [], [], []
    now = {}
    for p, rel in walk_files():
        now[rel.replace('\\', '/')] = p
    for rel, info in man['files'].items():
        p = now.pop(rel, None)
        if p is None:
            miss.append(rel)
            continue
        if os.path.getsize(p) != info['size'] or sha256(p) != info['sha256']:
            chg.append(rel)
    extra = sorted(now)
    if not (miss or chg):
        print('  ✓ 工具包与快照完全一致（%d 个文件），没有任何缺失或改动。'
              % len(man['files']))
    else:
        if miss:
            print('  ✗ 缺失 %d 个：' % len(miss))
            for x in miss[:40]:
                print('      %s' % x)
        if chg:
            print('  ✗ 与快照不同 %d 个：' % len(chg))
            for x in chg[:40]:
                print('      %s' % x)
    if extra:
        print('  · 快照之后新增 %d 个（正常，多为新脚本/新快照产物）：' % len(extra))
        for x in extra[:10]:
            print('      %s' % x)
    return 0 if not (miss or chg) else 2


def do_restore(which=None):
    zips = snapshots()
    if not zips:
        print('  ! 没有可用快照。')
        return 1
    z = None
    if which:
        cand = [p for p in zips if os.path.basename(p) == which]
        if not cand:
            print('  ! 找不到快照 %s' % which)
            return 1
        z = cand[0]
    else:
        z = zips[0]
    print('  从快照恢复：%s' % os.path.basename(z))
    n_new = n_fix = n_skip = 0
    with zipfile.ZipFile(z) as zf:
        try:
            man = json.loads(zf.read(MANIFEST).decode('utf-8'))
        except Exception:
            man = {'files': {k: None for k in zf.namelist() if k != MANIFEST}}
        for rel, info in man['files'].items():
            dest = os.path.join(HERE, rel.replace('/', os.sep))
            try:
                if os.path.exists(dest):
                    if info and sha256(dest) == info['sha256']:
                        n_skip += 1
                        continue
                    # 与快照不同 —— 不动它（可能是你手改的新版），另存为 .from-snapshot
                    alt = dest + '.from-snapshot'
                    with open(alt, 'wb') as f:
                        f.write(zf.read(rel))
                    n_fix += 1
                    print('      ~ %s 与快照不同，已另存为 %s'
                          % (rel, os.path.basename(alt)))
                else:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, 'wb') as f:
                        f.write(zf.read(rel))
                    n_new += 1
                    print('      + 补回 %s' % rel)
            except Exception as e:
                print('      ! %s -> %r' % (rel, e))
    print('  完成：补回 %d 个 · 另存比对 %d 个 · 本就一致 %d 个'
          % (n_new, n_fix, n_skip))
    print('  提示：另存的 *.from-snapshot 请你自行比对后决定用哪一份（本脚本不覆盖手改稿）。')
    return 0


def do_list():
    zips = snapshots()
    print('  快照目录：%s' % SNAP_DIR)
    if not zips:
        print('  （还没有快照）')
        return 0
    for p in zips:
        st = os.stat(p)
        print('   %s  %8.1f KB  %s'
              % (os.path.basename(p), st.st_size / 1024.0,
                 datetime.datetime.fromtimestamp(st.st_mtime)
                 .strftime('%Y-%m-%d %H:%M:%S')))
    return 0


def main():
    ap = argparse.ArgumentParser(description='工具包自保快照 / 校验 / 复活')
    ap.add_argument('--verify', action='store_true', help='只校验，不拍照')
    ap.add_argument('--list', action='store_true', help='列出现有快照')
    ap.add_argument('--restore', nargs='?', const='', default=None,
                    help='从快照补回缺失/损坏的文件（可指定快照文件名）')
    ap.add_argument('--keep', type=int, default=KEEP_DEFAULT, help='保留最新几份')
    a = ap.parse_args()

    print('=' * 78)
    print('  工具包自保快照')
    print('=' * 78)
    print('  工具包：%s' % HERE)
    print('  快照处：%s  （在工具包之外，被删时不会被一起带走）' % SNAP_DIR)
    print()

    if a.list:
        return do_list()
    if a.verify:
        return do_verify()
    if a.restore is not None:
        return do_restore(a.restore or None)

    do_snapshot(a.keep)
    print()
    return do_verify()


if __name__ == '__main__':
    sys.exit(main())
