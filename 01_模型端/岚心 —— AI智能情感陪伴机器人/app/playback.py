from __future__ import annotations

import io
import logging
import tempfile
import threading
import time
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

_DEVICE_COUNT: int | None = None


def output_device_count() -> int:
    """Return the number of Windows wave-out devices, cached for the process."""
    global _DEVICE_COUNT
    if _DEVICE_COUNT is None:
        try:
            import ctypes

            _DEVICE_COUNT = int(ctypes.WinDLL("winmm").waveOutGetNumDevs())
        except (AttributeError, OSError):
            _DEVICE_COUNT = 0
    return _DEVICE_COUNT


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1, sample_width: int = 2) -> bytes:
    """Wrap raw little-endian PCM in a RIFF/WAVE container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


class SpeakerPlayer:
    """Play synthesized speech through the computer's default audio output.

    The robot board has no speaker yet, so the computer speaks for it. Raw PCM
    from Doubao TTS is easier to play than mp3: Windows can play a WAV file with
    no third-party decoder installed.
    """

    def __init__(self, directory: Path | None = None, keep_files: int = 8) -> None:
        self.directory = Path(directory or (Path(tempfile.gettempdir()) / "xiaolan_speaker"))
        self.keep_files = keep_files
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        try:
            import winsound  # noqa: F401
        except ImportError:
            return False
        return output_device_count() > 0

    def play_pcm(self, pcm: bytes, sample_rate: int) -> bool:
        if not pcm or sample_rate <= 0:
            return False
        path = self._write(pcm_to_wav(pcm, sample_rate))
        if path is None:
            return False
        try:
            import winsound

            winsound.PlaySound(
                str(path),
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        except (ImportError, RuntimeError) as exc:
            logger.warning("电脑扬声器播放失败：%s", exc)
            return False
        logger.info("电脑扬声器正在播放回复语音：%s", path.name)
        return True

    def stop(self) -> None:
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except (ImportError, RuntimeError):
            pass

    def _write(self, wav_bytes: bytes) -> Path | None:
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._prune()
                path = self.directory / f"reply-{int(time.time() * 1000)}-{threading.get_ident()}.wav"
                path.write_bytes(wav_bytes)
                return path
        except OSError as exc:
            logger.warning("无法写入电脑播放临时文件：%s", exc)
            return None

    def _prune(self) -> None:
        try:
            files = sorted(self.directory.glob("reply-*.wav"), key=lambda item: item.stat().st_mtime)
        except OSError:
            return
        for path in files[: max(0, len(files) - self.keep_files)]:
            try:
                path.unlink()
            except OSError:
                # The file may still be playing; it is cleaned up on a later round.
                pass
