import torch
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

model_id = "FunAudioLLM/Fun-ASR-Nano-2512-hf"
revision = "d93b302ee7fd505e1b3576120fc142fc6f7820e1"
audio = "https://huggingface.co/FunAudioLLM/Fun-ASR-Nano-2512/resolve/272c57b82523ada6fd87095e955f8e29100979ab/example/en.mp3"

processor = AutoProcessor.from_pretrained(
    model_id, revision=revision, trust_remote_code=False, token=False, disable_mmap=True
)
model = (
    AutoModelForSpeechSeq2Seq.from_pretrained(
        model_id,
        revision=revision,
        trust_remote_code=False,
        token=False,
        dtype=torch.bfloat16,
        disable_mmap=True,
    )
    .to("xpu")
    .eval()
)
inputs = processor.apply_transcription_request(
    audio=audio,
    language="en",
    processor_kwargs={
        "return_tensors": "pt",
        "audio_kwargs": {"sampling_rate": 16000},
        "text_kwargs": {"padding": True},
    },
)
with torch.inference_mode():
    generated = model.generate(**inputs, max_new_tokens=128, do_sample=False)
new_tokens = generated[:, inputs.input_ids.shape[1] :]
print("结果如下：")
print(processor.batch_decode(new_tokens, skip_special_tokens=True)[0])
