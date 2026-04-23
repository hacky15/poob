---
type: gotcha
status: active
date: 2026-03-18
tags: [marketplace, facebook, graphql, price]
related: [[graphql-amount-with-offset-cents-bug]] [[facebook-graphql-anonymous-client]]
---

# Facebook GraphQL price fields — CENTS vs DOLLARS

## Trigger

Reading any price-related field from a Facebook GraphQL response. The field names look similar but the units are not.

## Why it happens

Different Facebook endpoints use different field names with different units. All are strings, not numbers. Mixing them up yields prices that are 100× too large (or small).

## Don't

- Treat `amount_with_offset_amount` or `amount_with_offset_in_currency` as dollars. They're cents.
- Use `str()` coercion blindly — numeric values may be strings but aren't always dollar-scaled.
- Assume only one field is populated. Different endpoints populate different variants.

## Do

Use the correct unit per field:

| Field | Units |
|---|---|
| `listing_price.amount` | Dollars (string, e.g. `"18.00"`) |
| `listing_price.amount_with_offset` | Cents (detail pages) |
| `listing_price.amount_with_offset_in_currency` | Cents (search results) |
| `listing_price.formatted_amount` | Display string (`"$18.00"`) |

When the `amount` field is absent, fall back in priority order and divide cents by 100:

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

## Reference

- Incident this was extracted from: [[graphql-amount-with-offset-cents-bug]].
- Field reference: [[facebook-graphql-anonymous-client]].
