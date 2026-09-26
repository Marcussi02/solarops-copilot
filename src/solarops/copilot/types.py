from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)


class ProviderError(RuntimeError):
    """A model provider failed or returned something unusable."""
