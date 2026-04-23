---
type: incident
status: resolved
date: 2026-03-18
tags: [marketplace, scanner, facebook, graphql, price]
related: [[price-none-is-not-zero]] [[facebook-graphql-anonymous-client]]
---

# $18.50 listings appeared as $1850 — cents treated as dollars

## Symptom

Occasional listings had prices 100× too large. A $18.50 item was stored as $1850. The pattern was not random — specific GraphQL response shapes triggered it.

## Root cause

`graphql_interceptor.py` treated `amount_with_offset_amount` identically to `amount`:

```python
amount_str = str(
    price_obj.get("amount")                        # DOLLARS as string
    or price_obj.get("amount_with_offset_amount")  # CENTS as string — NOT /100
    or price_obj.get("text", "")
)
```

Facebook uses different price field names in different endpoints:

| Field | Units |
|---|---|
| `listing_price.amount` | Dollars, string |
| `listing_price.amount_with_offset` | Cents, string (detail pages) |
| `listing_price.amount_with_offset_in_currency` | Cents, string (search results) |
| `listing_price.formatted_amount` | Display string ("$18.00") |

When `amount` was absent and any `amount_with_offset*` variant was present, the cents value was treated as dollars. Also: `amount_with_offset_in_currency` was not checked at all.

## Fix

```python
amount_str = price_obj.get("amount")
if not amount_str:
    cents_str = (
        price_obj.get("amount_with_offset_in_currency")
        or price_obj.get("amount_with_offset_amount")
        or price_obj.get("amount_with_offset")
    )
    if cents_str:
        amount_str = str(float(cents_str) / 100)
if not amount_str:
    amount_str = price_obj.get("text", "") or price_obj.get("formatted_amount", "")
```

## Validation

Spot-check on fresh patrol cycle: prices now match the display amount in the Facebook UI for all variants.

## Follow-ups

Gotcha written: [[graphql-amount-units-cents-vs-dollars]].
