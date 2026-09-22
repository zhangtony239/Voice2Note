"""ASR 阶段：Fun-ASR-Nano 转写。

模型固定（model_id/revision 写死，不暴露配置），设备与加载方式由配置驱动：
`TORCH_BACKEND` 决定设备，`DISABLE_MMAP` 生效值透传给模型与 processor 加载。

全程语句切分转写：模型按短话语训练，实测 >45s 开始幻觉（>122s 直接超
位置嵌入上限报错），因此先按静音做语句切分（短句合并到 30s 上限，超长
语句按音量感知保底硬切），逐句转写后拼接。
"""

from __future__ import annotations

from pathlib import Path

from voice2note.config import Config

MODEL_ID = "FunAudioLLM/Fun-ASR-Nano-2512-hf"
MODEL_REVISION = "d93b302ee7fd505e1b3576120fc142fc6f7820e1"
MAX_NEW_TOKENS = 512

#: 抑制自回归解码循环（如"零零零…"直至 token 上限）。实测 1.2 可消除
#: 循环且不影响正常转写质量（A/B：循环单元 dup 1.00 -> 0.04）。
REPETITION_PENALTY = 1.2

#: librosa/soundfile 无法解码、需经 PyAV（FFmpeg）解码的格式
AV_SUFFIXES = frozenset({".aac", ".m4a", ".m4b", ".mp4"})

#: 采样率（processor 的 audio_kwargs 与分块均按此值）
SAMPLING_RATE = 16000

#: 单块最大样本数（30 秒）。
#: 实测（会议录音）：≤30s 转写正确，45s 开始重复幻觉，60s+ 完全乱码，
#: >122s（位置嵌入 2048 帧）直接报张量尺寸错误——模型按短话语训练，
#: 有效上限远小于位置嵌入硬上限。全程按语句切分，该值仅作为单条语句
#: 超长时的保底硬切上限。
CHUNK_MAX_SAMPLES = 30 * SAMPLING_RATE

#: 语句切分参数（能量/VAD 式静音检测）
_SEGMENT_FRAME_SAMPLES = 320  # 20ms 帧长
_SEGMENT_MIN_SILENCE_FRAMES = 20  # 静音 ≥0.4s 即断开语音区
_SEGMENT_FLOOR_PERCENTILE = 20  # 噪声地板分位
_SEGMENT_FLOOR_FACTOR = 4.0  # 阈值 = 地板 × 该系数
_SEGMENT_GAP_MERGE_SAMPLES = int(0.5 * SAMPLING_RATE)  # 句间停顿 ≤0.5s 才合并
_SEGMENT_EDGE_PADDING = int(0.15 * SAMPLING_RATE)  # 语音区边缘保留的呼吸空间
_SEGMENT_MIN_UNIT_SAMPLES = SAMPLING_RATE  # <1s 的微单元并入相邻单元

#: 保底硬切的音量感知搜索参数（只向前搜索 5s，窗口 20ms）
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


def segment_audio(audio) -> list:
    """全程语句切分：解码单元只含密集语音。

    1. 20ms 帧级 RMS，自适应阈值（噪声地板 × 系数）判定语音/静音；
    2. 语音区 = 连续语音帧（容忍 <0.4s 的短停顿）；区间的长静音属于
       句间停顿，不并入任何单元——模型按短话语训练，混入大段静音会
       触发重复幻觉（实测"这个图很多表格"×100 即此原因）；
    3. 语音区边缘只保留 0.15s 呼吸空间（避免句界半截词/边缘幻觉）；
    4. 句间停顿 ≤0.5s 的相邻语音区合并（合并后 ≤30s）；
    5. <1s 的微单元并入相邻单元，避免碎片转写；
    6. 单元仍超 30s 时（持续发言无停顿），按音量感知保底硬切。
    """
    import numpy as np

    n_frames = len(audio) // _SEGMENT_FRAME_SAMPLES
    if n_frames == 0:
        return [audio]

    frames = audio[: n_frames * _SEGMENT_FRAME_SAMPLES].reshape(
        n_frames, _SEGMENT_FRAME_SAMPLES
    )
    rms = np.sqrt(np.mean(frames.astype("float64") ** 2, axis=1))
    floor = np.percentile(rms, _SEGMENT_FLOOR_PERCENTILE)
    threshold = max(floor * _SEGMENT_FLOOR_FACTOR, 1e-4)
    speech = rms > threshold

    # 语音区：连续语音帧，容忍 <0.4s 的短停顿
    regions: list[tuple[int, int]] = []
    i = 0
    while i < n_frames:
        if not speech[i]:
            i += 1
            continue
        start, end, gap = i, i, 0
        j = i
        while j < n_frames:
            if speech[j]:
                end, gap = j + 1, 0
            else:
                gap += 1
                if gap >= _SEGMENT_MIN_SILENCE_FRAMES:
                    break
            j += 1
        regions.append((start, end))
        i = j

    # 帧 -> 样本，边缘保留呼吸空间
    units: list[list[int]] = [
        [
            max(0, start * _SEGMENT_FRAME_SAMPLES - _SEGMENT_EDGE_PADDING),
            min(len(audio), end * _SEGMENT_FRAME_SAMPLES + _SEGMENT_EDGE_PADDING),
        ]
        for start, end in regions
    ]
    if not units:
        return [audio]

    # 合并：句间停顿 ≤0.5s 且合并后 ≤30s；<1s 的微单元无条件并入前一个
    merged: list[list[int]] = [units[0]]
    for start, end in units[1:]:
        gap = start - merged[-1][1]
        tiny = end - start < _SEGMENT_MIN_UNIT_SAMPLES
        if (gap <= _SEGMENT_GAP_MERGE_SAMPLES or tiny) and (
            end - merged[-1][0] <= CHUNK_MAX_SAMPLES
        ):
            merged[-1][1] = end
            continue
        merged.append([start, end])

    # 超长单元按音量感知保底硬切
    result = []
    for start, end in merged:
        pos = start
        while end - pos > CHUNK_MAX_SAMPLES:
            cut = _find_split_point(audio, pos + CHUNK_MAX_SAMPLES)
            result.append(audio[pos:cut])
            pos = cut
        result.append(audio[pos:end])
    return result


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
            **inputs,
            max_new_tokens=MAX_NEW_TOKENS,
            do_sample=False,
            repetition_penalty=REPETITION_PENALTY,
        )
    new_tokens = generated[:, inputs.input_ids.shape[1] :]
    return processor.batch_decode(new_tokens, skip_special_tokens=True)[0]


def transcribe(audio_path: Path, config: Config, progress_factory=None) -> str:
    """将单个音频文件转写为 STT 原稿文本（全程语句切分）。

    ``progress_factory(total) -> 更新对象``：语句多于一条时用于 tqdm 进度。
    """
    audio = load_audio_array(audio_path)
    segments = segment_audio(audio)

    bar = (
        progress_factory(total=len(segments))
        if progress_factory and len(segments) > 1
        else None
    )
    parts = []
    for segment in segments:
        parts.append(_transcribe_array(segment, config))
        if bar is not None:
            bar.update(1)
    if bar is not None:
        bar.close()
    return _join_transcripts(parts, config.asr_language)


def save_transcript(transcript: str, audio_path: Path, config: Config) -> Path:
    """将 STT 原稿写入 `TRANSCRIPT_PATH`，文件名与音频主名一致。"""
    out = config.transcript_path / f"{audio_path.stem}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(transcript, encoding="utf-8")
    return out
