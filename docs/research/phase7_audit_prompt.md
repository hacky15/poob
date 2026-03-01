# Deep Research Audit: Facebook Marketplace Patrol System

## Context for the Research LLM

You are auditing an autonomous deal-hunting bot that continuously monitors Facebook Marketplace for deals. The system recently underwent a **major architectural overhaul** from search-based to patrol-based monitoring (Phase 7). Your job is to identify any areas where the implementation may not be ideal based on **current (2025-2026) Facebook Marketplace behavior**, anti-bot research, reverse engineering findings, and user forum experiences.

Be brutally honest. Flag anything that won't work in practice, even if the theory is sound. Cite specific sources (Reddit threads, GitHub issues, research papers, blog posts, forum discussions) wherever possible.

---

## System Overview

**Goal:** Continuously monitor ALL new Facebook Marketplace listings in the Appleton, WI area (~650 new listings/day, ~27/hour avg, ~65-70/hour peak 4-9PM). Match listings against user interests (e.g., "PS5 under $400"), evaluate deal quality via LLM-powered price intelligence, and notify via Discord.

**Stack:**
- **Browser automation:** browser-use + Playwright (Chromium)
- **LLM:** Ollama (local qwen3:8b) + Gemini 2.5 Flash (cloud, free tier) for knowledge tasks
- **Bot:** discord.py for two-way interaction
- **Storage:** aiosqlite (SQLite)
- **Runtime:** Windows PC with RTX 2070 Super (8GB VRAM), Ryzen 7 3800X, 32GB RAM

---

## Implementation Details to Audit

### 1. Category Sweep Strategy

**What we do:** Navigate to Facebook Marketplace category pages in sequence, sorted by newest first.

**URL pattern:**
```
https://www.facebook.com/marketplace/category/{slug}?sortBy=creation_time_descend&daysSinceListed=1&deliveryMethod=local_pick_up&exact=false&radius={miles}
```

**Categories swept (10 + optional "all"):**
electronics, furniture, vehicles, sports, garden, appliances, free, toys, apparel, entertainment

**Serialized execution:** One category at a time, 15-25s random delay between categories (uniform distribution). No multi-tab or multi-context on the same account.

**Questions to research:**
1. Does Facebook Marketplace still support `sortBy=creation_time_descend` as a URL parameter in 2025-2026? Has the URL structure changed?
2. Does `daysSinceListed=1` actually filter to last 24 hours? What does Facebook's backend interpret this as?
3. Is `deliveryMethod=local_pick_up` still a valid filter parameter?
4. Are these category slugs still valid? Has Facebook changed their category taxonomy recently?
5. What does Facebook actually show when you browse a category page sorted by newest? Is it truly ALL new listings, or does FB's algorithm inject "recommended" listings and suppress some new ones?
6. Does Facebook paginate category results? How many listings appear on initial load vs. after scrolling? Is there a cap?
7. How many scroll actions are needed to see all listings from the last 2 hours in a category with ~7 listings/hour?
8. **Critical:** Do 10 categories + "all listings" page = 11 navigations per cycle cause any issues? Total cycle time at 15-25s delays = ~165-275s (2.75-4.6 min) just for navigation, before deep inspection.

---

### 2. Cache Busting via Radius Oscillation

**What we do:** Cycle the radius parameter through 4 values each sweep: `[20, 22, 18, 24]` miles (base=20, jitter=4).

**Theory:** Changing the geographic bounding box forces Facebook's backend to recalculate results instead of serving cached responses.

**Questions to research:**
1. Does this actually work? Is there evidence that Facebook caches category browse results per-radius?
2. Does oscillating radius by ±2-4 miles actually produce meaningfully different result sets, or does FB's geo-indexing use coarse grid cells that encompass this entire range?
3. Are there better cache-busting strategies reported by scrapers? (e.g., varying latitude/longitude, adding/removing filters, changing sort order)
4. Does the `radius` parameter even work for category browsing, or only for search queries?
5. Could aggressive radius changes themselves be a detection signal?

---

### 3. Adaptive Polling Schedule

**What we do:**
| Time of Day | Base Interval | With Jitter (±20%, clamped ±40%) |
|---|---|---|
| 4-9 PM (peak) | 120s (2 min) | 72-168s |
| 8 AM - 4 PM (moderate) | 300s (5 min) | 180-420s |
| 6-8 AM, 9 PM - midnight (off-peak) | 600s (10 min) | 360-840s |
| Midnight - 6 AM (dead) | 900s (15 min) | 540-1260s |

**Jitter method:** Gaussian distribution with σ = 20% of base, clamped to ±40%.

