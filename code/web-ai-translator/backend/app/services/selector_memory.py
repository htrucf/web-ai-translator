"""Persistent learned-selector memory for the 2-tier web navigation.

When the VLM successfully locates a UI element (because all hardcoded CSS
selectors failed), the translator derives a stable selector from the DOM at
those coordinates and records it here. Future runs try learned selectors
first — VLM only fires when both hardcoded *and* learned selectors fail,
recovering CSS-fast performance after Gemini/ChatGPT change their UI.

Score model:
    score = hits - 2 * fails
Selectors with score < -3 are dropped. Higher score → tried first.

Storage:
    JSON file at user_data_dir()/learned_selectors.json
    Schema:
        {
          "gemini": {
            "input_box": [
              {"selector": "...", "hits": 5, "fails": 0, "last_used": <epoch>}
            ],
            "send_button": [...]
          },
          "chatgpt": {...}
        }

The file is small (< 10 KB even after months of use) so we hold the whole
thing in memory and rewrite on every change. Atomic write prevents partial
files if the process is killed mid-save.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

from app import paths
from app.utils.safe_io import atomic_write_json
from app.audit import log_event

_DROP_SCORE = -3
_MAX_PER_TYPE = 8  # cap per (backend, element) — old/low-score entries fall off


def _default_path() -> str:
    return os.path.join(paths.user_data_dir(), "learned_selectors.json")


class SelectorMemory:
    """Process-wide singleton (one per Python process) — loaded once, written
    on every change. Thread-safe via an internal lock so concurrent backends
    in the same process don't clobber each other.
    """

    _instance: Optional["SelectorMemory"] = None

    def __init__(self, path: str | None = None):
        self.path = path or _default_path()
        self._lock = threading.Lock()
        self._data: dict = self._load()

    @classmethod
    def instance(cls) -> "SelectorMemory":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── persistence ──────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {}

    def _save(self) -> None:
        try:
            atomic_write_json(self.path, self._data)
        except Exception as e:
            # Persistence failure is non-fatal — in-memory still works for
            # the current run, just won't survive restart.
            print(f"[SelectorMemory] save failed (non-fatal): {e}")

    # ── public API ───────────────────────────────────────────────────
    def get(self, backend: str, element_type: str) -> list[str]:
        """Return learned selectors sorted best→worst, dropping dead ones."""
        with self._lock:
            entries = self._entries(backend, element_type)
            ranked = sorted(
                (e for e in entries if self._score(e) > _DROP_SCORE),
                key=self._score,
                reverse=True,
            )
            result = [e["selector"] for e in ranked]
        # Outside the lock: record cache lookup outcome (hit if any selector
        # survived the drop-score filter, miss otherwise).
        try:
            from app.metrics import selector_memory_lookup_total
            selector_memory_lookup_total.labels(
                backend=backend,
                element_type=element_type,
                outcome="hit" if result else "miss",
            ).inc()
        except Exception:
            pass
        log_event(
            "selector.memory_lookup",
            backend=backend,
            element_type=element_type,
            outcome="hit" if result else "miss",
            candidates=len(result),
            top_selector=result[0] if result else "",
        )
        return result

    def record_success(self, backend: str, element_type: str, selector: str) -> None:
        """Bump the hit count for a selector (creating the entry if new).

        A *new* entry is the signal that a selector was just learned from a
        successful VLM rescue → emits selector_learning_total.
        """
        if not selector:
            return
        is_new = False
        hits_after = 0
        fails_after = 0
        with self._lock:
            existing = self._find(backend, element_type, selector)
            is_new = existing is None
            entry = self._find_or_create(backend, element_type, selector)
            entry["hits"] = entry.get("hits", 0) + 1
            entry["last_used"] = int(time.time())
            hits_after = entry["hits"]
            fails_after = entry.get("fails", 0)
            self._trim(backend, element_type)
            self._save()
        if is_new:
            try:
                from app.metrics import selector_learning_total
                selector_learning_total.labels(
                    backend=backend, element_type=element_type,
                ).inc()
            except Exception:
                pass
            log_event(
                "selector.learned",
                backend=backend,
                element_type=element_type,
                selector=selector,
            )
        else:
            log_event(
                "selector.success",
                backend=backend,
                element_type=element_type,
                selector=selector,
                hits=hits_after,
                fails=fails_after,
            )

    def record_failure(self, backend: str, element_type: str, selector: str) -> None:
        """Bump the fail count. Selector is dropped on next get() if score
        falls below threshold."""
        if not selector:
            return
        with self._lock:
            entry = self._find(backend, element_type, selector)
            if entry is None:
                return  # nothing to penalize — wasn't ours
            entry["fails"] = entry.get("fails", 0) + 1
            hits = entry.get("hits", 0)
            fails = entry["fails"]
            self._save()
        score = hits - 2 * fails
        log_event(
            "selector.failure",
            backend=backend,
            element_type=element_type,
            selector=selector,
            hits=hits,
            fails=fails,
            score=score,
            will_drop=score <= _DROP_SCORE,
        )

    def stats(self) -> dict:
        """Snapshot for debugging / API exposure."""
        with self._lock:
            return json.loads(json.dumps(self._data))  # deep copy

    # ── helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _score(entry: dict) -> int:
        return entry.get("hits", 0) - 2 * entry.get("fails", 0)

    def _entries(self, backend: str, element_type: str) -> list[dict]:
        return self._data.setdefault(backend, {}).setdefault(element_type, [])

    def _find(self, backend: str, element_type: str, selector: str) -> dict | None:
        for e in self._entries(backend, element_type):
            if e.get("selector") == selector:
                return e
        return None

    def _find_or_create(self, backend: str, element_type: str, selector: str) -> dict:
        existing = self._find(backend, element_type, selector)
        if existing is not None:
            return existing
        new_entry = {
            "selector": selector,
            "hits": 0,
            "fails": 0,
            "last_used": int(time.time()),
        }
        self._entries(backend, element_type).append(new_entry)
        return new_entry

    def _trim(self, backend: str, element_type: str) -> None:
        """Keep only the top-N entries by score so the file doesn't grow
        unbounded as the UI evolves."""
        entries = self._entries(backend, element_type)
        if len(entries) <= _MAX_PER_TYPE:
            return
        entries.sort(key=self._score, reverse=True)
        del entries[_MAX_PER_TYPE:]
