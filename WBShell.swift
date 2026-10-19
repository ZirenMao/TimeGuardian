// WBShell.swift —— WorkBuddy 任务管家 · macOS 窗口外壳
// ============================================================================
// 这是什么
//   双击 /Applications/WorkBuddy任务管家.app 时真正跑起来的那个程序。它做的事只有三件：
//     ① 保证后台小服务在跑（跑 `wb_ui.py --ensure`，服务本身是 wb_ui.py 起的）；
//     ② 开一个**没有地址栏**的窗口，把 http://127.0.0.1:<port>/ 显示出来；
//     ③ 打开前按 _界面.json 还原上次的窗口大小/位置，并在窗口被拖动/缩放后写回去。
//
// 窗口几何谁负责（2026-09-27 定的）
//   **由本外壳负责**（NSWindowDelegate 的 windowDidMove / windowDidResize）。
//   原先想让页面上报，但页面只能拿到 window.outerWidth / screenX，而这两个在
//   WKWebView 里**取不到值**（实测页面一次都没上报成功 → 「窗口大小记不住」）。
//   外壳手里就是真实 NSWindow，直接读写 `_界面.json` 最准。
//   页面那侧同时加了 `shell=1` 判断：在外壳窗口里不再上报，免得两套值互相盖。
//   数据格式与 Windows 端一致（winW/winH/winX/winY；y 一律"从主屏顶边往下"）。
//
// 为什么不用浏览器
//   Windows 端是用 Edge/Chrome 的 `--app=` 起「应用窗口」的。Mac 这台**没装**
//   Edge / Chrome（只有 Safari，而 Safari 不支持 --app=）。所以改用系统自带的
//   WebKit（WKWebView）做外壳：零安装、零第三方依赖，而且窗口图标直接取 app
//   bundle 里的 icns —— 与 Windows 端那张脸完全一致。
//
// 编译（见 _build_app.sh）
//   swiftc -O -o WorkBuddyShell WBShell.swift -framework Cocoa -framework WebKit
//
// 排查
//   日志：~/Library/Application Support/WorkBuddyTaskSync/_界面.log（服务侧）
//         ~/Library/Application Support/WorkBuddyTaskSync/_外壳.log（本程序侧）
// ============================================================================

import Cocoa
import WebKit
import ApplicationServices      // AXIsProcessTrustedWithOptions（「辅助功能」权限查询/申请）
import Carbon                   // TISCopyCurrentKeyboardInputSource（输入法判断，中英切换那几条要用）
import IOKit                    // 设备归属：IORegistryEntryIDMatching / IOServiceGetMatchingService

/// 工具目录（放 wb_*.py 的那个文件夹）。
/// ★ 2026-10-09（自包含便携版）：优先认「本 .app 所在的目录」——
///   把 TimeGuardian.app 放进工具目录里，整个目录拷到哪台 Mac 都能跑；
///   认不到（app 单放在 /Applications、工具目录在别处）才退回本机固定目录。
func resolveToolDir() -> String {
    if let e = ProcessInfo.processInfo.environment["WB_TOOL_DIR"], !e.isEmpty { return e }
    let fm = FileManager.default
    let parent = (Bundle.main.bundlePath as NSString).deletingLastPathComponent
    if fm.fileExists(atPath: parent + "/wb_ui.py") { return parent }
    let fixed = NSHomeDirectory() + "/Applications/TimeGuardian-Mac"
    if fm.fileExists(atPath: fixed + "/wb_ui.py") { return fixed }
    return parent
}
let TOOL_DIR = resolveToolDir()
let DEFAULT_PORT = 49632
let APP_TITLE = "TimeGuardian"

/// 主菜单里**我们自己建的**菜单项都打这个 tag —— 只为和 AppKit 自动补的那些
/// （听写 / 表情与符号）区分开，好让启动日志只列我们建的那套键位。
let MK_TAG = 9301

// ------------------------------------------------------------------ 日志

/// ★★ 自测通道下把日志**同时**打到 stdout（2026-09-29 加）。
///   为什么要这样：自测进程和**用户正在跑的那个实例**写的是**同一个** `_外壳.log`，
///   靠「记下偏移、事后读文件」无法区分谁写的 —— 真踩到过：测试跑到一半读进了
///   另一个实例的「菜单栏面板已收起」，于是把「面板明明弹出来了」判成
///   「面板没被标记成已弹出」。走 stdout 就**只可能**看到自己写的。
///   ⚠️ 只在 `WB_SHELL_PANELTEST` 下开：`WB_SHELL_KEYTEST` 的输出已被第 9 套
///      按行解析，多塞启动日志进去会污染它的判据。
let LOG_ECHO_STDOUT = (ProcessInfo.processInfo.environment["WB_SHELL_PANELTEST"] != nil)
/// 「现在是面板自测」——给**只在自测里需要**的诊断日志用（正常跑不许刷屏）。
let PANEL_SELFTEST = (ProcessInfo.processInfo.environment["WB_SHELL_PANELTEST"] != nil)

/// ★★★ 2026-10-02（第六十六轮 · 用户第 1 条）：「开机自动打开，但不要弹窗口」。
///   登录项 `~/Library/LaunchAgents/com.wb.timeguardian-mac.plist` 现在是
///   `/usr/bin/open -g <app> --args --silent` —— 带上这个参数＝**这次是登录/后台拉起**。
///   为什么必须在**外壳自己**这一侧分叉：`open -g` 只管「别主动把 app 提到前台」，
///   而真正把窗口端上来的是 `applicationDidFinishLaunching` 末尾那两句
///   `makeKeyAndOrderFront` + `activate` —— 光靠 plist 给多少 `-g` 都压不住它。
///   ⚠️ 手动双击 / 点 Dock 图标 / 面板里的「打开界面」都不带这个参数 ⇒ 照旧弹窗口。
///   ⚠️ 别改用手直接执行 app 内二进制的方式传参：那样进程不由 LaunchServices 拉起，
///      TCC（辅助功能授权按 app 身份认）与 Dock/菜单栏行为都可能变味 —— 已实测
///      `open --args` 在「app 尚未运行时」参数一定送达，登录场景正是这一种。
let SILENT_LAUNCH = CommandLine.arguments.contains("--silent")

func logLine(_ s: String) {
    let f = TOOL_DIR + "/_外壳.log"
    let fmt = DateFormatter()
    fmt.dateFormat = "yyyy-MM-dd HH:mm:ss"
    let line = "[\(fmt.string(from: Date()))] \(s)\n"
    if LOG_ECHO_STDOUT, let d = line.data(using: .utf8) {
        FileHandle.standardOutput.write(d)
    }
    if let h = FileHandle(forWritingAtPath: f) {
        h.seekToEndOfFile()
        if let d = line.data(using: .utf8) { h.write(d) }
        h.closeFile()
    } else {
        try? line.write(toFile: f, atomically: true, encoding: .utf8)
    }
}

// ------------------------------------------------------------------ 小工具

func readConf() -> [String: Any] {
    let p = TOOL_DIR + "/_界面.json"
    guard let d = FileManager.default.contents(atPath: p),
          let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else {
        return [:]
    }
    return o
}

func confInt(_ c: [String: Any], _ k: String) -> Int? {
    if let n = c[k] as? Int { return n }
    if let n = c[k] as? Double { return Int(n) }
    if let s = c[k] as? String { return Int(s) }
    return nil
}

/// 把 _界面.json 写回去（先读后合，保住 port 等别的键）。
func writeConf(_ c: [String: Any]) {
    let p = TOOL_DIR + "/_界面.json"
    guard let d = try? JSONSerialization.data(
        withJSONObject: c, options: [.prettyPrinted, .sortedKeys]) else { return }
    try? d.write(to: URL(fileURLWithPath: p))
}

/// ★ 第卅七轮（2026-10-01 用户第 3 条）：栏目由「滚动」改名「鼠标」后，
///   `_界面.json` 里旧名留下的键（`scrollPerm` / `scrollEngine`）会被 `writeConf`
///   的「先读后合」**永久带下去** —— 这里主动清掉，保持「文件、键名、功能」三处同名。
///   放在写盘链路上 ⇒ 从任何旧备份恢复后都会自动清一次（自愈，不靠一次性手工清理）。
///   返回 true 表示确实清掉了东西（调用方据此也触发一次写盘）。
let LEGACY_CONF_KEYS: [String] = ["scrollPerm", "scrollEngine"]
@discardableResult
func pruneLegacyConfKeys(_ c: inout [String: Any]) -> Bool {
    var removed: [String] = []
    for k in LEGACY_CONF_KEYS where c[k] != nil {
        c.removeValue(forKey: k)
        removed.append(k)
    }
    if !removed.isEmpty {
        logLine("[鼠标] 清掉改名前的旧键：\(removed.joined(separator: " / "))")
    }
    return !removed.isEmpty
}

/// 端口上有没有人在监听（= 后台服务在不在跑）。纯 BSD socket，不依赖 runloop。
func portAlive(_ port: Int) -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    if fd < 0 { return false }
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = in_port_t(UInt16(port).bigEndian)
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    var tv = timeval(tv_sec: 0, tv_usec: 300_000)
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    let r = withUnsafePointer(to: &addr) { p -> Int32 in
        p.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    return r == 0
}

/// 找一个「装了本工具包要用的第三方包」的解释器。
/// 与 wb_ui._mac_best_python 同一套优先级 —— 别只写 /usr/bin/python3，
/// 那个解释器里没有 pymupdf / Pillow，菜单里的子脚本会跑挂。
func pickPython() -> String {
    var cands: [String] = []
    if let e = ProcessInfo.processInfo.environment["WB_PY"], !e.isEmpty { cands.append(e) }
    cands.append(TOOL_DIR + "/_runtime/python/bin/python3")   // ★ 随包自带运行时优先
    cands.append(NSHomeDirectory() + "/.workbuddy/binaries/python/envs/default/bin/python")
    cands.append(NSHomeDirectory() + "/.workbuddy/binaries/python/envs/default/bin/python3")
    cands.append("/opt/homebrew/bin/python3")
    cands.append("/usr/local/bin/python3")
    cands.append("/usr/bin/python3")
    for c in cands where FileManager.default.isExecutableFile(atPath: c) {
        return c
    }
    return "/usr/bin/python3"
}

/// 保证后台服务在跑；返回 true = 可以开窗口了。
func ensureService(port: Int) -> Bool {
    if portAlive(port) {
        logLine("服务已在跑（端口 \(port)）")
        return true
    }
    let py = pickPython()
    let ui = TOOL_DIR + "/wb_ui.py"
    guard FileManager.default.fileExists(atPath: ui) else {
        logLine("✗ 找不到 \(ui)")
        return false
    }
    logLine("拉起后台服务：\(py) \(ui) --ensure")
    let p = Process()
    p.executableURL = URL(fileURLWithPath: py)
    p.arguments = [ui, "--ensure"]
    p.currentDirectoryURL = URL(fileURLWithPath: TOOL_DIR)
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    // ⚠️ 不设 start_new_session 也没关系：真正常驻的**服务进程**是 wb_ui.py 自己
    //    用 start_new_session=True 起的（脱离本进程）；这里这个 --ensure 只是
    //    一个几秒就退出的引导进程。
    do {
        try p.run()
    } catch {
        logLine("✗ 启动 --ensure 失败：\(error)")
        return false
    }
    p.waitUntilExit()
    logLine("--ensure 退出码 \(p.terminationStatus)")
    for _ in 0..<60 {                       // 最多再等 ~15 秒
        if portAlive(port) { return true }
        usleep(250_000)
    }
    return false
}

/// 菜单栏面板 → 外壳 的消息桥。
/// ★★ 只挂在**面板那个 WKWebView** 上（见 Shell.ensurePanel）：
///   面板里那几颗动作按钮（打开窗口 / 状态与设置 / 关闭服务 / 退出）要操作
///   NSWindow 与 NSStatusItem，是网页干不了的活，只能回调进外壳。
///   主窗口那个 webView **不挂** —— 于是页面里 `window.webkit.messageHandlers.shell`
///   在浏览器/主窗口里都是 undefined，shellCmd() 会走退路分支（提示一句），不会抛。
/// ⚠️ 用 `weak var shell`：WKUserContentController 会**强引用** handler，
///    写 strong 就变成「webView → handler → shell → panelWeb → webView」的环。
final class PanelBridge: NSObject, WKScriptMessageHandler {
    weak var shell: Shell?
    init(shell: Shell) { self.shell = shell }

    func userContentController(_ ucc: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard let shell = shell else { return }
        // 页面里两种写法都支持：`postMessage('closePanel')` 与
        // `postMessage({cmd:'height', h:667})`（后者带参数）。
        if let s = message.body as? String {
            shell.panelCommand(s)
        } else if let d = message.body as? [String: Any],
                  let c = d["cmd"] as? String {
            shell.panelCommand(c, payload: d)
        } else {
            logLine("面板消息无法解析：\(message.body)")
        }
    }
}

/// 停掉界面服务（= 页面上那颗「关闭服务」按钮，POST /api/quit）。/// ★ 为什么退出前要停：用户 2026-09-29 二次指定「⌘Q 直接退出，停用服务」——
///   不留后台常驻进程。下次双击 App 时 ensureService() 会把它重新拉起来。
/// ★ 该接口除了关服务，还会收拾「运行监测」**上次开机留下的一次性附加项**
///   （跑完自动关机 / 到点自动结束）。★★ 2026-10-01（第廿九轮）改了语义：
///   **不再归零「防休眠」等 5 个开关** —— 那 5 个是用户的长期姿势，关了服务
///   也该原样保持（用户原话：「除了重启以外……它上次是什么样还是什么样」）。
/// ⚠️ 端口**显式传进来**：本函数在文件作用域（类外面），拿不到 Shell.port。
func stopUiService(_ port: Int) {
    guard portAlive(port) else {
        logLine("界面服务本来就没在跑，跳过停服务")
        return
    }
    guard let url = URL(string: "http://127.0.0.1:\(port)/api/quit") else { return }
    var req = URLRequest(url: url)
    req.httpMethod = "POST"
    req.timeoutInterval = 3
    let sem = DispatchSemaphore(value: 0)
    var code = -1
    URLSession.shared.dataTask(with: req) { _, r, e in
        if let h = r as? HTTPURLResponse { code = h.statusCode }
        if let e = e { logLine("停界面服务的请求出错：\(e.localizedDescription)") }
        sem.signal()
    }.resume()
    if sem.wait(timeout: .now() + 4) == .timedOut {
        logLine("停界面服务的请求超时（不管它，照样退出）")
    }
    logLine("已请求停掉界面服务（HTTP \(code)）")
    // 服务那边是「先还原运行监测、再 shutdown」，端口不一定马上放开 —— 最多再等 5 秒。
    for _ in 0..<20 {
        if !portAlive(port) { break }
        usleep(250_000)
    }
    let gone = !portAlive(port)
    logLine(gone ? "界面服务已停（端口 \(port) 已放开）"
                 : "界面服务仍在收尾，交给它后台走完（端口 \(port)）")
}

// ====================================================================== 平滑滚动
//
// ★★ 2026-09-30（用户第 2 条）：首页新增的「鼠标」板块里那两项 —— 平滑滚动 / 反转方向 /
//   速度 —— 真正干活的地方就是这一段。照 PommeToys 的效果做（它那边是
//   `features.smoothScroll.enabled` / `features.mouse.reverse` / `speedSlider`）。
//
// 怎么做的（每条都是做过才知道的）：
//   ① 在本进程挂一个 **CGEventTap**（`.cgSessionEventTap` + `.headInsertEventTap`
//      + `.defaultTap`）拦滚轮事件。**必须 `.defaultTap`**：`.listenOnly` 只能看，
//      吞不掉原事件，那就会「原件 + 自己合成的」一起滚，变成两倍。
//   ② 拦到的事件**不给下游**（回调 return nil），改由自己的 120Hz 计时器一帧一帧地
//      合成**像素级**滚轮事件发出去 —— 「平滑」就是这么来的。
//   ③ 合成事件发到 `.cghidEventTap`（管道最低层），**必然又经过自己的 tap** ——
//      所以给它们盖上 `kCGEventSourceUserData = magic` 的戳，见到就原样放行，
//      否则自激成无限循环（一滚就飞）。
//   ④ 触控板（`kCGScrollWheelEventIsContinuous != 0`）**一律原样放行** ——
//      它本来就是平滑的，再加工一次只会打架（PommeToys 文案也写着「触控板不受影响」）。
//      带 ⌘/⌥/⌃ 的滚轮同样放行：那是缩放 / 横向 / 系统手势，不该被我们改速率。
//   ⑤ 要动别人的输入就得有「辅助功能」权限（TCC）。没权限时 `tapCreate` 直接返回
//      nil，**只能**请用户去系统设置里勾 —— 权限状态写回 `_界面.json` 的
//      `mousePerm`，服务端读它下发给界面，界面上才显示得出那句提示。
//
// ⚠️ 配置来源是 `_鼠标.json`（由界面的 /api/mouse 写），本外壳只**读**它，
//    并且用 DispatchSource 盯着它 —— 文件一改就重读、重挂引擎，**不做轮询**。
// ======================================================================

/// 平滑滚动的**纯计算**部分（完全与 CGEvent 无关）。
/// 为什么单拎出来：`WB_SHELL_MOUSETEST=1` 要能拿它跑一遍确定性的核对
/// （累计推进量 == 输入量、尾巴一定收敛、速度档确实改变手感），
/// 而真挂 CGEventTap 需要「辅助功能」权限 —— 自动化测不了，只能把可测的部分测干净。
enum SmoothMath {
    static let pxPerLine: Double = 22.0     // 一格（line）≈ 多少像素（1× 时）
    static let maxPending: Double = 1800.0  // 攒着的待滚量上限（猛甩一下别滚飞）
    static let maxSpeed: Double = 5200.0    // 每秒最多推进多少像素

    /// 一次原始滚轮 → 要推进的像素。像素级的用点增量，按格滚的按 pxPerLine 换算。
    static func impulse(pointDelta: Double, lineDelta: Double, speed: Double) -> Double {
        let base = (pointDelta != 0) ? pointDelta : lineDelta * pxPerLine
        return base * speed
    }

    /// 尾巴的时间常数：速度档越大收得越干脆（0.5× → 0.15s，1.5× → 0.06s，3× → 0.03s）。
    static func tailTau(speed: Double) -> Double {
        return 0.06 / max(0.4, min(3.0, speed) / 1.5)
    }

    /// 这一拍推进多少（纯函数，dt 秒）。
    /// `pending * (1 - e^{-dt/tau})` —— 保证累计量**恒等于**输入量，
    /// 且尾巴是几何收敛（不是线性跑完），所以看着像惯性。
    static func step(pending: Double, dt: Double, speed: Double) -> Double {
        if pending == 0 || dt <= 0 { return 0 }
        var s = pending * (1 - exp(-dt / tailTau(speed: speed)))
        let cap = maxSpeed * dt
        if abs(s) > cap { s = (s < 0) ? -cap : cap }
        if abs(pending) < 0.6 { s = pending }        // 尾巴太碎就直接收干净
        return s
    }
}

/// 滚轮事件 tap 的回调。**必须是顶层的 C 函数**（`CGEventTapCallBack` 是
/// `@convention(c)`，闭包捕获不了任何东西），所以状态都放在单例里。
private func smoothTapCB(proxy: CGEventTapProxy, type: CGEventType,
                         event: CGEvent,
                         refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    let eng = SmoothEngine.shared
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        // 系统会因为「回调太慢」或「用户输入打断了」把 tap 停掉 —— 必须自己挂回来，
        // 否则表现是「用一会儿之后平滑突然没了」。
        logLine("[鼠标] 事件 tap 被系统暂停（\(type.rawValue)），重新挂上")
        eng.reenable()
        return Unmanaged.passUnretained(event)
    }
    if type != .scrollWheel { return Unmanaged.passUnretained(event) }
    return eng.handle(event)
}

final class SmoothEngine {
    static let shared = SmoothEngine()

    /// 自己合成的事件都盖这个戳（'WBSC'），用来认出「这是我发的」。
    private let magic: Int64 = 0x5742_5343
    private var tap: CFMachPort?
    private var runLoopSrc: CFRunLoopSource?

    /* ⚠️ `pending` / `carry` / `timer` **只在 q 上碰**；`speed` / `reverse` 由主线程
       在 apply() 里写、在 q 上读 —— 一个对齐的 Double/Bool 读写不会有撕裂，
       这里刻意不加锁（加了反而容易在热路径上引入优先级反转）。 */
    private let q = DispatchQueue(label: "com.wb.wb.mouse", qos: .userInteractive)
    private var timer: DispatchSourceTimer?
    private var pending: Double = 0
    private var carry: Double = 0
    private var lastTick: CFAbsoluteTime = 0

    private(set) var running = false
    private(set) var speed: Double = 1.5
    private(set) var reverse = false

    // ---------------------------------------------------------------- 权限
    static func permitted(prompt: Bool) -> Bool {
        let k = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        return AXIsProcessTrustedWithOptions([k: prompt] as CFDictionary)
    }

    /// 按配置开/关引擎。`prompt=true` 只用在「用户刚把开关打开」那一次 ——
    /// 启动时静默检查，免得每次开窗口都弹一次授权框。
    func apply(smooth: Bool, reverse: Bool, speed: Double, prompt: Bool) {
        self.reverse = reverse
        self.speed = max(0.2, min(5.0, speed))
        if smooth { start(prompt: prompt) } else { stop() }
    }

    func start(prompt: Bool) {
        if running { return }
        guard axPermitted(prompt: prompt) else {
            logLine("[鼠标] 平滑滚动没起来：还没有「辅助功能」权限"
                    + (prompt ? "（已弹系统提示）" : "（静默检查）"))
            return
        }
        let mask = CGEventMask(1) << CGEventType.scrollWheel.rawValue
        guard let t = CGEvent.tapCreate(tap: .cgSessionEventTap,
                                        place: .headInsertEventTap,
                                        options: .defaultTap,
                                        eventsOfInterest: mask,
                                        callback: smoothTapCB,
                                        userInfo: nil) else {
            logLine("[鼠标] ✗ CGEvent.tapCreate 失败（刚勾上权限时，重启一次本 app 再试）")
            return
        }
        tap = t
        runLoopSrc = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, t, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), runLoopSrc, .commonModes)
        CGEvent.tapEnable(tap: t, enable: true)
        running = true
        logLine("[鼠标] 平滑滚动已启用（速度 \(speed)×，方向"
                + (reverse ? "反转" : "原样") + "）")
    }

    func stop() {
        guard running else { return }
        if let t = tap { CGEvent.tapEnable(tap: t, enable: false) }
        if let r = runLoopSrc { CFRunLoopRemoveSource(CFRunLoopGetMain(), r, .commonModes) }
        tap = nil
        runLoopSrc = nil
        running = false
        q.async { [weak self] in
            self?.pending = 0
            self?.carry = 0
            self?.stopTicker()
        }
        logLine("[鼠标] 平滑滚动已停用（滚轮回到系统原生行为）")
    }

    func reenable() {
        if let t = tap { CGEvent.tapEnable(tap: t, enable: true) }
    }

    // ------------------------------------------------------------ 事件进来
    /// 返回 nil = 吞掉这个原事件（改由我们合成）；返回原事件 = 原样放行。
    func handle(_ ev: CGEvent) -> Unmanaged<CGEvent>? {
        if ev.getIntegerValueField(.eventSourceUserData) == magic {
            return Unmanaged.passUnretained(ev)          // 自己合成的 → 放行
        }
        if ev.getIntegerValueField(.scrollWheelEventIsContinuous) != 0 {
            return Unmanaged.passUnretained(ev)          // 触控板 → 放行
        }
        let flags = ev.flags
        if flags.contains(.maskCommand) || flags.contains(.maskAlternate)
            || flags.contains(.maskControl) {
            return Unmanaged.passUnretained(ev)          // ⌘/⌥/⌃ 的滚轮 → 放行
        }
        let ln1 = ev.getIntegerValueField(.scrollWheelEventDeltaAxis1)
        let pt1 = ev.getIntegerValueField(.scrollWheelEventPointDeltaAxis1)
        let ln2 = ev.getIntegerValueField(.scrollWheelEventDeltaAxis2)
        let pt2 = ev.getIntegerValueField(.scrollWheelEventPointDeltaAxis2)
        if ln1 == 0 && pt1 == 0 && (ln2 != 0 || pt2 != 0) {
            return Unmanaged.passUnretained(ev)          // 纯横向 → 放行
        }
        var px = SmoothMath.impulse(pointDelta: Double(pt1), lineDelta: Double(ln1),
                                    speed: speed)
        if reverse { px = -px }
        if px == 0 { return Unmanaged.passUnretained(ev) }

        q.async { [weak self] in
            guard let s = self else { return }
            s.pending = max(-SmoothMath.maxPending,
                            min(SmoothMath.maxPending, s.pending + px))
            s.ensureTicker()
        }
        return nil                                       // ★ 吞掉原事件
    }

    // ------------------------------------------------------------ 120Hz 推进
    private func ensureTicker() {
        if timer != nil { return }
        let t = DispatchSource.makeTimerSource(queue: q)
        t.schedule(deadline: .now(), repeating: 1.0 / 120.0, leeway: .milliseconds(1))
        t.setEventHandler { [weak self] in self?.tick() }
        lastTick = CFAbsoluteTimeGetCurrent()
        timer = t
        t.resume()
    }

    private func stopTicker() {
        timer?.cancel()
        timer = nil
    }

    private func tick() {
        let now = CFAbsoluteTimeGetCurrent()
        var dt = now - lastTick
        lastTick = now
        if dt <= 0 { dt = 1.0 / 120.0 }
        if dt > 0.05 { dt = 0.05 }                  // 卡了一下也别一下推太多
        let s = SmoothMath.step(pending: pending, dt: dt, speed: speed)
        if s == 0 {
            if pending == 0 { stopTicker() }
            return
        }
        pending -= s
        if abs(pending) < 0.02 { pending = 0 }
        emit(s)
        if pending == 0 { stopTicker() }
    }

    /// 发一个像素级的滚轮事件。
    /// ⚠️ 亚像素的量先攒着（`carry`）—— 不攒的话每拍都 `Int32` 截成 0，
    ///    慢速档会「一格都不动」，看着像坏了。
    private func emit(_ dy: Double) {
        carry += dy
        let out = carry.rounded(.towardZero)
        if out == 0 { return }
        carry -= out
        let v = Int32(max(-30000, min(30000, out)))
        guard let e = CGEvent(scrollWheelEvent2Source: nil, units: .pixel,
                              wheelCount: 1, wheel1: v, wheel2: 0, wheel3: 0) else { return }
        e.setIntegerValueField(.eventSourceUserData, value: magic)
        e.setIntegerValueField(.scrollWheelEventIsContinuous, value: 0)
        e.setIntegerValueField(.scrollWheelEventScrollPhase, value: 0)
        e.setIntegerValueField(.scrollWheelEventScrollCount, value: 1)
        e.post(tap: .cghidEventTap)
    }
}

// ------------------------------------------------------- 「鼠标」配置的读写与盯梢

func mouseConfPath() -> String { TOOL_DIR + "/_鼠标.json" }

/// 读 `_鼠标.json`。读不到 / 读坏了 → 回默认（与 wb_ui.MOUSE_DEFAULTS 一致）。
func readMouseConf() -> (smooth: Bool, reverse: Bool, speed: Double, dockMin: Bool) {
    var smooth = false, reverse = false, speed = 1.5, dockMin = false
    if let d = FileManager.default.contents(atPath: mouseConfPath()),
       let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] {
        if let b = o["smooth"] as? Bool { smooth = b }
        else if let n = o["smooth"] as? NSNumber { smooth = n.boolValue }
        if let b = o["reverse"] as? Bool { reverse = b }
        else if let n = o["reverse"] as? NSNumber { reverse = n.boolValue }
        if let n = o["speed"] as? NSNumber { speed = n.doubleValue }
        else if let s = o["speed"] as? String, let v = Double(s) { speed = v }
        // ★ 第卅七轮（2026-10-01 用户第 3 条）：点 Dock 图标最小化。
        if let b = o["dockMin"] as? Bool { dockMin = b }
        else if let n = o["dockMin"] as? NSNumber { dockMin = n.boolValue }
    }
    return (smooth, reverse, speed, dockMin)
}

/// 文件不存在就先落一份默认的 —— 这样下面那个 DispatchSource 一定有东西可盯。
func ensureMouseConfFile() {
    let p = mouseConfPath()
    if FileManager.default.fileExists(atPath: p) { return }
    let d: [String: Any] = ["smooth": false, "reverse": false, "speed": 1.5,
                            "dockMin": false]
    if let data = try? JSONSerialization.data(withJSONObject: d,
                                              options: [.prettyPrinted, .sortedKeys]) {
        try? data.write(to: URL(fileURLWithPath: p))
        logLine("[鼠标] 配置不存在，先落一份默认的：\(p)")
    }
}

/// 自测通道：`WB_SHELL_MOUSETEST=1`。
/// 只查**可确定核对**的东西（配置读得对不对、纯计算部分的量守恒不守恒、
/// 权限当前是什么状态、开机自启的 plist 在不在）—— 真挂 tap 要权限，自动化测不了。
func mouseSelfTest() {
    let c = readMouseConf()
    print("conf path=\(mouseConfPath())")
    print("conf smooth=\(c.smooth) reverse=\(c.reverse) speed=\(c.speed) dockMin=\(c.dockMin)")
    print("perm=\(axPermitted(prompt: false))")
    var allOK = true
    for sp in [0.5, 1.5, 3.0] {
        for ln in [1.0, 3.0] {
            let imp = SmoothMath.impulse(pointDelta: 0, lineDelta: ln, speed: sp)
            var pending = imp, total = 0.0, ticks = 0
            while pending != 0 && ticks < 4000 {
                let s = SmoothMath.step(pending: pending, dt: 1.0 / 120.0, speed: sp)
                if s == 0 { break }
                pending -= s
                if abs(pending) < 0.02 { pending = 0 }
                total += s
                ticks += 1
            }
            let ok = (abs(total - imp) < 0.02) && ticks > 3 && ticks < 400
            if !ok { allOK = false }
            print(String(format: "math speed=%.2f lines=%.0f impulse=%.1f total=%.1f"
                               + " ticks=%d ok=%@",
                         sp, ln, imp, total, ticks, ok ? "Y" : "N"))
        }
    }
    let plist = NSHomeDirectory() + "/Library/LaunchAgents/com.wb.timeguardian-mac.plist"
    print("autostart plist=\(plist) exists=\(FileManager.default.fileExists(atPath: plist))")
    print("app exists=\(FileManager.default.fileExists(atPath: "/Applications/TimeGuardian.app"))")
    print("mousetest \(allOK ? "OK" : "FAIL")")
}

