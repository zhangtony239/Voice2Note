"""ASR 阶段：Fun-ASR-Nano 转写。

模型固定（model_id/revision 写死，不暴露配置），设备与加载方式由配置驱动：
`TORCH_BACKEND` 决定设备，`DISABLE_MMAP` 生效值透传给模型与 processor 加载。
"""

from __future__ import annotations

from pathlib import Path

from voice2note.config import Config

MODEL_ID = "FunAudioLLM/Fun-ASR-Nano-2512-hf"
MODEL_REVISION = "d93b302ee7fd505e1b3576120fc142fc6f7820e1"
MAX_NEW_TOKENS = 512

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


def transcribe(audio_path: Path, config: Config) -> str:
    """将单个音频文件转写为 STT 原稿文本。"""
    import torch

    processor, model = _load(config)
    inputs = processor.apply_transcription_request(
        audio=str(audio_path),
        language=config.asr_language,
        processor_kwargs={
            "return_tensors": "pt",
            "audio_kwargs": {"sampling_rate": 16000},
            "text_kwargs": {"padding": True},
        },
    )
    with torch.inference_mode():
        generated = model.generate(
            **inputs, max_new_tokens=MAX_NEW_TOKENS, do_sample=False
        )
    new_tokens = generated[:, inputs.input_ids.shape[1] :]
    return processor.batch_decode(new_tokens, skip_special_tokens=True)[0]


def save_transcript(transcript: str, audio_path: Path, config: Config) -> Path:
    """将 STT 原稿写入 `TRANSCRIPT_PATH`，文件名与音频主名一致。"""
    out = config.transcript_path / f"{audio_path.stem}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(transcript, encoding="utf-8")
    return out
