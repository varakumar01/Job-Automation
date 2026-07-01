"""Job-source plugin contract. See PLAN.md §4.

A portal is added by dropping a ``<site>.py`` into this folder that defines a
``JobSourcePlugin`` subclass. ``registry.py`` auto-discovers it — no other code
changes. Both Apify-backed and custom Playwright-session plugins implement the
same three members.
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Job:
    """Normalized job record (matches the store schema in PLAN.md §3).

    ``source`` + ``ext_id`` uniquely identify a posting and are required; the
    rest are best-effort and may be ``None`` depending on the portal/mode.
    """

    source: str
    ext_id: str
    url: str | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    posted_at: str | None = None
    jd_text: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # portal-specific, not stored directly

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("Job.source is required")
        if self.ext_id is None or str(self.ext_id) == "":
            raise ValueError("Job.ext_id is required")
        self.ext_id = str(self.ext_id)

    def to_row(self) -> dict[str, Any]:
        """Dict of the columns the store persists (drops ``extra``)."""
        row = asdict(self)
        row.pop("extra", None)
        return row


class JobSourcePlugin(abc.ABC):
    """Base class every portal plugin implements.

    Subclasses set a unique ``name`` and implement ``is_available`` +
    ``fetch``. Keep network/credential checks in ``is_available`` so the
    scraper can skip unconfigured portals cleanly.
    """

    #: Stable portal identifier, also stored as ``jobs.source``.
    name: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Enforce a name on concrete subclasses (abstract intermediates may skip).
        if not getattr(cls, "__abstractmethods__", None) and not cls.name:
            raise TypeError(f"{cls.__name__} must set a non-empty `name`")

    @abc.abstractmethod
    def is_available(self) -> bool:
        """True if this plugin can run now (token present, actor/session reachable)."""

    @abc.abstractmethod
    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        """Search the portal for ``query`` and return up to ``limit`` normalized jobs."""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r}>"
