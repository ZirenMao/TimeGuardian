#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb_selfsign.py —— 本地代码签名证书的「唯一实现」（建 / 查 / 删）
================================================================================
为什么需要它（2026-10-01 第卅九轮，用户原话：「根治『每次重编都掉权限』」）
--------------------------------------------------------------------------------
ad-hoc 签名（`codesign --sign -`）下，系统记的是 **cdhash**：

    当前 app 的指定要求 = designated => cdhash H"0123abcd…"

cdhash 是二进制内容的哈希 —— **重编一次就变一次**。而 TCC（管"辅助功能／设备
控制和数据访问"那套）存的是**授权当时的那个身份**，于是：

    重编 → cdhash 变 → 授权对不上 → 权限失效，必须去系统设置里重勾一次

这就是"每次重编都掉权限"的**全部原因**。它无解——除非把身份从"内容哈希"换成
"证书"。

★★ 换成证书之后，指定要求变成（实测）：
    designated => identifier "com.wb.workbuddy-toolkit"
                  and certificate root = H"4567ef89ad26bd02d07366e809688d2bcd1ce009"
    ⇒ 只要还用这张证书签，**重编多少次、换多少次图标，这行字一字不变** ⇒ 权限永久保留。

★ 关键实测结论（2026-10-01，在 /tmp 副本上验证，未动真身）：
  1. 证书**不需要**加进信任设置。自签证书的 DR 是「certificate root = H"…"」，
     自锚定 —— `codesign --verify --deep --strict` 返回 0。
     ⇒ 省掉 `security add-trusted-cert` 那一步（那一步会弹系统授权框）。
  2. `security find-identity -v` 会把这张证书报成 `0 valid identities found`
     （未受信任），**但 `codesign --sign <CN>` 照样能用**。所以本文件解析
     **不带 `-v`** 的输出。
  3. 证书走**独立钥匙串**（`_自签证书/workbuddy-sign.keychain-db`），
     口令随机生成存在 `keychain.pw`（600）—— 这样签名全程**免弹框、免 sudo**，
     也不去动用户的登录钥匙串。

用法
--------------------------------------------------------------------------------
    python3 wb_selfsign.py --install     # 建证书（幂等；已存在就只报状态）
    python3 wb_selfsign.py --status      # 看状态（证书在不在、指纹、能否被找到）
    python3 wb_selfsign.py --uninstall   # 拆掉（钥匙串 + 文件；之后回退 ad-hoc）
    python3 wb_selfsign.py --identity    # 只打印身份名（无证书时非 0 退出）

被谁调用
--------------------------------------------------------------------------------
`wb_appsign.py` 在签名前调 `identity()`：有证书就用证书签，没有就回退 ad-hoc。
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE     = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.path.join(HERE, '_自签证书')
KC       = os.path.join(CERT_DIR, 'workbuddy-sign.keychain-db')
PW_FILE  = os.path.join(CERT_DIR, 'keychain.pw')
CERT_PEM = os.path.join(CERT_DIR, 'cert.pem')
KEY_PEM  = os.path.join(CERT_DIR, 'key.pem')
P12      = os.path.join(CERT_DIR, 'cert.p12')
CONF     = os.path.join(CERT_DIR, 'cs.cnf')
CN       = 'WorkBuddy Local Signer'
DAYS     = 3650          # 10 年

CONF_BODY = """\
[ req ]
default_bits         = 2048
default_md           = sha256
prompt               = no
distinguished_name   = dn
x509_extensions      = code_sign_ext

[ dn ]
CN = %s
O  = WorkBuddy Local Tools
C  = CN

[ code_sign_ext ]
basicConstraints     = critical, CA:FALSE
keyUsage             = critical, digitalSignature
extendedKeyUsage     = critical, codeSigning
subjectKeyIdentifier = hash
""" % CN


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True,
                          errors='replace', **kw)


