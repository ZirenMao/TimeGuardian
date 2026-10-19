#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把右键工具做成 macOS **原生「快速操作」**（.workflow），从此不再依赖
「右键助手专业版 / MouseBoost Pro」。

背景
====
这几个功能（图片转PDF / 合并PDF / PDF加密解密 / 文档转PDF）原本挂在 MouseBoost Pro
的配置里（`~/Library/Group Containers/group.com.shrek.rightmouse/...plist`）。
那是**第三方软件的私有配置**：软件一卸载、或者它自己升级把配置重置，功能就没了。
用户要求：**卸载 MouseBoost 之后这些功能照旧能用**。

做法
====
改用系统自带的「快速操作」——往 `~/Library/Services/` 放 `.workflow`，
访达右键菜单的「快速操作」区域会自己列出来。**不需要任何后台进程**。

★ 架构（刻意沿用已验证的那一套，见 `work/pdf_quickactions/configure_mouseboost.py`）：
    快速操作里的 AppleScript **只负责把一个 .job 任务文件写进队列**；
    真正干活的仍是常驻守护 `com.wb.pdfquick`（pdf_daemon.py，非沙盒进程），
    它负责调引擎、弹密码框、发通知、把结果写到源文件同目录。
  这样换掉的是「UI 层」（MouseBoost → 系统快速操作），**处理层一行没动**，
  而且卸载 MouseBoost 完全不影响守护。

⚠️ 最后一步「注册」必须由用户在自己的终端做（工具沙箱里 pbs 会报 XPC 错误）：
       /System/Library/CoreServices/pbs -update; killall Finder
   或者注销重登一次（100% 可靠）。

用法
====
    python3 wb_quickactions.py --status      # 看装了哪些、注册没注册
    python3 wb_quickactions.py --install     # 生成 / 刷新全部快速操作
    python3 wb_quickactions.py --uninstall   # 全部移除
    python3 wb_quickactions.py --open-template   # 「word模板创建」点下去真正跑的那句
    python3 wb_quickactions.py --pick-template   # 弹原生选文件框，选 Word 模板并记下来
    python3 wb_quickactions.py --set-template P  # 直接指定模板路径（脚本/界面用）

★ 「word模板创建」用的是哪个模板（2026-10-01 第四十九轮）
======================================================
    默认 = **Word 自己的共用模板**（Normal.dotm），macOS 上的官方位置：
        ~/Library/Group Containers/UBF8T346G9.Office/
            User Content.localized/Templates.localized/Normal.dotm
    用户换过之后记在 `_右键助手.json` 的 `wordTemplate` 里。
    ⚠️ 快速操作**不写死这个路径** —— 生成的 AppleScript 每次点都回来问一次
       `--open-template`，所以「在界面上换个模板」立刻生效，**不必重装、也不必
       再跑一次 `pbs -update`**。这是刻意为之：写死的话，每换一次模板就要走一趟
       桥（重装 + 重新注册），而那一步本来就最容易出岔子。
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
SERVICES = os.path.join(HOME, "Library", "Services")
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
# 借一个系统/用户已有的 workflow 当**模板骨架**（Automator 的文档结构很啰嗦，
# 从零手搓容易漏键；照抄一份再改最稳 —— 这也是 skill 里验证过的做法）。
TEMPLATE = os.path.join(SERVICES, "未命名.workflow")
QUEUE = os.path.join(HOME, "Library", "Logs", "mouseboost_pdf", "queue")

# ── Word 模板（「word模板创建」用哪个） ────────────────────────────────────
#  ★ Word for Mac 的**用户模板目录**（Word 2016 起一直没变）。`User Content.localized`
#    与 `Templates.localized` 里的 `.localized` 是苹果的本地化后缀，中文系统上就是它，
#    别"顺手"改成 `User Content` —— 那个目录在某些版本里也存在，但不是 Word 读的那个。
WORD_TPL_DIR = os.path.join(
    HOME, "Library", "Group Containers", "UBF8T346G9.Office",
    "User Content.localized", "Templates.localized")
