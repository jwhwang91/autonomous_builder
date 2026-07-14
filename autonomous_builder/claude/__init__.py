"""Claude Code control layer: driver abstraction, session lifecycle, result
parsing and dynamic prompt construction.

The whole layer is built on the :class:`~autonomous_builder.claude.driver.ClaudeDriver`
abstraction so that unit tests can substitute a deterministic
:class:`~autonomous_builder.claude.driver.FakeClaudeDriver` and never require a
live Claude Code process.
"""
