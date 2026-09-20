# Pi-Con

French-speaking voice home assistant for a Raspberry Pi (developed on macOS, deployed as arm64 Docker images).

**Read `PROJECT.md` before working**: it holds the confirmed requirements, target architecture, decisions, roadmap and open questions. Keep it updated (status, decisions, log) when you change something meaningful. `README.md` has the how-to (run on Mac, deploy).

Conventions:
- Tests: `pytest` (venv in `.venv`, `pip install -e ".[dev]"`). Tests must not need a microphone or network.
- Mic tests run natively on the Mac; Docker Desktop cannot pass the Mac microphone.
- Deploy target is `linux/arm64` only. Secrets go in `.env` (git-ignored) locally and GitHub secrets in CI; never commit them.