**Questions to research:**
1. Is 2-minute polling during peak hours aggressive enough to catch new listings before competitors?
2. Is 2-minute polling during peak hours TOO aggressive and likely to trigger rate limiting or shadow bans?
3. Facebook reportedly has a 15-30 minute batching delay before new listings appear in browse/search results. If true, does polling faster than every 15 minutes provide any benefit, or are we just re-fetching the same stale data?
4. What's the sweet spot between freshness and detection risk?
5. Does the Gaussian jitter distribution actually help avoid detection, or would uniform random be better? What do anti-bot researchers recommend?
6. Are there reports of Facebook implementing sophisticated timing analysis (beyond simple rate limiting) to detect automated browsing?
7. Should peak hours be adjusted for the Central Time Zone (Appleton, WI)? Facebook's peak listing times may differ from generic internet peak hours.

---

### 4. Anti-Detection / Stealth Measures

**Current measures:**
- Persistent browser profile (retains login cookies)
- Serialized category navigation (no multi-tab)
- Random delays between categories (15-25s uniform)
- Random delays between listing inspections (3-7s uniform)
- Human-like scroll pattern: 5 steps, 80% down / 20% up, 100-600px per scroll, 200-1500ms pauses between scrolls
- Gaussian jitter on polling intervals
- No CDP (Chrome DevTools Protocol) detection countermeasures beyond what Playwright provides
- No fingerprint randomization (same window size 1280x1100 every session)

**Questions to research:**
1. Does Facebook detect Playwright/Chromium automation in 2025-2026? What specific detection vectors does Meta use?
2. Is `navigator.webdriver` being checked? Does Playwright set this flag? Does browser-use patch it?
3. Does Facebook use Canvas/WebGL/AudioContext fingerprinting to detect headless or automated browsers?
4. Is the fixed window size (1280x1100) a fingerprinting risk? Should it vary?
5. What is Facebook's current approach to shadow banning automated browsers? How do you detect a shadow ban (silently degraded results)?
6. Are there recommended Playwright stealth plugins or patches for Facebook specifically?
7. Does Facebook track mouse movement patterns? Our system only scrolls, it doesn't move the mouse realistically.
8. How important is maintaining a "warm" session (occasional non-scraping activity like viewing profile, marketplace homepage browsing) to avoid suspicion?
9. Does the persistent browser profile help or hurt? Could accumulated state (cookies, localStorage) become a fingerprinting vector?
10. **Rate limiting specifics:** How many page loads per hour does Facebook allow before degrading results? Are there known thresholds?

---

### 5. Listing Data Extraction

**Surface extraction (category pages):**
- JavaScript executed in page context via `page.evaluate()`
- Selects all `a[href*="/marketplace/item/"]` links
- Extracts: external_id from URL, title from link text, price from text matching currency regex, image from `img[src*="scontent"]` or `img[src*="fbcdn"]`

**Deep extraction (individual listing pages):**
- Navigate to `https://facebook.com/marketplace/item/{id}`
- Wait 2000ms for page load
- Extract Open Graph meta tags: `og:title`, `og:description`, `og:image`, `og:url`
- Extract JSON-LD structured data: `<script type="application/ld+json">` with `@type === "Product"` or `@type === "Offer"`
- Extract price from JSON-LD `price` or `lowPrice` field
- Fallback: extract page text as markdown for description

**Questions to research:**
1. Does Facebook Marketplace still render `<a href="/marketplace/item/{id}">` links in the DOM for category browse pages? Or has the DOM structure changed (React hydration, Shadow DOM, obfuscated class names)?
2. Do Facebook Marketplace listing pages still include Open Graph meta tags in 2025-2026?
3. Do Facebook Marketplace listing pages still include JSON-LD Product/Offer structured data?
4. Is there any difference in what data is available in server-rendered HTML vs. client-rendered React content? Does the initial HTML response contain OG/JSON-LD, or does it require JavaScript execution?
5. Has Facebook moved to obfuscated CSS class names that change frequently? Could our selectors break without warning?
6. Are listing images still served from `scontent*.fbcdn.net` domains?
7. **Price extraction reliability:** Do all FB Marketplace listings have prices in the DOM? What about "Free" listings, "$0" listings, or "Contact for price" listings?
8. Does Facebook serve different HTML/meta tags based on whether the request comes from an authenticated session vs. a crawler?
9. Is 2000ms wait sufficient for listing detail pages to fully render? What if the page has lazy-loaded content?

---

### 6. Multiple Account / Instance Strategy

**Current implementation:** Single browser profile, single Facebook account, single serial patrol loop.