// ======================================================================
// 「鼠标」栏目共用的权限判据
// ======================================================================
/// 辅助功能（Accessibility）权限检查 —— 平滑滚动 / 键盘映射 / 点 Dock 图标最小化
/// 三处**共用同一个判据**，别各写各的。
/// ★ prompt=true 才会弹系统框；启动时的静默检查一律传 false（否则每次开窗口都弹一次）。
/// ⚠️ macOS 26 起「系统设置 → 隐私与安全性」里这一栏改名叫「设备控制和数据访问」了
///    （`Privacy_Accessibility` 这个 anchor 指向的就是它）—— 界面上给用户看的文案
///    必须跟着叫这个名，否则用户找不到（2026-10-01 真踩过）。
func axPermitted(prompt: Bool) -> Bool {
    let k = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
    return AXIsProcessTrustedWithOptions([k: prompt] as CFDictionary)
}

// ======================================================================
// 点 Dock 图标最小化（第卅七轮 · 2026-10-01 · 用户第 3 条）
//
// 用户原话：「学习一下 POMMETOYS 他的点击 dock 图标最小化功能，它和我们电脑上的
//   软件叫 Doc Minimizer 的操作逻辑应该是类似的，你看看哪个操作逻辑比较好 就用哪个，
//   还是以一个开关的形式放在任务管家的滚动栏目下」（栏目本轮已更名为「鼠标」）
//
// ★ 两个参考的结论（都是扒二进制量出来的，不是猜的）
//   · PommeToys：`features.dockIconClickMinimize.enabled`，说明文字是
//     「再次点击当前激活应用的 Dock 图标时最小化其窗口」
//     （英文原文 `Click the active app's Dock icon again to minimize its windows.`）
//     痕迹：DockIconClickMinimizeController / AXMinimizeButton / isMinimized /
//           canMinimize / unminimizeWindow / AXMinimizedWindowDockItem
//   · DockMinimizer（本机 1.3）：说明文字是
//     「应用正在后台运行，点击 Dock 中的前台应用图标即可最小化其窗口」
//     痕迹：DockMonitor / WindowMinimizer / **eventTap** / AXChildren / AXPosition /
//           AXSize / AXTitle / AXRole
//   ⇒ **触发条件与动作其实是同一件事**（点那个已经在最前的 app 的图标 → 最小化它的窗口），
//     **实现路径也是同一条**（CGEventTap 判位置 ＋ AX 取图标几何）。
//     所以这里取"把前提说清楚的那一个"：触发条件照 PommeToys —— "当前激活应用的"，
//     配合"再次点击"的语义 —— 它已经在最前，所以系统本来什么都不做，正好由我们接管。
//
// ★ 自己补的一条（两个参考的说明里都没写，但不补就是个死结）
//   「窗口已经全部最小化」时**不干预**，把这次点击**放行**掉 ——
//   于是第二次点击就走 macOS 原生的"还原最小化的窗口"。
//   要是连第二次也吞掉、再最小化一遍，用户就永远还原不回来了。
//
// ★ 为什么非得用 CGEventTap（试过才知道）
//   点 Dock 上**已经在前台**的 app 的图标时，那个 app 根本没换过，
//   `NSWorkspace.didActivateApplicationNotification` **一声不吭** —— 观察不到。
//   只有事件这一层能看见"用户确实点了一下"，也就只剩这一条路。
//
// ★ 性能：不能每次左键都去遍历 Dock 的 AX 树
//   先判"这个点落不落在 Dock 的矩形里"（矩形缓存 2 秒，Dock 自己动了才重算），
//   不在就直接放行 —— 日常绝大多数点击都死在这一步，代价只有一次 rect 比较。
//
// ★ 为什么要**吞掉**这一次点击
//   macOS 对"点前台 app 的 Dock 图标"有默认行为（把被遮住的窗口提到前面）。
//   不吞 = 「先提到前面、再被我们最小化」，肉眼能看到闪一下。
//   只在"确实命中了、且确实最小化了"时才吞 —— 中间任何一步没走通就原样放行。
// ======================================================================

/// Dock 的进程号（找不到 → nil，调用方一律当作"不干预"）。
private func dockPID() -> pid_t? {
    NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.dock")
        .first?.processIdentifier
}

/// 取一个 AX 元素的矩形（拿不到 → nil）。⚠️ 先验类型再强转，别直接 `as!`。
private func axRect(_ el: AXUIElement) -> CGRect? {
    var pv: CFTypeRef?, sv: CFTypeRef?
    guard AXUIElementCopyAttributeValue(el, kAXPositionAttribute as CFString, &pv) == .success,
          AXUIElementCopyAttributeValue(el, kAXSizeAttribute as CFString, &sv) == .success,
          let p0 = pv, let s0 = sv,
          CFGetTypeID(p0) == AXValueGetTypeID(), CFGetTypeID(s0) == AXValueGetTypeID()
    else { return nil }
    var p = CGPoint.zero, s = CGSize.zero
    guard AXValueGetValue(p0 as! AXValue, .cgPoint, &p),
          AXValueGetValue(s0 as! AXValue, .cgSize, &s) else { return nil }
    return CGRect(origin: p, size: s)
}

/// Dock 占的屏幕区域（多显示器时是好几块）—— 用来做"这一点落没落在 Dock 上"的粗判。
///
/// ⚠️ **别用 `CGWindowListCopyWindowInfo` 拿 Dock 的窗口 bounds**（第一版就是这么写的，
///    自测当场打脸）：Dock 有个**铺满整块屏**的窗口（实测返回 `0,0,1470x956`），
///    拿它当 Dock 区域的话，"粗判"等于没判 —— 屏幕上任何一次点击都会掉进细判那一步，
///    等于每次点鼠标都去遍历一遍 Dock 的 AX 树。
///    改用 Dock 自己的 `AXList`（图标条那一条）的 frame：那才是真正会接到点击的区域。
///    代价是 2 次 AX 调用，但**结果缓存 2 秒**，而且就在 `handle()` 里被
///    "Dock 矩形是否过期"挡着，日常点击根本走不到这儿。
private func dockScreenRects() -> [CGRect] {
    guard let dp = dockPID() else { return [] }
    let dock = AXUIElementCreateApplication(dp)
    var cv: CFTypeRef?
    guard AXUIElementCopyAttributeValue(dock, kAXChildrenAttribute as CFString, &cv) == .success,
          let lists = cv as? [AXUIElement] else { return [] }
    var out: [CGRect] = []
    for list in lists {
        if let r = axRect(list), r.width > 0, r.height > 0 { out.append(r) }
    }
    return out
}

/// 点落在哪个 Dock 图标上 → 那个图标对应的 app 的 bundle id 与标题。
/// ★ Dock 的 AX 结构：AXApplication(dock) → 第一层 children 里那些 role=AXList 的
///   （**一块屏一个**，不是只有一个）→ 它们的 children 才是图标。
private func dockIcon(at p: CGPoint) -> (bid: String?, title: String?)? {
    guard let dp = dockPID() else { return nil }
    let dock = AXUIElementCreateApplication(dp)
    var cv: CFTypeRef?
    guard AXUIElementCopyAttributeValue(dock, kAXChildrenAttribute as CFString, &cv) == .success,
          let lists = cv as? [AXUIElement] else { return nil }
    for list in lists {
        var iv: CFTypeRef?
        guard AXUIElementCopyAttributeValue(list, kAXChildrenAttribute as CFString, &iv) == .success,
              let items = iv as? [AXUIElement] else { continue }
        for it in items {
            guard let r = axRect(it), r.contains(p) else { continue }
            var u: CFTypeRef?, t: CFTypeRef?
            AXUIElementCopyAttributeValue(it, kAXURLAttribute as CFString, &u)
            AXUIElementCopyAttributeValue(it, kAXTitleAttribute as CFString, &t)
            // 图标 → app：优先用 AXURL 拿 bundle id（比名字可靠），取不到再退回标题比名字。
            let bid = (u as? URL).flatMap { Bundle(url: $0)?.bundleIdentifier }
            return (bid, t as? String)
        }
    }
    return nil
}

/// 某进程**可见**（还没被最小化）的窗口。
private func visibleWindows(of pid: pid_t) -> [AXUIElement] {
    let app = AXUIElementCreateApplication(pid)
    var wv: CFTypeRef?
    guard AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &wv) == .success,
          let wins = wv as? [AXUIElement] else { return [] }
    var out: [AXUIElement] = []
    for w in wins {
        var mv: CFTypeRef?
        AXUIElementCopyAttributeValue(w, kAXMinimizedAttribute as CFString, &mv)
        if !((mv as? Bool) ?? false) { out.append(w) }
    }
    return out
}

/// 自测通道：`WB_SHELL_DOCKTEST=1`。
/// 只查**可确定核对**的东西：Dock 的 AX 结构能不能按预期取到（图标数、每个的位置、
/// 能不能算出 bundle id）、前台 app 是谁、权限现在什么状态。
/// ⚠️ **不挂 tap、不发事件、不最小化任何窗口** —— 那些要权限，也不能在自动化里做。
/// 为什么值得留这么一条：Dock 的 AX 层级是**没有文档的**，不同系统版本可能不一样，
/// 「图标数 > 0 且都能取到 bundle id」这四条判据就是"这个引擎到底有没有戏"的体检口。
func dockSelfTest() {
    print("docktest perm=\(axPermitted(prompt: false))")
    let rects = dockScreenRects()
    let rs = rects.map { "\(Int($0.minX)),\(Int($0.minY)),\(Int($0.width))x\(Int($0.height))" }
    print("docktest dockrects=\(rects.count) \(rs.joined(separator: " | "))")
    guard let dp = dockPID() else {
        print("docktest FAIL 找不到 Dock 进程")
        return
    }
    print("docktest dockpid=\(dp)")
    let dock = AXUIElementCreateApplication(dp)
    var cv: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(dock, kAXChildrenAttribute as CFString, &cv)
    let lists = (cv as? [AXUIElement]) ?? []
    print("docktest dockchildren=\(lists.count) err=\(err.rawValue)")
    var icons = 0, withRect = 0, withBID = 0
    var sample = ""
    for list in lists {
        var iv: CFTypeRef?
        guard AXUIElementCopyAttributeValue(list, kAXChildrenAttribute as CFString, &iv) == .success,
              let items = iv as? [AXUIElement] else { continue }
        for it in items {
            icons += 1
            guard let r = axRect(it) else { continue }
            withRect += 1
            var u: CFTypeRef?
            AXUIElementCopyAttributeValue(it, kAXURLAttribute as CFString, &u)
            if let url = u as? URL, let b = Bundle(url: url)?.bundleIdentifier {
                withBID += 1
                if sample.isEmpty { sample = "\(b)@\(Int(r.midX)),\(Int(r.midY))" }
            }
        }
    }
    print("docktest icons=\(icons) withRect=\(withRect) withBundleID=\(withBID) sample=\(sample)")
    let front = NSWorkspace.shared.frontmostApplication
    print("docktest frontmost=\(front?.bundleIdentifier ?? "nil")"
          + " name=\(front?.localizedName ?? "nil")")
    // 四条都成立才算这个引擎有戏：Dock 区域拿得到、Dock 进程在、有图标、
    // 图标既能量到位置、又能算出 bundle id（拿不到 bundle id 就只能退回比名字）。
    let ok = !rects.isEmpty && icons > 0 && withRect > 0 && withBID > 0
    print("docktest \(ok ? "OK" : "FAIL")")
}

/// 左键 tap 的回调（`CGEventTapCallBack` 是 `@convention(c)`，状态只能放单例里）。
private func dockMinTapCB(proxy: CGEventTapProxy, type: CGEventType,
                          event: CGEvent,
                          refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    let eng = DockMinEngine.shared
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        logLine("[鼠标] 点 Dock 最小化的事件 tap 被系统暂停（\(type.rawValue)），重新挂上")
        eng.reenable()
        return Unmanaged.passUnretained(event)
    }
    if type != .leftMouseDown { return Unmanaged.passUnretained(event) }
    return eng.handle(event)
}

final class DockMinEngine {
    static let shared = DockMinEngine()
    private var tap: CFMachPort?
    private var runLoopSrc: CFRunLoopSource?
    private(set) var running = false

    /// Dock 矩形缓存：Dock 自己动了才重算，别每次点击都问一遍系统。
    private var rectCache: [CGRect] = []
    private var rectAt: CFAbsoluteTime = 0
    private static let RECT_TTL: CFAbsoluteTime = 2.0

    func apply(enabled: Bool, prompt: Bool) {
        if enabled { start(prompt: prompt) } else { stop() }
    }

    func start(prompt: Bool) {
        if running { return }
        guard axPermitted(prompt: prompt) else {
            logLine("[鼠标] 点 Dock 图标最小化没起来：还没有「辅助功能」权限"
                    + (prompt ? "（已弹系统提示）" : "（静默检查）"))
            return
        }
        let mask = CGEventMask(1) << CGEventType.leftMouseDown.rawValue
        guard let t = CGEvent.tapCreate(tap: .cgSessionEventTap,
                                        place: .headInsertEventTap,
                                        options: .defaultTap,
                                        eventsOfInterest: mask,
                                        callback: dockMinTapCB,
                                        userInfo: nil) else {
            logLine("[鼠标] ✗ 点 Dock 最小化：CGEvent.tapCreate 失败（权限刚勾上时重启一次再试）")
            return
        }
        tap = t
        runLoopSrc = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, t, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), runLoopSrc, .commonModes)
        CGEvent.tapEnable(tap: t, enable: true)
        running = true
        logLine("[鼠标] 点 Dock 图标最小化已启用")
    }

    func stop() {
        guard running else { return }
        if let t = tap { CGEvent.tapEnable(tap: t, enable: false) }
        if let r = runLoopSrc { CFRunLoopRemoveSource(CFRunLoopGetMain(), r, .commonModes) }
        tap = nil
        runLoopSrc = nil
        running = false
        logLine("[鼠标] 点 Dock 图标最小化已停用")
    }

    func reenable() {
        if let t = tap { CGEvent.tapEnable(tap: t, enable: true) }
    }

    /// 返回 nil = 吞掉这次点击；返回原事件 = 原样放行。
    func handle(_ ev: CGEvent) -> Unmanaged<CGEvent>? {
        let pass = Unmanaged.passUnretained(ev)
        let p = ev.location

        // ① 最便宜的一步：这个点根本不在 Dock 上 → 放行（绝大多数点击到此为止）
        let now = CFAbsoluteTimeGetCurrent()
        if rectCache.isEmpty || now - rectAt > DockMinEngine.RECT_TTL {
            rectCache = dockScreenRects()
            rectAt = now
        }
        guard rectCache.contains(where: { $0.contains(p) }) else { return pass }

        // ② 命中的是哪个图标
        guard let hit = dockIcon(at: p) else { return pass }

        // ③ 必须是**当前激活**那个 app 的图标（照 PommeToys 的说法）
        guard let front = NSWorkspace.shared.frontmostApplication else { return pass }
        let sameByBID = (hit.bid != nil && hit.bid == front.bundleIdentifier)
        let sameByName = (hit.bid == nil && hit.title != nil && hit.title == front.localizedName)
        guard sameByBID || sameByName else { return pass }

        // ④ 窗口已经全最小化了 → 不碰，放行给系统去还原（见文件头那段说明）
        let wins = visibleWindows(of: front.processIdentifier)
        if wins.isEmpty { return pass }

        var n = 0
        for w in wins {
            if AXUIElementSetAttributeValue(w, kAXMinimizedAttribute as CFString,
                                            kCFBooleanTrue) == .success { n += 1 }
        }
        if n == 0 { return pass }        // 一个都没成功就别吞 —— 免得用户觉得"点了没反应"
        logLine("[鼠标] 点 Dock 图标 → 已最小化「\(front.localizedName ?? "?")」的 \(n) 个窗口")
        return nil                        // 吞掉
    }
}

// ======================================================================
// 键盘映射引擎（第卅二轮 · 2026-10-01 · 用户第 1 条）
//
// 要解决什么（用户原话）
//   「把我用 Karabiner-Elements 对本电脑所做的**所有**键盘映射，以开关和选择
//    映射哪个按键的方式放到这个键盘栏目下，让我即使不用 Karabiner-Elements
//    这个软件，单独开任务管家也能做到键盘映射」；
//   「对于 Command X-V 这个软件所做的修改，你也放到键盘这个栏目下」。
//
// 为什么在外壳里做，而不是界面服务里
//   改别人的输入要有「辅助功能」权限（TCC），而界面服务跑在一棵**受限进程树**
//   里（见 wb_ui.py 顶部那段）—— 由外壳来做，与平滑滚动**同一套基建**：
//   同一个 `AXIsProcessTrustedWithOptions`、同一个 DispatchSource 盯配置文件。
//
// 配置：`_键盘.json`（界面的 /api/keymap 写）。本外壳只**读**它，
//   用 DispatchSource **事件驱动**地盯着（不轮询），与 `_鼠标.json` 一模一样。
//
// ⚠️ 做不到的那一件（必须在界面上说清楚）
//   Karabiner 的 `simple_modifications` 是**按设备**的（只映射那把外接键盘），
//   靠的是它自带的 DriverKit 虚拟 HID 驱动。用户态 CGEventTap 拿不到
//   「这个事件来自哪把键盘」；而且装着 Karabiner 时 `hidutil list` 里**只看得到
//   Karabiner 的虚拟键盘**，物理键盘根本不出现 —— 连「外接键盘在不在」都探不到。
//   所以本引擎的修饰键映射是**全键盘**生效的：内建键盘的 Option 也会变成 Command。
//
// 手法：**就地改事件**（in-place），不合成新事件
//   改键 = `ev.setIntegerValueField(.keyboardEventKeycode, ...)`；
//   改修饰键 = `ev.flags = ...`。为什么不「吃掉原事件 + 自己发一条」：
//     ① 合成事件要盖 magic 戳防自激（鼠标那边就是这么干的）；
//     ② 更要命的是 keyDown 被吃掉、keyUp 还在，应用会看到**孤立的 keyUp**。
//   就地改就没这两件事，而且 keyDown/keyUp 天然成对。
//
// 安全阀（三条，缺一不可）
//   ① 总开关关掉 = tap 整个卸掉（disable + 摘 RunLoop 源）；
//   ② **只在命中规则时才动事件**，其余一律原样 `passUnretained`；
//   ③ `tapDisabledByTimeout` / `tapDisabledByUserInput` 自动挂回
//      —— 不挂回就是「用一会儿突然失灵」，与鼠标那边同一个坑。
// ======================================================================

/// Karabiner 的 `key_code` 字符串 ↔ macOS 虚拟键码 / 修饰键位。
enum KeyTable {
    static let codes: [String: CGKeyCode] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
        "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
        "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
        "5": 23, "equal_sign": 24, "9": 25, "7": 26, "minus": 27, "8": 28,
        "0": 29, "close_bracket": 30, "o": 31, "u": 32, "open_bracket": 33,
        "i": 34, "p": 35, "return_or_enter": 36, "l": 37, "j": 38,
        "quote": 39, "k": 40, "semicolon": 41, "backslash": 42, "comma": 43,
        "slash": 44, "n": 45, "m": 46, "period": 47, "tab": 48,
        "spacebar": 49, "grave_accent_and_tilde": 50,
        "delete_or_backspace": 51, "escape": 53,
        "right_command": 54, "left_command": 55, "left_shift": 56,
        "caps_lock": 57, "left_option": 58, "left_control": 59,
        "right_shift": 60, "right_option": 61, "right_control": 62,
        "fn": 63, "f17": 64, "f18": 79, "f19": 80, "f20": 90,
        "f5": 96, "f6": 97, "f7": 98, "f3": 99, "f8": 100, "f9": 101,
        "f11": 103, "f13": 105, "f16": 106, "f14": 107, "f10": 109,
        "f12": 111, "f15": 113, "insert": 114, "home": 115, "page_up": 116,
        "delete_forward": 117, "f4": 118, "end": 119, "f2": 120,
        "page_down": 121, "f1": 122, "left_arrow": 123, "right_arrow": 124,
        "down_arrow": 125, "up_arrow": 126, "power": 127,
    ]

    /// 修饰键本身的虚拟键码 —— flagsChanged 事件靠它认出「按下的是哪一个」。
    static let modifierKeys: Set<CGKeyCode> = [54, 55, 56, 57, 58, 59, 60, 61, 62, 63]

    /// `left_control` → `control`：Karabiner 能分左右（驱动层），我们分不了
    /// （CGEvent 的 flags 只有 `maskControl` 这一位），统一按基础名匹配。
    static func base(_ mod: String) -> String {
        switch mod {
        case "left_control", "right_control": return "control"
        case "left_shift", "right_shift": return "shift"
        case "left_option", "right_option": return "option"
        case "left_command", "right_command": return "command"
        default: return mod
        }
    }

    static func flag(_ mod: String) -> CGEventFlags {
        switch base(mod) {
        case "control": return .maskControl
        case "option": return .maskAlternate
        case "command": return .maskCommand
        case "shift": return .maskShift
        default: return []
        }
    }

    /// 四个「普通修饰键」的位；其余位（如 `.maskNonCoalesced`）改写时要留着。
    static let realMods: CGEventFlags = [.maskShift, .maskControl,
                                         .maskAlternate, .maskCommand]

    /// 当前按着哪几个修饰键（基础名）。
    static func activeMods(_ f: CGEventFlags) -> Set<String> {
        var s = Set<String>()
        if f.contains(.maskControl) { s.insert("control") }
        if f.contains(.maskAlternate) { s.insert("option") }
        if f.contains(.maskCommand) { s.insert("command") }
        if f.contains(.maskShift) { s.insert("shift") }
        return s
    }

    /// 是不是功能键（F1~F24）。见 `KeyEngine.post()` 里为什么发它要补 fn。
    static func isFunctionKey(_ k: String) -> Bool {
        guard k.count >= 2, k.first == "f" else { return false }
        guard let n = Int(k.dropFirst()) else { return false }
        return n >= 1 && n <= 24
    }

    /// 把 flags 画成 `⌃⌥⇧⌘` 这样的符号串 —— 只给日志用。
    /// 排查「某条快捷键不灵」时，日志里能一眼分出「按的是 ⌃X」还是「按的是 ⌘X」。
    static func glyphs(_ f: CGEventFlags) -> String {
        var s = ""
        if f.contains(.maskControl) { s += "⌃" }
        if f.contains(.maskAlternate) { s += "⌥" }
        if f.contains(.maskShift) { s += "⇧" }
        if f.contains(.maskCommand) { s += "⌘" }
        return s
    }
}

/// 一条规则的**条件**（只保留本机用到的三种）。
struct KCond {
    var type = ""                    // frontmost_application_if/_unless / input_source_*
    var bundles: [String] = []       // 正则
    var paths: [String] = []         // 正则，匹配 app 的路径
    var imeIDs: [String] = []
    var regexes: [NSRegularExpression] = []
    var pathRegexes: [NSRegularExpression] = []

    static func compile(_ r: String) -> NSRegularExpression? {
        try? NSRegularExpression(pattern: r, options: [.caseInsensitive])
    }
    func matches(_ s: String, _ list: [NSRegularExpression]) -> Bool {
        let r = NSRange(s.startIndex..<s.endIndex, in: s)
        for re in list where re.firstMatch(in: s, options: [], range: r) != nil {
            return true
        }
        return false
    }
}

// ---------------------------------------------------------------- 设备归属
//
// ★★★ 2026-10-02（用户：「Alt→Cmd 从『只对外接键盘』变成『全键盘』…你最好还是
//     给我改成一模一样的」）：Karabiner 的 `devices[].simple_modifications` 是
//     **按设备**生效的，所以内建键盘的 Option 不受影响。要用用户态复刻这件事，
//     必须知道「这个事件是哪把键盘发出来的」。
//
// ★ Karabiner 官方 DEVELOPMENT.md 的原话：「With CGEventTap, there is no way to
//   determine which device an event came from」—— 那是针对**公开 API** 说的。
//   CoreGraphics 里有个私有符号 `CGEventCopyIOHIDEvent`（从 SkyLight 再导出），
//   能把 CGEvent 背后的 IOHIDEvent 取出来；再用 IOKit 的私有 `IOHIDEventGetSenderID`
//   拿到产出该事件的设备 registry id，反查 IORegistry 就有 Product / VendorID / Built-In。
//   两者都不在公开头文件里 ⇒ 用 `@_silgen_name` 直接绑符号。
//
// ⚠️ 私有 API 可能在某次系统更新后消失。所以**必须 fail-safe**：
//    拿不到归属（返回 nil / sid=0 / 查不到服务）时一律当作「目标设备」，
//    宁可多映射一把键盘，也绝不把用户要的功能悄悄关掉；同时写一行日志留痕。

@_silgen_name("CGEventCopyIOHIDEvent")
func _wbCGEventCopyIOHIDEvent(_ e: CGEvent) -> Unmanaged<AnyObject>!

@_silgen_name("IOHIDEventGetSenderID")
func _wbIOHIDEventGetSenderID(_ e: AnyObject) -> UInt64

/// 事件的发出设备（`sid == 0` ⇒ 拿不到，多为合成事件）。
func wbEventSenderID(_ ev: CGEvent) -> UInt64 {
    guard let hid = _wbCGEventCopyIOHIDEvent(ev) else { return 0 }
    // 函数名是 Copy ⇒ 按 CF 命名规范是 +1，takeRetainedValue 才是对的（不会漏）。
    return _wbIOHIDEventGetSenderID(hid.takeRetainedValue())
}

private func wbRegProp(_ svc: io_service_t, _ k: String) -> CFTypeRef? {
    IORegistryEntryCreateCFProperty(svc, k as CFString, kCFAllocatorDefault, 0)?
        .takeRetainedValue()
}
private func wbRegText(_ svc: io_service_t, _ k: String) -> String {
    guard let p = wbRegProp(svc, k), CFGetTypeID(p) == CFStringGetTypeID() else { return "" }
    return (unsafeBitCast(p, to: CFString.self) as String)
}
private func wbRegInt(_ svc: io_service_t, _ k: String) -> Int {
    guard let p = wbRegProp(svc, k), let n = p as? NSNumber else { return -1 }
    return n.intValue
}
private func wbRegBool(_ svc: io_service_t, _ k: String) -> Bool {
    guard let p = wbRegProp(svc, k), CFGetTypeID(p) == CFBooleanGetTypeID() else { return false }
    return CFBooleanGetValue(unsafeBitCast(p, to: CFBoolean.self))
}

/// 一把键盘的身份。
struct KDevInfo {
    var known = false               // 查到了吗（查不到 ⇒ 照旧映射）
    var name = ""                   // IORegistry 里的 Product
    var vendor = -1                 // VendorID
    var product = -1                // ProductID
    var builtin = false             // Built-In = Yes

    /// ⚠️ 「该不该对它做 Alt→Cmd」的判断**已搬到 `KeyEngine.takesModMap(_:)`** ——
    ///   因为它要看 `conf.modDevices`（设备白名单），这里拿不到 conf。
    ///   判定逻辑的来龙去脉见那个函数。
}

/// 一条键映射规则。形状**照抄 Karabiner**，这里只翻译。
struct KRule {
    var id = ""
    var enabled = true
    var desc = ""
    var ord = 0                      // Karabiner 里的原始序号（决定匹配顺序）
    var fromKey = ""
    var fromMods: [String] = []      // mandatory
    var anyOptional = false          // optional 里有 "any" → 允许多余修饰键
    var toKey = ""
    var toMods: [String] = []
    var aloneKeys: [String] = []     // to_if_alone 的 key_code 列表
    var conds: [KCond] = []
}

struct KGroup { var key = ""; var enabled = true; var rules: [KRule] = [] }
struct KModMap { var enabled = true; var from = ""; var to = "" }

struct KConf {
    var enabled = false
    var modEnabled = false
    var mods: [KModMap] = []
    /// 修饰键映射（Alt→Cmd）对谁生效：`"external"`＝按下面的**设备白名单**（默认）、
    /// `"all"`＝全键盘（逃生门，不看名单）。
    var modScope = "external"
    /// ★★★ 照抄 Karabiner `devices[].simple_modifications` 的语义：**只有列在这里的
    /// 设备**才做 Alt→Cmd。原本那 4 台是 `[13652,64007] [13652,64009] [2189,1327]`
    /// （内建键盘**不在**名单里，这才是它不受影响的真正原因）。
    /// 空 ⇒ 退回旧行为（内建键盘跳过、其余都映射）。
    var modDevices: [[Int]] = []
    var groups: [KGroup] = []
    var rules: [KRule] = []          // 拍平后按 ord 排序的「生效顺序」
    var finderEnabled = false
    var finderTargets: [String] = []
    var cutTimeout: Double = 300
}

func keyConfPath() -> String { TOOL_DIR + "/_键盘.json" }

