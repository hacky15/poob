# Research: Free Web Search & Price Comparison APIs
**Date:** March 17, 2026 | **Status:** CURRENT | **Sources:** results_5.md
**MUTABLE: Free tiers change frequently. Verify before relying on any provider.**

## Dead/Deprecated (Do Not Use)
- **Bing Search API**: Retired August 11, 2025. Replaced by "Grounding with Bing" at $35/1K (Azure only)
- **Brave Search free tier**: Eliminated February 2026. Now $5/month minimum
- **eBay Finding API**: Decommissioned February 5, 2025. findCompletedItems is gone
- **Amazon PA-API**: Deprecating April 30, 2026

## Active Free Search APIs (Ranked by Value)

| Provider | Free Limit | Index | Best For | Integration |
|----------|-----------|-------|----------|-------------|
| **Mojeek** | 2,000/day (~60K/month) | Own crawler | Massive free volume | REST JSON/XML |
| **Google Custom Search** | 100/day (~3K/month) | Google | Most reliable results | REST JSON |
| **You.com** | $100 credit (~20K searches) | Own + aggregated | Structured JSON for LLMs | REST JSON |
| **Tavily** | 1,000/month | Aggregated + AI-ranked | RAG-optimized content | REST JSON |
| **SerpAPI** | 250/month | Google SERP scrape | Google Shopping results | REST JSON |
| **Serper.dev** | 2,500 one-time | Google only | Cheapest Google scraping | REST JSON |
| **Exa.ai** | $10 credit (~2K) | Neural/semantic | Semantic search | REST JSON |
| **SearchAPI.io** | 100 total (one-time) | Multiple | Not recurring | REST JSON |

### Notes
- **DuckDuckGo**: NOT a web search API — Instant Answer API only returns Wikipedia abstracts/definitions
- **Yandex**: Complex pricing, weak for Western e-commerce
- Multiple Google Cloud projects can multiply the 100/day CSE quota

## Our Recommended Search Cascade
```
Google CSE (100/day) → Mojeek (2000/day) → You.com ($100 credit) → SearXNG (unlimited) → empty
```

## SearXNG Optimization for Deal Hunting

### Engine Selection
**Enable:** Google, Bing, Brave, DuckDuckGo, Startpage, eBay (native engine)
**Disable:** arXiv, PubMed, Wikipedia, Wikimedia, GitHub, StackOverflow (noise)

### Native eBay Engine
SearXNG has a built-in eBay engine (`searx/engines/ebay.py`) that uses XPath:
```yaml
engines:
  - name: ebay
    engine: xpath  # or native ebay engine
    categories: [general, shopping]
    disabled: false
    timeout: 3.0
```

### Production Config
```yaml
# settings.yml
search:
  formats: [html, json]  # Enable JSON API output
server:
  limiter: false  # Disable rate limiter for private instance
outgoing:
  request_timeout: 3.0
  pool_connections: 100
  pool_maxsize: 20
  enable_http2: true
```

### Docker Compose
- Use Valkey (Redis fork) for caching instead of Redis (license issues)
- Set `--maxmemory` on Valkey to prevent OOM
- Isolate on Docker bridge network
- Disable internal rate limiter

## eBay Sold Price Data

### No Free API Path
- Finding API: dead (Feb 2025)
- Browse API: 5,000 calls/day but **excludes sold/completed items** by design
- Marketplace Insights API: requires business partner approval (routinely denied)

### Direct Scraping (Only Option)
```
URL: https://www.ebay.com/sch/i.html?_nkw=KEYWORD&LH_Sold=1&LH_Complete=1
Selectors: .s-item__title, .s-item__price, .s-item__title--tag (date sold)
```
- "Best Offer" items show listing price, NOT accepted price — introduces error
- Rate limit: 1 request per 2-3 seconds with rotating proxies
- ScraperAPI/ScrapeOps free tiers: 1,000-5,000 requests

### Alternative Tools
- **Terapeak** (free for eBay sellers): 3 years of sold data, actual Best Offer prices — but no API
- **130point.com**: Free, shows actual Best Offer prices — collectibles focus, no API
- **SerpAPI**: Can query eBay sold listings (250 free/month) — same Best Offer inaccuracy

## Price Comparison APIs

### Truly Free
- **UPCitemdb**: 100 requests/day, no signup, 689M+ barcodes with pricing
- **eBay Browse API**: 5,000 calls/day for ACTIVE listing prices only
- **Open Food Facts**: Completely free, grocery/food products only

### Paid (Cheap)
- **Keepa**: €19/month minimum for API access, 1 token/minute
- **Zinc API**: $0.01/call for Amazon/Walmart real-time prices
- **Apify**: $5/month in compute credits

### Does Not Exist
- CamelCamelCamel API (no API, no plans for one)
- Free Google Shopping API (Content API is merchant-only)

## Self-Hosted Search Alternatives to SearXNG

| Tool | Status | Notes |
|------|--------|-------|
| **SearXNG** | Active, recommended | 240+ engines, JSON API, Docker |
| **4get** | Active | Best proxy rotation, resilient against Google blocks |
| **Websurfx** | Active | Rust-based, fast, low memory |
| **MeiliSearch** | Active | Local full-text search DB — complement, not replacement |
| **Stract** | Active | Own web crawler/index — no upstream dependency |
| **Whoogle** | Failing | Google blocking its scraping approach |
| **LibreX** | Stalled | Clean API but abandoned since 2023 |