DEFAULT_TPL = os.path.join(WORD_TPL_DIR, "Normal.dotm")
TPL_FILE = os.path.join(TOOL_DIR, "_右键助手.json")   # 用户换过的模板记在这儿
TPL_EXTS = (".dotm", ".dotx", ".docx", ".doc")

#  ── 选模板框的「允许类型」──────────────────────────────────────────────
#  ★★ 2026-10-01（第五十二轮 · 用户：「为什么这个模板选不了特定文件格式 改正一下」）
#   原来写死的是 `of type {"dotm", "dotx"}` —— 那样写**一个文件都选不了**：
#   AppleScript 的 `of type` 收的是 **UTI 或四字符 OSType**，**不是扩展名**。
#   `"dotm"` 被当成四字符码（没有哪个文件带这个码）→ 面板里每一行都是灰的、
#   「选取」永远点不动。所以修法不是「换个写法」，而是**把扩展名翻成 UTI**。
#   ⚠️ 下面这些 UTI 是**实测**来的（`touch x.dotm` → `mdls -name kMDItemContentType`），
#     不是照记忆写的。系统里存的小写是 `…template.macroenabled`，规范写法是
#     `…template.macroEnabled`，两种都放进去 —— UTI 比较本身不分大小写，
#     **多列一个不存在的类型是无害的，少列一个会把用户挡在外面**。
TPL_UTIS = {
    ".dotm": "org.openxmlformats.wordprocessingml.template.macroEnabled",
    ".dotx": "org.openxmlformats.wordprocessingml.template",
    ".docx": "org.openxmlformats.wordprocessingml.document",
    ".doc":  "com.microsoft.word.doc",
}
#  微软自己声明的那一套（有的机器上 Word 会把 .dotm 认成 `com.microsoft.word.dotm`，
#  那就不走上面那条系统 UTI 了）。
TPL_UTI_ALIAS = (
    "org.openxmlformats.wordprocessingml.template.macroenabled",
    "com.microsoft.word.dotm", "com.microsoft.word.dotx",
    "com.microsoft.word.docx", "com.microsoft.word.doc",
)


def tpl_allowed_utis():
    """选文件框 `of type` 的列表 —— 与 `TPL_EXTS` **同一份事实**，别各写各的。

    ⚠️ 以后加 / 删扩展名**只改 `TPL_EXTS`**，这里自动跟着走。
      反过来（只改这里）不算数：`tpl_save()` 才是最后那道闸（选完还会再校验一次）。
    """
    out = []
    for e in TPL_EXTS:
        u = TPL_UTIS.get(e)
        if u and u not in out:
            out.append(u)
    for u in TPL_UTI_ALIAS:
        if u not in out:
            out.append(u)
    return out

