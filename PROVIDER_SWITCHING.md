# Provider Switching During Session: Exact Steps

**TL;DR:** Change the `model` field in your request. That's it. No proxy restart needed.

---

## Method 1: Direct Model Specification (Recommended)

### Step 1️⃣: Identify Provider & Model Name

Check [config/provider_catalog.py](config/provider_catalog.py) for available providers:

```python
# All supported providers:
"nvidia_nim"    # NVIDIA NIM (Deepseek, Nemotron, etc.)
"opencode"      # OpenCode Zen (Qwen, GPT, Claude, etc.)
"openrouter"    # OpenRouter (Anthropic-native)
"deepseek"      # DeepSeek direct (Anthropic-native)
"kimi"          # Kimi/Moonshot (OpenAI-compat)
"wafer"         # Wafer (Anthropic-native)
"zai"           # Z.ai (OpenAI-compat)
"lmstudio"      # LM Studio (local)
"llamacpp"      # Llama.cpp (local)
"ollama"        # Ollama (local)
```

### Step 2️⃣: Format Model String

Use format: `provider_id/model_name`

```python
# OpenCode Zen (current)
"opencode/qwen3.6-plus"

# NVIDIA NIM Deepseek
"nvidia_nim/deepseek-ai/deepseek-v4-flash"

# NVIDIA NIM Nemotron
"nvidia_nim/nvidia/nemotron-3-super-120b-a12b"

# OpenRouter Claude
"open_router/anthropic/claude-3-5-sonnet-20241022"

# DeepSeek native
"deepseek/deepseek-v4"

# Kimi
"kimi/moonshot-v1-auto"

# Local LM Studio
"lmstudio/your-local-model"
```

### Step 3️⃣: Send Request with New Model

**Option A: Using cURL**

```bash
# Request 1: OpenCode (Alibaba Qwen Free)
curl -X POST http://localhost:5000/v1/messages \
  -H "Authorization: Bearer freecc" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opencode/qwen3.6-plus",
    "messages": [
      {"role": "user", "content": "Write a hello world function"}
    ]
  }' \
  -N  # -N shows streaming in real-time

# Wait for response to complete...

# Request 2: Switch to NIM Deepseek (same terminal)
curl -X POST http://localhost:5000/v1/messages \
  -H "Authorization: Bearer freecc" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nvidia_nim/deepseek-ai/deepseek-v4-flash",
    "messages": [
      {"role": "user", "content": "Now using NIM. What is 2+2?"}
    ]
  }' \
  -N

# Request 3: Switch to OpenRouter
curl -X POST http://localhost:5000/v1/messages \
  -H "Authorization: Bearer freecc" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "open_router/anthropic/claude-3-5-sonnet-20241022",
    "messages": [
      {"role": "user", "content": "Now using OpenRouter"}
    ]
  }' \
  -N
```

**What happens:**
- Request 1 → Proxy routes to OpenCode → Qwen 3.6 Plus answers
- ✅ Response complete
- Request 2 → Proxy recognizes new provider → Routes to NIM → Deepseek answers
- ✅ Response complete
- Request 3 → Proxy switches again → Routes to OpenRouter → Claude answers
- ✅ Response complete

**No restart needed. No configuration change needed.**

---

**Option B: Using Python SDK**

