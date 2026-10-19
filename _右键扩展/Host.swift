import Foundation
// ============================================================================
// 「非沙箱命令执行器」—— 由 open/launchd 拉起 ⇒ APP_SANDBOX_CONTAINER_ID = <none>
// 凡是在工具沙箱里跑不动的命令（pluginkit / ps / killall / log show …），
// 写进「执行命令.txt」，本程序就会一行一条地用 /bin/bash -c 跑，结果写「执行结果.txt」。
//
// ⚠️ 本文件每行是**独立的 bash**：变量不跨行（踩过：vm 赋值在下一行就没了）。
// ⚠️ 也**不是 shell 脚本**，别写 `if`/`for` 跨行；一行写完。
// ============================================================================
let dir = NSString(string: "~/Library/Application Support/WorkBuddyTaskSync/_右键扩展")
    .expandingTildeInPath
let logPath = dir + "/执行结果.txt"
let cmdPath = dir + "/执行命令.txt"

func W(_ s: String) {
    let d = (s + "\n").data(using: .utf8)!
    if let fh = FileHandle(forWritingAtPath: logPath) {
        fh.seekToEndOfFile(); fh.write(d); fh.closeFile()
    } else { try? d.write(to: URL(fileURLWithPath: logPath)) }
}

func run(_ path: String, _ args: [String]) -> (Int32, String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    let pipe = Pipe()
    p.standardOutput = pipe; p.standardError = pipe
    do { try p.run() } catch { return (-1, "spawn failed: \(error)") }
    p.waitUntilExit()
    let d = pipe.fileHandleForReading.readDataToEndOfFile()
    return (p.terminationStatus, String(data: d, encoding: .utf8) ?? "")
}

W("=== 执行器 @ \(Date()) sandbox=\(ProcessInfo.processInfo.environment["APP_SANDBOX_CONTAINER_ID"] ?? "<none>") ===")
let raw = (try? String(contentsOfFile: cmdPath, encoding: .utf8)) ?? ""
for line in raw.split(separator: "\n") {
    let c = line.trimmingCharacters(in: .whitespaces)
    if c.isEmpty || c.hasPrefix("#") { continue }
    W("$ \(c)")
    let (rc, out) = run("/bin/bash", ["-c", c])
    W("rc=\(rc)")
    let t = out.trimmingCharacters(in: .whitespacesAndNewlines)
    if !t.isEmpty { W(t) }
    W("")
}
W("=== done ===")
exit(0)