#  ── 任务清单 ───────────────────────────────────────────────────────────
#  action   ：守护进程认的动作名（见 pdf_daemon.py 的任务文件格式）
#  utis     ：NSSendFileTypes —— 只有选中这些类型的文件，该项才出现在右键菜单里
#            （这就是系统级的「类型过滤」，和 MouseBoost 的 formats 字段同一个作用）
#  ⚠️ 图标：这里用的是系统通用的操作图标（NSActionTemplate / 见 icon 字段）。
#     用户 2026-10-01 明确要求：**不要像 MouseBoost 那样一律挂 Word 文档图标**，
#     要按功能各配各的。所以下面每项单独指定一个 SF Symbol 名（icon 字段），
#     由 wb_qa_icon.py 渲染成 png 塞进 workflow 的 QuickLook/Thumbnail.png，
#     访达右键菜单里显示的就是它。
TASKS = [
    {
        "name": "图片转PDF",
        "action": "IMG2PDF",
        # desc ＝界面上给用户看的那句人话（不是给开发看的）。
        "desc": "选中的图片合成一份 PDF，生成在原文件夹里。",
        "when": "选中一张或多张图片时出现",
        "icon": "photo.on.rectangle.angled",
        "utis": ["public.image"],
        "bid": "com.wb.qa.img2pdf",
    },
    {
        "name": "合并PDF",
        "action": "MERGE",
        "desc": "选中的多个 PDF 合成一份（按文件名的顺序拼）。",
        "when": "选中两个及以上 PDF 时出现",
        "icon": "doc.on.doc",
        "utis": ["public.pdf"],
        "bid": "com.wb.qa.mergepdf",
    },
    {
        "name": "PDF加密解密",
        "action": "CRYPT",
        "desc": "给 PDF 加密码，或去掉已知的密码。",
        "when": "选中 PDF 时出现",
        "icon": "lock.doc",
        "utis": ["public.pdf"],
        "bid": "com.wb.qa.cryptpdf",
    },
    {
        "name": "文档转PDF",
        "action": "DOC2PDF",
        "desc": "Word / Excel / PPT / txt 等转成 PDF。",
        "when": "选中 Office 文档或纯文本时出现",
        "icon": "arrow.right.doc.on.clipboard",
        "utis": [
            "com.microsoft.word.doc",
            "org.openxmlformats.wordprocessingml.document",
            "org.openxmlformats.wordprocessingml.document.macroEnabled",
            "public.rtf",
            "public.plain-text",
            "org.oasis-open.opendocument.text",
            "com.microsoft.excel.xls",
            "org.openxmlformats.spreadsheetml.sheet",
            "org.openxmlformats.spreadsheetml.sheet.macroEnabled",
            "public.comma-separated-values-text",
            "org.oasis-open.opendocument.spreadsheet",
            "com.microsoft.powerpoint.ppt",
            "org.openxmlformats.presentationml.presentation",
            "com.microsoft.powerpoint.pps",
            "org.openxmlformats.presentationml.slideshow",
            "org.oasis-open.opendocument.presentation",
        ],
        "bid": "com.wb.qa.doc2pdf",
    },
    {
        # ★★ 2026-10-01（第四十九轮）：这一项原来一直是「待补」—— 脚本正文存在
        #   右键助手专业版的私有配置里读不到。用户把截图发来之后才拿到正文：
        #       #!/bin/zsh
        #       open "…/Templates.localized/公文模板.dotm"
        #   ⇒ 它**不经过守护**（那四个才是「写 .job → com.wb.pdfquick 干活」），
        #     它只是「用 Word 打开模板 → Word 给你一份新文稿」，所以 `kind='open'`
        #     走另一条分支，不写队列。
        "name": "word模板创建",
        "action": "WORDNEW",
        # 用 kind 区分「怎么跑」，action 仍是队列动作名（这一项没有）。
        "kind": "open",
        "desc": "用 Word 打开你指定的模板，等于新建一份文稿；模板可以在右边自己挑。",
        "when": "在访达里任何地方都能用（包括桌面空白处）",
        "icon": "doc.badge.plus",
        # ★★ 2026-10-01 第六十二轮 c（用户报障：「在桌面上点右键也没有创建模板word」）：
        #   这里原来是 ["public.item"]，注释里写的理由是「访达在空白处右键会把当前
        #   文件夹当输入传进来」——**这个假设是错的**，实测下来桌面空白处右键时
        #   访达**不传任何输入**，于是 `NSSendFileTypes` 一卡，这一项就不显示了。
        #   ⇒ 改成 None = **Info.plist 里干脆不写 `NSSendFileTypes`**。
        #     那个键的语义是「**选中的东西里含这些类型**才显示这一项」，
        #     不写它 = 「不挑输入，任何上下文都出现」（Automator 里对应
        #     「工作流程收到当前：没有输入」）。`NSRequiredContext` 仍限定只在访达里。
        "utis": None,
        "bid": "com.wb.qa.wordnew",
    },
]

#  ── 还没搬过来的那一项 ─────────────────────────────────────────────────
#  2026-10-01 第四十九轮：**已经空了** —— 「word模板创建」拿到脚本正文后搬进 TASKS。
#  这个机制留着：以后再遇到「脚本正文拿不到」的项，照这里挂上去，界面上会把它
#  显式列出来、标成「待补」，不让用户以为我们漏了。
PENDING = []