/// 读 `_键盘.json`。读不到 / 读坏了 → 回「全关」的空配置（**绝不**默认打开）。
func readKeyConf() -> KConf {
    var c = KConf()
    guard let d = FileManager.default.contents(atPath: keyConfPath()),
          let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else {
        return c
    }
    if let e = o["engine"] as? [String: Any] {
        c.enabled = (e["enabled"] as? Bool) ?? ((e["enabled"] as? NSNumber)?.boolValue ?? false)
    }
    if let m = o["modifiers"] as? [String: Any] {
        c.modEnabled = (m["enabled"] as? Bool) ?? ((m["enabled"] as? NSNumber)?.boolValue ?? false)
        // ★ scope 只认两个值；写歪了/没写 ⇒ 回默认 external（＝按设备白名单映射）。
        if let s = m["scope"] as? String, s == "all" || s == "external" { c.modScope = s }
        // ★ 设备白名单（Karabiner 的 `devices[].identifiers`）：`[[厂商,产品], …]`
        if let ds = m["devices"] as? [[Int]] {
            c.modDevices = ds.filter { $0.count == 2 }
        } else if let ds = m["devices"] as? [[NSNumber]] {
            c.modDevices = ds.filter { $0.count == 2 }.map { $0.map { $0.intValue } }
        }
        for raw in (m["items"] as? [[String: Any]]) ?? [] {
            let on = (raw["enabled"] as? Bool) ?? ((raw["enabled"] as? NSNumber)?.boolValue ?? true)
            let f = (raw["from"] as? [String: Any])?["key_code"] as? String ?? ""
            let t = ((raw["to"] as? [[String: Any]])?.first)?["key_code"] as? String ?? ""
            if !f.isEmpty && !t.isEmpty {
                c.mods.append(KModMap(enabled: on, from: f, to: t))
            }
        }
    }
    for g in (o["groups"] as? [[String: Any]]) ?? [] {
        var gg = KGroup()
        gg.key = g["key"] as? String ?? ""
        gg.enabled = (g["enabled"] as? Bool) ?? ((g["enabled"] as? NSNumber)?.boolValue ?? true)
        for raw in (g["rules"] as? [[String: Any]]) ?? [] {
            if let r = parseRule(raw) { gg.rules.append(r) }
        }
        c.groups.append(gg)
        if gg.enabled { c.rules.append(contentsOf: gg.rules.filter { $0.enabled }) }
    }
    // ★ 匹配顺序 = Karabiner 的原始序号。分组只是**呈现层**，不能改变先命中先赢的顺序。
    c.rules.sort { $0.ord < $1.ord }
    if let x = o["commandXV"] as? [String: Any] {
        c.finderEnabled = (x["enabled"] as? Bool) ?? ((x["enabled"] as? NSNumber)?.boolValue ?? false)
        c.finderTargets = (x["targets"] as? [String]) ?? []
        if let n = x["cutTimeoutSec"] as? NSNumber { c.cutTimeout = n.doubleValue }
    }
    if c.finderTargets.isEmpty { c.finderTargets = ["com.apple.finder"] }
    return c
}

private func parseRule(_ raw: [String: Any]) -> KRule? {
    var r = KRule()
    r.id = raw["id"] as? String ?? ""
    r.desc = raw["desc"] as? String ?? ""
    r.ord = (raw["ord"] as? NSNumber)?.intValue ?? 0
    r.enabled = (raw["enabled"] as? Bool) ?? ((raw["enabled"] as? NSNumber)?.boolValue ?? true)
    guard let f = raw["from"] as? [String: Any],
          let fk = f["key_code"] as? String else { return nil }
    r.fromKey = fk
    if let mo = f["modifiers"] as? [String: Any] {
        r.fromMods = (mo["mandatory"] as? [String]) ?? []
        let opt = (mo["optional"] as? [String]) ?? []
        r.anyOptional = opt.contains("any")
    }
    if let to = (raw["to"] as? [[String: Any]])?.first {
        r.toKey = to["key_code"] as? String ?? ""
        r.toMods = (to["modifiers"] as? [String]) ?? []
    }
    for a in (raw["to_if_alone"] as? [[String: Any]]) ?? [] {
        if let k = a["key_code"] as? String { r.aloneKeys.append(k) }
    }
    for cd in (raw["conditions"] as? [[String: Any]]) ?? [] {
        var c = KCond()
        c.type = cd["type"] as? String ?? ""
        c.bundles = (cd["bundle_identifiers"] as? [String]) ?? []
        c.paths = (cd["file_paths"] as? [String]) ?? []
        for s in (cd["input_sources"] as? [[String: Any]]) ?? [] {
            if let i = s["input_source_id"] as? String { c.imeIDs.append(i) }
        }
        c.regexes = c.bundles.compactMap { KCond.compile($0) }
        c.pathRegexes = c.paths.compactMap { KCond.compile($0) }
        r.conds.append(c)
    }
    return r
}

/// 滚轮那边是 `smoothTapCB`；键盘这边同理 —— `CGEventTapCallBack` 是
/// `@convention(c)`，闭包捕获不了东西，所以状态全在单例里。
private func keyTapCB(proxy: CGEventTapProxy, type: CGEventType,
                      event: CGEvent,
                      refcon: UnsafeMutableRawPointer?) -> Unmanaged<CGEvent>? {
    let eng = KeyEngine.shared
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        logLine("[键盘] 事件 tap 被系统暂停（\(type.rawValue)），重新挂上")
        eng.reenable()
        return Unmanaged.passUnretained(event)
    }
    return eng.handle(event)
}

final class KeyEngine {
    static let shared = KeyEngine()

    private var tap: CFMachPort?
    private var runLoopSrc: CFRunLoopSource?
    private(set) var running = false

    private var conf = KConf()
    /// 已经改写过 keyDown 的键：keyUp 要跟着改写，否则应用会收到孤立 keyUp。
    private var rewritten: [CGKeyCode: (key: String, mods: [String])] = [:]
    /// 「单击才算」的那些键（普通键的 `to_if_alone`）：按下时记为 true，
    /// 中间只要有**别的键**按下就置 false。
    private var aloneArmed: [CGKeyCode: Set<String>] = [:]
    /// 「修饰键单击」的候选（本机＝左右 Shift 切中英，kb-50~53）。
    /// ★ 必须与 `aloneArmed` 分开：那个走 `keyDown` 分支（普通键），
    ///   这个走 `flagsChanged` 分支（修饰键）—— 两条完全不同的路。
    private struct ModAlone {
        var code: CGKeyCode        // 按下的是哪一个修饰键（**原始**键码，未被 applyModMaps 翻过）
        var keys: [String]         // 单击时要发的键（本机是 f17）
        var at: CFAbsoluteTime     // 按下时刻（配合 aloneTimeout 判「单击」）
    }
    private var modAlone: ModAlone?
    /// Karabiner 的 `to_if_alone_timeout_milliseconds` 默认值（1000ms）。
    /// ⚠️ **必须超时**：按住 Shift 用鼠标点选文件时，本 tap 只订阅了键盘事件、
    ///    看不见鼠标，不设超时就会在松手那一刻**误切输入法**。
    ///    这正是 Karabiner 也留着这个超时的原因。
    private let aloneTimeout: Double = 1.0

    /// senderID → 设备身份。★ 必须缓存：反查 IORegistry 不便宜，而设备身份在一次
    /// 连接里不变（`IORegistryEntryIDMatching` 的 id 只到本次开机为止）。
    private var devCache: [UInt64: KDevInfo] = [:]
    /// 已经把哪些设备写进过日志（每台只写一次，免得刷屏）。
    private var devLogged: Set<UInt64> = []

    /// 最近一条「规则命中」日志的签名与时刻 —— 同一颗键按住连发时不重复刷屏。
    private var lastRuleLog = ""
    private var lastRuleLogAt: CFAbsoluteTime = 0

    /// 认出事件来自哪把键盘，顺便（首次见到就）写一行日志。
    /// 该不该对这台设备做「Alt→Cmd」这类修饰键映射？
    ///
    /// ★★★ 2026-10-02（用户：「你应该完整学习之前 Karabiner 的完整行为，他怎么做的我们就怎么做」）：
    ///   改成**照抄 Karabiner `devices[].simple_modifications` 的白名单语义** ——
    ///   只有**明确列进 `_键盘.json::modifiers.devices` 的设备**才映射。
    ///   原配置里那 4 台（`13652/64007`、`13652/64009`×2、`2189/1327`）每台各带 2 条相同的
    ///   `left_option→left_command` / `right_option→right_command`。
    ///   ★ **内建键盘根本不在名单里** —— 它不受影响是因为「不在名单」，
    ///     而不是因为我上一轮发明的 `Built-In` / `vendor != 2189` 那套判断。
    ///     那两个条件已经删掉（尤其 `vendor != 2189`：是我自己加的，Karabiner 里没有，
    ///     而且 `2189/1327` 明明白白就在名单上）。
    ///
    /// 三条兜底（都不会让用户正开着的功能突然全失效）：
    ///   · `scope == "all"` ⇒ 不看名单，全键盘都映射（界面上那颗开关）；
    ///   · 名单为空（配置里没写）⇒ 退回「非内建键盘都映射」；
    ///   · `!known`（认不出设备）⇒ 映射。**fail-safe**：宁可多认一把，也绝不悄悄关掉功能。
    private func takesModMap(_ d: KDevInfo) -> Bool {
        if conf.modScope == "all" { return true }
        if !d.known { return true }
        if conf.modDevices.isEmpty { return !d.builtin }
        return conf.modDevices.contains([d.vendor, d.product])
    }

    private func devInfo(_ sid: UInt64) -> KDevInfo {
        if let d = devCache[sid] { return d }
        var d = KDevInfo()
        if sid != 0, let m = IORegistryEntryIDMatching(sid) {
            // ⚠️ IOServiceGetMatchingService 会**吃掉**这个字典的引用，别再手工 release。
            let svc = IOServiceGetMatchingService(kIOMainPortDefault, m)
            if svc != 0 {
                defer { IOObjectRelease(svc) }
                d.known = true
                d.name = wbRegText(svc, "Product")
                d.vendor = wbRegInt(svc, "VendorID")
                d.product = wbRegInt(svc, "ProductID")
                d.builtin = wbRegBool(svc, "Built-In")
            }
        }
        devCache[sid] = d
        if !devLogged.contains(sid) {
            devLogged.insert(sid)
            if !d.known {
                logLine("[键盘][设备] 认不出来（sid=\(sid)，多是合成事件）→ 按「参与映射」处理")
            } else {
                let inList = conf.modDevices.contains([d.vendor, d.product])
                logLine("[键盘][设备] \(d.name.isEmpty ? "(无名)" : d.name)"
                        + " 厂商=\(d.vendor) 产品=\(d.product) 内建=\(d.builtin ? "是" : "否")"
                        + " 名单=\(conf.modDevices.isEmpty ? "(空)" : (inList ? "在" : "不在"))"
                        + " → 修饰键映射\(takesModMap(d) ? "生效" : "跳过")")
            }
        }
        return d
    }
    /// Finder 剪切待粘贴（Command X-V 那一套）。
    private(set) var cutPending = false
    private var cutAt: CFAbsoluteTime = 0
    private(set) var cutCount = 0

    var statusText: String {
        if !running { return "未运行" }
        return "运行中（\(conf.rules.count) 条规则" + (conf.modEnabled ? "，含修饰键" : "")
            + (conf.finderEnabled ? "，含 Finder 剪切" : "") + "）"
    }

    // ---------------------------------------------------------------- 开关
    func apply(_ c: KConf, prompt: Bool) {
        conf = c
        if c.enabled { start(prompt: prompt) } else { stop() }
        // 引擎重挂后这些瞬态必然失效，清掉免得留个「假的待粘贴」。
        rewritten.removeAll()
        aloneArmed.removeAll()
        modAlone = nil
        cutPending = false
    }

    func start(prompt: Bool) {
        if running { return }
        guard axPermitted(prompt: prompt) else {
            logLine("[键盘] 键映射没起来：还没有「辅助功能」权限"
                    + (prompt ? "（已弹系统提示）" : "（静默检查）"))
            return
        }
        let mask = (CGEventMask(1) << CGEventType.keyDown.rawValue)
                 | (CGEventMask(1) << CGEventType.keyUp.rawValue)
                 | (CGEventMask(1) << CGEventType.flagsChanged.rawValue)
        guard let t = CGEvent.tapCreate(tap: .cgSessionEventTap,
                                        place: .headInsertEventTap,
                                        options: .defaultTap,
                                        eventsOfInterest: mask,
                                        callback: keyTapCB,
                                        userInfo: nil) else {
            logLine("[键盘] ✗ CGEvent.tapCreate 失败（刚勾上权限时，重启一次本 app 再试）")
            return
        }
        tap = t
        runLoopSrc = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, t, 0)
        // ★ 必须挂 `.commonModes`：只挂 default 的话，菜单一拉开、面板一弹就停摆。
        CFRunLoopAddSource(CFRunLoopGetMain(), runLoopSrc, .commonModes)
        CGEvent.tapEnable(tap: t, enable: true)
        running = true
        logLine("[键盘] 键映射已启用：\(conf.rules.count) 条规则，修饰键映射 "
                + (conf.modEnabled ? "开" : "关") + "，Finder 剪切 "
                + (conf.finderEnabled ? "开" : "关"))
    }

    func stop() {
        guard running else { return }
        if let t = tap { CGEvent.tapEnable(tap: t, enable: false) }
        if let r = runLoopSrc { CFRunLoopRemoveSource(CFRunLoopGetMain(), r, .commonModes) }
        tap = nil
        runLoopSrc = nil
        running = false
        logLine("[键盘] 键映射已停用（按键回到系统原始行为）")
    }

    func reenable() {
        if let t = tap { CGEvent.tapEnable(tap: t, enable: true) }
    }

    // ---------------------------------------------------------- 条件判断
    private func frontmostOK(_ c: KCond) -> Bool {
        let app = NSWorkspace.shared.frontmostApplication
        let bid = app?.bundleIdentifier ?? ""
        let path = app?.bundleURL?.path ?? ""
        let hitB = !c.regexes.isEmpty && c.matches(bid, c.regexes)
        let hitP = !c.pathRegexes.isEmpty && c.matches(path, c.pathRegexes)
        return hitB || hitP
    }

    private func imeID() -> String {
        guard let src = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue(),
              let p = TISGetInputSourceProperty(src, kTISPropertyInputSourceID) else {
            return ""
        }
        return (Unmanaged<CFString>.fromOpaque(p).takeUnretainedValue() as String)
    }

    private func condsOK(_ r: KRule) -> Bool {
        for c in r.conds {
            switch c.type {
            case "frontmost_application_if":
                if !frontmostOK(c) { return false }
            case "frontmost_application_unless":
                if frontmostOK(c) { return false }
            case "input_source_if":
                if c.imeIDs.isEmpty || !c.imeIDs.contains(imeID()) { return false }
            case "input_source_unless":
                if c.imeIDs.contains(imeID()) { return false }
            case "device_if":
                // ⚠️ 用户态拿不到设备。本机只有一条用到它（[外置键盘] Cmd+F4→Cmd+Q），
                //    它真正想要的是「外接键盘插着时」——探不到，就按「总是满足」处理，
                //    反正这条的其它条件（frontmost_application_unless）照常生效。
                continue
            default:
                continue        // 不认识的类型一律不挡
            }
        }
        return true
    }

    // ------------------------------------------------------------ 事件进来
    func handle(_ ev: CGEvent) -> Unmanaged<CGEvent>? {
        guard running else { return Unmanaged.passUnretained(ev) }
        let t = ev.type
        if t != .keyDown && t != .keyUp && t != .flagsChanged {
            return Unmanaged.passUnretained(ev)
        }

        // ★ 2026-10-02：修饰键的**原始**键码与原始 flags 必须在 applyModMaps **之前**留一份。
        //    applyModMaps 会把 flagsChanged 的 Option 键码就地翻成 Command（见它函数里的注释），
        //    读完再翻，才不会把「按的是哪一个修饰键」认错。
        let rawCode = CGKeyCode(ev.getIntegerValueField(.keyboardEventKeycode))
        let rawFlags = ev.flags

        // ---- ① 修饰键映射：就地翻 flags（必要时连键码一起翻）
        //    ★ 顺序是死的：**先**做修饰键映射、**后**做规则匹配。
        //      这正是 Karabiner 的管线（simple_modifications 在驱动层先落地），
        //      也是它能「Alt+F4 → Cmd+Q」而不打架的原因 —— 见 §外置键盘那条补偿规则。
        //    ★ 设备归属也是在这一步问的：拿不到就按「参与映射」处理（fail-safe）。
        if conf.modEnabled { applyModMaps(ev, dev: devInfo(wbEventSenderID(ev))) }

        // ---- ② 修饰键自己：单击检测（Karabiner 对修饰键的 `to_if_alone`）
        //    ★★ 2026-10-02（用户：「Shift 切换输入法好像掉了，确实是掉了」）：
        //       这里原来是一句 `return` 直接放行 —— 于是 `from` 是修饰键的规则
        //       （kb-50~53 那四条「左右 Shift 单击切中英」）**永远匹配不到**：
        //       规则匹配整段都在下面的 `keyDown` 分支里，而修饰键走的是 `flagsChanged`。
        //       Karabiner 靠驱动层做到了这件事，用户态没有那一层，就得自己记状态。
        if t == .flagsChanged {
            handleModifier(code: rawCode, flags: rawFlags)
            return Unmanaged.passUnretained(ev)
        }

        let code = rawCode
        let down = (t == .keyDown)

        // ---- ③ Finder 剪切 / 粘贴（Command X-V 的等价物）
        //    ★ 2026-10-02：多传一个 `rawFlags`（用户**实际按的**修饰键，即 applyModMaps 之前那份），
        //      只为让日志能写清「按的是 ⌃X 还是 ⌘X」。判断本身用推演后的形态（见 finderHandle）。
        if conf.finderEnabled && finderHandle(ev, code: code, down: down, rawFlags: rawFlags) {
            return Unmanaged.passUnretained(ev)
        }

        // ---- ④ 别的键按下了 → 那些「等单击」的键（普通键 + 修饰键）都不再是单击
        if down && !KeyTable.modifierKeys.contains(code) {
            aloneArmed.removeAll()
            modAlone = nil
        }

        // ---- ⑤ 规则匹配
        if down {
            for r in conf.rules where condsOK(r) && fromMatches(r, code: code, flags: ev.flags) {
                if !r.aloneKeys.isEmpty {
                    // `to_if_alone`：**放行原事件**，只是把「还在等单击」记下来。
                    //   ★ 不吞、不延迟 —— 吞掉再补发会让 Shift 打字手感发黏。
                    aloneArmed[code] = Set(r.aloneKeys)
                    return Unmanaged.passUnretained(ev)
                }
                guard !r.toKey.isEmpty, let dst = KeyTable.codes[r.toKey] else { break }
                ev.setIntegerValueField(.keyboardEventKeycode, value: Int64(dst))
                ev.flags = flagsOf(r.toMods, keeping: ev.flags)
                rewritten[code] = (r.toKey, r.toMods)
                // ★ 2026-10-02：命中就打一行。**只有命中规则才打** —— 正常打字不命中任何
                //   规则，所以不会刷屏；而「某条快捷键不灵」时，这一行是唯一能区分
                //   「规则压根没命中」与「命中了、但后面被别的环节吃掉」的证据。
                //   Ctrl+X 那次就卡在后者（Finder 那步顺序错了），当时没这行只能靠猜。
                let sig = r.id + "|" + String(code)
                let now = CFAbsoluteTimeGetCurrent()
                if sig != lastRuleLog || now - lastRuleLogAt > 2 {
                    lastRuleLog = sig
                    lastRuleLogAt = now
                    logLine("[键盘] 规则命中 \(r.id)「\(r.desc)」"
                            + "\(KeyTable.glyphs(rawFlags))\(KeyTableCodes.reverse(code))"
                            + " → \(KeyTable.glyphs(ev.flags))\(KeyTableCodes.reverse(dst))")
                }
                return Unmanaged.passUnretained(ev)
            }
            return Unmanaged.passUnretained(ev)
        }

        // keyUp：配对的改写 / 单击触发
        if let w = rewritten.removeValue(forKey: code) {
            if let dst = KeyTable.codes[w.key] {
                ev.setIntegerValueField(.keyboardEventKeycode, value: Int64(dst))
                ev.flags = flagsOf(w.mods, keeping: ev.flags)
            }
            return Unmanaged.passUnretained(ev)
        }
        if let alone = aloneArmed.removeValue(forKey: code) {
            for k in alone { post(k, down: true, mods: []); post(k, down: false, mods: []) }
            logLine("[键盘] 单击 \(KeyTableCodes.reverse(code)) → \(alone.sorted().joined(separator: "、"))")
        }
        return Unmanaged.passUnretained(ev)
    }

    // ------------------------------------------------ 修饰键单击（to_if_alone）
    /// `flagsChanged` 只报「某个修饰键按下或抬起」，靠 `flags` 里对应的那一位判别方向。
    ///
    /// 判「单击」的三个条件**缺一不可**：
    ///   ① 按下那一刻，按着的修饰键**只有它自己**（`active == [base]`）——
    ///      先按 Shift 再按 Cmd、然后松 Shift，属于组合键，不算单击；
    ///   ② 中途没有普通键按下 —— 那一下会把 `modAlone` 清掉（见 handle 的 ④）；
    ///   ③ 松手距按下不超过 `aloneTimeout` —— 挡住「按住 Shift 用鼠标点选文件」这类。
    ///
    /// ★ 用传进来的 `flags`（＝applyModMaps **之前**的原始 flags）判断方向：
    ///   否则 Alt→Cmd 那两条会让「按的是 Option」被当成「按的是 Command」。
    private func handleModifier(code: CGKeyCode, flags: CGEventFlags) {
        let name = KeyTableCodes.reverse(code)                 // "left_shift" / "left_option" …
        let base = KeyTable.base(name)
        let f = KeyTable.flag(base)
        // fn / caps_lock 这类在 `CGEventFlags` 里**没有**对应的位（`flag()` 回空集），
        // 而 `flags.contains(空集)` 恒为 true ⇒ 不挡住的话会被误判成「一直按着」。
        // 本机它们也没有 to_if_alone 规则，直接退出即可。
        guard KeyTable.modifierKeys.contains(code), !f.isEmpty else { modAlone = nil; return }

        if flags.contains(f) {                                  // —— 按下
            guard KeyTable.activeMods(flags) == [base] else { modAlone = nil; return }   // ①②
            // 命中规则：`from` 就是这个修饰键、带 `to_if_alone`、且条件（输入法等）成立
            guard let r = conf.rules.first(where: {
                $0.fromKey == name && !$0.aloneKeys.isEmpty && condsOK($0)
            }) else { modAlone = nil; return }
            modAlone = ModAlone(code: code, keys: r.aloneKeys,
                                at: CFAbsoluteTimeGetCurrent())
            return
        }

        // —— 抬起：只有「就是这个键、且还在等」才触发
        guard let st = modAlone, st.code == code else { return }
        modAlone = nil
        let held = CFAbsoluteTimeGetCurrent() - st.at
        guard held <= aloneTimeout else {                                       // ③
            logLine(String(format: "[键盘] %@ 按住 %.1fs 才松 —— 不算单击", name, held))
            return
        }
        for k in st.keys { post(k, down: true, mods: []); post(k, down: false, mods: []) }
        logLine("[键盘] 单击 \(name) → \(st.keys.joined(separator: "、"))")
    }

    /// 把事件上的修饰键按配置翻一遍（Option → Command 就是这么来的）。
    ///
    /// ★★★ 2026-10-02：加设备闸门（＝Karabiner 的 `devices[].simple_modifications`）。
    ///   问「这条事件是哪把键盘发的」，**不在名单里的直接跳过** —— 内建键盘的 Option
    ///   因此原样保留（还打得出来 • ™）。判定规则见 `takesModMap(_:)`。
    private func applyModMaps(_ ev: CGEvent, dev: KDevInfo) {
        if !takesModMap(dev) { return }
        var flags = ev.flags
        var changed = false
        for m in conf.mods where m.enabled {
            let src = KeyTable.flag(m.from), dst = KeyTable.flag(m.to)
            if src == [] || dst == [] { continue }
            if flags.contains(src) {
                flags.remove(src)
                flags.insert(dst)
                changed = true
            }
            // flagsChanged：键码也要跟着翻，否则系统认为按下的是 Option，
            // 那一位的「按下/抬起」就跟 flags 对不上了。
            if ev.type == .flagsChanged {
                let kc = CGKeyCode(ev.getIntegerValueField(.keyboardEventKeycode))
                if kc == KeyTable.codes[m.from], let dk = KeyTable.codes[m.to] {
                    ev.setIntegerValueField(.keyboardEventKeycode, value: Int64(dk))
                }
            }
        }
        if changed { ev.flags = flags }
    }

    private func fromMatches(_ r: KRule, code: CGKeyCode, flags: CGEventFlags) -> Bool {
        guard let want = KeyTable.codes[r.fromKey], want == code else { return false }
        let active = KeyTable.activeMods(flags)
        let mandatory = Set(r.fromMods.map(KeyTable.base))
        if !mandatory.isSubset(of: active) { return false }
        if !r.anyOptional && !active.subtracting(mandatory).isEmpty { return false }
        return true
    }

    private func flagsOf(_ mods: [String], keeping old: CGEventFlags) -> CGEventFlags {
        var f = CGEventFlags()
        for m in mods { f.insert(KeyTable.flag(m)) }
        return f.union(old.subtracting(KeyTable.realMods))
    }

    /// 发一个自己的按键（只在 `to_if_alone` 用得上）。
    func post(_ key: String, down: Bool, mods: [String]) {
        guard let c = KeyTable.codes[key],
              let e = CGEvent(keyboardEventSource: nil, virtualKey: c, keyDown: down) else { return }
        var f = flagsOf(mods, keeping: [])
        // ★★★ 2026-10-02（用户：「之前改的那个 shift 映射还是不能切换输入法」）：
        //   发**功能键**时必须补上 fn（`maskSecondaryFn`）。原因不是猜的，是实测出来的：
        //   系统「选择下一个输入法」这条符号快捷键（symbolichotkeys id 61）里存的
        //   修饰符掩码是 `0x800000` ＝ fn（苹果键盘上 F 键本来就要配 fn）。
        //   所以 `flags = []` 的裸 F17 匹配不上，`flags = [fn]` 才切。
        //
        //   实测记录（同一台机器、同一状态，只改这一个变量）：
        //     plain（无 fn）→ 不切 ｜ fn       → 切到 ABC
        //     src （hidSystemState 事件源，无 fn）→ 不切 ｜ fn+src → 切回微信输入法
        //   ⇒ 变量是 fn，跟事件源无关。Karabiner 发功能键时同样会补 fn，所以以前它是好的。
        if KeyTable.isFunctionKey(key) { f.insert(.maskSecondaryFn) }
        e.flags = f
        e.post(tap: .cghidEventTap)
    }

    // -------------------------------------------------- Finder 剪切 / 粘贴
    /// Command X-V 干的事就是：Finder 里 ⌘X = 记下，⌘V = 「移动到这里」。
    /// 我们不自己去搬文件（跨卷 / iCloud / 权限 / 撤销都会踩坑）——
    /// **⌘X 就地改成 ⌘C、⌘V 就地改成 ⌥⌘V**，让 Finder 自己搬。这样
    /// 撤销（⌘Z）、跨卷、重名提示全都跟原生一致。
    /// 把「用户实际按的键」按规则表推演成「最终会发给系统的键」。
    ///
    /// ★★ 2026-10-02（用户：「Ctrl+X Ctrl+V 代替 Command+X Command+V，以前能实现，现在不行」）：
    ///   `finderHandle` 排在规则匹配**之前**，而它原来只看 `ev.flags` 里有没有 Command。
    ///   于是 Windows 习惯的 **Ctrl+X** 被它当成「不是 ⌘X」而放行，紧接着才被规则改写成 ⌘X
    ///   —— 结果是 **Finder 收到一个原生 ⌘X**，而 macOS 的 Finder 对文件不认 ⌘X
    ///   ⇒ 用户看到的就是「剪切没反应」；Ctrl+V 同理变成原生 ⌘V（复制粘贴），不是「移动到这里」。
    ///
    ///   在 Karabiner 时代这里是对的：驱动层先把 Ctrl 落成 Cmd，再轮到我们这一层看，
    ///   顺序天然正确。两层合成一层之后，就得自己先把这一步推演出来。
    ///
    ///   命中判据与 ⑤ 规则匹配**同源**（同一条规则表、同样的 `condsOK` / `fromMatches`），
    ///   免得两处判断走岔。
    private func resolveFinal(_ code: CGKeyCode, _ flags: CGEventFlags,
                              ignoreConds: Bool = false) -> (CGKeyCode, CGEventFlags) {
        for r in conf.rules
        where (ignoreConds || condsOK(r)) && fromMatches(r, code: code, flags: flags) {
            if !r.aloneKeys.isEmpty { break }        // `to_if_alone` 那类不参与推演
            guard !r.toKey.isEmpty, let dst = KeyTable.codes[r.toKey] else { break }
            return (dst, flagsOf(r.toMods, keeping: flags))
        }
        return (code, flags)
    }

    /// 自测入口：拿**当前配置文件**的规则表推演一次，**不看前台条件**。
    ///
    /// 为什么不直接用 `conf`：`keyMapSelfTest()` 跑在 app 启动早期，那时 tap 还没挂、
    /// `conf` 可能是空的；而条件判定靠 `NSWorkspace.shared.frontmostApplication`，
    /// 自测时前台是我们自己的终端 —— `frontmost_application_unless` 恰好会把终端排除掉，
    /// 条件在自测里**不可靠**，只能跳过。
    func resolveForSelfTest(_ code: CGKeyCode,
                            _ flags: CGEventFlags) -> (CGKeyCode, CGEventFlags) {
        let snapshot = conf
        conf = readKeyConf()
        defer { conf = snapshot }
        return resolveFinal(code, flags, ignoreConds: true)
    }

    /// 自测入口：给定一台设备的身份，回答「要不要对它做 Alt→Cmd」。
    /// 同样先临时装上**当前配置文件**（自测跑在 start 之前，`conf` 可能是空的）。
    func takesModMapForSelfTest(vendor: Int, product: Int, builtin: Bool) -> Bool {
        let snapshot = conf
        conf = readKeyConf()
        defer { conf = snapshot }
        var d = KDevInfo()
        d.known = true
        d.vendor = vendor
        d.product = product
        d.builtin = builtin
        return takesModMap(d)
    }

    /// 见下面这段的老注释：Finder 里 ⌘X = 记下、⌘V = 「移动到这里」。
    /// `rawFlags` ＝ applyModMaps **之前**的 flags，只给日志用（能分清按的是 ⌃X 还是 ⌘X）。
    private func finderHandle(_ ev: CGEvent, code: CGKeyCode, down: Bool,
                              rawFlags: CGEventFlags) -> Bool {
        guard down else { return false }
        // ★ 先推演出「最终形态」再判断 —— Ctrl+X / Ctrl+V 就是在这一步被认出来的。
        //   只在「原样按的是 Ctrl、且没按 ⌘」时推演：这样 ⌃⌘X / ⇧⌘X 这类
        //   **原本就不归我们管**的组合维持原样，不被误吞。
        var code2 = code
        var flags = ev.flags
        if flags.contains(.maskControl) && !flags.contains(.maskCommand) {
            (code2, flags) = resolveFinal(code, flags)
        }
        guard flags.contains(.maskCommand) else { return false }
        // 带别的修饰键（⇧⌃⌥）的 ⌘X/⌘V 是另一回事，不碰。
        guard flags.intersection([.maskShift, .maskControl, .maskAlternate]).isEmpty else {
            return false
        }
        guard let bid = NSWorkspace.shared.frontmostApplication?.bundleIdentifier,
              conf.finderTargets.contains(bid) else { return false }
        let userKey = KeyTable.glyphs(rawFlags)     // 日志用：⌃ / ⌘ / ⌥

        if code2 == KeyTable.codes["x"]! {
            guard let c = KeyTable.codes["c"] else { return false }
            ev.setIntegerValueField(.keyboardEventKeycode, value: Int64(c))
            ev.flags = flagsOf(["command"], keeping: flags)
            // ★ 让 keyUp 也跟着同一条改写 —— 否则会投出「⌘C 按下 + ⌃X 抬起」这种错配。
            rewritten[code] = ("c", ["command"])
            cutPending = true
            cutAt = CFAbsoluteTimeGetCurrent()
            logLine("[键盘] Finder 剪切已记下（\(userKey)X → ⌘C），"
                    + "去目标文件夹按同样的修饰键 +V")
            return true
        }
        if code2 == KeyTable.codes["v"]!, cutPending {
            if CFAbsoluteTimeGetCurrent() - cutAt > conf.cutTimeout {
                cutPending = false
                return false
            }
            guard let v = KeyTable.codes["v"] else { return false }
            ev.setIntegerValueField(.keyboardEventKeycode, value: Int64(v))
            ev.flags = flagsOf(["command", "option"], keeping: flags)   // → ⌥⌘V
            rewritten[code] = ("v", ["command", "option"])
            cutPending = false
            logLine("[键盘] Finder 粘贴＝移动到此处（\(userKey)V → ⌥⌘V）")
            return true
        }
        return false
    }
}

