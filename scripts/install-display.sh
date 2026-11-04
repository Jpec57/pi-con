#!/usr/bin/env bash
# Recreate the audited Pi-Con CN0516 display setup on Raspberry Pi OS Trixie.
# This script never reboots the Pi or restarts X11, LXDE, Chromium, or pi-con.service.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ASSET_DIR="$SCRIPT_DIR/display/assets"
EXPECTED_OVERLAY_SHA256=cf8ea1ad0fb4e8e3246bdd9bccd6ce2806dd4b163dddfaf1cdd3a31b3cbb7cd5
EXPECTED_CALIBRATION_SHA256=193dc44ae25aa2956f0e37f346336a78ba3b7dc92b4741a74815979cd958b39b
STATE_DIR=/var/lib/picon-display
ACTIVE_FILE="$STATE_DIR/active-backup"
TEST_MODE=${PICON_DISPLAY_TEST_MODE:-0}
TEST_ROOT=${PICON_DISPLAY_ROOT:-}
ACTION=install

usage() {
    cat <<'USAGE'
Usage: install-display.sh [--dry-run | --verify | --restore | --help]

Default: install the audited SunFounder MHS35IPS/CN0516 setup.
--dry-run  Show the intended operations without writing or installing packages.
--verify   Check the persistent configuration and (when present) live devices.
--restore  Restore the exact files captured before the first install.
USAGE
}

while (($#)); do
    case "$1" in
        --dry-run) [[ $ACTION == install ]] || { usage >&2; exit 2; }; ACTION=dry-run ;;
        --verify)  [[ $ACTION == install ]] || { usage >&2; exit 2; }; ACTION=verify ;;
        --restore) [[ $ACTION == install ]] || { usage >&2; exit 2; }; ACTION=restore ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [[ $TEST_MODE == 1 ]]; then
    [[ -n $TEST_ROOT ]] || { echo "PICON_DISPLAY_ROOT is required in test mode" >&2; exit 2; }
    TEST_ROOT=${TEST_ROOT%/}
elif [[ -n $TEST_ROOT ]]; then
    echo "PICON_DISPLAY_ROOT is accepted only with PICON_DISPLAY_TEST_MODE=1" >&2
    exit 2
fi

target() {
    if [[ $TEST_MODE == 1 ]]; then printf '%s%s' "$TEST_ROOT" "$1"; else printf '%s' "$1"; fi
}

if [[ $TEST_MODE != 1 && $EUID -ne 0 && ( $ACTION == install || $ACTION == restore ) ]]; then
    exec sudo -- bash "$0" "$ACTION"
fi

if [[ $TEST_MODE == 1 ]]; then
    DISPLAY_USER=${PICON_DISPLAY_USER:-pi-con}
    DISPLAY_HOME=${PICON_DISPLAY_HOME:-/home/$DISPLAY_USER}
    DISPLAY_UID=$(id -u)
    DISPLAY_GID=$(id -g)
else
    DISPLAY_USER=${PICON_DISPLAY_USER:-${SUDO_USER:-}}
    [[ -n $DISPLAY_USER && $DISPLAY_USER != root ]] || {
        echo "Run this script with sudo from the desktop account, or set PICON_DISPLAY_USER." >&2
        exit 2
    }
    passwd_entry=$(getent passwd "$DISPLAY_USER") || { echo "Unknown user: $DISPLAY_USER" >&2; exit 2; }
    DISPLAY_HOME=$(cut -d: -f6 <<<"$passwd_entry")
    DISPLAY_UID=$(id -u "$DISPLAY_USER")
    DISPLAY_GID=$(id -g "$DISPLAY_USER")
fi