def log(m):
    print(m)


#  ── Word 模板：读 / 写 / 现状 ───────────────────────────────────────────
#  ★ 三件事都要「一份事实」：默认值、用户改过的值、界面上显示什么。
def tpl_get():
    """当前模板的绝对路径。用户选过就用选过的；否则＝Word 的共用模板。"""
    try:
        with open(TPL_FILE, encoding='utf-8') as f:
            d = json.load(f)
        p = str(d.get('wordTemplate') or '').strip()
    except Exception:
        p = ''
    return os.path.expanduser(p) if p else DEFAULT_TPL


def tpl_save(path):
    """把模板路径记下来。返回 (ok, 路径或原因)。"""
    p = os.path.abspath(os.path.expanduser(str(path or '').strip()))
    if not p:
        return False, '没给路径'
    if not os.path.isfile(p):
        return False, '找不到这个文件：%s' % p
    if not p.lower().endswith(TPL_EXTS):
        return False, '要选 Word 模板或文稿（%s）：%s' % ('/'.join(TPL_EXTS),
                                                          os.path.basename(p))
    try:
        tmp = TPL_FILE + '.tmp.%d' % os.getpid()
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'wordTemplate': p}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TPL_FILE)
    except OSError:
        # 沙箱里 os.replace 偶尔被拦 —— 直接原地写一份，别让整条命令崩掉。
        with open(TPL_FILE, 'w', encoding='utf-8') as f:
            json.dump({'wordTemplate': p}, f, ensure_ascii=False, indent=2)
    return True, p


def tpl_info():
    """界面要显示的那一份（名字 / 是否默认 / 还在不在）。前端不自己拼。"""
    p = tpl_get()
    is_def = os.path.normpath(p) == os.path.normpath(DEFAULT_TPL)
    base = os.path.basename(p) or p
    return {
        'path': p,
        'name': base,
        # 「共用模板」是 Word 对 Normal.dotm 的叫法 —— 只有在用的就是那一份时才这么标。
        'label': ('%s（共用模板）' % base) if is_def else base,
        'isDefault': bool(is_def),
        'exists': os.path.isfile(p),
        'dir': os.path.dirname(p),
        # 给「恢复默认」用；前端不许自己拼这条路径。
        'defaultPath': DEFAULT_TPL,
    }


def interp():
    """跑自己的解释器：优先用 `_python_path.txt` 里记的那个（带 venv 的那份）。"""
    p = os.path.join(TOOL_DIR, '_python_path.txt')
    try:
        with open(p, encoding='utf-8') as f:
            t = f.read().strip()
        if t and os.path.isfile(t) and os.access(t, os.X_OK):
            return t
    except Exception:
        pass
    return '/usr/bin/python3'