```python
from anthropic import Anthropic

# Initialize client (once!)
client = Anthropic(
    base_url="http://localhost:5000",
    api_key="freecc"
)

# ═════════════════════════════════════════════════════════════════
# REQUEST 1: OpenCode Qwen
# ═════════════════════════════════════════════════════════════════
print("=" * 60)
print("Request 1: OpenCode (Qwen 3.6 Plus)")
print("=" * 60)

response1 = client.messages.create(
    model="opencode/qwen3.6-plus",  # ← CHANGE THIS to switch
    messages=[
        {"role": "user", "content": "Write a hello world function in Python"}
    ],
    max_tokens=500
)

print(response1.content[0].text)
print(f"Provider: OpenCode | Model: qwen3.6-plus | Tokens: {response1.usage.output_tokens}")

# ═════════════════════════════════════════════════════════════════
# REQUEST 2: NIM Deepseek (SWITCH PROVIDER)
# ═════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("Request 2: NIM Deepseek V4 Flash")
print("=" * 60)

response2 = client.messages.create(
    model="nvidia_nim/deepseek-ai/deepseek-v4-flash",  # ← SWITCHED!
    messages=[
        {"role": "user", "content": "What is the capital of France?"}
    ],
    max_tokens=500
)

print(response2.content[0].text)
print(f"Provider: NIM | Model: deepseek-v4-flash | Tokens: {response2.usage.output_tokens}")

# ═════════════════════════════════════════════════════════════════
# REQUEST 3: OpenRouter (SWITCH AGAIN)
# ═════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("Request 3: OpenRouter Claude")
print("=" * 60)

response3 = client.messages.create(
    model="open_router/anthropic/claude-3-5-sonnet-20241022",  # ← SWITCHED AGAIN!
    messages=[
        {"role": "user", "content": "Explain quantum computing in simple terms"}
    ],
    max_tokens=500
)

print(response3.content[0].text)
print(f"Provider: OpenRouter | Model: claude-3-5-sonnet | Tokens: {response3.usage.output_tokens}")

# ═════════════════════════════════════════════════════════════════
# REQUEST 4: Back to OpenCode
# ═════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("Request 4: Back to OpenCode (Switch Again)")
print("=" * 60)

response4 = client.messages.create(
    model="opencode/qwen3.6-plus",  # ← BACK TO OPENCODE
    messages=[
        {"role": "user", "content": "Write a sorting algorithm"}
    ],
    max_tokens=500
)

print(response4.content[0].text)
print(f"Provider: OpenCode | Model: qwen3.6-plus | Tokens: {response4.usage.output_tokens}")

# ═════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("SUMMARY: Switched Providers 3 Times in Same Session ✅")
print("=" * 60)
print("Request 1: OpenCode")
print("Request 2: NIM Deepseek (SWITCHED)")
print("Request 3: OpenRouter (SWITCHED)")
print("Request 4: OpenCode (SWITCHED)")
print("\nNo proxy restart. No config change. Seamless switching.")
```

**Console output:**
```
============================================================
Request 1: OpenCode (Qwen 3.6 Plus)
============================================================
def hello_world():
    print("Hello, World!")

if __name__ == "__main__":
    hello_world()

Provider: OpenCode | Model: qwen3.6-plus | Tokens: 31

============================================================
Request 2: NIM Deepseek V4 Flash
============================================================
The capital of France is Paris.

Provider: NIM | Model: deepseek-v4-flash | Tokens: 12

============================================================
Request 3: OpenRouter Claude
============================================================
Quantum computing harnesses the principles of quantum mechanics...

Provider: OpenRouter | Model: claude-3-5-sonnet | Tokens: 147

============================================================
Request 4: Back to OpenCode (Switch Again)
============================================================
def bubble_sort(arr):
    n = len(arr)
    for i in range(n):
        for j in range(0, n-i-1):
            if arr[j] > arr[j+1]:
                arr[j], arr[j+1] = arr[j+1], arr[j]
    return arr

Provider: OpenCode | Model: qwen3.6-plus | Tokens: 65

============================================================
SUMMARY: Switched Providers 3 Times in Same Session ✅
============================================================
Request 1: OpenCode
Request 2: NIM Deepseek (SWITCHED)
Request 3: OpenRouter (SWITCHED)
Request 4: OpenCode (SWITCHED)

No proxy restart. No config change. Seamless switching.
```

---

**Option C: Using Node.js SDK**

