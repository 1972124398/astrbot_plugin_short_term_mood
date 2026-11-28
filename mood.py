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
    "playfulness": "俏皮",
    "mischief": "恶作剧",
    "confusion": "迷糊",
    "boredom": "无聊",
    "surprise": "惊讶",
    "irritability": "烦躁",
    "anticipation": "期待",
    "longing": "想念",
    "relief": "释然",
    "contentment": "惬意",
    "hesitation": "犹豫",
    "jealousy": "吃醋",
    "frustration": "挫败",
    "guilt": "内疚",
    "gratitude": "感激",
    "admiration": "欣赏",
    "determination": "斗志",
    "tenderness": "心软",
    "amusement": "忍俊不禁",
    "sulking": "闹别扭",
    "absorption": "入迷",
    "nostalgia": "怀念",
    "skepticism": "将信将疑",
    "exasperation": "无奈",
    "competitiveness": "好胜",
    "awkwardness": "尴尬",
}
EPSILON = 0.04
DEFAULT_AMBIENT_POOL = (
    "neutral", "joy", "curiosity", "playfulness", "mischief", "confusion",
    "fatigue", "boredom", "irritability", "sadness",
    "contentment", "anticipation", "hesitation", "determination",
    "amusement", "absorption",
)
EMOTION_ALIASES = {
    **{label: name for name, label in EMOTIONS.items()},
    "平稳": "neutral", "放松": "contentment", "思念": "longing",
    "犯迷糊": "confusion", "开玩笑": "playfulness", "调皮": "mischief",
    "不服气": "determination", "纠结": "hesitation", "安心": "relief",
    "憋笑": "amusement", "想笑": "amusement", "别扭": "sulking",
    "着迷": "absorption", "怀旧": "nostalgia", "半信半疑": "skepticism",
    "哭笑不得": "exasperation", "想赢": "competitiveness", "社死": "awkwardness",
}


def emotion_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    name = EMOTION_ALIASES.get(name, name)
    return name if name == "neutral" or name in EMOTIONS else None


def mood_candidates(pool: object) -> list[str]:
    candidates = []
    if isinstance(pool, (list, tuple)):
        for item in pool:
            name = emotion_name(item)
            if name is not None and name not in candidates:
                candidates.append(name)
    return candidates or list(DEFAULT_AMBIENT_POOL)


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
    ambient_emotion: str = ""
    note_scope: str = ""
    note_origin: str = ""

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
        ambient = data.get("ambient_emotion", "")
        return cls(
            note_scope=str(data.get("note_scope") or "")[:64],
            note_origin=str(data.get("note_origin") or ""),
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
            ambient_emotion=ambient if isinstance(ambient, str) and ambient in levels else "",
        )

    def as_dict(self) -> dict:
        return {
            "note_scope": self.note_scope,
            "note_origin": self.note_origin,
            "levels": self.levels,
            "stimulated": self.stimulated,
            "updated": self.updated,
            "last_interaction": self.last_interaction,
            "recovery_ages": self.recovery_ages,
            "inner_state": self.inner_state,
            "response_tendency": self.response_tendency,
            "feeling_history": self.feeling_history,
            "ambient_emotion": self.ambient_emotion,
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
        if self.ambient_emotion not in self.levels:
            self.ambient_emotion = ""
        if not self.levels:
            self.inner_state = ""
            self.response_tendency = ""
            self.feeling_history.clear()

    def begin_interaction(self, now: float, *, random_after: float = 0) -> bool:
        """Report a return opportunity; the language model decides the actual mood."""
        previous = self.last_interaction
        self.last_interaction = max(previous, now)
        return random_after > 0 and previous > 0 and now - previous >= random_after

    def scoped_notes(self, scope: str) -> dict:
        if scope and scope == self.note_scope:
            return {
                "inner_state": self.inner_state,
                "response_tendency": self.response_tendency,
                "feeling_history": [dict(item) for item in self.feeling_history],
            }
        return {"inner_state": "", "response_tendency": "", "feeling_history": []}

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

    def adopt_mood(self, levels: dict[str, float], now: float) -> None:
        """Persist the model's complete decision without rate limits or cross-fades."""
        for name in list(self.levels):
            if name not in levels or levels[name] <= 0:
                self.stimulated.pop(name, None)
                self.recovery_ages.pop(name, None)
        for name, value in levels.items():
            if value > 0 and (name not in self.levels or value - self.levels.get(name, 0) >= EPSILON):
                self.stimulated[name] = now
                self.recovery_ages[name] = 0.0
        previous = self.levels
        self.levels = {name: value for name, value in levels.items() if value > 0}
        self.updated = max(self.updated, now)
        if self.ambient_emotion not in self.levels or abs(self.levels.get(self.ambient_emotion, 0) - previous.get(self.ambient_emotion, 0)) >= EPSILON:
            self.ambient_emotion = ""
        self.inner_state = ""
        self.response_tendency = ""
        if not self.levels:
            self.feeling_history.clear()

    def apply(self, deltas: dict[str, float], now: float, sensitivity: float, inertia: float = 0) -> None:
        self.updated = max(self.updated, now)
        for name, delta in deltas.items():
            change = min(0.35, max(-0.35, delta)) * sensitivity
            if change < 0:
                change *= 1 - inertia * self.levels.get(name, 0.0)
            value = min(1.0, max(0.0, self.levels.get(name, 0.0) + change))
            if value < EPSILON:
                self.levels.pop(name, None)
                self.stimulated.pop(name, None)
                self.recovery_ages.pop(name, None)
                if self.ambient_emotion == name:
                    self.ambient_emotion = ""
                continue
            self.levels[name] = value
            # Neutral conversation and reassurance never renew an old emotion's lifetime.
            if change >= EPSILON:
                self.stimulated[name] = now
                self.recovery_ages[name] = 0.0
                if self.ambient_emotion == name:
                    self.ambient_emotion = ""


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

    def load(self) -> MoodState:
        row = self.connection.execute("SELECT payload FROM agent_state WHERE id = 1").fetchone()
        return MoodState.from_dict(json.loads(row[0])) if row else MoodState()

    def save(self, state: MoodState, clocks: dict | None = None) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO agent_state VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (json.dumps(state.as_dict(), ensure_ascii=False, allow_nan=False),),
            )
            if clocks is not None:
                self.connection.execute(
                    "INSERT INTO conversation_clocks VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                    (json.dumps(clocks, ensure_ascii=False, allow_nan=False),),
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
