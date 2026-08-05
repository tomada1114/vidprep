"""Public package interface for vidprep."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .errors import VidprepError
from .models import (
    Cuts,
    Manifest,
    NoiseFloorReport,
    Profile,
    Styles,
    Telops,
    Transcript,
    VadReport,
)
from .project import Project
from .timeline import Timeline

try:
    __version__ = version("vidprep")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "Cuts",
    "Manifest",
    "NoiseFloorReport",
    "Profile",
    "Project",
    "Styles",
    "Telops",
    "Timeline",
    "Transcript",
    "VadReport",
    "VidprepError",
    "__version__",
]
