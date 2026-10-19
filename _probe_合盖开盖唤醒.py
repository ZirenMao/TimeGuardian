# -*- coding: utf-8 -*-
"""判据脚本：**「合盖后保持唤醒」开着时，开盖必须主动把屏叫回来**。

用户 2026-10-12 报的恶性 bug：开着「防休眠 + 合盖后保持唤醒」、合盖一段时间再打开，
屏幕黑着、鼠标变成彩色转圈（前台全像卡死），只能强按电源。

根因（2026-10-12 查明）：档位三会 `pmset -a disablesleep 1`，于是**合盖不再是睡眠**，
而只是工具自己 `pmset displaysleepnow` 把内建屏按灭；系统因此不会走「唤醒显示」那套
（它压根不认为睡过）。原来「开盖」那一步只写了句日志、**没有任何一句去点灯**。

本脚本不碰真实屏幕：把 `display_off_now` / `_run` / `display_asleep` / `lid_closed`
全部换成假的，只验「该发熄屏时发了、该唤醒时唤醒了、标记该置该清」。

用法：`python3 _probe_合盖开盖唤醒.py`（退出码 0 = 全过）
"""
import importlib.util
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location(
    "wb_powerguard_probe", os.path.join(HERE, "wb_powerguard.py"))
pw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pw)

# ---- 把落盘目标改到临时文件：绝不碰守护在用的 _运行监测状态.json ----
TMP = tempfile.mkdtemp(prefix="tg-probe-")
pw.STATE_FILE = os.path.join(TMP, "_运行监测状态.json")
pw.CONF_FILE = os.path.join(TMP, "_运行监测.json")
with open(pw.CONF_FILE, "w", encoding="utf-8") as f:
    f.write("{}")

# ---- 假环境 ----
STATE = {"lid": False}          # 假盖子
CALLS = {"off": 0, "wake": 0}
pw.lid_closed = lambda: STATE["lid"]
pw.lid_read = lambda: 1          # 假「合盖不睡已设上」，免得去读真系统
pw.external_display = lambda: False
pw.display_off_now = lambda: (CALLS.__setitem__("off", CALLS["off"] + 1), (True, ""))[1]
pw.display_asleep = lambda: True if CALLS["off"] > CALLS["wake"] else False
pw.thermal_state = lambda ttl=60: 0      # 假温度：不过热
pw.display_keepers = lambda *a, **k: []  # 假「没人按着屏」


def _fake_run(argv, **kw):
    if "-u" in argv:            # caffeinate -u -t 1 = 唤醒屏
        CALLS["wake"] += 1
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


pw._run = _fake_run
LOGS = []
pw.log = lambda m, *a: LOGS.append(str(m))

ok = []

# ---------- ① 合盖 → 熄屏，并落下「屏是我们按灭的」标记 ----------
STATE["lid"] = True
ctx = {}
for _ in range(3):
    pw.lid_screen_guard(ctx)
st = pw.read_state()
print("① 合盖后：熄屏调用 %d 次 ｜ 标记 lidScreenForced=%s" % (CALLS["off"], st.get("lidScreenForced")))
ok.append(("合盖会下发「补偿熄屏」（照 PommeToys：只补、不反复压）", CALLS["off"] > 0))
ok.append(("合盖熄屏后落下标记（供自愈用）", st.get("lidScreenForced") is True))

# ---------- ② 开盖 → 必须主动唤醒（这就是那个恶性 bug） ----------
before_wake = CALLS["wake"]
STATE["lid"] = False
pw.lid_screen_guard(ctx)
print("② 开盖后：唤醒调用 +%d 次 ｜ 标记=%s" % (CALLS["wake"] - before_wake, pw.read_state().get("lidScreenForced")))
ok.append(("开盖会主动发用户活动把屏叫回来", CALLS["wake"] > before_wake))
ok.append(("唤醒成功后清掉标记", pw.read_state().get("lidScreenForced") is False))
ok.append(("日志里说清了在叫屏", any("正在把屏幕叫回来" in m for m in LOGS)))

# ---------- ③ 自愈：标记还在 + 盖子已打开 → 也要叫回来（被强杀/中途关功能那两条路） ----------
pw._mark_lid_screen(True)
before_wake = CALLS["wake"]
healed = pw.lid_screen_selfheal(pw.log)
print("③ 自愈（盖开着）：%s ｜ 唤醒 +%d ｜ 标记=%s"
      % (healed, CALLS["wake"] - before_wake, pw.read_state().get("lidScreenForced")))
ok.append(("标记还在 + 盖子已打开 → 自愈把屏叫回来", healed is True and CALLS["wake"] > before_wake))
ok.append(("自愈成功后清掉标记", pw.read_state().get("lidScreenForced") is False))

# ---------- ④ 盖子还合着时，自愈不许动（屏本来就该灭着） ----------
pw._mark_lid_screen(True)
STATE["lid"] = True
before_wake = CALLS["wake"]
healed2 = pw.lid_screen_selfheal(pw.log)
print("④ 自愈（盖还合着）：%s ｜ 唤醒 +%d ｜ 标记=%s"
      % (healed2, CALLS["wake"] - before_wake, pw.read_state().get("lidScreenForced")))
ok.append(("盖子还合着时自愈不动手（屏该灭着）",
           healed2 is False and CALLS["wake"] == before_wake))
ok.append(("此时标记留着（等开盖再来）", pw.read_state().get("lidScreenForced") is True))

print()
for name, good in ok:
    print(("  ✓ " if good else "  ✗ ") + name)
npass = sum(1 for _, g in ok if g)
print("汇总：%d/%d 通过" % (npass, len(ok)))
sys.exit(0 if npass == len(ok) else 1)
