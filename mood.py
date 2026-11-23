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
        )

    def as_dict(self) -> dict:
        return {
            "levels": self.levels,
            "stimulated": self.stimulated,
            "updated": self.updated,
            "last_refusal": self.last_refusal,
            "refused_previous": self.refused_previous,
        }

    def recover(self, now: float, half_life: float, recovery_seconds: float) -> None:
        elapsed = max(0.0, now - self.updated)
        decay = math.exp2(-elapsed / half_life)
        for name in list(self.levels):
            value = self.levels[name] * decay
            expired = now - self.stimulated.get(name, 0.0) >= recovery_seconds
            if expired or value < EPSILON:
                self.levels.pop(name)
                self.stimulated.pop(name, None)
            else:
                self.levels[name] = value
        self.updated = now

    def apply(self, deltas: dict[str, float], now: float, sensitivity: float) -> None:
        for name, delta in deltas.items():
            change = min(0.35, max(-0.35, delta)) * sensitivity
            value = min(1.0, max(0.0, self.levels.get(name, 0.0) + change))
            if value < EPSILON:
                self.levels.pop(name, None)
                self.stimulated.pop(name, None)
                continue
            self.levels[name] = value
            # Neutral conversation and reassurance never renew an old emotion's lifetime.
            if change >= EPSILON:
                self.stimulated[name] = now

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
        self.connection = sqlite3.connect(path, timeout=5)
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

    def profile(self, key: str, now: float) -> dict[str, str] | None:
        row = self.connection.execute(
            "SELECT payload FROM profiles WHERE key = ? AND created > ?",
            (key, now - 7 * 86400),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_profile(self, key: str, profiles: dict[str, str], now: float) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO profiles VALUES (?, ?, ?)",
                (key, json.dumps(profiles, ensure_ascii=False), now),
            )
            self.connection.execute("DELETE FROM profiles WHERE created <= ?", (now - 7 * 86400,))
            self.connection.execute(
                "DELETE FROM profiles WHERE key NOT IN "
                "(SELECT key FROM profiles ORDER BY created DESC LIMIT 100)"
            )

    def clear_profiles(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM profiles")

    def close(self) -> None:
        self.connection.close()
