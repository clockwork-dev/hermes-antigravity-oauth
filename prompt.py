"""Prompt construction, OpenAI-to-Antigravity tool schema bridging, and context pruning."""

from __future__ import annotations

import json
import re
from typing import Any


def _render_message_content(content: Any) -> str:
    """Flatten string, dict, or multimodal list content into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        if isinstance(content.get("content"), str):
            return content["content"].strip()
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def normalize_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Normalize OpenAI chat messages (including assistant tool_calls) into `{role, content}` dicts."""
    normalized: list[dict[str, str]] = []
    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").strip().lower()
        content = _render_message_content(msg.get("content"))

        # Preserve assistant tool_calls in the transcript when replaying history
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            tc_blocks: list[str] = []
            for idx, tc in enumerate(tool_calls):
                if isinstance(tc, dict):
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    tc_id = str(tc.get("id") or f"call_{idx + 1}")
                    fn_name = str(fn.get("name") or "")
                    fn_args = fn.get("arguments", "{}")
                else:
                    fn = getattr(tc, "function", None)
                    tc_id = str(getattr(tc, "id", None) or f"call_{idx + 1}")
                    fn_name = str(getattr(fn, "name", "") if fn else "")
                    fn_args = getattr(fn, "arguments", "{}") if fn else "{}"
                if not isinstance(fn_args, str):
                    try:
                        fn_args = json.dumps(fn_args, ensure_ascii=False)
                    except TypeError:
                        fn_args = "{}"
                if fn_name:
                    payload = json.dumps(
                        {
                            "id": tc_id,
                            "type": "function",
                            "function": {"name": fn_name, "arguments": fn_args},
                        },
                        ensure_ascii=False,
                    )
                    tc_blocks.append(f"<tool_call>{payload}</tool_call>")
            if tc_blocks:
                joined_tc = "\n".join(tc_blocks)
                content = f"{content}\n{joined_tc}".strip() if content else joined_tc

        if content:
            normalized.append({"role": role, "content": content})
    return normalized

_DIRECT_TOOL_BRIDGE_PREAMBLE = (
    "CRITICAL SYSTEM OVERRIDE FOR SUBPROCESS BRIDGE:\n"
    "You are operating as the core reasoning engine inside the Hermes Agent framework. "
    "Because you are connected via a raw text stream, you DO NOT have direct access to your "
    "native internal tools (such as read_file, run_command, write_file, edit_file, grep_search, "
    "list_dir, or find_by_name). Attempting to invoke a native tool directly will immediately "
    "terminate your process.\n\n"
    "Instead, ALL actions MUST be executed by emitting Hermes tool calls in your text response "
    "using the exact XML wrapper below:\n"
    '<tool_call>{"id": "call_1", "type": "function", "function": {"name": "TOOL_NAME", "arguments": "{\\"arg\\": \\"val\\"}"}}</tool_call>\n\n'
    "Strict Rules for Tool Calls:\n"
    "1. Use ONLY the tools listed in the 'Available tools' JSON schema below.\n"
    "2. The `arguments` field MUST be a valid JSON-encoded string (escaped quotes inside the string).\n"
    "3. You may emit multiple <tool_call>...</tool_call> blocks in a single turn if needed.\n"
    "4. Stop generating immediately after emitting your <tool_call> block(s) and wait for the `[tool]` output."
)

_MAX_TOOL_OUTPUT_CHARS = 20_000
_MAX_INTACT_RECENT_TOOL_OUTPUTS = 8
_PRUNED_HISTORICAL_TOOL_MAX_CHARS = 300

_RECOVERY_NUDGE_RE = re.compile(
    r"Please continue where you left off|your previous response was cut off",
    re.IGNORECASE,
)


