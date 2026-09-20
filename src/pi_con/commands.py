from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence

logger = logging.getLogger(__name__)


def run_command(command: Sequence[str]) -> None:
    """Launch the configured command without involving a shell."""
    logger.info("Running wake command: %s", " ".join(command))
    try:
        subprocess.Popen(list(command), start_new_session=True)
    except OSError:
        logger.exception("Could not start configured command")
