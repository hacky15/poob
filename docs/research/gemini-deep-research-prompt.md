# Gemini Deep Research Prompt: Browser-Use Without Cloud Dependencies

> Paste this prompt into Gemini Deep Research (gemini.google.com → Deep Research mode)
> to get a comprehensive report on running browser-use 0.12+ entirely locally.

---

## Prompt

I'm building an autonomous web scraping bot using the Python library **browser-use** (version 0.12.0). Currently the browser automation agent requires NVIDIA NIM cloud API (OpenAI-compatible endpoint) because browser-use enforces **grammar-based structured output** — it passes a Pydantic JSON schema to the LLM and expects the response to conform exactly.

I want to run the **entire system locally with zero cloud costs**. Here's my current setup:

- **browser-use 0.12.0** with its own `ChatOllama`, `ChatOpenAI`, `ChatGoogle` wrappers (NOT LangChain)
- **Ollama** running locally with **qwen3:8b** (8B parameter model)
- The browser-use `Agent` requires the LLM to produce output conforming to `AgentOutput` — a complex Pydantic model with nested actions, union types (`anyOf`), and `min_items` constraints
- Ollama supports `format=<json_schema>` which triggers GBNF grammar-based constrained decoding in llama.cpp

### Research Questions (investigate ALL of these thoroughly):

**1. Ollama + browser-use compatibility (current state, February 2026)**
- Does `browser_use.ChatOllama` work reliably with Ollama's grammar-based structured output as of Ollama 0.6+ and browser-use 0.12+?
- What Ollama models work best? Specifically investigate: qwen3:8b, qwen3:14b, qwen3:32b, llama3.3, mistral, deepseek-r1, phi-4, gemma3
- Are there known bugs with Ollama's `json_schema_to_grammar` for complex schemas (nested `$ref`, `anyOf`, `min_items`)? The Ollama GitHub issue #8444 documents an alphabetical ordering bug — is this fixed?
- What is the minimum model size that produces semantically correct AgentOutput (not just syntactically valid JSON)?

**2. Alternative free/unlimited LLM providers for structured output**
- Are there any **free-tier cloud APIs** (besides NVIDIA NIM's 40 RPM) that support OpenAI-compatible `response_format` with `json_schema`? Investigate: Groq, Together AI, Fireworks AI, Cerebras, Lepton AI, SambaNova, Hyperbolic, any others.
- Which of these have browser-use wrapper support (ChatGroq, ChatMistral, etc.)?
- What are their free tier limits (RPM, RPD, monthly tokens)?

**3. Running browser-use WITHOUT grammar constraints**
- Can browser-use be configured to NOT send the JSON schema and instead just prompt the LLM for JSON output? Is there a config flag, environment variable, or monkey-patch?
- If grammar constraints are removed, does browser-use have fallback parsing that extracts structured data from freeform text?
- Has anyone successfully forked or patched browser-use to work with models that don't support structured output?
- What is browser-use's `SchemaOptimizer` and can it be configured to inject the schema into the prompt instead of using `format`/`response_format`?

**4. llama.cpp server as an alternative to Ollama**
- Can llama.cpp's built-in HTTP server (`llama-server` or `llama-cpp-python`) be used as an OpenAI-compatible endpoint that supports `response_format` with `json_schema`?
- Would this bypass Ollama's grammar bugs while still providing local constrained decoding?
- How does llama.cpp's grammar implementation compare to Ollama's? Is it more reliable for complex schemas?

**5. vLLM or other local inference servers**
- Does **vLLM** support structured output / guided decoding with JSON schemas? Can it serve as an OpenAI-compatible endpoint for browser-use's `ChatOpenAI`?
- What about **TGI** (Text Generation Inference), **SGLang**, **Aphrodite**, or **LocalAI**?
- Which local inference server has the most robust JSON schema constrained output?

**6. Practical recommendation**
- Given that I want ZERO ongoing costs, what is the most reliable way to run browser-use 0.12+ entirely locally?
- What hardware do I need? (I have a desktop with 32GB RAM and an NVIDIA RTX 3060 12GB)
- What specific model + inference server + configuration would you recommend?
- Are there any browser-use configuration tweaks (max retries, temperature, top_p) that improve structured output reliability with local models?

### Context about the AgentOutput schema:
The browser-use Agent expects output like:
```json
{
  "evaluation_previous_goal": "string",
  "memory": "string",
  "next_goal": "string",
  "action": [
    {
      "action_type": "click_element",
      "index": 5
    }
  ]
}
```
The `action` field is a list with `min_items: 1`, and each action is a union of ~15 different action types (click, type, scroll, extract, navigate, etc.), each with different parameters.

### Important:
- Focus on **what actually works in practice**, not just what theoretically should work.
- Cite GitHub issues, Discord discussions, blog posts, and benchmark results where possible.
- If the conclusion is "you can't reliably run browser-use locally with 8B models", say so clearly and recommend the minimum viable setup.
- Include any relevant changes in browser-use 0.13+ or upcoming versions that might affect this.
