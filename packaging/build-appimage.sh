#!/usr/bin/env bash
# 把本仓库打成 AppImage。程序代码打进包里，GTK 与 Python 用系统已安装的版本。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "${ROOT}/src/cursor_chat_desk/__init__.py")"
ARCH="$(uname -m)"
DIST="${ROOT}/dist"
APPDIR="${DIST}/AppDir"
OUT="${DIST}/cursor-chat-desk-${VERSION}-${ARCH}.AppImage"
TOOL_DIR="${DIST}/cache"
TOOL="${TOOL_DIR}/appimagetool-${ARCH}.AppImage"

rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/lib/cursor-chat-desk" "${APPDIR}/usr/bin" "${DIST}" "${TOOL_DIR}"

python3 - <<PY
import shutil
from pathlib import Path
src = Path("${ROOT}") / "src" / "cursor_chat_desk"
dest = Path("${APPDIR}") / "usr" / "lib" / "cursor-chat-desk" / "cursor_chat_desk"
shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
PY

cat > "${APPDIR}/usr/bin/cursor-chat-desk" <<'EOF'
#!/bin/sh
set -eu
HERE="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
export PYTHONPATH="${HERE}/usr/lib/cursor-chat-desk${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONNOUSERSITE=1
if ! python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw" >/dev/null 2>&1; then
  msg="打不开 Cursor 会话：系统里需要 python3、python3-gobject、gtk4 和 libadwaita。"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="Cursor 会话" --text="${msg}" || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send "Cursor 会话" "${msg}" || true
  fi
  echo "${msg}" >&2
  exit 1
fi
exec python3 -m cursor_chat_desk "$@"
EOF
chmod 755 "${APPDIR}/usr/bin/cursor-chat-desk"
cat > "${APPDIR}/AppRun" <<'EOF'
#!/bin/sh
set -eu
if [ -n "${APPDIR:-}" ]; then
  HERE="${APPDIR}"
else
  HERE="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
fi
exec "${HERE}/usr/bin/cursor-chat-desk" "$@"
EOF
chmod 755 "${APPDIR}/AppRun"
magick -background none "${ROOT}/share/cursor-chat-desk.svg" -resize 256x256 "${APPDIR}/cursor-chat-desk.png"

cat > "${APPDIR}/cursor-chat-desk.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Cursor 会话
Comment=查看并删除本机 Cursor 对话
Exec=cursor-chat-desk
Icon=cursor-chat-desk
Terminal=false
Categories=Utility;
Keywords=Cursor;chat;会话;
StartupNotify=true
StartupWMClass=dev.cursorchatdesk.Desk
X-AppImage-Version=${VERSION}
EOF

if [[ ! -x "${TOOL}" ]]; then
  curl -fL --retry 3 -o "${TOOL}" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
  chmod 755 "${TOOL}"
fi

export ARCH
export APPIMAGE_EXTRACT_AND_RUN=1
"${TOOL}" "${APPDIR}" "${OUT}"
chmod 755 "${OUT}"
echo "APPIMAGE dist/cursor-chat-desk-${VERSION}-${ARCH}.AppImage"