BOOT_CONFIG=$(target /boot/firmware/config.txt)
OVERLAY_TARGET=$(target /boot/firmware/overlays/mhs35ips.dtbo)
CALIBRATION_TARGET=$(target /etc/X11/xorg.conf.d/99-calibration.conf)
EVDEV_TARGET=$(target /usr/share/X11/xorg.conf.d/45-evdev.conf)
EVDEV_SOURCE=$(target /usr/share/X11/xorg.conf.d/10-evdev.conf)
AUTOSTART_TARGET=$(target "$DISPLAY_HOME/.config/lxsession/rpd-x/autostart")
DESKTOP_TARGET=$(target "$DISPLAY_HOME/.config/lxsession/rpd-x/desktop.conf")
PANEL_TARGET=$(target "$DISPLAY_HOME/.config/lxpanel-pi/panels/panel")
PCMANFM_TARGET=$(target "$DISPLAY_HOME/.config/pcmanfm/default/desktop-items-0.conf")
STATE_DIR=$(target "$STATE_DIR")
ACTIVE_FILE=$(target "$ACTIVE_FILE")

MANAGED_PATHS=(
    /boot/firmware/config.txt
    /boot/firmware/overlays/mhs35ips.dtbo
    /etc/X11/xorg.conf.d/99-calibration.conf
    /usr/share/X11/xorg.conf.d/45-evdev.conf
    "$DISPLAY_HOME/.config/lxsession/rpd-x/autostart"
    "$DISPLAY_HOME/.config/lxsession/rpd-x/desktop.conf"
    "$DISPLAY_HOME/.config/lxpanel-pi/panels/panel"
    "$DISPLAY_HOME/.config/pcmanfm/default/desktop-items-0.conf"
)

fail() { echo "ERROR: $*" >&2; exit 1; }

check_assets() {
    [[ -f $ASSET_DIR/mhs35ips.dtbo ]] || fail "Missing display overlay asset: $ASSET_DIR/mhs35ips.dtbo"
    [[ -f $ASSET_DIR/99-calibration.conf ]] || fail "Missing touchscreen calibration asset: $ASSET_DIR/99-calibration.conf"
    local overlay_sum calibration_sum
    overlay_sum=$(sha256sum "$ASSET_DIR/mhs35ips.dtbo" | cut -d' ' -f1)
    calibration_sum=$(sha256sum "$ASSET_DIR/99-calibration.conf" | cut -d' ' -f1)
    [[ $overlay_sum == "$EXPECTED_OVERLAY_SHA256" ]] || fail "Display overlay asset checksum mismatch"
    [[ $calibration_sum == "$EXPECTED_CALIBRATION_SHA256" ]] || fail "Touch calibration asset checksum mismatch"
}

check_system() {
    [[ $TEST_MODE == 1 ]] && return 0
    local os_release model display_manager
    os_release=$(target /etc/os-release)
    [[ -r $os_release ]] || fail "Cannot read /etc/os-release"
    # shellcheck disable=SC1090
    . "$os_release"
    [[ ${ID:-} == debian && ${VERSION_CODENAME:-} == trixie ]] || fail "Expected Raspberry Pi OS Debian Trixie; found ${PRETTY_NAME:-unknown}"
    model=$(tr -d '\000' <"$(target /proc/device-tree/model)" 2>/dev/null || true)
    [[ $model == *"Raspberry Pi 4"* ]] || fail "Expected Raspberry Pi 4; found ${model:-unknown}"
    [[ -f $BOOT_CONFIG ]] || fail "Missing $BOOT_CONFIG"
    display_manager=$(cat "$(target /etc/X11/default-display-manager)" 2>/dev/null || true)
    [[ $display_manager == */lightdm ]] || fail "LightDM is not the selected display manager; select the LXDE/X11 session first"
    [[ -f $(target /usr/share/xsessions/rpd-x.desktop) ]] || fail "LXDE X11 session rpd-x is not installed"
    local lightdm_config
    lightdm_config=$(target /etc/lightdm/lightdm.conf)
    [[ -r $lightdm_config ]] || fail "Cannot read LightDM configuration"
    grep -Eq '^[[:space:]]*user-session[[:space:]]*=[[:space:]]*rpd-x[[:space:]]*$' "$lightdm_config" || fail "LightDM is not configured to use the rpd-x X11 session"
    grep -Eq "^[[:space:]]*autologin-user[[:space:]]*=[[:space:]]*$DISPLAY_USER[[:space:]]*$" "$lightdm_config" || fail "LightDM autologin is not configured for $DISPLAY_USER"
    grep -Eq '^[[:space:]]*autologin-session[[:space:]]*=[[:space:]]*rpd-x[[:space:]]*$' "$lightdm_config" || fail "LightDM autologin session is not rpd-x"
    [[ -d $DISPLAY_HOME && ! -L $DISPLAY_HOME && $(stat -c %u "$DISPLAY_HOME") == "$DISPLAY_UID" ]] || fail "Desktop home directory does not belong to $DISPLAY_USER"
    local module kernel_modules
    kernel_modules=$(target "/lib/modules/$(uname -r)")
    [[ -d $kernel_modules ]] || fail "Kernel module directory is missing: $kernel_modules"
    for module in fb_ili9486 ads7846; do
        if [[ -z $(find "$kernel_modules" -type f -name "$module.ko*" -print -quit) ]] \
            && ! grep -Eq "(^|/)${module}\.ko$" "$kernel_modules/modules.builtin" 2>/dev/null; then
            fail "The active Raspberry Pi kernel does not provide the required $module driver"
        fi
    done
}

