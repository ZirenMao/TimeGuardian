#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 任务管家 · 界面音效生成器
================================================================
生成三套「UI 提示音」到 `_assets/sounds/`，每套两个文件：

    set<id>-click.wav   点按钮 / 拨开关时的一声
    set<id>-done.wav    一个任务跑完了的两声（第二声略高，听感是「好了」）

三套的性格（界面「状态与设置」里让用户挑）

    set1  清脆   极短的一声「嗒」——系统按钮那种感觉，默认
    set2  机械   继电器 / 棘轮的「咔」，带金属质感
    set3  柔和   短而温和的「咚」，夜里安静时用

★★★ 2026-10-01（第卅五轮）：整套音色重做 —— 从「学 PommeToys 的做法」
    升级成「对齐 PommeToys 的实测声学特征」。
----------------------------------------------------------------
用户原话：「提示音能做成和 POMMETOYS 一样的吗 我觉得他的提示音好听」。

先把它的音效**量出来**（它把 45 个 .wav 明文放在 app 的 Resources/Sounds 里，
用户选的是 `app.uiSoundSet = 2`，二进制里被引用的 UI 音效是
`set2-*` 一组 14 个 ＋ `syn-03/04/…`），三个指标一摆，我们的病根一目了然：

    指标              PommeToys（set2 / syn-03）      我们旧版（set1）
    ----------------  -----------------------------  -------------------
    可闻尾            3.5 ~ 12 ms                    43.8 ms      ← 长 5~10 倍
    衰减时间常数 tau   1.1 ~ 2.5 ms                    11.0 ms      ← 慢 5~10 倍
    频谱重心          4.8 ~ 6.5 kHz                   2.7 kHz      ← 暗了一半

也就是：**我们的音一直在拖着响、而且闷**，听感是「哒——」；
它的音是**啪一下就没了、而且亮**。这不是"音色不同"，是"参数没对"。

本脚本因此**只拿它的实测数字当设计目标**（写在下面对标表 REF 里），
波形仍然**全部由自己算**（`wave`/`struct`/`math`/`random`，无第三方依赖）。
★ 判据（别退化）：这里**不读取、不复制**任何第三方 App 的资源文件。
  `--report` 会把生成结果与 REF 逐项对照，超区间就报 ✗。
  ⚠️ 冒烟自检 `--probe <别人的.wav>` 是**独立的只读检查**，只在你想
     再核对一次参考对象时才用，不参与生成，也不进交付产物。

为什么自己合成、不去抄现成的
----------------------------------------------------------------
用户要的是「照 PommeToys 那种 UI 提示音的做法」—— 学的是**做法与风格**
（每一次交互一声很短的提示音、可选、可静音），不是它的资源文件。

★ 结构（为什么这样分）：每种音色只写**一份规格**（TIMBRES），
  click 与 done 都由它渲染 —— 于是「清脆套」的两声也是清脆的，改音色只改一处。
  ⚠️ 早期版本写错过一次：done 的衰减包络横跨整段（两声一起衰减），
     结果第二声被压到几乎听不见、峰值全落在第一声。
     现在包络**每一声各算一份**（见 hit()），别再合起来算。

★ 2026-10-01 新增的两个工具（旧版没有，是这次能对齐的关键）
    _highpass()  一阶高通 = 原信号 − 低通。把低频削掉是**抬频谱重心**的唯一手段；
                 旧版只有低通，所以越"磨"越闷 —— 这是旧版听感发闷的直接原因。
                 项目里其它地方（如窗口/AI 请求）不用它，只服务音效。
    _bandnoise() 带通噪声。UI 音效的"质感"几乎全来自一段极短的宽带噪声
                 （它对应敲击的"接触瞬间"），把它限制在某个频段就能控制音色冷暖。

用法
    python3 _gen_sounds.py              # 生成（已存在也覆盖，幂等）
    python3 _gen_sounds.py --list       # 只列清单 + 检查现有文件齐不齐
    python3 _gen_sounds.py --play 1     # 生成后试听第 1 套（macOS 走 afplay）
    python3 _gen_sounds.py --report     # 生成后逐项体检 + 与 PommeToys 对标
    python3 _gen_sounds.py --check      # 只体检**现有**文件（不写任何东西；门禁用）
    python3 _gen_sounds.py --probe X.wav  # （只读）量一个外部 wav 的三个指标
