# Pi-Con

Pi-Con listens to a microphone, detects an [OpenWakeWord](https://github.com/dscripka/openWakeWord) model, and launches a configured command. The code runs on Windows and macOS for development; deploy the Linux ARM64 image to a Raspberry Pi.

## Local development

Requires Python 3.11+ and a working microphone.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pi-con --list-devices
pi-con
```

On macOS/Linux, activate with `source .venv/bin/activate`. Update `config/settings.json` with the chosen device and the command that should run after detection. Commands are executed directly, not through a shell.

OpenWakeWord's bundled `hey_jarvis` model is the initial default. Add a custom model later by extending `WakeWordListener` to pass its path into `Model(wakeword_models=[...])`.

## Container use

Audio devices belong to the host, so the Docker image is portable but microphone access is enabled only at runtime. Docker Desktop on Windows and macOS cannot expose the host microphone as `/dev/snd`; use native Python mode for microphone testing there.

On Linux (including Raspberry Pi OS), enable the Raspberry Pi hardware override:

```sh
docker compose -f compose.yaml -f compose.pi.yaml up -d --build
```

```sh
docker buildx build --platform linux/arm64 --load -t pi-con:latest .
```

## Deploy to the Pi

Install Docker and enable SSH on the Pi first. From Windows PowerShell:

```powershell
.\scripts\deploy.ps1 -HostName raspberrypi.local
```

This builds an ARM64 image locally, sends it over SSH, copies the configuration, and starts a restartable container with `/dev/snd` access. Use a 64-bit Pi OS for `linux/arm64`; pass `-Platform linux/arm/v7` on a 32-bit system if your dependencies support it.

## GPU

The service starts with the CPU backend, which is the expected and simplest Raspberry Pi configuration. GPU support is deliberately optional: it requires a compatible host driver and a runtime-specific container configuration. Add it only after confirming the Pi hardware and ONNX Runtime execution provider; the microphone path is independent of GPU use.
