"""OpenAI-style tool calling + JSON mode on top of plain-text workers.

We don't change the worker contract. Instead the gateway:

1. **Wraps the prompt** with a tool registry preamble that instructs the
   model to emit tool calls as ``<tool_call>{...}</tool_call>`` JSON blocks.
2. **Parses the worker's output** to extract any tool_call blocks, returning
   them as OpenAI-shape ``tool_calls`` and stripping them from the visible
   text. ``finish_reason`` flips from ``"stop"`` to ``"tool_calls"`` when
   any are present.
3. **JSON mode** — when ``response_format={"type": "json_object"}`` is set,
   the gateway prepends a strong JSON-only instruction; ``"json_schema"``
   types include the schema so the model knows the shape. After generation
   the gateway scans for the first balanced ``{...}`` block and tries to
   parse it; if successful that becomes the visible content, else the raw
   text is returned with a ``json_parse_error`` field.

Models that follow the system instruction (Qwen-Omni, MiniCPM-o, most
instruction-tuned LLMs) work out of the box. Models that drift produce
``finish_reason="stop"`` with the unparsed text in ``content``; the caller
sees the raw output rather than a malformed tool call.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

# Match `<tool_call>...</tool_call>` blocks. Capture the whole inner block and
# stop only at the closing tag (non-greedy on the CLOSING TAG, not on `}`), so
# a JSON argument string that literally contains "}</tool_call>" isn't
# truncated mid-object. The captured text is trimmed and json.loads-ed by the
# consumer.
_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    re.DOTALL,
)
# Match an alternate function-style block some models emit.
_FUNC_CALL_RE = re.compile(
    r"<function=([A-Za-z_][\w]*)\s*>\s*(\{.*?\})\s*</function>",
    re.DOTALL,
)


def render_tool_preamble(tools: list[dict],
                         tool_choice: Any = "auto") -> str:
    """Build the system-prompt addition that teaches the model the tool registry."""
    if not tools:
        return ""

    lines: list[str] = [
        "# Tools",
        "",
        "You have access to the following tools. To invoke a tool, emit a "
        "block of the form:",
        "",
        '<tool_call>{"name": "<tool_name>", "arguments": {<json args>}}</tool_call>',
        "",
        "Emit one block per tool call. Do not invent tools that are not "
        "listed. After all tool calls, you may include a brief natural-language "
        "summary on a new line. If no tool is needed, answer the user directly "
        "without emitting any tool_call block.",
        "",
    ]
    if tool_choice == "none":
        lines.append("**Do not call any tool. Reply in plain text only.**")
        lines.append("")
    elif isinstance(tool_choice, dict):
        # OpenAI: {"type":"function","function":{"name":"X"}} — force a
        # specific tool. We do this with a strong instruction.
        function = tool_choice.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ValueError("tool_choice.function must contain a name")
        forced = function["name"]
        if forced:
            lines.append(f"**You MUST call the `{forced}` tool exactly once.**")
            lines.append("")
    elif tool_choice == "required":
        lines.append("**You MUST call at least one tool.**")
        lines.append("")

    for i, t in enumerate(tools):
        if not isinstance(t, dict):
            raise ValueError("Each tool must be an object")
        fn = t.get("function") or t
        if (not isinstance(fn, dict) or not isinstance(fn.get("name"), str)
                or not fn["name"] or not isinstance(fn.get("description", ""), str)
                or not isinstance(fn.get("parameters", {}), dict)):
            raise ValueError("Each tool must contain a named function and object parameters")
        name = fn.get("name", f"tool_{i}")
        desc = fn.get("description", "")
        params = fn.get("parameters") or {}
        try:
            params_str = json.dumps(params, indent=2)
        except (TypeError, ValueError):
            params_str = "{}"
        lines.append(f"## {name}")
        if desc:
            lines.append(desc)
        lines.append("")
        lines.append("Parameters (JSON Schema):")
        lines.append("```json")
        lines.append(params_str)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def render_json_mode_preamble(response_format: dict | None) -> str:
    """Build a system-prompt addition that forces JSON-only output.

    Recognised shapes (matching OpenAI's API):

    * ``{"type": "json_object"}`` - any valid JSON object
    * ``{"type": "json_schema", "json_schema": {...}}`` - object matching
      a specific JSON Schema (we just include the schema in the prompt;
      no constrained-decoding here)

    Returns ``""`` for unknown / null shapes.
    """
    if not isinstance(response_format, dict):
        return ""
    rtype = response_format.get("type")
    if rtype == "text":
        return ""
    if rtype not in ("json_object", "json_schema"):
        raise ValueError("response_format.type must be text, json_object, or json_schema")
    lines = [
        "# Response format",
        "",
        "Your entire response MUST be a single valid JSON object. Do NOT "
        "wrap it in Markdown, do NOT add any prose before or after, do NOT "
        "include code fences. The first character must be `{` and the last "
        "must be `}`.",
        "",
    ]
    if rtype == "json_schema":
        schema_block = response_format.get("json_schema") or {}
        if not isinstance(schema_block, dict):
            raise ValueError("response_format.json_schema must be an object")
        schema = schema_block.get("schema") or schema_block
        if schema:
            try:
                schema_str = json.dumps(schema, indent=2)
            except (TypeError, ValueError):
                schema_str = "{}"
            lines.append("The JSON object must conform to this JSON Schema:")
            lines.append("```json")
            lines.append(schema_str)
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def _extract_first_balanced_json(text: str) -> str | None:
    """Return the first balanced ``{...}`` substring in ``text`` (or None).

    Naive depth-counter that ignores braces inside strings. Good enough for
    well-formed model output; if the model writes JSON with literal
    unescaped braces inside string values, we fall through to None and
    let the caller surface a parse_error.
    """
    in_string = False
    escape = False
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        return text[start: i + 1]
    return None


def coerce_json_response(text: str) -> tuple[str, str | None]:
    """Try to coerce a JSON-mode response.

    Returns ``(visible_text, parse_error)``. If parsing succeeds,
    ``visible_text`` is the cleaned/serialized JSON string and
    ``parse_error`` is None. On failure, ``visible_text`` is the raw input
    and ``parse_error`` carries a short reason string.
    """
    if not text:
        return text, "empty response"
    candidate = _extract_first_balanced_json(text) or text.strip()
    try:
        parsed = json.loads(candidate)
    except (ValueError, json.JSONDecodeError) as e:
        return text, f"not valid JSON: {e}"
    return json.dumps(parsed, ensure_ascii=False), None


def parse_tool_calls(text: str) -> tuple[str, list[dict]]:
    """Return ``(visible_text, tool_calls)`` extracted from worker output.

    Tool calls are returned in OpenAI shape:

        [{"id": "call_<hex>", "type": "function",
          "function": {"name": "...", "arguments": "<json string>"}}]

    Note ``arguments`` is a JSON string (per OpenAI spec), not a parsed
    dict. The visible text has the matched blocks removed and is then
    stripped of leading/trailing whitespace.
    """
    if not text:
        return text, []
    calls: list[dict] = []

    def _emit(name: str, args_obj: Any) -> None:
        try:
            args_str = (json.dumps(args_obj) if not isinstance(args_obj, str)
                        else args_obj)
        except (TypeError, ValueError):
            args_str = "{}"
        calls.append({
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {"name": name, "arguments": args_str},
        })

    def _consume_tc(match: re.Match) -> str:
        try:
            obj = json.loads(match.group(1))
        except (ValueError, json.JSONDecodeError):
            return ""
        # The block may contain non-object JSON (e.g. a bare number or string);
        # guard before .get so it's ignored rather than raising AttributeError.
        if not isinstance(obj, dict):
            return ""
        name = str(obj.get("name") or "")
        args = obj.get("arguments") or {}
        if name:
            _emit(name, args)
        return ""

    def _consume_func(match: re.Match) -> str:
        name = match.group(1)
        try:
            args = json.loads(match.group(2))
        except (ValueError, json.JSONDecodeError):
            args = {}
        _emit(name, args)
        return ""

    visible = _TOOL_CALL_RE.sub(_consume_tc, text)
    visible = _FUNC_CALL_RE.sub(_consume_func, visible)
    return visible.strip(), calls