/// 给日志用的反向查找（只在出错/诊断路径上用，不在热路径）。
enum KeyTableCodes {
    static func reverse(_ c: CGKeyCode) -> String {
        for (k, v) in KeyTable.codes where v == c { return k }
        return "keycode \(c)"
    }
}

/// 自测通道：`WB_SHELL_KEYMAPTEST=1`。
/// 只查**可确定核对**的部分（配置读得对不对、规则表翻得对不对、
/// 匹配判据对不对、权限当前是什么状态）—— 真挂 tap 要权限，自动化测不了。
func keyMapSelfTest() {
    let c = readKeyConf()
    print("conf path=\(keyConfPath())")
    print("engine=\(c.enabled) mods=\(c.modEnabled)/\(c.mods.count)"
          + " groups=\(c.groups.count) rules=\(c.rules.count)"
          + " finder=\(c.finderEnabled) targets=\(c.finderTargets.count)")
    var allOK = true
    func chk(_ name: String, _ cond: Bool) {
        if !cond { allOK = false }
        print("check \(name)=\(cond ? "Y" : "N")")
    }
    // 「读不到配置 ⇒ 回全关」只在**配置真不在**时才有得验。
    // ★ 2026-10-02：原来写成 `!exists || !enabled` —— 配置一旦存在且开着（＝正常状态），
    //   这条就恒判负，整个自测永远 FAIL，把真问题淹掉（修 Shift 那次就被它误导过一回）。
    let confExists = FileManager.default.fileExists(atPath: keyConfPath())
    chk("missing_conf_means_off", confExists || !c.enabled)
    print("conf_exists=\(confExists) engine_on=\(c.enabled)")
    chk("rule_order_is_karabiner_order",
        c.rules.map { $0.ord } == c.rules.map { $0.ord }.sorted())
    chk("ctrl_c_wins_before_ctrl_shift_c", {
        let a = c.rules.firstIndex { $0.fromKey == "c" && $0.fromMods == ["left_control"] }
        let b = c.rules.firstIndex { $0.fromKey == "c" && Set($0.fromMods) == ["control", "shift"] }
        guard let a = a, let b = b else { return true }     // 没配就跳过
        return a < b
    }())
    // ★ 2026-10-02：`from` 是修饰键、带 `to_if_alone` 的规则（左右 Shift 切中英）
    //   必须能被解析出来 —— 这是「Shift 单击」那次修复的**回归判据**。
    //   修复前引擎在 flagsChanged 处直接放行，这几条永远匹配不到。
    let modAloneRules = c.rules.filter {
        !$0.aloneKeys.isEmpty
            && KeyTable.modifierKeys.contains(KeyTable.codes[$0.fromKey] ?? CGKeyCode(9999))
    }
    chk("modifier_alone_rules_present", !modAloneRules.isEmpty)
    // ⚠️ 别把这几段拼在一个 print 的插值里 —— 编译器会「type-check 超时」。
    let maIds = modAloneRules.map { $0.id }.joined(separator: ",")
    let maKeys = modAloneRules.map { $0.fromKey }.joined(separator: ",")
    let maOut = modAloneRules.flatMap { $0.aloneKeys }.joined(separator: ",")
    print("modifier_alone=\(modAloneRules.count) ids=\(maIds) keys=\(maKeys) out=\(maOut)")

    // ★ 2026-10-02 两处新判据。
    // ① 修饰键映射的作用域：默认必须是「只对外接键盘」。
    chk("mod_scope_external", c.modScope == "external")
    // ★★★ 2026-10-02（用户：「你应该完整学习之前 Karabiner 的完整行为，他怎么做的我们就怎么做」）：
    //   Alt→Cmd 的适用对象＝**设备白名单**（Karabiner `devices[].simple_modifications`）：
    //   只有列进 `modifiers.devices` 的设备才映射。
    //   判据用**配置里真实的名单**来验，不写死设备号 —— 换机器、换键盘也不会误红。
    if let d0 = c.modDevices.first, d0.count == 2 {
        chk("mod_map_listed_device",
            KeyEngine.shared.takesModMapForSelfTest(vendor: d0[0], product: d0[1],
                                                    builtin: false))
        let miss = [9999, 9998, 9997].first { !c.modDevices.contains([$0, $0]) } ?? 9996
        chk("mod_skip_unlisted_device",
            !KeyEngine.shared.takesModMapForSelfTest(vendor: miss, product: miss,
                                                      builtin: false))
        print("mod_devices=\(c.modDevices.count)台白名单 首台=\(d0[0])/\(d0[1])")
    } else {
        // 名单空 ⇒ 兜底行为：非内建都映射、内建跳过（不能因为配置没写就让功能全灭）
        chk("mod_map_listed_device",
            KeyEngine.shared.takesModMapForSelfTest(vendor: 9999, product: 9999, builtin: false))
        chk("mod_skip_unlisted_device",
            !KeyEngine.shared.takesModMapForSelfTest(vendor: 0, product: 0, builtin: true))
        print("mod_devices=(空) 走兜底：非内建映射、内建跳过")
    }
    // ② 功能键识别：发 F 键要补 fn（否则系统符号快捷键匹配不上，切不了输入法）。
    chk("function_key_recognised",
        KeyTable.isFunctionKey("f17") && KeyTable.isFunctionKey("f1")
            && !KeyTable.isFunctionKey("a") && !KeyTable.isFunctionKey("spacebar"))
    let aloneFnKeys = maOut.split(separator: ",").map(String.init)
        .filter { !$0.isEmpty }
    chk("alone_output_is_function_key", aloneFnKeys.allSatisfy { KeyTable.isFunctionKey($0) })
    print("mod_scope=\(c.modScope) alone_out_fn=\(aloneFnKeys.allSatisfy { KeyTable.isFunctionKey($0) })")

    // ★★★ 2026-10-02（用户：「Ctrl+X Ctrl+V 代替 ⌘X ⌘V，以前能实现，现在不行」）：
    //   「Finder 剪切」必须认得**会被规则改写成 ⌘X/⌘V 的 Ctrl 变体**。
    //   这几条是纯推演判据 —— 不挂 tap、也不看前台条件（自测时前台是终端，
    //   `frontmost_application_unless` 恰好排除终端 ⇒ 条件判定在自测里不可靠）。
    if let xk = KeyTable.codes["x"], let vk = KeyTable.codes["v"] {
        let (rx, rf) = KeyEngine.shared.resolveForSelfTest(xk, [.maskControl])
        chk("resolve_ctrl_x_to_cmd",
            rx == xk && rf.contains(.maskCommand) && !rf.contains(.maskControl))
        let (rv, vf) = KeyEngine.shared.resolveForSelfTest(vk, [.maskControl])
        chk("resolve_ctrl_v_to_cmd",
            rv == vk && vf.contains(.maskCommand) && !vf.contains(.maskControl))
        let (px, pf) = KeyEngine.shared.resolveForSelfTest(xk, [.maskCommand])
        chk("resolve_plain_cmd_x_untouched",
            px == xk && pf.contains(.maskCommand) && !pf.contains(.maskControl))
        print("resolve_ctrl_x=\(rx)/\(rf.rawValue) left_ctrl=\(rf.contains(.maskControl))")
    }
    if let k = c.rules.first(where: { $0.fromKey == "home" })?.id {
        print("sample rule id=\(k)")
    }
    print("perm=\(axPermitted(prompt: false))")
    print("cursor_home=\(KeyTable.codes["home"] ?? 0) cmd=\(KeyTable.flag("command").rawValue)")
    print("keymaptest \(allOK ? "OK" : "FAIL")")
}

// ------------------------------------------------------------------ 窗口
//
// ★★ 2026-09-29（用户第 2 条）：外壳改成「常驻型」工具：
//   ① **关掉窗口不退出**（applicationShouldTerminateAfterLastWindowClosed = false）。
//   ② 新增**菜单栏常驻图标**：一眼看到「服务在不在跑 / 防休眠开没开」，
//      并且能直接打开窗口、进「状态与设置」、刷新状态。
//   ★ 状态只在**菜单拉开时**拉一次（menuWillOpen），**不挂定时器** ——
//     与全工具「能不去轮询就不轮询」的一致原则。
//
// ★★ 2026-09-29 晚（用户报「⌘Q / ⌘W 都不管用」）—— **快捷键与主菜单**
//   【根因】本 app **从来没有主菜单**（`NSApp.mainMenu` 全文一次都没被赋值；本 app 是
//   纯代码建的、没有 nib/xib，AppKit 也不会替你造一个）。而 macOS 的 ⌘Q / ⌘W / ⌘C /
//   ⌘M 全都是**菜单项上的 keyEquivalent**，由 AppKit 拿 `NSApp.mainMenu` 去匹配 ——
//   mainMenu 为 nil 时**没有任何接收者**，按键被静默丢弃（连一声 beep 都没有）。
//   【为什么会漏】上一轮「⌘Q 已被拦下」的实测走的是 `osascript quit`（AppleEvent 直接
//   进 applicationShouldTerminate），**绕过了键盘派发** → 看着是好的，其实键盘那条是死的。
//   教训：验快捷键只能靠**真按键**；退一步也要验「菜单树里的 keyEquivalent」。
//   【现在的键位，2026-09-29 用户二次指定】
//     · **⌘Q = 真的退出**：先把界面服务停掉（POST /api/quit，与页面那颗「关闭服务」
//       同一条路 → 收掉上次开机的一次性残留、**「防休眠」等开关保持不动**），
//       再退出外壳。**不留后台常驻**。
//     · **⌘W = 关掉窗口**，外壳**不**退出（与服务「关闭服务」仍是两件事）。
//     · 其余照 macOS 标准：⌘H 隐藏 / ⌘M 最小化 / ⌘R 重载 / ⌘+ ⌘- ⌘0 缩放 / 编辑四项。

