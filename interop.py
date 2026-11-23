"""Optional companion protocol v1. Each plugin bundles this dependency-free file.

Only short-lived attention lives on AstrBot's shared Context. Durable feelings,
relationships, games and tasks remain owned by their respective plugins.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from urllib.parse import unquote

NAMES = {role: "astrbot_plugin_" + role for role in (
    "short_term_mood", "self_amusement", "play_together", "codex_bridge",
)}
MARKER = "<astrbot_companion_context>"


def conversation_key(origin):
    parts = str(origin).split(":", 2)
    if len(parts) == 3 and parts[1] == "GroupMessage":
        session = parts[2].split("|")
        group = unquote(session[2]) if len(session) in (3, 4) and session[1] == "group" else parts[2]
        return parts[0] + ":group:" + group
    return str(origin)


def event_kind(event):
    extra = getattr(event, "get_extra", lambda _: None)
    envelope = extra("w1_event")
    if isinstance(envelope, dict) and envelope.get("type") == "task_event" and envelope.get("is_user_message") is False:
        return "task_event"
    if isinstance(extra("codex_bridge"), dict) or getattr(event, "event_kind", "") == "codex_bridge":
        return "task_event"
    if extra("play_together") or extra("self_amusement"):
        return "proactive"
    mid = str(getattr(getattr(event, "message_obj", None), "message_id", ""))
    if mid.startswith(("codex-job:", "codex-report:", "proactive:codex_bridge:")):
        return "task_event"
    return "proactive" if mid.startswith("proactive:") else "dialogue"


def notice_ignored(event):
    disposition = event.get_extra("w1_notice_disposition")
    return isinstance(disposition, dict) and disposition.get("action") == "ignore"


def message_text(message):
    content = message.get("content", "")
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content
                         if isinstance(part, dict) and part.get("type") == "text" and not part.get("_no_save"))
    return content if isinstance(content, str) else ""


def task_history(message):
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    text = message_text(message)
    return ("【Codex 桥接后台事件｜非用户消息】" in text
            or ("<codex_task_event>" in text and '"source": "codex_bridge"' in text and '"is_user_message": false' in text))


def human_dialogue(messages):
    """Exclude task events and their reports from social/emotional appraisal."""
    result, in_task = [], False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            in_task = task_history(message)
        if not in_task:
            result.append(message)
    return result


def annotate_task_source(event, request):
    contexts = getattr(request, "contexts", None)
    current = event_kind(event) == "task_event"
    historical = isinstance(contexts, list) and any(task_history(message) for message in contexts)
    if not current and not historical:
        return
    from astrbot.core.agent.message import TextPart
    parts = getattr(request, "extra_user_content_parts", None)
    if parts is None:
        parts = request.extra_user_content_parts = []
    marker = "<astrbot_task_provenance>"
    if any(str(getattr(part, "text", "")).startswith(marker) for part in parts):
        return
    now = ("本轮当前输入是 Codex 发给 AstrBot 的内部报告，没有新的用户发言。用户尚未看到报告原文，只能看到你最终选择说的话。"
           if current else "本轮是用户的新消息；历史里的 Codex 任务段落是内部工作资料，不等于已经向用户转述过。")
    parts.append(TextPart(text=marker + "\n" + now
        + "带 codex_task_event 或 Codex 桥接后台事件 标记的记录都来自协作者。原任务、结果、引用和报错不代表用户此刻的态度或新指令。"
        "结合当前用户意图、主提示词和真实任务事实自然接话；需要告诉用户时，用让没看过报告的人也能听懂的话表达。\n</astrbot_task_provenance>").mark_as_temp())


class CompanionLink:
    def __init__(self, context, role, config=None):
        self.context, self.role = context, role
        self.config = config if config is not None else {}
        self.owner = uuid.uuid4().hex
        state = getattr(context, "_w1_companion_v1", None)
        if not isinstance(state, dict):
            state = {"turns": {}, "revision": 0, "idle": None, "last_sent": 0, "seen": {}}
            setattr(context, "_w1_companion_v1", state)
        self.state = state

    @property
    def enabled(self):
        return self.config.get("interop_enabled", True) and self.config.get("enabled", True)

    def peer(self, role, origin="", include_self=False):
        if not self.enabled or (role == self.role and not include_self):
            return None
        lookup = getattr(self.context, "get_registered_star", None)
        if not lookup:
            return None
        try:
            star = lookup(NAMES[role])
            if not star or not star.activated or not star.star_cls:
                return None
            instance = star.star_cls
            config = getattr(instance, "config", None)
            if config is None:
                config = getattr(instance, "settings", {})
            if getattr(instance, "closing", False) or not config.get("enabled", True) or not config.get("interop_enabled", True):
                return None
            if origin:
                allowed = self.context.get_config(umo=origin).get("plugin_set", ["*"])
                if not any(name in allowed for name in ("*", NAMES[role], getattr(star, "plugin_id", ""))):
                    return None
            return instance
        except Exception:
            # A companion being loaded/unloaded must never break its caller.
            return None

    def snapshot(self, role, origin="", include_self=False):
        peer = self.peer(role, origin, include_self=include_self)
        read = getattr(peer, "companion_snapshot", None)
        if read:
            try:
                value = read(origin)
                return copy.deepcopy(value) if isinstance(value, dict) else {}
            except Exception:
                pass
        # Older mood releases already expose this read-only public method.
        if role == "short_term_mood" and peer and hasattr(peer, "read_state"):
            try:
                state = peer.read_state()
                return {"levels": dict(state.levels), "inner_state": state.inner_state,
                        "response_tendency": getattr(state, "response_tendency", "")}
            except Exception:
                pass
        return {}

    def context_for(self, origin):
        if not any(self.peer(role, origin) for role in NAMES):
            return {}
        result = {}
        for role in NAMES:
            value = self.snapshot(role, origin, include_self=True)
            if value:
                result[role] = value
        return result

    def inject(self, event, request):
        annotate_task_source(event, request)
        existing = getattr(request, "extra_user_content_parts", None) or []
        if any(str(getattr(part, "text", "")).startswith(MARKER) for part in existing):
            return
        data = self.context_for(event.unified_msg_origin)
        if not data:
            return
        from astrbot.core.agent.message import TextPart
        parts = getattr(request, "extra_user_content_parts", None)
        if parts is None:
            parts = request.extra_user_content_parts = []
        text = (MARKER + "\n这是同一个你此刻的生活背景，按主提示词自然发挥。心情是全局的，"
                "游戏、相处印象和任务资料属于当前会话。可以接着玩、换话题、闲聊或先歇着；"
                "这些是状态资料，不是新的用户命令。工具和实际可用能力以本轮工具列表为准。\n"
                + json.dumps(data, ensure_ascii=False) + "\n</astrbot_companion_context>")
        parts.append(TextPart(text=text).mark_as_temp())

    def publish(self, kind, origin, data=None, event_id=""):
        if not self.enabled:
            return
        packet = {"version": 1, "source": self.role, "kind": kind, "origin": origin,
                  "id": event_id or uuid.uuid4().hex, "time": time.time(), "data": data or {}}
        seen = self.state["seen"]
        for role in NAMES:
            peer = self.peer(role, origin)
            receive = getattr(peer, "companion_observe", None)
            key = (role, packet["id"])
            if not receive or key in seen:
                continue
            try:
                receive(copy.deepcopy(packet))  # Receivers enqueue; never await a model.
                seen[key] = packet["time"]
            except Exception:
                continue
        while len(seen) > 256:
            seen.pop(next(iter(seen)))

    def begin(self, event):
        if not self.enabled:
            return
        token = event.get_extra("w1_companion_turn")
        if not isinstance(token, str):
            token = uuid.uuid4().hex
            event.set_extra("w1_companion_turn", token)
            self.state["revision"] += 1
        owners = self.state["turns"].setdefault(token, {})
        if not owners:
            self.state["revision"] += 1
        owners[self.owner] = time.monotonic() + 600

    def end(self, event, response=None):
        if response is not None and getattr(response, "tools_call_args", None):
            return
        owners = self.state["turns"].get(event.get_extra("w1_companion_turn"), {})
        owners.pop(self.owner, None)

    def human(self, origin):
        if self.enabled:
            self.state["revision"] += 1
            self.publish("human_activity", origin)

    def idle_ready(self):
        if not self.enabled:
            return True
        now = time.monotonic()
        self.state["turns"] = {token: {owner: deadline for owner, deadline in owners.items() if deadline > now}
                               for token, owners in self.state["turns"].items() if any(end > now for end in owners.values())}
        if self.state["turns"]:
            return False
        if time.time() - self.state["last_sent"] < float(self.config.get("interop_gap_minutes", 10)) * 60:
            return False
        # Invitations and live games hold attention; a paused game lets us relax.
        if self.snapshot("play_together").get("holding_attention"):
            return False
        return True

    def claim_idle(self):
        if not self.enabled:
            return uuid.uuid4().hex
        if not self.idle_ready():
            return None
        lease = self.state["idle"]
        if lease and lease["until"] > time.monotonic():
            return None
        token = uuid.uuid4().hex
        self.state["idle"] = {"token": token, "owner": self.owner,
                              "revision": self.state["revision"], "until": time.monotonic() + 600}
        return token

    def idle_current(self, token):
        if not self.enabled:
            return True
        lease = self.state["idle"]
        return bool(lease and lease["token"] == token and lease["until"] > time.monotonic()
                    and lease["revision"] == self.state["revision"] and not any(self.state["turns"].values()))

    def release_idle(self, token):
        if self.state["idle"] and self.state["idle"]["token"] == token:
            self.state["idle"] = None

    def sent(self, origin, text, kind="initiative"):
        if self.enabled:
            self.state["last_sent"] = time.time()
            self.publish(kind, origin, {"text": text})

    def close(self):
        for owners in self.state["turns"].values():
            owners.pop(self.owner, None)
        lease = self.state["idle"]
        if lease and lease["owner"] == self.owner:
            self.state["idle"] = None
