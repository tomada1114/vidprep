"""Tests for the public vidprep package interface."""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
from importlib.metadata import PackageNotFoundError, version

import vidprep
from vidprep import __all__, __version__


class TestPackageMetadata:
    def test_public_exports(self):
        assert set(__all__) == {
            "Cuts",
            "Manifest",
            "Profile",
            "Project",
            "Styles",
            "Telops",
            "Timeline",
            "Transcript",
            "VadReport",
            "VidprepError",
            "__version__",
        }

    def test_every_export_is_reachable_on_the_package(self):
        assert [name for name in __all__ if not hasattr(vidprep, name)] == []

    def test_version_matches_installed_metadata(self):
        assert __version__ == version("vidprep")

    def test_version_falls_back_when_package_not_installed(self, monkeypatch):
        def fake_version(_: str) -> str:
            raise PackageNotFoundError

        with monkeypatch.context() as patched:
            patched.setattr(importlib_metadata, "version", fake_version)
            reloaded = importlib.reload(vidprep)

        assert reloaded.__version__ == "0.0.0+unknown"
        importlib.reload(vidprep)
