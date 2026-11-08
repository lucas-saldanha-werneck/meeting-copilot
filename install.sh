#!/bin/bash
# Install meeting-copilot: symlink bin/meeting-copilot into ~/.local/bin (or $PREFIX).
#   ./install.sh               install
#   ./install.sh --uninstall   remove the symlink (only if it points to this repo)
#   ./install.sh --dry-run     show what would be done, change nothing
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${PREFIX:-$HOME/.local/bin}"
SRC="$ROOT/bin/meeting-copilot"
DEST="$BIN_DIR/meeting-copilot"

dry=0; uninstall=0
for a in "$@"; do
  case "$a" in
    --dry-run) dry=1 ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

run() {
  if [ "$dry" = 1 ]; then echo "would run: $*"; else "$@"; fi
}

if [ "$uninstall" = 1 ]; then
  if [ -L "$DEST" ] && [ "$(readlink "$DEST")" = "$SRC" ]; then
    run rm "$DEST"
    echo "removed $DEST"
  elif [ -e "$DEST" ] || [ -L "$DEST" ]; then
    echo "left $DEST alone: it is not a symlink to this repo" >&2
  else
    echo "nothing to remove at $DEST"
  fi
  echo "your config (~/.config/meeting-copilot) and saved sessions (~/.local/share/meeting-copilot) were kept"
  exit 0
fi

if [ "$(uname -s)" != "Darwin" ]; then
  echo "note: meeting-copilot needs the Muesli app, which runs on macOS. Installing anyway." >&2
fi
command -v python3 >/dev/null 2>&1 || echo "warning: python3 not found on PATH" >&2
command -v claude >/dev/null 2>&1 || echo "warning: claude (Claude Code) not found on PATH; only the speaking metrics will work" >&2

if [ -e "$DEST" ] && ! [ -L "$DEST" ]; then
  echo "refusing to overwrite $DEST: it is a regular file, not a symlink" >&2
  exit 1
fi
run mkdir -p "$BIN_DIR"
run chmod +x "$SRC"
run ln -sfn "$SRC" "$DEST"
echo "installed $DEST -> $SRC"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note: $BIN_DIR is not on your PATH" ;;
esac
