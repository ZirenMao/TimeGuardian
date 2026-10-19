#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 TimeGuardian-Mac 的原创图标：黑底 + 白色盾牌 + 盾牌里的表盘
（时针 10 点、分针 2 点；12/3/6/9 四个粗刻度；中心圆点）。

用系统自带的 pyobjc/Quartz 画，零第三方依赖。产出 1024×1024 PNG。
用法：python3 _gen_tg_icon.py <输出.png>
"""
import sys
import math
import Quartz as Q
from Quartz import (kCGImageAlphaPremultipliedLast,
                    CGImageDestinationCreateWithURL, CGImageDestinationAddImage,
                    CGImageDestinationFinalize)

W = H = 1024


def rgb(r, g, b, a=1.0):
    return Q.CGColorCreateGenericRGB(r, g, b, a)


def png_write(img, path):
    import CoreFoundation as CF
    url = CF.CFURLCreateFromFileSystemRepresentation(None, path.encode(), len(path.encode()), False)
    dest = CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    CGImageDestinationAddImage(dest, img, None)
    CGImageDestinationFinalize(dest)


def main(out):
    cs = Q.CGColorSpaceCreateDeviceRGB()
    ctx = Q.CGBitmapContextCreate(None, W, H, 8, 0, cs, kCGImageAlphaPremultipliedLast)
    Q.CGContextSetShouldAntialias(ctx, True)

    # ---- 背景：黑色圆角（macOS 图标网格留白内的圆角方块）----
    Q.CGContextSetRGBFillColor(ctx, 0.043, 0.043, 0.051, 1.0)  # ~#0B0B0D
    bg = Q.CGPathCreateWithRoundedRect(Q.CGRectMake(0, 0, W, H), 200, 200, None)
    Q.CGContextAddPath(ctx, bg)
    Q.CGContextFillPath(ctx)

    # ---- 白色「盾牌」描边（线条化，不填色）----
    shield = Q.CGPathCreateMutable()
    Q.CGPathMoveToPoint(shield, None, 246, 786)
    Q.CGPathAddLineToPoint(shield, None, 778, 786)
    Q.CGPathAddLineToPoint(shield, None, 778, 540)
    Q.CGPathAddCurveToPoint(shield, None, 778, 372, 660, 300, 512, 250)
    Q.CGPathAddCurveToPoint(shield, None, 364, 300, 246, 372, 246, 540)
    Q.CGPathCloseSubpath(shield)
    white = Q.CGColorCreateGenericRGB(1, 1, 1, 1)
    Q.CGContextSetStrokeColorWithColor(ctx, white)
    Q.CGContextSetLineWidth(ctx, 34)
    Q.CGContextSetLineJoin(ctx, Q.kCGLineJoinRound)
    Q.CGContextSetLineCap(ctx, Q.kCGLineCapRound)
    Q.CGContextAddPath(ctx, shield)
    Q.CGContextStrokePath(ctx)

    # ---- 表盘（白色线条）：圆环 + 12/3/6/9 刻度 + 10:10 指针 + 中心点 ----
    cx, cy, r = 512.0, 560.0, 118.0
    Q.CGContextSetStrokeColorWithColor(ctx, white)
    Q.CGContextSetLineWidth(ctx, 20)
    Q.CGContextStrokeEllipseInRect(ctx, Q.CGRectMake(cx - r, cy - r, 2 * r, 2 * r))

    Q.CGContextSetLineWidth(ctx, 22)
    tii, tio = r - 34, r - 6
    for ang in (0, 90, 180, 270):
        a = math.radians(ang)
        dx, dy = math.sin(a), math.cos(a)
        Q.CGContextMoveToPoint(ctx, cx + tii * dx, cy + tii * dy)
        Q.CGContextAddLineToPoint(ctx, cx + tio * dx, cy + tio * dy)
        Q.CGContextStrokePath(ctx)

    Q.CGContextSetLineCap(ctx, Q.kCGLineCapRound)
    Q.CGContextSetLineWidth(ctx, 22)
    a = math.radians(300)                  # 时针 → 10 点
    Q.CGContextMoveToPoint(ctx, cx, cy)
    Q.CGContextAddLineToPoint(ctx, cx + 60 * math.sin(a), cy + 60 * math.cos(a))
    Q.CGContextStrokePath(ctx)
    a = math.radians(60)                   # 分针 → 2 点
    Q.CGContextMoveToPoint(ctx, cx, cy)
    Q.CGContextAddLineToPoint(ctx, cx + 104 * math.sin(a), cy + 104 * math.cos(a))
    Q.CGContextStrokePath(ctx)

    Q.CGContextSetFillColorWithColor(ctx, white)
    Q.CGContextFillEllipseInRect(ctx, Q.CGRectMake(cx - 17, cy - 17, 34, 34))

    img = Q.CGBitmapContextCreateImage(ctx)
    png_write(img, out)
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "_assets/icon_1024_tg.png")
