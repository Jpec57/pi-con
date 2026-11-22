# Local STT engine selector (experimental)

This branch adds a local speech recognizer selector alongside the existing
OpenAI audio transcription setting. It currently exposes four engines:
Zipformer FR (the existing default), Canary 180M INT8, Orukeet INT8, and Vosk
(Small French or Small US English). No optional model weights are downloaded
by Pi-Con. Model selection is saved only after a worker has loaded and passed
its health probe. A rejected load returns to Zipformer when it is available.

**This remains an experimental feature.** Jpec has reported a trial on the Pi 4
with 2 GB RAM. This repository does not include the trial's engine-by-engine
results, software versions, or resource measurements, so that report does not
establish compatibility or performance for all listed engines. Use the
monitoring and rollback steps below. Do not turn off OS memory protections or
add swap as a workaround.

## Install this branch without replacing the current installation

The commands below keep the trial in a separate directory and do not install,
enable, edit, or restart a systemd unit. Adapt `pi` to the Pi's login name.

1. Record the existing install and service state. Back up any local settings
   before proceeding:

   ```sh
   systemctl status pi-con --no-pager
   cd ~
   git clone --branch test/local-stt-selector-pi4 --single-branch \
     https://github.com/Jpec57/pi-con.git pi-con-stt-selector
   cd ~/pi-con-stt-selector
   ```

2. Confirm architecture and Python. This branch expects Python 3.11 or newer;
   Jpec's known install uses Python 3.13 on 64-bit Raspberry Pi OS. Confirm the
   actual interpreter is ARM64 (`aarch64`) before installing native wheels:

   ```sh
   uname -m
   python3 --version
   python3 -c 'import platform; print(platform.machine())'
   ```

