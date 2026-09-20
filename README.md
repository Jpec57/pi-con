# Pi-Con

Pi-Con listens to a microphone, detects an [OpenWakeWord](https://github.com/dscripka/openWakeWord) wake word, and launches a configured command. Develop natively on macOS, deploy the Linux ARM64 Docker image to a Raspberry Pi. See [PROJECT.md](PROJECT.md) for goals, decisions and status.

## Run on your Mac (test the wake word)

Docker Desktop cannot expose the Mac microphone, so run natively.

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

Once running, open `http://<host>:8080` (`localhost` on the Mac, the Pi's IP on the Pi). It shows the microphone level, the wake word score against the threshold, the last thing heard, and a history. Speech-to-text transcripts will appear in the same page once STT exists. There is no login: keep it on your LAN and never port-forward it. Change the port with `"ui_port"` in `config/settings.json` (and the `8080:8080` mapping in `compose.yaml` / `scripts/deploy.sh`); `null` disables the UI.

## Run with Docker

### On your Mac (no microphone: replays a sample)

Docker Desktop on macOS cannot expose your microphone to containers, so this verifies the image, the model download, the UI and the detection pipeline by replaying a synthetic "Hey Jarvis" on a loop. Apple Silicon builds a native `linux/arm64` image, the same architecture as the Pi.

1. Start Docker Desktop.
2. Create the sample (macOS `say` → 16 kHz mono WAV): `./scripts/make-sample.sh`
3. Build and run:
   ```sh
   docker compose -f compose.yaml -f compose.mac.yaml up --build
   ```
4. Open **http://localhost:8080**. Every ~7 seconds you should see a `WAKE hey_jarvis` entry, and the logs show `Wake word detected`. The container configured command (`/usr/bin/logger`) runs inside the container.
5. Stop with `Ctrl-C`, then `docker compose down`.

To test with your real microphone on the Mac, use the native steps above.

### On the Raspberry Pi (real microphone)

On the Pi (64-bit OS): install Docker, add yourself to the groups (`sudo usermod -aG docker,audio $USER`, log out/in), and plug in the USB microphone.

```sh
arecord -l                       # the mic must be listed
docker compose -f compose.yaml -f compose.pi.yaml up -d --build
docker compose logs -f           # follow the detections
```

If the default device isn't the mic, run `docker compose run --rm pi-con --list-devices` and set `"audio_device"` in `config/settings.json` (an index, or a name substring such as `"XVF3800"`). The UI is at `http://<pi-ip>:8080`; on a screen attached to the Pi, open `http://localhost:8080` in its browser.

You can also build for the Pi from your Mac and push it over SSH, see below.

## Deploy to the Pi (free, plain SSH)

Prerequisites on the Pi: Docker installed, SSH enabled, your user in the `docker` and `audio` groups, and your SSH key authorised (`ssh-copy-id pi@raspberrypi.local`). The Pi is a 64-bit Pi 4 (`linux/arm64`).

```sh
cp .env.example .env      # then fill in PI_HOST, PI_USER, PI_SSH_KEY_PATH...
./scripts/deploy.sh
```

The script builds the arm64 image on your Mac, streams it over SSH (`docker save | ssh docker load`, no registry, no paid service), copies `config/settings.json`, restarts the container with `/dev/snd` and the UI port, and fails if the container isn't stable after 8 s. It prints the UI address. Logs: `ssh pi@<host> docker logs -f pi-con`.

### CI (GitHub Actions)

`.github/workflows/deploy.yml` runs the tests on every push and pull request. The **deploy job is manual** (Actions tab → Run workflow) because of a networking fact, not a paid feature: a GitHub-hosted runner runs on the internet, so it cannot open an SSH connection to a Pi that only exists on your home network. SSH is sufficient *if the Pi is reachable*; your options, all free, are:

- **Deploy from your Mac** with `./scripts/deploy.sh` (recommended, nothing to set up).
- Install a [self-hosted runner](https://docs.github.com/en/actions/hosting-your-own-runners) on a machine on your LAN.
- Join the runner and the Pi to a free Tailscale network, or forward SSH from your router (not recommended: it exposes the Pi to the internet).

If you use the manual deploy job, add these repository secrets (`.env` is git-ignored, so CI can't read it): `PI_HOST`, `PI_SSH_PRIVATE_KEY` (required), and optionally `PI_USER`, `PI_PORT`, `PI_KNOWN_HOSTS` (output of `ssh-keyscan <host>`), `PI_TARGET_DIR`.

## Hardware acceleration

The service uses the CPU backend (TFLite on the Pi), which is plenty for a wake word model. The Pi 4 has no NPU, so STT will run on the CPU; see PROJECT.md for the engine choices and constraints.
