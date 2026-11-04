#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALLER="$ROOT_DIR/scripts/install-display.sh"
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

new_root() {
    local root=$1
    mkdir -p "$root/boot/firmware" "$root/usr/share/X11/xorg.conf.d" \
        "$root/etc/xdg/lxpanel-pi/panels" "$root/etc/xdg/lxsession/rpd-x" \
        "$root/etc/xdg/pcmanfm/default" "$root/home/pi-con/.config/lxsession/rpd-x" \
        "$root/etc/X11/xorg.conf.d" "$root/etc/xdg/labwc" "$root/etc/bluetooth" \
        "$root/etc/systemd/system"
    cat >"$root/boot/firmware/config.txt" <<'EOF'
# Raspberry Pi OS baseline
dtparam=audio=on
[cm5]
dtoverlay=dwc2,dr_mode=host
[all]
arm_64bit=1
dtparam=spi=on
EOF
    cat >"$root/usr/share/X11/xorg.conf.d/10-evdev.conf" <<'EOF'
Section "InputClass"
        Identifier "evdev touchscreen catchall"
        MatchIsTouchscreen "on"
        MatchDevicePath "/dev/input/event*"
        Driver "evdev"
EndSection
EOF
    cat >"$root/etc/xdg/lxpanel-pi/panels/panel" <<'EOF'
Global {
  edge=top
  height=36
  iconsize=36
  fontsize=12
  usefontsize=0
}
Plugin {
  type=launchbar
}
EOF
    cat >"$root/etc/xdg/lxsession/rpd-x/desktop.conf" <<'EOF'
[Session]
window_manager=openbox-rpd
[GTK]
sGtk/FontName=Nunito Sans Light 12
EOF
    cat >"$root/etc/xdg/pcmanfm/default/desktop-items-0.conf" <<'EOF'
[*]
desktop_font=Nunito Sans Light 12
show_trash=1
EOF
    cat >"$root/home/pi-con/.config/lxsession/rpd-x/autostart" <<'EOF'
@lxpanel-pi
@pcmanfm-pi
@xscreensaver -no-splash
@custom-user-entry
EOF
    echo '<openbox />' >"$root/etc/xdg/labwc/rc.xml"
    echo 'bluetooth-sentinel' >"$root/etc/bluetooth/main.conf"
    echo '[Service]' >"$root/etc/systemd/system/pi-con.service"
    echo 'unrelated-xorg-sentinel' >"$root/etc/X11/xorg.conf.d/10-unrelated.conf"
}

run_installer_at() {
    local installer=$1 root=$2
    shift 2
    PICON_DISPLAY_TEST_MODE=1 PICON_DISPLAY_ROOT="$root" PICON_DISPLAY_USER=pi-con \
        bash "$installer" "$@"
}

run_installer() {
    local root=$1
    shift
    run_installer_at "$INSTALLER" "$root" "$@"
}

assert_contains() { grep -Fq -- "$2" "$1" || { echo "Expected '$2' in $1" >&2; exit 1; }; }
assert_not_contains() { ! grep -Fq -- "$2" "$1" || { echo "Unexpected '$2' in $1" >&2; exit 1; }; }

[[ $(sha256sum "$ROOT_DIR/scripts/display/assets/mhs35ips.dtbo" | cut -d' ' -f1) == \
    cf8ea1ad0fb4e8e3246bdd9bccd6ce2806dd4b163dddfaf1cdd3a31b3cbb7cd5 ]]
[[ $(sha256sum "$ROOT_DIR/scripts/display/assets/99-calibration.conf" | cut -d' ' -f1) == \
    193dc44ae25aa2956f0e37f346336a78ba3b7dc92b4741a74815979cd958b39b ]]
if command -v dtc >/dev/null 2>&1; then
    decompiled="$tmp/mhs35ips.dts"
    dtc -I dtb -O dts "$ROOT_DIR/scripts/display/assets/mhs35ips.dtbo" >"$decompiled" 2>/dev/null
    for property in \
        'compatible = "ilitek,ili9486"' 'spi-max-frequency = <0x6dac2c0>' \
        'rotate = <0x5a>' 'reset-gpios = <0xdeadbeef 0x19 0x01>' \
        'dc-gpios = <0xdeadbeef 0x18 0x00>' 'compatible = "ti,ads7846"' \
        'spi-max-frequency = <0x1e8480>' 'interrupts = <0x11 0x02>' \
        'ti,x-plate-ohms = [00 3c]' 'ti,pressure-max = [00 ff]'; do
        assert_contains "$decompiled" "$property"
    done
