# Pi-Con

Pi-Con listens to a microphone, detects an [OpenWakeWord](https://github.com/dscripka/openWakeWord) wake word, and launches a configured command. Develop natively on macOS, deploy to a Raspberry Pi 4/5 over SSH. See [PROJECT.md](PROJECT.md) for goals, decisions and status.

## Run on your Mac (test the wake word)



1. **Python 3.11+**: `python3 --version` (install with `brew install python@3.12` if older).
2. **Create a venv and install:**
   ```sh
   cd pi-con
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```
3. **Download the models** (once; the pip package ships none, that was the original crash):
   ```sh
   pi-con --download-models
   ```
4. **Allow microphone access.** The first run triggers a macOS prompt for your terminal app (Terminal, iTerm, VS Code…). If it doesn't, or you clicked "Don't Allow": System Settings → Privacy & Security → Microphone → enable your terminal, then restart it.
5. **Pick the input device:**
   ```sh
   pi-con --list-devices
   ```
   Note the index of your mic (the line marked `>` is the default). Optionally set `"audio_device"` in `config/settings.json` to that index. `null` uses the default.
6. **Make the trigger audible** by setting the command in `config/settings.json`:
   ```json
   "command": ["/usr/bin/say", "Yes?"]
   ```
7. **Run with live scores:**
   ```sh
   pi-con --verbose
   ```
   Say **"Hey Jarvis"** at normal volume, 1–2 m from the mic. You should see `score 0.xxx` lines rise, then `Wake word detected` and hear the reply.
   Open **http://localhost:8080** to see the live UI (mic level, wake score, detected words).
8. **Tune:** if it misses you, lower `"threshold"` (0.5 → 0.3); if it triggers on other speech, raise it (0.5 → 0.7). `"cooldown_seconds"` (default 2) prevents repeat triggers.
9. **Run the tests:** `pytest`.

Troubleshooting:

- `PortAudio`/`Input overflow` warnings: close other audio-heavy apps; usually harmless.
- No scores at all: wrong device (step 5) or mic permission (step 4). Check with `pi-con --list-devices`.
- Silence produces scores near 0.000 and nothing is printed in `--verbose` (only scores ≥ 0.1 are shown).

Config keys in `config/settings.json`: `wakeword` (`hey_jarvis`, `alexa`, `hey_mycroft`, `hey_rhasspy`, `timer`, `weather`, or a path to a custom `.onnx`/`.tflite`), `threshold`, `audio_device`, `command` (executed directly, not through a shell), `cooldown_seconds`.

## The web UI

Once running, open `http://<host>:8080` (`localhost` on the Mac, the Pi's IP on the Pi). It shows the microphone level, the wake word score against the threshold, the last thing heard, and a history. Speech-to-text transcripts will appear in the same page once STT exists. There is no login: keep it on your LAN and never port-forward it. Change the port with `"ui_port"` in `config/settings.json`; `null` disables the UI.

## Deploy to the Pi (Pi 4 or Pi 5, 64-bit OS)

One-time setup on the Pi: SSH enabled, your key authorised (`ssh-copy-id pi@raspberrypi.local`), a USB microphone plugged in (`arecord -l` lists it). Your user needs `sudo` (the default Pi user has it).

On your Mac:

```sh
cp .env.example .env      # fill in PI_HOST (and PI_USER, PI_SSH_KEY_PATH if needed)
./scripts/deploy.sh
```

That is all. The script copies the code over SSH, creates a venv on the Pi, installs the package and models, installs a `pi-con` systemd service (starts at boot, restarts on failure), restarts it, and fails with the logs if it isn't running after 8 s. It prints the UI address. Re-run it after every change. `config/settings.json` is overwritten on each deploy.

Logs: `ssh pi@<host> journalctl -fu pi-con`. If the default audio device isn't the mic, run `ssh pi@<host> /home/pi/pi-con/.venv/bin/pi-con --list-devices` and set `"audio_device"` in `config/settings.json` (an index, or a name substring such as `"XVF3800"`).

Testing without a microphone: `./scripts/make-sample.sh` (macOS) then `pi-con --wav samples/hey_jarvis.wav`.

### CI

`.github/workflows/test.yml` runs the tests on every push and pull request. Deploying is done from your Mac only: GitHub-hosted runners cannot reach a Pi on your home network.

## Hardware acceleration

The service uses the CPU backend (TFLite on the Pi), which is plenty for a wake word model. The Pi 4 has no NPU, so STT will run on the CPU; see PROJECT.md for the engine choices and constraints.
