from __future__ import annotations

import logging
import tarfile
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Streaming French zipformer (Common Voice) for sherpa-onnx. The archive is ~400 MB because it also holds
# unquantised models; we keep only the int8 files (~130 MB), which are fast enough on a Pi 4.
MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-fr-2023-04-14.tar.bz2"
MODEL_FILES = {
    "tokens.txt": "tokens.txt",
    "encoder": "encoder-epoch-29-avg-9-with-averaged-model.int8.onnx",
    "decoder": "decoder-epoch-29-avg-9-with-averaged-model.int8.onnx",
    "joiner": "joiner-epoch-29-avg-9-with-averaged-model.int8.onnx",
}

NO_SPEECH_TIMEOUT = 4.0  # seconds of silence at the start before giving up
END_SILENCE = 1.0  # seconds of silence after speech that ends the utterance


def ensure_stt_model(model_dir: str | Path) -> Path:
    """Download the French STT model into `model_dir` unless it is already there."""
    target = Path(model_dir)
    wanted = set(MODEL_FILES.values())
    if all((target / name).is_file() for name in wanted):
        return target
    logger.info("Downloading the French speech-to-text model (~400 MB, once) to %s", target)
    target.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(MODEL_URL, timeout=60) as response, tarfile.open(fileobj=response, mode="r|bz2") as archive:
        for member in archive:
            name = Path(member.name).name
            if member.isfile() and name in wanted:
                source = archive.extractfile(member)
                assert source is not None
                (target / name).write_bytes(source.read())
    missing = [name for name in wanted if not (target / name).is_file()]
    if missing:
        raise RuntimeError(f"STT model download incomplete, missing: {missing}")
    return target


class Utterance:
    """One spoken order being transcribed: feed 16 kHz int16 frames, read the growing text."""

    def __init__(self, recognizer: object) -> None:
        self._recognizer = recognizer
        self._stream = recognizer.create_stream()  # type: ignore[attr-defined]

    def accept(self, frame: np.ndarray) -> tuple[str, bool]:
        """Return (text so far, True once the speaker has stopped)."""
        recognizer, stream = self._recognizer, self._stream
        stream.accept_waveform(16_000, frame.astype(np.float32) / 32768)
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        text = " ".join(str(recognizer.get_result(stream)).lower().split())
        return text, bool(recognizer.is_endpoint(stream))


class CloudUtterance:
    """Same interface as `Utterance`: the local model still finds the end of speech and shows live words, but the final
    text comes from a cloud transcriber run on the recorded audio. If it fails or is too slow, the local text is kept."""

    def __init__(self, local: Utterance, transcribe: Callable[[np.ndarray], str], timeout: float = 6.0) -> None:
        self._local, self._transcribe, self._timeout = local, transcribe, timeout
        self._frames: list[np.ndarray] = []
        self._local_text = ""
        self._cloud_text = ""
        self._done = threading.Event()
        self._started: float | None = None

    def _run(self) -> None:
        try:
            self._cloud_text = self._transcribe(np.concatenate(self._frames))
        except Exception as error:  # the local text is the fallback
            logger.warning("Cloud transcription failed (%s); keeping the local text", error)
        finally:
            self._done.set()

    def accept(self, frame: np.ndarray) -> tuple[str, bool]:
        if self._started is None:
            self._local_text, finished = self._local.accept(frame)
            self._frames.append(frame)
            if finished:  # audio complete: send it, without blocking the microphone loop
                self._started = time.monotonic()
                threading.Thread(target=self._run, name="cloud-stt", daemon=True).start()
            return self._local_text, False
        if self._done.is_set() or time.monotonic() - self._started > self._timeout:
            return self._cloud_text or self._local_text, True
        return self._local_text, False


class Transcriber:
    def __init__(self, model_dir: str | Path, threads: int = 2, cloud: Callable[[], Callable[[np.ndarray], str] | None] | None = None) -> None:
        self._cloud = cloud  # returns a cloud transcribe function while AI mode wants one, else None
        import sherpa_onnx

        folder = ensure_stt_model(model_dir)
        logger.info("Loading the French speech-to-text model")
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(folder / MODEL_FILES["tokens.txt"]),
            encoder=str(folder / MODEL_FILES["encoder"]),
            decoder=str(folder / MODEL_FILES["decoder"]),
            joiner=str(folder / MODEL_FILES["joiner"]),
            num_threads=threads,
            sample_rate=16_000,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=NO_SPEECH_TIMEOUT,
            rule2_min_trailing_silence=END_SILENCE,
        )

    def start(self) -> Utterance | CloudUtterance:
        local = Utterance(self._recognizer)
        transcribe = self._cloud() if self._cloud else None
        return CloudUtterance(local, transcribe) if transcribe else local
