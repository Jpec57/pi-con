# Pi-Con display installer

`install-display.sh` recreates the currently working SunFounder CN0516 / MHS35IPS setup on a Raspberry Pi 4 running Raspberry Pi OS Debian Trixie with the LightDM `rpd-x` LXDE/X11 session. It configures the existing FBTFT display path, Xorg touchscreen calibration, LXPanel, desktop font, and Chromium kiosk startup. It does not install the experimental `panel-mipi-dbi` prototype.

## Reinstall from a new or re-imaged SD card

This procedure restores the audited setup from the repository; it does not need files from the previous SD card. Use Raspberry Pi OS Desktop 64-bit (Debian Trixie) on a Raspberry Pi 4, create the `pi-con` desktop account, connect the Pi to the network, and enable SSH if you will administer it remotely. Keep the `raspberrypi-ui-mods` desktop package and select LightDM with the LXDE/X11 `rpd-x` session and autologin for `pi-con` before running the installer. The installer deliberately does not switch a Wayland/labwc system to X11.

These are manual prerequisites; the installer checks them and stops if they are not met:

1. At the graphical login screen, select the Raspberry Pi X11/LXDE `rpd-x` session. Do not continue while the session is Wayland/labwc.
2. Ensure `/etc/X11/default-display-manager` contains `/usr/sbin/lightdm` and `/usr/share/xsessions/rpd-x.desktop` exists.
3. In `/etc/lightdm/lightdm.conf`, under `[Seat:*]`, configure `user-session=rpd-x`, `autologin-user=pi-con`, and `autologin-session=rpd-x`. Use `sudoedit /etc/lightdm/lightdm.conf` if these values need correction.
4. Verify the account and session settings before installation:

   ```bash
   id pi-con
   cat /etc/X11/default-display-manager
   test -f /usr/share/xsessions/rpd-x.desktop
   grep -E '^[[:space:]]*(user-session|autologin-user|autologin-session)[[:space:]]*=' /etc/lightdm/lightdm.conf
   ```

From an SSH session (or a local terminal on the Pi), run:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/Jpec57/pi-con.git
cd pi-con
PICON_DISPLAY_USER=pi-con bash scripts/install-display.sh --dry-run
sudo PICON_DISPLAY_USER=pi-con bash scripts/install-display.sh
PICON_DISPLAY_USER=pi-con bash scripts/install-display.sh --verify
```

`--dry-run` and `--verify` are read-only and do not require `sudo`. The install step checks that LightDM is selected and configured for `rpd-x`, that the chosen account is its autologin user, and that the active Raspberry Pi kernel module tree provides both `fb_ili9486` and `ads7846` (as loadable or built-in drivers). It stops before taking a backup or writing configuration if those prerequisites differ or another known display/touch overlay conflicts. It does not change LightDM to X11.

The script installs only missing packages from the Raspberry Pi OS repositories: `chromium`, `fonts-nunito-sans`, and `xserver-xorg-input-evdev`. It does not upgrade already-installed packages. The Pi must have working package-repository access if any package is missing. The LCD and touchscreen kernel drivers are supplied by the Raspberry Pi OS kernel package, not copied from the current Pi or bundled as `.ko` files: kernel modules are tied to the installed kernel ABI. The bundled hardware-specific resources are the audited `mhs35ips.dtbo` overlay and ADS7846 Xorg calibration. The Xorg evdev rule is copied from the installed `xserver-xorg-input-evdev` package. If either required kernel module is absent, the preflight stops; this installer does not compile or fetch an out-of-tree driver.

After install, inspect the result with:

```bash
PICON_DISPLAY_USER=pi-con bash scripts/install-display.sh --verify
```

The installer does not restart LXDE, Chromium, the Pi-Con service, or the Pi. Log out and back in or reboot manually to apply boot and session changes. Then run `--verify` again. From an X11 terminal, additional live checks are:

```bash
xrandr --query
xinput list --short
xinput list-props "ADS7846 Touchscreen"
systemctl is-active pi-con.service
curl -fsS http://localhost:8080/ >/dev/null
```

`--verify` checks boot settings, the installed overlay/calibration checksums, the evdev Xorg rule, LXDE autostart, panel values, configured fonts, and (after reboot) the loaded `fb_ili9486` module and enumerated ADS7846 input device. The live X11 commands confirm the actual mode and input calibration.

## Audited hardware settings

These values were read from the running Pi on 2026-11-04 and matched against the checked-in SunFounder assets by SHA-256. The boot configuration, kernel modules, framebuffer, XInput device, and current XRandR mode were also checked read-only.

| Part | Active value |
| --- | --- |
| LCD | SunFounder CN0516 / MHS35IPS, 480 × 320, SPI0 CE0 |
| LCD driver | Historical `ilitek,ili9486` overlay using `fb_ili9486` and FBTFT; the panel is documented as ST7796U |
| LCD SPI clock | 115 MHz, as declared by the active SunFounder DTBO |
| LCD control | D/C GPIO24, reset GPIO25, rotation 90° |
| Touch | ADS7846/XPT2046 on SPI0 CE1, 2 MHz |
| Touch IRQ | BCM GPIO17, falling edge |
| Touch plate/pressure | 60 ohms, maximum pressure 255 |
| Touch Xorg calibration | `3936 227 3880 268`, axes swapped; third-button emulation retained |
| X11 mode | `default` output at 480 × 320 |
| LXPanel | Top edge, 24 px high, 20 px icons, 10 px text |
| Font | Nunito Sans Light 10 for LXDE and PCManFM labels |
| Browser | `@chromium --kiosk --no-first-run --no-default-browser-check http://localhost:8080/` in the per-user `rpd-x` autostart |

