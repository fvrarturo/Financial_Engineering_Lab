"""growth_floor: research pipeline for 'Grow the Money, Don't Break the Floor' (MIT 15.456)."""
from .io_french import read_french, list_sections, FrenchParseError, SENTINELS  # noqa: F401
from .data import build_asset_frame, load_config, load_asset, read_asset_frame  # noqa: F401
from .checks import UnitsError, check_units  # noqa: F401

__version__ = "0.1.0"