#  ── AppleScript 源码 ──────────────────────────────────────────────────
#  只做三件事：拼路径串 → 取 uuid → printf 写 .job。
#  ⚠️ 路径串的形态必须与 MouseBoost 的 `@rfpaths` 一致：
#     每个路径用单引号包起来、空格分隔（守护进程会先剥引号再解析）。
def applescript(action, title, kind='job'):
    """生成快速操作里那段 AppleScript。

    kind='open'（word模板创建）走**另一条路**：不写队列、不惊动守护，
    只让 Word 拿模板开一份新文稿 —— 也就是用户原来那条 `open "…模板"` 的等价物。
    ★★ 这里**故意不把模板路径写死**，而是每次点都回来问一次 `--open-template`：
       写死的话，用户每换一次模板就得「重装 + 走桥重跑 pbs」，而那一步最容易出岔子。
       现问一次的成本是几十毫秒，换来「界面上一换就生效」。
    """
    if kind == 'open':
        # 单引号是 shell 的强引用 —— 路径里有空格 / 中文都不会散架。
        # ⚠️ 解释器前面加一层「在不在」的判断：万一哪天那个 venv 被搬走或重装，
        #   这一项不至于直接变哑巴，还能退到系统自带的 /usr/bin/python3（一定有）。
        #   （只判存在、不用 `||` 串两条命令 —— 后者会在"模板不在"这种正常失败时
        #     把命令**再跑一遍**，等于弹两次 Word。）
        shell = ("P='%s'; [ -x \"$P\" ] || P=/usr/bin/python3; "
                 "\"$P\" '%s' --open-template"
                 % (interp(), os.path.join(TOOL_DIR, 'wb_quickactions.py')))
        # 这段 shell 要塞进 AppleScript 的**双引号字符串**里 → 内层的 " 与 \ 要先转义。
        aslit = shell.replace('\\', '\\\\').replace('"', '\\"')
        return ('on run {input, parameters}\n'
                '\tdo shell script "%s"\n'
                '\treturn input\n'
                'end run' % aslit)
    return (
        'on run {input, parameters}\n'
        '\tset q to (POSIX path of (path to home folder)) & '
        '"Library/Logs/mouseboost_pdf/queue"\n'
        '\tset paths to ""\n'
        '\trepeat with f in input\n'
        '\t\tset paths to paths & " " & quoted form of (POSIX path of f)\n'
        '\tend repeat\n'
        '\tif paths is "" then return input\n'
        '\tset u to do shell script "uuidgen"\n'
        '\tset body to "%s" & linefeed & "%s" & linefeed & paths & linefeed\n'
        '\tdo shell script "mkdir -p " & quoted form of q & " && printf %%s " '
        '& quoted form of body & " > " & quoted form of (q & "/" & u & ".job")\n'
        '\treturn input\n'
        'end run' % (action, title)
    )


def make_info(t):
    """生成一份**完整**的 Info.plist。

    ⚠️ 最大的坑：Automator 自带的模板里 Info.plist **只有 NSServices 一个键**、
       没有任何 bundle 元数据 —— 照它改完，系统注册器（pbs）会拒绝注册，
       右键菜单里**永远不出现**，而且不报任何错。所以这里整份重写。
    """
    # ★★ 2026-10-01 第六十二轮 c：`utis` 为空/None ⇒ **`NSSendFileTypes` 这一条键整个不写**。
    #   语义差别很大，别当成"空列表"：
    #     · 写了 `NSSendFileTypes` ⇒ 「**选中的东西**里含这些类型时，这一项才出现」
    #       —— 换句话说就是"必须先在访达里选中点什么"；
    #     · 不写 ⇒ 「不挑输入，任何上下文都出现」（含**桌面空白处右键**）。
    #   Automator 界面上分别对应「工作流程收到当前：文件或文件夹」／「没有输入」。
    svc = {
        "NSMenuItem": {"default": t["name"]},
        "NSMessage": "runWorkflowAsService",
        # 限定只在**访达**里出现（不在 Safari、文本编辑等别的地方冒出来）
        "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
        "NSBackgroundColorName": "background",
        "NSIconName": "NSActionTemplate",
    }
    if t.get("utis"):
        svc["NSSendFileTypes"] = t["utis"]
    return {
        "CFBundleIdentifier": t["bid"],
        "CFBundleName": t["name"],
        "CFBundleDisplayName": t["name"],
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundlePackageType": "BNDL",
        "NSServices": [svc],
    }