The overlay asset checksum is `cf8ea1ad0fb4e8e3246bdd9bccd6ce2806dd4b163dddfaf1cdd3a31b3cbb7cd5`; it exactly matched both `/boot/firmware/overlays/mhs35ips.dtbo` and `mhs35ips-overlay.dtb` on the Pi. The calibration asset checksum is `193dc44ae25aa2956f0e37f346336a78ba3b7dc92b4741a74815979cd958b39b`; it exactly matched `/etc/X11/xorg.conf.d/99-calibration.conf`.

The current Pi has the matching `mhs35ips:rotate=90` boot directive, a 480 × 320 HDMI timing block, active `fb_ili9486`, and an ADS7846 input device. The audited boot file also has I²C and UART enabled; the installer leaves those values alone and manages only the SPI and display settings needed for this screen. The experimental `mipi-dbi-spi` and separate `ads7846` overlays are not used by this installer.

## Backups and restore

Before the first write, the script stores exact copies and an existence manifest for every managed file under `/var/lib/picon-display/backups/` and writes `/var/lib/picon-display/active-backup`. Re-running the installer reuses that first snapshot instead of backing up its own changes. The backup includes original permissions, ownership, file contents, and whether each file was absent.

If an installation attempt must be undone while the same SD card and its backup are available, restore the exact pre-install files with:

```bash
sudo PICON_DISPLAY_USER=pi-con bash scripts/install-display.sh --restore
```

Restore is repeat-safe after the active transaction is cleared. Before changing anything, it validates the backup path and the complete manifest against the installer's fixed managed-file list. It restores the original boot config, overlay-file presence, Xorg calibration and evdev files, and the selected user's LXDE/LXPanel/PCManFM files. It retains the backup directory and does not remove packages installed by the install command. It does not reboot; reboot manually if boot settings were restored.

The exact pre-install backup is stored on the Pi's SD card under `/var/lib/picon-display/`; it does **not** survive loss or re-imaging of that card. After an SD-card failure, follow the clean-card procedure above to recreate the known-good display setup from the GitHub repository. If you need exact restoration of a customized pre-install state, copy `/var/lib/picon-display/backups/` and `active-backup` to separate storage before replacing the card.

## Scope and limitations

The installer changes only these configuration targets: `/boot/firmware/config.txt`, `/boot/firmware/overlays/mhs35ips.dtbo`, `/etc/X11/xorg.conf.d/99-calibration.conf`, `/usr/share/X11/xorg.conf.d/45-evdev.conf`, and the chosen user's LXDE autostart, desktop, panel, and PCManFM desktop-font files. It preserves existing LXDE autostart entries, including `@lxpanel-pi`, `@pcmanfm-pi`, and `@xscreensaver -no-splash`, and adds the kiosk command only if one for `http://localhost:8080/` is not already present.

It does not edit Wayland/labwc configuration, Bluetooth/audio configuration, the Pi-Con service, or Xorg files unrelated to the touchscreen. It does not test physical touch accuracy or trigger a reboot. No software rollback can recover a Pi that cannot boot far enough to run Linux; in that case recover the SD card or re-image it, then use the clean-card procedure. The legacy overlay declares 115 MHz; the installer preserves the currently working setting rather than substituting the unvalidated lower-speed DRM prototype.

## Local tests

The lifecycle test runs against a temporary fake filesystem and never contacts the Pi:

```bash
bash scripts/tests/test_install_display.sh
```

It checks dry-run non-mutation, install and verify, idempotency, restoration including files initially absent, preservation of the three LXDE defaults and unrelated sentinel files, and refusal of a conflicting display overlay.
