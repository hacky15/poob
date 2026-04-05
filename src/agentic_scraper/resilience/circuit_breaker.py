"""Circuit breakers for external API calls.

Wraps aiobreaker to provide automatic failure detection and recovery
for all external services (VLM providers, Vision API, SerpAPI, search APIs).
When a service fails repeatedly, the circuit opens and calls fail fast
for a recovery period before retrying.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, TypeVar

from aiobreaker import CircuitBreaker, CircuitBreakerError

from agentic_scraper.utils.logging import get_logger

log = get_logger("resilience.circuit_breaker")

T = TypeVar("T")


class ServiceCircuitBreaker:
    """Circuit breaker for a single external service.

    Opens after `fail_max` consecutive failures, stays open for
    `recovery_timeout` seconds, then enters half-open state to test
    if the service has recovered.

    Args:
        name: Service name for logging.
        fail_max: Number of failures before opening the circuit.
        recovery_timeout: Seconds to wait before retrying after circuit opens.
    """

    def __init__(
        self,
        name: str,
        fail_max: int = 3,
        recovery_timeout: float = 60.0,
    ) -> None:
        self.name = name
        self._breaker = CircuitBreaker(
            fail_max=fail_max,
            timeout_duration=recovery_timeout,
        )

    async def call(
        self,
        func: Callable[..., Coroutine[Any, Any, T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute a function through the circuit breaker.

        Args:
            func: Async function to call.
            *args: Positional arguments.
            **kwargs: Keyword arguments.

        Returns:
            The function's return value.

        Raises:
            CircuitBreakerError: If the circuit is open.
            Exception: If the function raises and circuit is still closed.
        """
        try:
            result = await self._breaker.call_async(func, *args, **kwargs)
            return result
        except CircuitBreakerError:
            log.warning(
                "Circuit breaker open, failing fast",
                service=self.name,
            )
            raise
        except Exception:
            log.debug(
                "Service call failed through circuit breaker",
                service=self.name,
            )
            raise

    @property
    def is_open(self) -> bool:
        """Whether the circuit is currently open (blocking calls)."""
        return self._breaker.state.state.name == "OPEN"

    @property
    def state(self) -> str:
        """Current circuit state: 'closed', 'open', or 'half-open'."""
        return self._breaker.state.state.name.lower()


class CircuitBreakerRegistry:
    """Registry of circuit breakers for all external services.

    Provides centralized management and monitoring of circuit breakers.
    Each service gets its own breaker with configurable thresholds.
    """

    def __init__(self) -> None:
        self._breakers: dict[str, ServiceCircuitBreaker] = {}

    def register(
        self,
        name: str,
        fail_max: int = 3,
        recovery_timeout: float = 60.0,
    ) -> ServiceCircuitBreaker:
        """Register a circuit breaker for a service.

        Args:
            name: Unique service identifier.
            fail_max: Failures before opening.
            recovery_timeout: Recovery wait in seconds.

        Returns:
            The registered ServiceCircuitBreaker.
        """
        breaker = ServiceCircuitBreaker(
            name=name,
            fail_max=fail_max,
            recovery_timeout=recovery_timeout,
        )
        self._breakers[name] = breaker
        return breaker

    def get(self, name: str) -> ServiceCircuitBreaker | None:
        """Get a registered circuit breaker by name."""
        return self._breakers.get(name)

    def get_or_create(
        self,
        name: str,
        fail_max: int = 3,
        recovery_timeout: float = 60.0,
    ) -> ServiceCircuitBreaker:
        """Get existing breaker or create a new one."""
        if name not in self._breakers:
            return self.register(name, fail_max, recovery_timeout)
        return self._breakers[name]

    def get_all_states(self) -> dict[str, str]:
        """Return current state of all circuit breakers."""
        return {name: b.state for name, b in self._breakers.items()}

    def get_open_circuits(self) -> list[str]:
        """Return names of all currently open circuits."""
        return [name for name, b in self._breakers.items() if b.is_open]


# Global registry instance
breaker_registry = CircuitBreakerRegistry()
