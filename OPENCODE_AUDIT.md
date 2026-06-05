# OpenCode Zen API Integration & Multi-Provider Architecture Audit

**Created:** 2026-05-31  
**Status:** ✅ Configuration Complete  
**Provider:** OpenCode Zen (Qwen 3.6+ Free)

---

## 1. Configuration Summary

### 1.1 Changes Made
- ✅ Added `OPENCODE_API_KEY` to `.env` (configured with provided credentials)
- ✅ Set default model to `opencode/qwen-plus` (free tier)
- ✅ NIM configuration unchanged (multiple numbered keys preserved)
- ✅ All other providers remain available

### 1.2 Current Setup

```env
# OpenCode Zen Config (OpenAI-compatible Chat Completions at opencode.ai/zen/v1)
OPENCODE_API_KEY="sk-..."

# Default model (routes to OpenCode Zen Qwen 3.6+ Free)
MODEL="opencode/qwen-plus"
```

**No code changes needed** — OpenCode is already fully integrated in the provider catalog.

---

## 2. Multi-Provider Architecture

### 2.1 How the Proxy Works

When you run the proxy, it acts as an **Anthropic Messages API-compatible gateway** that can route to ANY configured provider. Here's the request flow:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Client (Claude Code, VS Code, curl, etc.)                         │
│  POST /v1/messages                                                  │
│  { "model": "claude-3-5-sonnet", "messages": [...] }              │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                    Routes to FastAPI
                           │
                           ▼
        ┌──────────────────────────────────┐
        │  Model Router (core logic)       │
        │  "What provider runs this model?"│
        └──────────────────┬───────────────┘
                           │
        ┌──────────────────┴───────────────┬────────────────┐
        │                                  │                │
        ▼                                  ▼                ▼
    NIM (nvidia/    OpenCode (qwen-    DeepSeek (native
    nemotron)       plus)               Anthropic)
        │                │                │
        ├─ /chat/        ├─ /chat/        ├─ /v1/messages
        │  completions   │  completions   │  (Anthropic format)
        │  (OpenAI fmt)  │  (OpenAI fmt)  │
        │                │                │
        └────────┬───────┴────────┬───────┘
                 │                │
        ┌────────▼────────┐       │
        │ Anthropic→OpenAI│       │
        │ Conversion      │       │
        │ (system msg,    │       │ (no conversion)
        │  tools, etc.)   │       │
        └────────┬────────┘       │
                 │                │
        ┌────────┴────────────────▼────────────┐
        │  Upstream Provider API Response      │
        │  (SSE stream with "message_delta"...)│
        └────────┬───────────────────────────┘
                 │ (normalize to Anthropic format)
                 ▼
        ┌────────────────────────────────┐
        │  Client receives SSE stream    │
        │  Anthropic-compatible response │
        └────────────────────────────────┘