3. Install OS audio/venv prerequisites, then make an isolated virtual
   environment. The test copy's `.env`, settings, state, and model paths are
   separate from the existing installation. Do not copy `.env` or commit
   settings or credentials. Create a local test config from the tracked
   defaults, then edit it to use unused UI and MCP ports, set
   `ai.cloud_stt` to `false`, and use a harmless command such as
   `["/usr/bin/true"]` so a wake-word false positive cannot run a
   home-automation action:

   ```sh
   cp config/settings.json config/settings-test.json
   ${EDITOR:-nano} config/settings-test.json
   ```

   Keep this file local and do not stage it.

   ```sh
   sudo apt-get update
   sudo apt-get install --no-install-recommends libportaudio2 python3-venv
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install --upgrade pip
   python -m pip install -r requirements-pi.txt
   python -m pip install --no-deps 'openwakeword>=0.6,<0.7' .
   python -m pip install 'vosk>=0.3.45'
   python -c 'import sherpa_onnx; print("sherpa_onnx", sherpa_onnx.__file__)'
   python -c 'import vosk; print("vosk import OK")'
   ```

   `sherpa-onnx` supplies Canary and Orukeet; `vosk` is optional. The existing
   Pi deployment installs OpenWakeWord with `--no-deps` because its
   `tflite-runtime` dependency has no wheel for recent Python, then installs
   the supported ONNX runtime dependencies from `requirements-pi.txt`.
   The [sherpa-onnx install guide](https://k2-fsa.github.io/sherpa/onnx/python/install.html)
   documents CPU wheels for Linux aarch64. [PyPI's Vosk release files](https://pypi.org/project/vosk/)
   include Vosk 0.3.45 as a Python 3, manylinux2014 aarch64 wheel. That wheel
   tag does not prove successful runtime operation on this exact Pi/Python
   image: the two import checks above are required. If pip reports no matching
   ARM64 wheel or either import fails, stop and record the error; do not build
   native libraries on the Pi as an unplanned workaround.

4. Keep Zipformer's existing model directory available to the test copy (the
   configured `stt_model_dir`, normally `models/stt-fr`). If you copy or
   symlink an existing model, verify the real path and permissions first.
   Never move or delete the live installation's weights.

## Get model weights legally

Download only from the upstream model publisher or the model's official
project page. Read the model card/license before use or redistribution; this
repository does not redistribute weights. Keep all downloads in the ignored
`models/` directory. Do not place private voice recordings in the checkout.

* **Zipformer FR:** keep the existing, working Pi-Con `stt_model_dir`. If the
  test copy needs its own copy, use Pi-Con's existing `pi-con --download-models`
  flow only after checking the settings path; this also downloads other
  configured assets, so prefer using the already-installed Zipformer directory.
* **Canary 180M INT8:** the [sherpa-onnx model instructions](https://k2-fsa.github.io/sherpa/onnx/nemo/canary.html)
  link to the [NVIDIA model card](https://huggingface.co/nvidia/canary-180m-flash),
  which states CC BY 4.0. Download and extract the INT8 archive into the
  expected directory:

  ```sh
  mkdir -p models/sherpa-onnx
  cd models/sherpa-onnx
  curl -fL -o canary.tar.bz2 \
    https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-canary-180m-flash-en-es-de-fr-int8.tar.bz2
  tar -xjf canary.tar.bz2
  mv sherpa-onnx-nemo-canary-180m-flash-en-es-de-fr-int8 canary-180m-flash-int8
  rm canary.tar.bz2
  cd ~/pi-con-stt-selector
  ```

  The extracted folder must include `encoder.int8.onnx`, `decoder.int8.onnx`,
  and `tokens.txt`. Check the publisher's current release page for a checksum
  and verify it before extraction when one is provided.
* **Orukeet INT8:** use the [Orukeet model card](https://huggingface.co/oruk/orukeet)
  and its linked [INT8 ONNX archive](https://huggingface.co/oruk/orukeet/resolve/55a984d46f68323301837194ce647c702f55facc/onnx/sherpa-onnx-orukeet-v0.1.0-int8.tar.bz2).
  The model card identifies the weights as CC BY-SA 4.0. The published archive
  SHA-256 is `f9191f30178cc9122ce2f023bf9fefafc822028307b0efa4caff645ba3fe8d0a`.
  Verify it before extracting:

  ```sh
  mkdir -p models/sherpa-onnx/orukeet-v0.1.0-int8
  curl -fL -o /tmp/orukeet-v0.1.0-int8.tar.bz2 \
    https://huggingface.co/oruk/orukeet/resolve/55a984d46f68323301837194ce647c702f55facc/onnx/sherpa-onnx-orukeet-v0.1.0-int8.tar.bz2
  echo 'f9191f30178cc9122ce2f023bf9fefafc822028307b0efa4caff645ba3fe8d0a  /tmp/orukeet-v0.1.0-int8.tar.bz2' | sha256sum -c -
  tar -xjf /tmp/orukeet-v0.1.0-int8.tar.bz2 -C models/sherpa-onnx/orukeet-v0.1.0-int8
  rm /tmp/orukeet-v0.1.0-int8.tar.bz2
  ```

  The final folder must contain `encoder.int8.onnx`, `decoder.int8.onnx`,
  `joiner.int8.onnx`, and `tokens.txt`.
* **Vosk Small French / English:** use the official
  [Vosk model list](https://alphacephei.com/vosk/models). It lists
  `vosk-model-small-fr-0.22` and `vosk-model-small-en-us-0.15` as Apache 2.0.
  Download those small models (not the multi-gigabyte server models), extract
  into `models/vosk/fr` and `models/vosk/en`, and retain the license files.
  The Pi-Con menu will enable these options only when the optional package and
  model directory are found.

Check that `models/` remains ignored before staging any changes:

```sh
git check-ignore -v models/sherpa-onnx/orukeet-v0.1.0-int8/tokens.txt
git status --short
```

## Start and test the four engines

The UI selector appears in Settings under **Local Speech Recognition**. It is
separate from the OpenAI audio transcription selector. Start the test copy in
the foreground so it can be stopped with Ctrl-C and so it cannot replace the
existing service:

```sh
cd ~/pi-con-stt-selector
. .venv/bin/activate
   python -m pi_con.main --config config/settings-test.json
```

If the package entry point is installed, `pi-con --config config/settings-test.json`
is equivalent. Open the test copy's local UI at the configured `ui_port`; use a
port not currently in use. Before starting, stop the existing Pi-Con service
manually if it owns the microphone or UI port (`sudo systemctl stop pi-con`).
This is temporary runtime control only; do not edit, disable, enable, or
reinstall the unit. Restart it after the test using the rollback steps.

For each engine, in order, record a short fresh sample or select a saved WAV in
the diagnostic panel, choose the engine, and use **Transcribe saved
recording**. Run tests in separate trials, returning to Zipformer between
other engines. Vosk appears as French and English choices but counts as one
engine. Keep the same WAV for direct comparisons and record model, language,
text, time, CPU, worker peak RSS, available RAM and swap from the diagnostic
result. Do not use private recordings unless the owner has agreed to the test;
diagnostic audio stays local and is not sent to OpenAI.

Monitor the Pi in a second SSH session during each model load and inference:

```sh
free -h
watch -n 1 'free -h; echo; ps -eo pid,comm,%cpu,%mem,rss --sort=-rss | head -12'
vmstat 1
```

Stop the trial immediately if Linux reports OOM activity, the UI or audio
becomes unresponsive, swap grows continuously, or memory pressure interferes
with the normal workload. Capture `journalctl -k --since -10min` and the
Pi-Con foreground log. The admission reserves in this branch are safety gates,
not measured memory requirements. Do not lower them, disable the OOM killer,
or add/change swap to force a model to load.

## Fallback and full rollback

* **A model fails to load or diagnose:** use the UI to choose **Zipformer FR
  (Original)**. The manager also queues Zipformer after worker failure when it
  is available. Confirm the UI reports Zipformer active before continuing.
  If the selector or process is unresponsive, press Ctrl-C in the foreground
  terminal. The current test process exits; it does not update systemd.
* **Return completely to the existing install:** press Ctrl-C, then restart
  the service only if it was stopped for this test:

  ```sh
  sudo systemctl start pi-con
  systemctl status pi-con --no-pager
  ```

  Confirm the original UI and audio work. The trial used a separate checkout,
  `.venv`, `.state`, and `models/`, so the original code and service unit were
  not replaced. Keep or remove `~/pi-con-stt-selector` manually after preserving
  any wanted diagnostics. Do not copy its config, state, or model preference
  into the original installation as part of rollback.

## Known limitations and validation status

* Before the Pi trial, validation on the ASUS ROG / Ubuntu 24.04 host used
  Python 3.12.3 x86_64 and passed 164 tests. During merge preparation on the
  same host, the full suite passed 165 tests. The host lacked the system
  `libportaudio2`, so its library and `libjack` dependency were extracted under
  `/tmp` for this test run; no system package or service was changed. This
  validates Ubuntu/x86_64 behavior only, not ARM64 or Python 3.13 on the Pi.
* Jpec's Pi 4 / 2 GB trial is reported, but the recovered repository has no
  results naming the engines exercised or recording memory and performance.
  Python package metadata and an ARM64 wheel do not establish compatibility,
  latency, or memory safety for engines not covered by that trial.
* Zipformer is the startup default. Canary and Orukeet buffer an utterance and
  decode after silence; microphone capture is paused during that inference, so
  speech during the pause can be missed. Vosk's ARM64 Python 3.13 runtime must
  be verified on the target.
* Memory admission thresholds are conservative software checks, not promises
  that the Pi has enough RAM. Chromium, Piper, wake-word detection and other
  services consume memory too.
* The prior handoff says two MCP tests failed, but the recovered Ubuntu tree
  does not identify their test names or failure output. The current complete
  `tests/test_mcp.py` run passed (10 tests); the earlier failures could not be
  matched to specific cases from the recovered evidence.
* Model downloads, benchmark archives, audio samples, `.env`, local settings,
  and weights must stay out of Git. The branch should contain only source,
  tests, dependency metadata, and this guide.
