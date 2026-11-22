"""Single-model local STT workers and safe, between-utterance switching.

The cloud transcription path remains in :mod:`pi_con.stt`; this module only
owns the on-device recognizer. Optional engines are never imported in the
application process. Optional engines are never downloaded; the existing
Zipformer startup download remains for backwards compatibility.
"""
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)
RATE = 16_000
FRAME = 1_280
DEFAULT_LOAD_TIMEOUT = 120.0
DEFAULT_RPC_TIMEOUT = 30.0


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    languages: tuple[str, ...]
    family: str
    model_dir: Path
    minimum_available_mib: int


def available_memory_bytes() -> int | None:
    """Read MemAvailable on Linux; return None where no safe estimate exists."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _requirements(spec: ModelSpec) -> tuple[list[Path], str | None]:
    if spec.family in ("zipformer", "orukeet"):
        names = ["tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx"]
    elif spec.family == "canary":
        names = ["tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx"]
    elif spec.family == "vosk":
        names = []
    else:
        return [], "unknown engine family"
    if spec.family == "zipformer":
        from .stt import MODEL_FILES
        names = list(MODEL_FILES.values())
    missing = [spec.model_dir / name for name in names if not (spec.model_dir / name).is_file()]
    if spec.family == "vosk":
        try:
            if not spec.model_dir.is_dir() or not any(spec.model_dir.iterdir()):
                missing = [spec.model_dir]
        except OSError:
            missing = [spec.model_dir]
    if missing:
        return missing, None
    module = "vosk" if spec.family == "vosk" else "sherpa_onnx"
    try:
        import importlib.util
        if importlib.util.find_spec(module) is None:
            return [], f"Python runtime {module} is not installed"
    except (ImportError, ValueError, OSError) as error:
        return [], f"Python runtime {module} is not available: {type(error).__name__}: {error}"
    return [], None


def _memory_reason(spec: ModelSpec, memory_provider: Callable[[], int | None]) -> str | None:
    available = memory_provider()
    if available is None:
        if spec.id != "zipformer-fr":
            return "available memory cannot be measured safely on this platform"
        return None
    required = spec.minimum_available_mib * 1024 * 1024
    if available < required:
        return f"requires a safety reserve of {spec.minimum_available_mib} MiB available; only {available // (1024 * 1024)} MiB available"
    return None


def default_catalog(zipformer_dir: str | Path, root: str | Path = "models") -> dict[str, ModelSpec]:
    root = Path(root)
    return {
        "zipformer-fr": ModelSpec("zipformer-fr", "Zipformer FR (Original)", ("fr",), "zipformer", Path(zipformer_dir), 256),
        "canary-fr": ModelSpec("canary-fr", "Canary 180M INT8", ("fr",), "canary", root / "sherpa-onnx" / "canary-180m-flash-int8", 900),
        "orukeet-fr": ModelSpec("orukeet-fr", "Orukeet INT8", ("fr",), "orukeet", root / "sherpa-onnx" / "orukeet-v0.1.0-int8", 1_300),
        "vosk-fr": ModelSpec("vosk-fr", "Vosk Small FR", ("fr",), "vosk", root / "vosk" / "fr", 500),
        "vosk-en": ModelSpec("vosk-en", "Vosk Small EN", ("en",), "vosk", root / "vosk" / "en", 500),
    }


def _resource_snapshot() -> dict[str, int | None]:
    result: dict[str, int | None] = {"cpu_seconds": None, "rss_bytes": None, "mem_available_bytes": available_memory_bytes(), "swap_used_bytes": None}
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        result["cpu_seconds"] = int(usage.ru_utime * 1_000_000 + usage.ru_stime * 1_000_000)
        # Linux reports KiB; macOS reports bytes. This application targets Linux.
        result["rss_bytes"] = int(usage.ru_maxrss * 1024)
    except (ImportError, OSError, AttributeError):
        pass
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("SwapTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("SwapFree:"):
                free = int(line.split()[1]) * 1024
        result["swap_used_bytes"] = max(0, total - free)
    except (OSError, ValueError, UnboundLocalError, IndexError):
        pass
    return result


class _OfflineUtterance:
    def __init__(self, engine: "_Engine") -> None:
        self.engine = engine
        self.samples: list[Any] = []
        self.started = time.monotonic()
        self.last_voice = self.started
        self.speech_seen = False
        self.local_text = ""

    def accept(self, frame):
        import numpy as np
        audio = np.asarray(frame, dtype=np.int16)
        self.samples.append(audio.copy())
        rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float32))))) if len(audio) else 0.0
        now = time.monotonic()
        if rms >= 250:
            self.speech_seen = True
            self.last_voice = now
        finished = (self.speech_seen and now - self.last_voice >= 1.0) or (not self.speech_seen and now - self.started >= 4.0)
        if not finished:
            return self.local_text, False
        if not self.speech_seen:
            return "", True
        # Signal the parent to pause mic capture before the expensive offline
        # decode, then perform that decode on an explicit finish command.
        self.needs_finalization = True
        return self.local_text, True

    def finish(self):
        if self.speech_seen and self.samples:
            import numpy as np
            self.local_text = self.engine.transcribe(np.concatenate(self.samples))
        self.needs_finalization = False
        return self.local_text, True


class _Engine:
    def __init__(self, model_id: str, language: str, model_dir: str, threads: int):
        self.model_id, self.language, self.model_dir = model_id, language, Path(model_dir)
        self.threads = threads
        self.recognizer = None
        self.vosk_model = None
        self.kind = "offline"

    def load(self) -> None:
        if self.model_id.startswith("vosk-"):
            import vosk
            self.vosk_model = vosk.Model(str(self.model_dir))
            self.kind = "vosk"
        else:
            import sherpa_onnx
            if self.model_id == "zipformer-fr":
                from .stt import MODEL_FILES
                f = MODEL_FILES
                self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                    tokens=str(self.model_dir / f["tokens.txt"]), encoder=str(self.model_dir / f["encoder"]),
                    decoder=str(self.model_dir / f["decoder"]), joiner=str(self.model_dir / f["joiner"]),
                    num_threads=self.threads, sample_rate=RATE, enable_endpoint_detection=True,
                    rule1_min_trailing_silence=4.0, rule2_min_trailing_silence=1.0,
                )
                self.kind = "sherpa-online"
            elif self.model_id == "canary-fr":
                self.recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_canary(
                    encoder=str(self.model_dir / "encoder.int8.onnx"), decoder=str(self.model_dir / "decoder.int8.onnx"),
                    tokens=str(self.model_dir / "tokens.txt"), src_lang="fr", tgt_lang="fr", num_threads=self.threads,
                )
            elif self.model_id == "orukeet-fr":
                self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=str(self.model_dir / "encoder.int8.onnx"), decoder=str(self.model_dir / "decoder.int8.onnx"),
                    joiner=str(self.model_dir / "joiner.int8.onnx"), tokens=str(self.model_dir / "tokens.txt"),
                    model_type="nemo_transducer", num_threads=self.threads,
                )
            else:
                raise RuntimeError(f"unsupported model: {self.model_id}")

    def new_utterance(self):
        if self.kind == "sherpa-online":
            return _OnlineUtterance(self)
        if self.kind == "vosk":
            import vosk
            return _VoskUtterance(self, vosk.KaldiRecognizer(self.vosk_model, RATE))
        return _OfflineUtterance(self)

    def transcribe(self, samples) -> str:
        import numpy as np
        if self.kind == "sherpa-online":
            utterance = _OnlineUtterance(self)
            text = ""
            padded = np.concatenate([np.asarray(samples, dtype=np.int16), np.zeros(int(RATE * 1.5), dtype=np.int16)])
            for start in range(0, len(padded), FRAME):
                text, _ = utterance.accept(padded[start:start + FRAME])
            return text
        if self.kind == "vosk":
            import vosk
            recognizer = vosk.KaldiRecognizer(self.vosk_model, RATE)
            recognizer.AcceptWaveform(np.asarray(samples, dtype=np.int16).tobytes())
            return str(json.loads(recognizer.FinalResult()).get("text", ""))
        stream = self.recognizer.create_stream()
        stream.accept_waveform(RATE, np.asarray(samples, dtype=np.float32) / 32768.0)
        self.recognizer.decode_stream(stream)
        return " ".join(str(getattr(stream.result, "text", stream.result)).split())


class _OnlineUtterance:
    def __init__(self, engine: _Engine):
        self.engine = engine
        self.stream = engine.recognizer.create_stream()
        self.local_text = ""

    def accept(self, frame):
        import numpy as np
        self.stream.accept_waveform(RATE, np.asarray(frame, dtype=np.float32) / 32768.0)
        while self.engine.recognizer.is_ready(self.stream):
            self.engine.recognizer.decode_stream(self.stream)
        self.local_text = " ".join(str(self.engine.recognizer.get_result(self.stream)).lower().split())
        return self.local_text, bool(self.engine.recognizer.is_endpoint(self.stream))

    def finish(self):
        return self.local_text, True


class _VoskUtterance:
    def __init__(self, engine: _Engine, recognizer):
        self.engine, self.recognizer = engine, recognizer
        self.local_text = ""

    def accept(self, frame):
        import numpy as np
        final = self.recognizer.AcceptWaveform(np.asarray(frame, dtype=np.int16).tobytes())
        if final:
            self.local_text = str(json.loads(self.recognizer.Result()).get("text", ""))
            return self.local_text, True
        self.local_text = str(json.loads(self.recognizer.PartialResult()).get("partial", ""))
        return self.local_text, False

    def finish(self):
        self.local_text = str(json.loads(self.recognizer.FinalResult()).get("text", ""))
        return self.local_text, True


def _worker_main(connection, model_id: str, language: str, model_dir: str, threads: int) -> None:
    try:
        engine = _Engine(model_id, language, model_dir, threads)
        engine.load()
        # Exercise the recognizer before the parent can publish this engine as active.
        import numpy as np
        engine.transcribe(np.zeros(RATE // 4, dtype=np.int16))
        connection.send({"ok": True, "kind": engine.kind})
        utterance = None
        while True:
            request = connection.recv()
            command = request.get("command")
            if command == "close":
                connection.send({"ok": True})
                break
            if command == "new":
                utterance = engine.new_utterance()
                connection.send({"ok": True})
            elif command == "frame" and utterance is not None:
                import numpy as np
                text, finished = utterance.accept(np.frombuffer(request["pcm"], dtype=np.int16))
                connection.send({"ok": True, "text": text, "finished": finished,
                                 "finalization_required": bool(getattr(utterance, "needs_finalization", False))})
                if finished:
                    utterance = None
            elif command == "finish" and utterance is not None:
                text, finished = utterance.finish() if hasattr(utterance, "finish") else (utterance.local_text, True)
                connection.send({"ok": True, "text": text, "finished": finished})
                utterance = None
            elif command == "transcribe":
                started = time.perf_counter()
                before = _resource_snapshot()
                text = engine.transcribe(np.frombuffer(request["pcm"], dtype=np.int16))
                after = _resource_snapshot()
                cpu_delta = None if before["cpu_seconds"] is None or after["cpu_seconds"] is None else (after["cpu_seconds"] - before["cpu_seconds"]) / 1_000_000
                connection.send({"ok": True, "text": text, "elapsed_s": time.perf_counter() - started,
                                 "cpu_seconds": cpu_delta, "rss_peak_bytes": after["rss_bytes"],
                                 "mem_available_before_bytes": before["mem_available_bytes"],
                                 "mem_available_bytes": after["mem_available_bytes"],
                                 "swap_used_before_bytes": before["swap_used_bytes"],
                                 "swap_used_bytes": after["swap_used_bytes"]})
    except BaseException as error:
        try:
            connection.send({"ok": False, "error": f"{type(error).__name__}: {error}"})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class ProcessWorker:
    """One model in one child process; terminating this worker releases native runtime memory."""
    def __init__(self, spec: ModelSpec, language: str, threads: int, load_timeout: float, rpc_timeout: float):
        self.spec, self.language, self.threads = spec, language, threads
        self.load_timeout, self.rpc_timeout = load_timeout, rpc_timeout
        self.process = None
        self.connection = None
        self.kind = spec.family
        self._rpc_lock = threading.Lock()

    def start(self) -> None:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_worker_main, args=(child, self.spec.id, self.language, str(self.spec.model_dir), self.threads), daemon=True)
        process.start()
        child.close()
        self.process, self.connection = process, parent
        if not parent.poll(self.load_timeout):
            self.stop()
            raise TimeoutError(f"model load exceeded {self.load_timeout:g}s")
        response = parent.recv()
        if not response.get("ok"):
            self.stop()
            raise RuntimeError(response.get("error", "model worker failed to load"))
        self.kind = response.get("kind", self.kind)

    def request(self, payload: dict, timeout: float | None = None) -> dict:
        with self._rpc_lock:
            if not self.process or not self.process.is_alive() or not self.connection:
                raise RuntimeError("STT worker is not running")
            self.connection.send(payload)
            if not self.connection.poll(self.rpc_timeout if timeout is None else timeout):
                self.stop()
                raise TimeoutError("STT worker response timed out")
            response = self.connection.recv()
            if not response.get("ok"):
                raise RuntimeError(response.get("error", "STT worker error"))
            return response

    def stop(self) -> None:
        process, connection = self.process, self.connection
        self.process, self.connection = None, None
        if connection:
            try:
                if process and process.is_alive():
                    connection.send({"command": "close"})
                    if connection.poll(1.0):
                        connection.recv()
            except (BrokenPipeError, EOFError, OSError):
                pass
            connection.close()
        if process:
            process.join(timeout=2.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)
            if process.is_alive() and hasattr(process, "kill"):
                process.kill()
                process.join(timeout=1.0)


class WorkerUtterance:
    def __init__(self, manager: "ManagedTranscriber", worker: ProcessWorker, source: str):
        self.manager, self.worker, self.source, self.local_text = manager, worker, source, ""
        self.started = time.monotonic()
        self.needs_finalization = False

    def accept(self, frame):
        try:
            response = self.worker.request({"command": "frame", "pcm": frame.astype("int16", copy=False).tobytes()})
        except Exception as error:
            with self.manager._lock:
                self.manager._utterance_active = False
            self.manager._queue_safe_fallback(error)
            return self.local_text, True
        self.local_text = response["text"]
        self.needs_finalization = bool(response.get("finalization_required"))
        if response["finished"] and not self.needs_finalization:
            with self.manager._lock:
                self.manager._utterance_active = False
            self.manager.note_final(self.local_text, self.source, time.monotonic() - self.started)
        return self.local_text, response["finished"]

    def finish(self):
        try:
            response = self.worker.request({"command": "finish"})
        except Exception as error:
            with self.manager._lock:
                self.manager._utterance_active = False
            self.manager._queue_safe_fallback(error)
            return self.local_text, True
        self.local_text = response["text"]
        self.needs_finalization = False
        with self.manager._lock:
            self.manager._utterance_active = False
        self.manager.note_final(self.local_text, self.source, time.monotonic() - self.started)
        return self.local_text, response["finished"]


class ManagedTranscriber:
    """Transcriber-compatible facade; changes are requested by UI and applied by the listener between turns."""
    def __init__(self, model_dir: str | Path, threads: int = 2, cloud=None, state_path: str | Path = ".state/local-stt.json",
                 model_root: str | Path = "models", bus=None, memory_provider=available_memory_bytes,
                 worker_factory=ProcessWorker, load_timeout: float = DEFAULT_LOAD_TIMEOUT,
                 rpc_timeout: float = DEFAULT_RPC_TIMEOUT):
        self.catalog = default_catalog(model_dir, model_root)
        self.threads, self.cloud, self.state_path, self.bus = threads, cloud, Path(state_path), bus
        self.memory_provider, self.worker_factory = memory_provider, worker_factory
        self.load_timeout, self.rpc_timeout = load_timeout, rpc_timeout
        self._lock = threading.RLock()
        self._utterance_active = False
        self._diagnostic_active = False
        self._pending: tuple[str, str] | None = None
        self._loading_model: str | None = None
        self.worker: ProcessWorker | Any | None = None
        self.active_id: str | None = None
        self.language = "fr"
        self.loading = False
        self.last_error: str | None = None
        self.load_seconds: float | None = None
        self.last_transcription: dict[str, Any] | None = None
        self._saved_id = "zipformer-fr"
        self._saved_language = "fr"
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            model, language = value.get("model"), value.get("language", "fr")
            if model in self.catalog and language in self.catalog[model].languages:
                self._saved_id, self._saved_language = model, language
        except (OSError, ValueError, AttributeError):
            pass

    def _emit(self) -> None:
        if self.bus:
            self.bus.publish("local_stt", persist=False, **self.snapshot())

    def request_switch(self, model_id: str, language: str | None = None) -> tuple[bool, str]:
        with self._lock:
            spec = self.catalog.get(model_id)
            if spec is None:
                return False, "unknown local STT model"
            chosen_language = language or spec.languages[0]
            if chosen_language not in spec.languages:
                return False, "language is not supported by this model"
            missing, incompatible = _requirements(spec)
            if missing:
                return False, f"model files are missing: {', '.join(p.name for p in missing)}"
            if incompatible:
                return False, incompatible
            memory_reason = _memory_reason(spec, self.memory_provider)
            if memory_reason:
                return False, memory_reason
            if self.loading:
                return False, "another model change is in progress"
            if self._utterance_active or self._diagnostic_active:
                return False, "wait until the current local transcription is finished"
            self._pending = (model_id, chosen_language)
            self._loading_model = model_id
            self.loading = True
            self.last_error = None
        self._emit()
        return True, "queued; will switch after the current phrase"

    def has_pending_switch(self) -> bool:
        with self._lock:
            return self._pending is not None

    @property
    def diagnostic_busy(self) -> bool:
        with self._lock:
            return self._diagnostic_active

    def start_initial(self) -> None:
        with self._lock:
            selected, language = self._saved_id, self._saved_language
        try:
            self._activate(selected, language, initial=True)
        except Exception as error:
            logger.error("Local STT %s could not start: %s; falling back to Zipformer", selected, error)
            self.last_error = str(error)
            if selected != "zipformer-fr":
                try:
                    self._activate("zipformer-fr", "fr", initial=True)
                except Exception as fallback_error:
                    self.last_error = f"selected model failed ({error}); Zipformer fallback failed ({fallback_error})"
                    logger.exception("Zipformer fallback could not start")
                else:
                    try:
                        self._save_preference("zipformer-fr", "fr")
                    except OSError as preference_error:
                        self.last_error = f"Zipformer is active, but its preference could not be saved: {preference_error}"
            self._emit()

    def apply_pending(self) -> bool:
        with self._lock:
            pending, self._pending = self._pending, None
        if not pending:
            return False
        target, language = pending
        self._stop_worker()
        try:
            self._activate(target, language)
            self._save_preference(target, language)
            self.loading = False
            self._loading_model = None
            self.last_error = None
            self._emit()
            return True
        except Exception as error:
            logger.exception("Could not activate local STT %s", target)
            self.last_error = str(error)
            try:
                self._stop_worker()
                fallback = "zipformer-fr"
                self._activate(fallback, "fr")
                self.active_id, self.language = fallback, "fr"
            except Exception as fallback_error:
                self._stop_worker()
                self.active_id = None
                self.last_error = f"{error}; Zipformer fallback failed: {fallback_error}"
            else:
                try:
                    self._save_preference(fallback, "fr")
                except OSError as preference_error:
                    self.last_error = f"Zipformer is active, but its preference could not be saved: {preference_error}"
            self.loading = False
            self._loading_model = None
            self._emit()
            return False

    def _activate(self, model_id: str, language: str, initial: bool = False) -> None:
        spec = self.catalog[model_id]
        missing, incompatible = _requirements(spec)
        if missing:
            # Preserve the existing Zipformer startup behavior: its model may be fetched if absent.
            if model_id == "zipformer-fr" and initial:
                from .stt import ensure_stt_model
                ensure_stt_model(spec.model_dir)
            else:
                raise FileNotFoundError("missing model files: " + ", ".join(str(p) for p in missing))
        if incompatible:
            raise RuntimeError(incompatible)
        reason = _memory_reason(spec, self.memory_provider)
        if reason:
            raise MemoryError(reason)
        worker = self.worker_factory(spec, language, self.threads, self.load_timeout, self.rpc_timeout)
        started = time.monotonic()
        worker.start()
        self.worker = worker
        self.active_id, self.language = model_id, language
        self.load_seconds = time.monotonic() - started

    def _stop_worker(self) -> None:
        worker, self.worker = self.worker, None
        self.active_id = None
        if worker is not None:
            worker.stop()

    def _save_preference(self, model_id: str, language: str) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps({"model": model_id, "language": language}), encoding="utf-8")
        os.replace(temporary, self.state_path)
        self._saved_id, self._saved_language = model_id, language

    def start_utterance(self):
        with self._lock:
            worker, source = self.worker, self.active_id
            if worker is None or source is None:
                self._queue_safe_fallback(RuntimeError(self.last_error or "local STT is unavailable"))
                return _FailedUtterance()
            self._utterance_active = True
        try:
            worker.request({"command": "new"})
        except Exception as error:
            with self._lock:
                self._utterance_active = False
            self._queue_safe_fallback(error)
            return _FailedUtterance()
        local = WorkerUtterance(self, worker, source)
        transcribe = self.cloud() if self.cloud else None
        if transcribe:
            from .stt import CloudUtterance
            return CloudUtterance(local, transcribe)
        return local

    # Keep the Transcriber interface used by the microphone-test recording path.
    def start(self):
        return self.start_utterance()

    def transcribe_diagnostic(self, samples, language: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self.worker is None or self.active_id is None:
                raise RuntimeError(self.last_error or "local STT is unavailable")
            if self._utterance_active or self._diagnostic_active or self.loading:
                raise RuntimeError("local STT is busy; retry after the current phrase")
            self._diagnostic_active = True
            worker, model_id, language_used = self.worker, self.active_id, language or self.language
        try:
            started = time.monotonic()
            response = worker.request({"command": "transcribe", "pcm": samples.astype("int16", copy=False).tobytes()}, timeout=self.rpc_timeout)
            result = {"model": model_id, "language": language_used, "text": response["text"],
                      "elapsed_s": response.get("elapsed_s", time.monotonic() - started), "cpu_seconds": response.get("cpu_seconds"),
                  "rss_peak_bytes": response.get("rss_peak_bytes"),
                  "mem_available_before_bytes": response.get("mem_available_before_bytes"),
                  "mem_available_bytes": response.get("mem_available_bytes"),
                  "swap_used_before_bytes": response.get("swap_used_before_bytes"),
                  "swap_used_bytes": response.get("swap_used_bytes"), "source": "local"}
            self.last_transcription = result
            self._emit()
            return result
        except Exception as error:
            self._queue_safe_fallback(error)
            raise
        finally:
            with self._lock:
                self._diagnostic_active = False

    def transcribe(self, samples) -> str:
        return str(self.transcribe_diagnostic(samples)["text"])

    def note_final(self, text: str, source: str, elapsed_s: float) -> None:
        self.last_transcription = {"model": self.active_id, "language": self.language, "text": text,
                                   "elapsed_s": elapsed_s, "source": source}
        self._emit()

    def snapshot(self) -> dict[str, Any]:
        entries = []
        for model_id, spec in self.catalog.items():
            missing, incompatible = _requirements(spec)
            installed = not missing and incompatible is None
            reason = incompatible or ("missing model files" if missing else None)
            if installed:
                reason = _memory_reason(spec, self.memory_provider)
            entries.append({"id": model_id, "label": spec.label, "languages": list(spec.languages),
                            "status": "incompatible" if incompatible else ("installed" if not missing else "not_installed"),
                            "active": model_id == self.active_id, "loading": bool(self.loading and self._loading_model == model_id),
                            "reason": reason})
        last = self.last_transcription
        last_public = ({key: last[key] for key in ("model", "language", "elapsed_s", "source") if key in last}
                       if last else None)
        return {"models": entries, "active_model": self.active_id, "active_language": self.language,
                "loading": self.loading, "error": self.last_error, "load_seconds": self.load_seconds,
                "last_transcription": last_public, "pending": self._pending is not None,
                "diagnostic_busy": self._diagnostic_active,
                "source": "local"}

    def close(self) -> None:
        self._stop_worker()

    def _queue_safe_fallback(self, error: Exception) -> None:
        logger.error("Local STT worker failed: %s; scheduling Zipformer fallback", error)
        with self._lock:
            self.last_error = str(error)
            self._pending = ("zipformer-fr", "fr")
            self._loading_model = "zipformer-fr"
            self.loading = True
        self._emit()


class _FailedUtterance:
    source = "unavailable"
    local_text = ""

    @staticmethod
    def accept(_frame):
        return "", True


def diagnostic_from_recording(manager: ManagedTranscriber, recordings, rec_id: str, reference: str | None = None) -> dict[str, Any]:
    """Transcribe one saved WAV for diagnostics only; this never publishes a command transcript."""
    path = recordings.wav(rec_id) if recordings else None
    if path is None:
        raise FileNotFoundError("recording not found")
    import numpy as np
    with wave.open(str(path), "rb") as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getcomptype()) != (RATE, 1, 2, "NONE"):
            raise ValueError("recording must be PCM 16 kHz mono")
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16).copy()
    result = manager.transcribe_diagnostic(samples)
    result["recording"] = rec_id
    result["reference"] = reference
    if reference is not None:
        result["scores"] = _score_text(reference, result["text"])
    # Results contain no audio and remain local. Keep an append-only history for comparison.
    folder = recordings.folder.parent / "stt-diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "results.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def _score_text(reference: str, hypothesis: str) -> dict[str, float | int]:
    """Small offline WER/CER scorer for an optional user-verified reference."""
    import re
    import unicodedata

    def normalize(text: str) -> str:
        text = unicodedata.normalize("NFKC", text).casefold()
        return " ".join(re.findall(r"[\w]+", text, flags=re.UNICODE))

    def distance(left: list[str], right: list[str]) -> int:
        previous = list(range(len(right) + 1))
        for i, a in enumerate(left, 1):
            current = [i]
            for j, b in enumerate(right, 1):
                current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
            previous = current
        return previous[-1]

    ref, hyp = normalize(reference), normalize(hypothesis)
    word_edits = distance(ref.split(), hyp.split())
    char_edits = distance(list(ref.replace(" ", "")), list(hyp.replace(" ", "")))
    return {"wer": word_edits / max(1, len(ref.split())), "cer": char_edits / max(1, len(ref.replace(" ", "")))}
