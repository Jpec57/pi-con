# Start Pi-Con

Say the wake word ("Hey Jarvis"), then give an order in French. The UI is at `http://<host>:8080`.

## On the Mac

First time only:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pi-con --download-models      # wake word, French speech model, French voice
```

Then, every time:

```sh
source .venv/bin/activate
pi-con                        # open http://localhost:8080
```

- Allow microphone access for your terminal when macOS asks.
- Wrong mic: `pi-con --list-devices`, then set `"audio_device"` in `config/settings.json`.
- No mic: `./scripts/make-sample.sh` then `pi-con --wav samples/hey_jarvis.wav`.
- Download fails with `CERTIFICATE_VERIFY_FAILED`: run `export SSL_CERT_FILE=$(python -c "import certifi;print(certifi.where())")` first.
- Spoken replies: press **Voix** in the UI. Test the voice with `pi-con --say "Bonjour"`.

## On the Raspberry Pi (4 or 5, 64-bit OS)

You deploy from the Mac. The Pi must have SSH enabled and your key authorised (`ssh-copy-id pi@raspberrypi.local`), and a USB mic plugged in.

First time only:

```sh
cp .env.example .env          # set PI_HOST (and PI_USER, PI_SSH_KEY_PATH if needed)
```

Then deploy (and re-run after every change):

```sh
./scripts/deploy.sh           # copies the code, installs it, (re)starts the pi-con service
```

The service starts at every boot. Open `http://<pi-ip>:8080`.

- Logs: `ssh pi@<host> journalctl -fu pi-con`
- Restart: `ssh pi@<host> sudo systemctl restart pi-con`
- Wrong mic or speaker: `ssh pi@<host> /home/pi/pi-con/.venv/bin/pi-con --list-devices`, then set `"audio_device"` / `"tts"."output_device"` in `config/settings.json` and redeploy.

More detail: `README.md`. Project status and decisions: `PROJECT.md`.