def read_pw():
    try:
        with open(PW_FILE, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def unlock():
    """解锁独立钥匙串。成功返回 True。

    ⚠️ 重启后钥匙串是锁着的 —— 每次签名前都要解一次（口令就存在旁边，600）。
    """
    pw = read_pw()
    if not pw or not os.path.exists(KC):
        return False
    r = _run(['security', 'unlock-keychain', '-p', pw, KC])
    return r.returncode == 0


def identity():
    """找可用的签名身份。返回 dict 或 None。

    ★ 解析的是**不带 `-v`** 的输出 —— `-v` 只列"受信任"的身份，而自签证书
      在没加信任设置时会被报成 CSSMERR_TP_NOT_TRUSTED，可是它**照样能签名**。
      输出形如：
          Policy: Code Signing
            Matching identities
            1) F3ED1173… "WorkBuddy Local Signer" (CSSMERR_TP_NOT_TRUSTED)
    """
    if not (os.path.exists(KC) and os.path.exists(PW_FILE)):
        return None
    r = _run(['security', 'find-identity', '-p', 'codesigning', KC])
    out = (r.stdout or '') + (r.stderr or '')
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln or ')' not in ln[:4]:
            continue
        parts = ln.split(')', 1)
        if len(parts) != 2:
            continue
        rest = parts[1].strip()
        fp = rest.split(' ', 1)[0]
        if len(fp) != 40:
            continue
        if '"%s"' % CN not in rest:
            continue
        return {'name': CN, 'keychain': KC, 'fingerprint': fp.lower()}
    return None


def cert_info():
    """证书本身的信息（不看钥匙串）。返回 dict 或 None。"""
    if not os.path.exists(CERT_PEM):
        return None
    r = _run(['openssl', 'x509', '-in', CERT_PEM, '-noout',
              '-subject', '-dates', '-fingerprint'])
    txt = (r.stdout or '') + (r.stderr or '')
    info = {'raw': txt.strip()}
    for ln in txt.splitlines():
        ln = ln.strip()
        if ln.startswith('notAfter='):
            info['not_after'] = ln.split('=', 1)[1]
    try:
        r2 = _run(['security', 'find-certificate', '-c', CN, '-p', KC])
        info['in_keychain'] = r2.returncode == 0
    except Exception:
        info['in_keychain'] = False
    return info


def install(force=False):
    """生成并导入证书。幂等：已装好就直接返回。"""
    if not force and identity():
        print('  · 证书已就绪，无需重装（--force 可强制重建）')
        return True
    if not shutil.which('openssl'):
        print('  ✗ 找不到 openssl')
        return False

    os.makedirs(CERT_DIR, exist_ok=True)
    os.chmod(CERT_DIR, 0o700)

    # ① 证书配置
    with open(CONF, 'w', encoding='utf-8') as f:
        f.write(CONF_BODY)

    # ② 私钥 + 自签证书
    print('  · 生成私钥与自签证书（%d 天）...' % DAYS)
    r = _run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
              '-sha256', '-days', str(DAYS),
              '-keyout', KEY_PEM, '-out', CERT_PEM, '-config', CONF])
    if r.returncode != 0 or not os.path.exists(CERT_PEM):
        print('  ✗ 生成失败：%s' % ((r.stderr or r.stdout or '').strip()[:200],))
        return False
    os.chmod(KEY_PEM, 0o600)
    os.chmod(CERT_PEM, 0o644)

    # ③ 独立钥匙串（随机口令，存 600 文件 ⇒ 全程免弹框、免 sudo）
    pw = _run(['openssl', 'rand', '-base64', '24']).stdout.strip()
    pw = pw.replace('/', '').replace('+', '').replace('=', '')[:24]
    if not pw:
        print('  ✗ 生成钥匙串口令失败')
        return False
    with open(PW_FILE, 'w', encoding='utf-8') as f:
        f.write(pw)
    os.chmod(PW_FILE, 0o600)

    print('  · 建独立钥匙串 ...')
    if os.path.exists(KC):
        _run(['security', 'delete-keychain', KC])
    for args in (['security', 'create-keychain', '-p', pw, KC],
                 ['security', 'set-keychain-settings', KC],
                 ['security', 'unlock-keychain', '-p', pw, KC]):
        if _run(args).returncode != 0:
            print('  ✗ 失败：%s' % (' '.join(args[1:3]),))
            return False

    # ④ 打成 p12 再导入（-A：免"某程序想使用钥匙串"的弹框）
    print('  · 导入证书与私钥 ...')
    r = _run(['openssl', 'pkcs12', '-export', '-inkey', KEY_PEM, '-in', CERT_PEM,
              '-name', CN, '-out', P12, '-passout', 'pass:' + pw])
    if r.returncode != 0:
        print('  ✗ p12 导出失败：%s' % ((r.stderr or '').strip()[:200],))
        return False
    os.chmod(P12, 0o600)
    r = _run(['security', 'import', P12, '-k', KC, '-P', pw, '-A',
              '-T', '/usr/bin/codesign', '-T', '/usr/bin/security'])
    if r.returncode != 0:
        print('  ✗ 导入失败：%s' % ((r.stderr or r.stdout or '').strip()[:200],))
        return False

    # ⑤ 分区列表（少了它，每次签名都弹一次钥匙串口令框）
    _run(['security', 'set-key-partition-list',
          '-S', 'apple-tool:,apple:', '-s', '-k', pw, KC])

    ident = identity()
    if not ident:
        print('  ✗ 导入后仍找不到签名身份 —— 检查 openssl 是否给了 codeSigning 扩展')
        return False
    print('  ✓ 就绪：%s（fingerprint=%s）' % (CN, ident['fingerprint']))
    return True


def status(verbose=True):
    ident = identity()
    info = cert_info()
    if verbose:
        print('  证书目录：%s' % CERT_DIR)
        if not info:
            print('  · 没有证书（签名会回退 ad-hoc ⇒ 每次重编都要重勾权限）')
            return 1
        for ln in (info.get('raw') or '').splitlines():
            print('    %s' % ln)
        if info.get('not_after'):
            print('    到期：%s' % info['not_after'])
        print('  钥匙串：%s' % ('在' if os.path.exists(KC) else '缺'))
        if ident:
            print('  ✓ 可用签名身份：%s（%s）' % (ident['name'], ident['fingerprint']))
            print('    ⇒ 用它签名后，重编不会再掉权限。')
            return 0
        print('  ✗ 钥匙串里找不到身份（--install 重建）')
        return 1
    return 0 if ident else 1


def uninstall():
    """拆掉证书与钥匙串，回到 ad-hoc（权限会再掉一次，需要重勾）。"""
    if os.path.exists(KC):
        _run(['security', 'delete-keychain', KC])
    for f in (P12,):
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass
    print('  · 已删钥匙串。证书文件仍留在 %s（想彻底清就整目录删掉）' % CERT_DIR)
    return 0


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--install', action='store_true')
    g.add_argument('--status', action='store_true')
    g.add_argument('--uninstall', action='store_true')
    g.add_argument('--identity', action='store_true')
    g.add_argument('--force', action='store_true',
                   help='配合 --install：已存在也重建')
    a = ap.parse_args()

    if a.identity:
        ident = identity()
        if not ident:
            return 1
        print(ident['name'])
        return 0
    if a.status:
        return status()
    if a.uninstall:
        return uninstall()
    ok = install(force=a.force)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