fi

root="$tmp/install"
new_root "$root"
unrelated_before=$(find "$root/etc/xdg/labwc" "$root/etc/bluetooth" "$root/etc/systemd/system" "$root/etc/X11/xorg.conf.d/10-unrelated.conf" -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
python3 - "$root/boot/firmware/config.txt" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
PY
before_config=$(sha256sum "$root/boot/firmware/config.txt" | cut -d' ' -f1)
run_installer "$root" --dry-run >/dev/null
[[ ! -e $root/var/lib/picon-display ]] || { echo "Dry run created state" >&2; exit 1; }
[[ $(sha256sum "$root/boot/firmware/config.txt" | cut -d' ' -f1) == "$before_config" ]]

run_installer "$root" >/dev/null
unrelated_after=$(find "$root/etc/xdg/labwc" "$root/etc/bluetooth" "$root/etc/systemd/system" "$root/etc/X11/xorg.conf.d/10-unrelated.conf" -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
[[ $unrelated_before == "$unrelated_after" ]] || { echo "An unrelated Wayland, Bluetooth, Picon, or Xorg file changed" >&2; exit 1; }
cmp -s "$root/usr/share/X11/xorg.conf.d/45-evdev.conf" "$root/usr/share/X11/xorg.conf.d/10-evdev.conf"
cmp -s "$root/boot/firmware/overlays/mhs35ips.dtbo" "$ROOT_DIR/scripts/display/assets/mhs35ips.dtbo"
cmp -s "$root/etc/X11/xorg.conf.d/99-calibration.conf" "$ROOT_DIR/scripts/display/assets/99-calibration.conf"
assert_contains "$root/boot/firmware/config.txt" 'dtoverlay=mhs35ips:rotate=90'
assert_contains "$root/boot/firmware/config.txt" 'hdmi_cvt 480 320 60 6 0 0 0'
[[ $(grep -c 'dtparam=spi=on' "$root/boot/firmware/config.txt") == 1 ]]
grep -q $'\r$' "$root/boot/firmware/config.txt" || { echo "CRLF boot config was normalized" >&2; exit 1; }
assert_contains "$root/home/pi-con/.config/lxsession/rpd-x/autostart" '@lxpanel-pi'
assert_contains "$root/home/pi-con/.config/lxsession/rpd-x/autostart" '@pcmanfm-pi'
assert_contains "$root/home/pi-con/.config/lxsession/rpd-x/autostart" '@xscreensaver -no-splash'
assert_contains "$root/home/pi-con/.config/lxsession/rpd-x/autostart" '@custom-user-entry'
assert_contains "$root/home/pi-con/.config/lxsession/rpd-x/autostart" '@chromium --kiosk --no-first-run --no-default-browser-check http://localhost:8080/'
[[ $(grep -c 'http://localhost:8080/' "$root/home/pi-con/.config/lxsession/rpd-x/autostart") == 1 ]]
assert_contains "$root/home/pi-con/.config/lxpanel-pi/panels/panel" 'height=24'
assert_contains "$root/home/pi-con/.config/lxpanel-pi/panels/panel" 'iconsize=20'
assert_contains "$root/home/pi-con/.config/lxpanel-pi/panels/panel" 'fontsize=10'
assert_contains "$root/home/pi-con/.config/lxpanel-pi/panels/panel" 'usefontsize=1'
assert_contains "$root/home/pi-con/.config/lxsession/rpd-x/desktop.conf" 'sGtk/FontName=Nunito Sans Light 10'
assert_contains "$root/home/pi-con/.config/pcmanfm/default/desktop-items-0.conf" 'desktop_font=Nunito Sans Light 10'
run_installer "$root" --verify >/dev/null

snapshot=$(find "$root" -type f ! -path '*/var/lib/picon-display/backups/*' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
run_installer "$root" >/dev/null
snapshot_after=$(find "$root" -type f ! -path '*/var/lib/picon-display/backups/*' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
[[ $snapshot == "$snapshot_after" ]] || { echo "Second installation was not idempotent" >&2; exit 1; }

run_installer "$root" --restore >/dev/null
[[ $(sha256sum "$root/boot/firmware/config.txt" | cut -d' ' -f1) == "$before_config" ]]
[[ ! -e $root/boot/firmware/overlays/mhs35ips.dtbo ]]
[[ ! -e $root/etc/X11/xorg.conf.d/99-calibration.conf ]]
[[ ! -e $root/usr/share/X11/xorg.conf.d/45-evdev.conf ]]
[[ ! -e $root/home/pi-con/.config/lxpanel-pi/panels/panel ]]
[[ ! -e $root/home/pi-con/.config/lxsession/rpd-x/desktop.conf ]]
[[ ! -e $root/home/pi-con/.config/pcmanfm/default/desktop-items-0.conf ]]
assert_contains "$root/home/pi-con/.config/lxsession/rpd-x/autostart" '@custom-user-entry'
assert_not_contains "$root/home/pi-con/.config/lxsession/rpd-x/autostart" 'http://localhost:8080/'
[[ $(cat "$root/etc/xdg/labwc/rc.xml") == '<openbox />' ]]
[[ $(cat "$root/etc/bluetooth/main.conf") == 'bluetooth-sentinel' ]]
[[ $(cat "$root/etc/systemd/system/pi-con.service") == '[Service]' ]]
run_installer "$root" --restore >/dev/null

corrupt="$tmp/corrupt-backup"
new_root "$corrupt"
run_installer "$corrupt" >/dev/null
installed_config=$(sha256sum "$corrupt/boot/firmware/config.txt" | cut -d' ' -f1)
backup=$(cat "$corrupt/var/lib/picon-display/active-backup")
printf '%s\t%s\n' '/etc/passwd' 'absent' >>"$backup/manifest.tsv"
if run_installer "$corrupt" --restore >/dev/null 2>&1; then
    echo "Unexpected path in backup manifest was not rejected" >&2
    exit 1
fi
[[ $(sha256sum "$corrupt/boot/firmware/config.txt" | cut -d' ' -f1) == "$installed_config" ]]
[[ -f $corrupt/var/lib/picon-display/active-backup ]]

recovery_project="$tmp/recovery-project"
mkdir -p "$recovery_project/scripts/display"
cp "$INSTALLER" "$recovery_project/scripts/install-display.sh"
cp -a "$ROOT_DIR/scripts/display/assets" "$recovery_project/scripts/display/"
recovery_root="$tmp/recovery-root"
new_root "$recovery_root"
run_installer_at "$recovery_project/scripts/install-display.sh" "$recovery_root"
rm -rf -- "$recovery_project/scripts/display/assets"
run_installer_at "$recovery_project/scripts/install-display.sh" "$recovery_root" --restore >/dev/null
[[ ! -e $recovery_root/boot/firmware/overlays/mhs35ips.dtbo ]]
[[ ! -e $recovery_root/etc/X11/xorg.conf.d/99-calibration.conf ]]

already="$tmp/already-configured"
new_root "$already"
cat >>"$already/boot/firmware/config.txt" <<'EOF'
hdmi_force_hotplug=1
dtparam=i2c_arm=on
enable_uart=1
dtoverlay=mhs35ips:rotate=90
hdmi_group=2
hdmi_mode=1
hdmi_mode=87
hdmi_cvt 480 320 60 6 0 0 0
hdmi_drive=2
EOF
expected_config=$(sha256sum "$already/boot/firmware/config.txt" | cut -d' ' -f1)
run_installer "$already" >/dev/null
[[ $(sha256sum "$already/boot/firmware/config.txt" | cut -d' ' -f1) == "$expected_config" ]]
assert_not_contains "$already/boot/firmware/config.txt" '# BEGIN PICON DISPLAY CONFIG'

conflict="$tmp/conflict"
new_root "$conflict"
printf '%s\n' 'dtoverlay=mipi-dbi-spi,spi0-0' >>"$conflict/boot/firmware/config.txt"
if run_installer "$conflict" --dry-run >/dev/null 2>&1; then
    echo "Conflicting display overlay was not rejected" >&2
    exit 1
fi
[[ ! -e $conflict/var/lib/picon-display ]]

duplicate="$tmp/duplicate"
new_root "$duplicate"
printf '%s\n' 'dtoverlay=mhs35ips:rotate=90' 'dtoverlay=mhs35ips:rotate=90' >>"$duplicate/boot/firmware/config.txt"
if run_installer "$duplicate" --dry-run >/dev/null 2>&1; then
    echo "Duplicate CN0516 overlay was not rejected" >&2
    exit 1
fi
[[ ! -e $duplicate/var/lib/picon-display ]]

echo "install-display tests passed"
