"""Voice2Note：从原始录音到笔记 markdown 的工作流。"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("voice2note")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.1.0"
