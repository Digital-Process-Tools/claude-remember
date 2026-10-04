from __future__ import annotations
from dataclasses import dataclass, field
@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    cache: int = 0
    cost_usd: float = 0.0
    def __str__(self) -> str:
        return f"{self.input}+{self.cache}cache->{self.output}out (${self.cost_usd:.4f})"
@dataclass
class HaikuResult:
    text: str = ""
    tokens: TokenUsage = field(default_factory=TokenUsage)
    is_skip: bool = False
    is_rejected: bool = False
    provider: str = "claude"
@dataclass
class ExtractResult:
    exchanges: str = ""
    position: int = 0
    human_count: int = 0
    assistant_count: int = 0
    corrupt_lines: int = 0
    envelope: str = ""
    skip_lines: int = 0
    unread_sidecar_unreadable: bool = False
    envelope_unreadable: bool = False
    envelope_capped: bool = False
    envelope_has_unmapped_step: bool = False
@dataclass
class SaveResult:
    action: str = ""
    entry: str = ""
    position: int = 0
    tokens: TokenUsage = field(default_factory=TokenUsage)
@dataclass
class ConsolidationResult:
    recent: str = ""
    archive: str = ""
    tokens: TokenUsage = field(default_factory=TokenUsage)