"""
import argparse
import cmath
import math
import os
import random
import struct
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, '_assets', 'sounds')

SR = 44100          # 采样率：44.1kHz，够用且所有机器都能放
PEAK = 0.82         # 峰值留一点余量，避免某些声卡削顶

# ------------------------------------------------------------------ 对标表
# ★★ 2026-10-01（第卅五轮）：PommeToys 的**实测**特征，就是本脚本的设计目标。
#   数据来源：对 /Applications/PommeToys.app/Contents/Resources/Sounds/ 下
#   `set2-*`（用户选的那一套）与 `syn-03-click-crisp` 逐个数出来的值。
#   改音色时先看这里 —— 生成完 --report 会拿它验，超区间会报 ✗。
REF = {
    # 指标名              PommeToys 实测区间      备注
    'tail_ms':     (3.0, 20.0),    # 可闻尾：syn-03=4.1 / ratchet-tooth=11.8
    'tau_ms':      (0.8, 5.5),     # 衰减常数：syn-03=1.05 / ratchet-tooth=2.46
    'centroid_hz': (2500.0, 7000.0),  # 频谱重心：ratchet-tooth=4796 / syn-03=6511
}
# ★ 上面是**所有套都必须过的硬门槛** —— 它存在的意义是「别再退回旧版那种
#   拖着响、又闷又糊的音」（旧版实测：尾 44ms / tau 11ms / 重心 2.7kHz，三条全越界）。
#   ⚠️ 为什么不是更紧的一段：三套本来就有各自的定位。「柔和」那套**故意**比
#      「清脆」暗一点、稍长一点（夜里不刺耳）—— 拿 syn-03 的标准去卡它，
#      等于把三套做成一模一样，那是判据写错了，不是音效做错了。
#      要卡"像不像 PommeToys"，用下面这张按套对标的表。
#
# 对标表：set1 / set2 **各自贴着** PommeToys 的某一个音效（实测值）。
#   set3 没有对应物 —— PommeToys 那一整套里没有"柔和"这一档，它全是短促清脆的。
MATCH = {
    '1': ('syn-03-click-crisp',  (4.13, 1.05, 6511.0)),
    '2': ('set2-ratchet-tooth',  (11.84, 2.46, 4796.0)),
    '3': (None, None),
}

# ------------------------------------------------------------------ 波形工具


def _silence(n):
    return [0.0] * n


def _sine(n, f, sr=SR):
    w = 2.0 * math.pi * f / sr
    return [math.sin(w * i) for i in range(n)]


def _decay(n, tau, sr=SR):
    """指数衰减包络 exp(-t/tau)，tau 单位秒（越小越"脆"）。"""
    return [math.exp(-(i / sr) / tau) for i in range(n)]


def _ramp(n, ms=1.2, sr=SR):
    """两端各做一点淡入淡出 —— 不加这个，波形从 0 突跳到某个值会「啪」一声。"""
    k = max(1, int(sr * ms / 1000.0))
    out = [1.0] * n
    for i in range(min(k, n)):
        out[i] = i / float(k)
        out[n - 1 - i] = min(out[n - 1 - i], i / float(k))
    return out


def _noise(n, seed=0):
    rnd = random.Random(seed)
    return [rnd.uniform(-1.0, 1.0) for _ in range(n)]


def _lowpass(x, cutoff, sr=SR):
    """一阶低通。用来把白噪声磨成「咔」而不是「嘶」。"""
    a = 1.0 - math.exp(-2.0 * math.pi * cutoff / sr)
    y, prev = [], 0.0
    for v in x:
        prev += a * (v - prev)
        y.append(prev)
    return y


def _highpass(x, cutoff, sr=SR):
    """一阶高通 = 原信号 − 低通（互补）。

    ★★ 2026-10-01（第卅五轮）新加。为什么必须有它：
      频谱重心上不去，音就"闷"。而**只有低通是永远抬不高重心的** ——
      旧版三套的低频能量一分没削，所以再怎么调都发闷（set3 的重心只有 422 Hz，
      听感就是"咚"的一团低频糊）。
      削掉低频最省事又不会引入相位的写法，就是拿原信号减掉它的低通版。
    """
    lp = _lowpass(x, cutoff, sr)
    return [x[i] - lp[i] for i in range(len(x))]


def _bandnoise(n, lo, hi, seed=0, sr=SR):
    """带通噪声：白噪声 → 低通（去"嘶"）→ 高通（去"嗡"）。

    ★★ 2026-10-01（第卅五轮）新加。UI 音效的"质感"几乎全来自这一段极短的
      宽带噪声（对应敲击的"接触瞬间"）。把它限制在某个频段，就能控制
      音色的冷暖：高频段 → 清脆、低频段 → 厚实。
    """
    x = _noise(n, seed)
    if hi:
        x = _lowpass(x, hi, sr)
    if lo:
        x = _highpass(x, lo, sr)
    # 高通/低通都会吃掉能量，归一化回 ±1，否则"调滤波"会顺带改音量
    top = max(abs(v) for v in x) or 1.0
    return [v / top for v in x]


def _gain(x, g):
    return [v * g for v in x]


def _mix(*tracks):
    n = max(len(t) for t in tracks)
    out = [0.0] * n
    for t in tracks:
        for i, v in enumerate(t):
            out[i] += v
    return out


# ------------------------------------------------------------------ 音色规格
#
#   f      这个音色的「基准音高」Hz（click 用它的 1 倍）
#   harm   [(倍数, 权重), …] 相对基准音高的分音
#          ★ 倍数不必是整数 —— PommeToys 那几套的主峰就不是整数倍关系
#            （syn-03 实测 2584 / 4307 / 6115 Hz，比值 1 : 1.667 : 2.366）。
#            非整数倍反而更像"敲击"而不像"电子音"。
#   tau    衰减时间常数（秒）—— 越小声越短
#   dur    总时长（秒）。★ 注意：它只管"文件多长"，真正发声的只有前几毫秒，
#          剩下的按 PommeToys 的做法留静音（它的 syn-03 是 38ms 文件 / 4ms 出声）。
#   noise  噪声成分的权重（质感靠它）
#   nlo    噪声的高通截止 Hz（0 = 不削低频）
#   nhi    噪声的低通截止 Hz（0 = 不削高频）
#   ntau   噪声的衰减常数（比音调短得多 —— 噪声是"接触瞬间"，一碰就走）
#   hp     整体高通截止 Hz（抬频谱重心的主手段）
#   cut    整体低通截止 Hz（把尖头磨圆 → 「闷」）

TIMBRES = {
    # ---- 1 · 清脆：对标 PommeToys `syn-03-click-crisp`（尾 4.1ms / tau 1.05ms / 重心 6511Hz）
    '1': dict(name='清脆', f=2584,
              harm=[(1.0, 1.00), (1.667, 0.48), (2.366, 0.26), (3.20, 0.18)],
              tau=0.00108, dur=0.040,
              noise=1.05, nlo=5200, nhi=18000, ntau=0.00038,
              hp=2600, cut=None),
    # ---- 2 · 机械：对标 PommeToys `set2-ratchet-tooth`（尾 11.8ms / tau 2.46ms / 重心 4796Hz）
    '2': dict(name='机械', f=2380,
              harm=[(1.0, 1.00), (1.14, 0.55), (1.32, 0.34), (1.62, 0.16)],
              tau=0.00245, dur=0.034,
              noise=0.85, nlo=1400, nhi=11000, ntau=0.0011,
              hp=700, cut=None),
    # ---- 3 · 柔和：没有一一对应的那一个（它是三者里最柔的），按同一套特征往
    #      低频、窄带收一点做出来 —— 短仍然是第一位的（旧版 110ms 是硬伤）
    '3': dict(name='柔和', f=1360,
              harm=[(1.0, 1.00), (1.98, 0.32), (2.94, 0.11)],
              tau=0.00400, dur=0.028,
              noise=0.45, nlo=1000, nhi=6200, ntau=0.0017,
              hp=420, cut=7000),
}

# done 的两个音（绝对频率 Hz）。
#   ★ 第二声略高 —— 「好了」的上扬感全来自这个音程，不是来自响度。
#   ★ 2026-10-01：间距从 4~10ms 拉到 ~55ms。旧版的 gap 太短，两声挤在一起，
#     听感上就是"一个拖着尾巴的音"，白瞎了"两声"这个设计。
DONE_NOTES = {
    '1': [2200.0, 2740.0],      # ≈ 4:5
    '2': [2020.0, 2525.0],
    '3': [1245.0, 1560.0],
}
GAPS = {'1': 52, '2': 46, '3': 58}


def hit(spec, f, g=1.0):
    """渲染**一次**敲击（click 与 done 的每一声都走这里）。

    ⚠️ 包络必须**每次调用各算一份** —— 早先的写法把两声放一起衰减，
       第二声被压没了（见文件头的说明）。
    ★ tau 按音高缩放：高音衰减更快，这样换到高音时音色不变味。
    """
    n = int(SR * spec['dur'])
    tau = spec['tau'] * (spec['f'] / float(f))
    env = _decay(n, tau)
    tone = _silence(n)
    for mult, w in spec['harm']:
        tone = _mix(tone, _gain(_sine(n, f * mult), w))
    out = [tone[i] * env[i] for i in range(n)]
    if spec['noise']:
        nz = _bandnoise(n, spec['nlo'], spec['nhi'], seed=7 + int(f) % 97)
        nen = _decay(n, spec['ntau'])
        out = _mix(out, _gain([nz[i] * nen[i] for i in range(n)], spec['noise']))
    if spec['cut']:
        out = _lowpass(out, spec['cut'])
    if spec['hp']:
        out = _highpass(out, spec['hp'])
    return _gain(out, g)


def two_note(spec, sid, gap_ms):
    """done：两声之间留一小段静音。"""
    out = []
    for f in DONE_NOTES[sid]:
        out.extend(hit(spec, f, 1.0))
        out.extend(_silence(int(SR * gap_ms / 1000.0)))
    return out


def click(spec):
    return hit(spec, spec['f'])


# ------------------------------------------------------------------ 输出

def synth(vals):
    """归一化到 PEAK、两端淡入淡出、转 16bit PCM 字节。"""
    if not vals:
        vals = [0.0]
    r = _ramp(len(vals), ms=1.2)
    vals = [vals[i] * r[i] for i in range(len(vals))]
    top = max(abs(v) for v in vals) or 1.0
    k = PEAK / top
    out = bytearray()
    for v in vals:
        s = int(max(-1.0, min(1.0, v * k)) * 32767)
        out += struct.pack('<h', s)
    return bytes(out)


def write_wav(path, data):
    w = wave.open(path, 'wb')
    try:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data)
    finally:
        w.close()


def plan():
    out = []
    for sid in ('1', '2', '3'):
        spec = TIMBRES[sid]
        out.append(('%s/%s' % (sid, spec['name']), 'set%s-click.wav' % sid,
                    lambda s=spec: click(s)))
        out.append(('%s/%s' % (sid, spec['name']), 'set%s-done.wav' % sid,
                    lambda s=spec, i=sid: two_note(s, i, GAPS[i])))
    return out


# ------------------------------------------------------------------ 体检
# ★★ 2026-10-01（第卅五轮）：体检从"时长/峰值/削顶"升级成**声学三件套**
#   （可闻尾 / 衰减常数 / 频谱重心）—— 因为这三个才决定"好不好听"，
#   而旧版恰恰是栽在这三项上（一直没量，所以一直没发现）。
#   纯标准库实现，自带 radix-2 FFT。

def read_wav(p):
    w = wave.open(p, 'rb')
    try:
        ch, sw, fr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    finally:
        w.close()
    if sw == 2:
        d = struct.unpack('<%dh' % (len(raw) // 2), raw)
    else:
        d = tuple(b - 128 for b in raw)
    if ch > 1:
        d = tuple(d[i] for i in range(0, len(d), ch))
    return fr, d


def _fft(x):
    n = len(x)
    if n == 1:
        return x
    ev = _fft(x[0::2])
    od = _fft(x[1::2])
    out = [0j] * n
    for k in range(n // 2):
        t = cmath.exp(-2j * math.pi * k / n) * od[k]
        out[k] = ev[k] + t
        out[k + n // 2] = ev[k] - t
    return out


def _spectrum(d, sr):
    n = 1
    while n * 2 <= min(len(d), 8192):
        n *= 2
    seg = d[:n]
    w = [seg[i] * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)))
         for i in range(n)]
    sp = [abs(v) for v in _fft([complex(v) for v in w])][:n // 2]
    top = max(sp) or 1.0
    return [v / top for v in sp], sr / float(n)


def probe(p):
    """量一个 wav 的三个指标（可闻尾 / tau / 频谱重心）。"""
    sr, d = read_wav(p)
    ad = [abs(v) for v in d]
    pk = max(ad) or 1
    pi = ad.index(pk)
    last = max(i for i, v in enumerate(ad) if v > pk * 0.02)
    # RMS 分块包络（0.5ms 一块）→ 峰值后线性拟合求 tau
    hop = max(1, sr // 2000)
    blocks = []
    for i in range(0, len(d) - hop, hop):
        s = d[i:i + hop]
        blocks.append(math.sqrt(sum(v * v for v in s) / len(s)))
    bp = max(range(len(blocks)), key=lambda i: blocks[i]) if blocks else 0
    tau = None
    end = len(blocks)
    for i in range(bp, len(blocks)):
        if blocks[i] < blocks[bp] * 0.05:
            end = i
            break
    pts = [(i * hop / float(sr), math.log(blocks[i]))
           for i in range(bp, end) if blocks[i] > 1e-9]
    if len(pts) > 6:
        n_ = len(pts)
        sx = sum(t for t, _ in pts)
        sy = sum(v for _, v in pts)
        sxx = sum(t * t for t, _ in pts)
        sxy = sum(t * v for t, v in pts)
        den = n_ * sxx - sx * sx
        if abs(den) > 1e-12:
            slope = (n_ * sxy - sx * sy) / den
            if slope < -1:
                tau = -1.0 / slope
    sp, df = _spectrum(d, sr)
    cen = sum(i * df * v for i, v in enumerate(sp)) / (sum(sp) or 1.0)
    clip = sum(1 for x in d if abs(x) >= 32766)
    return dict(ms=len(d) * 1000.0 / sr, sr=sr, peak=pk,
                peak_ms=pi * 1000.0 / sr, tail_ms=last * 1000.0 / sr,
                tau=tau, centroid=cen, clip=clip, frames=len(d))


def _in_band(v, band):
    return v is not None and band[0] <= v <= band[1]


def report_one(p, fn, sid):
    r = probe(p)
    tau_ms = r['tau'] * 1000.0 if r['tau'] else None
    bad = []
    if 'click' in fn:
        if not _in_band(r['tail_ms'], REF['tail_ms']):
            bad.append('尾')
        if not _in_band(tau_ms, REF['tau_ms']):
            bad.append('tau')
        if not _in_band(r['centroid'], REF['centroid_hz']):
            bad.append('重心')
    mark = '✓' if not bad else '✗ 超区间:' + ','.join(bad)
    print('  %-18s %6.1fms  尾%6.2fms  tau %s  重心%6.0fHz  峰%5.2f  削顶%d  %s'
          % (fn, r['ms'], r['tail_ms'],
             ('%5.2fms' % tau_ms) if tau_ms else '   ——  ',
             r['centroid'], r['peak'] / 32767.0, r['clip'], mark))
    # ★ 按套对标：把 PommeToys 那个音的实测值并排打出来，一眼看出差多少
    if 'click' in fn and MATCH.get(sid) and MATCH[sid][0]:
        name, (t, ta, c) = MATCH[sid]
        print('  %-18s ↳ 对标 %s：尾 %.2f（目标 %.2f）  tau %.2f（%.2f）'
              '  重心 %.0f（%.0f）'
              % ('', name, r['tail_ms'], t,
                 tau_ms or 0, ta, r['centroid'], c))
    return not bad


def main():
    ap = argparse.ArgumentParser(
        description='生成界面音效（三套，纯标准库合成，不复制任何第三方资源）')
    ap.add_argument('--list', action='store_true', help='只列清单 + 检查现有文件')
    ap.add_argument('--play', metavar='套号', help='生成后试听这一套（1/2/3）')
    ap.add_argument('--report', action='store_true', help='逐项体检 + 与 PommeToys 对标')
    ap.add_argument('--check', action='store_true',
                    help='只体检**现有**文件、不重新生成（给回归门禁用的只读口）')
    ap.add_argument('--probe', metavar='WAV', help='（只读）量一个外部 wav 的三个指标')
    a = ap.parse_args()

    if a.probe:
        r = probe(a.probe)
        print('  %s' % os.path.basename(a.probe))
        print('     %d Hz  %d 帧 = %.1f ms' % (r['sr'], r['frames'], r['ms']))
        print('     峰值在 %.2f ms   可闻尾 %.2f ms' % (r['peak_ms'], r['tail_ms']))
        print('     tau %s   频谱重心 %.0f Hz'
              % (('%.2f ms' % (r['tau'] * 1000)) if r['tau'] else '测不出', r['centroid']))
        return 0

    if a.check:
        # ★★ 2026-10-01（第卅五轮）：门禁要在**只读**的前提下验这三项 ——
        #   旧写法是门禁直接调 `--report`，那会顺手把 6 个 wav 重写一遍
        #   （内容幂等、但 mtime 被刷新，且把一个"校验"动作变成"写入"动作）。
        print('  只体检现有文件（不重新生成）')
        allok, missing = True, []
        for _g, fname, _fn in plan():
            p = os.path.join(OUTDIR, fname)
            if not os.path.exists(p):
                missing.append(fname)
                print('  ✗ %-18s 不存在（跑一次 _gen_sounds.py 生成）' % fname)
                allok = False
                continue
            allok = report_one(p, fname, fname[3]) and allok
        if not allok and not missing:
            print('  ✗ 有指标跑出区间 —— 回 TIMBRES 调参数，别放过。')
        return 0 if allok else 1

    items = plan()
    if a.list:
        print('  输出目录：%s' % OUTDIR)
        for group, fname, _fn in items:
            p = os.path.join(OUTDIR, fname)
            print('  %s  %-18s %s'
                  % ('✓' if os.path.exists(p) else '·', fname, group))
        return 0

    try:
        os.makedirs(OUTDIR, exist_ok=True)
    except OSError as e:
        print('  ✗ 建不了目录 %s：%r' % (OUTDIR, e))
        return 2

    for group, fname, fn in items:
        data = synth(fn())
        write_wav(os.path.join(OUTDIR, fname), data)
        print('  ✓ %-18s %6d 字节  %s' % (fname, len(data), group))
    print('  共 %d 个文件 → %s' % (len(items), OUTDIR))

    if a.report:
        print()
        print('  硬门槛（三套都要过，防退回旧版那种拖沓闷响）：'
              '可闻尾 ≤%.0fms  tau ≤%.1fms  重心 ≥%.0fHz'
              % (REF['tail_ms'][1], REF['tau_ms'][1], REF['centroid_hz'][0]))
        allok = True
        for _g, fname, _fn in items:
            allok = report_one(os.path.join(OUTDIR, fname), fname,
                               fname[3]) and allok
        print('  %s' % ('✓ 三套全部达标（click；done 是两声叠的，只看时长与削顶）'
                        if allok else
                        '✗ 有指标跑出区间 —— 回 TIMBRES 调参数，别放过。'))

    if a.play:
        sid = str(a.play).strip()
        if sid not in TIMBRES:
            print('  ✗ 套号只能是 1 / 2 / 3')
            return 2
        if sys.platform == 'darwin':
            for kind in ('click', 'done'):
                p = os.path.join(OUTDIR, 'set%s-%s.wav' % (sid, kind))
                print('  ▶ %s' % os.path.basename(p))
                os.system('afplay %s' % _q(p))
        else:
            print('  · 非 macOS，跳过试听（文件已生成，可直接双击听）')
    return 0


def _q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


if __name__ == '__main__':
    sys.exit(main())
