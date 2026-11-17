#!/usr/bin/env bash
# Install the Pixel You GRUB theme, generated from your wallpaper, and turn on
# os-prober so other operating systems (Windows, other distros, macOS) appear.
#
#   sudo ./install.sh [-w wallpaper] [-r WxH] [-m dark|light|auto] [-s tonal-spot|vibrant|neutral]
#                     [-c '#rrggbb'] [-t title] [-z zoom] [--no-os-prober] [--uninstall]
set -euo pipefail

NAME=pixel-you
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULTS=/etc/default/grub

die()  { printf '\e[31m✗ %s\e[0m\n' "$*" >&2; exit 1; }
info() { printf '\e[36m•\e[0m %s\n' "$*"; }
warn() { printf '\e[33m! %s\e[0m\n' "$*"; }

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

ORIG_ARGS=("$@")
GEN_ARGS=() WALL="" RES="" OSPROBER=1 UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    -w|--wallpaper)  WALL="$(realpath "$2")"; shift 2 ;;
    -r|--resolution) RES="$2"; shift 2 ;;
    -m|--mode|-s|--scheme|-c|--color|-t|--title|-z|--zoom|--font|--mono-font) GEN_ARGS+=("$1" "$2"); shift 2 ;;
    --no-os-prober)  OSPROBER=0; shift ;;
    --uninstall)     UNINSTALL=1; shift ;;
    -h|--help)       usage ;;
    *)               usage 1 ;;
  esac
done

[ "$EUID" -eq 0 ] || exec sudo "$0" "${ORIG_ARGS[@]}"
[ -f "$DEFAULTS" ] || die "$DEFAULTS not found — is GRUB installed?"

# ── locate GRUB (Arch/Debian use /boot/grub, Fedora/openSUSE /boot/grub2) ──
GRUB_DIR=""
for d in /boot/grub /boot/grub2; do [ -f "$d/grub.cfg" ] && { GRUB_DIR=$d; break; }; done
[ -n "$GRUB_DIR" ] || for d in /boot/grub /boot/grub2; do [ -d "$d" ] && { GRUB_DIR=$d; break; }; done
[ -n "$GRUB_DIR" ] || die "no /boot/grub or /boot/grub2 directory"
THEME_DIR="$GRUB_DIR/themes/$NAME"

regen() {
  if command -v update-grub >/dev/null; then update-grub
  elif command -v grub-mkconfig >/dev/null; then grub-mkconfig -o "$GRUB_DIR/grub.cfg"
  elif command -v grub2-mkconfig >/dev/null; then grub2-mkconfig -o "$GRUB_DIR/grub.cfg"
  else die "no grub-mkconfig/update-grub found"; fi
}

# set KEY="value" in /etc/default/grub, uncommenting or appending as needed
set_var() {
  if grep -qE "^#?[[:space:]]*$1=" "$DEFAULTS"; then
    sed -i -E "s|^#?[[:space:]]*$1=.*|$1=\"$2\"|" "$DEFAULTS"
  else
    printf '%s="%s"\n' "$1" "$2" >> "$DEFAULTS"
  fi
}

cp -n "$DEFAULTS" "$DEFAULTS.pixel-you.bak" 2>/dev/null || true

if [ "$UNINSTALL" -eq 1 ]; then
  rm -rf "$THEME_DIR"
  sed -i -E "s|^(GRUB_THEME=.*$NAME.*)|#\1|" "$DEFAULTS"
  info "removed $THEME_DIR"
  regen
  exit 0
fi

# ── generate ──
command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import PIL' 2>/dev/null || die "Pillow is required (pacman -S python-pillow / apt install python3-pil / dnf install python3-pillow)"

[ -n "$RES" ] && GEN_ARGS+=(-r "$RES")
[ -n "$WALL" ] && GEN_ARGS=("$WALL" "${GEN_ARGS[@]}")
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
python3 "$HERE/generate.py" "${GEN_ARGS[@]}" -o "$TMP/$NAME"
RES="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["resolution"])' "$TMP/$NAME/palette.json")"

# ── install ──
mkdir -p "$GRUB_DIR/themes"
rm -rf "$THEME_DIR"
cp -r "$TMP/$NAME" "$THEME_DIR"
info "theme installed to $THEME_DIR"

set_var GRUB_THEME "$THEME_DIR/theme.txt"
set_var GRUB_GFXMODE "$RES,auto"
# graphical themes need gfxterm; a forced text console would hide the theme
sed -i -E 's|^(GRUB_TERMINAL_OUTPUT=.*console.*)|#\1|' "$DEFAULTS"
grep -qE '^GRUB_TIMEOUT_STYLE="?hidden' "$DEFAULTS" && \
  warn "GRUB_TIMEOUT_STYLE=hidden: the menu only shows if you hold Shift/Esc at boot"

# ── os-prober ──
if [ "$OSPROBER" -eq 1 ]; then
  set_var GRUB_DISABLE_OS_PROBER false
  if command -v os-prober >/dev/null; then
    info "os-prober enabled — other systems will be detected and get matching icons"
  else
    warn "os-prober is not installed; install it so other systems are detected:"
    warn "  Arch: pacman -S os-prober   Debian/Ubuntu: apt install os-prober   Fedora: dnf install os-prober"
  fi
fi

info "previous settings saved to $DEFAULTS.pixel-you.bak"
regen
info "done — reboot to see it (generated for $RES)"