check_boot_conflicts() {
    python3 - "$BOOT_CONFIG" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
expected_overlay = "mhs35ips:rotate=90"
matching_overlays = 0
for line in text.splitlines():
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or not stripped.startswith("dtoverlay="):
        continue
    overlay = stripped.split("=", 1)[1].strip()
    name = re.split(r"[:,]", overlay, maxsplit=1)[0].lower()
    if name in {"mhs35ips"} and overlay != expected_overlay:
        raise SystemExit(f"Conflicting CN0516 overlay setting: {stripped}")
    if name == "mhs35ips":
        matching_overlays += 1
    if re.search(r"mhs|mipi-dbi|ads7846|tft35|tft934|waveshare|ili948", name) and overlay != expected_overlay:
        raise SystemExit(f"Conflicting display/touch overlay: {stripped}")
if matching_overlays > 1:
    raise SystemExit("Duplicate mhs35ips overlays in config.txt")
PY
}

check_desktop_templates() {
    local template
    for template in \
        /etc/xdg/lxpanel-pi/panels/panel \
        /etc/xdg/lxsession/rpd-x/desktop.conf \
        /etc/xdg/pcmanfm/default/desktop-items-0.conf; do
        [[ -f $(target "$template") ]] || fail "Required Raspberry Pi OS desktop template is missing: $template"
    done
}

backup_state() {
    if [[ -s $ACTIVE_FILE ]]; then
        local existing
        [[ ! -L $ACTIVE_FILE ]] || fail "Active display backup pointer must not be a symlink"
        existing=$(cat "$ACTIVE_FILE")
        validate_backup "$existing"
        echo "Reusing original backup: $existing"
        return
    fi
    local stamp backup path destination
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    backup="$STATE_DIR/backups/$stamp-$$"
    install -d -m 0700 "$backup/files"
    : >"$backup/manifest.tsv"
    chmod 0600 "$backup/manifest.tsv"
    for path in "${MANAGED_PATHS[@]}"; do
        destination="$backup/files$path"
        if [[ -e $(target "$path") || -L $(target "$path") ]]; then
            [[ ! -L $(target "$path") ]] || fail "Refusing to back up symlink target: $path"
            install -d -m 0700 "$(dirname -- "$destination")"
            cp -a -- "$(target "$path")" "$destination"
            printf '%s\tpresent\n' "$path" >>"$backup/manifest.tsv"
        else
            printf '%s\tabsent\n' "$path" >>"$backup/manifest.tsv"
        fi
    done
    local pointer_tmp="$ACTIVE_FILE.tmp.$$"
    printf '%s\n' "$backup" >"$pointer_tmp"
    chmod 0600 "$pointer_tmp"
    mv -f -- "$pointer_tmp" "$ACTIVE_FILE"
    echo "Created exact pre-install backup: $backup"
}

