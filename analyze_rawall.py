"""Analyze wasd_rawall.jsonl (rawlogger) for W-hold sensor drops.

Each row: {"t":..., "os":"WASD"(empty=none), "raw": [64 ints or null]}.
Frames are classified by bank (A: cells 15-19 dead; B: cells 0,5-14,21-28
dead).  Reports every cell that drops >= 300 counts (or rises >= 150)
relative to its own rest median, during W-held rows.
"""
import json, stat...[truncated]