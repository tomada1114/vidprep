"""Public package interface for vidprep."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .errors import VidprepError
from .models import Cuts, Manifest, Profile, Styles, Telops, Transcript
from .project import Project

try:
    __version__ = version("vidprep")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "Cuts",
    "Manifest",
    "Profile",
    "Project",
    "Styles",
    "Telops",
    "Transcript",
    "VidprepError",
    "__version__",
]
