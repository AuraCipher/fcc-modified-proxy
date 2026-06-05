# Message Handling & Request Conversion Analysis

## Overview
The NVIDIA NIM proxy acts as a bridge between Anthropic's Claude API format (v1/messages) and upstream providers (OpenAI-compatible, native Anthropic, etc.). This document traces how messages are converted and normalized across the entire pipeline.

---

## 1. Request Entry Points & Initial Processing

### API Route (`api/routes.py`)
```python
@router.post("/v1/messages")
async def create_message(
    request_data: MessagesRequest,
    service: ClaudeProxyService = Depends(get_proxy_service),
    _auth=Depends(require_api_key),
):
    """Create a message (always streaming)."""
    return service.create_message(request_data)
```

**Key Points:**
- All requests (CLI, VS Code extension, etc.) come in via `/v1/messages` endpoint
- Requests are parsed into `MessagesRequest` Pydantic model
- **No differentiation at entry point** between CLI vs extension requests

### Request Model (`api/models/anthropic.py`)
The `MessagesRequest` class defines:
```python
class MessagesRequest(BaseModel):
    model: str
    messages: list[Message]
    system: str | list[SystemContent] | None = None  # System prompt as string or list
    max_tokens: int | None = None
    tools: list[Tool] | None = None
    thinking: ThinkingConfig | None = None
    # ... other fields
```

**System Message Shape:**
- Can be a simple `str`
- Or a list of `SystemContent` blocks (only `{"type": "text", "text": "..."}` blocks supported)
- Stored separately from the messages array (not as a "system" role message)

---

## 2. CLI-Specific Event Handling (messaging/event_parser.py)

**CRITICAL: System events are explicitly filtered at CLI level:**

```python
def parse_cli_event(event: Any, *, log_raw_cli: bool = False) -> list[dict]:
    # ... 
    # Some CLI/proxy layers emit "system" events that are not user-visible and
    # carry no transcript content. Ignore them explicitly to avoid noisy logs.
    if etype == "system":
        return []
```

**Test confirms this:**
```python
def test_parse_cli_event_system_ignored():
    assert parse_cli_event({"type": "system", "foo": "bar"}) == []
```

**Implications:**
- CLI events with `"type": "system"` are stripped at the messaging/event_parser level
- This is **different from API requests** - API requests preserve system prompts in the `system` field
- **This is not a message conversion issue; it's intentional filtering of telemetry events**

---

## 3. Message Routing (`api/model_router.py`)

```python
def resolve_messages_request(
    self, request: MessagesRequest
) -> RoutedMessagesRequest:
    """Return an internal routed request context."""
    resolved = self.resolve(request.model)
    routed = request.model_copy(deep=True)
    routed.model = resolved.provider_model
    return RoutedMessagesRequest(request=routed, resolved=resolved)
```

**Flow:**
1. Incoming Claude model name (e.g., "claude-3-opus") is resolved to a provider/model pair
2. Request is copied and model field updated with provider's actual model ID
3. Provider ID determines which request builder is used

**OpenAI Chat Upstream Providers:**
```python
_OPENAI_CHAT_UPSTREAM_IDS = frozenset({"nvidia_nim", "opencode", "zai"})
```

These providers use OpenAI-compatible `/chat/completions` endpoint and require message conversion.

---

## 4. System Message Conversion (`core/anthropic/conversion.py`)

### `convert_system_prompt()` - The Key Function
```python
@staticmethod
def convert_system_prompt(system: Any) -> dict[str, str] | None:
    if isinstance(system, str):
        return {"role": "system", "content": system}
    if isinstance(system, list):
        text_parts = [
            get_block_attr(block, "text", "")
            for block in system
            if get_block_type(block) == "text"
        ]
        if text_parts:
            return {"role": "system", "content": "\n\n".join(text_parts).strip()}
    return None
```

**Behavior:**
- Converts string system prompt → `{"role": "system", "content": "..."}`
- Converts list of text blocks → single `{"role": "system", "content": "..."}` (concatenates with `\n\n`)
- Ignores non-text blocks in system list
- Returns `None` if no system prompt provided

### System Message Insertion in `build_base_request_body()`
```python
def build_base_request_body(
    request_data: Any,
    *,
    default_max_tokens: int | None = None,
    reasoning_replay: ReasoningReplayMode = ReasoningReplayMode.THINK_TAGS,
) -> dict[str, Any]:
    """Build the common parts of an OpenAI-format request body."""
    # Convert user/assistant messages
    messages = AnthropicToOpenAIConverter.convert_messages(
        request_data.messages,
        reasoning_replay=reasoning_replay,
    )

    # Convert and INSERT system message at position 0
    system = getattr(request_data, "system", None)
    if system:
        system_msg = AnthropicToOpenAIConverter.convert_system_prompt(system)
        if system_msg:
            messages.insert(0, system_msg)  # ← SYSTEM GOES FIRST
```

