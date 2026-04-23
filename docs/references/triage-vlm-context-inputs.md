---
type: reference
status: active
date: 2026-03-28
tags: [vlm, triage, llm, prompt-construction]
related: [[vlm-triage-pipeline]] [[price-none-is-not-zero]]
---

# What each evaluation stage sees

## Summary

Exact input list for text triage and VLM evaluation. Critical for prompt-construction invariants and for reasoning about why a given listing was scored the way it was.

## Text triage LLM receives

- Title
- Price ("$X", "FREE", or "Price not listed" — see [[price-none-is-not-zero]])
- Description (300 chars max)
- Location
- Seller
- Condition
- Watchlist preferences as HARD RULES
- Batch of 5 listings per prompt
- **Does NOT receive**: images, comparable sales, enrichment data

## VLM receives (15+ data points)

- 2 listing images (first + last)
- Title, price, description (500 chars max), location, seller, posted date
- Seller-stated original price (regex-extracted from description)
- Comparable sales (eBay medians or MSRP)
- Visual enrichment (product name, brand, model, OCR text)
- Condition signals (regex-detected from text)
- Model numbers (regex-detected)
- Title quality score (0-1, penalizes ALL CAPS, emojis, spam)
- Freshness bonus
- Multi-item / bulk flag
- Triage signals (urgency, misspelling, scam)
- Watchlist context with BINDING preference constraints

## Price display consistency

Every LLM-facing stage uses the same three-way display logic:

```python
if listing.price is not None and listing.price > 0: "$X.XX"
elif listing.price is not None and listing.price == 0: "FREE"
else: "Price not listed" / "Not listed (check listing image for price)"
```

Before this was enforced, triage and VLM both showed "FREE" for unknown prices and VLMs hallucinated 100% discounts. See [[price-none-is-not-zero]].

## Usage in this project

- [vlm/prompt_builder.py](../../src/poob/vlm/prompt_builder.py) — VLM prompt construction
- [triage/triage.py](../../src/poob/triage/triage.py) — text triage prompt

## Source

Internal — invariants enforced in code, not an external contract.
