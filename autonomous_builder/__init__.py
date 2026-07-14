"""Autonomous Builder.

An external orchestration tool that autonomously drives *fresh* Claude Code
sessions through a long implementation plan, executing exactly one authoritative
"packet" per session and verifying repository truth rather than trusting Claude's
prose.

The builder never edits the target repository itself; Claude makes all target
changes. The builder only orchestrates, verifies, and records.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
