"""合成音频生成器（标准库生成 PCM WAV 块与故障块）。"""

from __future__ import annotations

import hashlib
import io
import math
import struct
import wave


def _pack_wav_bytes(
    pcm_bytes: bytes,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    bits_per_sample: int = 16,
    declared_data_bytes: int | None = None,
) -> bytes:
    """用 struct 手工封装 RIFF/WAVE 头，支持构造合法或被篡改头部的 WAV。"""
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    data_size = declared_data_bytes if declared_data_bytes is not None else len(pcm_bytes)
    riff_size = 36 + data_size
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_size,
        b"WAVE",
        b"fmt ",
        16,  # PCM fmt chunk size
        1,  # AudioFormat = 1 (PCM)
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + pcm_bytes


def make_sine_pcm_s16le(sample_count: int, *, freq: float = 440.0, sample_rate: int = 16000) -> bytes:
    """生成 16-bit 小端单声道正弦波 PCM 字节。"""
    buf = bytearray(sample_count * 2)
    # 使用周期缓存在大样本数（如 480000）时加速生成
    period = max(1, int(sample_rate / freq))
    one_cycle = [
        int(12000 * math.sin(2.0 * math.pi * i / period))
        for i in range(period)
    ]
    cycle_bytes = struct.pack(f"<{period}h", *one_cycle)
    full_cycles, rem = divmod(sample_count, period)
    pos = 0
    for _ in range(full_cycles):
        buf[pos : pos + len(cycle_bytes)] = cycle_bytes
        pos += len(cycle_bytes)
    if rem:
        rem_bytes = struct.pack(f"<{rem}h", *one_cycle[:rem])
        buf[pos : pos + len(rem_bytes)] = rem_bytes
    return bytes(buf)


def make_standard_30s_wav() -> tuple[bytes, int, str]:
    """生成标准 30 秒音频块（16000 Hz, 单声道, pcm_s16le, 480000 样本）。"""
    sample_count = 480000
    pcm = make_sine_pcm_s16le(sample_count)
    wav_bytes = _pack_wav_bytes(pcm, sample_rate=16000, channels=1, bits_per_sample=16)
    return wav_bytes, sample_count, hashlib.sha256(wav_bytes).hexdigest()


def make_short_wav(sample_count: int = 16000) -> tuple[bytes, int, str]:
    """生成短块（默认 1 秒 = 16000 样本）。"""
    pcm = make_sine_pcm_s16le(sample_count)
    wav_bytes = _pack_wav_bytes(pcm, sample_rate=16000, channels=1, bits_per_sample=16)
    return wav_bytes, sample_count, hashlib.sha256(wav_bytes).hexdigest()


def make_silent_wav(sample_count: int = 32000) -> tuple[bytes, int, str]:
    """生成纯静音块（全零样本）。"""
    pcm = b"\x00\x00" * sample_count
    wav_bytes = _pack_wav_bytes(pcm, sample_rate=16000, channels=1, bits_per_sample=16)
    return wav_bytes, sample_count, hashlib.sha256(wav_bytes).hexdigest()


def make_zero_sample_wav() -> tuple[bytes, int, str]:
    """生成样本数为 0 的非法块。"""
    wav_bytes = _pack_wav_bytes(b"", sample_rate=16000, channels=1, bits_per_sample=16)
    return wav_bytes, 0, hashlib.sha256(wav_bytes).hexdigest()


def make_tampered_header_wav(
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    bits_per_sample: int = 16,
    sample_count: int = 1600,
    declared_data_bytes: int | None = None,
) -> tuple[bytes, int, str]:
    """生成头部字段被篡改（如采样率、声道数、位深或数据长度不符）的 WAV 块。"""
    bytes_per_sample = max(1, bits_per_sample // 8)
    pcm = b"\x00" * (sample_count * channels * bytes_per_sample)
    wav_bytes = _pack_wav_bytes(
        pcm,
        sample_rate=sample_rate,
        channels=channels,
        bits_per_sample=bits_per_sample,
        declared_data_bytes=declared_data_bytes,
    )
    return wav_bytes, sample_count, hashlib.sha256(wav_bytes).hexdigest()


def parse_wav_with_stdlib(wav_bytes: bytes) -> dict[str, int]:
    """使用标准库 wave 解析 WAV 头部信息。"""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return {
            "channels": wf.getnchannels(),
            "sample_width": wf.getsampwidth(),
            "sample_rate": wf.getframerate(),
            "nframes": wf.getnframes(),
        }
