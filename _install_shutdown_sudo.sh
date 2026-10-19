#!/bin/sh
# 给 /sbin/shutdown -h now 加一条免密白名单（供 TimeGuardian「跑完自动关机」使用）
# 这个脚本由 osascript 以管理员身份运行（系统会弹一次密码框）。
set -e
tmp=/tmp/_wb_shutdown_sudo
printf '%s\n' "$(whoami) ALL=(root) NOPASSWD: /sbin/shutdown -h now" > "$tmp"
# 先做语法自检，过了才装 —— 装坏一个 sudoers 文件会让 sudo 整体失效
/usr/sbin/visudo -cf "$tmp" >/dev/null
install -m 440 -o root -g wheel "$tmp" /etc/sudoers.d/workbuddy-shutdown
rm -f "$tmp"
# 装完再整体校验一次
/usr/sbin/visudo -c >/dev/null
exit 0
