"""Persistent agent-wide short-term affect, independent of the AstrBot runtime."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


EMOTIONS = {
    "joy": "开心",
    "affection": "亲近",
    "sadness": "失落",
    "anger": "生气",
    "anxiety": "不安",
    "fatigue": "疲惫",
    "curiosity": "好奇",
    "embarrassment": "害羞",
    "pride": "得意",
    "grievance": "委屈",
}
EPSILON = 0.04


def finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Expected a finite number")
    return number


@dataclass
class MoodState:
    levels: dict[str, float] = field(default_factory=dict)
    stimulated: dict[str, float] = field(default_factory=dict)
    updated: float = 0.0
    last_interaction: float = 0.0
    recovery_ages: dict[str, float] = field(default_factory=dict)
    inner_state: str = ""
    response_tendency: str = ""
    feeling_history: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> MoodState:
        levels = {
            name: min(1.0, max(0.0, finite_number(value)))
            for name, value in data.get("levels", {}).items()
            if name in EMOTIONS
        }
        stimulated = {
            name: finite_number(data.get("stimulated", {}).get(name, 0))
            for name in levels
        }
        return cls(
            levels=levels,
            stimulated=stimulated,
            updated=finite_number(data.get("updated", 0)),
            last_interaction=finite_number(data.get("last_interaction", 0)),
            recovery_ages={
                name: max(0.0, finite_number(value))
                for name, value in data.get("recovery_ages", {}).items()
                if name in levels
            },
            inner_state=str(data.get("inner_state") or "")[:160],
            response_tendency=str(data.get("response_tendency") or "")[:160],
            feeling_history=[
                {"feeling": str(item.get("feeling") or "")[:160],
                 "tendency": str(item.get("tendency") or "")[:160]}
                for item in data.get("feeling_history", [])[-5:]
                if isinstance(item, dict)
            ],
        )

    def as_dict(self) -> dict:
        return {
            "levels": self.levels,
            "stimulated": self.stimulated,
            "updated": self.updated,
            "last_interaction": self.last_interaction,
            "recovery_ages": self.recovery_ages,
            "inner_state": self.inner_state,
            "response_tendency": self.response_tendency,
            "feeling_history": self.feeling_history,
        }

    def recover(self, now: float, half_life: float, recovery_seconds: float, idle_seconds: float = 0) -> None:
        protected_until = self.last_interaction + idle_seconds if self.last_interaction > 0 else 0
        elapsed = max(0.0, now - max(self.updated, protected_until))
        decay = math.exp2(-elapsed / half_life)
        for name in list(self.levels):
            age = self.recovery_ages.get(name, max(0.0, self.updated - self.stimulated.get(name, self.updated)))
            age += elapsed
            value = self.levels[name] * decay
            expired = age >= recovery_seconds
            if expired or value < EPSILON:
                self.levels.pop(name)
                self.stimulated.pop(name, None)
                self.recovery_ages.pop(name, None)
            else:
                self.levels[name] = value
                self.recovery_ages[name] = age
        self.updated = max(self.updated, now)
        if not self.levels:
            self.inner_state = ""
            self.response_tendency = ""
            self.feeling_history.clear()

    def remember_feeling(self, feeling: str, tendency: str) -> None:
        if not self.levels:
            self.inner_state = ""
            self.response_tendency = ""
            self.feeling_history.clear()
            return
        self.inner_state = feeling or self.inner_state
        self.response_tendency = tendency or self.response_tendency
        entry = {"feeling": self.inner_state, "tendency": self.response_tendency}
        if any(entry.values()) and (not self.feeling_history or self.feeling_history[-1] != entry):
            self.feeling_history.append(entry)
            self.feeling_history = self.feeling_history[-5:]

    def apply(self, deltas: dict[str, float], now: float, sensitivity: float, inertia: float = 0) -> None:
        for name, delta in deltas.items():
            change = min(0.35, max(-0.35, delta)) * sensitivity
            if change < 0:
                change *= 1 - inertia * self.levels.get(name, 0.0)
            value = min(1.0, max(0.0, self.levels.get(name, 0.0) + change))
            if value < EPSILON:
                self.levels.pop(name, None)
                self.stimulated.pop(name, None)
                self.recovery_ages.pop(name, None)
                continue
            self.levels[name] = value
            # Neutral conversation and reassurance never renew an old emotion's lifetime.
            if change >= EPSILON:
                self.stimulated[name] = now
                self.recovery_ages[name] = 0.0


class MoodStore:
    """One shared state and a bounded cache of persona-derived expression profiles."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # The plugin serializes access and performs runtime writes off the event loop.
        self.connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            "CREATE TABLE IF NOT EXISTS agent_state "
            "(id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS profiles "
            "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS conversation_clocks "
            "(id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL);"
        )

    def load_clocks(self) -> dict:
        row = self.connection.execute("SELECT payload FROM conversation_clocks WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else {}

    def save_clocks(self, clocks: dict) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO conversation_clocks VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (json.dumps(clocks, ensure_ascii=False, allow_nan=False),),
            )

    def load(self) -> MoodState:
        row = self.connection.execute("SELECT payload FROM agent_state WHERE id = 1").fetchone()
        return MoodState.from_dict(json.loads(row[0])) if row else MoodState()

    def save(self, state: MoodState) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO agent_state VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (json.dumps(state.as_dict(), ensure_ascii=False, allow_nan=False),),
            )

    def profile(self, key: str, now: float, cache_seconds: float = 7 * 86400) -> dict[str, str] | None:
        row = self.connection.execute(
            "SELECT payload FROM profiles WHERE key = ? AND created > ?",
            (key, now - cache_seconds),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def load_profiles(self) -> dict[str, tuple[dict[str, str], float]]:
        rows = self.connection.execute("SELECT key, payload, created FROM profiles").fetchall()
        return {key: (json.loads(payload), created) for key, payload, created in rows}

    def save_profile(self, key: str, profiles: dict[str, str], now: float, cache_seconds: float = 7 * 86400) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO profiles VALUES (?, ?, ?)",
                (key, json.dumps(profiles, ensure_ascii=False), now),
            )
            self.connection.execute("DELETE FROM profiles WHERE created <= ?", (now - cache_seconds,))
            self.connection.execute(
                "DELETE FROM profiles WHERE key NOT IN "
                "(SELECT key FROM profiles ORDER BY created DESC LIMIT 100)"
            )

    def clear_profiles(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM profiles")

    def close(self) -> None:
        self.connection.close()
