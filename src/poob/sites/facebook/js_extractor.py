"""JavaScript extraction scripts for Facebook Marketplace DOM.

These scripts run inside the browser via page.evaluate() to extract
structured listing data directly from the DOM. No LLM needed.

Isolated in this module so DOM selector updates (when Facebook changes
their HTML structure) only require editing this file.
"""

# Extract listing data from marketplace search results.
# Finds all links to /marketplace/item/ pages, then walks up to the
# listing card container to extract title, price, location, and image.
EXTRACT_LISTINGS_JS = """() => {
    const links = Array.from(
        document.querySelectorAll('a[href*="/marketplace/item/"]')
    );

    const seen = new Set();
    const listings = [];

    for (const link of links) {
        const href = link.href.split('?')[0];
        const match = href.match(/\\/marketplace\\/item\\/(\\d+)/);
        if (!match) continue;

        const externalId = match[1];
        if (seen.has(externalId)) continue;
        seen.add(externalId);

        // Walk up to find the card container (usually 3-5 levels up)
        let card = link;
        for (let i = 0; i < 6; i++) {
            if (card.parentElement) card = card.parentElement;
        }

        // Extract text content from the card
        const allText = card.innerText || '';
        const lines = allText.split('\\n').map(s => s.trim()).filter(Boolean);

        // Price is typically the first line starting with $ or a currency symbol
        let price = null;
        let title = '';
        let location = '';
        const priceIdx = lines.findIndex(l => /^[$\\u00A3\\u20AC]/.test(l) || /^\\d+[,.]?\\d*\\s*[$\\u00A3\\u20AC]/.test(l));

        if (priceIdx >= 0) {
            const priceStr = lines[priceIdx].replace(/[^\\d.]/g, '');
            price = parseFloat(priceStr) || null;
            // Title is usually the line after price
            title = lines[priceIdx + 1] || '';
            // Location is usually 1-2 lines after title
            location = lines[priceIdx + 2] || '';
        } else {
            // Second pass: find $ anywhere in concatenated text
            // (FB sometimes omits newlines: "Just listed$200Haaka sausage stuffer")
            const embedded = allText.match(/\\$(\\d[\\d,]*\\.?\\d{0,2})/);
            if (embedded) {
                price = parseFloat(embedded[1].replace(/,/g, '')) || null;
                // Remove price + common FB prefixes from title
                title = allText
                    .replace(/(?:Just listed|Listed \\w+ ago)?\\s*\\$\\d[\\d,]*\\.?\\d{0,2}\\s*/i, '')
                    .split('\\n')[0].trim() || lines[0] || '';
                location = lines.length > 1 ? lines[lines.length - 1] : '';
            } else {
                // Fallback: first line is title
                title = lines[0] || '';
                location = lines[1] || '';
            }
        }

        // Freshness hint: "Just listed", "Listed X hours ago", etc.
        // Primary: find as a standalone line
        // Fallback: regex across full text for concatenated layouts
        let freshness = '';
        const freshLine = lines.find(l => /^(Just listed|Listed\\s)/i.test(l));
        if (freshLine) freshness = freshLine;
        if (!freshness) {
            const freshMatch = allText.match(
                /(Just listed|Listed (?:\\d+ (?:minutes?|hours?|days?|weeks?) ago|yesterday|last week))/i
            );
            if (freshMatch) freshness = freshMatch[0];
        }

        // Sponsored/promoted detection
        // innerText resolves CSS reordering that FB uses to obfuscate "Sponsored"
        let isSponsored = false;
        const lowerText = allText.toLowerCase();
        if (lowerText.includes('sponsored')) {
            isSponsored = true;
        }
        if (!isSponsored) {
            const ariaSponsored = card.querySelector('[aria-label*="Sponsored" i]');
            if (ariaSponsored) isSponsored = true;
        }
        if (!isSponsored) {
            const adLink = card.querySelector('a[href*="/ads/about"]');
            if (adLink) isSponsored = true;
        }

        // Find first image in the card
        const img = card.querySelector('img[src*="scontent"], img[src*="fbcdn"]');
        const imageUrl = img ? img.src : '';

        if (title || price !== null) {
            listings.push({
                title: title,
                price: price,
                location: location,
                listing_url: href,
                external_id: externalId,
                image_url: imageUrl,
                freshness: freshness,
                is_sponsored: isSponsored
            });
        }
    }

    return listings;
}"""

# Extract just the listing URLs (lighter weight, used as fallback).
EXTRACT_LISTING_URLS_JS = """() => {
    return Array.from(
        document.querySelectorAll('a[href*="/marketplace/item/"]')
    ).map(el => el.href.split('?')[0])
     .filter((v, i, a) => a.indexOf(v) === i);
}"""

# Scroll down by a given number of pixels. Returns the new scroll height.
SCROLL_DOWN_JS = """(pixels) => {
    window.scrollBy(0, pixels);
    return document.body.scrollHeight;
}"""

# Extract Open Graph meta tags from a listing detail page.
# OG tags provide title, description, image, and canonical URL
# without requiring JavaScript rendering.
EXTRACT_OPEN_GRAPH_JS = """() => {
    const meta = {};
    document.querySelectorAll('meta[property^="og:"]').forEach(el => {
        const key = el.getAttribute('property').replace('og:', '');
        meta[key] = el.getAttribute('content') || '';
    });
    return meta;
}"""

# Extract JSON-LD structured data from a listing detail page.
# Facebook injects Product schema with price, condition, and availability.
EXTRACT_JSON_LD_JS = """() => {
    const scripts = Array.from(
        document.querySelectorAll('script[type="application/ld+json"]')
    );
    for (const script of scripts) {
        try {
            const data = JSON.parse(script.textContent);
            if (data['@type'] === 'Product' || data['@type'] === 'Offer') {
                return data;
            }
            if (data.offers && typeof data.offers === 'object' && !Array.isArray(data.offers)) {
                return data.offers;
            }
            if (Array.isArray(data.offers) && data.offers.length > 0 && typeof data.offers[0] === 'object') {
                return data.offers[0];
            }
        } catch (e) {}
    }
    return {};
}"""
