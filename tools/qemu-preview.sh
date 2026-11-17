#!/usr/bin/env bash
# Boot a generated theme in real GRUB (QEMU + OVMF) and save a screenshot.
# Needs: qemu-system-x86_64, OVMF (edk2-ovmf), grub-mkstandalone.
#
#   tools/qemu-preview.sh build/pixel-you shot.png [WxH] [qemu sendkey keys…]
#   tools/qemu-preview.sh build/pixel-you shot.png 1920x1080 down down
set -euo pipefail

THEME=$(realpath "$1"); OUT=$(realpath -m "$2"); RES=${3:-1920x1080}; shift 2; [ $# -gt 0 ] && shift
W=${RES%x*}; H=${RES#*x}
OVMF=""
for d in /usr/share/edk2/x64 /usr/share/OVMF /usr/share/edk2/ovmf /usr/share/qemu; do
  for c in OVMF_CODE.4m.fd OVMF_CODE.fd; do [ -f "$d/$c" ] && { OVMF=$d/$c; break 2; }; done
done
[ -n "$OVMF" ] || { echo "OVMF firmware not found (install edk2-ovmf / ovmf)" >&2; exit 1; }
MKSA=$(command -v grub-mkstandalone || command -v grub2-mkstandalone)

WD=$(mktemp -d); trap 'kill $QPID 2>/dev/null || true; rm -rf "$WD"' EXIT
mkdir -p "$WD/esp/EFI/BOOT" "$WD/root/boot/grub/themes"
cp -r "$THEME" "$WD/root/boot/grub/themes/pixel-you"
T=/boot/grub/themes/pixel-you
{
  echo "insmod all_video; insmod gfxterm; insmod gfxmenu; insmod png"
  echo "set gfxmode=${W}x${H},auto"
  echo "terminal_output gfxterm"
  for f in "$WD/root$T"/*.pf2; do echo "loadfont $T/$(basename "$f")"; done
  cat <<CFG
set theme=$T/theme.txt
set timeout=60
menuentry 'Arch Linux' --class arch --class gnu-linux --class gnu --class os { true }
submenu 'Advanced options for Arch Linux' { menuentry 'x' { true } }
menuentry 'Windows Boot Manager (on /dev/nvme0n1p1)' --class windows --class os { true }
menuentry 'Ubuntu 24.04.1 LTS (on /dev/sda2)' --class ubuntu --class gnu-linux --class gnu --class os { true }
menuentry 'Fedora Linux 42 (on /dev/sdb3)' --class fedora --class gnu-linux --class gnu --class os { true }
menuentry 'Manjaro Linux (on /dev/sdc1)' --class manjarolinux --class gnu-linux --class gnu --class os { true }
menuentry 'Some Unknown OS' --class mystery --class os { true }
menuentry 'UEFI Firmware Settings' { true }
CFG
} > "$WD/root/boot/grub/grub.cfg"

(cd "$WD/root" && "$MKSA" -O x86_64-efi -o ../esp/EFI/BOOT/BOOTX64.EFI \
   $(find boot -type f -printf '/%p=%p ')) >/dev/null 2>&1
cp "$(dirname "$OVMF")/$(basename "$OVMF" | sed 's/CODE/VARS/')" "$WD/vars.fd"
# relative socket path: UNIX socket paths are limited to ~108 chars
(cd "$WD" && exec qemu-system-x86_64 -m 512 -machine q35 \
  -drive if=pflash,format=raw,readonly=on,file="$OVMF" \
  -drive if=pflash,format=raw,file="$WD/vars.fd" -drive format=raw,file=fat:rw:"$WD/esp" \
  -device VGA,xres="$W",yres="$H" -display none -net none \
  -monitor unix:mon.sock,server,nowait >/dev/null 2>&1) &
QPID=$!
sleep 15
(cd "$WD" && python3 - mon.sock shot.ppm "$@") <<'PY'
import socket, sys, time
s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); time.sleep(0.3); s.recv(65536)
for k in sys.argv[3:]:
    s.send(f"sendkey {k}\n".encode()); time.sleep(0.6)
time.sleep(1)
s.send(f"screendump {sys.argv[2]}\n".encode()); time.sleep(1.5)
PY
python3 -c "import sys; from PIL import Image; Image.open(sys.argv[1]).save(sys.argv[2])" "$WD/shot.ppm" "$OUT"
echo "screenshot → $OUT"
