"""congress-trades: US congressional stock-trade disclosures, from the source."""
# Importing config here is what makes the .env file reach every entry point.
# config.load_env() runs at config import, and llm.py reads its environment at
# call time without otherwise needing config -- so `from congress_trades import
# llm` alone would once have missed the file and quietly fallen back to the
# default provider. Loading it at the package boundary makes that impossible.
from . import config as config  # noqa: F401

__version__ = "0.1.0"
