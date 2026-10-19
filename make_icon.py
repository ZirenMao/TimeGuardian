#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_icon.py —— 从 WorkBuddy 原图标生成「同形状 · 换颜色」的图标
================================================================================

用途：WorkBuddy任务管家.app 需要一个和 WorkBuddy 同款（圆角方块 + 猫头）
但颜色不同的图标，便于在访达里一眼区分。

做法（关键在"别用色相旋转"）：
  1. 从 /Applications/WorkBuddy.app/Contents/Resources/icon.icns 解出 1024px 原图；
  2. 底色原本是青绿 H≈165°、V≈0.78；白色吉祥物区其实是**薄荷绿白**（B>R），
     直接整体色相旋转的话，白区会因为对比而显蓝/发青，而且黄色发暗发橄榄；
  3. 所以改成 **全局统一色相 + 保留原 S/V 结构**：
       · 所有像素的色相统一设为目标黄色（默认 52°）
       · S、V 按原图逐像素保留（V 整体提亮到 ~0.97，黄色才够亮）
     → 低饱和的白区自然变成"暖白"，与黄底协调；吉祥物上的绿色阴影
       自然变成黄色阴影，明暗结构一模一样。
  4. 生成完整 iconset → iconutil 打包成 .icns。

与 _gen_icon.py 的分工（2026-09-27 起 · 2026-10-01 调整）
--------------------------------------------------------------------------------
    本脚本 = **只管换颜色**，产出 `_assets/icon_1024_yellow.png`（沿用官方图**原样**的
             几何：内容 820/1024、四周留白 102 px）＋ `_assets/AppIcon_yellow.icns`。
    _gen_icon.py = **只管几何 + 安装**：按 `--geom` 把主图摆正（mac = 820 见方居中，
             win = 满幅），缩放打包、装进 app、重注册 LaunchServices。

    ★ 所以本脚本"安装"那一步是**交给 _gen_icon.py 做**的（见 build() 结尾）——
      两个脚本各装各的会打架；统一在 _gen_icon 那里收口。
      两张中间产物都留在 `_assets/`，随时可回退比对。

    ★★ 2026-10-01 起 `_gen_icon.py` 的默认几何是 **mac**（Apple 网格），不再是满幅：
      用户拉开「应用程序」堆栈时发现两个 app 图标大小不一致 —— 满幅那颗在 macOS 的
      Dock 里是唯一的异类。满幅那颗仍可用 `--geom win` 生成，给 Windows 端用。

用法：
  python3 make_icon.py                       # 生成到 _assets/ 并装到 app
  python3 make_icon.py --deg 45              # 换别的黄（40 暖橙黄 / 45 金黄 / 52 正黄）
  python3 make_icon.py --no-install          # 只生成，不装
  python3 _gen_icon.py --check               # 验收：与 Windows 那颗 ico 的逐像素差异
