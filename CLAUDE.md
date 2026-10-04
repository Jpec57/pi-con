# Pi-Con

French-speaking voice home assistant for a Raspberry Pi (developed on macOS, deployed natively on the Pi 4/5 over SSH via `scripts/deploy.sh`, no Docker).

**Read `PROJECT.md` before working**: it holds the confirmed requirements, target architecture, decisions, roadmap and open questions. Keep it updated (status, decisions, log) when you change something meaningful. `README.md` has the how-to (run on Mac, deploy).

Conventions:
- Tests: `pytest` (venv in `.venv`, `pip install -e ".[dev]"`). Tests must not need a microphone or network.
- Mic tests run natively on the Mac.
- Deploy target is Pi 4 and Pi 5 (64-bit OS) only. Secrets go in `.env` (git-ignored); never commit them.