```

### 2.2 Provider Types in the Proxy

The system manages **two categories** of providers:

#### **OpenAI-Compatible Providers** (use `/chat/completions`)
- **NIM** (nvidia_nim)
- **OpenCode Zen** (opencode) ← **YOUR NEW PROVIDER**
- **Z.ai** (zai)
- Kimi (kimi)

**Request Conversion Flow:**
1. Receive Anthropic `MessagesRequest`
2. Convert to OpenAI format via `AnthropicToOpenAIConverter`
3. System message → role=system message in array
4. User/assistant messages → role=user/assistant
5. Tools/thinking preserved
6. Stream response back as Anthropic SSE

**Files:**
- `core/anthropic/conversion.py` — Anthropic→OpenAI conversion logic
- `providers/opencode/request.py` — OpenCode-specific request builder

#### **Anthropic-Native Providers** (use `/v1/messages`)
- **DeepSeek** (deepseek)
- **OpenRouter** (open_router)
- **Wafer** (wafer)
- **Z.ai native** (separate from OpenAI-compat mode)

**Request Flow:**
1. Receive Anthropic `MessagesRequest`
2. Keep native format (system in top-level field)
3. No conversion needed
4. Stream Anthropic SSE directly

---

## 3. Model Routing: How the Proxy Decides Which Provider to Use

### 3.1 Routing Resolution Order

When a request arrives with a `model` field like `"claude-3-5-sonnet"`, the proxy resolves it in this order:

```python
ModelRouter.resolve(claude_model_name) → ResolvedModel
```

**Step 1: Gateway Model ID Decoding**  
Check if the model matches patterns:
- `anthropic/provider_id/model_name` → decode to `(provider_id, model_name, thinking=None)`
- `claude-3-freecc-no-thinking/provider_id/model_name` → decode with `thinking=False`

```python
# Example: request arrives with model="anthropic/opencode/qwen-plus"
decoded = decode_gateway_model_id("anthropic/opencode/qwen-plus")
# Returns: DecodedGatewayModelId(
#   provider_id="opencode",
#   provider_model="qwen-plus",
#   force_thinking_enabled=None
# )
```

**Step 2: Direct Provider/Model Format**  
If Gateway ID doesn't match, check for direct format: `provider_id/model_name`

```python
# Example: model="opencode/qwen-plus"
provider_id, sep, model_name = "opencode/qwen-plus".partition("/")
# provider_id = "opencode"
# model_name = "qwen-plus"
```

**Step 3: Environment Variable Mapping** (if model name doesn't have "/" separator)  
Look up the model in settings:

```python
# If model="claude-3-5-sonnet", check:
# 1. Direct override: MODEL_SONNET env var
# 2. If not found: MODEL env var (fallback)
# 3. If not found: raise error
```

### 3.2 Routing Examples

```
CLIENT REQUEST                          RESOLVED PROVIDER        ACTUAL UPSTREAM MODEL
─────────────────────────────────────   ──────────────────────   ──────────────────────
model: "opencode/qwen-plus"         →   opencode / qwen-plus     qwen-plus
model: "anthropic/opencode/qwen-plus"   →   opencode / qwen-plus     qwen-plus
model: "nvidia_nim/nemotron-3-super"→   nvidia_nim / nemotron...  nemotron-3-super-120b-a12b
model: "claude-3-5-sonnet"          →   (check MODEL_SONNET)     (from env or fallback)
model: "claude-opus-4"              →   (check MODEL_OPUS)       (from env or fallback)
```

### 3.3 Routing Code Location

**[api/model_router.py](api/model_router.py)** — Main routing logic
```python
class ModelRouter:
    def resolve(self, claude_model_name: str) -> ResolvedModel:
        # Step 1: Try gateway model ID decoding
        # Step 2: Try direct provider/model format
        # Step 3: Fall back to settings mapping
        ...
```

**[config/settings.py](config/settings.py)** — Environment variable resolution
```python
def resolve_model(self, model_name: str) -> str:
    # Lookup MODEL_OPUS, MODEL_SONNET, MODEL_HAIKU, or MODEL...
```

**[api/gateway_model_ids.py](api/gateway_model_ids.py)** — Gateway ID encoding/decoding
```python
def decode_gateway_model_id(model_name: str) -> DecodedGatewayModelId | None:
    # Decode anthropic/provider_id/model_name patterns
```

---

## 4. Changing Models During a Session

### 4.1 Three Ways to Switch Models

#### **Method 1: Direct Model ID in Request** (Recommended for Runtime Switching)
Send the model explicitly in the request:

```bash
# Switch to NIM Deepseek
curl -X POST http://localhost:5000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia_nim/deepseek-ai/deepseek-v4-flash",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# Switch to OpenCode Qwen Free
curl -X POST http://localhost:5000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opencode/qwen-plus",
    "messages": [{"role": "user", "content": "Hello"}]
  }'

# Switch to OpenRouter with native Anthropic
curl -X POST http://localhost:5000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "open_router/anthropic/claude-3-5-sonnet-20241022",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

