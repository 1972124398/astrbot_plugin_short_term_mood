"""Real elapsed time for a conversation, independent of the agent's global mood."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from .interop import conversation_key, human_dialogue, message_text

TIME_CONTEXT = "short_term_mood.time_context"
TIME_MARKER = "<astrbot_time_context>"


def fingerprint(message):
    text = json.dumps([message.get("role"), message_text(message)], ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def iso_time(value):
    return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat(timespec="seconds") if value else None


class ConversationClocks:
    def __init__(self, saved=None):
        self.sessions = saved or {}

    def moment(self, origin):
        return self.sessions.setdefault(conversation_key(origin), {
            "last_user": 0, "last_reply": 0, "break_at": 0, "old_tail": [], "fresh": [],
        })

    @staticmethod
    def boundary(moment, history):
        if not moment.get("break_at"):
            return 0
        keys = [fingerprint(message) for message in history]
        tail = moment.get("old_tail", [])
        if tail:
            for index in range(len(keys) - len(tail), -1, -1):
                if keys[index:index + len(tail)] == tail:
                    return index + len(tail)
        fresh = set(moment.get("fresh", []))
        return next((index for index, key in enumerate(keys) if key in fresh), len(history))

    def prepare(self, origin, history, prompt, now, fade_seconds, forget_seconds, previous_at=0):
        moment = self.moment(origin)
        previous = max(moment.get("last_user", 0), moment.get("last_reply", 0))
        if not previous and not moment.get("break_at"):
            previous = previous_at
        gap = max(0, now - previous) if previous else None
        dialogue = human_dialogue(history)
        if gap is not None and gap >= forget_seconds:
            moment.update(break_at=now, old_tail=[fingerprint(item) for item in dialogue[-6:]], fresh=[])
        cutoff = self.boundary(moment, dialogue)
        moment["last_user"] = max(now, moment.get("last_user", 0))
        moment["fresh"] = [*moment.get("fresh", []), fingerprint({"role": "user", "content": prompt})][-64:]
        phase = "unknown" if gap is None else "set_aside" if gap >= forget_seconds else "fading" if gap >= fade_seconds else "ongoing"
        return {
            "now": iso_time(now), "last_real_exchange": iso_time(previous),
            "gap_seconds": round(gap) if gap is not None else None,
            "phase": phase, "fresh_segment_since": iso_time(moment.get("break_at", 0)),
            "earlier_dialogue_count": cutoff,
            "recent_dialogue": dialogue[cutoff:],
        }

    def finish(self, origin, text, now):
        moment = self.moment(origin)
        moment["last_reply"] = max(now, moment.get("last_reply", 0))
        if text:
            moment["fresh"] = [*moment.get("fresh", []), fingerprint({"role": "assistant", "content": text})][-64:]

    def human_activity(self, origin, now):
        moment = self.moment(origin)
        moment["last_user"] = max(now, moment.get("last_user", 0))


def time_hint(data, preferences=""):
    public = {key: value for key, value in data.items() if key != "recent_dialogue"}
    return (TIME_MARKER + "\n" + json.dumps(public, ensure_ascii=False)
            + "\n这是和当前会话上次真实交流以来经过的现实时间，聊天记录相邻不等于刚刚发生。"
            "fading 表示旧话题的临场感已经淡了；set_aside 表示隔了较久，可以像人一样暂时不惦记旧细节，从眼前的话重新相处。"
            "fresh_segment_since 之前的交流是较早背景，之后的新几句才是这次聊天。"
            "旧的争执、打趣、临时要求、告别和情绪，不必因为还在历史中就继续演下去，也不要把旧刺激当成刚发生而反复生气。"
            "用户自然提到旧事或想接着做任务时，可以想起来、询问或顺着接上，由你结合人设判断；熟悉感、重要关系和正在办的任务仍可有延续。"
            "不用每次报时、解释遗忘机制或固定说‘好久不见’，自然体现当下的距离与兴趣即可。"
            + ("\n使用者的时间感偏好：" + preferences if preferences else "")
            + "\n</astrbot_time_context>")
