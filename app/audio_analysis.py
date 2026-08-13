"""
Task 3 — audio feature extraction.

Browsers record audio as webm/ogg (Opus codec) via MediaRecorder, not WAV.
soundfile/librosa can't read those directly, so every upload is first
transcoded to a temp WAV with ffmpeg (via pydub, which just shells out to
ffmpeg). That's the one non-obvious "figure it out" piece of this task —
worth calling out in the stuck log.
"""

import subprocess
import numpy as np
import soundfile as sf
from pathlib import Path
from pydub import AudioSegment
from pydub.utils import mediainfo


def _to_wav(src_path: Path, wav_path: Path):
    """Transcode any input audio (webm/ogg/mp3/m4a/wav/...) to 16-bit PCM WAV."""
    audio = AudioSegment.from_file(src_path)
    audio.export(wav_path, format="wav")
    return audio


def _estimate_bitrate_kbps(src_path: Path, duration_sec: float) -> float:
    """Prefer ffprobe's reported bitrate (accurate for compressed formats
    like webm/opus). Fall back to file_size*8 / duration if ffprobe can't
    tell us (e.g. some raw WAV files don't carry a bitrate tag)."""
    try:
        info = mediainfo(str(src_path))
        br = info.get("bit_rate")
        if br:
            return round(int(br) / 1000, 1)
    except Exception:
        pass
    if duration_sec > 0:
        size_bits = src_path.stat().st_size * 8
        return round((size_bits / duration_sec) / 1000, 1)
    return 0.0


def _loudness_dbfs(samples: np.ndarray) -> float:
    """RMS loudness in dBFS (dB relative to full scale). Silence -> -inf,
    clamped to a floor so it's still a sane number to store/display."""
    if samples.size == 0:
        return -120.0
    rms = np.sqrt(np.mean(samples.astype(np.float64) ** 2))
    if rms <= 0:
        return -120.0
    dbfs = 20 * np.log10(rms)
    return round(max(dbfs, -120.0), 1)


def _quality_estimate(loudness_dbfs: float, duration_sec: float, sample_rate: int) -> str:
    """Rough, explainable heuristic (this is the assignment's optional
    bonus, not a real audio-quality model):
      - too quiet (< -40 dBFs)  -> likely too far from mic / low gain
      - clipping  (> -1 dBFS)   -> probably clipped/distorted
      - too short (< 0.5s)      -> probably an accidental/empty recording
      - low sample rate (< 16kHz) -> poor capture quality
      - otherwise -> good
    """
    if duration_sec < 0.5:
        return "poor (too short — likely an empty/accidental recording)"
    if loudness_dbfs > -1.0:
        return "poor (likely clipping/distortion — recorded too loud)"
    if loudness_dbfs < -40.0:
        return "poor (very quiet — mic too far or gain too low)"
    if sample_rate < 16000:
        return "fair (low sample rate for speech)"
    return "good"


def analyze_audio(input_path: str) -> dict:
    """Main entry point. input_path: path to the raw uploaded file (any
    browser-recorded or user-uploaded audio format)."""
    src = Path(input_path)
    wav_path = src.with_suffix(".analysis.wav")
    try:
        _to_wav(src, wav_path)
        data, sample_rate = sf.read(str(wav_path), always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)  # downmix to mono for loudness calc
        duration_sec = len(data) / sample_rate if sample_rate else 0.0
        bitrate_kbps = _estimate_bitrate_kbps(src, duration_sec)
        loudness = _loudness_dbfs(data)
        quality = _quality_estimate(loudness, duration_sec, sample_rate)
        return {
            "duration_sec": round(duration_sec, 2),
            "sample_rate_hz": int(sample_rate),
            "bitrate_kbps": bitrate_kbps,
            "loudness_dbfs": loudness,
            "quality_estimate": quality,
        }
    finally:
        if wav_path.exists():
            wav_path.unlink()
