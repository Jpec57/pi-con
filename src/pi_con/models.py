from __future__ import annotations

import logging
from importlib.util import find_spec
from pathlib import Path

import openwakeword
from openwakeword.model import Model
from openwakeword.utils import download_models

logger = logging.getLogger(__name__)


def inference_framework() -> str:
    """tflite-runtime is installed on Linux (Pi); macOS/Windows fall back to ONNX."""
    return "tflite" if find_spec("tflite_runtime") else "onnx"


def ensure_models(wakeword: str) -> None:
    """Fetch the wake word, feature and VAD models unless already on disk.

    The openwakeword wheel ships no model files, so they must be downloaded once.
    A wakeword that is a path to an existing file is a custom model and needs nothing.
    """
    if Path(wakeword).is_file():
        return
    logger.info("Ensuring openwakeword models are present for '%s'", wakeword)
    download_models([wakeword])


def load_model(wakeword: str) -> Model:
    """Load only the configured model; `Model()` would load all six pretrained ones."""
    ensure_models(wakeword)
    framework = inference_framework()
    logger.info("Loading wake word model '%s' (%s)", wakeword, framework)
    return Model(wakeword_models=[wakeword], inference_framework=framework)


def available_wakewords() -> list[str]:
    return sorted(openwakeword.MODELS)