```javascript
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic({
  baseURL: "http://localhost:5000",
  apiKey: "freecc",
});

async function switchProviders() {
  // Request 1: OpenCode
  console.log("=".repeat(60));
  console.log("Request 1: OpenCode (Qwen 3.6 Plus)");
  console.log("=".repeat(60));

  const response1 = await client.messages.create({
    model: "opencode/qwen3.6-plus", // ← CHANGE THIS to switch
    messages: [
      {
        role: "user",
        content: "Write a hello world function in JavaScript",
      },
    ],
    max_tokens: 500,
  });

  console.log(response1.content[0].type === "text" ? response1.content[0].text : "");
  console.log(`Provider: OpenCode | Tokens: ${response1.usage.output_tokens}\n`);

  // Request 2: NIM Deepseek (SWITCH)
  console.log("=".repeat(60));
  console.log("Request 2: NIM Deepseek V4 Flash");
  console.log("=".repeat(60));

  const response2 = await client.messages.create({
    model: "nvidia_nim/deepseek-ai/deepseek-v4-flash", // ← SWITCHED!
    messages: [
      {
        role: "user",
        content: "What is 5 * 7?",
      },
    ],
    max_tokens: 500,
  });

  console.log(response2.content[0].type === "text" ? response2.content[0].text : "");
  console.log(`Provider: NIM | Tokens: ${response2.usage.output_tokens}\n`);

  // Request 3: OpenRouter (SWITCH AGAIN)
  console.log("=".repeat(60));
  console.log("Request 3: OpenRouter Claude");
  console.log("=".repeat(60));

  const response3 = await client.messages.create({
    model: "open_router/anthropic/claude-3-5-sonnet-20241022", // ← SWITCHED AGAIN!
    messages: [
      {
        role: "user",
        content: "Explain async/await",
      },
    ],
    max_tokens: 500,
  });

  console.log(response3.content[0].type === "text" ? response3.content[0].text : "");
  console.log(`Provider: OpenRouter | Tokens: ${response3.usage.output_tokens}\n`);

  console.log("=".repeat(60));
  console.log("✅ Switched 3 providers without restart!");
  console.log("=".repeat(60));
}

switchProviders().catch(console.error);
```

---

## Method 2: Gateway Model ID Format (For IDE Discovery)

Use format: `anthropic/provider_id/model_name`

This allows Claude Code IDE to auto-discover the model as available:

```python
# Standard format
model="opencode/qwen3.6-plus"

# Gateway format (discovered by Claude Code)
model="anthropic/opencode/qwen3.6-plus"

# Disable thinking on-the-fly
model="claude-3-freecc-no-thinking/opencode/qwen3.6-plus"
```

**They're equivalent for routing. Gateway format is just IDE-friendly.**

---

## Method 3: Environment Variable (Requires Restart)

⚠️ This requires a proxy restart, so NOT recommended for session switching.

```bash
# Edit .env
MODEL="nvidia_nim/deepseek-ai/deepseek-v4-flash"

# Restart proxy
fcc-server

# Now all requests without explicit model use NIM
```

---

## What Changes & What Doesn't

### ✅ Changes Per Request
```
Request 1: model="opencode/qwen3.6-plus"        → OpenCode processes
Request 2: model="nvidia_nim/deepseek-v4-flash" → NIM processes
Request 3: model="open_router/..."              → OpenRouter processes
```

### ✅ Stays the Same
```
✅ Client connection (same websocket/HTTP connection)
✅ API endpoint (localhost:5000/v1/messages)
✅ Auth token (same "Bearer freecc")
✅ Conversation context (continues from previous request)
✅ Proxy (no restart needed)
✅ Rate limits (global + per-provider)
```

---

## Real-World Examples

### Example 1: A/B Testing Responses

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://localhost:5000", api_key="freecc")

prompt = "Explain machine learning in 100 words"

print("Testing 3 providers on same prompt:\n")

providers = [
    ("OpenCode", "opencode/qwen3.6-plus"),
    ("NIM Deepseek", "nvidia_nim/deepseek-ai/deepseek-v4-flash"),
    ("OpenRouter", "open_router/anthropic/claude-3-5-sonnet-20241022"),
]

for name, model in providers:
    response = client.messages.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150
    )
    
    print(f"[{name}]")
    print(f"Response: {response.content[0].text}")
    print(f"Tokens: {response.usage.output_tokens}")
    print("-" * 60)
```

### Example 2: Cost Optimization

```python
# Use cheap provider first, upgrade if needed
client = Anthropic(base_url="http://localhost:5000", api_key="freecc")

user_query = "Write production-grade code for a REST API"

# Step 1: Try cheaper model first
print("Step 1: Testing with OpenCode Qwen (cheap)...")
response_cheap = client.messages.create(
    model="opencode/qwen3.6-plus",
    messages=[{"role": "user", "content": user_query}],
    max_tokens=1000
)

