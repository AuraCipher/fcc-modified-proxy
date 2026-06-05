# OpenCode Zen Request-Response Flow: Complete Trace

**When you send a prompt to the proxy, here's EXACTLY what happens:**

---

## 🔴 STEP 1: Request Arrives at Proxy

```bash
# You send this:
curl -X POST http://localhost:5000/v1/messages \
  -H "Authorization: Bearer freecc" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-3-5-sonnet",
    "messages": [
      {"role": "user", "content": "Write a hello world function"}
    ]
  }'

# OR via SDK:
from anthropic import Anthropic
client = Anthropic(base_url="http://localhost:5000", api_key="freecc")
response = client.messages.create(
    model="claude-3-5-sonnet",
    messages=[{"role": "user", "content": "Write a hello world function"}]
)
```

**Request lands at:** `api/routes.py` → `/v1/messages` endpoint

---

## 🟡 STEP 2: Authentication Check

**File:** [api/dependencies.py](api/dependencies.py) → `require_api_key()`

```python
def require_api_key(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """
    Checks: x-api-key header OR Authorization: Bearer ...
    Current config: ANTHROPIC_AUTH_TOKEN="freecc"
    """
    token = "freecc"  # extracted from Authorization header
    
    if token == "freecc":  # ✅ Matches settings
        return  # Continue to next step
    else:
        raise HTTPException(status_code=401, detail="Invalid API key")
```

**What happens:**
- ✅ Authorization validated
- ✅ Request continues to service layer

---

## 🟠 STEP 3: Model Router Decides Which Provider to Use

**File:** [api/model_router.py](api/model_router.py) → `ModelRouter.resolve()`

```python
class ModelRouter:
    def resolve(self, claude_model_name: str) -> ResolvedModel:
        """
        Input: model="claude-3-5-sonnet"
        Task: Figure out which provider actually handles this
        """
        
        # STEP 3A: Try gateway ID format (anthropic/provider/model)
        decoded = decode_gateway_model_id("claude-3-5-sonnet")
        # Result: None (not a gateway-encoded model)
        
        # STEP 3B: Try direct provider/model format (provider_id/model_name)
        provider_id, sep, model_name = "claude-3-5-sonnet".partition("/")
        # provider_id = "claude-3-5-sonnet"
        # sep = ""  (no "/" found)
        # Result: None (not provider/model format)
        
        # STEP 3C: Fall back to environment variable mapping
        provider_model_ref = self._settings.resolve_model("claude-3-5-sonnet")
        # Checks: MODEL_SONNET env var → not set
        # Checks: MODEL env var → "opencode/qwen3.6-plus"
        
        provider_id = Settings.parse_provider_type("opencode/qwen3.6-plus")
        # provider_id = "opencode"
        
        provider_model = Settings.parse_model_name("opencode/qwen3.6-plus")
        # provider_model = "qwen3.6-plus"
        
        thinking_enabled = self._settings.resolve_thinking("claude-3-5-sonnet")
        # thinking_enabled = True  (from ENABLE_MODEL_THINKING=true)
        
        return ResolvedModel(
            original_model="claude-3-5-sonnet",
            provider_id="opencode",                    # ← THIS DECIDES THE PROVIDER
            provider_model="qwen3.6-plus",             # ← THIS IS THE ACTUAL MODEL
            provider_model_ref="opencode/qwen3.6-plus", # ← FULL PATH
            thinking_enabled=True                      # ← REASONING ENABLED
        )
```

**Summary of Step 3:**
```
Input model: "claude-3-5-sonnet" (Claude model name)
  ↓
Check gateway ID? No
  ↓
Check provider/model format? No
  ↓
Check environment mapping: MODEL="opencode/qwen3.6-plus"
  ↓
Decision: Use OpenCode provider with "qwen3.6-plus" model
  ↓
Output: ResolvedModel(provider_id="opencode", provider_model="qwen3.6-plus")
```

**Log output:**
```
MODEL MAPPING: 'claude-3-5-sonnet' -> 'opencode/qwen3.6-plus'
```

---

## 🟡 STEP 4: Get Provider Instance

