"""Separate runtime intelligence layer for paper trading.

This package is advisory and paper-only. It never places broker orders, never
reads secrets into model prompts, and never modifies the active strategy unless
deterministic evolution validation explicitly promotes a version.
"""