def make_wflow(t):
    if not os.path.isfile(os.path.join(TEMPLATE, "Contents", "document.wflow")):
        raise SystemExit("找不到模板 %s —— 请先在「自动操作」里存一个空的快速操作"
                         "（名字「未命名」），或改本文件的 TEMPLATE 常量。" % TEMPLATE)
    with open(os.path.join(TEMPLATE, "Contents", "document.wflow"), "rb") as f:
        d = plistlib.load(f)

    src = applescript(t["action"], t["name"], t.get("kind", "job"))
    md = d.setdefault("workflowMetaData", {})
    md.update({
        # ★ 必须是 quickAction —— 若是模板带出来的 servicesMenu，
        #   该项会躲进「服务」子菜单、或者干脆不显示。
        "workflowTypeIdentifier": "com.apple.Automator.quickAction",
        "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
        "inputTypeIdentifier": "com.apple.Automator.fileSystemObject",
        "outputTypeIdentifier": "com.apple.Automator.nothing",
        "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
        "processesInput": True,
        "serviceProcessesInput": True,
        "useAutomaticInputType": False,
        "presentationMode": 11,
        "systemImageName": t["icon"],
    })
    acts = d.get("actions") or []
    if not acts:
        raise SystemExit("模板里没有 actions —— 模板文件不对")
    a = acts[0]["action"]
    a["ActionParameters"]["source"] = src
    a.setdefault("arguments", {}).setdefault("0", {})["default value"] = src
    a["ActionName"] = "运行AppleScript"
    return d


def install_one(t, verbose=True):
    dest = os.path.join(SERVICES, t["name"] + ".workflow")
    contents = os.path.join(dest, "Contents")
    # ⚠️ 先整份重建：旧目录里可能残留上一次的 nib/缩略图，混着会很难查。
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(contents, exist_ok=True)

    # ① Info.plist —— 写完立刻断言顶层是 dict
    ip = os.path.join(contents, "Info.plist")
    with open(ip, "wb") as f:
        plistlib.dump(make_info(t), f)
    with open(ip, "rb") as f:
        back = plistlib.load(f)
    if not isinstance(back, dict):
        # 这个坑曾经把 Info.plist 写成**顶层数组**（["public.image"]）：
        # pbs 解析失败 → 右键永远不显示，而 plutil -lint 却是"通过"的。
        raise SystemExit("Info.plist 写坏了（顶层是 %s，应为 dict）：%s"
                         % (type(back).__name__, ip))

    # ② document.wflow
    with open(os.path.join(contents, "document.wflow"), "wb") as f:
        plistlib.dump(make_wflow(t), f)

    # ③ 清隔离属性 + ad-hoc 自签（防系统当"未受信任"把它藏起来）
    subprocess.run(["xattr", "-cr", dest], check=False, capture_output=True)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", dest],
                   check=False, capture_output=True)
    if verbose:
        log("  ✓ %s" % t["name"])
    return dest


#  ── 状态采集 ───────────────────────────────────────────────────────────
#  一个函数管两头：`--status` 拿去打印给人看，`--json` 拿去给界面画。
#  ★★ 界面上每一项的「人话说明」必须从这里出（前端不许自己拼）——
#     这是本项目一贯的规矩：事实只留一份。
def daemon_state():
    """读 com.wb.pdfquick 守护在 launchd 里的实况（查不到回 None）。

    ⚠️ 与三条线那些 label 不同，这个 label 在工具进程（沙箱）里**读得到**，
       所以不必走桥。空字符串同样当「查不到」。
    """
    try:
        r = subprocess.run(
            ["launchctl", "print", "gui/%d/com.wb.pdfquick" % os.getuid()],
            capture_output=True, text=True, timeout=10)
        out = r.stdout or ""
        if not out.strip():
            return None
        d = {}
        for ln in out.splitlines():
            s = ln.strip()
            for pre, key in (("state = ", "state"), ("pid = ", "pid"),
                             ("runs = ", "runs"),
                             ("last exit code = ", "lastExit")):
                if s.startswith(pre):
                    d[key] = s[len(pre):]
        return d or None
    except Exception:
        return None


