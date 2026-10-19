# 未随仓库分发：_自签证书/（本机代码签名私钥与钥匙串）。重建前需自备证书放回该目录。
#!/bin/bash
# ============================================================================
# _build.sh —— 「改完右键扩展源码 → 一条命令装好」的唯一入口
#
#   用法：  bash _build.sh
#
# 做五件事：
#   ① 编译（clang/ObjC 编扩展 + swiftc 编执行器）
#   ② 组装 → _out/WorkBuddy右键扩展.appex、_out/.bin/WorkBuddy执行器.app
#   ③ 签名（本地自签证书；身份固定 ⇒ 重编不掉权限）
#   ④ 装进任务管家：扩展 → Contents/PlugIns/，执行器 → Contents/Resources/
#      （两个都住宿主包里 ⇒ 系统里"看得见的应用"只有任务管家一个）
#      并重签任务管家（wb_appsign.py，唯一签名入口）
#   ⑤ 借非沙箱执行器注册扩展（跑两遍）+ 重启 Finder
#
# ★ 扩展为什么寄生在任务管家里：
#   FinderSync 扩展**必须**住在一个 .app 的 Contents/PlugIns 里（macOS 强制，
#   `.workflow` 快速操作到不了右键顶层，只有扩展进得去）。任务管家本来就是我们的
#  常驻 app，寄到它里面 ⇒ /Applications 里不再多一个图标。
#   ⚠️ 因此 `_build_app.sh` 里那份 `rm -rf "${APP}"` 会把扩展一起删掉 —— 那边已
#      补了「重建时自动装回 _out/*.appex」的步骤，两处必须同时存在。
#
# ★ 为什么注册必须借执行器：
#   本项目工具进程带 seatbelt 沙箱，`pluginkit -a` / `-e use` 在里面**静默无效**
#   （只打印 `match: unauthorized discovery flag (PKDiscoverAll)`，rc 还是 0）。
#   由 `open` 拉起的 app 是 APP_SANDBOX_CONTAINER_ID=<none> 的普通进程，
#   同一批命令在那里全都能跑。
# ★ 注册要**跑两遍**：替换之后第一次往往扫不到，第二次才进列表（实测）。
# ★ `-a` 之后必须留时间给 pkd 扫描，否则 `-e use` 静默无效。
# ============================================================================
set -e

SELF="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"      # ★ 2026-10-10：右键扩展归 TimeGuardian
BUILD=/tmp/wbqa_build
OUT="$SELF/_out"
CERT="$ROOT/_自签证书"
KC="$CERT/workbuddy-sign.keychain-db"
CY="$ROOT/_runtime/python/bin/python3"
[ -x "$CY" ] || CY="/usr/bin/python3"
SIGNER="$ROOT/wb_appsign.py"

TASK="/Applications/TimeGuardian.app"                # ★ 宿主 app 已从任务管家改为 TimeGuardian
OLDHOST="/Applications/WorkBuddy任务管家.app"        # ★ 旧宿主：安装时要把里面那份摘掉并重签
PLUG="$TASK/Contents/PlugIns"
APPEX="$PLUG/WorkBuddy右键扩展.appex"
EXT_BID="com.wb.wbqa.rightclick.ext"
RES="$TASK/Contents/Resources"                      # 执行器的**正式驻地**（包内 ⇒ 系统看不见）
HOST_EXEC="$RES/WorkBuddy执行器.app"
EXEC_APP="$OUT/.bin/WorkBuddy执行器.app"            # 构建产物（隐藏目录；构建期用它跑命令）
OLD_EXEC="$OUT/WorkBuddy执行器.app"                 # ★ 上一版的**可见**位置 —— 每次构建都清掉
OLD_APP="/Applications/WorkBuddy右键.app"           # 上一代独立宿主（本次删掉）
OLD_APPEX="$OLD_APP/Contents/PlugIns/WorkBuddy右键扩展.appex"

[ -x "$CY" ] || CY="$(command -v python3 || true)"
command -v swiftc >/dev/null 2>&1 || { echo "✗ 找不到 swiftc：xcode-select --install"; exit 1; }

rm -rf "$BUILD"; mkdir -p "$BUILD" "$OUT"

echo "── 1/5 编译（ObjC！swiftc 编出来的主类是 local 符号，系统找不到）"
clang -fobjc-arc -O -o "$BUILD/WBRCFinderSync" \
      "$SELF/WBRCFinderSync.m" "$SELF/main.m" \
      -framework Cocoa -framework FinderSync
swiftc -O -o "$BUILD/Host" "$SELF/Host.swift"
echo "   ✓ 扩展 $(du -h "$BUILD/WBRCFinderSync" | cut -f1) / 执行器 $(du -h "$BUILD/Host" | cut -f1)"

echo "── 2/5 组装"
"$CY" "$SELF/assemble.py" "$BUILD" "$OUT"

echo "── 3/5 签名（本地自签证书；先 appex 后执行器）"
security unlock-keychain -p "$(cat "$CERT/keychain.pw")" "$KC" >/dev/null
codesign --force --keychain "$KC" --sign "WorkBuddy Local Signer" \
         --identifier "$EXT_BID" --entitlements "$SELF/ext.entitlements" \
         "$OUT/WorkBuddy右键扩展.appex"
