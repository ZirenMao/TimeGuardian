#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb_appsign.py —— app 的「签名 + 校验」唯一实现
================================================================================
为什么单独一个文件
--------------------------------------------------------------------------------
2026-10-01（第卅八轮）用户报：「我都已经授权了，界面还是说没拿到权限」。
查下来是**两层叠加**，真凶在这一层：

  `make_icon.py` 换图标时会重写 `Contents/Info.plist`（补 `CFBundleIconFile`
  / `CFBundleIconName`，再 `bump_version()` 把小版本号 +0.1），**改完不重签**。
  Info.plist 的哈希是封进签名里的 —— 一改，`codesign --verify` 就报
  `invalid Info.plist (plist or signature have been modified)`。
  签名一坏，TCC 校验不了这条授权 → `AXIsProcessTrusted()` 恒 false。
  可「系统设置 → 隐私与安全性 → 设备控制和数据访问」那一栏是**按名字列行、
  不校验签名**的，开关照样是蓝的 —— 用户看到"开着"、程序说"没权限"，
  **两边都没说谎**。

第卅九轮（同日）用户接着要求「**根治**『每次重编都掉权限』」，于是本文件多了
「证书优先」这条路 —— 详见下面「两种签名身份」。

两种签名身份（本文件自动择优）
--------------------------------------------------------------------------------
| | ad-hoc（`--sign -`） | 证书（`wb_selfsign.py` 建的自签证书） |
|---|---|---|
| 指定要求（DR） | `cdhash H"0123abcd…"` | `identifier "com.wb…" and certificate root = H"4567ef89…"` |
| 重编后 | **变** ⇒ 权限失效，要重勾 | **不变** ⇒ 权限保留 |
| 依赖 | 无 | `_自签证书/`（私钥 + 独立钥匙串） |

★ 判定方式：读 `codesign -d -r-` 打出来的**指定要求** —— 里面是 `cdhash` 就是
  ad-hoc，是 `certificate root`/`certificate leaf` 就是证书签的。
  （**不看** `security find-identity -v`：它在没加信任设置时会把自签证书报成
  `0 valid identities found`，可那张证书**照样能用**。实测结论，别被它骗。）

用法
--------------------------------------------------------------------------------
    python3 wb_appsign.py --sign     # 重签 + 校验 + 更新记录（失败非 0 退出）
    python3 wb_appsign.py --check    # 只校验（门禁用；签名坏了非 0 退出）
    python3 wb_appsign.py --cdhash   # 只打印 cdhash
    python3 wb_appsign.py --kind     # 打印当前签名身份（cert:… / adhoc）
    python3 wb_appsign.py --dr       # 打印指定要求（排障用）

谁调用它
--------------------------------------------------------------------------------
    · 装 app  → `_build_app.sh`
    · 换图标  → `make_icon.py`（`resign()`）
**凡动过 Info.plist 就必须重签** —— 这两条路都走本文件，别再各写一份。
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

APP       = '/Applications/TimeGuardian.app'
BID       = 'com.wb.timeguardian-mac'
CDH_FILE  = os.path.join(HERE, '_外壳.cdhash')
KIND_FILE = os.path.join(HERE, '_外壳.signkind')


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True,
                          errors='replace', **kw)