def _extract_tool_specs(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten OpenAI function tool definitions into compact `{name, description, parameters}` dicts."""
    if not tools:
        return []
    specs: list[dict[str, Any]] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if item.get("type") == "function" or "function" in item else item
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        specs.append({
            "name": name.strip(),
            "description": str(fn.get("description") or "").strip(),
            "parameters": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}},
        })
    return specs


def _render_tool_section(tools: list[dict[str, Any]] | None) -> list[str]:
    """Render the tool bridge preamble and JSON tool schemas."""
    specs = _extract_tool_specs(tools)
    if not specs:
        return []
    schema_json = json.dumps(specs, ensure_ascii=False, separators=(",", ":"))
    return [
        f"[system]\n{_DIRECT_TOOL_BRIDGE_PREAMBLE}\n\nAvailable tools (OpenAI function schema):\n{schema_json}"
    ]


def _truncate_tool_content(content: str, *, is_historical: bool) -> str:
    """Truncate tool output to preserve context window efficiency."""
    text = content or ""
    if is_historical and len(text) > _PRUNED_HISTORICAL_TOOL_MAX_CHARS:
        omitted = len(text) - _PRUNED_HISTORICAL_TOOL_MAX_CHARS
        return (
            f"{text[:_PRUNED_HISTORICAL_TOOL_MAX_CHARS]}\n"
            f"... [Historical tool output pruned for context efficiency ({omitted} chars omitted)]"
        )
    if len(text) > _MAX_TOOL_OUTPUT_CHARS:
        half = _MAX_TOOL_OUTPUT_CHARS // 2
        omitted = len(text) - _MAX_TOOL_OUTPUT_CHARS
        return (
            f"{text[:half]}\n"
            f"... [Tool output truncated ({omitted} chars omitted)] ...\n"
            f"{text[-half:]}"
        )
    return text


def build_prompt_from_messages(
    messages: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None = None,
) -> str:
    """Assemble a full multi-turn prompt string from OpenAI-shaped messages and tool schemas."""
    sections: list[str] = []
    sections.extend(_render_tool_section(tools))

    normalized = normalize_messages(messages or [])
    tool_indices = [
        idx for idx, msg in enumerate(normalized)
        if str(msg.get("role") or "").strip().lower() == "tool"
    ]
    historical_cutoff_set = set(tool_indices[:-_MAX_INTACT_RECENT_TOOL_OUTPUTS]) if len(tool_indices) > _MAX_INTACT_RECENT_TOOL_OUTPUTS else set()

    for idx, msg in enumerate(normalized):
        role = str(msg.get("role") or "user").strip().lower()
        content = str(msg.get("content") or "").strip()
        if role == "tool":
            content = _truncate_tool_content(content, is_historical=(idx in historical_cutoff_set))
        if not content:
            continue
        sections.append(f"[{role}]\n{content}")

    return "\n\n".join(sections).strip()


def format_delta_messages(delta_messages: list[dict[str, Any]] | None) -> str:
    """Format only the new messages added since the previous turn on a persistent `--conversation`."""
    normalized = normalize_messages(delta_messages or [])
    if not normalized:
        return ""

    # Single user message fast path
    if len(normalized) == 1 and str(normalized[0].get("role") or "").strip().lower() == "user":
        content = str(normalized[0].get("content") or "").strip()
        if _RECOVERY_NUDGE_RE.search(content):
            return (
                "[system]\nYour previous turn ended early before completing the response. "
                "Resume immediately from the exact point where you stopped without repeating prior text.\n\n"
                f"[user]\n{content}"
            )
        return content

    tool_indices = [
        idx for idx, msg in enumerate(normalized)
        if str(msg.get("role") or "").strip().lower() == "tool"
    ]
    historical_cutoff_set = set(tool_indices[:-_MAX_INTACT_RECENT_TOOL_OUTPUTS]) if len(tool_indices) > _MAX_INTACT_RECENT_TOOL_OUTPUTS else set()

    sections: list[str] = []
    for idx, msg in enumerate(normalized):
        role = str(msg.get("role") or "user").strip().lower()
        content = str(msg.get("content") or "").strip()
        if role == "tool":
            content = _truncate_tool_content(content, is_historical=(idx in historical_cutoff_set))
        if not content:
            continue
        sections.append(f"[{role}]\n{content}")

    return "\n\n".join(sections).strip()
