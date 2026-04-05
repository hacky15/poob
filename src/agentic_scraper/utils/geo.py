"""Geo-distance utilities for filtering marketplace listings by proximity.

Uses the haversine formula for great-circle distance. Accurate to ~0.3%
vs the WGS-84 ellipsoidal model — well within the precision of marketplace
listing locations (typically neighborhood-level, ~0.5-1 mile).
"""

from __future__ import annotations

import math

# Earth's mean radius in miles (WGS-84 derived).
_EARTH_RADIUS_MILES = 3958.8


def haversine_miles(
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> float:
    """Great-circle distance between two points in miles.

    Args:
        lat1, lon1: First point in decimal degrees.
        lat2, lon2: Second point in decimal degrees.

    Returns:
        Distance in miles.
    """
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = lat2_r - lat1_r
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    )
    return _EARTH_RADIUS_MILES * 2 * math.asin(math.sqrt(a))


def has_valid_coordinates(lat: float | None, lon: float | None) -> bool:
    """Check if coordinates are usable (not None, not null island, in range).

    Args:
        lat: Latitude in decimal degrees (or None).
        lon: Longitude in decimal degrees (or None).

    Returns:
        True if coordinates are valid and usable for distance calculation.
    """
    if lat is None or lon is None:
        return False
    # Null island (0, 0) in the Gulf of Guinea — no legitimate US marketplace listings.
    if lat == 0.0 and lon == 0.0:
        return False
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return False
    return True


# US state approximate centroids (latitude, longitude).
# Used for pre-enrichment location text filtering — rejects listings whose
# state centroid is impossibly far from the user's configured search center.
# This is a coarse filter (~100-200 mile accuracy) that runs BEFORE the
# expensive detail page enrichment step.
_US_STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "AL": (32.8, -86.8), "AK": (64.0, -153.0), "AZ": (34.3, -111.7),
    "AR": (34.8, -92.2), "CA": (37.2, -119.5), "CO": (39.0, -105.5),
    "CT": (41.6, -72.7), "DE": (39.0, -75.5), "FL": (28.6, -82.4),
    "GA": (32.7, -83.4), "HI": (20.5, -157.4), "ID": (44.4, -114.6),
    "IL": (40.0, -89.2), "IN": (39.9, -86.3), "IA": (42.0, -93.5),
    "KS": (38.5, -98.3), "KY": (37.8, -85.7), "LA": (31.1, -91.9),
    "ME": (45.4, -69.2), "MD": (39.0, -76.8), "MA": (42.2, -71.5),
    "MI": (44.3, -84.5), "MN": (46.3, -94.3), "MS": (32.7, -89.7),
    "MO": (38.4, -92.5), "MT": (47.1, -109.6), "NE": (41.5, -99.8),
    "NV": (39.3, -116.6), "NH": (43.7, -71.6), "NJ": (40.1, -74.7),
    "NM": (34.4, -106.1), "NY": (42.9, -75.5), "NC": (35.6, -79.4),
    "ND": (47.5, -100.4), "OH": (40.4, -82.8), "OK": (35.6, -97.5),
    "OR": (44.1, -120.5), "PA": (40.9, -77.8), "RI": (41.7, -71.5),
    "SC": (33.9, -80.9), "SD": (44.4, -100.2), "TN": (35.9, -86.4),
    "TX": (31.5, -99.3), "UT": (39.3, -111.7), "VT": (44.1, -72.6),
    "VA": (37.5, -78.9), "WA": (47.4, -120.7), "WV": (38.6, -80.6),
    "WI": (44.6, -89.8), "WY": (43.0, -107.6), "DC": (38.9, -77.0),
}

# Full state names → abbreviations for location text parsing.
_STATE_NAME_TO_ABBR: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}


def parse_state_from_location(location: str) -> str | None:
    """Extract US state abbreviation from Facebook location text.

    Handles formats like "Appleton, WI", "Madison, Wisconsin", "Green Bay, WI".

    Returns:
        2-letter state abbreviation, or None if unparseable.
    """
    if not location:
        return None
    parts = location.rsplit(",", 1)
    if len(parts) != 2:
        return None
    state = parts[1].strip()
    if not state:
        return None
    # Already a 2-letter abbreviation
    upper = state.upper()
    if len(upper) == 2 and upper in _US_STATE_CENTROIDS:
        return upper
    # Full name lookup
    lower = state.lower()
    return _STATE_NAME_TO_ABBR.get(lower)


def state_centroid_distance(
    state_abbr: str, center_lat: float, center_lon: float,
) -> float | None:
    """Distance in miles from a state's centroid to a center point.

    Returns None if state abbreviation is unknown.
    """
    centroid = _US_STATE_CENTROIDS.get(state_abbr)
    if centroid is None:
        return None
    return haversine_miles(center_lat, center_lon, centroid[0], centroid[1])
