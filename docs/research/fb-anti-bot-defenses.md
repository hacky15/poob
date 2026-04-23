---
type: research
status: active
date: 2026-03-17
tags: [marketplace, facebook, stealth, anti-bot]
related: [[browser-stealth-signals]] [[facebook-scrolling-and-listing-volume]]
---

# Research: Facebook Anti-Bot Defenses & Scraping Landscape 2025-2026

**MUTABLE: Facebook's defenses evolve constantly.** Sources: results_1.md, results_4.md.

## Defense Layers (6 total)

### 1. TLS Fingerprinting (JA3/JA4)
- Every TLS ClientHello produces a unique fingerprint
- Python `requests`, Go `net/http`, `curl` all have distinctive fingerprints
- Facebook cross-references JA3 hash against User-Agent — mismatch = instant block
- **Our mitigation**: Using real Chromium browser (browser-use) — TLS matches naturally

### 2. HTTP/2 Fingerprinting
- SETTINGS frame values, WINDOW_UPDATE, priority tree, pseudo-header ordering
- Chrome, Firefox, curl each have distinct HTTP/2 signatures
- Most libraries can't spoof these low-level params
- **Our mitigation**: Real browser — HTTP/2 stack is genuine

### 3. Browser Automation Detection
- `navigator.webdriver` flag
- Selenium-specific DOM variables (`window._selenium`, `document.$cdc_`)
- Empty `navigator.plugins`, headless Chrome UA signatures
- Canvas/WebGL rendering anomalies
- **Our mitigation**: browser-use with stealth scripts

### 4. Behavioral Analysis (ML models)
- Constant request intervals → flagged
- Linear navigation paths → flagged
- Missing scroll events → flagged
- Instant form fills → flagged
- **Our mitigation**: Random delays, scroll patterns, mouse simulation

### 5. Silent Degradation (Shadow Banning)
- Rather than blocking, Facebook returns incomplete data, empty responses, or redirects
- Scraper may not realize it's been detected
- Our shadow ban detection: 3+ consecutive empty sweeps

### 6. Dynamic __dyn/__csr Parameters
- Compressed encodings of loaded JS/CSS modules
- Session-specific, change per page
- Act as implicit fingerprints

## Safe Request Rates
- **3-7 seconds** between page navigations (consensus)
- **10-15 seconds** for conservative long-running scrapes
- Sub-second intervals: guaranteed penalties
- Sessions >1-2 hours from single IP: elevated detection risk

## Proxy Requirements
| Type | Success Rate | Cost | Notes |
|------|-------------|------|-------|
| Datacenter | <2% | Low | Blocked on first request |
| Residential | 90-98% | High | Required for sustained access |
| Mobile Carrier (CGNAT) | >95% | Very High | Best — FB can't ban shared IPs |
| ISP Dedicated | 15-30% | Moderate | Appears as single active user |

**Our situation**: Running from home ISP (residential) — should be fine at our volume.

## Detection Timelines
- Raw HTTP libraries: blocked on **first request**
- Headless browser, no stealth: 10-30 minutes
- Stealth browser + datacenter IP: 1-4 hours
- Stealth browser + residential IP: hours to days
- Anti-detect browser + mobile proxy: weeks to months

## sortBy=creation_time_descend Is Bugged
- "Severely bugged or deliberately deprecated" (multiple Reddit threads, community analysis)
- Facebook frequently returns algorithmic sort regardless of parameter
- Ignores geographic search radii in some cases
- **Our mitigation**: Verify sort ourselves, re-sort by posted_at after extraction

## Legal Landscape
- **Meta v. Bright Data (Jan 2024)**: Scraping publicly visible data while logged OUT does not violate TOS
- **Meta v. Voyager Labs**: Using fake accounts to bypass login = severe legal liability
- **hiQ v. LinkedIn**: Accessing publicly available data ≠ "access without authorization" under CFAA
- **Our situation**: We use a real account with genuine credentials — moderate risk. We're personal use, not commercial scale.

## mbasic.facebook.com: Discontinued
- The text-only mobile endpoint is deprecated
- Marketplace content doesn't render properly
- Not a viable fallback
