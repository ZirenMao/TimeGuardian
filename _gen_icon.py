#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_gen_icon.py —— 图标几何 + 装进 app
================================================================================
「几何」指的是**内容在 1024 画布里占多大**：

    mac 几何（默认） → 内容 **820/1024 = 80.1%**，四周留白 102 px
                       ＝ Apple 的图标网格，**与 WorkBuddy 官方图标分毫不差**
    win 几何         → 内容 **1024/1024 = 100%**（满幅，四周不留白）
                       ＝ Windows 端那颗「WorkBuddy任务管家.ico」的几何

★★ 为什么默认是 mac（2026-10-01 第卅四轮 · 用户报「这里为什么图标显示的不一样」）
--------------------------------------------------------------------------------
    用户拉开 Dock 里的「应用程序」堆栈，两个 app 的图标并排：WorkBuddy 那颗四周
    有留白、显得小；任务管家那颗撑满画布、又显得方又显得大。原因是 2026-09-27
    为「与 Windows 端一致」，把 Mac 端也改成了满幅。

    但 **Mac 和 Windows 的图标规矩本来就不同**：macOS 上 Apple 的图标一律是
    「1024 画布、内容 824 见方、留白 ~10%」（系统自带的 app 全这样），所以自建 app
    跟着满幅，在自己的 Dock 里就是**唯一的异类**。

    ⇒ 于是把两边**分开管**：Mac 端 app 装 mac 几何（与官方图标同轮廓，只有色相不同），
      满幅那颗继续留着给 Windows 端用（`--geom win` 一键切回）。
      ★ 换色母版 `_assets/icon_1024_yellow.png` 是**官方图重新着色**得来的，
        它的 alpha 与官方图标**逐像素差恒为 0**，所以 mac 几何这条路上不存在
        「重新绘形」的风险 —— 轮廓就是从官方图标原样继承的。

用法
--------------------------------------------------------------------------------
    python3 _gen_icon.py                 # mac 几何（默认）+ 装进 app
    python3 _gen_icon.py --check         # 只算与参照图的差异，不写任何产物
    python3 _gen_icon.py --geom win      # 切成满幅（与 Windows 端一致）并装进 app
    python3 _gen_icon.py --no-install    # 只生成，不装 app
    python3 _gen_icon.py --repair        # 图标没显示时用：只重新注册，不重生成
    python3 _gen_icon.py --src X.png     # 换一张主图源
    python3 _gen_icon.py --pad 102       # 显式指定留白（覆盖 --geom）

★ 换完图标若访达/Dock 还显示旧的：先跑 `--repair`；再不行注销重登一次
  （登录时系统会重建图标库，必好）。原因与排查见技能 `macos-port-win-script-tool`
  第八节「换了但不显示 = app 没登记进 LaunchServices」。