**Questions to research:**
1. Would running multiple Facebook accounts in parallel (separate browser profiles) significantly increase coverage?
2. What are the practical risks of running 2-3 accounts? (linking via IP, device fingerprint, behavioral correlation)
3. Is there a meaningful coverage gap with a single account? With 10 categories taking ~3-5 minutes to sweep, plus deep inspection time, could we miss listings that appear and get sold within one sweep cycle?
4. What do experienced FB Marketplace scrapers recommend for scaling? Multiple accounts? Multiple IPs? Residential proxies?
5. Does Facebook link accounts that share the same IP address or browser fingerprint?
6. Would a single account with no category filtering (just browsing "all" new listings) be more efficient than sweeping 10 individual categories?
7. **Appleton-specific:** With only ~27 new listings/hour average, is a single account with 2-5 minute cycles actually sufficient to catch everything?

---

### 7. Interest Matching Algorithm

**What we do:** All-word substring matching. Every word in the user's interest (e.g., "PS5 Disc Edition") must appear somewhere in the listing title (case-insensitive).

**No fuzzy matching.** "PS5" matches "PS5 Disc Edition Bundle" but NOT "PlayStation 5" or "Play Station Five."

**Questions to research:**
1. Is strict substring matching too rigid for Facebook Marketplace titles? How much variation is there in how sellers title their listings?
2. Do sellers commonly use abbreviations, misspellings, or alternate names that would be missed? (e.g., "Playstation" vs "PS5", "fridge" vs "refrigerator")
3. Would simple synonym expansion or edit-distance matching significantly improve recall without hurting precision?
4. Should we use the LLM for fuzzy matching on titles, or is that too slow/expensive for 27+ listings per hour?
5. How do other deal-hunting bots handle matching? What approaches have the best precision/recall tradeoff?

---

### 8. SmartDealRadar v2 Price Intelligence

**Pipeline:** Identify item → eBay sold lookup (HTTP) → Retail price estimate (LLM) → Category estimate (LLM) → Score

**Scoring:**
- Compares listing price to estimated market price (from eBay sold data or LLM estimate)
- INCREDIBLE: ≥60% discount, GREAT: ≥40%, GOOD: ≥20%, FAIR: >0%
- Scam threshold: listings priced below a configurable % of market price are flagged as potential scams

**Max evaluations per cycle:** 20 (configurable)

**Questions to research:**
1. Is eBay sold data a reliable proxy for Facebook Marketplace pricing? Are FB prices typically higher, lower, or comparable to eBay sold?
2. Is 20 evaluations per cycle sufficient for ~27 new listings/hour? What happens when there are more new listings than the evaluation cap?
3. How accurate are LLM-based retail price estimates for used goods? What's the typical error margin?
4. What's the false positive rate for scam detection at various thresholds? (e.g., 70% below market = scam)
5. Are there better pricing data sources than eBay for local marketplace items? (e.g., PriceCharting for games/electronics, KBB for vehicles)

---

### 9. Deep Inspection Phase

**What we do:** For every new listing, navigate to its detail page (3-7s delay between navigations), extract OG + JSON-LD data.

**Questions to research:**
1. With ~27 new listings/hour, deep inspection at 3-7s per listing = 81-189 seconds per hour. Is this sustainable?
2. During peak hours (65-70/hour), deep inspection = 195-490 seconds (3-8 minutes). Combined with the ~3-5 minute category sweep, does this exceed the 2-minute polling interval? What happens when cycles overlap?
3. Is navigating to every listing page suspicious behavior? Would Facebook flag an account that views 30+ listing detail pages per hour?
4. Can we extract sufficient data from category browse pages alone (skipping deep inspection) to still make meaningful deal evaluations?
5. Is the OG + JSON-LD data actually richer than what's available on the category browse page? What additional fields does deep inspection provide?
6. Would it be better to selectively deep-inspect only listings that passed a preliminary interest/price filter?

---

### 10. Facebook Marketplace Specific Behavior (2025-2026)

**General questions about current FB Marketplace state:**
1. Has Facebook made any major changes to Marketplace in 2025-2026 that would affect automated monitoring?
2. Are there any new anti-scraping measures deployed specifically for Marketplace?
3. Has Facebook changed the Marketplace URL structure or moved to a new frontend architecture?
4. Does Facebook A/B test Marketplace features (showing different results/layouts to different users)?
5. Are there any known API endpoints (GraphQL, REST) that could be used instead of browser automation for listing data?
6. What is the current state of Facebook's login wall for Marketplace? Can you browse without logging in?
7. Has Facebook implemented any CAPTCHA or challenge systems for Marketplace browsing?
8. Are there any legal considerations (CFAA, Terms of Service enforcement) for automated Marketplace monitoring in 2025-2026?
9. What does Facebook's rate limiting look like? Is it per-account, per-IP, per-device fingerprint, or some combination?
10. Are there any Facebook Marketplace API alternatives (official or unofficial) that would be more reliable than browser automation?