**Key Point:** System message is always **prepended** to the messages array at index 0.

---

## 5. Message Format Conversion (`core/anthropic/conversion.py`)

### `AnthropicToOpenAIConverter.convert_messages()`

Converts Anthropic message format to OpenAI format:

**Role Handling:**
```python
for msg in messages:
    role = msg.role  # "user" or "assistant"
    content = msg.content
```

**Supported Roles:**
- ✅ `"user"` - Converted as-is
- ✅ `"assistant"` - Converted with tool handling
- ❌ `"system"` role messages - **NOT in Anthropic format** (system is top-level field)

**Content Block Conversion:**

For **user messages:**
```python
if block_type == "text":
    text_parts.append(get_block_attr(block, "text", ""))
elif block_type == "tool_result":
    # Converted to role: "tool" with tool_call_id
    result.append({
        "role": "tool",
        "tool_call_id": tool_use_id,
        "content": serialized_content,
    })
```

For **assistant messages:**
```python
if block_type == "text":
    content_parts.append(get_block_attr(block, "text", ""))
elif block_type == "tool_use":
    tool_calls.append({
        "id": tool_id,
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps(tool_input),
        },
    })
elif block_type == "thinking":
    # Handled based on reasoning_replay mode:
    # THINK_TAGS: wrapped in <think>...</think>
    # REASONING_CONTENT: stored in "reasoning_content" field
    # DISABLED: omitted
```

---

## 6. Provider-Specific Request Building

### NVIDIA NIM (`providers/nvidia_nim/request.py`)

**For OpenAI-compatible endpoints:**
```python
def build_request_body(
    request_data: Any, nim: NimSettings, *, thinking_enabled: bool
) -> dict:
    """Build OpenAI-format request body from Anthropic request."""
    # 1. Convert to OpenAI format using base converter
    body = build_base_request_body(
        request_data,
        reasoning_replay=ReasoningReplayMode.REASONING_CONTENT
        if thinking_enabled
        else ReasoningReplayMode.DISABLED,
    )
    
    # 2. Sanitize tool schemas (NIM-specific)
    _sanitize_nim_tool_schemas(body)
    
    # 3. Apply NIM-specific settings
    # - max_tokens capping
    # - temperature/top_p defaults
    # - stop sequences
    # - extra_body parameters
```

**System message handling in NIM:** Inherited from `build_base_request_body()` - system is inserted at position 0.

### DeepSeek (Native Anthropic) (`providers/deepseek/request.py`)

**Native Anthropic format (preserves Anthropic structure):**
```python
def build_request_body(request_data: Any, *, thinking_enabled: bool) -> dict:
    """Build a DeepSeek ``/v1/messages`` JSON body (Anthropic format)."""
    # Uses native Anthropic builder, not OpenAI converter
    body = build_base_native_anthropic_request_body(
        request_data,
        default_max_tokens=ANTHROPIC_DEFAULT_MAX_OUTPUT_TOKENS,
        thinking_enabled=thinking_enabled,
    )
```

**System message handling:** Kept in `system` field (Anthropic native format, not converted to role).

---

## 7. OpenRouter (Native Anthropic) (`core/anthropic/native_messages_request.py`)

```python
def build_openrouter_native_request_body(
    request_data: Any,
    *,
    thinking_enabled: bool,
    default_max_tokens: int,
) -> dict[str, Any]:
    """Build an Anthropic-format request body for OpenRouter."""
    # ...
    
    # SYSTEM NORMALIZATION for OpenRouter
    if "system" in body:
        body["system"] = _normalize_system_prompt_for_openrouter(body["system"])
```

**System normalization function:**
```python
def _normalize_system_prompt_for_openrouter(system: Any) -> Any:
    """Flatten Claude SDK system blocks for OpenRouter's native endpoint."""
    if not isinstance(system, list):
        return system  # Return string as-is

    # Extract text blocks and concatenate
    text_parts: list[str] = []
    for block in system:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
    return "\n\n".join(text_parts).strip() if text_parts else system
```

**Key Difference:** System blocks are flattened to text before sending to OpenRouter (native Anthropic format still uses `system` field, not role).

---

## 8. Comparison: CLI vs VS Code Extension Requests

| Aspect | CLI | VS Code Extension |
|--------|-----|-------------------|
| **Entry Point** | `/v1/messages` API endpoint | Same API endpoint |
| **Request Format** | `MessagesRequest` Pydantic model | Same model |
| **System Message Field** | In `system` field | In `system` field |
| **Message Roles** | "user", "assistant" only | "user", "assistant" only |
| **Event Filtering** | `"system"` events stripped by event_parser | N/A (uses HTTP API) |
| **Message Conversion** | Same path through router → provider | Same path |
| **Provider Routing** | Same logic | Same logic |
| **Thinking Support** | Same | Same |

