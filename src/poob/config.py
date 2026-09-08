"""Application configuration loaded from environment variables and .env file."""

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """All configuration loaded from environment variables and .env file.
    Single source of truth for every tunable parameter."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Discord ---
    discord_bot_token: str
    discord_deals_channel_id: int
    discord_command_prefix: str = "!"
    # Bot DMs this user on startup with version + git SHA. Leave 0 to disable.
    discord_owner_user_id: int = 0
    # Injected at Docker build time via GIT_SHA build-arg; "dev" for local runs.
    git_sha: str = "dev"

    # --- LLM ---
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    ollama_num_ctx: int = 12000
    ollama_json_temperature: float = 0.0
    llm_temperature: float = 0.3

    # --- Search APIs (cascade: Tavily → Serper → empty) ---
    tavily_api_key: str = ""
    serper_api_key: str = ""  # Serper.dev: 2,500 free searches (one-time, no CC)
    searxng_base_url: str = "http://localhost:8080"  # Self-hosted, unlimited free

    # --- Cloud LLM ---
    google_api_key: str = ""
    # Optional SEPARATE Google key for the scraper's VLM image evaluation so it
    # doesn't burn the voice router's shared free-tier budget (1,500 RPD / ~20
    # RPM per model). Empty → VLM falls back to google_api_key (no behavior
    # change). Set VLM_GOOGLE_API_KEY in .env (a 2nd free Google project) to
    # fully isolate the scraper's Gemini usage from voice routing.
    vlm_google_api_key: str = ""
    google_model: str = "gemini-2.5-flash"
    # Primary Gemini ROUTER rung. 2.5-flash-lite (GA, free) is the reliable
    # default: in the 2026-06-22 busy-VC audit it routed 14/14 with ZERO
    # timeouts, while 3.1-flash-lite PREVIEW timed out (>6s httpx ceiling) on
    # ~39% of calls under load — burning 6s before falling through. The earlier
    # ~587ms 3.1 benchmark was unloaded/single-call and didn't reflect busy-VC
    # tail latency. 3.1 is demoted to the overflow alt rung (gemini_router_model_alt).
    # See docs/decisions/gemini-router-prefer-2.5-ga-over-3.1-preview.md.
    agent_google_model: str = "gemini-2.5-flash-lite"
    # Gemini 3 Flash: non-thinking, fast — preferred agent fallback over 2.5-flash-lite
    # 2026-09-08: "gemini-3-flash" (no such id — confirmed absent from the
    # live catalog) replaced with gemini-3.6-flash, smoke-tested live
    # (~1.7s median vs a same-day 3.5-flash-lite run that swung 0.6-8s —
    # rejected for this "fast" role on that inconsistency). See
    # docs/decisions/disable-dead-vision-and-fallback-model-rungs.md.
    agent_google_model_fast: str = "gemini-3.6-flash"

    # --- Cerebras (free tier: 1K RPM with max_tokens capped, 64K TPM, 1M tokens/day) ---
    cerebras_api_key: str = ""
    cerebras_model: str = "qwen-3-235b-a22b-instruct-2507"

    # --- Groq (free tier: 30 RPM, 14,400 RPD, no credit card) ---
    groq_api_key: str = ""
    deepgram_api_key: str = ""  # Deepgram streaming STT ($200 free credit)
    # Deepgram streaming model. "nova-3" (stable, default) or
    # "flux-general-en" (Oct 2025, ~450ms P50 faster, same keyterm API).
    deepgram_model: str = "nova-3"
    # Spotify Web API credentials (free; Developer Dashboard). Public
    # playlist reads only — ClientCredentials flow, no OAuth, no Premium
    # required. Both empty disables the queue_spotify_playlist action
    # with a friendly soft error. See
    # docs/plans/music-spotify-playlist-import.md.
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    picovoice_access_key: str = (
        ""  # Vestigial — Porcupine replaced by OpenWakeWord; field kept for back-compat
    )
    # Path to the .onnx wake-word model loaded by OpenWakeWord at boot.
    # Authoritative env-var is WAKE_WORD_MODEL_PATH; the legacy
    # PORCUPINE_KEYWORD_PATH alias keeps older .env files / Komodo
    # stacks working through one rotation cycle. See
    # docs/gotchas/wake-word-model-path-conventions.md for why models
    # live at /app/*.onnx and not /app/data/ (the volume shadows it).
    wake_word_model_path: str = Field(
        default="",
        validation_alias=AliasChoices(
            "WAKE_WORD_MODEL_PATH",
            "PORCUPINE_KEYWORD_PATH",
        ),
    )
    groq_model: str = "openai/gpt-oss-20b"
    # Empty by default: Groq exited the vision-model business entirely
    # (confirmed live via models.list() 2026-09-08 — zero vision-capable
    # models in the catalog, not just this one removed). No live
    # replacement exists on Groq. Set only if Groq ever ships one again;
    # main.py and vlm_cascade.py both gate on this being non-empty. See
    # docs/decisions/disable-dead-vision-and-fallback-model-rungs.md.
    groq_vision_model: str = ""
    # Agent brain primary: GPT-OSS 120B — reasoning-capable, 500 T/sec on Groq
    agent_groq_model: str = "openai/gpt-oss-120b"

    # --- NVIDIA NIM ---
    nvidia_api_key: str = ""
    # qwen3-next: supports grammar-based structured output required by browser-use
    nvidia_model: str = "qwen/qwen3-next-80b-a3b-instruct"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    # Agent brain model on NVIDIA NIM (tool calling for conversational agent)
    agent_nvidia_model: str = "qwen/qwen3-next-80b-a3b-instruct"

    # --- Browser ---
    browser_llm_provider: str = "nvidia"  # "nvidia", "gemini", or "ollama"
    # 2026-09-08: "gemini-2.0-flash" (confirmed absent from the live
    # catalog) replaced with gemini-3.5-flash-lite, smoke-tested live and
    # functional (latency swings 0.6-8s, tolerable here since browser
    # navigation steps already dominate wall-clock time; unlike
    # agent_google_model_fast, which needed the more consistent
    # gemini-3.6-flash). Kept distinct from every other configured Gemini
    # model for its own RPM bucket. See docs/decisions/
    # disable-dead-vision-and-fallback-model-rungs.md.
    browser_google_model: str = "gemini-3.5-flash-lite"  # separate model avoids sharing RPM quota
    browser_headless: bool = False
    browser_profiles_dir: Path = Path("browser_profiles")
    browser_use_vision: bool = True

    # --- Scanning ---
    scan_mode: str = "direct"  # "direct" (CDP) or "agent" (browser-use)
    scan_interval_minutes: int = 15
    scan_max_listings_per_query: int = 50  # Per GraphQL page (FB supports up to ~120)
    scan_stealth_min_delay_ms: int = 1500
    scan_stealth_max_delay_ms: int = 4000
    scan_browse_enabled: bool = True  # Browse latest local listings alongside watches

    # --- Deal Radar ---
    deal_radar_enabled: bool = True
    deal_radar_min_score: str = "good"  # Minimum score to create a Deal
    deal_public_min_score: str = "incredible"  # Public channel: only INCREDIBLE deals
    deal_watchlist_min_score: str = "good"  # Watchlist DMs: GOOD or better
    # PUBLIC-feed selectivity floors (watchlist DMs are exempt — gated by their
    # own per-item notification_threshold). Tune against the live feed. See
    # docs/decisions/public-incredible-selectivity-floors.md.
    deal_incredible_abs_dollar_floor: float = 50.0  # INCREDIBLE needs >=$50 real savings
    deal_max_value_multiple: float = 4.0  # clamp unbranded value to <=4x asking
    free_item_min_value: float = 40.0  # free item below this resale value -> FAIR
    free_item_incredible_min_value: float = 80.0  # free item below this -> cap GREAT
    vision_model: str = "qwen2.5vl:7b"  # Vision-capable model (local Ollama VLM fallback)
    vision_model_num_ctx: int = 4096  # Context window for vision model
    # Local fast model for simple tasks — saves cloud quota
    local_fast_model: str = "qwen3:4b"
    local_fast_num_ctx: int = 4096
    ebay_http_timeout_seconds: int = 10
    deal_radar_max_evaluations: int = 50  # VLM evaluation cap (top-N after enrichment)
    # Detail-page enrichment cap, decoupled from the VLM eval cap. Enrichment
    # is the ONLY source of posted_at for anon-GQL listings (FB strips
    # creation_time), and it yields a timestamp ~100% of the time it runs —
    # so enriching MORE listings directly raises the count that become
    # notification-eligible. Set above deal_radar_max_evaluations: enrich a
    # wider set for timestamps, then VLM only the freshest top-N. Bounded by
    # the 600s cycle timeout + per-listing 20s timeout + early-bail.
    # See docs/decisions/enrichment-cap-decoupled-from-eval-cap.md.
    patrol_enrichment_cap: int = 75
    # Aggregate wall-clock budget for the detail-page enrichment phase. The
    # candidate list is freshness-sorted (newest-first), so when a high-volume
    # cycle would enrich more listings than fit in the cycle, this budget stops
    # enrichment and proceeds with the freshest already enriched — turning a
    # hard 600s cycle abandonment (which drops the WHOLE cycle, deals included)
    # into graceful partial completion. 0 disables the bound. Sized well inside
    # the 600s scheduler ceiling, leaving headroom for sweep + VLM + notify.
    # See docs/incidents/enrichment-no-time-budget-cycle-abandonment.md.
    patrol_enrichment_max_seconds: float = 300.0
    # Aggregate wall-clock budget for the VLM evaluation phase (evaluate_batch).
    # A degraded/rate-limited cascade on a full survivor batch could otherwise
    # run past the 600s cycle ceiling. On timeout the cycle proceeds with no
    # deals from this batch rather than being abandoned. 0 disables the bound.
    patrol_evaluation_max_seconds: float = 180.0
    # Cycle-level soft deadline (under the 600s scheduler hard ceiling). The
    # per-phase budgets above are independent, so on a HEAVY cycle (lots of
    # fresh inventory) sweep + enrichment + eval can sum past 600s and the
    # cycle is abandoned — losing its deals on exactly the highest-value
    # cycles. Clamping enrichment AND eval to this shared deadline guarantees
    # the cycle completes-partial (deferring leftovers to backlog) instead of
    # abandoning. Keep < the scheduler's 600s timeout with margin. 0 disables.
    patrol_cycle_soft_budget_seconds: float = 540.0
    # Reliability watchdog. A wedged CDP get_page() does NOT honor
    # asyncio.wait_for cancellation, so the per-cycle timeout abandons a hung
    # cycle but cannot recover the browser — every subsequent cycle re-wedges
    # (observed: ~18.5h of zero output on 2026-05-31). After this many
    # CONSECUTIVE failed cycles the scheduler force-exits the process so the
    # container restart policy (unless-stopped) recovers a fresh session.
    # Bounds worst-case zero-output to N x the cycle timeout (~N x 10min).
    # See docs/incidents/main-browser-cdp-wedge-infinite-hang.md.
    patrol_max_consecutive_failures: int = 3
    # The watchlist DOM fallback drives the MAIN browser and was the ONE
    # unprotected CDP path — an unbounded navigation hung the whole 600s cycle
    # when the main browser's CDP wedged (~every 7h), causing repeated watchdog
    # force-exits and a 15.5h dark-out (2026-06-02). Bound it like the other
    # DOM sweeps. See docs/incidents/watchlist-sweep-unprotected-cdp-wedge.md.
    patrol_watchlist_dom_timeout_s: float = 60.0
    # Preemptive main-browser recycle. The main browser's CDP session ages into
    # a wedge over ~5.5-7h; recycle it on this interval (BETWEEN cycles, where
    # stop/start is safe) BEFORE it wedges, re-importing+persisting the live
    # cookies so the fresh session stays authenticated. 0 disables.
    patrol_main_browser_max_age_s: float = 14400.0  # 4h, below the ~7h wedge age
    # Proactive health heartbeat: DM the owner when the scanner is structurally
    # dark — recovered-from-forced-restart, FB auth FAILED (fresh feed dark,
    # needs a cookie re-seed), or a long delivery silence — so a 15h dark-out is
    # never silent again. discord_owner_user_id==0 disables it.
    # See docs/decisions/proactive-health-heartbeat.md.
    patrol_health_check_interval_s: int = 300
    patrol_no_delivery_alert_hours: float = 6.0
    # Memory-pressure guard. Chromium leaks renderer processes over hours
    # (audit 2026-05-30: ~5.6 GB across ~22 procs, climbing to the 4 GiB cap),
    # which both risks an OOM-kill AND degrades the CDP session into the wedge
    # above. browser-use spawns chromium internally, so surgical process reaping
    # would be a fragile heuristic that could kill the LIVE browser; instead,
    # when container memory crosses this fraction of the cgroup limit the
    # scheduler force-exits between cycles so the restart policy brings up a
    # fresh process with fresh chromium (memory reset). 0 disables the guard.
    patrol_memory_restart_pct: float = 0.85
    # Don't trigger the memory guard within this many seconds of process start
    # (avoids any startup-spike restart loop; the leak builds over hours).
    patrol_memory_restart_min_uptime_s: float = 600.0
    # In-process browser recycle (the FIRST line of defense, well below the
    # os._exit guard above). When memory crosses THIS softer fraction, recycle
    # just the chromium browsers between cycles — a clean stop()+start() kills
    # the leaked renderers and respawns fresh while the persistent profile keeps
    # the FB session, so Discord/voice/the process stay up. The whole-process
    # os._exit (patrol_memory_restart_pct) becomes a last-resort backstop for
    # non-browser memory only. 0 disables the in-process recycle.
    # See docs/decisions/in-process-browser-recycle.md.
    patrol_memory_recycle_pct: float = 0.70
    # Throttle: never recycle browsers more often than this (avoids thrashing if
    # memory stays high from a source a browser recycle can't reclaim).
    patrol_browser_recycle_min_interval_s: float = 600.0
    # Voice-priority backoff. poob runs the voice pipeline + this scanner in one
    # process on a CPU-only box; a chromium patrol cycle during multi-user voice
    # starves real-time wake/STT/TTS. When True, the scheduler skips a cycle
    # while voice processed audio within the last window, and resumes once voice
    # is quiet (no scanner throughput lost outside VC). Set False to disable.
    # See docs/decisions/patrol-backoff-during-voice.md.
    patrol_skip_during_voice: bool = True
    patrol_voice_activity_window_s: float = 120.0
    # FB session self-refresh. Facebook rolls the session token as the
    # authenticated browser is used; a real browser stays logged in for weeks
    # because it keeps the rolled token. We import a one-time cookie snapshot,
    # so without this the session reverts to the stale original on every
    # restart and dies in days. This interval throttles how often the patrol
    # engine captures the LIVE (rolled) cookies back to the volume so restarts
    # reload the freshest session. 0 disables.
    # See docs/decisions/fb-session-cookie-persistence.md.
    patrol_cookie_persist_interval_s: int = 1800

    # --- Visual Enrichment (reverse image search) ---
    google_cloud_vision_api_key: str = ""  # Google Cloud Vision API key
    google_cloud_vision_enabled: bool = True  # Enable Vision API enrichment
    google_cloud_vision_monthly_quota: int = 1000  # Free tier: 1,000 calls/month
    serpapi_api_key: str = ""  # SerpAPI key for Google Lens products
    serpapi_enabled: bool = True  # Enable SerpAPI Google Lens enrichment
    serpapi_monthly_quota: int = 250  # Free tier: 250 searches/month

    # --- VLM Evaluator ---
    # Cascade order/membership is authoritative in src/poob/llm/vlm_cascade.py
    # (build_vlm_cascade); there is intentionally no provider-list config knob.
    # Two unused provider-list fields were removed 2026-05-30 (they were never
    # read — a no-op tuning trap). See docs/decisions/vlm-cascade-dead-rung-cleanup.md.
    vlm_max_images: int = 2  # Images per evaluation (2 = hero + detail, optimal)
    vlm_confidence_threshold: float = 0.6  # Below this, request second opinion
    vlm_second_opinion_enabled: bool = True  # Enable confidence-based escalation
    vlm_evaluation_timeout: int = 30  # Timeout per VLM call (seconds)
    vlm_voting_enabled: bool = True  # Enable parallel voting (3 diverse providers)
    vlm_voting_panel_size: int = 3  # Number of providers in voting panel
    vlm_value_agreement_threshold: float = 0.15  # Max CV for value agreement (15%)

    # --- Gemini VLM (separate from agent Gemini — different quota) ---
    gemini_flash_model: str = "gemini-3-flash-preview"  # VLM primary (rank 4, score 1274)
    gemini_pro_model: str = "gemini-2.5-pro"  # VLM tiebreaker (no free Gemini 3 Pro)
    gemini_flash_rpd: int = 250  # Daily limit tracking
    gemini_pro_rpd: int = 100  # Daily limit tracking
    # Gemini 3.1 Flash Lite: 15 RPM, 1000 RPD free — high-volume voter (rank 35, score 1188)
    gemini_flash_lite_model: str = "gemini-3.1-flash-lite-preview"
    gemini_flash_lite_rpd: int = 1000

    # --- Mistral (Pixtral 12B, 1 RPS free tier) ---
    mistral_api_key: str = ""

    # --- Google Custom Search (100 queries/day free) ---
    google_cse_api_key: str = ""
    google_cse_cx: str = ""  # Custom Search Engine ID

    # --- Mojeek (2,000 queries/day free) ---
    mojeek_api_key: str = ""

    # --- OpenRouter (multi-model aggregator, free tier available) ---
    openrouter_api_key: str = ""
    # The primary OpenRouter VLM rung was removed 2026-05-30 (its model 404'd
    # "No endpoints found"); the Nemotron secondary serves the OpenRouter/NVIDIA
    # fallback. See docs/decisions/vlm-cascade-dead-rung-cleanup.md.
    openrouter_model_secondary: str = "nvidia/nemotron-nano-12b-v2-vl:free"  # v2 fallback
    openrouter_enabled: bool = True

    # --- Text Triage (batch pre-filter) ---
    triage_batch_size: int = 5  # Listings per triage prompt
    triage_provider: str = "cerebras"  # Primary: cerebras, fallback: groq, failsafe: ollama

    # --- Deal Notification ---
    notification_max_per_day: int = 10  # Cap notifications per user per day (alert fatigue)
    notification_include_distance: bool = True  # Show distance in notifications

    # --- Enrichment + Comparables ---
    enrichment_tavily_enabled: bool = True  # Use Tavily for comparable sales after enrichment
    enrichment_tavily_only_with_product_name: bool = (
        True  # Only search when enrichment found a name
    )

    # --- Conversational Agent ---
    # Agent brain cascade: Groq GPT-OSS 120B → Groq Llama 70B → NVIDIA NIM → Gemini 3 Flash → Ollama.
    agent_llm_provider: str = "auto"  # Unused; agent always cascades fastest-first
    agent_max_history: int = 20
    agent_max_tool_iterations: int = 10

    # --- Timezone ---
    display_timezone: str = "America/Chicago"  # Central Time — used for peak hours + display

    # --- Storage ---
    database_path: Path = Path("data/scraper.db")
    log_dir: Path = Path("data/logs")
    log_level: str = "INFO"
    quota_db_path: Path = Path("data/quota_state.db")  # Persistent quota tracking
    cache_dir: Path = Path("data/cache")  # Perceptual hash cache directory
    cache_size_limit: int = 1_073_741_824  # Max cache size (1GB default)

    # --- Browser Pool ---
    browser_pool_size: int = 1  # Number of browser contexts (1=single, 3+=pool)
    browser_pool_cooldown_seconds: float = 3600.0  # Ban cooldown per slot (1hr)

    # --- Patrol ---
    patrol_enabled: bool = True
    # Auto-start the patrol scheduler loop on bot startup. When False, patrols
    # only run when the user types "scan" or !scan. "Just listed" detection is
    # incompatible with manual triggering, so this defaults to True.
    patrol_scheduler_auto_start: bool = True
    patrol_sweep_mode: str = "unified"  # "unified" (1 page) or "categories" (10+ pages)
    patrol_categories: list[str] = [
        "electronics",
        "furniture",
        "sports",
        "garden",
        "appliances",
        "free",
        "toys",
        "apparel",
        "entertainment",
    ]
    patrol_base_radius_miles: int = 40
    patrol_radius_jitter: int = 4
    patrol_radius_oscillation_enabled: bool = False
    patrol_inter_category_delay_min_ms: int = 8000  # Was 15000, reduced for faster watchlist sweeps
    patrol_inter_category_delay_max_ms: int = 12000  # Was 25000, still stealthy
    patrol_inter_listing_delay_min_ms: int = 3000
    patrol_inter_listing_delay_max_ms: int = 7000
    # Polling intervals: research shows FB indexing is 3-15 min (eventual consistency)
    patrol_peak_interval_seconds: int = 180  # 3 min (was 5 min)
    patrol_moderate_interval_seconds: int = 300  # 5 min (was 10 min, 2026-05-25 — more chances to catch fresh listings before they age out)
    patrol_offpeak_interval_seconds: int = 900  # 15 min
    patrol_dead_interval_seconds: int = 1800  # 30 min
    patrol_peak_hours_start: int = 16
    patrol_peak_hours_end: int = 21
    patrol_include_all_categories: bool = True
    patrol_days_since_listed: int = 1
    # Evaluation cutoff: listings older than this are dropped from the VLM pipeline.
    # Empirically settled at 6h after the 1h and 3h attempts both produced
    # vlm_evaluated=0 over multi-hour windows: FB's anon ranked feed surfaces
    # mostly stale-popular content, so the eval pool needs to be wide enough
    # to contain SOME listings or VLM never runs. The post-enrichment
    # FreshnessFilter still rejects no-posted_at listings — that piece is
    # load-bearing and stays. The user-facing notification gates
    # (10-min public / 30-min watchlist) are what enforce "just listed"
    # at product level; eval cutoff is just the rough floor that keeps
    # VLM from burning credit on day-old garbage. See
    # docs/decisions/triage-freshness-converge-with-notify.md.
    listing_max_age_hours: int = 6
    # Public channel notification cutoff: a deal reaching the public
    # #facebook-marketplace channel must be posted within this many minutes.
    # This is the "just listed AND clearly worth something" product rule.
    public_notification_max_age_minutes: int = 10
    # Adaptive public freshness when the authenticated feed is DOWN. The strict
    # 10-min "just-listed" bar above is only achievable from the authenticated
    # "newest near you" feed; when that feed is unavailable (dead session) the
    # only live source is the staler anonymous feed, so the strict bar yields
    # ZERO notifications. When auth is down, relax to this window so INCREDIBLE
    # deals still reach the channel (operator-chosen graceful degradation,
    # 2026-06-02) instead of nothing. See
    # docs/decisions/adaptive-public-freshness-when-auth-down.md.
    public_notification_max_age_no_auth_minutes: int = 60
    # NOTE: watchlist DMs are intentionally NOT freshness-gated — a wishlist is
    # about the item being available, not just-listed. The per-item
    # notification_threshold is the sole gate. See
    # docs/decisions/watchlist-honors-threshold-not-freshness.md.
    patrol_deep_inspect_enabled: bool = True
    # --- Anonymous GraphQL ---
    patrol_anonymous_graphql_enabled: bool = True  # Try depersonalized GraphQL before browser
    patrol_anonymous_browse_categories: list[str] = [
        "",  # Empty query = general browse (returns few but free)
        "electronics",
        "furniture",
        "appliances",
        "free stuff",
        "sporting goods",
        "toys",
        "tools",
    ]
    patrol_anonymous_browser_enabled: bool = True  # Separate headless browser for DOM sweep
    # Establish an authenticated FB session on the main browser at startup
    # (cookie-reuse if a session persists, else a one-time headless login with
    # the .env creds). Never solves CAPTCHAs — checkpoint => fall back to anon.
    # See docs/decisions/authenticated-session-no-human.md.
    patrol_authenticated_login_enabled: bool = True
    # Rate limit: min delay between GQL requests. Restored 6->12s on
    # 2026-05-29: 6s was tripping FB's anon rate limit hard (consecutive_hits
    # >130, 177/182 cycles returned zero). Halving the request RATE is the
    # $0 lever to stay under the limit. See
    # docs/decisions/multi-center-general-browse.md.
    patrol_graphql_min_delay_seconds: float = 12.0
    patrol_graphql_max_pages: int = 3  # Max pagination pages per search (3 * 50 = ~150 listings)
    patrol_graphql_watchlist_enabled: bool = True  # Use GraphQL for watchlist searches too
    patrol_graphql_concurrency: int = 2  # Concurrent GraphQL search streams (2-3 safe for anon)
    # --- Watchlist Sweep ---
    patrol_watchlist_sweep_enabled: bool = True  # Search FB for each watchlist item per cycle
    patrol_watchlist_max_items: int = 50  # All items every cycle (was 10)
    marketplace_default_location: str = "madison"  # FB city slug for search URL path
    # General-browse search centers (NOT watchlist — those carry their own
    # bounds). Each cycle rotates to ONE of these so POST volume per cycle
    # stays flat (one center x N categories) while coverage spans all centers
    # across consecutive cycles — the $0 way to cover two metros without
    # doubling GQL requests. The GeoDistanceFilter accepts listings within
    # radius of ANY of these. See docs/decisions/multi-center-general-browse.md.
    patrol_browse_locations: list[str] = ["madison", "appleton"]
    # Geo-filter center point: auto-resolved from marketplace_default_location
    # if left at 0.0. Set explicitly to override (e.g., for a user in a non-mapped city).
    patrol_center_lat: float = 0.0
    patrol_center_lon: float = 0.0
    # --- Scroll Depth ---
    patrol_scroll_steps_category: int = 20  # DOM scroll steps for category sweep (was 5)
    patrol_scroll_steps_search: int = 30  # DOM scroll steps for keyword search (was 15)
    patrol_scroll_until_stable: bool = True  # Keep scrolling until no new listings load
    patrol_scroll_max_stable_checks: int = 3  # Stop after N scrolls with no new content
    # --- Stealth ---
    stealth_viewport_randomize: bool = True
    stealth_mouse_movement: bool = True
    stealth_inject_scripts: bool = True

    # --- Music ---
    music_enabled: bool = True  # Enable music bot integration
    music_volume: float = 0.5  # Default music volume (0.0-1.0)
    music_idle_timeout: float = 300.0  # Seconds idle before auto-stop (0 = disabled)
    music_ytdl_cookie_file: str = ""  # Path to cookies.txt for age-restricted content
    music_duck_volume: float = 0.25  # Music volume when TTS is active (0.0-1.0)

    # --- Voice Chat ---
    voice_enabled: bool = True  # Enable voice channel integration
    voice_stt_provider: str = "groq_whisper"  # groq_whisper | local_whisper
    # Neural VAD opt-in. Default False keeps the energy-RMS gate as the
    # active end-of-speech detector (matches behavior pre-refactor). Flip
    # to True after validating the shared-model Silero path in a real
    # voice channel. See docs/decisions/voice-latency-phase1-silero-reenabled.md.
    voice_use_silero_vad: bool = False
    voice_tts_provider: str = "google_tts"  # google_tts | edge_tts | kokoro
    voice_tts_voice: str = "en-US-RogerNeural"  # Edge TTS voice name (fallback)
    voice_tts_rate: str = "+35%"  # Speech rate for Edge TTS (fallback, match Fenrir pacing)
    voice_google_tts_voice: str = "en-US-Chirp3-HD-Fenrir"  # Google Chirp3-HD — natural, expressive
    voice_google_tts_rate: float = 1.25  # Faster for natural conversational pacing
    # Latency-mask "thinking noise" filler (Phase 2). Pre-generated once at
    # startup and cached; played before a response to mask LLM+TTS delay.
    # Generated in Poob's own Google voice by default so the noise sounds like
    # Poob, not a stranger. Operator knobs (test without code changes):
    voice_filler_enabled: bool = True
    voice_filler_voice: str = ""  # empty → use voice_google_tts_voice (Fenrir)
    voice_silence_threshold_ms: int = (
        350  # Silence duration before utterance ends (snappier turn-taking)
    )
    voice_energy_threshold: float = 150.0  # RMS energy threshold for speech detection
    voice_min_speech_ms: int = 200  # Minimum speech duration to avoid spurious triggers
    voice_max_utterance_seconds: int = 30  # Max single utterance length
    # 2026-08-26: was "llama-3.1-8b-instant" — Groq removed it from their
    # catalog entirely (confirmed via a live models.list() call, not a
    # transient 429/503). This field also was not being passed into
    # PoobBrain's constructor (see main.py), so changing it here previously
    # had NO effect; every call site hardcoded the literal directly. Both are
    # now fixed — this is the actual value in effect. gpt-oss-20b was
    # benchmarked FASTER (~485ms vs ~720ms) than the dead model in
    # docs/research/free-llm-tier-audit-2026-06.md, with no clearly-better
    # free alternative found for this quick-reaction voice role.
    # 2026-09-06: qwen3.8-27b — measured faster (438ms vs 601ms median) and a
    # better persona fit than gpt-oss-20b, and critically a DIFFERENT model
    # from groq_model so voice replies stop sharing its 8k TPM bucket.
    # See docs/research/voice-model-eval-2026-09-06.md.
    voice_llm_model: str = "qwen/qwen3.8-27b"  # Fast Groq model for voice
    voice_llm_max_tokens: int = 200  # Allow 2-4 sentences for more natural conversation
    voice_conversation_max_history: int = 10  # Rolling context window

    # --- Site Credentials ---
    facebook_email: str = ""
    facebook_password: str = ""
