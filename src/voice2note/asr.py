"""ASR 阶段：Fun-ASR-Nano 转写。

模型固定（model_id/revision 写死，不暴露配置），设备与加载方式由配置驱动：
`TORCH_BACKEND` 决定设备，`DISABLE_MMAP` 生效值透传给模型与 processor 加载。

长音频分块转写：audio tower 位置嵌入上限 2048 帧（约 122 秒），超出会报
张量尺寸错误；因此按上限分块，并在目标切点附近做音量感知（选最安静的
窗口下刀），避免切断句子。
"""

from __future__ import annotations

from pathlib import Path

from voice2note.config import Config

MODEL_ID = "FunAudioLLM/Fun-ASR-Nano-2512-hf"
MODEL_REVISION = "d93b302ee7fd505e1b3576120fc142fc6f7820e1"
MAX_NEW_TOKENS = 512

#: librosa/soundfile 无法解码、需经 PyAV（FFmpeg）解码的格式
AV_SUFFIXES = frozenset({".aac", ".m4a", ".m4b", ".mp4"})

#: 采样率（processor 的 audio_kwargs 与分块均按此值）
SAMPLING_RATE = 16000

#: 单块最大样本数。实测帧率约 16.7 帧/秒（7.18s -> 120 帧），位置嵌入
#: 上限 2048 帧 ≈ 122.6s；留出余量取 ~112s。
CHUNK_MAX_SAMPLES = 1_800_000

#: 音量感知切点的搜索半径与窗口（20ms）
_SPLIT_SEARCH_SAMPLES = 5 * SAMPLING_RATE
_SPLIT_WINDOW_SAMPLES = 320

_processor = None
_model = None


def _load(config: Config):
    """懒加载模型（进程内单例），避免 `v2n config` 等轻量命令导入 torch。"""
    global _processor, _model
    if _model is None:
        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

        _processor = AutoProcessor.from_pretrained(
            MODEL_ID,
            revision=MODEL_REVISION,
            trust_remote_code=False,
            token=False,
            disable_mmap=config.disable_mmap,
        )
        _model = (
            AutoModelForSpeechSeq2Seq.from_pretrained(
                MODEL_ID,
                revision=MODEL_REVISION,
                trust_remote_code=False,
                token=False,
                dtype=torch.bfloat16,
                disable_mmap=config.disable_mmap,
            )
            .to(config.torch_backend)
            .eval()
        )
    return _processor, _model


def _decode_audio_with_av(audio_path: Path, sampling_rate: int = SAMPLING_RATE):
    """经 PyAV（FFmpeg）解码为 16kHz 单声道 float32 NumPy 数组。"""
    import av
    import numpy as np

    container = av.open(str(audio_path))
    try:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", rate=sampling_rate, layout="mono")
        chunks: list = []
        for packet in container.demux(stream):
            for frame in packet.decode():
                for resampled in resampler.resample(frame):
                    chunks.append(resampled.to_ndarray())
    finally:
        container.close()
    if not chunks:
        raise RuntimeError(f"音频文件无可解码的音频流: {audio_path}")
    # fltp 的 to_ndarray 形状为 (channels, samples)，已重采样为 mono
    return np.concatenate(chunks, axis=1)[0].astype("float32")


def load_audio_array(audio_path: Path):
    """加载音频为 16kHz 单声道 float32 数组（librosa 或 PyAV）。"""
    if audio_path.suffix.lower() in AV_SUFFIXES:
        return _decode_audio_with_av(audio_path)
    import librosa

    audio, _ = librosa.load(str(audio_path), sr=SAMPLING_RATE, mono=True)
    return audio


def _find_split_point(audio, target: int) -> int:
    """在 ``target`` 之前搜索最安静的 20ms 窗口作为切点（音量感知）。

    只向前搜索，保证分块不超过单块上限。
    """
    import numpy as np

    lo = max(0, target - _SPLIT_SEARCH_SAMPLES)
    hi = min(len(audio), target)
    n_windows = (hi - lo) // _SPLIT_WINDOW_SAMPLES
    if n_windows < 1:
        return target
    windows = audio[lo : lo + n_windows * _SPLIT_WINDOW_SAMPLES].reshape(
        n_windows, _SPLIT_WINDOW_SAMPLES
    )
    rms = np.sqrt(np.mean(windows.astype("float64") ** 2, axis=1))
    return lo + int(np.argmin(rms)) * _SPLIT_WINDOW_SAMPLES


def split_audio(audio) -> list:
    """将音频按单块上限分块，切点做音量感知以避免切断句子。"""
    if len(audio) <= CHUNK_MAX_SAMPLES:
        return [audio]

    chunks = []
    pos = 0
    while pos < len(audio):
        end = pos + CHUNK_MAX_SAMPLES
        if end < len(audio):
            end = _find_split_point(audio, end)
        chunks.append(audio[pos:end])
        pos = end
    return chunks


def _join_transcripts(parts: list[str], language: str) -> str:
    """拼接各块转写文本：中日文不加空格，其余语言以空格连接。"""
    glue = "" if language.lower() in {"zh", "ja", "中文", "日语"} else " "
    return glue.join(part.strip() for part in parts if part.strip())


def _transcribe_array(audio, config: Config) -> str:
    """转写单段音频数组。"""
    import torch

    processor, model = _load(config)
    inputs = processor.apply_transcription_request(
        audio=audio,
        language=config.asr_language,
        processor_kwargs={
            "return_tensors": "pt",
            "audio_kwargs": {"sampling_rate": SAMPLING_RATE},
            "text_kwargs": {"padding": True},
        },
    ).to(config.torch_backend)
    with torch.inference_mode():
        generated = model.generate(
            **inputs, max_new_tokens=MAX_NEW_TOKENS, do_sample=False
        )
    new_tokens = generated[:, inputs.input_ids.shape[1] :]
    return processor.batch_decode(new_tokens, skip_special_tokens=True)[0]


def transcribe(audio_path: Path, config: Config) -> str:
    """将单个音频文件转写为 STT 原稿文本（长音频自动分块）。"""
    audio = load_audio_array(audio_path)
    parts = [_transcribe_array(chunk, config) for chunk in split_audio(audio)]
    return _join_transcripts(parts, config.asr_language)


def save_transcript(transcript: str, audio_path: Path, config: Config) -> Path:
    """将 STT 原稿写入 `TRANSCRIPT_PATH`，文件名与音频主名一致。"""
    out = config.transcript_path / f"{audio_path.stem}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(transcript, encoding="utf-8")
    return out