"""
import os
import sys
import shutil
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, '_assets')
WORK = os.path.join(ASSETS, '_iconset_work')
APP = '/Applications/WorkBuddy任务管家.app'
APP_PLIST = os.path.join(APP, 'Contents', 'Info.plist')
SRC_ICNS = '/Applications/WorkBuddy.app/Contents/Resources/icon.icns'
LSREG = ('/System/Library/Frameworks/CoreServices.framework/Frameworks/'
         'LaunchServices.framework/Support/lsregister')

# iconset 需要的全部规格（文件名 → 像素）
SPEC = {
    'icon_16x16.png': 16,   'icon_16x16@2x.png': 32,
    'icon_32x32.png': 32,   'icon_32x32@2x.png': 64,
    'icon_128x128.png': 128, 'icon_128x128@2x.png': 256,
    'icon_256x256.png': 256, 'icon_256x256@2x.png': 512,
    'icon_512x512.png': 512, 'icon_512x512@2x.png': 1024,
}


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# 本工具的其它脚本只用标准库，所以走的是 WorkBuddy 自带的 Python（干净、随叫随到）。
# 但换图标要用 Pillow，而那个解释器里没装 —— 不动 WorkBuddy 的环境，
# 改成自动找一个"带 Pillow 的解释器"把自己重跑一遍。
# --------------------------------------------------------------------------
def _pil_capable_python():
    """找一个能 import PIL 的解释器；找不到返回 None。"""
    cands = [
        os.path.expanduser('~/.workbuddy/binaries/python/envs/default/bin/python'),
        os.path.expanduser('~/.workbuddy/binaries/python/envs/default/bin/python3'),
        '/opt/homebrew/bin/python3',
        '/usr/local/bin/python3',
    ]
    me = os.path.abspath(sys.executable)
    for c in cands:
        # ⚠️ 这里只能比**路径**，不能用 realpath：
        #    venv 里的 bin/python 是指向 versions/<ver>/bin/python3.13 的符号链接，
        #    realpath 会解析成同一个文件，导致"明明有 Pillow 却被跳过"
        #    （2026-09-12 踩过）。
        if not os.path.exists(c) or os.path.abspath(c) == me:
            continue
        try:
            r = subprocess.run([c, '-c', 'import PIL'], capture_output=True, timeout=25)
        except Exception:
            continue
        if r.returncode == 0:
            return c
    return None


def ensure_pillow(script=None):
    """确保当前进程能 import PIL；必要时换解释器重跑自己。

    ★ `script` —— 换解释器时要重跑哪个脚本。默认是本文件；
      `_gen_icon.py` 借用本函数时必须把**它自己**传进来，
      否则会变成「用带 Pillow 的解释器重跑 make_icon.py，却带着 _gen_icon 的参数」
      （2026-09-27 移植时发现：_gen_icon 复用了这里的 ensure_pillow）。
    """
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        pass

    if os.environ.get('WB_ICON_REEXEC') == '1':
        log('✗ 换了解释器仍然没有 Pillow —— 请先安装：')
        log('    %s -m pip install Pillow' % sys.executable)
        return False

    alt = _pil_capable_python()
    if not alt:
        log('✗ 找不到带 Pillow 的 Python。装一个即可，例如：')
        log('    %s -m pip install Pillow' % sys.executable)
        return False

    target = os.path.abspath(script or __file__)
    log('· 当前解释器没有 Pillow，改用 %s 重跑' % alt)
    env = dict(os.environ, WB_ICON_REEXEC='1')
    os.execve(alt, [alt, target] + sys.argv[1:], env)
    return False        # execve 成功就不会走到这


def build(deg, install=True):
    from PIL import Image

    if not os.path.exists(SRC_ICNS):
        log('✗ 找不到原图标：%s' % SRC_ICNS)
        return 1

    os.makedirs(WORK, exist_ok=True)
    iconset = os.path.join(WORK, 'src.iconset')
    if os.path.isdir(iconset):
        shutil.rmtree(iconset)

    log('① 解包 WorkBuddy 原图标 ...')
    r = subprocess.run(['iconutil', '-c', 'iconset', SRC_ICNS, '-o', iconset],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log('  ✗ iconutil 失败：%s' % (r.stderr or r.stdout))
        return 1

    big = os.path.join(iconset, 'icon_512x512@2x.png')
    if not os.path.exists(big):
        big = os.path.join(iconset, 'icon_512x512.png')
    im = Image.open(big).convert('RGBA')
    log('  ✓ 源图 %dx%d' % im.size)

    log('② 换色（全局色相 %.0f°，保留原明暗结构）...' % deg)
    A = im.getchannel('A')
    H, S, V = im.convert('HSV').split()
    # 原底色 V≈0.78；提到 0.97 黄色才亮。用固定比例，保留相对明暗。
    vgain = 0.97 / 0.78
    V2 = V.point(lambda p: min(255, int(p * vgain)))
    H2 = Image.new('L', im.size, int(round(deg / 360.0 * 255)))
    rgb = Image.merge('HSV', (H2, S, V2)).convert('RGB')
    out = Image.merge('RGBA', rgb.split() + (A,))

    log('③ 打包 iconset → .icns ...')
    dst_iconset = os.path.join(WORK, 'yellow.iconset')
    os.makedirs(dst_iconset, exist_ok=True)
    for name, px in SPEC.items():
        out.resize((px, px), Image.LANCZOS).save(os.path.join(dst_iconset, name))
    icns = os.path.join(ASSETS, 'AppIcon_yellow.icns')
    r = subprocess.run(['iconutil', '-c', 'icns', dst_iconset, '-o', icns],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log('  ✗ 打包失败：%s' % (r.stderr or r.stdout))
        return 1
    out.save(os.path.join(ASSETS, 'icon_1024_yellow.png'))
    log('  ✓ %s' % icns)
    # 用完即删（中间产物不留在目录里）；删不掉不算错 —— 受限环境里 rmtree 会被拦。
    try:
        shutil.rmtree(WORK)
    except OSError:
        pass

    if not install:
        log('（--no-install，未装到 app）')
        return 0

    if not os.path.isdir(APP):
        log('· app 不存在，跳过安装：%s' % APP)
        return 0

    # ★ 2026-09-27 改：**安装这一步交给 _gen_icon.py**。
    #   ★★ 2026-10-01 再改：_gen_icon.py 的默认几何已从「满幅」换成 **mac 几何**
    #      （Apple 网格，与 WorkBuddy 官方图标同轮廓）—— 所以这里传什么参数都不用动，
    #      跟着它走就对了。要满幅（给 Windows 端用）就显式跑 `_gen_icon.py --geom win`。
    #   本脚本上面那张 icon_1024_yellow.png 是**沿用官方图原样几何**的中间产物，
    #   留白/去留白这一层统一归 _gen_icon.py 管。
    helper = os.path.join(HERE, '_gen_icon.py')
    if os.path.exists(helper):
        log('④ 交给 _gen_icon.py 安装（默认 mac 几何 → 与官方图标同轮廓）...')
        r = subprocess.run([sys.executable, helper], cwd=HERE)
        if r.returncode == 0:
            return 0
        log('  ! _gen_icon.py 失败（rc=%s）→ 退回按 macOS 惯例直接安装' % r.returncode)

    log('④ 装到 app ...')
    return install_icns(icns)


# --------------------------------------------------------------------------
# 安装 + 让访达真正认到新图标
#
# ⚠️ 2026-09-12 踩坑记录（换了图标但访达一直显示空白通用图标）：
#   根因**不是**缓存，而是这个 app 的记录**从 LaunchServices 数据库里丢了**。
#   查法：`lsregister -dump | grep workbuddy-toolkit` 若 0 命中，就是没注册。
#   正确记录必须含这两行（可对照 /Applications/WorkDaddy.app）：
#       icon flags: relative-icon-path  supports-asset-catalog
#       icons:      Contents/Resources/AppIcon.icns
#   另外 launch-disabled 标记是正常的（WorkDaddy 也有），不是问题。
#   备忘：NSWorkspace.iconForFile_ 会直接读 bundle 里的 icns，
#         即使 LS 没注册也返回正确图标 —— 所以它**不能**用来验证访达显示。
# --------------------------------------------------------------------------
def _plist(args):
    return subprocess.run(['plutil'] + args + [APP_PLIST],
                          capture_output=True, text=True)


def refresh_icon_services():
    """重启承载图标的 UI 进程（不需要 sudo）。"""
    for p in ('iconservicesagent', 'Dock', 'Finder'):
        r = subprocess.run(['killall', p], capture_output=True, text=True)
        log('  · 重启 %-18s %s' % (p, 'ok' if r.returncode == 0 else '（未运行）'))


def verify_registration():
    """确认 app 已在 LaunchServices 数据库里注册。"""
    if not os.path.exists(LSREG):
        return None
    r = subprocess.run([LSREG, '-dump'], capture_output=True, text=True,
                       errors='replace')
    out = r.stdout
    has_id = 'com.wb.workbuddy-toolkit' in out
    has_icon = 'Contents/Resources/AppIcon.icns' in out
    return has_id, has_icon


def resign(reason):
    """改过 Info.plist 之后**必须**重签一遍 —— 否则签名封印失效、授权作废。

    ★★★ 2026-10-01（第卅八轮）用户报「我明明已经授权了，界面还说没拿到权限」，
      真凶就是这里：本脚本换图标时会补 `CFBundleIconFile` / `CFBundleIconName`、
      再 `bump_version()` 把小版本号 +0.1，**改完不重签**。Info.plist 的哈希是封在
      签名里的 —— 一改，`codesign --verify` 就报 `invalid Info.plist`；
      签名一坏，TCC 校验不了授权，`AXIsProcessTrusted()` 恒 false。
      而系统设置那栏**按名字列行、不校验签名**，开关照样是蓝的
      ⇒ 用户看到"开着"、程序说"没权限"，两边都没说谎。

    签名逻辑收归 `wb_appsign.py`（与 `_build_app.sh` 同一入口），签完自校验。
    """
    try:
        import wb_appsign
    except Exception as e:                       # 极端情况（文件被删/挪走了）
        log('  ✗ 找不到 wb_appsign.py（%r）' % (e,))
        log('    → 手工跑一次 `bash _build_app.sh` 兜底（它会重新签）')
        return False
    ok, msg = wb_appsign.sign(APP, quiet=True)
    if ok:
        log('  ✓ 已重新签名（%s）—— %s' % (msg, reason))
    else:
        log('  ✗ 重新签名失败：%s' % msg)
        log('    → 手工跑一次 `bash _build_app.sh` 兜底')
    return ok


def install_icns(icns_path):
    """把 icns 装进 app，并确保访达能显示出来。"""
    if not os.path.isdir(APP):
        log('· app 不存在，跳过安装：%s' % APP)
        return 0

    res = os.path.join(APP, 'Contents', 'Resources')
    os.makedirs(res, exist_ok=True)
    shutil.copy2(icns_path, os.path.join(res, 'AppIcon.icns'))
    subprocess.run(['chmod', '644', os.path.join(res, 'AppIcon.icns')],
                   capture_output=True)

    # 两个键都写（与 WorkDaddy 一致）；有 CFBundleIconName 时系统才会走
    # asset-catalog 那条路径，缺了它图标常常不生效。
    for key in ('CFBundleIconFile', 'CFBundleIconName'):
        r = _plist(['-replace', key, '-string', 'AppIcon'])
        if r.returncode != 0:
            _plist(['-insert', key, '-string', 'AppIcon'])

    # 换版本号 —— 让 LaunchServices 认为这是一个"新版本"，重建图标记录
    bump_version()
    subprocess.run(['touch', APP], capture_output=True)

    log('  · 重新注册到 LaunchServices ...')
    if os.path.exists(LSREG):
        subprocess.run([LSREG, '-f', APP], capture_output=True)
        v = verify_registration()
        if v:
            has_id, has_icon = v
            log('    · 注册：%s   图标记录：%s'
                % ('已登记' if has_id else '✗ 缺失',
                   '已登记' if has_icon else '✗ 缺失'))
            if not (has_id and has_icon):
                log('    ⚠️ 注册不完整 —— 换菜单 12「重新注册 app」再试一次')

    log('  · 刷新访达图标 ...')
    refresh_icon_services()
    # ★★★ 重签**必须在所有 Info.plist 改动之后**：上面 bump_version() 动过 plist，
    #   不重签签名就坏了（当天事故的根因）→ 辅助功能权限从此失效。
    resign('改过 Info.plist，重签以保住签名有效性')
    log('  ✓ 完成。')
    return 0


def bump_version():
    """把 CFBundleVersion / CFBundleShortVersionString 的小版本号 +0.1。"""
    r = _plist(['-extract', 'CFBundleShortVersionString', 'raw'])
    cur = (r.stdout or '').strip() or '1.0'
    try:
        major, minor = cur.split('.')[:2]
        new = '%s.%d' % (major, int(minor) + 1)
    except Exception:
        new = '1.1'
    _plist(['-replace', 'CFBundleShortVersionString', '-string', new])
    _plist(['-replace', 'CFBundleVersion', '-string', new])
    log('  · 版本号 %s → %s（强制系统重建图标记录）' % (cur, new))
    return new


def repair():
    """只重新注册 + 刷新访达，不重新生成图标（图标不显示时用这个）。"""
    if not os.path.isdir(APP):
        log('✗ app 不存在：%s' % APP)
        return 1
    log('① 检查图标资源 ...')
    ic = os.path.join(APP, 'Contents', 'Resources', 'AppIcon.icns')
    if not os.path.exists(ic):
        log('  ✗ 缺少 %s —— 请先跑一次生成图标（不带 --repair）' % ic)
        return 1
    log('  ✓ %.1f KB' % (os.path.getsize(ic) / 1024.0))

    log('② 修正 Info.plist ...')
    for key in ('CFBundleIconFile', 'CFBundleIconName'):
        r = _plist(['-replace', key, '-string', 'AppIcon'])
        if r.returncode != 0:
            _plist(['-insert', key, '-string', 'AppIcon'])
        log('  · %s = AppIcon' % key)
    # CFBundleSignature 是 Windows 时代的遗留键，参照物没有它，清掉更干净
    _plist(['-remove', 'CFBundleSignature'])
    bump_version()

    log('③ 重新注册 ...')
    subprocess.run(['touch', APP], capture_output=True)
    if os.path.exists(LSREG):
        subprocess.run([LSREG, '-f', APP], capture_output=True)
        v = verify_registration()
        if v:
            log('  · bundle id 记录：%s' % ('✓' if v[0] else '✗'))
            log('  · icns 图标记录 ：%s' % ('✓' if v[1] else '✗'))
            if not all(v):
                log('  ⚠️ 仍不完整，可尝试：注销重登一次')

    log('④ 刷新访达 ...')
    refresh_icon_services()
    # ★★★ 同 install_icns：上面 bump_version() 动过 Info.plist，必须重签。
    resign('改过 Info.plist，重签以保住签名有效性')
    log('✓ 已修复。若仍显示旧图标，请注销重登一次（登录时系统会重建图标库）。')
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--deg', type=float, default=None,
                    help='目标色相角度：40 暖橙黄 / 45 金黄 / 52 正黄（默认）')
    ap.add_argument('deg_pos', nargs='?', type=float, default=None,
                    help='同上，也支持直接写数字，方便菜单里「11 45」这么用')
    ap.add_argument('--no-install', action='store_true')
    ap.add_argument('--repair', action='store_true',
                    help='图标不显示时用：只重新注册 app + 刷新访达')
    args = ap.parse_args()
    if args.repair:
        return repair()
    deg = args.deg if args.deg is not None else args.deg_pos
    if deg is None:
        deg = 52.0
    if not ensure_pillow():
        return 1
    return build(deg, install=not args.no_install)


if __name__ == '__main__':
    sys.exit(main())
