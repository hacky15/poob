---
type: incident
status: resolved
date: 2026-05-29
tags: [scanner, graphql, geo, location, coverage, madison]
related: [[anon-browser-cdp-death-no-recovery]] [[husqvarna-enrichment-cross-contamination]] [[listing-freshness-verification]]
---

# Browse-path GQL ignored the configured location → wrong metro (Appleton not Madison)

## Symptom

Operator asked, during a coverage audit: "how many listings should Madison WI be getting and are we seeing all of them." The DB showed the captured feed was **Appleton / Fox-Valley dominant, not Madison**: 24h distinct location mix Appleton 35, Neenah 9, Green Bay 7, Oshkosh 5, Fond du Lac 6 … **Madison WI only 5**. Appleton is ~100 miles north of Madison — entirely outside the configured 40-mile Madison radius. ~64% of captured listings were Fox Valley; only ~14% Madison-area.

## Root cause

A one-call-site config-default bug — **not** IP geolocation (that hypothesis was raised and then disproven).

- `graphql_client.py` hardcodes `DEFAULT_LATITUDE=44.2619 / DEFAULT_LONGITUDE=-88.4154` = **Appleton**, and `build_search_params` falls through to those defaults when no `location_slug`/lat/lon is passed.
- The primary general-browse path, `PatrolEngine._fetch_anonymous_graphql._fetch_category`, called `build_search_params(query=category, radius_miles=..., days_listed=..., count=...)` with **no location** — so every category-browse query was centered on the Appleton default, and FB faithfully returned Appleton-area inventory.
- The filter chain's `GeoDistanceFilter` is correctly centered on Madison, so it discarded most of the Fox-Valley inventory downstream — but only *after* it had flooded the eval pool (≈109 of 200 evaluated listings were Fox Valley, wasting eval slots).

Disproofs gathered during the audit:
- **FB honors our lat/lon param, it's not IP.** The watchlist GQL path *does* pass `location_slug=marketplace_default_location` → those rows come back Madison-clustered (15 Madison / 11 Fox Valley / 7 CA). Same anon `__user=0` session, same IP, different lat/lon param → different metro. So FB obeys the param; the browse path just wasn't sending it.
- **The IP is Madison, not Appleton.** `ipinfo.io` and `ipapi.co` both resolve the homelab IP (68.79.103.128, AS7018 AT&T) to Madison (43.0731, -89.4012). The "datacenter IP geolocates to Fox Valley" theory is false.

## Fix

`src/poob/scanner/patrol_engine.py` `_fetch_category`: thread the configured location into the browse query, mirroring what the watchlist path already does:

```python
params = build_search_params(
    query=category,
    location_slug=getattr(self._config, "marketplace_default_location", None),
    radius_miles=self._config.patrol_base_radius_miles,
    days_listed=self._config.patrol_days_since_listed,
    count=self._config.scan_max_listings_per_query,
)
```

Zero additional requests — it changes *where* the same queries point, so it does not worsen the FB rate-limit pressure (the audit's #1 constraint). Regression test `TestBrowsePathLocationThreading` asserts every browse category call carries the configured `location_slug`.

## Validation

- New regression test passes; full `test_patrol_engine.py` suite green.
- Post-deploy (once GQL rate-limit lapses enough to ingest): the no-coords/browse-path region split should flip from Fox-Valley-dominant to Madison-dominant. Re-run the audit's DB location-mix query to confirm.

## Follow-ups (from the coverage audit, NOT fixed here)

- **Defense-in-depth:** the hardcoded Appleton `DEFAULT_LATITUDE/LONGITUDE` in `graphql_client.py` is a stale single-tenant default that silently masquerades as "the user's location." Per the meta-rule (no hardcoded single-tenant defaults), it should derive from config or make an unlocated query fail loud. Deferred — broader blast radius, wanted the focused fix tested first.
- **FB rate-limit is the dominant volume constraint** (177/182 cycles saw zero; anon GQL hard-limited on the IP). The geo fix corrects *targeting*, not *volume*. Volume needs a residential/rotating egress ($ — operator approval) or a slower cadence / fewer POSTs per cycle. Separate problem.
- **posted_at NULL on 67% of ingested** → hard-blocked from notification. Largest downstream funnel loss; fix at the extraction layer.
- **Freshness-window vs cadence arithmetic**: `public_notification_max_age_minutes=10` vs a 5-30min cadence + FB indexing lag → ~98% of timestamped listings miss the window. Needs a decision note before changing (freshness/safety-valve rule).
- **Operator decision still open:** Madison vs also-Appleton/Fox-Valley. The fresh local supply genuinely lives in the Fox Valley; the homelab IP is Madison. The browse fix makes `marketplace_default_location` (currently `madison`) authoritative — flipping the target metro or adding a second center is a config + decision-note change.