def status():
    """界面读的那份快照（每一项都带够画界面所需的信息）。"""
    items = []
    for t in TASKS:
        p = os.path.join(SERVICES, t["name"] + ".workflow")
        ok = os.path.isfile(os.path.join(p, "Contents", "Info.plist"))
        items.append({
            "name": t["name"], "action": t["action"], "desc": t["desc"],
            "when": t["when"], "icon": t["icon"],
            "on": bool(ok), "path": p, "pending": False,
            # ★ 「这一项有没有可挑的模板」由后端说 —— 前端不许按名字/action 写 if
            #   （本项目一贯的规矩：事实只留一份）。目前只有 word模板创建 是 True。
            "hasTpl": bool(t.get("kind") == "open"),
        })
    for t in PENDING:
        items.append({
            "name": t["name"], "action": "", "desc": t["desc"],
            "when": t["when"], "icon": "", "on": False, "path": "",
            "pending": True, "why": t["why"], "todo": t["todo"],
            "hasTpl": False,
        })
    return {
        "services": SERVICES, "template": os.path.isdir(TEMPLATE),
        "daemon": daemon_state(), "items": items,
        "on": sum(1 for x in items if x["on"]),
        "total": len(items),
        # ★ 当前用的 Word 模板（名字 / 完整路径 / 是不是默认那份 / 还在不在）。
        #   即使 word模板创建 这项还没装，也一并下发 —— 用户可能先挑好模板再装。
        "wordTpl": tpl_info(),
    }


def cmd_status():
    st = status()
    log("Services 目录：%s" % SERVICES)
    log("队列目录　　：%s" % QUEUE)
    log("模板　　　　：%s" % ("在" if st["template"] else "✗ 缺"))
    w = st["wordTpl"]
    log("Word 模板　 ：%s%s" % (w["path"], "" if w["exists"] else "  ← ✗ 这份不在"))
    log("")
    for it in st["items"]:
        if it["pending"]:
            log("  · %-12s （待补脚本：右键助手的私有配置里读不到）" % it["name"])
        else:
            log("  %s %-12s %s" % ("✓" if it["on"] else "·", it["name"],
                                   it["path"] if it["on"] else "（未安装）"))
    log("")
    log("守护进程（真正干活的那个）：")
    d = st["daemon"]
    if d:
        for k in ("state", "runs", "pid", "lastExit"):
            if k in d:
                log("  %s = %s" % (k, d[k]))
    else:
        log("  查不到（守护没在跑？）")


def cmd_json():
    sys.stdout.write(json.dumps(status(), ensure_ascii=False))


def cmd_install(names=None):
    """names 为空＝全量；否则只装名单里那几个。返回 {ok, bad}。"""
    todo = [t for t in TASKS if not names or t["name"] in names]
    if not todo:
        return {"ok": False, "bad": [], "error": "没有匹配的功能名：%s" % (names,)}
    log("安装 / 刷新 %d 个快速操作 → %s" % (len(todo), SERVICES))
    os.makedirs(SERVICES, exist_ok=True)
    os.makedirs(QUEUE, exist_ok=True)
    bad = []
    for t in todo:
        try:
            install_one(t)
        except SystemExit as e:
            bad.append(t["name"])
            log("  ✗ %s：%s" % (t["name"], e))
    if not bad:
        log("")
        log("★ 还差最后一步让它出现在右键菜单里 —— 界面上那颗「让右键菜单生效」会替你做"
            "（走任务管家外壳，那棵树不是沙箱）：")
        log("      /System/Library/CoreServices/pbs -update; killall Finder")
    return {"ok": not bad, "bad": bad}


def cmd_uninstall(names=None):
    n = 0
    for t in TASKS:
        if names and t["name"] not in names:
            continue
        p = os.path.join(SERVICES, t["name"] + ".workflow")
        if os.path.isdir(p):
            # ★ ignore_errors：沙箱里 rmtree 偶发被拦，别让整条命令崩掉 ——
            #   装没装由 status() 复查，不靠这里的返回值下结论。
            shutil.rmtree(p, ignore_errors=True)
            log("  ✗ 已移除 %s" % t["name"])
            n += 1
    log("共移除 %d 个。同样跑一次 pbs -update 让它立刻消失。" % n)
    return {"ok": True, "removed": n}