**Pros:**
- ✅ No restart needed
- ✅ Per-request granularity
- ✅ Full provider/model control
- ✅ Best for A/B testing

**Cons:**
- ⚠️ Client must support model selection
- ⚠️ Verbose for long API calls

**SDK Usage (Python/Node/etc):**
```python
from anthropic import Anthropic
client = Anthropic(base_url="http://localhost:5000", api_key="freecc")

# Switch providers mid-session
response = client.messages.create(
    model="nvidia_nim/deepseek-ai/deepseek-v4-flash",  # ← THIS REQUEST uses NIM
    messages=[{"role": "user", "content": "Hello"}]
)

response2 = client.messages.create(
    model="opencode/qwen-plus",  # ← THIS REQUEST uses OpenCode (different session)
    messages=[{"role": "user", "content": "Hi"}]
)
```

#### **Method 2: Gateway Model ID Format** (For API Discovery)
Use the `anthropic/provider_id/model_name` format for auto-discovery:

```python
# Claude Code IDE recognizes these as available models
response = client.messages.create(
    model="anthropic/opencode/qwen-plus",
    messages=[...]
)

# Disable thinking on-the-fly
response = client.messages.create(
    model="claude-3-freecc-no-thinking/opencode/qwen-plus",
    messages=[...]
)
```

**Pros:**
- ✅ Auto-discovered by Claude Code IDE
- ✅ Can override thinking per model
- ✅ Gateway-safe encoding

**Router Documentation:** [api/gateway_model_ids.py](api/gateway_model_ids.py)

#### **Method 3: Environment Variable Mapping** (Global Default)
Edit `.env` and restart the proxy:

```env
# This becomes the default for ALL "claude-3-5-sonnet" requests
MODEL_SONNET="opencode/qwen-plus"

# This is the fallback for unmapped Claude model names
MODEL="opencode/qwen-plus"
```

Then restart:
```bash
fcc-server  # Reloads .env
```

**Pros:**
- ✅ Clean, simple global default
- ✅ No per-request overhead

**Cons:**
- ⚠️ Requires restart to change
- ⚠️ All requests use same provider (no mixing)
- ⚠️ Less granular control

---

## 5. Managing Multiple Providers Simultaneously

The proxy natively supports **concurrent use of multiple providers**. Here's how:

### 5.1 Architecture Support

The `ClaudeProxyService` has a **provider getter** that resolves the provider per-request:

```python
class ClaudeProxyService:
    def __init__(self, ..., provider_getter: ProviderGetter, ...):
        # provider_getter resolves provider_type → actual provider instance
        self._provider_getter = provider_getter
    
    def create_message(self, request_data: MessagesRequest):
        # 1. Parse the model field to determine provider
        routed = self._model_router.resolve_messages_request(request_data)
        
        # 2. Get the specific provider instance
        provider = self._provider_getter(routed.resolved.provider_id)
        
        # 3. Route request to that provider's adapter
        return provider.create_message(routed.request, ...)
```

**[api/services.py](api/services.py)** — Service coordination  
**[api/dependencies.py](api/dependencies.py)** — Provider resolution

### 5.2 Concurrent Provider Usage

Multiple clients can use different providers **in parallel**:

```
Client A (using NIM)          Client B (using OpenCode)
        │                              │
        ├─ model: nvidia_nim/...      ├─ model: opencode/qwen-plus
        │                              │
        └─────────┬──────────┬─────────┘
                  │          │
        ┌─────────▼┐    ┌────▼──────────┐
        │ NIM API  │    │ OpenCode API  │
        │ (Qwen3)  │    │ (Qwen Free)   │
        └─────────┬┘    └────┬──────────┘
                  │          │
        ┌─────────▼──────────▼───────────┐
        │ Responses merged & normalized  │
        │ (both as Anthropic SSE)        │
        └───────────────────────────────┘
```

### 5.3 Rate Limiting Per Provider

Each provider has isolated rate limit tracking:

```env
# Global rate limit (applies to all providers)
PROVIDER_RATE_LIMIT=1       # 1 request max
PROVIDER_RATE_WINDOW=3      # per 3 seconds
PROVIDER_MAX_CONCURRENCY=5  # 5 concurrent requests

# NIM-specific (numbered keys rotate automatically)
NVIDIA_NIM_RPM_PER_KEY=35       # 35 requests per minute per NIM key
NVIDIA_NIM_KEY_WINDOW_SEC=60    # 60 second sliding window
NVIDIA_NIM_KEY_COOLDOWN_SEC=65  # Cool off after hitting limit
NVIDIA_NIM_KEY_SWITCH_DELAY_SEC=5  # Delay before switching keys
```

**Files:**
- [core/rate_limit.py](core/rate_limit.py) — Rate limit tracking
- [providers/rate_limit.py](providers/rate_limit.py) — Provider-specific limits
- [providers/base.py](providers/base.py) — Base provider with rate limit hooks

---

## 6. How It Works: From Request to Response

### 6.1 Complete Request Flow for OpenCode

```
1. REQUEST ARRIVES
   POST /v1/messages
   {
     "model": "claude-3-5-sonnet",
     "messages": [{"role": "user", "content": "Write code"}],
     "system": "You are helpful"
   }

2. DEPENDENCY INJECTION
   → get_proxy_service() called with request context
   → AppRuntime provides app.state.provider_registry

3. MODEL ROUTING
   → ModelRouter.resolve("claude-3-5-sonnet")
   → Check: is it provider_id/model format? NO
   → Check: is it gateway ID? NO
   → Lookup: settings.resolve_model("claude-3-5-sonnet")
   → Found: MODEL="opencode/qwen-plus"
   → Returns: ResolvedModel(
       provider_id="opencode",
       provider_model="qwen-plus",
       thinking_enabled=true
     )

4. PROVIDER RESOLUTION
   → resolve_provider("opencode", app, settings)
   → app.state.provider_registry.get("opencode", settings)
   → Instantiates OpenCodeProvider with:
      - API key from OPENCODE_API_KEY env
      - Base URL from config/provider_catalog.py
      - Rate limiter instance

5. REQUEST BUILDING
   → providers/opencode/request.py : build_request_body()
   → Converts requests.model = "qwen-plus"
   → Calls core/anthropic/conversion.py : build_base_request_body()
     - System prompt → role=system message
     - Messages converted to OpenAI format
     - Tools converted to OpenAI format
   → Returns OpenAI /chat/completions body

6. UPSTREAM CALL
   → POST https://opencode.ai/zen/v1/chat/completions
   → Headers: Authorization: Bearer sk-...
   → Body: {
       "model": "qwen-plus",
       "messages": [...OpenAI format...],
       "stream": true
     }

7. STREAM RESPONSE
   → OpenCode returns SSE stream
   → Events: message_start, content_block_start, delta, message_delta...
   → Proxy normalizes to Anthropic SSE format
   → Tracks tokens from message_delta events

8. CLIENT RESPONSE
   → SSE stream sent back as Anthropic format
   → Content blocks parsed, tokens counted
   → Session continues with same provider until model changes
```

### 6.2 Key Code Paths

| Step | File | Function |
|------|------|----------|
| 2 | `api/routes.py` | `get_proxy_service()` |
| 3 | `api/model_router.py` | `ModelRouter.resolve()` |
| 4 | `api/dependencies.py` | `resolve_provider()` |
| 5 | `providers/opencode/request.py` | `build_request_body()` |
| 5b | `core/anthropic/conversion.py` | `build_base_request_body()` |
| 6 | `providers/opencode/client.py` | `OpenCodeProvider.create_message()` |
| 7 | `core/anthropic/sse.py` | SSE event normalization |

---

## 7. Supported OpenCode Models

OpenCode Zen provides free access to Qwen 3.6+ and other models:

```python
# From provider_catalog.py
"opencode": ProviderDescriptor(
    provider_id="opencode",
    transport_type="openai_chat",  # Uses /chat/completions
    credential_env="OPENCODE_API_KEY",
    credential_url="https://opencode.ai/auth",
    credential_attr="opencode_api_key",
    default_base_url="https://opencode.ai/zen/v1",
    capabilities=("chat", "streaming", "tools", "thinking", "rate_limit"),
)
```

**Available Models (as of 2026-05):**
- `qwen-plus` — Qwen 3.6+ Free (recommended)
- `qwen-turbo` — Qwen 3.6+ Pro
- Other models may be available via OpenCode API

**Request Format:**
```python
# All requests use format: opencode/model_name
model: "opencode/qwen-plus"
model: "anthropic/opencode/qwen-plus"  # Gateway-safe format
```

---

## 8. Troubleshooting

### Issue: "Unknown provider_type: 'opencode'"
**Solution:** Check that `OPENCODE_API_KEY` is set in `.env` and restart proxy

```bash
# Verify key is loaded
fcc-server  # Should show "Provider initialized: opencode" in logs
```

### Issue: "413 Payload Too Large" from OpenCode
**Solution:** OpenCode may have stricter size limits; reduce message contexts or chunk requests

### Issue: "429 Rate Limited" from OpenCode
**Solution:** Configure rate limit settings:
```env
PROVIDER_RATE_LIMIT=1
PROVIDER_RATE_WINDOW=5      # More lenient
PROVIDER_MAX_CONCURRENCY=2  # Reduce concurrency
```

### Issue: Model not found / "Invalid model"
**Solution:** Verify model name matches OpenCode catalog
```bash
# Test with known working model
curl -X POST http://localhost:5000/v1/messages \
  -d '{"model": "opencode/qwen-plus", "messages": [...]}'
```

---

## 9. Summary: Multi-Provider Management

| Aspect | Details |
|--------|---------|
| **Providers Configured** | NIM, OpenCode, DeepSeek, OpenRouter, Kimi, Wafer, Zai, Z.ai, Local (5 total active) |
| **Request Routing** | Per-request model field → Provider ID → Provider instance |
| **Switching Models** | Direct ID (`opencode/qwen-plus`), Gateway ID, or Environment variable |
| **Concurrent Usage** | ✅ Yes — different clients can use different providers simultaneously |
| **Rate Limiting** | Per-provider + globally configurable |
| **Hot Reload** | Supported via `config/hot_reload.py` for model changes |
| **Thinking Support** | OpenCode supports thinking; enabled per-model config |

---

## 10. Next Steps

1. **Test OpenCode integration:**
   ```bash
   fcc-server  # Starts proxy on localhost:5000
   ```

2. **Test with a simple request:**
   ```bash
   curl -X POST http://localhost:5000/v1/messages \
     -H "Authorization: Bearer freecc" \
     -H "Content-Type: application/json" \
     -d '{
       "model": "opencode/qwen-plus",
       "messages": [{"role": "user", "content": "Hello!"}]
     }'
   ```

3. **Switch between NIM and OpenCode in client:**
   ```python
   # Same client, different providers
   response1 = client.messages.create(
       model="nvidia_nim/deepseek-ai/deepseek-v4-flash",
       messages=[...]
   )
   
   response2 = client.messages.create(
       model="opencode/qwen-plus",
       messages=[...]
   )
   ```

4. **Monitor logs:**
   ```bash
   tail -f ~/.cache/freecc/logs/server.log | grep -i "MODEL\|OPENCODE\|ROUTING"
   ```

---

## References

- **Multi-Provider Routing:** [api/model_router.py](api/model_router.py)
- **OpenCode Integration:** [providers/opencode/](providers/opencode/)
- **Request Conversion:** [core/anthropic/conversion.py](core/anthropic/conversion.py)
- **Provider Registry:** [providers/registry.py](providers/registry.py)
- **Configuration:** [config/provider_catalog.py](config/provider_catalog.py)
- **Settings:** [config/settings.py](config/settings.py)
