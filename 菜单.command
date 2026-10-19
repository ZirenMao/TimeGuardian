#!/bin/bash
# 时间守护 · 菜单入口（自包含便携版：优先用随包自带的运行时）
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" || exit 1
PY="$HERE/_runtime/python/bin/python3"
[ -x "$PY" ] || PY="/usr/bin/python3"
exec "$PY" "$HERE/wb_menu.py"