**File:** [api/dependencies.py](api/dependencies.py) → `resolve_provider()`

```python
def resolve_provider(
    provider_type: str,  # "opencode"
    app: Starlette,
    settings: Settings
) -> BaseProvider:
    """
    Get the OpenCode provider instance (or create if not cached)
    """
    
    # Use app-scoped registry (created during app startup)
    reg = app.state.provider_registry  # ProviderRegistry instance
    
    # Request: Get provider for "opencode"
    provider = reg.get("opencode", settings)
    
    # Inside registry.get():
    #   1. Check if "opencode" provider is cached
    #   2. If not, call: _create_opencode(config, settings)
    #   3. Instantiate: OpenCodeProvider(config)
    #      - Load API key from settings.opencode_api_key
    #      - Load base URL from provider_catalog.py
    #      - Initialize rate limiter
    
    return provider  # OpenCodeProvider instance
```

**Step 4 Details:**

| Item | Value |
|------|-------|
| Provider Class | `OpenCodeProvider` |
| API Key Source | `OPENCODE_API_KEY` env var |
| API Key Value | `sk-KK1vg7D6...` |
| Base URL | `https://opencode.ai/zen/v1` |
| Transport Type | OpenAI-compatible (`/chat/completions`) |

**Log output:**
```
Provider initialized: opencode
```

---

## 🟢 STEP 5: Request Body Conversion (Anthropic → OpenCode)

**File:** [providers/opencode/request.py](providers/opencode/request.py) → `build_request_body()`

```python
def build_request_body(request_data: MessagesRequest, thinking_enabled: bool) -> dict:
    """
    Convert Anthropic format → OpenAI format for OpenCode
    """
    
    # Input (Anthropic format):
    request_data = {
        "model": "qwen3.6-plus",  # Already routed to Alibaba model
        "messages": [
            {"role": "user", "content": "Write a hello world function"}
        ],
        "system": None,  # If present, will be converted
        "thinking": None,
        "tools": None,
        "max_tokens": 4096
    }
    
    # Call base converter
    body = build_base_request_body(
        request_data,
        reasoning_replay=ReasoningReplayMode.REASONING_CONTENT  # thinking enabled
    )
    
    # Inside build_base_request_body() [core/anthropic/conversion.py]:
    #   1. Convert system prompt (if present)
    #   2. Convert messages array
    #   3. Convert tools array
    #   4. Set model = "qwen3.6-plus"
    #   5. Add stream=true for streaming
    
    # Output (OpenAI ChatCompletions format):
    return {
        "model": "qwen3.6-plus",
        "messages": [
            {"role": "user", "content": "Write a hello world function"}
        ],
        "stream": True,  # Always streaming
        "max_tokens": 4096,
        "temperature": 0.3,  # If specified
        "thinking": {"type": "enabled"}  # If thinking_enabled=true
    }
```

**Conversion Details:**

| Field | Anthropic Input | OpenCode Output |
|-------|-----------------|-----------------|
| `model` | `"qwen3.6-plus"` | `"qwen3.6-plus"` ✅ |
| `messages` | `[{role, content}]` | `[{role, content}]` ✅ |
| `system` | `"You are helpful"` (if present) | `[{role: "system", content: "..."}]` ✅ |
| `thinking` | (from request) | `{"type": "enabled"}` ✅ |
| `tools` | (from request) | Converted to OpenAI format ✅ |
| `stream` | (implicit) | `true` ✅ |

**Log output:**
```
OPENCODE_REQUEST: conversion start model=qwen3.6-plus msgs=1
OPENCODE_REQUEST: conversion done model=qwen3.6-plus msgs=1 tools=0
```

---

## 🔵 STEP 6: HTTP Request to OpenCode API

**File:** [providers/opencode/client.py](providers/opencode/client.py) → `OpenCodeProvider.stream_response()`

