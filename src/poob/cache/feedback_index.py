"""Feedback-based dynamic few-shot example selection.

Stores past VLM evaluations with user feedback (claimed/overpriced/scam)
and retrieves semantically similar examples for new listings.
Prioritizes false positive examples (overpriced/scam reactions).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from poob.utils.logging import get_logger

log = get_logger("cache.feedback_index")


@dataclass
class FeedbackExample:
    """A past evaluation with user feedback for few-shot prompting.

    Attributes:
        listing_title: Title of the evaluated listing.
        listing_price: Listed price.
        vlm_output: The VLM's evaluation output.
        feedback_type: User reaction (claimed/overpriced/scam/not_interested).
        deal_quality: What the VLM rated this.
        estimated_value: VLM's estimated market value.
        provider_used: Which VLM provider produced this evaluation.
        created_at: When the evaluation was made.
    """

    listing_title: str = ""
    listing_price: float = 0.0
    vlm_output: dict = field(default_factory=dict)
    feedback_type: str = ""
    deal_quality: str = ""
    estimated_value: float = 0.0
    provider_used: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_few_shot_text(self) -> str:
        """Format this example as text for inclusion in a VLM prompt."""
        correctness = "CORRECT" if self.feedback_type == "claimed" else "INCORRECT"
        return (
            f"Example ({correctness} — user reacted '{self.feedback_type}'):\n"
            f"  Title: {self.listing_title}\n"
            f"  Price: ${self.listing_price}\n"
            f"  VLM rated: {self.deal_quality}, est. value: ${self.estimated_value:.0f}\n"
            f"  Lesson: {'Good call' if self.feedback_type == 'claimed' else 'This was a false positive — the VLM overrated this deal.'}"
        )