# Step 2: If output is good, use it. If not, upgrade.
if "TODO" in response_cheap.content[0].text or "?" in response_cheap.content[0].text:
    print("Step 2: Response was incomplete. Upgrading to Claude...")
    response_good = client.messages.create(
        model="open_router/anthropic/claude-3-5-sonnet-20241022",
        messages=[{"role": "user", "content": user_query}],
        max_tokens=1000
    )
    result = response_good.content[0].text
else:
    result = response_cheap.content[0].text

print(f"Final result:\n{result}")
```

### Example 3: Parallel Processing

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic(
    base_url="http://localhost:5000",
    api_key="freecc"
)

async def get_response(model, prompt):
    response = await client.messages.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200
    )
    return response.content[0].text

async def main():
    prompt = "Write 5 bullet points about Python"
    
    # Run all 3 providers in parallel
    results = await asyncio.gather(
        get_response("opencode/qwen3.6-plus", prompt),
        get_response("nvidia_nim/deepseek-ai/deepseek-v4-flash", prompt),
        get_response("open_router/anthropic/claude-3-5-sonnet-20241022", prompt),
    )
    
    for i, (name, result) in enumerate([
        ("OpenCode", results[0]),
        ("NIM", results[1]),
        ("OpenRouter", results[2]),
    ], 1):
        print(f"\n[{i}] {name}:\n{result}")

asyncio.run(main())
```

---

## Exact Provider/Model Reference Table

| Provider | Model ID | Format | Example |
|----------|----------|--------|---------|
| OpenCode | `qwen3.6-plus` | `opencode/qwen3.6-plus` | ✅ Your current |
| OpenCode | `qwen3.5-plus` | `opencode/qwen3.5-plus` | Cheaper |
| NIM | Deepseek V4 Flash | `nvidia_nim/deepseek-ai/deepseek-v4-flash` | Fast |
| NIM | Nemotron 3 Super | `nvidia_nim/nvidia/nemotron-3-super-120b-a12b` | Powerful |
| OpenRouter | Claude 3.5 | `open_router/anthropic/claude-3-5-sonnet-20241022` | Best |
| DeepSeek | V4 | `deepseek/deepseek-v4` | Native Anthropic |
| Kimi | Auto | `kimi/moonshot-v1-auto` | Chinese |
| Wafer | Chat | `wafer/api/messages` | Anthropic-native |

---

## Troubleshooting Provider Switches

### ❌ Problem: "Unknown provider_type: 'xyz'"

**Cause:** You typo'd the provider name

**Fix:**
```python
# ❌ Wrong
model="open_code/qwen3.6-plus"

# ✅ Correct
model="opencode/qwen3.6-plus"  # Note: open + code = "opencode"
```

### ❌ Problem: "401 Unauthorized" on NIM request

**Cause:** NVIDIA_NIM_API_KEY not set in .env

**Fix:**
```bash
# Check .env has:
NVIDIA_NIM_API_KEY1="nvapi-..."  # or just NVIDIA_NIM_API_KEY=""
```

### ❌ Problem: "503 Provider initialization failed"

**Cause:** Missing API key for the provider

**Fix:**
```bash
# For OpenCode
OPENCODE_API_KEY="sk-..."

# For OpenRouter
OPENROUTER_API_KEY="sk-..."

# For DeepSeek
DEEPSEEK_API_KEY="sk-..."
```

### ❌ Problem: "429 Too Many Requests"

**Cause:** Rate limit hit

**Fix:**
```bash
# .env - be more lenient
PROVIDER_RATE_LIMIT=5  # was 1
PROVIDER_RATE_WINDOW=10  # was 3
```

---

## Summary: Exact Steps to Switch

**Minimum steps:**

1. ✅ **Proxy running:** `fcc-server` (terminal 1)
2. ✅ **Create request:** Change `model` field to new provider
3. ✅ **Send request:** API call with new model
4. ✅ **Receive response:** Proxy routes to new provider

**That's it. No restart. No config change.**

```python
# One line change to switch:
client.messages.create(
    model="opencode/qwen3.6-plus",  # ← CHANGE THIS
    messages=[...]
)
```

Done! 🎯
