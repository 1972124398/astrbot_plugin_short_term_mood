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
}
NEGATIVE = {"anger", "sadness", "anxiety", "fatigue"}
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
    last_refusal: float = 0.0
    refused_previous: bool = False
    last_interaction: float = 0.0
    recovery_ages: dict[str, float] = field(default_factory=dict)

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
            last_refusal=finite_number(data.get("last_refusal", 0)),
            refused_previous=data.get("refused_previous") is True,
            last_interaction=finite_number(data.get("last_interaction", 0)),
            recovery_ages={
                name: max(0.0, finite_number(value))
                for name, value in data.get("recovery_ages", {}).items()
                if name in levels
            },
        )

    def as_dict(self) -> dict:
        return {
            "levels": self.levels,
            "stimulated": self.stimulated,
            "updated": self.updated,
            "last_refusal": self.last_refusal,
            "refused_previous": self.refused_previous,
            "last_interaction": self.last_interaction,
            "recovery_ages": self.recovery_ages,
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

    def apply(self, deltas: dict[str, float], now: float, sensitivity: float) -> None:
        for name, delta in deltas.items():
            change = min(0.35, max(-0.35, delta)) * sensitivity
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

    def dominant(self) -> tuple[str, float]:
        return max(self.levels.items(), key=lambda item: item[1], default=("neutral", 0.0))

    def choose_refusal(
        self,
        now: float,
        *,
        allowed: bool,
        enabled: bool,
        threshold: float,
        probability: float,
        cooldown: float,
        draw: float,
    ) -> bool:
        name, strength = self.dominant()
        chance = probability * strength
        refuse = (
            enabled
            and allowed
            and name in NEGATIVE
            and strength >= threshold
            and not self.refused_previous
            and (self.last_refusal == 0 or now - self.last_refusal >= cooldown)
            and draw < chance
        )
        self.refused_previous = refuse
        if refuse:
            self.last_refusal = now
        return refuse


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
