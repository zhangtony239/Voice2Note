"""配置加载、校验与模板创建。

`config.yaml`（当前工作目录相对路径）是 v2n 的唯一配置来源；
包内模板 `assets/config-template.yaml` 是所有配置的唯一默认值源。
本模块不为任何必填字段提供备用默认值：缺失即抛出 :class:`ConfigError`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

CONFIG_FILE_NAME = "config.yaml"
TEMPLATE_RESOURCE = "config-template.yaml"

#: 必填字段清单（含模板中带默认值的字段；代码中不设备用默认值）
REQUIRED_FIELDS: tuple[str, ...] = (
    "VOICE_PATH",
    "VOICE_FILE_KEYWORD",
    "TORCH_BACKEND",
    "ASR_LANGUAGE",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "SYSTEM_PROMPT",
    "NOTE_PATH",
    "TRANSCRIPT_PATH",
    "MAX_TITLE_LENGTH",
)

VALID_TORCH_BACKENDS: tuple[str, ...] = ("xpu", "cuda")

#: DISABLE_MMAP 未显式配置时按 TORCH_BACKEND 推导的默认值
_DISABLE_MMAP_DEFAULTS: dict[str, bool] = {"xpu": True, "cuda": False}


class ConfigError(Exception):
    """配置文件缺失、无法解析或字段非法。"""


@dataclass(frozen=True)
class Config:
    """校验后的 v2n 配置。"""

    voice_path: Path
    voice_file_keyword: re.Pattern[str]
    torch_backend: str
    disable_mmap: bool
    asr_language: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    system_prompt: str
    note_path: Path
    transcript_path: Path
    max_title_length: int


def config_path(base_dir: Path | None = None) -> Path:
    """返回配置文件的（绝对）路径。"""
    base = base_dir if base_dir is not None else Path.cwd()
    return base / CONFIG_FILE_NAME


def read_template() -> str:
    """读取包内 config 模板的原始文本（保留注释）。"""
    return (
        resources.files("voice2note.assets")
        .joinpath(TEMPLATE_RESOURCE)
        .read_text(encoding="utf-8")
    )


def create_config(base_dir: Path | None = None) -> Path:
    """从模板在 ``base_dir`` 下原样创建 ``config.yaml``，返回其路径。

    原样复制模板文本而非 load→dump，以保留模板中的注释。
    """
    target = config_path(base_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(read_template(), encoding="utf-8")
    return target


def _is_missing(value: object) -> bool:
    return value is None or value == ""


def load_config(base_dir: Path | None = None) -> Config:
    """读取并校验 ``base_dir``（默认当前工作目录）下的 ``config.yaml``。

    任何缺失或非法字段都会抛出 :class:`ConfigError`，缺失字段一次性全部列出。
    """
    path = config_path(base_dir)
    if not path.is_file():
        raise ConfigError(
            f"未找到配置文件: {path}\n请在目标目录下先运行 `v2n config` 创建配置文件。"
        )

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 {path} 不是合法的 YAML:\n{exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {path} 的顶层结构必须是键值映射（mapping）。")

    missing = [field for field in REQUIRED_FIELDS if _is_missing(data.get(field))]
    if missing:
        raise ConfigError(
            f"配置文件 {path} 缺少以下必填字段（代码中无备用默认值）:\n"
            + "\n".join(f"  - {field}" for field in missing)
        )

    torch_backend = str(data["TORCH_BACKEND"]).strip().lower()
    if torch_backend not in VALID_TORCH_BACKENDS:
        raise ConfigError(
            f"TORCH_BACKEND 取值非法: {data['TORCH_BACKEND']!r}，"
            f"仅允许 {' | '.join(VALID_TORCH_BACKENDS)}。"
        )

    try:
        keyword = re.compile(str(data["VOICE_FILE_KEYWORD"]))
    except re.error as exc:
        raise ConfigError(f"VOICE_FILE_KEYWORD 不是合法的正则表达式: {exc}") from exc

    max_title_length = data["MAX_TITLE_LENGTH"]
    if isinstance(max_title_length, bool) or not isinstance(max_title_length, int):
        raise ConfigError(f"MAX_TITLE_LENGTH 必须是整数，当前为 {max_title_length!r}。")

    if "DISABLE_MMAP" in data and not _is_missing(data["DISABLE_MMAP"]):
        disable_mmap = bool(data["DISABLE_MMAP"])
    else:
        disable_mmap = _DISABLE_MMAP_DEFAULTS[torch_backend]

    return Config(
        voice_path=Path(str(data["VOICE_PATH"])),
        voice_file_keyword=keyword,
        torch_backend=torch_backend,
        disable_mmap=disable_mmap,
        asr_language=str(data["ASR_LANGUAGE"]),
        llm_base_url=str(data["LLM_BASE_URL"]),
        llm_api_key=str(data["LLM_API_KEY"]),
        llm_model=str(data["LLM_MODEL"]),
        system_prompt=str(data["SYSTEM_PROMPT"]),
        note_path=Path(str(data["NOTE_PATH"])),
        transcript_path=Path(str(data["TRANSCRIPT_PATH"])),
        max_title_length=max_title_length,
    )


def discover_voice_files(config: Config) -> list[Path]:
    """在 ``VOICE_PATH`` 下发现文件名匹配 ``VOICE_FILE_KEYWORD`` 正则的音频文件。

    相对路径的 ``VOICE_PATH`` 相对当前工作目录解析。
    """
    root = config.voice_path
    if not root.is_dir():
        raise ConfigError(f"VOICE_PATH 不存在或不是目录: {root}")

    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and config.voice_file_keyword.search(path.name)
    )


#: START-END 范围参数格式（数字段：整数或小数）
_RANGE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)")

#: 文件名中的首个数字段
_NUMBER_SEGMENT_PATTERN = re.compile(r"\d+(?:\.\d+)?")


def parse_range(arg: str) -> tuple[float, float]:
    """解析 ``START-END`` 为数值区间（按 float 比较，支持整数与小数）。

    解析失败或 START > END 时抛出 :class:`ConfigError`。
    """
    match = _RANGE_PATTERN.fullmatch(arg.strip())
    if match is None:
        raise ConfigError(
            f"START-END 格式非法: {arg!r}，应为形如 \"141-142\" 或 \"1.2-2.4\" 的范围。"
        )
    start, end = float(match.group(1)), float(match.group(2))
    if start > end:
        raise ConfigError(f"START-END 范围非法: START ({start}) 大于 END ({end})。")
    return start, end


def select_voice_files(config: Config, start: float, end: float) -> list[Path]:
    """按文件名首个数字段的数值选取落在 ``[start, end]`` 闭区间内的音频文件。

    数字段按数值（int/float）而非字符串比较；无数字段的文件不入选；
    结果按数值升序排列。
    """
    selected: list[tuple[float, Path]] = []
    for path in discover_voice_files(config):
        match = _NUMBER_SEGMENT_PATTERN.search(path.stem)
        if match is None:
            continue
        number = float(match.group())
        if start <= number <= end:
            selected.append((number, path))
    return [path for _, path in sorted(selected, key=lambda item: item[0])]