"""
import io
import os
import sys
import glob
import shutil
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, '_assets')
APP = '/Applications/WorkBuddy任务管家.app'

# 参照图（`--check` 用）：mac 几何对官方图标，win 几何对 Windows 那颗 ico
OFFICIAL_ICNS = '/Applications/WorkBuddy.app/Contents/Resources/icon.icns'
ICO_SRC = os.path.join(ASSETS, 'icon-source.ico')
WIN_ICO = os.path.join(HERE, 'WorkBuddy任务管家.ico')      # 老位置，留着兼容

# 主图源（make_icon.py 生成的那张 1024 母版）
MASTER_SRC = os.path.join(ASSETS, 'icon_1024_yellow.png')

# ---- 两种几何 ----------------------------------------------------------------
GEOM_TARGET = {'mac': 820, 'win': 1024}          # 内容边长（画布恒为 1024）
GEOM_ICNS = {'mac': 'AppIcon_mac.icns', 'win': 'AppIcon_win.icns'}
GEOM_MASTER = {'mac': 'icon_1024_mac.png', 'win': 'icon_1024_win.png'}
GEOM_DESC = {
    'mac': 'Apple 惯例 —— 与 WorkBuddy 官方图标同几何（内容 820/1024 = 80.1%）',
    'win': '满幅 —— 与 Windows 端 .ico 同几何（内容 1024/1024 = 100%）',
}

# iconset 需要的全部规格（文件名 → 像素）
SPEC = {
    'icon_16x16.png': 16,    'icon_16x16@2x.png': 32,
    'icon_32x32.png': 32,    'icon_32x32@2x.png': 64,
    'icon_128x128.png': 128, 'icon_128x128@2x.png': 256,
    'icon_256x256.png': 256, 'icon_256x256@2x.png': 512,
    'icon_512x512.png': 512, 'icon_512x512@2x.png': 1024,
}

PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


def log(msg):
    print(msg, flush=True)


def _pil():
    """确保能 import PIL；没有就换一个带 Pillow 的解释器重跑自己（复用 make_icon）。

    ★ 必须把**本文件**传给 ensure_pillow —— 否则它会用自己的路径重跑，
      变成「跑的是 make_icon.py，带的却是 _gen_icon 的参数」（见那边的注释）。
    """
    import make_icon
    if not make_icon.ensure_pillow(os.path.abspath(__file__)):
        raise SystemExit(1)
    from PIL import Image
    return Image


# --------------------------------------------------------------------------
# 读参照图：自己解 .icns，不起 iconutil、不落临时文件
# --------------------------------------------------------------------------
def _icns_largest_png(path):
    """从一个 .icns 里取出**最大那一档**的 PNG 字节。

    .icns 容器格式极简：'icns' + 总长 + 若干 (4 字节类型, 4 字节长度[含这 8 字节头], 数据)。
    现代 icns 的档位数据本身就是完整 PNG（ic07 / ic08 / … / ic14），直接交给 PIL 就行。
    ★ 走这条路是为了让 `--check` **不产生任何临时文件**、也不起子进程。
    """
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return None
    if data[:4] != b'icns':
        return None
    best = None                                  # (像素边长, 字节)
    off = 8
    while off + 8 <= len(data):
        ln = int.from_bytes(data[off + 4:off + 8], 'big')
        if ln < 8 or off + ln > len(data):
            break
        blob = data[off + 8:off + ln]
        off += ln
        if blob[:8] == PNG_MAGIC:
            w = int.from_bytes(blob[16:20], 'big')       # IHDR 的宽
            if best is None or w > best[0]:
                best = (w, blob)
    return best[1] if best else None


def _png_bytes(path):
    """从一个 .icns **或** .png / .ico 里取出 RGBA 主图；失败返回 None。"""
    from PIL import Image
    if path.lower().endswith('.icns'):
        blob = _icns_largest_png(path)
        if not blob:
            return None
        return Image.open(io.BytesIO(blob)).convert('RGBA')
    if not os.path.exists(path):
        return None
    im = Image.open(path)
    im.size = (256, 256)                     # .ico：挑 256 档
    im.load()
    return im.convert('RGBA')


def _source_master(Image, src=None):
    """找到主图源：显式 --src → icon_1024_yellow.png → _assets 下任意 1024 图。"""
    cands = []
    if src:
        cands.append(src)
    cands.append(MASTER_SRC)
    cands += sorted(glob.glob(os.path.join(ASSETS, 'icon_1024*.png')), reverse=True)
    for c in cands:
        if c and os.path.exists(c):
            log('· 主图源：%s' % c)
            return Image.open(c).convert('RGBA')
    log('✗ 找不到主图源。先用 make_icon.py 生成一张，或用 --src 指定。')
    log('  期望位置：%s' % MASTER_SRC)
    raise SystemExit(1)


def make_master(Image, src, target):
    """把主图做成「内容边长 = target、居中摆在 1024 画布上」的方图。

    ★ 关键：先按 alpha 裁掉源图**自带的**白边，再按目标尺寸摆回去 —— 这样
      「源图本身是满幅还是带留白」都不影响结果，输出永远落在目标几何上。
      （源图 `icon_1024_yellow.png` 本来就带 102 px 留白，裁掉再摆回 820，
        数字完全对上，等于原样透传。）
    """
    im = _source_master(Image, src)
    bb = im.getchannel('A').getbbox()
    if bb:
        im = im.crop(bb)
    t = max(1, min(1024, int(target)))
    if im.size != (t, t):
        im = im.resize((t, t), Image.LANCZOS)
    pad = (1024 - t) // 2
    canvas = Image.new('RGBA', (1024, 1024), (0, 0, 0, 0))
    # ★★ 必须用 alpha_composite，**不能**用 `canvas.paste(im, box, im)`：
    #   paste 的 mask 会连 alpha 通道一起混 —— 于是 alpha 被自己乘了一遍（＝平方）。
    #   半透明 128 的边会变成 64，圆角的抗锯齿被削薄、显得比别家图标「硬」。
    #   2026-10-01 抓出来的：mac 几何验收时 alpha 最大差恒为 64，一路查到这。
    canvas.alpha_composite(im, (pad, pad))
    log('· 主图 1024×1024（内容 %d，四周留白 %d px）' % (t, pad))
    return canvas


def _flat(im):
    """取像素序列（兼容 Pillow 14 的 get_flattened_data 改名）。"""
    if hasattr(im, 'get_flattened_data'):
        return list(im.get_flattened_data())
    return list(im.getdata())


def mad(a, b):
    """两张同尺寸 RGB 图的平均绝对差（只看颜色通道）。"""
    from PIL import ImageChops
    d = ImageChops.difference(a.convert('RGB'), b.convert('RGB'))
    px = _flat(d)
    return sum(sum(p) / 3.0 for p in px) / max(1, len(px))


def check(Image, master, geom):
    """与参照图比：几何一致性验收（不写任何产物）。

    ★ 两种几何的**判据不同**，别混用：
      · mac → 颜色本来就换过（黄 vs 绿），所以**只比轮廓**（alpha 逐像素）；
        期望最大差 = 0，因为母版是官方图重新着色来的，轮廓原样继承。
      · win → 与那颗 .ico 同源同形，颜色和轮廓都该对得上。
    """
    from PIL import ImageChops
    if geom == 'mac':
        ref, label = _png_bytes(OFFICIAL_ICNS), 'WorkBuddy 官方图标'
    else:
        p = WIN_ICO if os.path.exists(WIN_ICO) else ICO_SRC
        ref = _png_bytes(p) if os.path.exists(p) else None
        label = os.path.basename(p)
    if ref is None:
        log('· 找不到参照图（%s），跳过对照' % label)
        return None

    if ref.size == master.size:
        a, b = ref, master
    else:                                   # 尺寸不同就都缩到 256 再比
        a = ref.resize((256, 256), Image.LANCZOS)
        b = master.resize((256, 256), Image.LANCZOS)

    da = ImageChops.difference(a.getchannel('A'), b.getchannel('A'))
    amax, aavg = max(_flat(da)), sum(_flat(da)) / max(1, a.size[0] * a.size[1])

    def bb(im):
        x0, y0, x1, y1 = im.getchannel('A').getbbox()
        return '%d,%d→%d,%d' % (x0, y0, x1, y1)

    log('· 与「%s」对照（%d 档）：' % (label, a.size[0]))
    log('    轮廓(alpha)  最大差 %d   平均差 %.4f   ← 判据' % (amax, aavg))
    log('    %-12s %s' % ('参照图内容框', bb(a)))
    log('    %-12s %s' % ('本图内容框', bb(b)))

    if geom == 'mac':
        if amax == 0:
            log('  ✓ 轮廓与官方图标**分毫不差**（alpha 最大差 = 0）—— Dock 里并排看不出大小差')
        else:
            log('  ⚠️ 轮廓与官方图标不一致（最大差 %d）—— 请核对主图源' % amax)
        return amax, aavg

    color = mad(a, b)
    log('    颜色 MAD %.2f' % color)
    # ★ win 几何这边判**平均差**（不是最大差）：那颗 .ico 的圆角是硬边、
    #   我们的是抗锯齿边，角落那几个像素的最大差天然能到 ~190；均值才是有意义的量。
    #   （2026-09-27 记的实测值就是「颜色 MAD ≈ 5.5、轮廓均值 ≈ 0.5」。）
    if color < 10 and aavg < 3:
        log('  ✓ 一致（颜色 MAD < 10 且轮廓均值 < 3 —— 肉眼不可分）')
    else:
        log('  ⚠️ 差异偏大，请核对主图源是否与 Windows 端同一张画')
    return color, aavg


def build(Image, master, geom='mac', install=True):
    work = os.path.join(ASSETS, '_iconset_' + geom)
    dst = os.path.join(work, geom + '.iconset')
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst, exist_ok=True)
    log('② 缩放出 10 档 ...')
    for name, px in SPEC.items():
        master.resize((px, px), Image.LANCZOS).save(os.path.join(dst, name))

    log('③ iconutil 打包 .icns ...')
    icns = os.path.join(ASSETS, GEOM_ICNS[geom])
    r = subprocess.run(['iconutil', '-c', 'icns', dst, '-o', icns],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log('  ✗ 打包失败：%s' % (r.stderr or r.stdout))
        return 1
    master.save(os.path.join(ASSETS, GEOM_MASTER[geom]))
    master.resize((256, 256), Image.LANCZOS).save(
        os.path.join(ASSETS, '图标预览_%s几何_256.png' % geom))
    log('  ✓ %s（%.1f KB）' % (icns, os.path.getsize(icns) / 1024.0))
    # 用完即删（用户口径：中间产物不留在目录里；.icns 已经打完包，iconset 没用了）。
    # ⚠️ 删的是**外层 work 目录**，不是里面的 `mac.iconset` —— 只删里层会留个空壳目录。
    # 删不掉不算错（受限环境下 rmtree 会被拦）—— 下次开跑第一件事就是重建它。
    try:
        shutil.rmtree(work)
    except OSError:
        pass

    if not install:
        log('（--no-install，未装到 app）')
        return 0
    if not os.path.isdir(APP):
        log('· app 不存在，跳过安装：%s' % APP)
        return 0

    log('④ 装进 app 并让访达认到 ...')
    import make_icon
    return make_icon.install_icns(icns)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--geom', choices=('mac', 'win'), default='mac',
                    help='mac = Apple 惯例（默认，与 WorkBuddy 官方图标同几何）；'
                         'win = 满幅（与 Windows 端 .ico 同几何）')
    ap.add_argument('--check', action='store_true', help='只算与参照图的差异，不写任何产物')
    ap.add_argument('--pad', type=int, default=None,
                    help='显式指定留白像素（覆盖 --geom；102 ≈ mac 惯例，0 = 满幅）')
    ap.add_argument('--no-install', action='store_true')
    ap.add_argument('--repair', action='store_true', help='只重新注册 app + 刷新访达')
    ap.add_argument('--src', help='换一张主图源（png/ico）')
    args = ap.parse_args()

    if args.repair:
        import make_icon
        if not make_icon.ensure_pillow(os.path.abspath(__file__)):
            return 1
        return make_icon.repair()

    target = GEOM_TARGET[args.geom]
    if args.pad is not None:
        target = 1024 - 2 * max(0, int(args.pad))

    Image = _pil()
    log('① 取主图、按 %s 几何摆正 ...' % GEOM_DESC[args.geom])
    master = make_master(Image, args.src, target)
    check(Image, master, args.geom)
    if args.check:
        log('（--check，未写任何文件）')
        return 0
    return build(Image, master, geom=args.geom, install=not args.no_install)


if __name__ == '__main__':
    sys.exit(main())