codesign --force --keychain "$KC" --sign "WorkBuddy Local Signer" \
         --identifier com.wb.workbuddy-toolkit.exec \
         "$EXEC_APP"
codesign --verify --strict "$OUT/WorkBuddy右键扩展.appex" && echo "   ✓ appex 签名有效"
codesign --verify --strict "$EXEC_APP"  && echo "   ✓ 执行器签名有效（构建产物 _out/.bin/）"

echo "── 4/5 + 5/5 装进任务管家 + 注册（借非沙箱执行器跑）"
cat > "$SELF/执行命令.txt" <<EOF
# ---- 1) 卸掉上一代独立宿主 app（/Applications 里不再多一个图标） ----
/usr/bin/pluginkit -r "$OLD_APPEX" 2>/dev/null
/bin/rm -rf "$OLD_APP"
# ---- 2) 把扩展装进宿主（TimeGuardian） ----
/bin/mkdir -p "$PLUG"
/usr/bin/ditto "$OUT/WorkBuddy右键扩展.appex" "$APPEX"
/bin/ls -la "$PLUG"
# ---- 2a) 旧宿主（任务管家）里若还留着扩展 —— 摘掉并重签它 ----
#   ★ 2026-10-10：扩展改归 TimeGuardian。两份同 bundle id 的扩展会让系统随手挑一份，
#     且卸载任务管家会把右键菜单一起带走。这里顺手把旧宿主清干净。
if [ -d "$OLDHOST/Contents/PlugIns/WorkBuddy右键扩展.appex" ]; then
  /bin/rm -rf "$OLDHOST/Contents/PlugIns/WorkBuddy右键扩展.appex"
  /usr/bin/python3 "$ROOT/wb_appsign.py" --sign
fi
# ---- 2b) 把执行器也装进任务管家**包内**（正式驻地；系统里看不见它） ----
#   ★ 2026-10-02（第六十五轮）：用户看到访达/启动台里冒出个「WorkBuddy执行器」app
#     —— 就是它原来躺在 _out/ 下当正经 .app。现在两处一起改：构建产物藏 _out/.bin/，
#     正式驻地搬进 Contents/Resources/（既是"整合进任务管家一个软件"，又照样隐身）。
/bin/mkdir -p "$RES"
/usr/bin/ditto "$EXEC_APP" "$HOST_EXEC"
/bin/rm -rf "$OLD_EXEC"
/bin/ls -la "$RES"
# ---- 3) 重签任务管家（唯一签名入口；证书签名 ⇒ 不掉 TCC 权限） ----
#   ⚠️ `[S]` 是给 pkill **自己**打码：`pkill -f WorkBuddyShell` 的 bash 命令行里
#      含这串字，pkill 会把自己那条也匹配上（本执行器是逐行起 bash，所以只丢那一行，
#      但换成字符类更干净、也更经得起以后改成"整段脚本"）。
/usr/bin/pkill -f "WorkBuddy[S]hell"
/bin/sleep 2
"$CY" "$SIGNER" --sign
# ---- 4) 注册（跑两遍）+ 重启 Finder ----
/usr/bin/pluginkit -a "$APPEX"
/bin/sleep 6
/usr/bin/pluginkit -e use -i "$EXT_BID"
/bin/sleep 3
/usr/bin/pluginkit -a "$APPEX"
/bin/sleep 6
/usr/bin/pluginkit -e use -i "$EXT_BID"
/bin/sleep 3
echo "=== 已启用的 FinderSync 扩展 ==="
/usr/bin/pluginkit -m -p com.apple.FinderSync
echo "=== 我们的注册详情 ==="
/usr/bin/pluginkit -m -A -vvv -p com.apple.FinderSync 2>&1 | /usr/bin/grep -A5 "wbqa"
/usr/bin/killall Finder
/bin/sleep 10
# ---- 5) 恢复外壳 ----
/usr/bin/open -g -a "$TASK"
/bin/sleep 5
echo "=== 进程 ==="
/bin/ps -Ao pid,etime,comm | /usr/bin/grep -E "WBRCFinderSync|WorkBuddyShell" | /usr/bin/grep -v grep
echo "=== 扩展日志（最近 120s） ==="
/usr/bin/log show --last 120s --predicate 'process == "WBRCFinderSync"' --style compact 2>&1 | /usr/bin/grep "WBRC:" | /usr/bin/tail -20
echo "=== 旧 app 还在不在（应当 no） ==="
/bin/ls -d "$OLD_APP" 2>&1
EOF

rm -f "$SELF/执行结果.txt"
open -n "$EXEC_APP"
for i in $(seq 1 60); do
    [ -f "$SELF/执行结果.txt" ] && grep -q "=== done ===" "$SELF/执行结果.txt" && break
    sleep 2
done
cat "$SELF/执行结果.txt" 2>/dev/null || echo "（没拿到执行结果 —— 执行器没起来？）"