---

### 11. Overall Architecture Evaluation

**Questions about the big picture:**
1. Is browser automation (Playwright) still the right approach for Facebook Marketplace in 2025-2026, or have better methods emerged?
2. How do commercial deal-hunting services (e.g., Marketplace Alerts, Deal Finder apps) approach this problem?
3. Is a 5-phase serial pipeline (sweep → dedup → inspect → evaluate → notify) optimal, or would a streaming/event-driven architecture be better?
4. What's the expected operational lifetime before detection? Days? Weeks? Months? What do forum users report?
5. What's the recovery strategy when detection occurs? Account rotation? IP rotation? Cool-down periods?
6. Are there any alternative data sources for Facebook Marketplace listings that don't require direct scraping? (RSS feeds, third-party aggregators, Facebook's own notification system)
7. For a small-market area like Appleton, WI, is this level of automation overkill? Could simpler approaches (e.g., Facebook's own notification preferences, IFTTT, or email alerts) achieve 80% of the value?

---

## Implementation Code Highlights for Reference

### Patrol URL Builder
```python
def build_patrol_url(
    category: str | None = None,
    radius: int = 20,
    days_since_listed: int = 1,
    exact: bool = False,
) -> str:
    base = "https://www.facebook.com/marketplace"
    if category:
        slug = get_category_slug(category)
        base = f"{base}/category/{slug}"
    params = {
        "sortBy": "creation_time_descend",
        "daysSinceListed": str(days_since_listed),
        "deliveryMethod": "local_pick_up",
        "exact": str(exact).lower(),
        "radius": str(radius),
    }
    return f"{base}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
```

### Radius Oscillator
```python
class RadiusOscillator:
    def __init__(self, base: int = 20, jitter: int = 4):
        half = jitter // 2
        self._radii = [base, base + half, base - half, base + jitter]
        self._index = 0

    def next(self) -> int:
        r = self._radii[self._index % len(self._radii)]
        self._index += 1
        return r
```

### Adaptive Scheduler Timing
```python
def _calculate_base_interval(self, hour: int) -> int:
    if self._config.patrol_peak_hours_start <= hour < self._config.patrol_peak_hours_end:
        return self._config.patrol_peak_interval_seconds      # 120s
    if 8 <= hour < self._config.patrol_peak_hours_start:
        return self._config.patrol_moderate_interval_seconds   # 300s
    if hour < 6:
        return self._config.patrol_dead_interval_seconds       # 900s
    return self._config.patrol_offpeak_interval_seconds        # 600s

def _apply_jitter(self, base_seconds: int) -> float:
    jittered = random.gauss(base_seconds, base_seconds * 0.2)
    lower = base_seconds * 0.6
    upper = base_seconds * 1.4
    return max(lower, min(upper, jittered))
```

### Interest Matcher
```python
def _interest_matches(self, title: str, interest: str) -> bool:
    title_lower = title.lower()
    return all(word in title_lower for word in interest.lower().split())
```

### Scroll Pattern
```python
def random_scroll_pattern(steps: int = 5) -> list[tuple[int, int]]:
    pattern = []
    for _ in range(steps):
        direction = 1 if random.random() < 0.8 else -1  # 80% down, 20% up
        distance = random.randint(100, 600) * direction
        pause_ms = random.randint(200, 1500)
        pattern.append((distance, pause_ms))
    return pattern
```

### Surface Listing Extraction (JS)
```javascript
// Executed via page.evaluate() on category pages
const links = document.querySelectorAll('a[href*="/marketplace/item/"]');
// Extract: external_id from href, title from innerText, price from text matching currency regex,
// image from img[src*="scontent"] or img[src*="fbcdn"]
```

### OG + JSON-LD Extraction (JS)
```javascript
// Open Graph
const metas = document.querySelectorAll('meta[property^="og:"]');
// Returns: {title, description, image, url}

// JSON-LD
const scripts = document.querySelectorAll('script[type="application/ld+json"]');
// Searches for @type === "Product" or @type === "Offer"
// Returns: {price, condition, availability, ...}
```

---

## What I Need from This Research

1. **Specific, actionable findings** — not generic "be careful with scraping" advice. Tell me exactly what works and what doesn't based on real evidence.
2. **Sources** — link to specific Reddit threads, GitHub issues, blog posts, research papers, or forum discussions that support your findings.
3. **Priority ranking** — which issues are most likely to cause total failure vs. minor degradation?
4. **Recommended fixes** — for each problem identified, suggest a specific technical solution.
5. **Comparisons** — how does our approach compare to what others doing similar work have found effective?

Focus especially on **2024-2026 Facebook Marketplace behavior**, as the platform changes frequently and older information may be outdated.