```python
class OpenCodeProvider(OpenAIChatTransport):
    """Extends OpenAIChatTransport which handles HTTP calls"""
    
    async def stream_response(self, request, thinking_enabled=None):
        # Build the request body (from Step 5)
        body = self._build_request_body(request, thinking_enabled)
        
        # Make HTTP request to OpenCode Zen
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url="https://opencode.ai/zen/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",  # sk-KK1vg7D6...
                    "Content-Type": "application/json"
                },
                json=body,  # The converted request
                stream=True  # Server Sent Events
            )
            
            # Response is streaming (SSE format)
            async for line in response.aiter_lines():
                yield line
```

**Network Details:**

```
POST https://opencode.ai/zen/v1/chat/completions
Headers:
  Authorization: Bearer sk-KK1vg7D6QDWA53OQntdL0nlmBsRho02D2o1Xjqxk3XwvCaOHLv8ep3BWFcq59sQ8
  Content-Type: application/json

Body:
{
  "model": "qwen3.6-plus",
  "messages": [{"role": "user", "content": "Write a hello world function"}],
  "stream": true,
  "thinking": {"type": "enabled"}
}
```

**What Happens at OpenCode:**
1. OpenCode validates API key
2. Checks model availability ("qwen3.6-plus" exists ✅)
3. Routes to Alibaba Qwen 3.6 API
4. Generates response with thinking enabled
5. Streams back as Server Sent Events (SSE)

---

## 🟣 STEP 7: OpenCode Returns Streaming Response

**OpenCode SSE format:**

```
data: {"id": "chatcmpl-...", "object": "text_completion.chunk", "created": 1717100000, ...}

data: {"choices": [{"delta": {"content": "def ", "reasoning_content": null}, ...}]}

data: {"choices": [{"delta": {"content": "hello_world", ...}]}

data: {"choices": [{"delta": {"content": "():", ...}]}

...more content chunks...

data: {"choices": [{"finish_reason": "stop"}], "usage": {"prompt_tokens": 15, "completion_tokens": 47}}

data: [DONE]
```

**Response contains:**
- `delta.content` — The actual text being generated
- `delta.reasoning_content` — Internal thinking (if enabled)
- `usage` — Token counts for billing
- `finish_reason` — "stop" when complete

---

## 🟠 STEP 8: Normalize Response to Anthropic Format

**File:** [core/anthropic/sse.py](core/anthropic/sse.py) + [providers/openai_compat.py](providers/openai_compat.py)

OpenCode returns OpenAI format, but your client expects Anthropic format:

```python
# OpenCode (OpenAI) format incoming:
delta = {
    "content": "def hello_world",
    "reasoning_content": "I need to write a function..."
}

# Convert to Anthropic SSE format:
anthropic_event = {
    "type": "content_block_delta",
    "index": 0,
    "delta": {
        "type": "text_delta",
        "text": "def hello_world"
    }
}

# Anthropic reasoning blocks (if thinking enabled):
reasoning_event = {
    "type": "content_block_start",
    "index": 1,
    "content_block": {
        "type": "thinking",
        "thinking": "I need to write a function..."
    }
}
```

**Conversion Logic:**
```
OpenAI deltas → Parse token type → Map to Anthropic blocks → Emit as SSE
```

---

## 🔴 STEP 9: Proxy Streams Response Back to Client

**File:** [api/services.py](api/services.py) → `ClaudeProxyService.create_message()`

```python
async def create_message(self, request_data: MessagesRequest):
    # Steps 1-8 completed ↑
    
    # Get the OpenCode streaming response
    stream = await provider.stream_response(routed_request)
    
    # Wrap in Anthropic SSE format
    async def stream_wrapper():
        async for chunk in stream:
            # Chunk is already normalized to Anthropic format
            yield chunk
    
    # Return streaming response to client
    return anthropic_sse_streaming_response(stream_wrapper())
```

**Response headers sent to client:**

```
HTTP/1.1 200 OK
content-type: text/event-stream
cache-control: no-cache
connection: keep-alive
Transfer-Encoding: chunked
```

**Response body (Anthropic SSE format):**