**No difference in HTTP API message handling between CLI and extension.**

---

## 9. System Message Handling Summary

### Where System Messages Live
1. **Input:** `MessagesRequest.system` field (top-level, not in messages array)
2. **After Conversion (OpenAI):** Inserted as `{"role": "system", "content": "..."}` at `messages[0]`
3. **Native Anthropic:** Stays in top-level `system` field (not converted)

### Conversion Logic
| Provider Type | Conversion | System Field | Notes |
|---------------|-----------|--------------|-------|
| **OpenAI Compatible** (NIM, Opencode, Zai) | `convert_system_prompt()` | Converted to message with role | Inserted at messages[0] |
| **Native Anthropic** (DeepSeek, OpenRouter, Kimi) | `_normalize_*()` | Stays as `system` field | Text blocks flattened to string |

### Potential Issues (None Found)
✅ **System messages are correctly preserved** - no filtering or loss
✅ **Role validation** - only "user" and "assistant" messages in message array
✅ **System prompt insertion** - always at index 0 for OpenAI format
✅ **No content loss** - text blocks in system list are concatenated, not dropped

---

## 10. Tool Result Serialization

**User message tool_result blocks** are converted to OpenAI role="tool" format:

```python
elif block_type == "tool_result":
    tool_content = get_block_attr(block, "content", "")
    serialized = _serialize_tool_result_content(tool_content)
    result.append(
        {
            "role": "tool",
            "tool_call_id": get_block_attr(block, "tool_use_id"),
            "content": serialized if serialized else "",
        }
    )
```

**Serialization handles:**
- `None` → `""`
- `str` → as-is
- `dict` → `json.dumps()`
- `list` → each item serialized, joined with `\n`

---

## 11. Reasoning Content Handling

**Different replay modes for assistant reasoning:**

```python
class ReasoningReplayMode(StrEnum):
    DISABLED = "disabled"           # Omit thinking blocks
    THINK_TAGS = "think_tags"       # Wrap in <think>...</think> tags
    REASONING_CONTENT = "reasoning_content"  # OpenAI reasoning_content field
```

**Selection by Provider:**
- **OpenAI-compatible (NIM):** `REASONING_CONTENT` for thinking_enabled, else `DISABLED`
- **OpenRouter:** Different policy per request
- **DeepSeek:** Native thinking support

---

## 12. Request Flow Diagram

```
┌─────────────────────────────────────┐
│  API Request Entry                  │
│  POST /v1/messages                  │
│  MessagesRequest (Pydantic)         │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Model Router                       │
│  Resolve: claude-3-x → provider/x   │
│  Copy request, update model field   │
└──────────────┬──────────────────────┘
               │
         ┌─────┴─────┐
         ▼           ▼
    ┌─────────┐  ┌──────────────┐
    │OpenAI   │  │Native Anthro │
    │Compat   │  │pich (DeepSeek│
    │(NIM)    │  │OpenRouter)   │
    └────┬────┘  └──────┬───────┘
         │               │
         ▼               ▼
┌──────────────────┐  ┌────────────────┐
│build_base_       │  │build_native_   │
│request_body()    │  │request_body()  │
│                  │  │                │
│1. Convert        │  │1. Preserve     │
│   messages via   │  │   Anthropic    │
│   Converter      │  │   format       │
│                  │  │                │
│2. Convert system │  │2. Normalize    │
│   → role:"system"│  │   system if    │
│                  │  │   needed       │
│3. Insert system  │  │                │
│   at messages[0] │  │3. Keep system  │
│                  │  │   field as-is  │
└──────────────────┘  └────────────────┘
```

---

## 13. Analysis Conclusions

### ✅ No System Message Issues Found
1. **System messages are correctly converted** from string/blocks to correct format
2. **No filtering or loss** - all system prompt content is preserved
3. **Correct insertion point** - system messages go first for OpenAI format
4. **Native format respected** - Anthropic-compatible providers keep system in top-level field

### ✅ CLI vs Extension: No Difference
- Both use same `/v1/messages` API endpoint
- Both go through same message conversion pipeline
- CLI "system" event filtering is for telemetry events, not message content

### ⚠️ Note on CLI "system" Events
The test `test_parse_cli_event_system_ignored()` filters CLI events with `"type": "system"`. This is **intentional and correct** - it's filtering non-user-facing telemetry events, not user message content. The `MessagesRequest.system` field (which contains user-facing system prompts) is handled separately and correctly.

### 🔑 Key Code Paths
1. **System message conversion:** `core/anthropic/conversion.py:convert_system_prompt()`
2. **Insertion:** `core/anthropic/conversion.py:build_base_request_body()`
3. **OpenAI building:** `providers/nvidia_nim/request.py:build_request_body()`
4. **Native building:** `core/anthropic/native_messages_request.py:build_*_request_body()`