#  ── 模板三件事：用它开一份 / 挑一个 / 直接指定 ──────────────────────────
def cmd_open_template():
    """「word模板创建」被点下去时真正跑的 —— 与用户原来那句 `open "…模板"` 等价。

    ⚠️ 为什么不让 AppleScript 直接 open：那样模板路径就被**烤进 workflow** 了，
       用户换个模板就得重装 + 重跑 pbs。多起一次进程（几十毫秒）换「一换就生效」，
       很划算。
    ★ 用 `open` 而不是 `open -a "Microsoft Word"`：谁是 .dotm 的默认程序由系统定，
       硬写 Word 的话，用户哪天换了别的就用不了。
    """
    p = tpl_get()
    if not os.path.isfile(p):
        log("模板不在：%s" % p)
        log("（到任务管家 →「鼠标」→ 右键助手 → word模板创建 右边那颗按钮重新选一个）")
        return 1
    subprocess.run(['/usr/bin/open', p], check=False)
    log(p)
    return 0


def cmd_set_template(path=None):
    if not path:
        log("用法：--set-template /完整/路径.dotm")
        return 2
    ok, msg = tpl_save(path)
    log(("✓ 已改为 %s" % msg) if ok else ("✗ %s" % msg))
    return 0 if ok else 1


def cmd_pick_template():
    """弹**系统原生**的选文件框，把用户挑的模板记下来。

    实现用 osascript 的 `choose file`（Standard Additions 自带，不需要辅助功能权限、
    也不去控制别的 App）。⚠️ 它**会一直等**用户选完或按取消，所以超时给得宽（5 分钟）。
    返回码 / 输出都是给 wb_ui.py 解析用的：
        rc=0 → stdout 是选中的完整路径
        rc=3 → 用户取消（不是错误，界面别报红）
        rc=1 → 真出错（弹不出来、或选完存不下）
    """
    cur = tpl_get()
    d = os.path.dirname(cur)
    if not os.path.isdir(d):
        d = HOME
    # ★ 允许的类型＝`TPL_EXTS` 翻成的 UTI 列表（见文件头 TPL_UTIS 的说明）。
    #   ⚠️ 千万别退回 `of type {"dotm"}` —— 那是四字符 OSType 的写法，一个文件都选不了。
    utis = ", ".join('"%s"' % u for u in tpl_allowed_utis())
    script = (
        'try\n'
        '  set p to choose file with prompt "选一个 Word 模板或文稿（%s）" '
        'of type {%s} default location (POSIX file "%s" as alias)\n'
        'on error number -128\n'
        '  return "__CANCEL__"\n'
        'end try\n'
        'return POSIX path of p'
    ) % (" / ".join(TPL_EXTS), utis,
         d.replace('\\', '\\\\').replace('"', '\\"'))
    try:
        r = subprocess.run(['/usr/bin/osascript', '-e', script],
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        log("选文件框等太久（5 分钟）—— 当你没选。")
        return 3
    except Exception as e:
        log("弹不出选文件框：%r" % (e,))
        return 1
    out = (r.stdout or '').strip()
    if out == '__CANCEL__' or 'User canceled' in (r.stderr or ''):
        log("已取消")
        return 3
    if r.returncode != 0 or not out:
        log("选文件失败：%s" % ((r.stderr or '').strip()[:300] or 'rc=%d' % r.returncode))
        return 1
    ok, msg = tpl_save(out)
    if not ok:
        log(msg)
        return 1
    log(msg)
    return 0


def main():
    a = sys.argv[1:]

    def names_after(flag):
        """取 `--install` 之后那些**不以 - 开头**的参数（＝功能名）。"""
        i = a.index(flag)
        return [x for x in a[i + 1:] if not x.startswith('-')]

    if '--json' in a:
        cmd_json()
    elif '--open-template' in a:
        sys.exit(cmd_open_template())
    elif '--pick-template' in a:
        sys.exit(cmd_pick_template())
    elif '--set-template' in a:
        i = a.index('--set-template')
        nxt = [x for x in a[i + 1:] if x.strip()]
        sys.exit(cmd_set_template(nxt[0] if nxt else None))
    elif '--install' in a:
        cmd_install(names_after('--install') or None)
    elif '--uninstall' in a:
        cmd_uninstall(names_after('--uninstall') or None)
    else:
        cmd_status()


if __name__ == "__main__":
    main()