```
event: message_start
data: {
  "type": "message_start",
  "message": {
    "id": "msg_3c6...",
    "type": "message",
    "role": "assistant",
    "model": "claude-3-5-sonnet",
    "stop_reason": null,
    "thinking": {"type": "enabled"},
    "usage": {"input_tokens": 15, "output_tokens": 0}
  }
}

event: content_block_start
data: {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}

event: content_block_delta
data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "def hello_world"}}

event: content_block_delta
data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "():\n"}}

...more deltas...

event: message_delta
data: {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 47}}

event: message_stop
data: {"type": "message_stop"}

event: message_end
data: {"type": "message_end"}
```

---

## 🟩 STEP 10: Client Receives & Processes Response

**Client (SDK or curl) receives the stream:**

```python
# Python SDK example
from anthropic import Anthropic

client = Anthropic(base_url="http://localhost:5000", api_key="freecc")
response = client.messages.create(
    model="claude-3-5-sonnet",  # This is what you request
    messages=[{"role": "user", "content": "Write a hello world function"}],
    stream=True
)

# Client automatically:
# 1. Parses SSE stream
# 2. Accumulates text deltas
# 3. Returns message when done

full_response = ""
thinking_output = ""

for event in response:
    if event.type == "content_block_delta":
        if event.delta.type == "text_delta":
            full_response += event.delta.text  # Accumulate
        elif event.delta.type == "thinking_delta":
            thinking_output += event.delta.thinking
    elif event.type == "message_stop":
        break

print(full_response)
# Output:
# def hello_world():
#     print("Hello, World!")
#
# if __name__ == "__main__":
#     hello_world()
```

---

## 📊 Complete Request-Response Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         YOUR CLIENT                                         │
│                    (curl / Python SDK / IDE)                                │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                    STEP 1: Send Request
                  (model: "claude-3-5-sonnet")
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        PROXY SERVER (localhost:5000)                         │
│                     [api/routes.py /v1/messages]                            │
│                                                                              │
│  STEP 2: Auth Check [api/dependencies.py]                                   │
│  ✅ Authorization: Bearer freecc validated                                  │
│                                                                              │
│  STEP 3: Model Router [api/model_router.py]                                 │
│  "claude-3-5-sonnet" → Env mapping → MODEL="opencode/qwen3.6-plus"         │
│  Decision: provider_id="opencode"                                            │
│                                                                              │
│  STEP 4: Provider Resolution [api/dependencies.py]                          │
│  Instantiate: OpenCodeProvider(api_key="sk-...", base="opencode.ai/zen...")│
│                                                                              │
│  STEP 5: Request Conversion [providers/opencode/request.py]                 │
│  Anthropic format → OpenAI ChatCompletions format                           │
│  ✅ System prompts converted                                                │
│  ✅ Thinking enabled                                                        │
│  ✅ Messages reformatted                                                    │
│                                                                              │
│  STEP 6: HTTP Request [providers/opencode/client.py]                        │
│  POST to: https://opencode.ai/zen/v1/chat/completions                     │
│  Headers: Authorization: Bearer sk-...                                      │
│  Body: {model: "qwen3.6-plus", messages: [...], stream: true}             │
│                                                                              │
│  STEP 7-8: Streaming & Normalization                                        │
│  OpenCode streams SSE → Normalize to Anthropic format                       │
│  [core/anthropic/sse.py]                                                    │
│                                                                              │
│  STEP 9: Stream Back [api/services.py]                                      │
│  Return: StreamingResponse(event_stream, media_type="text/event-stream")   │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               │
                    STEP 10: Receive Stream
                  (Anthropic SSE format)
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                      OPENCODE/ALIBABA API                                    │
│              https://opencode.ai/zen/v1/chat/completions                    │
│                                                                              │
│  Receives: {model: "qwen3.6-plus", messages, thinking: enabled}            │
│  Routes to: Alibaba Qwen 3.6 Plus (via OpenCode gateway)                   │
│  Generates: Response with reasoning/thinking                                │
│  Returns: SSE stream with text + reasoning deltas + token counts           │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 🎯 Token Tracking

**File:** [api/services.py](api/services.py) → `_stream_and_track_tokens()`

