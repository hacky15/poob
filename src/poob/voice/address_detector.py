"""Multi-signal fusion address detector for multi-party voice conversations.

Determines whether Poob is being addressed using a research-backed
two-pass architecture that combines multiple weak signals into a
reliable classifier operating within <100ms.

Architecture based on:
- Shriberg et al. (2012) H-H-C dialogue addressee detection
- Exchange-based decay (Arguello & Rosé 2006, AAAI 2021)
- Semantic relevance scoring via Model2Vec embeddings
- Multi-signal weighted fusion (Webb 2025 "Enthusiasm" scoring)

Pass 1 — Instant deterministic filters (<1ms):
  Wake word → RESPOND (override)
  Other user name as addressee → DON'T RESPOND (override)

Pass 2 — Parallel signal scoring (~5-50ms):
  - Bot was last speaker (state lookup)
  - Bot's last utterance was a question (state lookup)
  - Semantic relevance to bot's last response (Model2Vec cosine sim)
  - Exchange count decay since bot spoke (exponential decay)
  - Question/command with "you" (regex heuristics)
  - Other user name mentioned (negative signal)

Fusion: Weighted sum → three-tier threshold decision.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

import numpy as np

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from numpy.typing import NDArray

log = get_logger("voice.address")

# ---------------------------------------------------------------------------
# Wake word patterns — Whisper commonly mishears "Poob" as these
# ---------------------------------------------------------------------------
# Wake word variants — every STT engine mishears "Poob" differently.
# Confirmed from real Discord transcriptions:
#   Groq Whisper: Boob, Boop, Poop, Poob, Pube
#   Deepgram: Hoob, Poob, Poop, Hoove, Pube
#   General: Poub, Pewb, Puub, Pub, Noob, Goop
_WAKE_WORDS = re.compile(
    r"\b(hey\s+)?(poob|poop|poub|poobe?y|poo[bp]?|boob|boop|pube?|pewb|puub|"
    r"hoob|hoove?|noob|goop|goob|doob|toob|roob|moob|koob|loob|foob|"
    r"p[ou]{1,2}[bp]e?)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Patterns indicating a question or command directed at "you"
# ---------------------------------------------------------------------------
_YOU_QUESTION = re.compile(
    r"\b(what do you|how do you|can you|do you|are you|did you|would you|"
    r"could you|will you|don'?t you|why do you|why did you|tell me|"
    r"what'?s your|how'?s your)\b",
    re.IGNORECASE,
)

# Patterns for explicit addressee of another user ("hey Simon", "Simon,")
_EXPLICIT_ADDRESS = re.compile(
    r"^(hey|yo|ok|okay|alright|so|well|um|uh)\s+{name}\b|^{name}\s*[,!]",
    re.IGNORECASE,
)


class MultiSignalAddressDetector:
    """Research-backed multi-signal fusion system for addressee detection.

    Combines 7 signals with calibrated weights to determine if an utterance
    in a multi-party voice channel is directed at the bot.

    The system tracks conversation state (who spoke last, exchange count,
    bot's last response embedding) and scores each incoming utterance
    against all signals in parallel.
    """

    def __init__(self) -> None:
        # Conversation state
        self._bot_was_last_speaker: bool = False
        self._bot_asked_question: bool = False
        self._exchanges_since_bot: int = 999  # Start high = not in conversation
        self._bot_last_response: str = ""
        self._bot_last_embedding: NDArray | None = None

        # Channel member names (for negative signal)
        self._member_names: set[str] = set()

        # Model2Vec for semantic similarity (lazy-loaded). _embed_load_failed
        # latches a failed import so we don't retry + re-warn every decision.
        self._embed_model = None
        self._embed_load_failed = False

        # Signal weights (calibrated from research — Shriberg 2012, Webb 2025)
        self._weights = {
            "wake_word": 1.0,       # Binary override — always respond
            "bot_last_speaker": 0.22,
            "bot_asked_question": 0.08,
            "semantic_relevance": 0.18,
            "exchange_decay": 0.12,
            "you_question": 0.08,
            "other_name": -0.20,    # Negative — strong signal NOT addressed
        }

        # Decision thresholds — tuned from live 5-person call testing
        # Without wake word, ONLY respond if strong semantic match + you-question
        # Wake word always overrides (score=1.0)
        self._threshold_high = 0.60    # Above this → respond always
        self._threshold_mid = 0.60     # Same as high — no "mid-tier" ambiguity
        # Below 0.60 → don't respond (wake word is the only reliable path)

    def _ensure_model(self) -> None:
        """Lazy-load Model2Vec on first use.

        Idempotent and self-limiting: a failed load sets ``_embed_load_failed``
        so we don't retry the import (and re-log the warning) on every
        addressee decision. The model is pre-baked into the image
        (Dockerfile 4c); a failure here means the dep/model is genuinely
        absent and semantic scoring stays disabled for the session.
        """
        if self._embed_model is not None or self._embed_load_failed:
            return
        try:
            from model2vec import StaticModel
            self._embed_model = StaticModel.from_pretrained(
                "minishlab/potion-base-8M"
            )
            log.info("Model2Vec loaded for semantic scoring")
        except Exception as exc:
            self._embed_load_failed = True
            log.warning("Model2Vec unavailable, semantic scoring disabled", error=str(exc)[:80])

    def prewarm(self) -> None:
        """Eagerly load the Model2Vec model (otherwise lazy on the first
        addressee decision, ~tens of seconds into a session). Idempotent;
        VoiceSession runs it off-thread on join. See
        incidents/voice-pipeline-cold-start-drops-requests."""
        self._ensure_model()

    # ----- State update methods (called by VoiceSession) -----

    def update_member_names(self, names: set[str]) -> None:
        """Update the set of human member names in the channel.

        Used for the negative signal: if someone says "hey Simon",
        it's addressed to Simon, not Poob.
        """
        self._member_names = {n.lower() for n in names if n}

    def mark_bot_spoke(self, response_text: str) -> None:
        """Call after Poob finishes a response.

        Updates state for: bot_was_last_speaker, exchange counter,
        question detection, and caches response embedding.

        Sets exchange counter to 2 so the NEXT utterance gets decay=0.25
        instead of 1.0. Combined with bot_last_speaker(0.22), total is
        only 0.25 — well below the 0.60 threshold. Requires wake word
        or very strong semantic + you-question to trigger.
        """
        self._bot_was_last_speaker = True
        self._exchanges_since_bot = 2  # Start at 2 — decay=0.25, prevents chain-responding
        self._bot_last_response = response_text

        # Check if Poob asked a question
        self._bot_asked_question = bool(re.search(r"\?", response_text))

        # Pre-compute embedding for semantic similarity
        self._ensure_model()
        if self._embed_model is not None and response_text.strip():
            try:
                self._bot_last_embedding = self._embed_model.encode(
                    [response_text.strip()[:200]]
                )[0]
            except Exception:
                self._bot_last_embedding = None

    def mark_human_spoke(self, addressed_bot: bool) -> None:
        """Call after processing any human utterance.

        If the human was NOT addressing the bot, increment the exchange
        counter. If they WERE addressing the bot, reset it.
        """
        if addressed_bot:
            self._exchanges_since_bot = 0
        else:
            self._exchanges_since_bot += 1
            # After enough non-bot exchanges, clear the "bot was last speaker" flag
            if self._exchanges_since_bot >= 4:
                self._bot_was_last_speaker = False
                self._bot_asked_question = False

    # ----- Signal computation methods -----

    def _check_wake_word(self, text: str) -> float:
        """Check for explicit wake word. Returns 1.0 or 0.0."""
        return 1.0 if _WAKE_WORDS.search(text) else 0.0

    def _check_other_name(self, text: str) -> float:
        """Check if another user's name is explicitly addressed.

        "Hey Simon" or "Simon, what do you think" → 1.0 (negative signal).
        Returns 1.0 if another name found as addressee, 0.0 otherwise.
        """
        if not self._member_names:
            return 0.0
        text_lower = text.lower().strip()
        for name in self._member_names:
            # Check "hey <name>" or "<name>," at start of utterance
            pattern = rf"^(hey|yo|ok|okay|so|well)\s+{re.escape(name)}\b|^{re.escape(name)}\s*[,!]"
            if re.search(pattern, text_lower):
                return 1.0
        return 0.0

    def _compute_semantic_relevance(self, text: str) -> float:
        """Cosine similarity between utterance and bot's last response.

        Returns 0.0-1.0. Returns 0.0 if no prior bot response or model unavailable.
        """
        if self._bot_last_embedding is None or self._embed_model is None:
            return 0.0
        try:
            utterance_emb = self._embed_model.encode([text.strip()[:200]])[0]
            # Cosine similarity
            dot = np.dot(self._bot_last_embedding, utterance_emb)
            norm_a = np.linalg.norm(self._bot_last_embedding)
            norm_b = np.linalg.norm(utterance_emb)
            if norm_a == 0 or norm_b == 0:
                return 0.0
            sim = float(dot / (norm_a * norm_b))
            return max(0.0, sim)  # Clamp to [0, 1]
        except Exception:
            return 0.0

    def _compute_exchange_decay(self) -> float:
        """Exponential decay based on exchanges since bot spoke.

        0 exchanges → 1.0, 1 → 0.50, 2 → 0.25, 3 → 0.12, 5 → 0.03
        """
        return math.exp(-0.7 * self._exchanges_since_bot)

    def _check_you_question(self, text: str) -> float:
        """Check if text contains a question/command pattern with 'you'."""
        return 1.0 if _YOU_QUESTION.search(text) else 0.0

    # ----- Main scoring method -----

    def score(self, text: str) -> tuple[float, dict[str, float]]:
        """Compute the address probability score for an utterance.

        Returns:
            (score, signals_dict) where score is 0.0-1.0+ and
            signals_dict maps signal names to their raw values.
        """
        signals = {
            "wake_word": self._check_wake_word(text),
            "bot_last_speaker": 1.0 if self._bot_was_last_speaker else 0.0,
            "bot_asked_question": 1.0 if self._bot_asked_question else 0.0,
            "semantic_relevance": self._compute_semantic_relevance(text),
            "exchange_decay": self._compute_exchange_decay(),
            "you_question": self._check_you_question(text),
            "other_name": self._check_other_name(text),
        }

        # Weighted fusion
        total = sum(
            self._weights[name] * value
            for name, value in signals.items()
        )

        return total, signals

    def should_respond(
        self,
        text: str,
    ) -> tuple[bool, float, dict[str, float]]:
        """Full address detection decision.

        Args:
            text: The transcribed utterance.

        Returns:
            (should_respond, score, signals_dict)
        """
        # Pass 1: Instant deterministic filters
        if _WAKE_WORDS.search(text):
            return True, 1.0, {"wake_word": 1.0}

        # Pass 2: Multi-signal fusion
        score, signals = self.score(text)

        # Three-tier decision
        if score >= self._threshold_high:
            return True, score, signals

        if score >= self._threshold_mid and self._bot_was_last_speaker:
            return True, score, signals

        return False, score, signals
