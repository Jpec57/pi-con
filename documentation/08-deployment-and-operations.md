# 08 Deployment and operations

## Deploying

From the Mac: `./scripts/deploy.sh` (reads `.env` for `PI_HOST`, `PI_USER`, `PI_PORT`, `PI_SSH_KEY_PATH`). A value given on the command line wins over `.env`: `PI_HOST=192.168.1.162 ./scripts/deploy.sh`.

What it does, in order:

1. Checks SSH (key only, `BatchMode`) and prints the Pi's architecture and Python version.
2. Sends `pyproject.toml`, `README.md`, `requirements-pi.txt`, `src/` and `config/` as a tar stream. **`config/settings.json` and `config/skills.json` are overwritten on every deploy.**
3. Merges only the AI keys and the MCP token from the Mac's `.env` into the Pi's private `.env` (mode 600), leaving the rest (Home Assistant token...) alone.
4. On the Pi: installs `libportaudio2` and `python3-venv` if missing, creates `.venv`, installs `requirements-pi.txt` (with `--timeout 30 --retries 5`), installs openWakeWord with `--no-deps` (it demands `tflite-runtime`, which has no wheel for Python 3.13, so the ONNX backend is used), then runs `pi-con --download-models` (wake word, French STT, Piper voice, Needle).
5. Writes `/etc/systemd/system/pi-con.service`, enables and restarts it.
6. Waits 8 s and fails with the last 50 log lines if the service is not active.

Re-run it after every change. There is no rollback; the previous version is whatever is in git.

## The systemd unit

```
[Unit]   After=network-online.target sound.target user@<uid>.service   Wants=user@<uid>.service
[Service] User=<service user>   SupplementaryGroups=audio
          Environment=XDG_RUNTIME_DIR=/run/user/<uid>
          WorkingDirectory=~/pi-con   ExecStart=~/pi-con/.venv/bin/pi-con
          Restart=always   RestartSec=3
```

`<uid>` is computed on the Pi at deploy time. `XDG_RUNTIME_DIR` is what gives the service the `pulse` audio device (doc 06). The service runs as the normal user, not root, and starts at boot.

## State on the Pi (`~/pi-con`)

| Path | Content | Safe to delete? |
|---|---|---|
| `.env` | Secrets and Home Assistant token (mode 600) | No |
| `.state/secrets.json`, `ai.json`, `tts.json` | Keys saved from the UI, AI and voice choices | Yes (back to `.env` and settings defaults) |
| `.state/recordings/` | The last 5 sentences (WAV + JSON) | Yes |
| `models/` | Wake word, French STT, Piper voice, Needle | Yes, re-downloaded by the next deploy (slowly: see below) |
| `.venv/` | Python environment | Yes, rebuilt |
| `~/homeassistant` | Home Assistant Core, its own venv | No |

## Running day to day

- Logs: `ssh <pi> journalctl -fu pi-con`; the last lines are also in the UI (gear, Recent logs).
- Restart: `ssh <pi> sudo systemctl restart pi-con`, or the "Restart app" button.
- Which devices exist: `ssh <pi> ~/pi-con/.venv/bin/pi-con --list-devices`. Set `audio_device` (microphone) and `tts.output_device` (speaker) by index or by name substring.
- The microphone and the stream watchdog: doc 02.

## Home Assistant

HA Core runs natively on the Pi (`scripts/install-ha.sh`, about 730 MB, systemd unit `homeassistant`, port 8123), no Docker. A camera is the "generic" integration with its RTSP URL. The long-lived HA token and the RTSP credentials live only in `.env` files, never in git or the browser. `scripts/ha_setup.py` creates the admin user and the token.

## Known operational traps (all hit at least once)

| Symptom | Cause | Fix |
|---|---|---|
| Deploy "stuck" on `pip` | A stalled IPv6 download from PyPI; `pip` never noticed the dead connection | Retries and timeout are now in `deploy.sh`; kill the hung `pip` and rerun |
| Deploy "stuck" on the voice or model download | Hugging Face reaches the Pi at about 80 KB/s | Copy the file from the Mac (`scp models/tts-fr/* pi:pi-con/models/tts-fr/`) and redeploy |
| `raspberrypi.local` does not resolve | mDNS dropped on the Mac | `PI_HOST=<ip> ./scripts/deploy.sh` |
| AI answer understood but nothing shown or heard | A browser tab older than the deploy (fixed: pages reload themselves) | Reload the tab |
| AI answers are silent | No audio device for the service (fixed), or nothing connected to HDMI | Doc 06 |
| `CERTIFICATE_VERIFY_FAILED` downloading on the Mac | python.org Python has no CA bundle | Run "Install Certificates.command" or set `SSL_CERT_FILE` to certifi's bundle |
| `input overflow` warnings | About one per second on the Pi; cause not found | Try another USB port or a powered hub |
| Native `recursive_mutex lock failed` at the end of a full `pytest` run | Not investigated; all tests pass | None |

## Testing

`pytest` (venv in `.venv`, `pip install -e ".[dev]"`). Tests use fakes for the microphone, the speaker, the network and the providers; none needs hardware or internet. There is **no CI workflow in the repository today** (the README and `PROJECT.md` still mention one). GitHub-hosted runners could never reach a Pi on a home network, so deployment stays a manual step from the Mac.
