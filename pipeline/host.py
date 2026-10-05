from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Mapping
TRANSCRIPT_PATH_VAR = "REMEMBER_TRANSCRIPT_PATH"
CWD_VAR = "REMEMBER_HOOK_CWD"
@dataclass(frozen=True)
class Host:
    name: str
    plugin_root_vars: tuple[str, ...] = ()
    project_dir_vars: tuple[str, ...] = ()
    signature_vars: tuple[str, ...] = field(default=())
    def plugin_root(self, values: Mapping[str, str]) -> str | None:
        return _first_set(values, self.plugin_root_vars)
    def project_dir(self, values: Mapping[str, str]) -> str | None:
        return _first_set(values, self.project_dir_vars)
CLAUDE_CODE = Host(
    name="claude-code",
    plugin_root_vars=("CLAUDE_PLUGIN_ROOT",),
    project_dir_vars=("CLAUDE_PROJECT_DIR",),
    signature_vars=("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"),
)
CODEX = Host(
    name="codex",
    plugin_root_vars=("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"),
    project_dir_vars=("CLAUDE_PROJECT_DIR",),
    signature_vars=("CODEX_SESSION_ID", "CODEX_THREAD_ID"),
)
GEMINI = Host(name="gemini-cli", project_dir_vars=("CLAUDE_PROJECT_DIR",))
ANTIGRAVITY = Host(
    name="antigravity",
    plugin_root_vars=(),
    project_dir_vars=(),
    signature_vars=("ANTIGRAVITY_CONVERSATION_ID",),
)
UNKNOWN = Host(name="unknown", plugin_root_vars=(), project_dir_vars=())
REGISTRY: tuple[Host, ...] = (CLAUDE_CODE, CODEX, ANTIGRAVITY)
PLUGIN_ROOT_VARS: tuple[str, ...] = tuple(
    dict.fromkeys(var for host in REGISTRY for var in host.plugin_root_vars)
)
def _environment_values() -> dict[str, str]:
    return {
        "CLAUDE_PLUGIN_ROOT": os.environ.get("CLAUDE_PLUGIN_ROOT", ""),
        "PLUGIN_ROOT": os.environ.get("PLUGIN_ROOT", ""),
        "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR", ""),
        "CLAUDE_CODE_ENTRYPOINT": os.environ.get("CLAUDE_CODE_ENTRYPOINT", ""),
        "CLAUDE_CODE_SESSION_ID": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        "CODEX_SESSION_ID": os.environ.get("CODEX_SESSION_ID", ""),
        "CODEX_THREAD_ID": os.environ.get("CODEX_THREAD_ID", ""),
        "ANTIGRAVITY_CONVERSATION_ID": os.environ.get("ANTIGRAVITY_CONVERSATION_ID", ""),
        "REMEMBER_TRANSCRIPT_PATH": os.environ.get("REMEMBER_TRANSCRIPT_PATH", ""),
    }
def _first_set(values: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = values.get(name, "")
        if value.strip():
            return value
    return None
def detect_host(overrides: Mapping[str, str] | None = None) -> Host:
    values = _environment_values() if overrides is None else overrides
    for host in REGISTRY:
        if _first_set(values, host.signature_vars) is not None:
            return host
    return UNKNOWN
def plugin_root(overrides: Mapping[str, str] | None = None) -> str | None:
    values = _environment_values() if overrides is None else overrides
    return _first_set(values, PLUGIN_ROOT_VARS)
def sniff_envelope(obj: object) -> str:
    if not isinstance(obj, dict):
        return "unrecognised"
    if isinstance(obj.get("payload"), dict):
        return "codex"
    if isinstance(obj.get("message"), dict) or obj.get("type") in ("user", "assistant", "summary", "system"):
        return "claude-code"
    if (
        "step_index" in obj
        and "source" in obj
        and isinstance(obj.get("content"), str)
    ):
        return "antigravity"
    return "unrecognised"
def codex_exchange(obj: dict) -> tuple[str, str] | None:
    if obj.get("type") != "event_msg":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type == "UserMessage":
        role = "HUMAN"
    elif item_type == "AgentMessage":
        role = "AGENT"
    else:
        return None
    content = item.get("content")
    if not isinstance(content, list):
        return None
    texts = [
        text.strip()
        for block in content
        if isinstance(block, dict)
        for text in [block.get("text")]
        if isinstance(text, str) and text.strip()
    ]
    if not texts:
        return None
    return role, "\n".join(texts)
_ANTIGRAVITY_STEP_ROLES = {
    "USER_INPUT": "HUMAN",
    "PLANNER_RESPONSE": "AGENT",
}
def antigravity_exchange(obj: dict) -> tuple[str, str] | None:
    role = _ANTIGRAVITY_STEP_ROLES.get(obj.get("type"))
    if role is None:
        return None
    content = obj.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return role, content
def antigravity_step_is_unmapped(obj: dict) -> bool:
    step_type = obj.get("type")
    return isinstance(step_type, str) and step_type not in _ANTIGRAVITY_STEP_ROLES
def transcript_path(overrides: Mapping[str, str] | None = None) -> str | None:
    if overrides is None:
        value = (os.environ.get("REMEMBER_TRANSCRIPT_PATH") or "").strip()
    else:
        value = (overrides.get("REMEMBER_TRANSCRIPT_PATH") or "").strip()
    if not value:
        return None
    if not os.path.isfile(value):
        return None
    return value
