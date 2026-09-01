"""Configuration, entirely from the environment.

The one setting you should actually set is CONGRESS_CONTACT. The SEC and Wikimedia
both ask automated clients to identify themselves with a way to get in touch, and
the SEC in particular throttles or blocks anonymous-looking traffic. Everything else
has a working default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Config", "CONFIG", "user_agent"]


def _p(v: str) -> Path:
    return Path(os.path.expanduser(v))


@dataclass
class Config:
    # --- storage -------------------------------------------------------------
    db_path: Path = field(default_factory=lambda: _p(
        os.getenv("CONGRESS_DB", "./data/congress.db")))
    cache_dir: Path = field(default_factory=lambda: _p(
        os.getenv("CONGRESS_CACHE", "./cache")))
    out_html: Path = field(default_factory=lambda: _p(
        os.getenv("CONGRESS_OUT", "./out/congress.html")))

    # --- who you are, for the APIs that ask ----------------------------------
    # Used in the User-Agent for SEC and Wikipedia. Set it to a real address.
    contact: str = os.getenv("CONGRESS_CONTACT", "")

    # --- collection ----------------------------------------------------------
    # Years of House filings to collect. The Clerk publishes one ZIP per year.
    years: tuple[str, ...] = tuple(
        y.strip() for y in os.getenv("CONGRESS_YEARS", "2026,2025").split(",") if y.strip())
    # Store every disclosed bracket; the published page filters for display, so the
    # floor can change without re-collecting. Raise it to shrink the database.
    min_amount: int = int(os.getenv("CONGRESS_MIN_AMOUNT", "0"))
    # What the page selects by default ($1,001-$15,000 is mostly rebalancing noise).
    default_floor: int = int(os.getenv("CONGRESS_DEFAULT_FLOOR", "15001"))
    lookback_days: int = int(os.getenv("CONGRESS_LOOKBACK_DAYS", "30"))
    congress_number: int = int(os.getenv("CONGRESS_NUMBER", "119"))

    # --- endpoints (override only if one moves) ------------------------------
    house_disc_base: str = os.getenv(
        "HOUSE_DISC_BASE", "https://disclosures-clerk.house.gov/public_disc")

    def __post_init__(self):
        for p in (self.db_path.parent, self.cache_dir, self.out_html.parent):
            p.mkdir(parents=True, exist_ok=True)


CONFIG = Config()


def user_agent(cfg: Config | None = None) -> str:
    """A User-Agent the SEC and Wikimedia will accept.

    Both ask automated clients to say who they are. Without CONGRESS_CONTACT set we
    still send something honest, but the SEC may throttle it -- so the CLI warns.
    """
    cfg = cfg or CONFIG
    who = cfg.contact.strip() or "CONGRESS_CONTACT-unset"
    # Keep this shape. A bare URL in the User-Agent makes sec.gov answer 403; the
    # "name/version (contact)" form is what it accepts.
    return f"congress-trades/0.1 ({who})"