validate_backup() {
    local backup=$1 manifest path state extra allowed expected source backup_name
    manifest="$backup/manifest.tsv"
    [[ $backup == "$STATE_DIR"/backups/* ]] || fail "Backup path is outside the managed backup directory"
    backup_name=${backup#"$STATE_DIR"/backups/}
    [[ -n $backup_name && $backup_name != */* && $backup_name != . && $backup_name != .. ]] || fail "Backup path is not a direct child of the managed backup directory"
    [[ -d $backup && ! -L $backup && -f $manifest && ! -L $manifest ]] || fail "Backup directory or manifest is missing or unsafe: $backup"
    declare -A states=()
    while IFS=$'\t' read -r path state extra || [[ -n ${path:-}${state:-}${extra:-} ]]; do
        [[ -n ${path:-} && -n ${state:-} && -z ${extra:-} ]] || fail "Malformed backup manifest: $manifest"
        allowed=0
        for expected in "${MANAGED_PATHS[@]}"; do
            [[ $path == "$expected" ]] && { allowed=1; break; }
        done
        [[ $allowed == 1 ]] || fail "Unexpected path in backup manifest: $path"
        [[ -z ${states[$path]+present} ]] || fail "Duplicate path in backup manifest: $path"
        case "$state" in
            present)
                source="$backup/files$path"
                [[ -f $source && ! -L $source ]] || fail "Backed-up file is missing or unsafe for $path"
                ;;
            absent) ;;
            *) fail "Invalid backup state for $path: $state" ;;
        esac
        states["$path"]=$state
    done <"$manifest"
    for expected in "${MANAGED_PATHS[@]}"; do
        [[ -n ${states[$expected]:-} ]] || fail "Backup manifest does not cover managed path: $expected"
    done
}

restore_state() {
    if [[ ! -s $ACTIVE_FILE ]]; then
        echo "No active display install; nothing to restore"
        return 0
    fi
    local backup path state source destination
    [[ ! -L $ACTIVE_FILE ]] || fail "Active display backup pointer must not be a symlink"
    backup=$(cat "$ACTIVE_FILE")
    validate_backup "$backup"
    while IFS=$'\t' read -r path state; do
        [[ -n $path ]] || continue
        destination=$(target "$path")
        source="$backup/files$path"
        case "$state" in
            present)
                [[ -e $source || -L $source ]] || fail "Backup file missing for $path"
                install -d -m 0755 "$(dirname -- "$destination")"
                [[ ! -d $destination || -L $destination ]] || fail "Refusing to replace a directory at file path: $destination"
                rm -f -- "$destination"
                cp -a -- "$source" "$destination"
                ;;
            absent)
                [[ ! -d $destination || -L $destination ]] || fail "Refusing to remove a directory at file path: $destination"
                rm -f -- "$destination"
                ;;
            *) fail "Invalid backup state for $path: $state" ;;
        esac
    done <"$backup/manifest.tsv"
    rm -f -- "$ACTIVE_FILE"
    echo "Restored exact pre-install files from $backup"
    echo "The backup was retained for inspection. No reboot or service restart was performed."
}