```python
async def _stream_and_track_tokens(streamed, provider_id, model_id):
    """Track tokens while streaming"""
    
    output_tokens = 0
    
    async for chunk in streamed:
        # Extract token count from message_delta events
        if '"message_delta"' in chunk and '"output_tokens"' in chunk:
            # Parse JSON to get output_tokens
            output_tokens = extract_tokens(chunk)  # e.g., 47
        
        yield chunk  # Pass through to client
    
    # After stream completes
    tracker = get_token_tracker()
    tracker.add_tokens(
        provider_id="opencode",
        model_id="qwen3.6-plus",
        input_tokens=15,
        output_tokens=47
    )
```

**Where tokens are stored:** [core/token_tracking.py](core/token_tracking.py)

---

## 🔍 Logging & Debugging

**Enable verbose logs to see the entire flow:**

```bash
# In .env:
LOG_RAW_API_PAYLOADS=true       # Log all HTTP payloads
LOG_RAW_SSE_EVENTS=true         # Log all SSE events
LOG_API_ERROR_TRACEBACKS=true   # Log full exceptions

# Then run:
fcc-server

# Tail logs:
tail -f ~/.cache/freecc/logs/server.log | grep -i "OPENCODE\|MODEL\|REQUEST\|CONVERSION"
```

**Expected log sequence:**

```
01:23:45.123 | INFO     | routes:post_messages:125 - /v1/messages request_id=abc123
01:23:45.124 | DEBUG    | model_router:resolve:45 - MODEL MAPPING: 'claude-3-5-sonnet' -> 'opencode/qwen3.6-plus'
01:23:45.125 | INFO     | dependencies:resolve_provider:89 - Provider initialized: opencode
01:23:45.126 | DEBUG    | request:build_request_body:32 - OPENCODE_REQUEST: conversion start model=qwen3.6-plus msgs=1
01:23:45.127 | DEBUG    | openai_compat:stream_response:156 - POST https://opencode.ai/zen/v1/chat/completions
01:23:45.500 | DEBUG    | sse:parse_openai_event:72 - SSE event: content_block_delta text_delta: "def"
01:23:45.502 | DEBUG    | sse:parse_openai_event:72 - SSE event: content_block_delta text_delta: " hello_world"
01:23:46.200 | DEBUG    | sse:parse_openai_event:72 - SSE event: message_delta output_tokens: 47
01:23:46.205 | INFO     | token_tracking:add_tokens:45 - opencode/qwen3.6-plus: +47 output tokens
01:23:46.210 | INFO     | routes:post_messages:189 - /v1/messages complete_ms=1087
```

---

## 🔐 Security & Rate Limiting

### Authentication
```
Header: Authorization: Bearer freecc
  ↓
Validated in api/dependencies.py
  ↓
Matches ANTHROPIC_AUTH_TOKEN="freecc" from .env
  ↓
✅ Request continues
❌ If wrong token → HTTP 401 Unauthorized
```

### Rate Limiting (Per Request)
```
Incoming request
  ↓
Check: PROVIDER_RATE_LIMIT=1 (1 req per window)
Check: PROVIDER_RATE_WINDOW=3 (3 seconds)
Check: PROVIDER_MAX_CONCURRENCY=5 (max 5 simultaneous)
  ↓
If exceeded → HTTP 429 Too Many Requests
```

### OpenCode API Key Security
```
OPENCODE_API_KEY="sk-..." (in .env, NOT in code)
  ↓
Loaded at startup into settings.opencode_api_key
  ↓
Injected into OpenCodeProvider instance
  ↓
Used only in Authorization header for OpenCode requests
  ↓
NOT logged unless LOG_RAW_API_PAYLOADS=true (and redacted)
```

---

## 📍 Summary: Request Path

```
You → Request
  ↓
Proxy:
  1. Check auth (/api/dependencies.py)
  2. Route model (/api/model_router.py)
  3. Get provider (/api/dependencies.py)
  4. Convert request (/providers/opencode/request.py)
  5. Call OpenCode API (/providers/opencode/client.py)
  6. Normalize response (/core/anthropic/sse.py)
  7. Stream back (/api/services.py)
  ↓
You ← Response (Anthropic SSE format)
```

**Total latency:** ~500ms-2s (depending on OpenCode's response time)

**Token costs:** Billed by OpenCode ($0.50/$3.00 per 1M tokens)