def _read(p):
    try:
        with open(p, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def _write(p, s):
    try:
        with open(p, 'w', encoding='utf-8') as f:
            f.write(s)
    except OSError:
        pass


def cdhash(app=APP):
    """取当前 app 的 cdhash。ad-hoc 签名下，TCC 记的就是这个值。"""
    r = _run(['codesign', '-dv', '--verbose=4', app])
    # ⚠️ codesign 的 -dv 输出走 stderr（老代码里靠 `2>&1` 兜的），这里两边都看。
    for ln in ((r.stderr or '') + (r.stdout or '')).splitlines():
        ln = ln.strip()
        if ln.startswith('CDHash='):
            return ln.split('=', 1)[1].strip()
    return ''


def designated_req(app=APP):
    """打印 app 的指定要求（DR）。**判定签名身份的唯一依据**。

    ad-hoc   → `cdhash H"…"`
    证书签名 → `identifier "…" and certificate root = H"…"`（自锚定，不随内容变）
    """
    r = _run(['codesign', '-d', '-r-', app])
    for ln in ((r.stdout or '') + (r.stderr or '')).splitlines():
        ln = ln.strip()
        if ln.startswith('designated') or ln.startswith('# designated'):
            return ln.split('=>', 1)[-1].strip()
    return ''


def signature_kind(app=APP):
    """返回 'cert:<指纹>' / 'adhoc' / ''（读不出）。"""
    dr = designated_req(app)
    if not dr:
        return ''
    if 'certificate root' in dr or 'certificate leaf' in dr:
        fp = ''
        if 'H"' in dr:
            fp = dr.split('H"', 1)[1].split('"', 1)[0]
        return 'cert:' + fp.lower()
    if 'cdhash H"' in dr:
        return 'adhoc'
    return ''


def kind_label(kind):
    if kind.startswith('cert:'):
        return '证书（%s…）' % kind[5:17]
    if kind == 'adhoc':
        return 'ad-hoc（临时的，重编即变）'
    return '未知'


def verify(app=APP):
    """签名静态校验。返回 (ok, 说明)。"""
    r = _run(['codesign', '--verify', '--deep', '--strict', app])
    msg = ((r.stderr or '') + (r.stdout or '')).strip()
    return r.returncode == 0, msg


def _sign_args(app, bid):
    """拼签名命令。**有证书就用证书** —— 这是「根治」的落点。"""
    try:
        import wb_selfsign as ss
        ident = ss.identity()
    except Exception:
        ident = None
    if ident:
        ss.unlock()                    # 重启后钥匙串是锁的，签名前必须解一次
        return ['codesign', '--force', '--keychain', ident['keychain'],
                '--sign', ident['name'], '--identifier', bid, app], ident
    return ['codesign', '--force', '--sign', '-', '--identifier', bid, app], None


def _sign_in_place(app, bid):
    args, ident = _sign_args(app, bid)
    r = _run(args)
    return r.returncode == 0, ((r.stdout or '') + (r.stderr or '')).strip(), ident


def _sweep_cstemp(app):
    """清掉 codesign 失败时可能留下的 `*.cstemp` 残渣（2026-10-01 踩过）。"""
    for dp, _, fs in os.walk(app):
        for fn in fs:
            if fn.endswith('.cstemp'):
                try:
                    os.remove(os.path.join(dp, fn))
                except OSError:
                    pass


def sign(app=APP, bid=BID, quiet=False):
    """就地把 app 重签一遍，**并自己校验**。返回 (ok, msg)。

    ★ 为什么备了「/tmp 里签好再拷回」这条退路：
      本工具一大半进程跑在 seatbelt 沙箱里，`codesign --force` 在 /Applications 下
      要**替换**已存在的 `_CodeSignature`，会被安全规则拦下（打印
      `replacing existing signature` 之后失败，还留下 `*.cstemp` 残渣）。
      实测可行的路：把 bundle 复制到 /tmp 签好，再把**签名产物**拷回真身 ——
      Mach-O 的签名**嵌在二进制内部**，bundle 的封印是 `_CodeSignature/CodeResources`。
      ⚠️ **绝不拷 Info.plist**：真身那份才是准的（/tmp 那份是从它复制的、内容相同，
         所以拷回的 CodeResources 里记的哈希依然对得上）。
    """
    def say(s):
        if not quiet:
            print(s)

    if not os.path.isdir(app):
        return False, 'app 不存在：%s' % app

    ok, msg, ident = _sign_in_place(app, bid)
    if ok:
        vok, vmsg = verify(app)
        if vok:
            _sweep_cstemp(app)
            return True, ('证书签名成功' if ident else '就地签名成功（ad-hoc）')
        say('  · 就地签名返回 0，但校验不过：%s' % vmsg)
    else:
        lines = msg.splitlines() or ['']
        say('  · 就地签名没成（%s）→ 走「/tmp 里签好再拷回」的退路' % lines[0])

    tmp = tempfile.mkdtemp(prefix='wbsign-')
    try:
        dst = os.path.join(tmp, os.path.basename(app))
        _run(['ditto', app, dst])
        if not os.path.isdir(dst):
            return False, '复制到 /tmp 失败'
        ok2, msg2, _ = _sign_in_place(dst, bid)
        if not ok2:
            return False, '在 /tmp 里也签不上：%s' % msg2
        # 只把「签名产物」拷回真身
        for sub in (('Contents', 'MacOS'), ('Contents', '_CodeSignature')):
            s_dir = os.path.join(dst, *sub)
            d_dir = os.path.join(app, *sub)
            if not os.path.isdir(s_dir):
                continue
            os.makedirs(d_dir, exist_ok=True)
            for fn in os.listdir(s_dir):
                sf, df = os.path.join(s_dir, fn), os.path.join(d_dir, fn)
                if not os.path.isfile(sf):
                    continue
                shutil.copy2(sf, df)
                if sub[-1] == 'MacOS':          # 可执行位不许丢
                    os.chmod(df, 0o755)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    _sweep_cstemp(app)
    vok, vmsg = verify(app)
    return vok, ('退路签名成功' if vok else '退路签名后校验仍不过：%s' % vmsg)


# ---------------------------------------------------------------------------
# 提示语（三条，别混）
# ---------------------------------------------------------------------------
ADVISORY_ADHOC = """\
⚠️  外壳是 ad-hoc 签名，而它（cdhash）**变了** —— 之前给过的「辅助功能」权限已失效。
    症状：系统设置里那一行看着还是开的，但平滑滚动 / 键盘映射 / 点 Dock 图标最小化不生效。
    修法（这次）：系统设置 → 隐私与安全性 → 设备控制和数据访问
                  （macOS 26 及更早这一栏叫「辅助功能」）
                  → 把「WorkBuddy任务管家」的开关【关掉、隔两秒再打开】。
    根治（一劳永逸）：python3 wb_selfsign.py --install
                  建一份本地自签证书 ⇒ 身份固定在证书上，以后**重编多少次都不用再勾**。\
"""

ADHOC_REGRESS = """\
⚠️  签名身份**从证书退回 ad-hoc** 了 —— 权限又要开始"每次重编掉一次"。
    常见原因：`_自签证书/` 被删、钥匙串没锁上、或有人手动跑了 `codesign --sign -`。
    回到证书：python3 wb_selfsign.py --status（看缺什么）→ --install（重建）。\
"""

CERT_SWITCH = """\
★ 外壳已改用「本地自签证书」签名 —— 身份从此固定在证书上，
  以后**重编、换图标都不会再掉权限**。

  ⚠️ 但正因为身份换了（指定要求从 cdhash 变成证书根），
     这一次是【最后一次】需要重勾：

     系统设置 → 隐私与安全性 → 设备控制和数据访问
     （macOS 26 及更早叫「辅助功能」）
     → 把「WorkBuddy任务管家」的开关【关掉、隔两秒再打开】。

  勾完就永久生效。界面上那条提示不挂后台轮询 —— 下次打开面板会自测一次，
  通过了它就自己消失、功能自己开始工作。\
"""


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--sign', action='store_true', help='重签 + 校验 + 更新记录')
    g.add_argument('--check', action='store_true', help='只校验签名')
    g.add_argument('--cdhash', action='store_true', help='只打印 cdhash')
    g.add_argument('--kind', action='store_true', help='只打印签名身份')
    g.add_argument('--dr', action='store_true', help='打印指定要求（排障）')
    a = ap.parse_args()

    if a.cdhash:
        print(cdhash())
        return 0
    if a.kind:
        print(signature_kind())
        return 0
    if a.dr:
        print(designated_req())
        return 0

    if a.check:
        ok, msg = verify()
        kind = signature_kind()
        # ★★ 第卅九轮：`--check` 顺手把两份留痕对齐。
        #   为什么放在这里：门禁 `[6c]` 每次都跑 `--check` ⇒ 只要跑过一次回归或体检，
        #   记录必然与盘上一致，不会再出现"文件里记的是上一代值"。
        #   （原来只有 `--sign` 才写，而换图标 / 重注册这些路径不经过它 —— 2026-10-01
        #     实际踩到：文件停在 14:11 那一代，盘上已经是 14:40 那一代。）
        if kind:
            _write(KIND_FILE, kind)
        ch = cdhash()
        if ch:
            _write(CDH_FILE, ch)
        if ok:
            print('  ✓ 签名有效（身份：%s）' % kind_label(kind))
            if kind.startswith('cert'):
                print('    指定要求与内容无关 ⇒ 重编不会掉权限')
            else:
                print('    指定要求＝cdhash ⇒ **每次重编都要重勾一次**；'
                      '根治：python3 wb_selfsign.py --install')
            return 0
        print('  ✗ 签名无效：%s' % msg)
        print('    → 就地修：python3 wb_appsign.py --sign')
        return 1

    # ---- --sign ----
    old_kind = _read(KIND_FILE)
    cdh_old = _read(CDH_FILE)

    ok, msg = sign()
    if not ok:
        print('  ✗ 签名失败：%s' % msg)
        return 1

    new_kind = signature_kind()
    cdh_new = cdhash()
    print('  ✓ %s（Identifier=%s，身份=%s，CDHash=%s…）'
          % (msg, BID, kind_label(new_kind), (cdh_new or '')[:12]))

    switched_to_cert = new_kind.startswith('cert') and new_kind != old_kind
    switched_to_adhoc = (new_kind == 'adhoc') and old_kind.startswith('cert')
    cdh_changed = bool(cdh_new and cdh_old and cdh_new != cdh_old)

    if switched_to_cert:
        print()
        print(CERT_SWITCH)
        print()
    elif switched_to_adhoc:
        print()
        print(ADHOC_REGRESS)
        print()
    elif new_kind == 'adhoc' and cdh_changed:
        print()
        print(ADVISORY_ADHOC)
        print()
    else:
        print('  · 签名身份未变（%s）⇒ 已授权的权限不受影响' % kind_label(new_kind))

    if cdh_new:
        _write(CDH_FILE, cdh_new)
    if new_kind:
        _write(KIND_FILE, new_kind)
    return 0


if __name__ == '__main__':
    sys.exit(main())
