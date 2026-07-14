"""Repository-truth layer: git, ledger, reports and the graphify gate helper.

Everything here is read-only with respect to the target repository. The builder
never mutates target source or history; it only observes and records.
"""