install_missing_packages() {
    [[ $TEST_MODE == 1 ]] && { echo "TEST: package installation skipped"; return; }
    local packages=(chromium fonts-nunito-sans xserver-xorg-input-evdev)
    local missing=() package status
    for package in "${packages[@]}"; do
        status=$(dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null || true)
        [[ $status == ii* ]] || missing+=("$package")
    done
    if ((${#missing[@]})); then
        echo "Installing only missing display packages: ${missing[*]}"
        apt-get update
        apt-get install -y --no-upgrade "${missing[@]}"
    else
        echo "Display packages already installed"
    fi
}

copy_asset_if_absent_or_identical() {
    local source=$1 destination=$2 expected=$3 current
    if [[ -e $destination ]]; then
        [[ ! -L $destination ]] || fail "Refusing to follow symlink: $destination"
        [[ -f $destination ]] || fail "Expected a regular file: $destination"
        current=$(sha256sum "$destination" | cut -d' ' -f1)
        [[ $current == "$expected" ]] || fail "Refusing to replace a different existing file: $destination"
        return
    fi
    install -d -m 0755 "$(dirname -- "$destination")"
    if [[ $TEST_MODE == 1 ]]; then
        install -m 0644 "$source" "$destination"
    else
        install -o root -g root -m 0644 "$source" "$destination"
    fi
}

patch_text_files() {
    python3 - "$PANEL_TARGET" "$DESKTOP_TARGET" "$PCMANFM_TARGET" "$AUTOSTART_TARGET" "$DISPLAY_UID" "$DISPLAY_GID" <<'PY'
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

panel, desktop, pcmanfm, autostart = map(Path, sys.argv[1:5])
uid, gid = map(int, sys.argv[5:7])

def atomic_update(path: Path, transform):
    raw = path.read_bytes()
    info = path.stat()
    updated = transform(raw.decode("utf-8"))
    data = updated.encode("utf-8")
    if data == raw:
        return
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.picon-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(name, stat.S_IMODE(info.st_mode))
        if os.geteuid() == 0:
            os.chown(name, info.st_uid if info.st_uid != 0 else uid,
                     info.st_gid if info.st_uid != 0 else gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def eol_of(text):
    return "\r\n" if "\r\n" in text else "\n"

def set_ini(text, section, key, value):
    eol = eol_of(text)
    lines = text.splitlines(keepends=True)
    section_re = re.compile(r"^\s*\[\s*" + re.escape(section) + r"\s*\]\s*(?:\r?\n)?$", re.I)
    key_re = re.compile(r"^\s*" + re.escape(key) + r"\s*=.*?(?:\r?\n)?$", re.I)
    start = next((i for i, line in enumerate(lines) if section_re.match(line)), None)
    if start is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += eol
        lines.extend([f"[{section}]{eol}", f"{key}={value}{eol}"])
        return "".join(lines)
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[", lines[i])), len(lines))
    found = False
    output = []
    for index, line in enumerate(lines):
        if start < index < end and key_re.match(line):
            output.append(f"{key}={value}{eol if line.endswith((chr(10), chr(13))) else ''}")
            found = True
        else:
            output.append(line)
    if not found:
        output.insert(end, f"{key}={value}{eol}")
    return "".join(output)

def set_panel(text):
    eol = eol_of(text)
    match = re.search(r"(?ms)(^\s*Global\s*\{\s*\r?\n)(.*?)(^\s*\})", text)
    if not match:
        raise SystemExit(f"LXPanel config has no Global block: {panel}")
    body = match.group(2)
    for key, value in (("height", "24"), ("iconsize", "20"), ("fontsize", "10"), ("usefontsize", "1")):
        pattern = re.compile(r"(?m)^(\s*" + re.escape(key) + r"\s*=)[^\r\n]*")
        if pattern.search(body):
            body = pattern.sub(lambda m: m.group(1) + value, body)
        else:
            body += f"  {key}={value}{eol}"
    return text[:match.start(2)] + body + text[match.end(2):]

def set_autostart(text):
    eol = eol_of(text)
    lines = text.splitlines()
    required = ["@lxpanel-pi", "@pcmanfm-pi", "@xscreensaver -no-splash"]
    for entry in required:
        if entry not in [line.strip() for line in lines]:
            lines.append(entry)
    kiosk = "@chromium --kiosk --no-first-run --no-default-browser-check http://localhost:8080/"
    if not any(line.strip().startswith("@chromium ") and "http://localhost:8080/" in line for line in lines):
        lines.append(kiosk)
    suffix = eol if text.endswith(("\n", "\r")) or text == "" else ""
    return eol.join(lines) + suffix

atomic_update(panel, set_panel)
atomic_update(desktop, lambda text: set_ini(text, "GTK", "sGtk/FontName", "Nunito Sans Light 10"))
atomic_update(pcmanfm, lambda text: set_ini(text, "*", "desktop_font", "Nunito Sans Light 10"))
atomic_update(autostart, set_autostart)
PY
}

ensure_user_parent() {
    local destination=$1 home dir relative segment current
    local -a segments=()
    home=$(target "$DISPLAY_HOME")
    dir=$(dirname -- "$destination")
    [[ $dir == "$home"/* ]] || fail "User configuration path is outside $DISPLAY_HOME: $destination"
    relative=${dir#"$home"/}
    current=$home
    IFS=/ read -r -a segments <<<"$relative"
    for segment in "${segments[@]}"; do
        [[ -n $segment ]] || continue
        current="$current/$segment"
        [[ ! -L $current ]] || fail "Refusing symlink in user configuration path: $current"
        if [[ ! -d $current ]]; then
            mkdir -m 0755 -- "$current"
            [[ $TEST_MODE == 1 ]] || chown "$DISPLAY_UID:$DISPLAY_GID" "$current"
        elif [[ $TEST_MODE != 1 && $(stat -c %u "$current") != "$DISPLAY_UID" ]]; then
            fail "User configuration directory is not owned by $DISPLAY_USER: $current"
        fi
    done
}

configure_boot() {
    python3 - "$BOOT_CONFIG" <<'PY'
import os
import re
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_bytes().decode("utf-8")
eol = "\r\n" if "\r\n" in text else "\n"
begin = "# BEGIN PICON DISPLAY CONFIG"
end = "# END PICON DISPLAY CONFIG"
expected = [
    "hdmi_force_hotplug=1",
    "dtparam=spi=on",
    "dtoverlay=mhs35ips:rotate=90",
    "hdmi_group=2",
    "hdmi_mode=1",
    "hdmi_mode=87",
    "hdmi_cvt 480 320 60 6 0 0 0",
    "hdmi_drive=2",
]
lines = text.splitlines()
active = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
if begin in lines or end in lines:
    if lines.count(begin) != 1 or lines.count(end) != 1 or lines.index(begin) >= lines.index(end):
        raise SystemExit("Malformed managed block in config.txt; refusing to change it")
    if not all(line in active for line in expected):
        raise SystemExit("Managed display block is incomplete; inspect config.txt before retrying")
    raise SystemExit(0)

by_key = {}
for line in active:
    if line.startswith("dtoverlay="):
        value = line.split("=", 1)[1]
        name = re.split(r"[:,]", value, maxsplit=1)[0].lower()
        if re.search(r"mhs|mipi-dbi|ads7846|tft35|tft934|waveshare|ili948", name) and value != "mhs35ips:rotate=90":
            raise SystemExit(f"Conflicting display/touch overlay: {line}")
    if line.startswith("dtparam="):
        param = line.split("=", 1)[1]
        key = "dtparam:" + param.split("=", 1)[0]
        value = param.split("=", 1)[1] if "=" in param else ""
    elif "=" in line:
        key, value = line.split("=", 1)
    else:
        key, _, value = line.partition(" ")
    by_key.setdefault(key.strip(), []).append(value.strip())

wanted = {
    "hdmi_force_hotplug": ["1"], "dtparam:spi": ["on"],
    "hdmi_group": ["2"],
    "hdmi_mode": ["1", "87"], "hdmi_cvt": ["480 320 60 6 0 0 0"],
    "hdmi_drive": ["2"],
}
for key, values in wanted.items():
    actual = by_key.get(key, [])
    if key == "hdmi_mode":
        if actual and actual != values[:len(actual)]:
            raise SystemExit(f"Conflicting {key} settings in config.txt: {actual}")
    elif actual and actual != values:
        raise SystemExit(f"Conflicting {key} setting in config.txt: {actual}")

last_all = max((i for i, line in enumerate(lines) if re.fullmatch(r"\s*\[all\]\s*", line)), default=-1)
effective = [line.strip() for line in lines[last_all + 1:] if line.strip() and not line.lstrip().startswith("#")]
missing = [line for line in expected if line not in effective]
if not missing:
    raise SystemExit(0)
if text and not text.endswith(("\n", "\r")):
    text += eol
block = ["[all]", begin, *missing, end]
text += eol.join(block) + eol
fd, temp_name = tempfile.mkstemp(prefix=".config.txt.picon-", dir=path.parent)
try:
    original = path.stat()
    os.fchmod(fd, original.st_mode & 0o7777)
    if os.geteuid() == 0:
        os.fchown(fd, original.st_uid, original.st_gid)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)
    Path(temp_name).replace(path)
finally:
    Path(temp_name).unlink(missing_ok=True)
PY
}

copy_templates() {
    local pair source destination
    local -a pairs=(
        "/etc/xdg/lxpanel-pi/panels/panel|$PANEL_TARGET"
        "/etc/xdg/lxsession/rpd-x/desktop.conf|$DESKTOP_TARGET"
        "/etc/xdg/pcmanfm/default/desktop-items-0.conf|$PCMANFM_TARGET"
    )
    for pair in "${pairs[@]}"; do
        source=$(target "${pair%%|*}")
        destination=${pair#*|}
        ensure_user_parent "$destination"
        if [[ ! -e $destination ]]; then
            [[ -f $source ]] || fail "Missing LXDE template: $source"
            cp -a -- "$source" "$destination"
            if [[ $TEST_MODE != 1 ]]; then chown "$DISPLAY_UID:$DISPLAY_GID" "$destination"; fi
        fi
    done
    ensure_user_parent "$AUTOSTART_TARGET"
    if [[ ! -e $AUTOSTART_TARGET ]]; then
        : >"$AUTOSTART_TARGET"
        [[ $TEST_MODE == 1 ]] || chown "$DISPLAY_UID:$DISPLAY_GID" "$AUTOSTART_TARGET"
    fi
}

verify_configuration() {
    local line
    for line in "$OVERLAY_TARGET" "$CALIBRATION_TARGET" "$EVDEV_TARGET" \
        "$AUTOSTART_TARGET" "$DESKTOP_TARGET" "$PANEL_TARGET" "$PCMANFM_TARGET"; do
        [[ -f $line ]] || fail "Installed display configuration file is missing: $line"
    done
    for line in \
        'hdmi_force_hotplug=1' 'dtparam=spi=on' \
        'dtoverlay=mhs35ips:rotate=90' 'hdmi_group=2' 'hdmi_mode=1' 'hdmi_mode=87' \
        'hdmi_cvt 480 320 60 6 0 0 0' 'hdmi_drive=2'; do
        grep -Fxq "$line" <(tr -d '\r' <"$BOOT_CONFIG") || fail "Missing boot setting: $line"
    done
    [[ $(sha256sum "$OVERLAY_TARGET" | cut -d' ' -f1) == "$EXPECTED_OVERLAY_SHA256" ]] || fail "Installed display overlay does not match audited CN0516 asset"
    cmp -s "$CALIBRATION_TARGET" "$ASSET_DIR/99-calibration.conf" || fail "Touch calibration file differs from the audited profile"
    cmp -s "$EVDEV_TARGET" "$EVDEV_SOURCE" || fail "Xorg evdev touchscreen rule is missing or differs from the installed package default"
    grep -Fxq '@lxpanel-pi' "$AUTOSTART_TARGET" || fail "LXPanel startup entry missing"
    grep -Fxq '@pcmanfm-pi' "$AUTOSTART_TARGET" || fail "PCManFM startup entry missing"
    grep -Fxq '@xscreensaver -no-splash' "$AUTOSTART_TARGET" || fail "XScreenSaver startup entry missing"
    grep -Eq '^@chromium .*--kiosk.*http://localhost:8080/' "$AUTOSTART_TARGET" || fail "Chromium kiosk startup entry missing"
    python3 - "$PANEL_TARGET" "$DESKTOP_TARGET" "$PCMANFM_TARGET" <<'PY'
import re
import sys
from pathlib import Path

panel, desktop, pcmanfm = (Path(p).read_text(encoding="utf-8") for p in sys.argv[1:])
match = re.search(r"(?ms)^\s*Global\s*\{(.*?)^\s*\}", panel)
if not match:
    raise SystemExit("LXPanel Global config missing")
values = dict(re.findall(r"(?m)^\s*(height|iconsize|fontsize|usefontsize)\s*=\s*(\S+)", match.group(1)))
if values != {"height": "24", "iconsize": "20", "fontsize": "10", "usefontsize": "1"}:
    raise SystemExit(f"Unexpected LXPanel values: {values}")
if not re.search(r"(?m)^sGtk/FontName=Nunito Sans Light 10\s*$", desktop):
    raise SystemExit("LXDE GTK font is not Nunito Sans Light 10")
if not re.search(r"(?m)^desktop_font=Nunito Sans Light 10\s*$", pcmanfm):
    raise SystemExit("PCManFM desktop font is not Nunito Sans Light 10")
PY
    if [[ $TEST_MODE != 1 ]]; then
        grep -q '^fb_ili9486 ' "$(target /proc/modules)" || fail "The MHS35IPS framebuffer module is not loaded yet"
        grep -q 'Name="ADS7846 Touchscreen"' "$(target /proc/bus/input/devices)" || fail "The ADS7846 touchscreen is not enumerated yet"
        grep -q 'Nunito Sans' <(fc-match 'Nunito Sans:style=Light') || fail "Nunito Sans Light is not installed or discoverable"
        if [[ -n ${DISPLAY:-} ]] && command -v xrandr >/dev/null 2>&1; then
            xrandr --query | grep -q 'current 480 x 320' || fail "X11 is not currently using 480 x 320"
        fi
    fi
    echo "Persistent display configuration verified"
}

case "$ACTION" in
    dry-run)
        check_assets
        check_system
        check_boot_conflicts
        check_desktop_templates
        cat <<EOF
Dry run only; no files or packages changed.
Target: Raspberry Pi 4 / Debian Trixie / LightDM rpd-x (X11)
Kernel drivers: fb_ili9486 and ads7846 are available for the active kernel
LCD: MHS35IPS overlay, SPI0 CE0, 115 MHz, GPIO24 D/C, GPIO25 reset, rotation 90, 480x320 HDMI timing
Touch: ADS7846 on SPI0 CE1, 2 MHz, GPIO17 falling-edge IRQ, 60-ohm plate, pressure max 255
Xorg: audited ADS7846 calibration plus evdev rule
LXDE: preserve base autostart; ensure Chromium kiosk at http://localhost:8080/
LXPanel: height 24, icon size 20, font size 10
Fonts: Nunito Sans Light 10 for LXDE and PCManFM
Backup: $STATE_DIR/backups (created on first install)
EOF
        ;;
    restore)
        [[ $TEST_MODE == 1 || $EUID -eq 0 ]] || fail "Restore requires root; run with sudo"
        restore_state
        ;;
    verify)
        check_assets
        check_system
        verify_configuration
        ;;
    install)
        [[ $TEST_MODE == 1 || $EUID -eq 0 ]] || fail "Install requires root; run with sudo"
        check_assets
        check_system
        check_boot_conflicts
        check_desktop_templates
        backup_state
        install_missing_packages
        [[ -f $EVDEV_SOURCE ]] || fail "Missing Xorg evdev rule after package installation"
        copy_templates
        configure_boot
        copy_asset_if_absent_or_identical "$ASSET_DIR/mhs35ips.dtbo" "$OVERLAY_TARGET" "$EXPECTED_OVERLAY_SHA256"
        copy_asset_if_absent_or_identical "$ASSET_DIR/99-calibration.conf" "$CALIBRATION_TARGET" "$EXPECTED_CALIBRATION_SHA256"
        if [[ -e $EVDEV_TARGET ]]; then
            [[ ! -L $EVDEV_TARGET ]] || fail "Refusing to follow symlink: $EVDEV_TARGET"
            cmp -s "$EVDEV_TARGET" "$EVDEV_SOURCE" || fail "Refusing to replace a different Xorg evdev rule: $EVDEV_TARGET"
        else
            install -d -m 0755 "$(dirname -- "$EVDEV_TARGET")"
            if [[ $TEST_MODE == 1 ]]; then
                install -m 0644 "$EVDEV_SOURCE" "$EVDEV_TARGET"
            else
                install -o root -g root -m 0644 "$EVDEV_SOURCE" "$EVDEV_TARGET"
            fi
        fi
        patch_text_files
        # The live module/input checks are expected to become true after the next user-initiated reboot.
        echo "Display files installed. No service, desktop session, or Raspberry Pi was restarted."
        echo "Backup available with: sudo bash $0 --restore"
        ;;
esac