final class Shell: NSObject, NSApplicationDelegate, NSWindowDelegate,
                   WKUIDelegate, WKNavigationDelegate, NSMenuDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var port: Int = DEFAULT_PORT
    var titleObs: NSKeyValueObservation?
    var saveTimer: Timer?
    var lastSaved = ""

    // ---- 菜单栏（状态栏）图标 -------------------------------------------
    var statusItem: NSStatusItem!
    var statusMenu: NSMenu!
    var statHead: NSMenuItem!        // 第一行：服务状态（不可点）
    var statLine: NSMenuItem!        // 第二行：防休眠状态（不可点）
    var menuFetching = false

    // ---- 菜单栏「自定义面板」（2026-09-29 用户第 8 条）-------------------
    //   照 PommeToys：顶上那颗图标点开的**不是系统下拉菜单**，而是一块自定义面板
    //   （它那边是 MenuPanelWindowController + MenuPanelView + Rail/LCD/Lever 自制控件）。
    //   这里的做法：一个无标题栏的 NSPanel，里面装一个 WKWebView，载
    //   `http://127.0.0.1:端口/?panel=1` —— 页面在面板模式下只画
    //   「一排状态灯 + 运行监测那张黑卡 + 一排动作」，跟首页是**同一份 DOM**。
    var panelWin: NSPanel!
    var panelWeb: WKWebView!
    var panelOpen = false
    var outsideMon: Any?             // 点面板外面就收起来（全局鼠标监视）
    var panelH: CGFloat = 430        // 面板内容高度（由页面回报，见 panelCommand）
    let PANEL_W: CGFloat = 360       // 面板宽度：量过，运行监测卡在这个宽度下不折行
    // ★★ 面板页的「预载」状态（2026-09-29 用户第 18 条，见 preloadPanel 的长注释）。
    //   `panelLoaded` 只在校验过的 `didFinishNavigation` 里置 true ——
    //   不要用 `panelWeb.url != nil` 代替：`load()` 一发出 url 就有了，
    //   那时页面还是一片空白，拿它当「能用」会导致点开是白板。
    var panelLoaded = false          // 页面已载完（内容可以看了）
    var panelLoading = false         // 正在载（别重复发 load）
    // ★★ 2026-09-30（第廿八轮 · 用户第 1 条）：「先闪出来一半、马上又补全」。
    //   修法见 showPanel → panelPrepareThen：**内容先就位、窗口后出现**。
    //   `panelPrepareToken` 是「这次 prepare 还算不算数」的凭据 —— 用户连点两下
    //   （开→关→开）时，上一轮的回调与超时都靠它对不上号而作废，不会把刚收起的
    //   面板又拉出来。超时值就是「最多让用户等多久才不管页面」。
    var panelPrepareToken = 0
    // ★★★ 2026-10-01（第五十三轮 · 用户「界面消失不见了 / 圆角也不见了」）。
    //   见本文件末尾「WKWebView 的自我修复」那一节 —— 这两个计数器就是它的账本：
    //     · `panelStuck`   面板页**连续几次**问话没人应（≥2 就强制重载那一页）；
    //     · `mainStuck`    主窗口页无响应的重载次数（封顶，防止崩-重载-再崩的死循环）；
    //     · `pageProcKill` 内容进程被终止的次数（同样封顶）。
    var panelStuck = 0
    var mainStuck = 0
    var pageProcKill = 0
    // 主窗口那一次「你还活着吗」探针的凭据：重新载入会让旧的探针作废。
    var mainProbeToken = 0
    // ★ 超时＝「最多让用户等多久才不管页面」。页面有快照时 prepare 只要 1~2ms，
    //   用不上它；没有快照时要等一次 /api/status（实测 160~280ms），这时宁可
    //   早一点按记忆高度把窗口弹出来（内容随后校正），也不要让人盯着空屏等。
    //   60ms 在人的感知阈值以下 —— 再长就「点了没反应」了。
    let PANEL_PREPARE_TIMEOUT = 0.06
    // ★★★ 2026-10-01（第卅轮 · 用户第 1 条，第廿八轮**没治净**的那最后一层）
    //   「长时间不点之后，点一下还是闪出来一半界面，马上就又显示完全，类似卡一下」。
    //   前两轮治掉的是：① 每次点击重载整页（363~408ms 白屏）；② 按**残缺高度**上屏
    //   （206 / 300 vs 真高 755）。这一层是**绘制本身**，与前两层无关 ——
    //   面板平时是 `orderOut` 收着的，对 WebKit 来说那等于「窗口不可见」：
    //   它的 display link 停摆、渲染进程被 App Nap 降频。我们 `orderFront` 的那一瞬间
    //   窗口就上屏了，而 `#panwrap` 是 360×755、要分好几块 tile 才画得完 →
    //   用户先看到半截，随后才补全。**闲置越久降得越狠**，所以「长时间不点」最明显。
    //   修法一句话：**别把没画完的窗口亮出来**（见 revealPanel / panelFadeIn）。
    var panelFadeToken = 0
    /// 兜底：到点无论如何都亮出来。宁可偶发一帧没画完，也绝不能「点了图标面板不出来」。
    let PANEL_WARMUP_TIMEOUT = 0.12
    /// 淡入时长。60ms 在「看得出是动画」与「不觉得慢」之间 —— 再长就像在等了。
    let PANEL_FADE_DURATION = 0.06

    // ---- 「鼠标」（平滑滚动）---------------------------------------------
    //   ★★ 2026-09-30（用户第 2 条）：配置在 `_鼠标.json`，由界面的 /api/mouse 写；
    //   本外壳**只读**它，并且用 DispatchSource **事件驱动**地盯着（不轮询）。
    var mouseWatch: DispatchSourceFileSystemObject?
    /// ★★ 第卅八轮：上一次实测到的「辅助功能」权限结论（nil ＝ 还没测过）。
    ///   权限看护（`permWatchTick`）靠它判断"结论有没有变"，变了才重挂引擎、才写盘。
    var permSeen: Bool?

    // ---- 「键盘」（键映射 + Finder 剪切增强）-------------------------------
    //   ★★ 2026-10-01（用户第 1 条）：配置在 `_键盘.json`，由界面的 /api/keymap 写；
    //   与鼠标同一套：本外壳只读 + DispatchSource 事件驱动地盯着（不轮询）。
    var keyWatch: DispatchSourceFileSystemObject?

    // ---- 提权桥的「有没有人来敲门」 ---------------------------------------
    //   ★★★ 2026-10-01（自检轮 · 用户第 1 条「不要一直轮巡监测」）：
    //   这里原来是 `Timer(timeInterval: 1.0, repeats: true)` —— **每秒醒一次**，
    //   一天 86400 次，而它要看的事情（`_sudo_req.json` 出现）一年到头也没几回。
    //   现在改成**内核文件事件**：盯着请求文件，谁写了它、谁换了它，内核立刻叫醒我们。
    //   ⚠️ 与 `mouseWatch`/`keyWatch` 同一套 `DispatchSource.makeFileSystemObjectSource`。
    var sudoWatch: DispatchSourceFileSystemObject?
    //   ★★★ 2026-10-01（第四十八轮 · 用户第 2 条「外壳 · 守护自愈同理如上」）：
    //   `guardTimer` / `guardInterval` 两个**常驻定时器字段已删除** ——
    //   守护自愈整体搬去 `wb_guard.py`（launchd 按事件拉起、跑完即退），
    //   外壳里不再留任何与它相关的定时器。详见 `applicationDidFinishLaunching`
    //   里那段说明与 `guardFix()` 的新实现。

    func applicationDidFinishLaunching(_ n: Notification) {
        let conf = readConf()
        port = confInt(conf, "port") ?? DEFAULT_PORT
        // ★ 面板高度用**上次记下的**（2026-09-29 用户第 18 条）。
        //   不读它的话，启动后**第一次**点图标会先按默认 430 摆，等内容报上来再跳到
        //   真高度 —— 日志里实测见过 430 → 667 那一跳，正是「弹出来卡一下」的一部分
        //   （窗口先以矮的高度出现、再把内容挤出来）。写入见 panelCommand 的 height 分支。
        if let ph = confInt(conf, "panelH"), ph > 120 { panelH = CGFloat(ph) }
        logLine("=== 外壳启动（tool=\(TOOL_DIR)，port=\(port)，面板高=\(Int(panelH))）===")

        // ★★ 提权桥（常驻侧）：**有人往请求文件里写字**才醒，平时一次都不醒。
        //   为什么要常驻实例做这个：用户点「合盖后保持唤醒」时，界面服务在
        //   **seatbelt 沙箱**里（exec sudo 被内核拒、launchctl 也不可用），
        //   而本 app 由 LaunchServices/launchd 拉起、在**干净上下文** ——
        //   由它代跑那一条 `sudo -n pmset`，就不会再弹密码框。
        //
        //   ★★★ 2026-10-01（自检轮）：原来这里是 `Timer(1.0)` 常驻轮询 ——
        //   每秒醒一次、一天 86400 次，只为了等一个一年没几回的请求。用户原话：
        //   「不要又放一个监测，每 5 秒监测一次我非常反感这种一直挂载着一个监测的后台」。
        //   现在改成 **DispatchSource 文件事件**（与 `mouseWatch`/`keyWatch` 同一套）：
        //   请求文件的 inode 一被写、一被替换，内核立刻把这一觉叫醒 ——
        //   **空闲时零唤醒、零 CPU**，而响应比原来还快（毫秒级，不再最多等 1 秒）。
        //   ⚠️ 原来那段「必须挂 .common，否则面板一开就停摆」的坑在这里自动消失：
        //      DispatchSource 挂在主队列上，**不吃 runloop mode**，面板开着照样收事件。
        armSudoWatch()

        // ★★★ 2026-10-01（第四十八轮 · 用户第 2 条「外壳 · 守护自愈同理如上」）：
        //   **外壳里的常驻自愈定时器整个删掉了。** 用户原话是「为什么需要轮询？
        //   ……改为事件驱动」；它原来是「一次性定时器 + 回调里再排下一拍」，
        //   60 秒起、每轮全好 ×1.5、封顶 10 分钟，一天要给三条线各 spawn 一次
        //   `launchctl print`（每天 4300 次进程创建）。
        //
        //   现在自愈住在 `wb_guard.py`（**跑完即退的一次性脚本**），由一条新的
        //   LaunchAgent `com.wb.workbuddy-guard` 按**事件**拉起：
        //     ① 登录 / 开机        → `RunAtLoad`
        //     ② LaunchAgents 变了   → `WatchPaths`（plist 被加 / 删 / 改）
        //     ③ 有人真去部署守护     → 桥（`case "guard"` → `guardFix()`），**即刻**
        //   只剩一条**每小时**的 `StartInterval` 兜底：因为「心跳看护」要判的是
        //   **「什么都没发生」**（周期型线僵住 = 一段时间内没有事件），这类判断
        //   本质上需要一个时间基准 —— 交给 launchd（零常驻进程、一小时一次），
        //   比原来让外壳每 60 秒醒一次便宜 60 倍。
        //   ⚠️ 部署：`wb_guard.py --install` 写 plist → 走一次桥把它装进 launchd。
        //
        //   ★ 外壳每次起来只做**一次性**的「保证它装着」（不是定时器）：
        //     plist 不在就写出来、没装载就 bootstrap。之后交给 launchd 按事件跑。
        // ★ 2026-10-11（拆家）：原来这里 3 秒后调 ensureGuardAgent()
        //   （装 WorkBuddy 同步线的自愈 LaunchAgent）—— 那是任务管家的事，已删。

        // ★★★ 权限实测：**只挂在事件上，绝不挂定时器**（2026-10-01 第卅九轮 · 用户明确要求）。
        //   原来这里是 `Timer(5.0)` 常驻轮询。用户原话：「不要又放一个监测，
        //   每 5 秒监测一次我非常反感这种一直挂载着一个监测的后台」—— 这个意见是对的：
        //   要等的那个状态变化**只能由用户亲手触发**，为它让一个进程每 5 秒醒一次，
        //   代价与收益完全不成比例（而且它是那种"看不出在干活"的后台）。
        //   改成四条**事件驱动**的路，覆盖全部真实场景、平时**一次唤醒都没有**：
        //     ① 启动时（下面 `applyMouseConf/applyKeyConf(prompt:false)` 各测一次）；
        //     ② 用户改了配置（`_鼠标.json` / `_键盘.json` 的文件监视 —— 内核事件，不是轮询）；
        //     ③ **每次打开面板**（`showPanel()`）—— 用户从系统设置勾完回来第一件事就是点开它；
        //     ④ **每次切 App**（`actAppSwitched`）—— 从系统设置勾完**切回来**必然经过这里。
        //   ④ 是这一轮新加的：它本身就是"用户去勾权限、再回来"那个动作的化身。

        // ★★ 主菜单**必须在开窗之前建好** —— macOS 的 ⌘Q / ⌘W 全靠它派发。
        //    （漏了它 = 所有 ⌘ 键静默失效，2026-09-29 踩过。）
        buildMainMenu()

        // ★★ 平滑滚动（用户第 2 条）：读 `_鼠标.json` 把引擎挂上，并**盯着这个文件**。
        //   ⚠️ 两个自测通道下不挂：那两条通道的输出要按行解析，多一行日志就毁了判据；
        //      而且它们本来也不该去动用户的鼠标输入。
        if ProcessInfo.processInfo.environment["WB_SHELL_KEYTEST"] == nil,
           ProcessInfo.processInfo.environment["WB_SHELL_PANELTEST"] == nil {
            // 启动时**静默**检查权限（prompt=false）—— 每次开窗口都弹授权框太烦。
            // 用户真去点「平滑滚动」开关时，文件监视那条路才带 prompt=true。
            applyMouseConf(prompt: false)
            watchMouseConf()
            // ★★ 键映射（用户第 1 条）：与鼠标同一套 —— 启动静默读一次 + 盯配置文件。
            //    ⚠️ 配置不存在时 `applyKeyConf` 会读到「全关」的空配置，**不会**顺手挂引擎
            //       （键映射的默认值必须是关的：不能因为用户点开一次界面就改他的键盘）。
            applyKeyConf(prompt: false)
            watchKeyConf()
            // 从「系统设置」里勾完权限回来、app 重新激活时再试一次（事件驱动，不轮询）。
            NotificationCenter.default.addObserver(
                self, selector: #selector(actMouseRetry),
                name: NSApplication.didBecomeActiveNotification, object: nil)
            NotificationCenter.default.addObserver(
                self, selector: #selector(actKeyRetry),
                name: NSApplication.didBecomeActiveNotification, object: nil)
        }

        let ok = ensureService(port: port)

        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        // ★★★ 2026-10-01（第四十七轮 · 用户第 1 条）：
        //   **主窗口也挂上 `shell` 通道**（以前只有面板那个 webView 挂）。
        //   为什么：用户要求把「关闭服务」和「退出」合并成同一个按钮，而且
        //   「应该直接退出程序，包括菜单栏的程序图标」—— 顶栏那颗「退出」必须
        //   叫得动外壳去 terminate。没有这条通道时，主窗口里
        //   `window.webkit.messageHandlers.shell` 是 undefined，点了只会弹一句
        //   「这一条要装在『WorkBuddy任务管家』里点菜单栏图标才好使」。
        //   ⚠️ `panelCommand` 对这个来源只会收到 `quit`（页面按 IS_SHELL 分流），
        //      其余分支即便误入也无害（height 会照常量、openWindow 会照常前置窗口）。
        cfg.userContentController.add(PanelBridge(shell: self), name: "shell")
        webView = WKWebView(frame: .zero, configuration: cfg)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.allowsMagnification = true
        webView.setValue(false, forKey: "drawsBackground")

        // 用 _界面.json 里记的尺寸/位置还原上次的窗口（该文件由页面上报、wb_ui 写入）。
        // ★ 与 Windows 端同一套数据，所以两边「调过的窗口大小」语义一致。
        //
        // ★★ 2026-09-30（第廿七轮 · 用户第 1 条）：「记住现在界面的大小，让它保持固定大小
        //   就不能调整了」。于是这一版起窗口**定尺**：
        //     · `_界面.json` 里的 winW/winH 就是「现在的大小」，照旧读出来当**唯一**尺寸；
        //     · 下面 styleMask 去掉 `.resizable`，并把 minSize/maxSize 都钉成它 ——
        //       右下角拖不动、绿色按钮也不缩放，连代码里 setFrame 都改不动它；
        //     · 位置（winX/winY）**照旧可动**：固定的是大小，不是位置。
        //   ⚠️ 默认值从 1180×820 改成 617×689 —— 就是用户截图那一刻的实际尺寸。
        //      只在 _界面.json 缺失/不合法时才会用到它。
        //   ⚠️ `_界面.json` 里 winW/winH 是**内容区**尺寸（contentRect），
        //      而 NSWindow 的 minSize/maxSize 收的是**整窗**（含标题栏）——
        //      下面必须用 frameRect(forContentRect:) 换算，别直接把 w/h 塞进去。
        var frame = NSRect(x: 0, y: 0, width: 617, height: 689)
        var w = confInt(conf, "winW") ?? 0
        var h = confInt(conf, "winH") ?? 0
        if !(520...8000 ~= w) || !(420...6000 ~= h) { w = 0; h = 0 }
        if w > 0 && h > 0 { frame.size = NSSize(width: w, height: h) }
        if w > 0, let x = confInt(conf, "winX"), let y = confInt(conf, "winY"),
           (-20000...20000 ~= x) && (-20000...20000 ~= y) {
            // 页面报的 y 是「从主屏顶边往下」；Cocoa 的窗口原点是「从左下角往上」。
            let screenH = NSScreen.screens.first?.frame.height ?? 900
            frame.origin = NSPoint(x: CGFloat(x), y: screenH - CGFloat(y) - frame.height)
        } else {
            frame.origin = NSPoint(x: 0, y: 0)
        }

        window = NSWindow(contentRect: frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = APP_TITLE
        // ★ 可缩放（2026-10-09 用户要求）：拖右下角 / 绿色按钮都能调大小；
        //   尺寸由 windowDidResize → saveGeometry 记进 _界面.json，下次打开按它还原。
        //   minSize 给个下限（别缩到没法用），**不设上限**。
        let fullSize = window.frameRect(forContentRect: frame).size
        let minFull = window.frameRect(forContentRect: NSRect(x: 0, y: 0, width: 520, height: 420)).size
        window.minSize = minFull
        window.setContentSize(frame.size)
        window.contentView = webView
        window.center()
        if w > 0 && h > 0 {
            // ★ setFrame 收的是**整窗**矩形（含标题栏），而上面的 frame 是 contentRect。
            //   直接 window.setFrame(frame) 会矮掉一个标题栏，还原不准、再存又漂一次。
            //   用 frameRect(forContentRect:) 换算过去，与 saveGeometry 里的
            //   contentRect(forFrameRect:) 严格互逆。
            //   ★ 定尺之后这一句只在**位置**上起作用（尺寸已被 min/max 夹住）。
            window.setFrame(window.frameRect(forContentRect: frame), display: true)
        }
        window.isReleasedWhenClosed = false
        logLine("窗口：内容 \(Int(frame.width))x\(Int(frame.height))"
                + "（可缩放，minH=420，尺寸会记住）")

        // 页面改标题时同步到标题栏
        // ⚠ 页面 <title> 本身就是「WorkBuddy 任务管家」，直接拼 "... — APP_TITLE"
        //    会把工具名显示两遍。页面标题已含工具名就原样用，不再叠一次。
        titleObs = webView.observe(\.title, options: [.new]) { [weak self] wv, _ in
            guard let self = self else { return }
            let t = (wv.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if t.isEmpty {
                self.window.title = APP_TITLE
            } else if t.contains(APP_TITLE) {
                self.window.title = t
            } else {
                self.window.title = "\(t) — \(APP_TITLE)"
            }
        }

        // ★ 自测模式（WB_SHELL_KEYTEST / WB_SHELL_PANELTEST）跑在**后台**：
        //   不前置、不抢焦点 —— 它只是拿这个窗口当事件目标，跑完自己就退。
        // ★★★ 2026-10-02（第六十六轮 · 用户第 1 条）：**登录自启（`--silent`）同样不上屏**。
        //   用户原话：「开机时自动打开，但是不要弹出来窗口。」
        //   窗口**照建、页面照载**（菜单栏图标、提权桥、面板预载全都照常工作），
        //   只是不走 `makeKeyAndOrderFront` / `activate` —— 既不抢前台，也不会
        //   「排在后面露一角」。用户点 Dock 图标或面板里的「打开界面」时，
        //   `showWindowEnsuringService()` 里那份 orderFront 照旧把它端出来。
        if SILENT_LAUNCH {
            window.orderOut(nil)
            logLine("静默启动（--silent）：窗口不上屏、不抢前台")
        } else if ProcessInfo.processInfo.environment["WB_SHELL_KEYTEST"] == nil,
                  ProcessInfo.processInfo.environment["WB_SHELL_PANELTEST"] == nil {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        } else {
            window.orderBack(nil)
        }

        // ★ 还原完成之后才挂 delegate：否则启动阶段的 center()/setFrame 会先触发
        //   一次「保存」，把还没放正的默认几何写进 _界面.json。
        window.delegate = self
        // 挂上之后**立刻主动存一次**：光靠 windowDidMove/Resize 的话，
        // 用户不拖窗口就永远不触发 → 文件一直不存在（2026-09-27 实测踩到）。
        saveGeometry()

        logLine("→ 准备挂菜单栏图标")
        buildStatusItem()          // ★ 菜单栏常驻图标（用户第 2 条）
        logLine("→ 菜单栏图标步骤结束")

        if ok {
            // `shell=1` 告诉页面「你正跑在外壳窗口里」——
            // 页面据此**不再**上报窗口几何（见 _ui.html 的 reportWin）。
            // 原因：页面只能拿到 window.outerWidth / screenX，而 WKWebView 里这两个
            // 取不到值（实测从来没上报成功过，所以「记住窗口大小」一直没生效）；
            // 外壳手里就是真实 NSWindow，由下面 windowDidMove/Resize 负责记录最准。
            let url = URL(string: "http://127.0.0.1:\(port)/?appwin=1&shell=1")!
            logLine("加载 \(url.absoluteString)")
            webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData,
                                    timeoutInterval: 20))
            // ★★ 顺带把**菜单栏面板**也预载上（用户第 18 条：「点图标弹出来会卡一下」，
            //   见 preloadPanel 的实测数字）。延后 1.5 秒是为了别跟主窗口抢
            //   WebKit 的首次初始化 —— 主窗口才是用户先看到的那一个。
            //   ⚠️ 自测通道（WB_SHELL_PANELTEST）下**也照常预载**（第廿八轮改）。
            //      以前这里跳过预载，于是自测只能验到「点击时页面还没载起来」那条
            //      **兜底路**（`panelPrepareThen` 直接 fire(0)）—— 而真人 99% 的情形是
            //      **预载好的**，偏偏装着新逻辑的那条主路一次都没被测过。
            //      现在 `panelTest()` 改成「等预载就绪再弹」，就不会再有「重新载一遍」的问题。
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                self?.preloadPanel()
            }
        } else {
            logLine("✗ 服务没起来，显示提示页")
            showFailPage()
        }

        // ★ 快捷键自测通道（见文件末尾 keyTest / probeKeys 的注释）。
        //   `WB_SHELL_KEYTEST=probe`    只**静态核对**每个键位有没有落在菜单项上；
        //   `WB_SHELL_KEYTEST=w,h,r`    真的**派发**这几个键（⌘W 只收窗口，不会退）；
        //   `WB_SHELL_KEYTEST=q`        真的派发 ⌘Q（会真退出 + 停界面服务）。
        //   两种模式跑完都自行 exit(0)，**不碰界面服务**（除非你测的就是 q）。
        if let t = ProcessInfo.processInfo.environment["WB_SHELL_KEYTEST"] {
            let mode = t.trimmingCharacters(in: .whitespaces)
            if mode.lowercased() == "probe" {
                logLine("快捷键自测(probe)：" + probeKeys())
            } else {
                for c in mode.split(separator: ",") {
                    keyTest(String(c))
                }
            }
            logLine("快捷键自测结束 → 自行退出")
            exit(0)
        }

        // ★★ 菜单栏面板自测通道（2026-09-29 新增，第 10 套回归用它）。
        //   `WB_SHELL_PANELTEST=1` → 把面板弹出来，等页面渲染完，报三件事：
        //     ① 面板 frame（要在菜单栏图标下方、且夹在屏幕里）；
        //     ② 面板里到底画出了几个运行监测开关（`.sw[data-key]` 的个数）；
        //     ③ 面板页里那排动作按钮在不在。
        //   然后 exit(0)。**不碰界面服务**（服务本来没跑也不会去拉）。
        if ProcessInfo.processInfo.environment["WB_SHELL_PANELTEST"] != nil {
            panelTest()
            return
        }
    }

    /// 面板自测：给回归脚本一个不依赖截图/权限的判决依据。
    func panelTest() {
        logLine("面板自测：开始")
        // ⚠️ 这里用 if 而不是 guard：guard 的 else 分支必须「离开作用域」，
        //   而这里只是想「顺手拉一下服务、然后继续」—— 用 guard 会编译不过
        //   （`swiftc -typecheck` 不报，`-O` 编译会报 "'guard' body must not fall through"）。
        if !portAlive(port) {
            logLine("面板自测：✗ 界面服务没在跑，先跑一次 ensureService")
            _ = ensureService(port: port)
        }
        // ★★ 2026-09-30（第廿八轮）：**等页面预载好再弹**。
        //   这才是用户真实碰到的情形 —— 面板是外壳启动时预载的，点图标时它早就载好了。
        //   以前这里一上来就 showPanel()，走的是「点击时页面还没载起来」那条**兜底路**
        //   （fire(0) → 直接按记忆高度弹），于是自测**永远验不到**「上屏前量真高」这条主路
        //   —— 而那正是本轮修的东西。
        //   ⚠️ 等不到（服务没起 / 页面载不动）也必须继续：自测是诊断，不能挂死。
        waitLoadedThenShow(tries: 0)
        // 页面要拉 /api/status 再画开关，给足时间。
        // ★ 第廿八轮：这个时钟是**从 panelTest() 起算**的，而"弹出"本身要等预载
        //   （最多 5 秒 + 0.4 秒缓冲）—— 原来的 4 秒会在窗口**还没弹出来**的时候就去量
        //   frame，把好结果判成坏的。给到 7 秒（正常情形下 ~2.9 秒就弹出来了，仍然富余）。
        DispatchQueue.main.asyncAfter(deadline: .now() + 7.0) { [weak self] in
            guard let self = self else { return }
            let f = self.panelWin?.frame ?? .zero
            let vis = (NSScreen.main?.visibleFrame ?? .zero)
            let onScreen = vis.contains(f) || (f.minX >= vis.minX - 1 && f.maxX <= vis.maxX + 1)
            var anchorOK = false
            if let bw = self.statusItem?.button?.window {
                anchorOK = f.maxY <= bw.frame.minY + 1          // 面板上沿不高于图标下沿
            }
            logLine("面板自测：frame=\(Int(f.width))x\(Int(f.height))"
                         + " @\(Int(f.minX)),\(Int(f.minY))"
                         + " 可见=\(self.panelOpen) 在屏内=\(onScreen) 贴图标下沿=\(anchorOK)")
            self.panelWeb?.evaluateJavaScript(
                "(function(){"
                + "var n=document.querySelectorAll('#panbox .sw[data-key]').length;"
                + "var f=document.querySelectorAll('#panfoot .pbtn, #panhead #pan-open').length;"
                + "var pg=!!document.getElementById('power-card');"
                + "var bg=getComputedStyle(document.body).backgroundColor;"
                + "return JSON.stringify({sw:n,btns:f,card:pg,bodyBg:bg,"
                + "head:(document.getElementById('panhead')||{}).children?document.getElementById('panhead').children.length:0});"
                + "})()"
            ) { res, err in
                if let e = err {
                    logLine("面板自测：✗ 取页面内容失败 \(e.localizedDescription)")
                } else {
                    logLine("面板自测：页面=\(res ?? "nil")")
                }
                logLine("面板自测结束 → 自行退出")
                exit(0)
            }
        }
    }

    /// 自测用：等面板页**预载就绪**（最多约 5 秒）再弹 —— 走的是和真人**一模一样**的那条路
    /// （`showPanel` → `panelPrepareThen` → 页面的 `__panelPrepare` 量真高 → 才上屏）。
    /// 为什么非要等：不等的话，页面还没载起来，`panelPrepareThen` 只能走 `fire(0)` 兜底，
    /// 于是自测**永远验不到**本轮新加的那条主路（等于没测）。
    /// ⚠️ 等不到也要照弹：自测是诊断，不能因为服务没起就挂死在那儿。
    func waitLoadedThenShow(tries: Int) {
        if panelLoaded {
            // 再给 0.4 秒：让页面自己那次 /api/status 回来（PANEL_LAST 就位 → 走快路，
            // 弹出只剩 1~2ms）。少了这一口，判断就会落在"还没快照"的慢路上。
            logLine("面板自测：页面已预载就绪，等首份数据回来再弹")
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
                self?.showPanel()
            }
            return
        }
        if tries >= 100 {                    // 100 × 50ms = 5 秒
            logLine("面板自测：页面 5 秒没载起来 → 按兜底路弹（自测继续）")
            showPanel()
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
            self?.waitLoadedThenShow(tries: tries + 1)
        }
    }

    // ---- 记住窗口几何（外壳负责，2026-09-27 改）------------------------------
    // 拖动标题栏 / 拉边框会连续触发几十次，所以先抖 0.6 秒再写盘，别把盘写爆。
    // ---------------------------------------------------------------- 「鼠标」
    //
    // 两条入口：① 启动时读一次（静默）；② 文件监视（用户改了配置，带 prompt）。
    // 每次读完都把「引擎实况 + 权限」写回 `_界面.json` —— 服务端（wb_ui）读它，
    // 下行到界面上，用户才知道该不该去勾「辅助功能」。
    /// `why`：日志里那句「配置重读（……）」的括注。不传就按 prompt 推
    /// （true＝用户改了配置 / false＝启动时那次）。第卅八轮加了「权限看护」这一路，
    /// 三种来路必须分得清 —— 不然日志里冒出一行「启动」，会让人误以为 app 重启过。
    func applyMouseConf(prompt: Bool, why: String? = nil) {
        let c = readMouseConf()
        SmoothEngine.shared.apply(smooth: c.smooth, reverse: c.reverse,
                                  speed: c.speed, prompt: prompt)
        // ★ 第卅七轮（2026-10-01 用户第 3 条）：鼠标栏目下第二个功能 —— 点 Dock 图标最小化。
        //   与平滑滚动**同一份配置、同一套权限、同一条文件监视**，只是各挂各的事件 tap。
        DockMinEngine.shared.apply(enabled: c.dockMin, prompt: prompt)
        let perm = axPermitted(prompt: false)
        let engine = SmoothEngine.shared.running
        let dmEngine = DockMinEngine.shared.running
        // ⚠️ 只在「权限 / 两个引擎」这三个结论上跟上次不一样时才写 `_界面.json` ——
        //    文件监视每次改动都会走到这里，无脑写会把窗口几何那些键反复重写。
        var conf = readConf()
        // ★ 顺路清掉改名前的旧键（scrollPerm / scrollEngine）；清过也要写一次盘把它落定。
        let pruned = pruneLegacyConfKeys(&conf)
        let changed = (conf["mousePerm"] as? Bool) != perm
                   || (conf["mouseEngine"] as? Bool) != engine
                   || (conf["dockMinEngine"] as? Bool) != dmEngine
        if changed || pruned {
            conf["mousePerm"] = perm
            conf["mouseEngine"] = engine
            conf["dockMinEngine"] = dmEngine
            writeConf(conf)
        }
        logLine("[鼠标] 配置重读（\(why ?? (prompt ? "用户改了" : "启动"))）：平滑=\(c.smooth)"
                + " 反转=\(c.reverse) 速度=\(c.speed) 点Dock最小化=\(c.dockMin)"
                + " 权限=\(perm) 平滑引擎=\(engine ? "开" : "关")"
                + " Dock最小化引擎=\(dmEngine ? "开" : "关")")
    }

    /// 盯着 `_鼠标.json`：改了就读、重挂引擎。**不轮询**（DispatchSource + kqueue）。
    func watchMouseConf() {
        ensureMouseConfFile()
        let p = mouseConfPath()
        let fd = open(p, O_EVTONLY)
        if fd < 0 {
            logLine("[鼠标] ✗ 盯不上配置（open 失败，errno=\(errno)）：\(p)")
            return
        }
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .delete, .rename, .extend, .attrib],
            queue: .main)
        src.setEventHandler { [weak self] in
            guard let self = self else { return }
            let ev = self.mouseWatch?.data ?? []
            // ★ prompt=true：走到这条路说明是**有人动了配置**（多半就是用户点了开关），
            //   这时才值得弹一次「需要辅助功能权限」的系统框。
            self.applyMouseConf(prompt: true)
            if ev.contains(.delete) || ev.contains(.rename) {
                // inode 换了（文件被删/被替换）→ 原来那个 fd 已经盯不住，重挂一次。
                // ⚠️ 界面的写入是**就地覆盖**（不 rename），正常走不到这里；
                //    这是兜住「有人手工删了文件再新建」这种情况。
                logLine("[鼠标] 配置被换掉了，重挂监视")
                self.mouseWatch?.cancel()
                self.mouseWatch = nil
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
                    self?.watchMouseConf()
                }
            }
        }
        src.setCancelHandler { close(fd) }
        src.resume()
        mouseWatch = src
    }

    /// app 重新激活时（多半是刚从「系统设置」勾完权限回来）再试一次开引擎。
    @objc func actMouseRetry() {
        // ★ 第卅九轮：先静默实测一次权限（结论一变，它顺带把两个引擎都重挂），
        //   再补一次「配置开着、引擎却没起来」的兜底。
        permWatchTick()
        let c = readMouseConf()
        if c.smooth && !SmoothEngine.shared.running {
            applyMouseConf(prompt: false)
        }
    }

    // ================================================================ 提权桥
    //   ★★★ 2026-10-01（自检轮 · 用户第 1 条）：**事件驱动，不再每秒轮询**。
    //   三个函数：
    //     · `armSudoWatch()`      挂内核文件事件（写/删/换名/改属性都叫醒）
    //     · `handleSudoRequestIfAny()` 真正干活的（下面全局区，冷启实例也用它）
    //     · `sudoReqPending()`    请求文件里**还有没有活**（判据从「文件在不在」
    //                             改成「里面还有没有 kind」——见下面长注释）
    //
    //   为什么改成事件：原来那个 `Timer(1.0)` 一天醒 86400 次，只为等一个
    //   一年没几回的请求；改成内核事件后**空闲时一次都不醒**，响应还更快。

    /// 请求文件不存在就补一个空的 —— 事件源要靠它开 fd，没文件就盯不上。
    func ensureSudoReqFile() {
        let fm = FileManager.default
        if fm.fileExists(atPath: SUDO_REQ) { return }
        // 空请求 = `{}`（没有 kind → 不算待办）。就地写，别用原子替换：
        // 原子替换会换 inode，等于自己把自己的监视弄失效。
        try? Data("{}".utf8).write(to: URL(fileURLWithPath: SUDO_REQ))
    }

    /// 挂上「请求文件被写/被换」的内核事件。失败就 5 秒后再试一次（不无限重试）。
    func armSudoWatch() {
        ensureSudoReqFile()
        let fd = open(SUDO_REQ, O_EVTONLY)
        if fd < 0 {
            logLine("[提权桥] ✗ 盯不上请求文件（open 失败 errno=\(errno)）：\(SUDO_REQ)")
            DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) { [weak self] in
                self?.armSudoWatch()
            }
            return
        }
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .delete, .rename, .extend, .attrib],
            queue: .main)
        src.setEventHandler { [weak self] in
            guard let self = self else { return }
            let ev = self.sudoWatch?.data ?? []
            // 先干活：即便这一下是「被替换」的事件，新内容也已经就位了。
            _ = handleSudoRequestIfAny()
            if ev.contains(.delete) || ev.contains(.rename) {
                // 调用方是**原子写**（先写 .tmp 再 os.replace）→ 目标 inode 被换掉，
                // 原来那个 fd 已经盯不住新文件了，必须重挂一次。
                // ⚠️ 这里**不能**延迟太久：重挂之前来的请求只能靠冷启实例接。
                self.sudoWatch?.cancel()
                self.sudoWatch = nil
                self.armSudoWatch()
            }
        }
        src.setCancelHandler { close(fd) }
        src.resume()
        sudoWatch = src
    }

    // ---------------------------------------------------------------- 权限看护
    //
    /// 自测通道下**不碰引擎**：那些通道的输出要按行解析，多一行日志就毁了判据；
    /// 而且它们本来也不该去动用户的鼠标输入。
    var engineGagged: Bool {
        let e = ProcessInfo.processInfo.environment
        return e["WB_SHELL_KEYTEST"] != nil || e["WB_SHELL_PANELTEST"] != nil
    }

    /// 静默实测一次「辅助功能」权限；**结论变了**才重挂引擎并写盘。
    /// 返回：这次有没有发现变化（`force=true` 时无条件走一遍流程）。
    ///
    /// ★★ 2026-10-01（第卅八轮）为什么单开这一条：
    ///   原来外壳只在「启动」与「配置文件被改」两个时刻测权限。用户去系统设置里
    ///   把开关勾上之后，**没有任何东西会重测** —— 引擎不会自己起来，
    ///   `_界面.json` 里的 `mousePerm` / `keyPerm` 也不会变，界面那条引导条就一直
    ///   挂着。于是「形式问题」（勾了还提示）与「实质问题」（到底生效没有）
    ///   同时无解：用户没法验证自己那一下勾到底管不管用。
    ///   现在**在事件上**测、一变就同步 → 界面自己变绿、功能自己开始工作。
    ///
    /// ★★ 2026-10-01（第卅九轮）：**已经没有 5 秒定时器了**。用户明确反感「一直挂着
    ///   一个监测的后台」，而且这意见是对的 —— 它要等的那个状态变化只有用户能触发。
    ///   改成四条事件路：启动 / 配置变化 / 打开面板（`showPanel`）/ 切 App
    ///   （`actAppSwitched`）。**平时一次唤醒都没有**；而用户真去勾权限时，
    ///   无论他是「切回来」还是「点开面板」，都必然撞上其中一条。
    ///
    /// 为什么不担心开销：`AXIsProcessTrustedWithOptions(prompt:false)` 只是读内核里
    /// 那份 TCC 状态；而且**没变化时什么也不做**（不写盘、不重挂引擎、不打日志）。
    @discardableResult
    func permWatchTick(force: Bool = false) -> Bool {
        if engineGagged { return false }
        let p = axPermitted(prompt: false)          // ⚠️ 绝不 prompt：不许弹系统框
        if !force, let last = permSeen, last == p { return false }
        let first = (permSeen == nil)
        permSeen = p
        // 两个栏目共用同一份 TCC 授权，所以一起重挂（内部各自判"结论有没有变"）。
        let why = first ? "权限实测·首次" : "权限实测·有变化"
        applyMouseConf(prompt: false, why: why)
        applyKeyConf(prompt: false, why: why)
        if first {
            // 打一行留痕：用户排障时能拿它证明"这个实测真的跑过"，
            // 而不是又一处"看着像在工作"的东西。
            // ★ 特意写明「无后台轮询」—— 这是用户第卅九轮明确要的姿势。
            logLine("[权限] 实测：\(p ? "已通过" : "未通过")"
                    + "（无后台轮询；结论一变就重挂引擎并同步界面，不用重启）")
        } else {
            logLine("[权限] 实测变化 → \(p ? "已通过" : "未通过")"
                    + "（已按新状态重挂引擎；界面 3 秒内自动跟上）")
        }
        return true
    }

    // ---------------------------------------------------------------- 「键盘」
    //
    // 与「鼠标」同一套：① 启动时读一次（静默）；② 文件监视（用户改了配置，带 prompt）。
    // 每次读完都把「引擎实况 + 权限」写回 `_界面.json` —— 服务端读它、下行到界面，
    // 用户才知道该不该去勾「辅助功能」。
    func applyKeyConf(prompt: Bool, why: String? = nil) {
        let c = readKeyConf()
        KeyEngine.shared.apply(c, prompt: prompt)
        let perm = axPermitted(prompt: false)
        let engine = KeyEngine.shared.running
        var conf = readConf()
        var changed = false
        if (conf["keyPerm"] as? Bool) != perm { conf["keyPerm"] = perm; changed = true }
        if (conf["keyEngine"] as? Bool) != engine { conf["keyEngine"] = engine; changed = true }
        if conf["keyStatus"] as? String != KeyEngine.shared.statusText {
            // 「键盘」页要显示引擎实况（跑着几条规则、Finder 剪切开没开）——
            // 与鼠标那边只回两个布尔不同，这里多回一行**人话**。
            conf["keyStatus"] = KeyEngine.shared.statusText
            changed = true
        }
        if changed { writeConf(conf) }
        logLine("[键盘] 配置重读（\(why ?? (prompt ? "用户改了" : "启动"))）：总开关=\(c.enabled)"
                + " 修饰键=\(c.modEnabled) 规则=\(c.rules.count)"
                + " Finder=\(c.finderEnabled) 权限=\(perm) 引擎=\(engine ? "开" : "关")")
    }

    /// 盯着 `_键盘.json`：改了就读、重挂引擎。**不轮询**（DispatchSource + kqueue）。
    func watchKeyConf() {
        let p = keyConfPath()
        if !FileManager.default.fileExists(atPath: p) { return }
        let fd = open(p, O_EVTONLY)
        if fd < 0 {
            logLine("[键盘] ✗ 盯不上配置（open 失败，errno=\(errno)）：\(p)")
            return
        }
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .delete, .rename, .extend, .attrib],
            queue: .main)
        src.setEventHandler { [weak self] in
            guard let self = self else { return }
            let ev = self.keyWatch?.data ?? []
            self.applyKeyConf(prompt: true)
            if ev.contains(.delete) || ev.contains(.rename) {
                // ⚠️ 与 `_鼠标.json` 不同：键盘配置是 wb_keymap 用 **原子写**
                //    （先 .tmp 再 os.replace）落盘的 → inode 会换，
                //    所以「重挂」这条路是**正常路径**，不是兜底。
                logLine("[键盘] 配置 inode 变了（原子写），重挂监视")
                self.keyWatch?.cancel()
                self.keyWatch = nil
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
                    self?.watchKeyConf()
                }
            }
        }
        src.setCancelHandler { close(fd) }
        src.resume()
        keyWatch = src
    }

    /// app 重新激活时（多半是刚从「系统设置」勾完权限回来）再试一次开引擎。
    @objc func actKeyRetry() {
        permWatchTick()          // 同上：事件驱动的那次静默实测（两条栏目共用一份授权）
        let c = readKeyConf()
        if c.enabled && !KeyEngine.shared.running {
            applyKeyConf(prompt: false)
        }
    }
    /// 键位捕获用：把「按了哪个键」翻成 Karabiner 的 key_code 名，回给页面。
    /// 页面那边点「改键」→ 进捕获态 → 用户按一下 → 调这里拿名字。
    func keyNameForCode(_ code: Int) -> String {
        return KeyTableCodes.reverse(CGKeyCode(code))
    }

    func windowDidMove(_ n: Notification) { scheduleSaveGeometry() }
    func windowDidResize(_ n: Notification) { scheduleSaveGeometry() }

    func windowDidResignKey(_ n: Notification) { }

    func scheduleSaveGeometry() {
        saveTimer?.invalidate()
        saveTimer = Timer.scheduledTimer(withTimeInterval: 0.6, repeats: false) {
            [weak self] _ in self?.saveGeometry()
        }
    }

    func saveGeometry() {
        guard let win = window else { return }
        // ★ 一定要用 contentRect，不能用 window.frame ——
        //   NSWindow(frame:) 与 NSWindow(contentRect:) 差一个**标题栏高度**（28px 左右）。
        //   还原时给的是 contentRect，保存时若读 frame，y 就会每次漂移 ~28px
        //   （2026-09-27 实测：写 200 → 读回 164，再开一次会继续漂）。
        //   两边都用 contentRect 才能严格互逆：
        //     还原  cr.origin.y = screenH - y - h
        //     保存  y = screenH - cr.origin.y - h   ⇒ 恰好回到 y
        let cr = win.contentRect(forFrameRect: win.frame)
        let screenH = NSScreen.screens.first?.frame.height ?? 900
        let y = Int(screenH - cr.origin.y - cr.height)
        let key = "\(Int(cr.width))x\(Int(cr.height))@\(Int(cr.origin.x)),\(y)"
        if key == lastSaved { return }
        lastSaved = key
        var conf = readConf()
        conf["winW"] = Int(cr.width)
        conf["winH"] = Int(cr.height)
        conf["winX"] = Int(cr.origin.x)
        conf["winY"] = y
        writeConf(conf)
        logLine("窗口几何已记（外壳）：\(key)")
    }

    func applicationWillTerminate(_ n: Notification) {
        saveGeometry()
    }

    // ================================================================== 主菜单
    // ★★ 2026-09-29 晚（用户报「⌘Q / ⌘W 都不管用」）：**根因是本 app 从来没有主菜单**。
    //   macOS 的 ⌘Q / ⌘W / ⌘C / ⌘M 全是**菜单项上的 keyEquivalent**，AppKit 拿
    //   `NSApp.mainMenu` 去匹配；mainMenu 为 nil 时**没有任何接收者**，按键被静默
    //   丢弃（连一声 beep 都没有）。本 app 纯代码创建、没有 nib/xib，AppKit 也不会
    //   替你造一个 —— 必须自己建。这里建一套 macOS 标准五段菜单。
    //
    //   键位（2026-09-29 用户二次指定）：
    //     ⌘Q = **真的退出**（先把界面服务停掉，与页面「关闭服务」同一条路）
    //     ⌘W = **关掉窗口，外壳不退出**
    //     其余照标准：⌘H 隐藏 / ⌘M 最小化 / ⌘R 重载 / ⌘+ ⌘- ⌘0 缩放 / 编辑四项

    func buildMainMenu() {
        // ⚠️ `NSSelectorFromString("copy:")` 这种**多一层括号**的写法是拿标准动作名的老办法：
        //    copy: / cut: / paste: / selectAll: 都定义在 NSText 上，
        //    `#selector(NSText.copy(_:))` 在部分 SDK 上会报找不到；字符串 Selector 最稳。
        func mk(_ title: String, _ action: Selector?, _ key: String,
                _ mods: NSEvent.ModifierFlags = [.command],
                _ target: AnyObject? = nil) -> NSMenuItem {
            let it = NSMenuItem(title: title, action: action, keyEquivalent: key)
            it.keyEquivalentModifierMask = key.isEmpty ? [] : mods
            it.target = target
            // ★ 打个标记：macOS 会**自动**往「编辑」里塞「开始听写」(⌘D×2) 与
            //   「表情与符号」，下面那行日志只列**我们自己建的**，不列它们。
            it.tag = MK_TAG
            return it
        }

        let main = NSMenu()

        // ① 应用菜单（标题只是占位；macOS 顶栏显示的是 CFBundleName）
        let host1 = NSMenuItem()
        main.addItem(host1)
        let mApp = NSMenu(title: APP_TITLE)
        host1.submenu = mApp
        mApp.addItem(mk("关于 \(APP_TITLE)",
                        #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""))
        mApp.addItem(.separator())
        // ★ 标准的「设置… ⌘,」位置。原来只有**菜单栏图标**里有 ⌘, —— 那个键位
        //   只在那个菜单**拉开时**才生效，等于没有；挪到主菜单里才是全局的。
        mApp.addItem(mk("状态与设置…", #selector(actSettings), ",", [.command], self))
        mApp.addItem(.separator())
        mApp.addItem(mk("隐藏 \(APP_TITLE)", #selector(NSApplication.hide(_:)), "h"))
        mApp.addItem(mk("隐藏其他", #selector(NSApplication.hideOtherApplications(_:)), "h",
                        [.command, .option]))
        mApp.addItem(mk("全部显示", #selector(NSApplication.unhideAllApplications(_:)), ""))
        mApp.addItem(.separator())
        // ★ 这一条就是 ⌘Q：动作是标准的 terminate: → 落到 applicationShouldTerminate
        //   （那里先停界面服务、再退壳）。**target 留 nil**，交给 NSApp。
        mApp.addItem(mk("退出 \(APP_TITLE)", #selector(NSApplication.terminate(_:)), "q"))

        // ② 文件：⌘W 只关窗口 —— 与服务「关闭服务」是两件事，别混
        let host2 = NSMenuItem()
        main.addItem(host2)
        let mFile = NSMenu(title: "文件")
        host2.submenu = mFile
        mFile.addItem(mk("关闭窗口（服务继续跑）", #selector(actCloseWindow), "w", [.command], self))

        // ③ 编辑：target 全留 nil → 沿响应链走到 WKWebView（页面里选中文字才 ⌘C 得动）
        let host3 = NSMenuItem()
        main.addItem(host3)
        let mEdit = NSMenu(title: "编辑")
        host3.submenu = mEdit
        mEdit.addItem(mk("撤销", NSSelectorFromString("undo:"), "z"))
        mEdit.addItem(mk("重做", NSSelectorFromString("redo:"), "z", [.command, .shift]))
        mEdit.addItem(.separator())
        mEdit.addItem(mk("剪切", NSSelectorFromString("cut:"), "x"))
        mEdit.addItem(mk("拷贝", NSSelectorFromString("copy:"), "c"))
        mEdit.addItem(mk("粘贴", NSSelectorFromString("paste:"), "v"))
        mEdit.addItem(.separator())
        mEdit.addItem(mk("全选", NSSelectorFromString("selectAll:"), "a"))

        // ④ 显示
        let host4 = NSMenuItem()
        main.addItem(host4)
        let mView = NSMenu(title: "显示")
        host4.submenu = mView
        mView.addItem(mk("重新载入界面", #selector(actReload), "r", [.command], self))
        mView.addItem(.separator())
        mView.addItem(mk("放大", #selector(actZoomIn), "+", [.command], self))
        mView.addItem(mk("缩小", #selector(actZoomOut), "-", [.command], self))
        mView.addItem(mk("实际大小", #selector(actZoomReset), "0", [.command], self))

        // ⑤ 窗口（挂给 windowsMenu；AppKit 会自己往末尾追加窗口列表）
        let host5 = NSMenuItem()
        main.addItem(host5)
        let mWin = NSMenu(title: "窗口")
        host5.submenu = mWin
        mWin.addItem(mk("最小化", #selector(NSWindow.performMiniaturize(_:)), "m"))
        // ★★ 2026-09-30（第廿七轮）：「缩放」（performZoom）这一项**删掉** ——
        //   窗口已定尺（minSize == maxSize），点它什么都不会发生，
        //   留着就是个骗人的菜单项。最小化不受影响。
        mWin.addItem(.separator())
        mWin.addItem(mk("前置全部窗口", #selector(NSApplication.arrangeInFront(_:)), ""))

        NSApp.mainMenu = main
        NSApp.windowsMenu = mWin

        // ★ 落一条日志：以后「快捷键又不管用」时，先看这行有没有建出来。
        //   （只列自己建的项 —— AppKit 会往「编辑」里自动补听写/表情，别混进来。）
        var brief: [String] = []
        for it in main.items {
            guard let sub = it.submenu else { continue }
            let keys = sub.items.filter { $0.tag == MK_TAG && !$0.keyEquivalent.isEmpty }
                .map { i -> String in
                    let m = i.keyEquivalentModifierMask
                    let pre = (m.contains(.control) ? "⌃" : "")
                            + (m.contains(.option) ? "⌥" : "")
                            + (m.contains(.shift) ? "⇧" : "") + "⌘"
                    return pre + i.keyEquivalent.uppercased()
                }
            brief.append("\(sub.title)(\(keys.joined(separator: " ")))")
        }
        logLine("主菜单已建：" + brief.joined(separator: " ｜ "))
        logLine("键位：⌘Q 退出并停服务 ｜ ⌘W 只关窗口（外壳不退）｜ ⌘H 隐藏 ｜ ⌘R 重载 ｜ ⌘M 最小化")
    }

    // ---- 主菜单里那几个动作 ------------------------------------------------

    /// ⌘W / 文件菜单：只把窗口收起来。**界面服务与三条守护照跑**，下次开窗瞬间出来。
    @objc func actCloseWindow() {
        logLine("⌘W → 只收窗口（界面服务与三条守护继续跑）")
        hideWindow()
    }

    @objc func actReload() {
        logLine("⌘R → 重新载入页面")
        webView.reload()
    }

    @objc func actZoomIn()    { applyZoom(webView.pageZoom + 0.1) }
    @objc func actZoomOut()   { applyZoom(webView.pageZoom - 0.1) }
    @objc func actZoomReset() { applyZoom(1.0) }

    func applyZoom(_ z: CGFloat) {
        let v = min(max(z, 0.5), 3.0)
        webView.pageZoom = v
        logLine("页面缩放 → \(Int(v * 100))%")
    }

    // ================================================================== 菜单栏
    // ★★ 2026-09-29（用户第 2 条）：「像 PommeToys 那种软件」——顶上常驻一个图标，
    //    既能**看**（服务在不在跑、防休眠开没开），也能**操作**（开窗口 / 设置 / 刷新 / 退出）。

    /// ★★ 2026-09-29（用户第 11 条）：自绘菜单栏图标 —— **填充圆角方块 + 镂空粗对勾**。
    ///    之前的 SF Symbol `checklist` 是细描边风格，用户嫌没设计感，点名要
    ///    「加一个框，或者让它变成那种大部分是填充色的样子」—— 本图取后者：
    ///    一整块实心圆角芯片，对勾是从里面**抠掉**的（alpha 镂空）。
    ///    为什么纯代码画：app 要保持自包含（构建只依赖源码），不往包里塞图片资源；
    ///    模板图（isTemplate=true）只看 alpha，系统自动适配菜单栏深浅色，不用管 light/dark。
    ///    2x 位图（36×36 像素、18pt 逻辑尺寸）保证 Retina 菜单栏上边缘干净。
    ///    ⚠️ 两处踩过的坑：
    ///      ① `NSGraphicsContext(bitmapImageRep:)` 会按 rep.size(pt)↔像素**自动缩放**
    ///         （rep.size 设成 18、像素 36 → 上下文自带 2x）—— 自己再 scaleBy 就画飞了
    ///         （实测：内容缩到画布一角）。所以下面**不再手动 scaleBy**，直接按 pt 画。
    ///      ② destinationOut 镂空可靠起见包在 transparency layer 里（CG 混合模式
    ///         在部分上下文上只对 layer 内容生效）。
    private func makeMenuIcon() -> NSImage? {
        let S: CGFloat = 18
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: Int(S * 2), pixelsHigh: Int(S * 2),
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)
        else { return nil }
        rep.size = NSSize(width: S, height: S)
        NSGraphicsContext.saveGraphicsState()
        guard let g = NSGraphicsContext(bitmapImageRep: rep) else {
            NSGraphicsContext.restoreGraphicsState()
            return nil
        }
        NSGraphicsContext.current = g
        let ctx = g.cgContext

        ctx.beginTransparencyLayer(auxiliaryInfo: nil)
        // ① 芯片：16×16 圆角方块，四周留 1pt 呼吸边（贴着菜单栏光学网格居中）
        let chip = NSBezierPath(roundedRect: NSRect(x: 1, y: 1, width: 16, height: 16),
                                xRadius: 4.6, yRadius: 4.6)
        ctx.addPath(chip.cgPath)
        ctx.setFillColor(NSColor.black.cgColor)
        ctx.fillPath()
        // ② 镂空对勾（对勾四变体里 V3 最正：顶点略偏左、两臂 45°/50°、跨距宽）
        ctx.setBlendMode(.destinationOut)
        ctx.setStrokeColor(CGColor(gray: 0, alpha: 1))
        ctx.setLineWidth(2.2)
        ctx.setLineCap(.round)
        ctx.setLineJoin(.round)
        ctx.beginPath()
        ctx.move(to: CGPoint(x: 3.9, y: 8.8))
        ctx.addLine(to: CGPoint(x: 6.9, y: 11.8))
        ctx.addLine(to: CGPoint(x: 13.9, y: 4.0))
        ctx.strokePath()
        ctx.setBlendMode(.normal)
        ctx.endTransparencyLayer()

        NSGraphicsContext.restoreGraphicsState()
        let img = NSImage(size: NSSize(width: S, height: S))
        img.addRepresentation(rep)
        img.isTemplate = true
        return img
    }

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = statusItem.button {
            // ★ 2026-09-29（用户第 11 条）：优先用自绘的「填充芯片 + 镂空对勾」模板图；
            //   SF Symbol checklist 只剩退路（自绘万一失败）。
            if let img = makeMenuIcon() {
                b.image = img
                logLine("菜单栏图标：自绘「填充芯片+镂空对勾」模板图（18pt@2x）")
            } else if let img = NSImage(systemSymbolName: "checklist",
                                        accessibilityDescription: APP_TITLE) {
                img.isTemplate = true
                b.image = img
                logLine("菜单栏图标：自绘失败 → 退回 SF Symbol checklist")
            } else {
                b.title = "WB"          // 极老系统上的退路
                logLine("菜单栏图标：两种都取不到 → 退回文字 WB")
            }
            b.toolTip = APP_TITLE
        }

        let m = NSMenu()
        m.delegate = self
        statHead = NSMenuItem(title: "检查中…", action: nil, keyEquivalent: "")
        statHead.isEnabled = false
        statLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        statLine.isEnabled = false
        m.addItem(statHead)
        m.addItem(statLine)
        m.addItem(.separator())
        m.addItem(NSMenuItem(title: "打开任务管家窗口", action: #selector(actOpen),
                             keyEquivalent: "o"))
        m.addItem(NSMenuItem(title: "状态与设置…", action: #selector(actSettings),
                             keyEquivalent: ","))
        m.addItem(NSMenuItem(title: "刷新状态", action: #selector(actRefresh),
                             keyEquivalent: "r"))
        m.addItem(.separator())
        // ★★★ 2026-10-01（第四十七轮 · 用户第 1 条）：「关闭界面服务（保留守护）」与
        //   「退出任务管家」**合并成一条** —— 只留「退出」。
        //   用户原话：「按理讲『关闭服务』和『退出』这两个操作应该都是一致的，应该
        //   直接退出程序，包括菜单栏的程序图标」。「只停服务、外壳留着」那条半截路
        //   已被证明会让人困惑（Dock 图标没了、菜单栏图标还在），整条撤掉。
        m.addItem(NSMenuItem(title: "退出任务管家", action: #selector(actQuit),
                             keyEquivalent: ""))
        for it in m.items where it.action != nil { it.target = self }
        statusMenu = m
        // ★★ 2026-09-29（用户第 8 条）：**不再把菜单挂到 statusItem.menu 上** ——
        //   挂了它，点一下出来的就是系统那列纯文字下拉菜单（改不出花样）。
        //   改成自己接点击：**左键 → 弹自定义面板**，**右键（或 ⌃+左键）→ 出老菜单**
        //   （老菜单留着的理由：① 服务没起来时面板载不出来，得有退路；
        //     ② 键盘党/无障碍用户还是想要一列能上下选的菜单）。
        if let b = statusItem.button {
            b.target = self
            b.action = #selector(actStatusClick)
            b.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        statusItem.menu = nil
        logLine("菜单栏图标已挂上（isVisible=\(statusItem.isVisible)，点击＝弹面板）")
        // 1 秒后报一次「菜单栏项到底画在哪儿」——被刘海/空间不足挤出屏幕时 x 会是负的。
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
            guard let self = self else { return }
            let sw = NSScreen.main?.frame.width ?? -1
            if let w = self.statusItem?.button?.window {
                logLine("菜单栏项几何：x=\(Int(w.frame.origin.x)) y=\(Int(w.frame.origin.y))"
                        + " 宽=\(Int(w.frame.width)) 可见=\(w.isVisible)"
                        + " ｜屏幕宽=\(Int(sw))")
            } else {
                logLine("菜单栏项：button.window 取不到 ｜屏幕宽=\(Int(sw))")
            }
        }
    }

    // ================================================================ 菜单栏面板
    // ★★ 2026-09-29（用户第 8 条，照 PommeToys）：
    //   「关于任务栏顶端，虽然说有按钮了，但是按钮的 UI 界面还是要参考我给你的这个
    //     软件的界面来进行设计，起码运行监测栏目里要具备同样的功能显示在顶部的任务栏」
    //
    //   查过 PommeToys 的实际做法（从它的二进制里挖出来的符号）：
    //     MenuPanelWindowController / MenuPanelHostingView / MenuPanelView
    //     MenuPanelNavigation / MenuPanelPage / MenuPanelRail / MenuPanelSegPlate
    //     MenuPanelAwakeLCD / MenuPanelAwakeTextTrail / SevenSegmentReadout
    //     KeepAwakeRail / KeepAwakeRailHand / KeepAwakeDurationFader / KeepAwakeReadout
    //     AwakeEntryButton / PowerLever / LeverSlot
    //   结论：它那块**不是系统下拉菜单，是一个自定义面板窗口**，而且里面装的正是
    //   防休眠那一套 —— 和它的字符串对得上：
    //     Keep the display on / Stop when battery is low /
    //     Stay awake with the lid closed / Keep the built-in display on with the lid closed
    //   （正好是我们的 屏幕保持常亮 / 电量不足时自动停 / 合盖后保持唤醒 /
    //     合盖后内建显示器保持点亮）。
    //
    //   所以这里照同一路子做：NSPanel + WKWebView，载页面的面板模式。
    //   ★ 为什么用页面而不是纯 AppKit 重画一遍：首页那张运行监测卡**就是**照
    //     PommeToys 做的（深色手持屏 + 开关 + 数码管质感的收尾按钮），
    //     把同一份 DOM 搬进面板 → 样式天然一致、开关事件与 /api/status 轮询也照旧，
    //     两边状态**不需要任何同步代码**（同一份卡，从哪儿点都改的是同一处）。

    /// 左键点图标 = 弹/收面板；右键（或 ⌃+左键）= 出老菜单。
    /// ⚠️ 用 `statusItem.button` 的 action 接点击时，**右键默认不会触发** ——
    ///    必须 `sendAction(on:)` 把 `.rightMouseUp` 也加进去（见 buildStatusItem）。
    @objc func actStatusClick() {
        // ★★ 自测模式**忽略点击**（2026-09-29 踩到）：
        //   自测把面板弹在**图标正下方**并等 4 秒，这期间用户顺手点一下菜单栏图标
        //   （或者刚好看一眼新面板）就会触发 `togglePanel()` 把面板收了 ——
        //   日志里表现为「已收起（再点一次图标）」，测试却把它判成「面板没弹出来」。
        //   实测 5 次里中 2 次。自测是无人值守的诊断，本来就不该有交互参与。
        if PANEL_SELFTEST { return }
        let ev = NSApp.currentEvent
        let right = (ev?.type == .rightMouseUp)
                 || (ev?.modifierFlags.contains(.control) ?? false)
        if right { showPlainMenu(); return }
        togglePanel()
    }

    /// 把老的纯文字菜单弹出来（右键、或服务没起来时的退路）。
    /// 手法：临时把 menu 挂回 statusItem，performClick 之后立刻摘掉 ——
    /// 这是 AppKit 里「既想自定义点击、又想偶尔弹一次菜单」的标准做法。
    func showPlainMenu() {
        guard let si = statusItem, let b = si.button else { return }
        refreshMenuStatus()
        si.menu = statusMenu
        b.performClick(nil)
        si.menu = nil
        logLine("菜单栏：弹出老版文字菜单")
    }

    /// 建一次面板窗口（之后复用）。面板**不抢焦点以外的任何东西**：
    /// 无标题栏、无背景、圆角交给页面里的 #panwrap 画。
    func ensurePanel() {
        if panelWin != nil { return }
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        // ★ 只给**面板**这个 webView 注册 shell 消息通道（主窗口那个不注册）——
        //   面板里那几颗按钮要操作 NSWindow / NSStatusItem，网页自己干不了。
        //   副作用：从普通浏览器打开 ?panel=1 时 window.webkit 是 undefined，
        //   页面里的 shellCmd() 会走退路分支（提示「要装在 app 里才好使」），不会炸。
        cfg.userContentController.add(PanelBridge(shell: self), name: "shell")

        let wv = WKWebView(frame: .zero, configuration: cfg)
        wv.uiDelegate = self
        wv.navigationDelegate = self
        wv.setValue(false, forKey: "drawsBackground")   // 让窗口的透明圆角露出来
        panelWeb = wv

        let win = NSPanel(contentRect: NSRect(x: 0, y: 0, width: PANEL_W, height: panelH),
                          styleMask: [.borderless, .nonactivatingPanel],
                          backing: .buffered, defer: false)
        win.isOpaque = false
        win.backgroundColor = .clear
        win.hasShadow = true
        win.level = .popUpMenu                 // 浮在普通窗口之上，别被主窗口盖住
        win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        win.hidesOnDeactivate = false
        win.isReleasedWhenClosed = false
        win.delegate = self
        win.contentView = wv
        panelWin = win
        // ★★ 面板的「失焦就收」改由**切 App** 来触发（2026-09-29 修）。
        //   原来挂在 `windowDidResignKey` 上，实测**是错的**：
        //   面板用 `.nonactivatingPanel` 是**故意**不激活本 App 的（否则会抢走用户
        //   正在打字的焦点），代价是本 App 常常不是前台 —— 那它拿到的 key 随时会被
        //   真正的前台 App 收回去。实测：面板弹出后**约 1 秒**就来一次
        //   `windowDidResignKey`，于是面板把自己收了 —— 用户看到的是
        //   「点一下、闪一下就没了」。（自检 3 次里中 2 次，正是这个。）
        //   换成 NSWorkspace 的「有 App 被激活」通知：只有**真的切走**才收。
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(actAppSwitched(_:)),
            name: NSWorkspace.didActivateApplicationNotification, object: nil)
        logLine("菜单栏面板已创建（\(Int(PANEL_W)) 宽，内容高 \(Int(panelH))）")
    }

    /// 用户切到**别的 App** → 把面板收起来（系统的菜单栏面板都是这个脾气）。
    /// ⚠️ 只认「**能确定**被激活的不是自己」—— 取不到是谁时**宁可不动**：
    ///    收起还有另外三条路兜着（点面板外 / Esc / 再点图标），
    ///    但「误收」是用户直接看得见的坏体验。
    @objc func actAppSwitched(_ n: Notification) {
        // ★ 自测模式同理：自测那 4 秒里用户切个 App（或系统自己激活谁）就会把面板收了，
        //   于是把「面板弹出来了」判成「没弹出来」。收起规则不参与自测。
        if PANEL_SELFTEST { return }
        // ★★★ 第卅九轮：用户「切 App」这个动作，就是「跑去系统设置勾完权限、再切回来」
        //   的化身 —— 在这里**静默**实测一次权限（结论没变时它什么都不做：
        //   不写盘、不重挂引擎、不打日志）。这就是原来那个 5 秒定时器的替代品：
        //   由**真实动作**驱动，平时零唤醒。
        permWatchTick()
        let app = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
        let self_pid = ProcessInfo.processInfo.processIdentifier
        guard panelOpen else { return }
        guard let app = app else {
            logLine("菜单栏面板：收到「切 App」通知但取不到是哪个 App → 不收（保守）")
            return
        }
        let pid = app.processIdentifier
        if pid == self_pid { return }
        if let b = app.bundleIdentifier, b == Bundle.main.bundleIdentifier { return }
        logLine("菜单栏面板：切到别的 App（\(app.localizedName ?? "?") pid=\(pid)"
                + "，自己 pid=\(self_pid)）→ 收起")
        hidePanel()
    }

    @objc func togglePanel() {
        if panelOpen { hidePanel("再点一次图标"); return }
        // ★ 服务没在跑：面板载不出来 → 先弹老菜单给用户一个反馈，
        //   同时在后台把服务拉起来（下次点就是面板了）。绝不让用户点了没反应。
        guard portAlive(port) else {
            logLine("菜单栏点击：服务没在跑 → 先弹老菜单，并在后台拉起服务")
            showPlainMenu()
            DispatchQueue.global().async { [weak self] in
                guard let self = self else { return }
                if ensureService(port: self.port) {
                    DispatchQueue.main.async { self.reloadPage() }
                } else {
                    DispatchQueue.main.async { self.showFailPage() }
                }
            }
            return
        }
        showPanel()
    }

    func showPanel() {
        ensurePanel()
        // ★ 第卅八 / 卅九轮：用户多半是「刚从系统设置勾完权限回来」才点开面板 ——
        //   顺手静默实测一次，让面板一亮就是最新状态。
        //   幂等、不弹系统框；结论没变时它什么都不做。
        //   ★ 第卅九轮起，这就是**唯一的**那个"节拍"：由「用户点图标」这个真实动作驱动，
        //     不再有任何后台定时器（原来那个 5 秒的已经删了，用户明确反感）。
        permWatchTick()
        // ★★ 2026-09-29（用户第 18 条）：「点一下图标，界面弹出来会卡一下」。
        //   原来是**每次点击都重载整页**（`panelWeb.load(…, reloadIgnoringLocalCacheData)`）。
        //   实测（`_panel_lat.swift`，真窗口量「点下 → 看得见内容」）：
        //       408 / 363 / 372 ms —— 其中 didFinishNavigation 就占 363~407ms，
        //       之后的绘制只花了 0.3ms。
        //   而窗口是**立刻** orderFront 的 → 用户看到的就是「先弹出来、空一下、内容才到」。
        //   现在：页面在启动时就预载好（见 preloadPanel），点图标只剩
        //   「摆位 ＋ 端上来 ＋ 叫页面刷一次数据」→ 窗口与内容同时到。
        //   ⚠️ 页面没载起来（服务重启过 / 预载失败）时**必须**现载一次 ——
        //      弹出一张白板比慢更糟，这条退路不能省。
        if !panelLoaded && !panelLoading {
            loadPanelPage("点击时还没有可用页面")
        }
        // ★★ 2026-09-30（第廿八轮 · 用户第 1 条）：「长时间不点之后，点一下还是闪出来
        //   一半界面，马上就又显示完全，类似卡一下的效果」。
        //   第 18 条那轮治的是「每次点击都重载整页」（363~408ms 白屏），但还剩两层没治：
        //     ① **高度**：面板页刚载入、数据还没到时报了个残缺高度（`_外壳.log` 实测
        //        755 → 206），外壳不但照它摆窗口，还把它写进了 `_界面.json` →
        //        下次弹出真的按 206 摆 = 只露出上半截。页面侧的守卫见
        //        `_ui.html::reportPanelHeight`（**画过一次真值才准报高度**）。
        //     ② **顺序**：原来是「先 orderFront 端上来 → 再叫页面睁眼」。窗口一上屏
        //        就摆好了，内容随后才到 → 用户看到「先半截、再补全」，那一下就是「卡」。
        //   现在把它翻过来：**先问页面「你现在多高」**（页面的 __panelPrepare 会顺手
        //   把快照画好、真值拉回来、轮询开起来），**拿到高度才摆位 + 上屏**。
        //   窗口出现的那一帧就是完整内容 + 正确高度 —— 没有「补全」这个动作。
        //   ⚠️ 一定要有超时兜底（PANEL_PREPARE_TIMEOUT）：页面卡死 / 服务慢时，
        //      宁可先用记忆高度把窗口弹出来，也绝不能让用户「点了没反应」。
        panelPrepareThen { [weak self] h in
            guard let self = self else { return }
            // h > 120 = 页面真回来报了高（不是超时兜底的 0）→ __panelPrepare 跑完了，
            // 快照/真值/轮询它都办过了，下面不必再叫一次 __panelWake。
            let pageDidIt = h > 120
            if pageDidIt { self.panelH = CGFloat(h) }
            self.revealPanel(pagePrepared: pageDidIt)
        }
    }

    /// 面板「真正端上来」的那一半：摆位 + 上屏 + 装收起规则 + 记账。
    /// 由 `showPanel` 在**内容就位之后**调用（`pagePrepared` = 页面已经自己画好了）。
    func revealPanel(pagePrepared: Bool) {
        positionPanel()
        // ★★★ 第卅轮：**先透明上屏**，等页面真画出第一帧再亮（见 panelFadeIn）。
        //   顺序是死的：`alphaValue` 必须在 `orderFront` **之前**置下去 ——
        //   反过来的话会先亮一帧没画完的窗口，那正是要治的「闪出来一半」。
        //   ⚠️ 用 0.01 而不是 0：`_panel_lat.swift` 量延迟用的就是这一招（它用 0.02）——
        //      WebKit 认「窗口在屏可见」要看 `isVisible` / 遮挡状态，1% 已经够；
        //      而 1% 亮度在人眼看来就是「没有」，不会先闪一下底。
        panelWin.alphaValue = PANEL_SELFTEST ? 1 : 0.01
        panelWin.makeKeyAndOrderFront(nil)     // nonactivatingPanel → 不抢前台 App
        panelOpen = true
        // ★ 睁眼：先画上次那份快照，再对一次真值（见 _ui.html 的 __panelWake）。
        //   收起期间那个页面是**睡着的**（轮询已停），所以这一步不是可选项 ——
        //   ⚠️ 但页面刚在 `__panelPrepare` 里做过同一件事（连轮询一起开了）时就别再补一刀：
        //      那会白发一次 /api/status。这里只在「页面没跑成 prepare」（没载起来 / 超时）时走老路。
        if !pagePrepared { panelPageWake() }
        // ★★ 自测模式**不装**「点面板外面就收起」（2026-09-29 踩到）：
        //   外部鼠标监听收到的是**别的 App 里**的鼠标按下 —— 也就是**真人在别处点一下**
        //   （比如用户在 WorkBuddy / 访达里点一下）。自测要等 4 秒再自报，
        //   这期间只要用户碰一下鼠标，面板就被这条规则收了，判成「面板没弹出来」。
        //   实测就是它造成 3 次里中 2 次的抖动（加上日志里的「（点面板外面）」才认出来）。
        //   自测要验的是「弹出来了、摆在正位、内容对、**并且留得住**」；
        //   收起规则由静态断言钉（`_chk_menubar_panel.py`：那些函数必须还在、接法必须对）。
        if !PANEL_SELFTEST { addOutsideMonitor() }
        logLine("菜单栏面板已弹出（x=\(Int(panelWin.frame.origin.x)) y=\(Int(panelWin.frame.origin.y))"
                + " \(Int(panelWin.frame.width))x\(Int(panelWin.frame.height))"
                + "，内容高 \(Int(panelH))\(pagePrepared ? "" : "（没等页面，按记忆高度）")）")
        // ★★★ 第卅轮：窗口已经「在屏」了（只是看不见）→ 现在等页面出第一帧，再淡入。
        panelFadeIn()
    }

    /// ★★★ 2026-10-01（第卅轮 · 用户第 1 条）：**等页面真画出第一帧，再把面板亮出来。**
    ///
    ///   `revealPanel` 已经先 `alphaValue = 0.01` 上屏了 —— 这一步是承上启下的：
    ///   窗口既然「在屏可见」，页面里的 `requestAnimationFrame` 就会正常触发
    ///   （面板**收着**的时候它是**不会**触发的，`_ui.html::__panelPrepare` 那条注释
    ///   记过同一件事）。于是可以直接问 WebKit「你画完没有」：
    ///     等**两帧** —— 第一帧把 DOM 的改动排进合成，第二帧确认已经出画。
    ///
    ///   为什么不「固定等 100ms 再亮」：等多久取决于渲染进程有多困，而那件事只有
    ///   WebKit 自己知道。让它报信最准：醒着时 1~2 帧就到（约 16~30ms），
    ///   闲置久了它自己会多花几十毫秒去醒 —— 反正窗口还看不见，多等一会儿也不亏。
    ///
    ///   ⚠️ 三条硬约束（都是踩过的）：
    ///     ① `done` **一定会被调用、且只调一次**，且一定在 `PANEL_WARMUP_TIMEOUT` 内。
    ///        宁可偶发一帧没画完，也绝不能「点了图标面板不出来」。
    ///     ② 连点两下（开→关→开）时靠 `panelFadeToken` 作废上一轮 ——
    ///        否则上一轮迟到的回调会把**已经收起来**的面板又亮出来。
    ///     ③ 自测模式不等动画：那个通道量的是「弹出来了没、摆得对不对」，
    ///        多一层淡入只会给判据添乱。
    func panelFadeIn() {
        panelFadeToken += 1
        let token = panelFadeToken
        var fired = false
        func fire() {
            if fired { return }                // 超时与 rAF 谁先到都行，只认第一个
            fired = true
            guard let w = panelWin else { return }
            // 这一轮已经作废（被收起 / 被下一次弹出顶掉）→ 只把透明度归位，别做动画
            guard panelOpen, panelFadeToken == token else { w.alphaValue = 1; return }
            if PANEL_SELFTEST { w.alphaValue = 1; return }
            NSAnimationContext.runAnimationGroup({ ctx in
                ctx.duration = PANEL_FADE_DURATION
                w.animator().alphaValue = 1
            }, completionHandler: nil)
        }
        if panelLoaded, panelWeb != nil {
            // ★ rAF 回调跑在「这一帧**开始**画」之前，所以第 2 帧之后再让一拍
            //   （setTimeout 0）—— 那一帧才真的送进合成器。
            let js = "(function(){return new Promise(function(res){"
                   + "requestAnimationFrame(function(){requestAnimationFrame(function(){"
                   + "setTimeout(res,0);});});});})()"
            panelWeb.callAsyncJavaScript(js, arguments: [:], in: nil, in: .page) { _ in
                DispatchQueue.main.async { fire() }
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + PANEL_WARMUP_TIMEOUT) {
            DispatchQueue.main.async { fire() }
        }
    }

    /// ★★ 2026-09-30（第廿八轮 · 用户第 1 条）：**上屏前的「内容就位」这一步**。
    ///   问页面「你现在多高」（`_ui.html::__panelPrepare` 会先把快照画上、真值拉回来，
    ///   再量 `#panwrap` 的高度），拿到才让 `done` 去摆位 + 上屏。
    ///   ⚠️ `done` **一定会被调用，且只调一次**、且一定在 `PANEL_PREPARE_TIMEOUT` 内：
    ///       页面没载起来 → 立刻用 0 放行；页面卡住/服务慢 → 到点用 0 放行。
    ///       （0 的约定 = "没量到"，调用方拿 `panelH` 这个记忆值兜底。）
    func panelPrepareThen(_ done: @escaping (Double) -> Void) {
        panelPrepareToken += 1
        let token = panelPrepareToken
        var fired = false
        func fire(_ h: Double) {
            if fired { return }        // 超时与回调谁先到都行，只认第一个
            fired = true
            done(h)
        }
        var answered = false
        // 兜底闹钟：到点还等不到页面 → 放行（用记忆高度弹，绝不让人干等）
        DispatchQueue.main.asyncAfter(deadline: .now() + PANEL_PREPARE_TIMEOUT) { [weak self] in
            guard let self = self, self.panelPrepareToken == token else { return }
            // ★★★ 2026-10-01（第五十三轮）：**记一笔**。页面没在 60ms 内回话，
            //   就等于「这一页已经不会动了」—— 连问两次就重载它（见 notePanelUnresponsive）。
            if !answered { self.notePanelUnresponsive() }
            fire(0)
        }
        guard panelLoaded, panelWeb != nil else {
            logLine("面板上屏：页面还没载起来 → 先按记忆高度弹（内容到了再校正）")
            fire(0)
            return
        }
        // ⚠️ 用 callAsyncJavaScript 而不是 evaluateJavaScript：页面的 __panelPrepare 返回
        //    Promise（要等它把真值拉回来），evaluateJavaScript 拿不到 Promise 的结果。
        panelWeb.callAsyncJavaScript(
            "return await (window.__panelPrepare ? window.__panelPrepare() : 0);",
            arguments: [:], in: nil, in: .page) { [weak self] result in
            guard let self = self, self.panelPrepareToken == token else { return }
            answered = true
            var h: Double = 0
            if case .success(let v) = result {
                if let n = v as? NSNumber { h = n.doubleValue }
                else if let d = v as? Double { h = d }
            }
            if h > 0 { logLine("面板上屏：页面回报内容高 \(Int(h))") }
            if h > 0 { self.panelStuck = 0 } else { self.notePanelUnresponsive() }
            fire(h)
        }
    }

    /// ★★★ 2026-10-01（第五十三轮 · 用户「界面消失不见了，再解决一下」）。
    ///   面板页「问话没人应」记一笔；**连续两次**就把那一页整个重新载一遍。
    ///   为什么需要它：面板页是**启动时预载一次、之后一直复用**的（为了点图标不卡），
    ///   一旦那一页自己卡住/变成白板，外壳这边 `panelLoaded` 还是 true →
    ///   `showPanel` 永远不会再载它（它只在 `!panelLoaded && !panelLoading` 时才重载）
    ///   ⇒ 用户看到的就是「面板成了一张白纸，而且怎么点都回不来」。
    ///   ⚠️ 连问两次才动手：60ms 在页面忙着拉数据时偶尔会超一次，一次就重载会白闪。
    func notePanelUnresponsive() {
        panelStuck += 1
        if panelStuck < 2 {
            logLine("面板页无响应（第 \(panelStuck) 次）→ 再给一次机会")
            return
        }
        panelStuck = 0
        logLine("⚠️ 面板页连续无响应 → 重载那一页（「界面不见了」多半就是它）")
        if !panelLoading { loadPanelPage("页面无响应") }
    }

    /// 面板页的地址（预载 / 退路重载 / 日志共用一处拼）。
    var panelURLString: String { "http://127.0.0.1:\(port)/?panel=1" }

    /// 真去**载**一次面板页。只在「手上还没有可用页面」时才走这儿
    /// （见 showPanel 与 panelCommand 的 refresh）。
    /// ⚠️ 这里是面板页**唯一**的 load 入口 —— 别处不许再直接 `panelWeb.load(...)`：
    ///    那正是用户第 18 条那个「每次点图标都重载整页 → 卡一下」的来源。
    func loadPanelPage(_ why: String = "首次载入") {
        guard panelWin != nil, let u = URL(string: panelURLString) else { return }
        panelLoaded = false
        panelLoading = true
        panelWeb.load(URLRequest(url: u, cachePolicy: .reloadIgnoringLocalCacheData,
                                timeoutInterval: 20))
        logLine("面板载入（\(why)）")
    }

    /// ★★ 面板**预载**（2026-09-29 用户第 18 条）。
    ///   为什么非预载不可：面板是「点一下图标才出现」的东西，而一次整页加载实测要
    ///   363~408ms（数字与量法见 showPanel 顶上的注释）—— 那正好就是那一下卡。
    ///   启动时先把它载好、收着不显示，之后再点就只剩「摆位 + orderFront」，≈0ms。
    ///   ⚠️ 时机放在主窗口开始加载**之后**（asyncAfter 1.5s）：两个 WKWebView 同时抢
    ///      WebKit 的首次初始化会互相拖慢，而主窗口才是用户先看到的那一个。
    ///   ⚠️ 服务没在跑就别预载：载出来是一句「连不上」，白等一轮还让人以为界面坏了。
    func preloadPanel() {
        guard portAlive(port) else {
            logLine("面板预载：服务没在跑 → 跳过（第一次点击时再载）")
            return
        }
        ensurePanel()
        if panelLoaded || panelLoading { return }      // 自测通道可能已经载过一次
        loadPanelPage("启动预载")
    }

    /// 面板页「睁眼」（弹出时调）。页面对应 `_ui.html` 的 `window.__panelWake`。
    /// ⚠️ 那边画的是**上一次的快照**（PANEL_LAST），不是重新拉一遍才画 ——
    ///    所以这个 evaluateJavaScript 的回调**不能**当作「内容已就绪」来用。
    func panelPageWake() {
        panelWeb?.evaluateJavaScript("window.__panelWake && window.__panelWake()") { _, _ in }
    }

    /// 面板页「闭眼」（收起时调）：让它把 3 秒轮询停掉。
    /// 为什么：面板是预载的，收着的时候这个页面还活着 —— 不叫停就是白白多烧一份
    /// （主窗口那份轮询照旧，两边加起来才是对服务的真实压力）。
    /// 页面对应 `_ui.html` 的 `window.__panelSleep`。
    func panelPageSleep() {
        panelWeb?.evaluateJavaScript("window.__panelSleep && window.__panelSleep()") { _, _ in }
    }

    /// `why` 只用于日志 —— 面板「自己收了」是最难查的一类毛病，
    /// 必须能从日志一眼看出**是哪条规则**收的（2026-09-29 为此加）。
    func hidePanel(_ why: String = "") {
        removeOutsideMonitor()
        // ★ 第卅轮：作废**还没跑完的淡入**。否则上一轮迟到的回调会把
        //   「已经收起来的面板」又亮出来（它判 `panelFadeToken == token` 才动手，
        //   这里 +1 就对不上号了）。
        panelFadeToken += 1
        panelWin?.orderOut(nil)
        // ★ 透明度**归位**：下一次 revealPanel 会自己置 0.01，
        //   但这里不归位的话，万一将来有别的路径直接 orderFront，就会亮一张隐身面板。
        panelWin?.alphaValue = 1
        panelPageSleep()          // ★ 收起来就让它睡（停掉 3 秒轮询），弹出时再叫醒
        if panelOpen { logLine("菜单栏面板已收起" + (why.isEmpty ? "" : "（\(why)）")) }
        panelOpen = false
    }

    /// 贴着菜单栏图标下沿摆，并夹在屏幕可见区内（图标被刘海挤到边上时也不跑出去）。
    func positionPanel() {
        guard let pw = panelWin, let b = statusItem?.button, let bw = b.window else { return }
        let bf = bw.frame                                   // 图标在屏幕坐标里的 frame
        let vis = (bw.screen ?? NSScreen.main ?? NSScreen.screens[0]).visibleFrame
        let W = PANEL_W
        let H = min(max(panelH, 180), max(240, vis.height - 60))
        var x = bf.midX - W / 2
        x = min(max(x, vis.minX + 8), vis.maxX - W - 8)
        // 菜单栏图标紧贴屏幕顶端 → 面板上沿就在图标下方 6pt
        var y = bf.minY - H - 6
        if y < vis.minY + 8 { y = vis.minY + 8 }
        pw.setFrame(NSRect(x: x, y: y, width: W, height: H), display: true)
    }

    /// 点面板**外面**就收起来（系统面板都这样）。
    /// ⚠️ 用 global monitor 而不是 local：面板内部也要正常收点击；
    ///    而且点在**自己的图标**上时不收 —— 那一下交给 actStatusClick 去切换，
    ///    否则会出现「按下收起 → 抬起又弹出」的抖动。
    func addOutsideMonitor() {
        removeOutsideMonitor()
        outsideMon = NSEvent.addGlobalMonitorForEvents(
            matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]) { [weak self] _ in
            guard let self = self else { return }
            if let bw = self.statusItem?.button?.window,
               bw.frame.insetBy(dx: -8, dy: -8).contains(NSEvent.mouseLocation) { return }
            self.hidePanel("点面板外面")
        }
    }

    func removeOutsideMonitor() {
        if let m = outsideMon { NSEvent.removeMonitor(m); outsideMon = nil }
    }

    /// 面板 → 外壳的指令入口（页面里 `window.webkit.messageHandlers.shell.postMessage(...)`
    /// 会打到这儿，见 PanelBridge）。
    /// ★ 关服务/退出**只在外壳里确认一次**（页面那边刻意不再 confirm()），
    ///   免得弹两个框 —— 主窗口那两颗同名按钮走的也是外壳这套 NSAlert。
    func panelCommand(_ cmd: String, payload: [String: Any] = [:]) {
        switch cmd {
        case "height":
            if let h = (payload["h"] as? NSNumber)?.doubleValue, h > 120 {
                let nh = CGFloat(h)
                // ★ 值没变就别动：页面 250ms 报一次，每次都重排＋重画是白费。
                if abs(nh - panelH) >= 1 {
                    let old = panelH
                    panelH = nh
                    positionPanel()
                    // ★ 记一笔：面板高度为什么会变，是"内容真的长了"还是"报了个残缺值"，
                    //   看日志一眼就分得出（第廿八轮那次的 755 → 206 正是从这儿看出来的）。
                    logLine("面板内容高度 \(Int(old)) → \(Int(nh))（窗口跟着重摆）")
                    // ★★ 顺手记进 _界面.json（2026-09-29 用户第 18 条）：
                    //   不记的话，**启动后第一次**点图标会先按默认 430 摆，
                    //   等内容报上来再跳到真高度（日志里实测见过 430 → 667 那一跳）。
                    //   记下来之后，第一次点就是对的（预载 + 这一条，双保险）。
                    //   ⚠️ 但这个"记下来"必须建立在**报的是真高**之上 —— 页面侧的
                    //      reportPanelHeight 守卫（画过真值才准报）就是为此加的，
                    //      否则这里会把残缺值固化进配置（第廿八轮的 206）。
                    // ★★★ 2026-10-01（第卅轮）再补一道：**只在面板开着的时候才落盘**。
                    //   预载那一两秒里页面是"半成品"—— 日志实测报过 **300**（真高 755），
                    //   那一刻虽然页面侧的守卫已经放行，但值仍然是残缺的。
                    //   写进去的下场就是：下次弹出按 300 摆 = 只露出上半截，
                    //   等 250ms 数据到了再长高，**又是一次「先闪出来一半」**。
                    //   面板开着时报的就是稳定值，那才是该记住的那个数。
                    // ★★★ 2026-10-01（用户第 3 条）再补一道：**展开态的高度不许落盘**。
                    //   面板里那张「运行监测」卡现在**默认折叠**（高 ≈270，展开则 ≈755）。
                    //   若把展开后的 755 记进配置，下次弹出就先按 755 摆、再缩到 270
                    //   —— 又变成「先闪出来一半」（这次是反过来）。所以页面在展开态
                    //   会带 `persist:false`，这里就**只用它摆窗口、不记进配置**：
                    //   配置里永远留「折叠态」那个高度，那才是每次弹出该用的数。
                    if panelOpen, (payload["persist"] as? Bool) != false {
                        var c = readConf(); c["panelH"] = Int(nh); writeConf(c)
                    }
                }
            }
        case "closePanel":
            hidePanel("页面 Esc / 关闭面板")
        case "openWindow":
            hidePanel("面板点「打开窗口」"); actOpen()
        case "settings":
            hidePanel("面板点「状态与设置」"); actSettings()
        case "refresh":
            // ★★ 面板自己的「刷新」**不再重载整页**（用户第 18 条：重载一次实测
            //   363~408ms，那正是"点一下卡一下"的手感）。改成叫页面**原地重拉一遍
            //   数据并重画** —— 服务端 /api/status 已预热（p50 0.4ms），看不出等待。
            if panelLoaded {
                panelWeb?.evaluateJavaScript(
                    "window.__panelRefresh && window.__panelRefresh()") { _, _ in }
                logLine("面板：刷新（原地重拉数据，不重载整页）")
            } else {
                // 页面压根没载起来 → 只能现载一次（否则点了刷新还是白板）。
                loadPanelPage("点刷新时页面还没载起来")
            }
        // ★★★ 2026-10-01（第四十七轮 · 用户第 1 条）：`case "stopService"` **已撤**
        //   —— 「关闭界面服务（保留守护）」这条半截路与「退出」合并了。
        //   现在面板底栏、主窗口顶栏、菜单栏下拉，三处都只有一个「退出」。
        case "quit":
            hidePanel("面板点「退出」"); actQuit()
        default:
            logLine("面板收到不认识的指令：\(cmd)")
        }
    }

    // 菜单拉开时刷新一次状态（★ 不挂定时器 —— 沿用「能不去轮询就不轮询」）。
    func menuWillOpen(_ menu: NSMenu) { refreshMenuStatus() }

    func menuDidClose(_ menu: NSMenu) { }

    func refreshMenuStatus() {
        // ① 先用**本地**事实（socket 探活，微秒级）把菜单填上 —— 拉菜单绝不卡。
        if !portAlive(port) {
            statHead?.title = "后台服务未运行（点「打开任务管家窗口」可拉起）"
            statLine?.title = ""
            statusItem?.button?.contentTintColor = .tertiaryLabelColor
            return
        }
        statusItem?.button?.contentTintColor = nil
        statHead?.title = "界面服务运行中 · 端口 \(port)"
        statLine?.title = "防休眠：读取中…"
        // ② 细节（WorkBuddy 在不在跑 / 防休眠开关）**异步**补上，菜单已经能用了。
        if menuFetching { return }
        menuFetching = true
        fetchStatus(timeout: 2.0) { [weak self] j in
            guard let self = self else { return }
            self.menuFetching = false
            self.applyStatus(j)
        }
    }

    func applyStatus(_ j: [String: Any]?) {
        guard let j = j else {
            statHead?.title = "界面服务运行中 · 端口 \(port)（状态读取超时）"
            statLine?.title = ""
            return
        }
        if let st = j["status"] as? [String: Any] {
            let wb = ((st["wb"] as? [String: Any])?["text"] as? String) ?? "?"
            statHead?.title = "WorkBuddy \(wb) · 界面服务运行中（端口 \(port)）"
        }
        guard let pw = j["power"] as? [String: Any] else { statLine?.title = ""; return }
        let on = (pw["enabled"] as? Bool) ?? false
        let busy = (pw["busy"] as? Bool) ?? false
        var s = "防休眠：" + (on ? "开" : "关")
        if on { s += busy ? " · 有任务在跑" : " · 暂无任务" }
        if let f = pw["features"] as? [[String: Any]] {
            var onList: [String] = []
            for x in f where (x["on"] as? Bool) == true {
                if let lab = x["label"] as? String { onList.append(lab) }
            }
            if !onList.isEmpty { s += "（" + onList.joined(separator: "、") + "）" }
        }
        statLine?.title = s
    }

    /// 拉一次 /api/status（异步，结果回主线程）。
    func fetchStatus(timeout: Double, done: @escaping ([String: Any]?) -> Void) {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/status") else {
            done(nil); return
        }
        var req = URLRequest(url: url)
        req.timeoutInterval = timeout
        req.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: req) { d, _, _ in
            var o: [String: Any]?
            if let d = d,
               let parsed = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                o = parsed
            }
            DispatchQueue.main.async { done(o) }
        }.resume()
    }

    // ---- 菜单动作 -------------------------------------------------------

    /// 把窗口弄出来；服务不在就先拉起来（在后台线程，别冻住菜单）。
    func showWindowEnsuringService() {
        saveGeometryForReopen()
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        if !portAlive(port) {
            statHead?.title = "正在拉起后台服务…"
            menuFetching = true
            DispatchQueue.global().async { [weak self] in
                guard let self = self else { return }
                let ok = ensureService(port: self.port)
                DispatchQueue.main.async {
                    self.menuFetching = false
                    if ok { self.reloadPage() } else { self.showFailPage() }
                    self.refreshMenuStatus()
                }
            }
        } else if webView.url == nil {
            reloadPage()
        }
    }

    func reloadPage() {
        let url = URL(string: "http://127.0.0.1:\(port)/?appwin=1&shell=1")!
        logLine("（菜单）加载 \(url.absoluteString)")
        webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData,
                                timeoutInterval: 20))
    }

    @objc func actOpen() { showWindowEnsuringService() }

    /// 打开窗口并点一下页面上那个按钮（菜单栏 = 同一个页面，不另做一套设置界面）。
    func clickInPage(_ js: String) {
        showWindowEnsuringService()
        // 页面可能刚 reload，等它把 DOM 铺好再点。多试几次，成功即止。
        tryClick(js, attempt: 0)
    }

    func tryClick(_ js: String, attempt: Int) {
        guard attempt < 8 else { logLine("（菜单）注入 JS 放弃：\(js)"); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + (attempt == 0 ? 0.6 : 0.5)) {
            [weak self] in
            guard let self = self, let wv = self.webView else { return }
            wv.evaluateJavaScript(js) { _, err in
                if err != nil {
                    self.tryClick(js, attempt: attempt + 1)     // 页面还没就绪，再试
                }
            }
        }
    }

    /// 打开窗口并切到「设置」页（菜单栏 = 同一个页面，不另做一套设置界面）。
    /// ★★ 2026-09-30（第廿七轮 · 用户第 4 条）：设置**不再是顶栏那颗按钮 + 整屏覆盖层**，
    ///   改成首页第 4 个分页。所以这里不再去 click 一个 id（`btn-set` 已删），
    ///   改成调页面暴露的 `__gotoTab('settings')`。
    ///   ⚠️ 页面还没加载好时那个函数是 undefined —— 必须**抛错**，
    ///      否则 evaluateJavaScript 认为成功，tryClick 的重试机制就白设了
    ///      （表现：点菜单里的「状态与设置」窗口开了却停在原来那一页）。
    @objc func actSettings() {
        clickInPage("(function(){ if (!window.__gotoTab) throw new Error('page not ready');"
                    + " window.__gotoTab('settings'); })()")
    }
    @objc func actRefresh()  { clickInPage("document.getElementById('btn-refresh').click()") }

    // ★★★ 2026-10-01（第四十七轮 · 用户第 1 条）：`actStopSvc()`（只停服务、
    //   外壳留着）**已整条删除** —— 与下面的 `actQuit()` 合并成唯一一条退出路径。
    //   此后「退出」是**真的退出整个程序**：窗口、Dock 图标、菜单栏图标一起消失。
    //   四条入口（面板底栏 / 主窗口顶栏 / 菜单栏下拉 / ⌘Q）全都落到 actQuit。

    /// 退出整个程序（窗口 + Dock 图标 + 菜单栏图标 + 后台界面服务）。
    ///
    /// ★ 入口有四个，都落到这里：
    ///   · 面板底栏那颗「退出」（面板 → `shellCmd('quit')` → panelCommand 的 case "quit"）
    ///   · 主窗口顶栏那颗「退出」（第四十七轮起主窗口也挂了 shell 通道）
    ///   · 菜单栏下拉的「退出任务管家」
    ///   · ⌘Q（标准 terminate: → applicationShouldTerminate）
    /// ★ 真正停服务那一步在 `applicationShouldTerminate` 里（`stopUiService(port)`），
    ///   ⌘Q / Dock 右键退出走的也是那一条 —— 所以这里只要 `NSApp.terminate` 就够，
    ///   不要在这里再停一次（会停两回）。
    @objc func actQuit() {
        let a = NSAlert()
        a.messageText = "退出任务管家？"
        a.informativeText = "窗口、Dock 图标、菜单栏图标都会消失，"
            + "后台界面服务（端口 \(port)）也会一起停掉。\n"
            + "「运行监测」只收掉上次开机留下的一次性项（跑完自动关机 / 到点自动结束）；"
            + "「防休眠」等开关**保持不动** —— 开着就继续在后台守着。\n"
            + "三条同步守护（记忆线 / 跨机任务线 / 本机任务线）不受影响，仍照常运行。\n\n"
            + "只想关掉窗口、让程序留在后台继续跑：按 ⌘W 或点窗口的 ✕。"
        a.addButton(withTitle: "退出")
        a.addButton(withTitle: "取消")
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertFirstButtonReturn else { return }
        logLine("用户点了「退出」→ 停服务并退出外壳（菜单栏图标一起消失）")
        NSApp.terminate(nil)
    }

    /// 收窗口前先记一次几何（用户可能刚拖过）。
    func saveGeometryForReopen() { saveGeometry() }

    func hideWindow() {
        saveGeometry()
        window?.orderOut(nil)
    }

    // ---- 生命周期：⌘Q = 退出（顺带停服务）；关窗 = 不退出 ------------------

    /// ★★ ⌘Q / 应用菜单「退出」/ Dock 右键「退出」都会走到这儿。
    ///   【2026-09-29 晚，用户二次指定】改成**真的退出**，并且**把界面服务一起停掉**
    ///   （用户原话：「直接退出，停用服务」）。原先是「拦下、只收窗口」。
    ///   ⚠️ 停服务走的就是页面那颗「关闭服务」的同一条路（POST /api/quit）——
    ///      它收掉「上次开机留下的一次性附加项」（那是用户 2026-09-28 定的语义：
    ///      「关闭服务之后不要让『跑完自动关机』还开着」），
    ///      ★★★ 但 2026-10-01（第廿九轮）起**不再归零「防休眠」等 5 个开关** ——
    ///      那 5 个是用户的长期姿势，关服务/退出后必须原样保持。
    ///      三条守护不受影响。
    ///   ★ 关窗（✕ / ⌘W）**不退出**，由 applicationShouldTerminateAfterLastWindowClosed 管。
    func applicationShouldTerminate(_ s: NSApplication) -> NSApplication.TerminateReply {
        logLine("退出请求（⌘Q / 菜单 / Dock）→ 先停界面服务，再退出外壳")
        stopUiService(port)
        return .terminateNow
    }

    /// ★ 关掉最后一个窗口**不退出**进度（2026-09-29 改；原来返回 true）。
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows f: Bool) -> Bool {
        // 点 Dock 图标也走同一条路：确保服务在跑、再把窗口端出来。
        showWindowEnsuringService()
        return true
    }

    // 页面里 window.close()（点「关闭服务」时会走这条）→ 关掉窗口。
    func webViewDidClose(_ webView: WKWebView) {
        logLine("页面请求关窗")
        saveGeometry()                  // 关之前记一次最后几何
        window?.performClose(nil)
    }

    // target=_blank / window.open → 就在本窗口里打开，不另开无地址栏的怪窗口。
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = navigationAction.request.url { webView.load(URLRequest(url: u)) }
        return nil
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        logLine("加载失败：\(error.localizedDescription)")
    }

    /// ★★★ 2026-10-01（第五十三轮 · 用户「界面消失不见了，再解决一下」）。
    ///   页面**载了一半断掉**（服务正好在重启、连接被掐）→ 那一页就是一张白板，
    ///   而且 `didFinish` 不会再来 ⇒ 面板的 `panelLoaded` 永远停在 false、
    ///   主窗口也没人管它。这里认账并重载一次。
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!,
                 withError error: Error) {
        let which = (webView === panelWeb) ? "面板页" : "主窗口页"
        logLine("加载中断（\(which)）：\(error.localizedDescription)")
        if webView === panelWeb {
            panelLoaded = false
            panelLoading = false            // 放开，好让下一次点击/预载能再载
            if panelOpen { loadPanelPage("加载中断后重试") }
        } else if mainStuck < 3 {
            mainStuck += 1
            logLine("→ 重载主窗口页（第 \(mainStuck) 次）")
            webView.reload()
        }
    }

    /// ★★★ 2026-10-01（第五十三轮）：**WebKit 内容进程被终止**（崩溃 / 被系统回收）。
    ///
    ///   这是「界面突然整个变成白板、而且再也回不来」的头号原因：
    ///   **同一个 App 里的所有 WKWebView 默认共用一个 WebContent 进程** ——
    ///   它一没，**主窗口和菜单栏面板会在同一瞬间一起变白**（用户看到的就是
    ///   「界面消失不见了」），而 WKWebView **自己不会做任何事**：
    ///   它既不重载、也不报错，页面的 DOM 整个没了，`didFinish` 也不会再来。
    ///   于是外壳这边 `panelLoaded` 还是 true → `showPanel` 永远不会再载它
    ///   （它只在 `!panelLoaded && !panelLoading` 时才重载）⇒ 白板成了永久状态。
    ///   过去外壳**没有实现这个方法**，所以这条路一次都没被兜住。
    ///
    ///   ⚠️ 它会对**两个 web view 各回调一次**（同一条命），所以两边各自重载一次，
    ///      正合适。⚠️ 但必须封顶：进程要是「一载就崩」，这里会变成崩-重载-再崩的死循环。
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        pageProcKill += 1
        let which = (webView === panelWeb) ? "面板页" : "主窗口页"
        logLine("⚠️ WebKit 内容进程被终止（\(which)，累计第 \(pageProcKill) 次）→ 立刻重载")
        guard pageProcKill <= 5 else {
            logLine("→ 内容进程反复被终止（>5 次）→ 停止自动重载，避免死循环；请看 _界面.log")
            return
        }
        if webView === panelWeb {
            loadPanelPage("内容进程被终止")
        } else {
            webView.reload()
        }
    }

    /// 主窗口页载完之后**问一声**（只问一次，不是轮询）：
    ///   内容进程「还活着但卡死」（页面的 JS 死循环 / 被同步 API 堵住）时，
    ///   `webViewWebContentProcessDidTerminate` 不会来 —— 只能自己探一次。
    ///   `evaluateJavaScript` 在页面卡死时会**超时回一个 error**，正好当判据。
    ///   ⚠️ 只探一次、不挂定时器：用户明确反感「后台一直挂着一个监测」。
    func probeMainAlive() {
        mainProbeToken += 1
        let token = mainProbeToken
        DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) { [weak self] in
            guard let self = self, self.mainProbeToken == token, let wv = self.webView else { return }
            wv.evaluateJavaScript("1+1") { [weak self] _, err in
                guard let self = self, self.mainProbeToken == token else { return }
                if err == nil { self.mainStuck = 0; return }      // 活着 → 一笔勾销
                logLine("⚠️ 主窗口页无响应：\(err?.localizedDescription ?? "?")")
                if self.mainStuck >= 3 {
                    logLine("→ 已达重载上限（3 次）→ 不再自动重载；请看 _界面.log")
                    return
                }
                self.mainStuck += 1
                logLine("→ 重载主窗口页（第 \(self.mainStuck) 次）")
                wv.reload()
            }
        }
    }

    /// ★★ 2026-09-29（用户第 18 条）：面板页载完了 → 记下「可以用」，并在面板**收着**时
    ///   让它睡（停掉 3 秒轮询）。
    ///   为什么要睡：面板是在启动时**预载**的（见 preloadPanel），不睡的话它会和主窗口
    ///   一样每 3 秒拉一次 /api/status —— 白白多烧一份电，而它 99% 的时间是收着的。
    ///   `panelLoaded` 是「面板能不能直接端上来」的唯一判据（showPanel 用它决定要不要
    ///   现载），所以只能在这里置 true：`load()` 一发出去 url 就有了，那种"有 url
    ///   但页面还是一片白"的状态拿不得。
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard webView === panelWeb else {
            /* ★★★ 2026-10-01（第五十三轮）：主窗口页载完 → 探一次它还活着没有。
               以前这里直接 `return`，主窗口页**从头到尾没人管** ——
               它变成白板之后，只能靠用户自己发现「界面没了」再重启整个 App。 */
            probeMainAlive()
            return
        }
        panelLoaded = true
        panelLoading = false
        panelStuck = 0                      // 新的一页来了 → 欠账清零
        logLine("面板页已就绪（\(panelOpen ? "正显示" : "收着，转入休眠")）")
        if !panelOpen { panelPageSleep() }
    }

    func showFailPage() {
        let html = """
        <html><head><meta charset="utf-8"><style>
        body{font:15px/1.7 -apple-system,"PingFang SC";padding:40px;color:#333}
        code{background:#f2f2f2;padding:2px 6px;border-radius:4px}
        </style></head><body>
        <h2>界面服务没起来</h2>
        <p>后台小服务（<code>wb_ui.py</code>）没能启动，所以窗口里没有内容。</p>
        <p>排障顺序：</p>
        <ol>
          <li>看日志：<code>\(tooldisp())_界面.log</code></li>
          <li>在「终端」里手工跑一次：<br><code>cd "\(tooldisp())" &amp;&amp; /usr/bin/python3 wb_ui.py --serve</code></li>
          <li>把上面那行报的错修掉（多数是端口被占或界面文件缺失）。</li>
        </ol>
        <p>修好后把这个窗口关掉，再双击一次「WorkBuddy任务管家」即可。</p>
        </body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    func tooldisp() -> String { TOOL_DIR + "/" }
}

// ------------------------------------------------------------------ 快捷键自测
//
// ★★ 为什么非要有这一段（2026-09-29 的教训）
//   「⌘Q 已经被拦下、实测过」这句话是**错的** —— 那次验的是 `osascript quit`
//   （AppleEvent 直接进 applicationShouldTerminate），**绕过了键盘派发**。
//   而真按键在没给辅助功能权限的环境里根本发不出去
//   （System Events 直接报「osascript 不允许发送按键」(1002)）——
//   于是「快捷键是死的」这件事一直没被照出来，直到用户自己按了发现没反应。
//
//   这里给一条**不依赖任何权限**的自测通道：自己构造 NSEvent，
//   丢给 `NSApp.mainMenu.performKeyEquivalent(with:)` —— 这正是 AppKit 处理真按键时
//   走的那一步（`sendEvent` → 主菜单 `performKeyEquivalent`），只是把
//   「谁会收到这个事件」那段绕开了。所以：
//     · `probe` 查得到菜单项 ⇔ 真按键有接收者（少了主菜单就会 ✗）
//     · `press` 打出「命中菜单项」⇔ 动作真的会被执行
//
// 用法（直接跑 app 包里那个可执行文件，**不是** open）：
//   WB_SHELL_KEYTEST=probe "/Applications/WorkBuddy任务管家.app/Contents/MacOS/WorkBuddyShell"
//   WB_SHELL_KEYTEST=w,h "/Applications/WorkBuddy任务管家.app/Contents/MacOS/WorkBuddyShell"

/// 静态核对：每个常用键位到底有没有落在某个菜单项上（只读，不动手）。
func probeKeys() -> String {
    let combos = [("q", "退出"), ("w", "关窗口"), ("h", "隐藏"), ("r", "重载"),
                  ("m", "最小化"), ("c", "拷贝"), ("v", "粘贴"), ("a", "全选"),
                  (",", "设置"), ("+", "放大"), ("-", "缩小"), ("0", "实际大小")]
    let flat = (NSApp.mainMenu?.items ?? []).flatMap { $0.submenu?.items ?? [] }
    var out: [String] = []
    for (k, _) in combos {
        let hit = flat.first {
            $0.keyEquivalent.lowercased() == k && $0.keyEquivalentModifierMask.contains(.command)
        }
        out.append("⌘\(k.uppercased())" + (hit != nil ? "→\(hit!.title)" : "→✗没有菜单项"))
    }
    return out.joined(separator: " ｜ ")
}

/// 真的派发一个 ⌘ 键（走 AppKit 的同一条路）。⚠️ 测 ⌘Q 会真的退出。
func keyTest(_ combo: String) {
    let key = combo.trimmingCharacters(in: .whitespaces).lowercased()
    guard !key.isEmpty, key.count == 1 else {
        logLine("快捷键自测：不认识的组合「\(combo)」")
        return
    }
    let win = NSApp.keyWindow ?? NSApp.windows.first
    guard let ev = NSEvent.keyEvent(with: .keyDown, location: .zero,
                                    modifierFlags: [.command], timestamp: 0,
                                    windowNumber: win?.windowNumber ?? 0, context: nil,
                                    characters: key, charactersIgnoringModifiers: key,
                                    isARepeat: false, keyCode: 0) else {
        logLine("快捷键自测：造不出 ⌘\(key.uppercased()) 的事件")
        return
    }
    logLine("快捷键自测：派发 ⌘\(key.uppercased()) …")
    let hit = NSApp.mainMenu?.performKeyEquivalent(with: ev) ?? false
    logLine("快捷键自测：⌘\(key.uppercased()) → "
            + (hit ? "命中菜单项（真按键也走这条路）" : "✗ 没有任何菜单项接住"))
}

// ------------------------------------------------------------------ 网页对话框
//
// ★★ 这一段**必须有**，否则页面上那些按钮会「点了没反应」。
//
//   WKWebView **默认不实现** window.alert / confirm / prompt：WebKit 会去找
//   `uiDelegate` 要回调，**找不到就直接回调 false**（等于用户点了「取消」），
//   而且**不报任何错、控制台一个字都没有**。
//
//   页面（`_ui.html`）里有三处依赖它，全都是关键按钮：
//     · 「关闭服务」   → `if (!window.confirm(...)) return;`  ⇒ 永远直接 return
//     · 「跑完自动关机」→ 后端首次返回 `needConfirm`，页面 confirm 一下再重发
//                        ⇒ confirm 恒 false ⇒ 什么都不发生
//     · 所有标「会改数据」的卡片（2 / 16 / 18 号那些）⇒ 同样永远不执行
//
//   Windows 端用的是 Chromium 的 `--app=` 窗口，**自带**这些对话框，
//   所以在 Win 上一直好好的 —— 这个坑只有 Mac 的 WKWebView 外壳会踩。
//
//   按钮顺序：**「确定」在前、「取消」在后** —— 与 Chromium 的 `confirm()`
//   （确定/取消、默认确定）**逐字一致**，这样同一段页面代码在两端的表现相同。
//   NSAlert 把第一个按钮当默认按钮（回车触发），所以回车 = 确定，与网页里
//   按回车确认的行为也对得上。（这些都是用户点按钮之后才弹的框，不是意外触发。）

extension Shell {

    /// 统一的弹窗落地：能挂 sheet 就挂 sheet，否则退回应用模态。
    private func presentPanel(_ message: String,
                              buttons: [String],
                              accessory: NSView? = nil,
                              completion: @escaping (NSApplication.ModalResponse) -> Void) {
        let a = NSAlert()
        a.messageText = APP_TITLE
        a.informativeText = message
        a.alertStyle = .warning
        for t in buttons { a.addButton(withTitle: t) }
        if let v = accessory {
            a.accessoryView = v
            a.window.initialFirstResponder = v
        }
        NSApp.activate(ignoringOtherApps: true)
        if let w = (NSApp.keyWindow ?? window), w.isVisible {
            a.beginSheetModal(for: w) { resp in completion(resp) }
        } else {
            completion(a.runModal())          // 兜底：窗口还没出来 / 已关掉
        }
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        presentPanel(message, buttons: ["好"]) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        presentPanel(message, buttons: ["确定", "取消"]) { resp in
            completionHandler(resp == .alertFirstButtonReturn)
        }
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.stringValue = defaultText ?? ""
        presentPanel(prompt, buttons: ["确定", "取消"], accessory: field) { resp in
            completionHandler(resp == .alertFirstButtonReturn ? field.stringValue : nil)
        }
    }
}

// ------------------------------------------------------------------ 提权桥
//
// ★★ 2026-09-29（用户反复报「点合盖后保持唤醒就弹密码框」的根治）
//
// 病根：WorkBuddy 会话/工具进程带 **seatbelt 沙箱**。在沙箱里 exec
//   /usr/bin/sudo 会被内核直接拒（PermissionError: Operation not permitted），
//   `launchctl submit` 也失败（5: I/O error），`launchctl bootstrap` 同样。
//   过去 `_lid_write_sudo` 三步走完都失败 → 退到 AppleScript 的
//   「系统授权框」，于是**每次点那个开关都弹一次密码**。
//
// 出口：`open -n -g -a WorkBuddy任务管家 --args --sudo-once ...`
//   —— 由 LaunchServices/launchd 拉起的**新实例不继承调用方的沙箱**，
//   这里的 sudo -n 能正常命中 /etc/sudoers.d/workbuddy-lid 的免密白名单。
//
// 这一支**必须在 NSApplication 起来之前**返回：不开窗口、不拉服务、
// 不加菜单栏图标、不留进程。
//
// 参数：<kind> <val> <结果文件路径>；结果是一个小 JSON。
// ⚠️ 只用 `sudo -n`（非交互）—— 白名单没命中就**立刻失败**，
//    绝不在这里弹任何授权框（那正是要根治的东西）。

/// 跑一条命令，返回 (退出码, 合并 stdout+stderr 并去掉首尾空白)
func runCmd(_ launch: String, _ args: [String]) -> (Int32, String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: launch)
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = pipe
    do {
        try p.run()
    } catch {
        return (-1, "无法执行 \(launch)：\(error.localizedDescription)")
    }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    let s = String(data: data, encoding: .utf8) ?? ""
    return (p.terminationStatus, s.trimmingCharacters(in: .whitespacesAndNewlines))
}

/// 写结果文件（小 JSON；把会破坏 JSON 的字符折掉，调用方直接 json.loads）
func writeSudoResult(_ res: String, _ rc: Int32, _ msg: String) {
    guard !res.isEmpty else { return }
    var safe = msg
    for (a, b) in [("\"", "'"), ("\\", "/"), ("\n", " "), ("\r", " "), ("\t", " ")] {
        safe = safe.replacingOccurrences(of: a, with: b)
    }
    if safe.count > 400 { safe = String(safe.prefix(400)) }
    let json = "{\"rc\":\(rc),\"msg\":\"\(safe)\",\"who\":\"shellbridge\","
             + "\"at\":\(Int(Date().timeIntervalSince1970))}\n"
    try? json.write(toFile: res, atomically: true, encoding: .utf8)
}

// ------------------------------------------------------ 守护自愈（实现已外移）
//
// ★★ 2026-09-29（用户反复报「记忆线为什么总掉」的根治）
//
// 病根：`wb_repair.py` 第 6 步「同步线」会跑 `wb_sync_ctl.py --up all`
//   → `agent_bootout()` **先把 LaunchAgent 卸掉** → `agent_bootstrap()` **恒失败**
//     （工具进程写不了 launchd 域：`Bootstrap failed: 5: Input/output error`）
//   → 本来好好在跑的守护被拔掉、**再也装不回来** → 记忆线掉了。
//   而 `--up` 被「部署和修复」「一键整合」「体检自愈」多处调用 → **反复发生**。
//
// 出口与提权桥同源：本 app 由 LaunchServices/launchd 拉起、在**干净上下文**
//   （有 GUI 会话），`launchctl bootstrap` 在这里能成功。
//
// ★★★ 2026-10-01（第四十八轮 · 用户第 2 条「外壳 · 守护自愈同理如上」）：
//   **实现整体搬到 `wb_guard.py`**（launchd 按事件拉起、跑完即退的一次性脚本）。
//   外壳这里只留一个**薄封装** `guardFix()`：桥（kind=guard）要「马上补一次」
//   时调它，它转发给 `wb_guard.py --fix` —— **一处实现，两条路（launchd 事件 /
//   提权桥）共用**。于是外壳里再没有任何自愈定时器（`guardTimer` /
//   `scheduleGuardTick` 已删），`GUARD_BACKOFF`/`GUARD_STALL`/`GUARD_IDLE_*`
//   也随之下线 —— 退避与心跳阈值现在都住在 `wb_guard.py` 里。
//
// ⚠️ 三条铁律（都在 `wb_guard.py` 里落实）：
//   ① **尊重用户意图**：`<plist>.disabled`（彻底停用）或 `_pause_<key>`（暂停）
//      在时**一律不碰** —— 自愈绝不能把用户主动关掉的东西又拉起来；
//   ② 失败要**退避**（5 分钟），别刷日志；
//   ③ 只认下面这张**写死的表**，绝不接任意命令
//      （`_sudo_req.json` 本机任何进程都能写）。

// ★★★ 2026-10-11（拆家）：这里原来住着「三条同步线（记忆线 / 会话线 / 任务线）的
//   守护自愈」—— `GUARD_KEYS` / `guardFix()` / `ensureGuardAgent()` / `guardPython()`。
//   那是**任务管家**的职能，本程序（时间守护）只做电源/鼠标/键盘，所以整块删除：
//     · 它的实现与 LaunchAgent（com.wb.workbuddy-guard）由任务管家自己安装维护；
//     · 本程序不再代跑、也不再写那条 plist。
//   （历史上它是「一处实现、两条路：launchd 事件 / 提权桥」，现在两条都归任务管家。）

/// 执行一条已在免密白名单里的命令，返回 (退出码, 输出)。
/// ⚠️ 只用 `sudo -n`（非交互）—— 白名单没命中就**立刻失败**，
///    绝不在这里弹任何授权框（那正是要根治的东西）。
func runSudoKind(_ kind: String, _ val: String) -> (Int32, String) {
    switch kind {
    case "guard", "diag":
        // ★ 2026-10-11（拆家）：这两条都是「三条同步线」的活（补齐/只读诊断），
        //   属任务管家 —— 本程序不再代跑。
        return (0, "（本程序不负责 WorkBuddy 同步线；请在任务管家里操作）")
    case "autostart":
        // ★★ 2026-09-30（用户第 2 条）：开机自启。
        //   plist 的**写与删由界面服务（Python）负责**（纯文件操作，那棵树里也能干）；
        //   这里只负责让 launchd **立刻**认账 —— bootout / bootstrap 在本进程
        //   （由 launchd 拉起、干净上下文）能成，在工具进程里恒报 `5: I/O error`（见 §18）。
        //   ⚠️ 就算这一步失败，开关的**本义**（下次登录自动运行）也已经由那份 plist 保证了，
        //      所以 Python 那边只把它当「提示」、不当失败。
        // ★ 2026-10-11（拆家）：本程序的自启 label 是 timeguardian —— 原来写的是
        //   任务管家的 label，等于替它 bootout/bootstrap（串了家）。
        let label = "com.wb.timeguardian-mac"
        let plist = NSHomeDirectory() + "/Library/LaunchAgents/\(label).plist"
        let duid = getuid()
        var msgs: [String] = []
        let (rc0, _) = runCmd("/bin/launchctl", ["bootout", "gui/\(duid)/\(label)"])
        msgs.append("bootout=\(rc0)")           // 本来就没装着时也会非 0，无所谓
        if val == "on" {
            if !FileManager.default.fileExists(atPath: plist) {
                return (3, "找不到 \(plist)（plist 应该由界面服务先写好）")
            }
            let (rc, out) = runCmd("/bin/launchctl", ["bootstrap", "gui/\(duid)", plist])
            msgs.append("bootstrap=\(rc)\(out.isEmpty ? "" : " \(out.prefix(120))")")
            if rc != 0 {
                // 已经装着的时候 bootstrap 会报 "service already loaded" —— 那也算成。
                let low = out.lowercased()
                if low.contains("already") || low.contains("already loaded") {
                    return (0, msgs.joined(separator: " | "))
                }
                return (rc, msgs.joined(separator: " | "))
            }
        }
        return (0, msgs.joined(separator: " | "))
    case "disablesleep":
        guard val == "0" || val == "1" else { return (2, "取值只能是 0 或 1（收到 \(val)）") }
        let (rc, out) = runCmd("/usr/bin/sudo",
                               ["-n", "/usr/bin/pmset", "-a", "disablesleep", val])
        return (rc, out.isEmpty ? (rc == 0 ? "已设置 disablesleep=\(val)" : "sudo 退出码 \(rc)") : out)
    case "pbs":
        // ★★ 2026-10-01（第四十八轮）：让新装的「快速操作」**立刻**出现在访达右键菜单。
        //   病根：`pbs -update` 要连的是**用户图形会话**里的注册服务，
        //   而工具进程（界面服务那棵树）跑在 seatbelt 沙箱里 →
        //   恒报 `XPC message error: Connection interrupted`（实测）。
        //   本 app 由 launchd 在 gui/<uid> 会话里拉起、**不在沙箱**，只有它跑得动
        //   —— 与 `launchctl bootstrap` 同一个道理（见 §18）。
        //   ⚠️ 跟着 `killall Finder` 重启访达：桌面图标会闪一下、访达窗口重排，
        //      这是让右键菜单立刻刷新最直接的办法；不做就得等下次登录。
        // ★★ 2026-10-02（第六十六轮）：这一步现在必须干**四件事**，缺任何一件，
        //   用户看到的都是同一句话：「右键菜单里那几项没了」。而且它横跨两套机制 ——
        //     ① FinderSync 右键扩展（appex，寄生在 PlugIns 里，提供合并PDF/图片转PDF 等）
        //        → 靠**访达重启**重挂；且必须**先处于启用态**，禁用时重启访达也白搭
        //        （唯一入口是 `pluginkit -e use`）。
        //     ② 传统「快速操作」(.workflow，在 ~/Library/Services)
        //        → 靠 `pbs -update` 重新注册。
        //   ⚠️ 用户 2026-10-02 报的缺口正在这里：外壳当时只做了 ②③，没做 ①，
        //      而真正坏的是 ① —— 于是「点了按钮甚至都不管用」。
        var parts: [String] = []
        var bad = false

        // ① 先确认扩展还在包里。重建外壳（`rm -rf`）最容易把它带走，
        //    症状只有「右键里那几项没了」，不报任何错 —— 必须让这一条显式露出来。
        let extExec = "/Applications/WorkBuddy任务管家.app/Contents/PlugIns/WorkBuddy右键扩展.appex/Contents/MacOS/WBRCFinderSync"
        if FileManager.default.fileExists(atPath: extExec) {
            parts.append("右键扩展在包内 ✓")
        } else {
            parts.append("⚠️ 右键扩展不在包内（需先跑 _右键扩展/_build.sh 重建）")
            bad = true
        }

        // ② 把它**启用**：禁用状态下访达根本不会挂它的菜单。
        let (rcE, outE) = runCmd("/usr/bin/pluginkit",
                                 ["-e", "use", "-i", "com.wb.wbqa.rightclick.ext"])
        if rcE != 0 { bad = true }
        parts.append("启用右键扩展 rc=\(rcE)\(outE.isEmpty ? "" : " \(outE.prefix(80))")")

        // ③ 传统快速操作重新注册。`pbs -update` 连的是**用户图形会话**里的注册服务，
        //    而界面服务那棵树跑在 seatbelt 沙箱里 → 恒报 XPC 错；只有本 app 跑得动。
        let (rc1, out1) = runCmd("/System/Library/CoreServices/pbs", ["-update"])
        if rc1 != 0 || out1.lowercased().contains("xpc")
            || out1.lowercased().contains("interrupt") { bad = true }
        parts.append("pbs rc=\(rc1)\(out1.isEmpty ? "" : " \(out1.prefix(160))")")

        // ④ 重启访达：让 FinderSync 扩展**立刻**重新挂上。
        //    ⚠️ 必须排在 ② 之后 —— 先启用再重启；顺序反了就白重启一次。
        let (rc2, out2) = runCmd("/usr/bin/killall", ["Finder"])
        parts.append("访达重启 rc=\(rc2)\(out2.isEmpty ? "" : " \(out2.prefix(120))")")

        return (bad ? 1 : 0, parts.joined(separator: " | "))
    case "tccreset":
        // ★★ 2026-10-01（第卅九轮）：清掉 TCC 里那条「针对本 app 的辅助功能授权」。
        //   为什么需要它：那条授权的**身份**就是代码签名要求（DR）。app 从 ad-hoc
        //   换成自签证书之后，旧记录里的 DR 与当前签名对不上了 —— 症状正是
        //   「系统设置里开关明明是蓝的、程序却说没权限」；而且**把开关关掉再打开
        //   也常常不好使**（系统按 bundle id 命中了那条旧记录，只翻转了开关位，
        //   没有重算 DR）。`tccutil reset` 是唯一能把它**彻底删掉**、
        //   逼系统在下次请求时重新认一遍的动作。
        //   ⚠️ 为什么必须走桥：本机工具进程都跑在 seatbelt 沙箱里，`tccutil` 一律报
        //      `Operation not permitted from sandbox`（rc=77，实测）；
        //      本 app 由 launchd 拉起、**不在沙箱**，所以只有它跑得动。
        //   ⚠️ 清掉之后**需要用户重新勾一次** —— 这正是目的：那条旧记录已经无效，
        //      重来一遍反而干净。下一次界面上的请求会带系统弹框，由系统指路。
        //   ★★ **这是排障通道，界面上刻意没有入口。** 用户明确反对"给个按钮让他
        //      反复授权" —— 那等于承认这东西会反复坏，是掩盖问题而不是解决问题。
        //      正常路径下它一辈子只会被用到**一次**：即"签名身份换代"那一次
        //      （ad-hoc → 自签证书，见 §40）。身份一旦固定，授权就一次到位、
        //      以后重编换图标都不再失效。调用方式：写 `_sudo_req.json`
        //      （kind=tccreset，val 留空即本 app）或直接 `bridge_call('tccreset')`。
        let tb = val.isEmpty ? (Bundle.main.bundleIdentifier ?? "com.wb.workbuddy-toolkit") : val
        let (rc, out) = runCmd("/usr/bin/tccutil", ["reset", "Accessibility", tb])
        return (rc, out.isEmpty
                ? (rc == 0
                   ? "已清掉 TCC 里 \(tb) 的辅助功能记录 —— 接下来需要重新勾一次"
                   : "tccutil 退出码 \(rc)")
                : out)
    case "probe":
        // 只读体检：免密白名单长什么样、sudo 在这个上下文能不能用。
        let (_, out) = runCmd("/bin/bash", ["-c",
            "{ echo uid=$(/usr/bin/id -u); echo '--- sudo -n -l ---'; "
            + "/usr/bin/sudo -n -l 2>&1; echo \"rc=$?\"; }"])
        return (0, out)
    default:
        return (2, "不认识的命令种类：\(kind)")
    }
}

func runSudoOnce(_ args: [String]) {
    let kind = args.count > 0 ? args[0] : ""
    let val  = args.count > 1 ? args[1] : ""
    let res  = args.count > 2 ? args[2] : ""
    let (rc, out) = runSudoKind(kind, val)
    writeSudoResult(res, rc, out)
}

// ------------------------------------------------------- 提权请求文件（主通道）
//
// ⚠️ 为什么不用命令行参数（`open -n -a App --args ...`）：
//    实测（2026-09-29）：对**已在运行的**同 bundle id 的 app，`open -n` 只是
//    "再启一个实例"，`--args` **传不进去**（新实例的 CommandLine 里没有它们）。
//    所以改用**请求文件**交接：调用方写 JSON，本 app 读走、执行、写回结果。
//
// 两条入口：
//   ① 冷启实例（`open -n -g -a` 或用户双击）：起来先看有没有请求，有就处理完
//      **直接退出，不开窗口**；
//   ② 常驻实例：**内核文件事件**驱动（见 Shell.armSudoWatch）。
//      ★★★ 2026-10-01（自检轮 · 用户第 1 条）：这里是 `Timer(1.0)` 常驻轮询，
//      每秒醒一次。已改成 `DispatchSource` 文件事件 —— 空闲零唤醒，响应还更快。

// ★ 文件名**一律纯 ASCII**：诊断/录日志时不用跟编码较劲（2026-09-29 晚定）。
let SUDO_REQ = TOOL_DIR + "/_sudo_req.json"
let SUDO_INVITE = TOOL_DIR + "/_sudo_invite"   // 「拉临时实例来干活」的邀请标记
let SUDO_TTL: TimeInterval = 30                // 比这还旧的请求，当陈旧丢弃
let SUDO_INVITE_TTL: TimeInterval = 15         // 邀请的新鲜期（够冷启起来看它一眼就行）

/// 请求文件里**还有没有活**（判据＝能不能解析出非空的 `kind`）。
///
/// ★★★ 2026-10-01（改成事件驱动时一并改的）：原来判「有没有请求」是看
///   **文件在不在**（`fileExists`）—— 因为办完就把文件删了。现在办完是把内容
///   清成 `{}`（**文件留着**，好让文件事件源的 fd 一直有效），所以「在不在」
///   不再等于「有没有活」，必须看内容。
///   ⚠️ 冷启兜底那句「文件不在 且 有邀请 → 直接退出」也一并改成看这个：
///      不然被 `open` 拉来干活的临时实例会以为「文件在 ＝ 有活」→
///      **照样开窗口常驻**（旧版涨到 7 个实例那个坑）。
func sudoReqPending() -> Bool {
    guard let d = FileManager.default.contents(atPath: SUDO_REQ),
          let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any],
          let k = o["kind"] as? String, !k.isEmpty else { return false }
    return true
}

/// 把请求文件**就地**清成空（`{}`）。
/// ⚠️ 刻意**不用**原子替换：`os.replace` 会换 inode，把正盯着它的文件事件源废掉；
///    这里必须原地写（`Data.write` 不带 `.atomic` = open(O_TRUNC)+write，inode 不变）。
func clearSudoReq() {
    try? Data("{}".utf8).write(to: URL(fileURLWithPath: SUDO_REQ))
}

/// 有新鲜的提权请求就处理掉。返回 true = 刚处理了一条。
///
/// 抢占靠一个独立的锁文件（`O_CREAT|O_EXCL` 是原子的）：谁建出来谁负责，
/// 其余实例建不出来就放手 —— 多实例不会重复执行同一条。
func handleSudoRequestIfAny() -> Bool {
    let fm = FileManager.default
    guard let data = fm.contents(atPath: SUDO_REQ) else { return false }
    guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
        // 调用方是「原子写」的（先写 .tmp 再 rename），理论上读不到半截；
        // 真碰上就等下一轮，**绝不删**（删了就等于把这条请求吞掉）。
        logLine("[提权桥] 请求文件解析不出来（可能正在写入），等下一轮")
        return false
    }

    if ((obj["kind"] as? String) ?? "").isEmpty { return false }   // 空的 `{}` ＝ 没有待办

    let at = (obj["at"] as? NSNumber)?.doubleValue ?? 0
    if at > 0 && Date().timeIntervalSince1970 - at > SUDO_TTL {
        logLine("[提权桥] 请求太旧（\(Int(Date().timeIntervalSince1970 - at)) 秒前），丢弃")
        clearSudoReq()
        return false
    }

    // ★ 原来靠「把请求文件改名成 .busy」抢占；现在文件必须留在原地给事件源盯着，
    //   所以换成一个独立的锁文件（建得出＝抢到）。
    let claim = SUDO_REQ + ".claim"
    let lockFd = open(claim, O_CREAT | O_EXCL | O_WRONLY, 0o600)
    if lockFd < 0 { return false }                                 // 被别的实例抢走了
    defer {
        close(lockFd)
        try? fm.removeItem(atPath: claim)
    }
    // 抢锁这一瞬间内容可能已被换掉 → 再读一次**当下**的值。
    guard let d2 = fm.contents(atPath: SUDO_REQ),
          let o2 = (try? JSONSerialization.jsonObject(with: d2)) as? [String: Any],
          let kind = o2["kind"] as? String, !kind.isEmpty else { return false }

    let val = (o2["val"] as? String) ?? ""
    let res = (o2["res"] as? String) ?? ""
    clearSudoReq()                       // 先清空：办完之前若再来一次，不会再走一遍
    let (rc, out) = runSudoKind(kind, val)
    writeSudoResult(res, rc, out)
    logLine("[提权桥] 办完 kind=\(kind) val=\(val) rc=\(rc)")
    return true
}

/// 本实例是不是「被 open 拉来干一次活」的临时实例？
///
/// ★★ 为什么需要这个（2026-09-29 晚踩到）：`_poll` 超时后的冷启兜底会
///   `open -n -g` 拉一个新实例；可**如果请求已经被别的实例办完**，新实例就
///   找不到活儿，于是**照常开窗口常驻** —— 试一次多一个窗口，越试越乱
///   （实测涨到 7 个实例，还把桥拖成 20 秒超时）。
///   邀请文件由调用方在 `open` 前写下、拿到结果后删掉，只有在这段窗口期里
///   才新鲜；所以**用户正常双击几乎不可能被误吃掉**。
func sudoInviteFresh() -> Bool {
    let fm = FileManager.default
    guard let a = try? fm.attributesOfItem(atPath: SUDO_INVITE),
          let d = a[.modificationDate] as? Date else { return false }
    return Date().timeIntervalSince(d) < SUDO_INVITE_TTL
}

// ------------------------------------------------------------------ main

// ★ 提权桥（两条入口）：
//   ① 命令行 `--sudo-once`：给自检脚本直接 exec 二进制用
//      （`open --args` 传不进来，见上面「提权请求文件」段的说明）；
//   ② 请求文件：**主通道**。谁先读到谁处理，处理完直接走 —— **不开窗口**。
if let i = CommandLine.arguments.firstIndex(of: "--sudo-once") {
    runSudoOnce(Array(CommandLine.arguments.dropFirst(i + 1)))
    exit(0)
}
// ★ 平滑滚动自测（第 11 套回归用它）：只查可确定核对的部分，不碰鼠标输入、不开窗口。
if ProcessInfo.processInfo.environment["WB_SHELL_MOUSETEST"] != nil {
    mouseSelfTest()
    exit(0)
}
// ★ 点 Dock 图标最小化自测（第卅七轮）：只探 Dock 的 AX 结构，不挂 tap、不碰鼠标输入。
//   为什么值得单开一条通道：Dock 的 AX 层级是没文档的，这四条判据就是"引擎有没有戏"的体检口。
if ProcessInfo.processInfo.environment["WB_SHELL_DOCKTEST"] != nil {
    dockSelfTest()
    exit(0)
}
// ★ 键盘映射自测（第 12 套回归用它）：只查可确定核对的部分，不碰键盘输入、不开窗口。
if ProcessInfo.processInfo.environment["WB_SHELL_KEYMAPTEST"] != nil {
    keyMapSelfTest()
    exit(0)
}
if handleSudoRequestIfAny() { exit(0) }
// ★ 被 open 拉来干活的临时实例，但活儿已被别的实例办完 → 也别开窗口添乱。
//   （旧版漏了这一步：每次桥超时冷启都多留一个常驻窗口，越试越乱。）
//   ★★★ 2026-10-01：判据从「文件在不在」改成「**里面还有没有活**」——
//   常驻实例办完会把文件清成 `{}` 留着（好让文件事件源的 fd 有效），
//   所以文件是常在的，不能拿它当「有请求」的证据。
if !sudoReqPending() && sudoInviteFresh() { exit(0) }

let app = NSApplication.shared
app.setActivationPolicy(.regular)          // 有 Dock 图标（图标就是 app bundle 那张）
let shell = Shell()
app.delegate = shell
app.run()
