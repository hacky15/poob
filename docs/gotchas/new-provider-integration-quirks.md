---
type: gotcha
status: active
date: 2026-03-18
tags: [llm, provider, integration]
related: [[vlm-cascade-operational-findings]]
---

# Provider integration quirks — Google CSE, Together.ai, Mistral Pixtral

## Trigger

Adding a new LLM, VLM, or search provider and hitting a non-obvious failure that isn't a quota issue.

## The quirks

### Google Custom Search API — HTTP 403 is NOT a quota error

The Custom Search JSON API must be explicitly enabled at [console.cloud.google.com/apis/library/customsearch.googleapis.com](https://console.cloud.google.com/apis/library/customsearch.googleapis.com). An API key alone is not enough — the API has to be enabled for that specific project. 403 means "API not enabled" or "key restricted to other APIs," NOT "rate limited."

Treat 403 as session-exhausted (same as 429) so the cascade stops retrying.

### Together.ai — HTTP 402 "Credit limit exceeded" on their "free" model

Together.ai's "Llama-Vision-Free" model is NOT truly free without a payment method on file. With `$0` credit limit, every call returns 402. Treat `402 + "credit"` as a rate-limit error in the VLM cascade. User needs to add a payment method; Together requires it as anti-abuse even for the free tier.

### Mistral Pixtral — HTTP 422 `extra_forbidden`

LangChain's `ChatOpenAI` sends `max_tokens` in the request body, but Mistral's API rejects unknown/extra fields with 422. Two fixes:

- **Preferred**: use `langchain-mistralai` (`ChatMistralAI`) which knows Mistral's parameter names.
- **Fallback**: use `ChatOpenAI` without `max_tokens`.

## Don't

- Assume HTTP 403 / 402 / 422 are quota errors — they're often shaped like quota errors (retry-respecting cascade) but happen for configuration reasons and will NEVER succeed without intervention.
- Retry 402/422 on the same request — it will fail every time.

## Do

- Check the provider's actual docs when integrating. Each free tier has a quirk; it's faster to learn them upfront.
- Map permanent-error codes (402, 403, 404, 422) to "disable this provider for 24h" logic rather than transient-retry logic.

## Reference

See [[vlm-cascade-operational-findings]] — transient vs permanent error handling in the cascade.
