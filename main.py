from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .mood import EMOTIONS, MoodState, MoodStore, finite_number
from .prompts import APPRAISAL_SYSTEM, MARKER, build_hint, parse_appraisal


EVENT_HINT = "astrbot_short_term_mood.hint"


class ShortTermMoodPlugin(Star):
    """A single emotional state shared by the entire agent."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.lock = asyncio.Lock()
        self.store = MoodStore(Path(StarTools.get_data_dir("astrbot_plugin_short_term_mood")) / "mood.sqlite3")
        self.retry_after = 0.0

    def number(self, key: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = finite_number(self.config.get(key, default))
        except ValueError:
            value = default
        return min(maximum, max(minimum, value))

    def read_state(self) -> MoodState:
        try:
            state = self.store.load()
        except (ValueError, TypeError, AttributeError):
            logger.warning("Short-term mood discarded an invalid saved state")
            state = MoodState()
        state.recover(
            time.time(),
            self.number("half_life_seconds", 300, 10, 86400),
            self.number("recovery_seconds", 1200, 30, 86400),
        )
        return state

    def persona_prompt(self, event: AstrMessageEvent, request: ProviderRequest) -> str:
        persona_id = event.get_extra("_trace_persona_id")
        if persona_id == "[%None]":
            return ""
        if persona_id == "_chatui_default_":
            from astrbot.core.astr_main_agent_resources import CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT

            return CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT if event.get_extra("enable_default_system_prompt") is not False else ""
        if persona_id:
            persona = self.context.persona_manager.get_persona_v3_by_id(persona_id)
            if persona:
                return str(persona.get("prompt") or "")
        return request.system_prompt or ""

    @staticmethod
    def recent_dialogue(request: ProviderRequest) -> list[dict[str, str]]:
        recent = []
        for message in (request.contexts or [])[-6:]:
            if message.get("role") not in {"user", "assistant"}:
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text" and not part.get("_no_save")
                )
            if isinstance(content, str):
                recent.append({"role": message["role"], "content": content[-800:]})
        return recent

    async def appraise(self, event: AstrMessageEvent, request: ProviderRequest, state: MoodState):
        provider_id = str(self.config.get("analysis_provider_id", "") or "").strip()
        if not provider_id:
            provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        persona = self.persona_prompt(event, request)
        key = hashlib.sha256(json.dumps(["v1", persona, provider_id, request.model], ensure_ascii=False).encode()).hexdigest()
        profiles = self.store.profile(key, time.time())
        message_id = getattr(getattr(event, "message_obj", None), "message_id", "")
        proactive = str(message_id).startswith("proactive:")
        if proactive and profiles is not None:
            return {}, False, profiles
        payload = {
            "persona": persona,
            "current_mood": state.levels,
            "event_kind": "proactive" if proactive else "dialogue",
            "recent_dialogue": [] if proactive else self.recent_dialogue(request),
            "current_input": "" if proactive else (request.prompt or event.get_message_str() or "")[-10000:],
            "need_profiles": profiles is None,
        }
        # The direct SDK call avoids recursively dispatching dialogue hooks or tools.
        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=APPRAISAL_SYSTEM,
            prompt=json.dumps(payload, ensure_ascii=False),
            image_urls=list(request.image_urls or [])[:2] if self.config.get("analyze_images", True) else [],
        )
        deltas, allowed, generated = parse_appraisal(response.completion_text, profiles is None)
        if generated is not None:
            self.store.save_profile(key, generated, time.time())
            profiles = generated
        if proactive:
            return {}, False, profiles
        return deltas, allowed, profiles

    @filter.on_llm_request(priority=-100)
    async def apply_mood(self, event: AstrMessageEvent, request: ProviderRequest):
        if not self.config.get("enabled", True):
            return
        if event.get_extra("short_term_mood_skip") is True or event.is_stopped():
            return
        message = (event.get_message_str() or "").lstrip()
        prefixes = self.config.get("command_prefixes", ["/", "!"])
        if any(message.startswith(prefix) for prefix in prefixes if isinstance(prefix, str) and prefix):
            return
        async with self.lock:
            hint = event.get_extra(EVENT_HINT)
            if not isinstance(hint, str):
                state = self.read_state()
                allowed, profiles = False, None
                deltas = {}
                if time.monotonic() >= self.retry_after:
                    try:
                        async with asyncio.timeout(self.number("analysis_timeout_seconds", 15, 1, 120)):
                            deltas, allowed, profiles = await self.appraise(event, request, state)
                    except Exception as error:
                        # Existing affect continues recovering if appraisal is temporarily unavailable.
                        self.retry_after = time.monotonic() + 30
                        logger.warning("Short-term mood appraisal unavailable (%s), retrying after 30s", type(error).__name__)
                state.recover(
                    time.time(),
                    self.number("half_life_seconds", 300, 10, 86400),
                    self.number("recovery_seconds", 1200, 30, 86400),
                )
                state.apply(deltas, time.time(), self.number("sensitivity", 1.0, 0.1, 2.0))
                refuse = state.choose_refusal(
                    time.time(),
                    allowed=allowed,
                    enabled=bool(self.config.get("enable_refusal", True)),
                    threshold=self.number("refusal_threshold", 0.65, 0.1, 1.0),
                    probability=self.number("refusal_probability", 0.3, 0, 1),
                    cooldown=self.number("refusal_cooldown_seconds", 180, 0, 86400),
                    draw=random.random(),
                )
                hint = build_hint(state.levels, profiles, refuse)
                self.store.save(state)
                event.set_extra(EVENT_HINT, hint)
            if not any(isinstance(part, TextPart) and part.text.startswith(MARKER) for part in request.extra_user_content_parts):
                request.extra_user_content_parts.append(TextPart(text=hint).mark_as_temp())

    @filter.command("mood")
    async def mood_status(self, event: AstrMessageEvent):
        """查看智能体此刻跨所有会话共享的情绪。"""
        async with self.lock:
            state = self.read_state()
            self.store.save(state)
            levels = sorted(state.levels.items(), key=lambda item: item[1], reverse=True)
        description = "、".join(f"{EMOTIONS[name]} {value:.0%}" for name, value in levels) or "平稳"
        yield event.plain_result(f"当前共享心境为{description}。情绪会随时间自动恢复。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mood_reset")
    async def mood_reset(self, event: AstrMessageEvent):
        """将智能体的共享情绪恢复平稳。"""
        async with self.lock:
            self.store.save(MoodState(updated=time.time()))
        yield event.plain_result("共享心境已恢复平稳。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mood_set")
    async def mood_set(self, event: AstrMessageEvent, emotion: str, strength: float = 0.8):
        """设置共享情绪，用于调试。"""
        aliases = {label: name for name, label in EMOTIONS.items()}
        name = aliases.get(emotion, emotion)
        try:
            value = finite_number(strength)
            if name not in EMOTIONS or not 0.04 <= value <= 1.0:
                raise ValueError("Invalid mood setting")
        except ValueError:
            yield event.plain_result("用法 /mood_set <joy|affection|sadness|anger|anxiety|fatigue|curiosity> [0.04～1]，也支持中文情绪名。")
            return
        async with self.lock:
            now = time.time()
            self.store.save(MoodState(levels={name: value}, stimulated={name: now}, updated=now))
        yield event.plain_result(f"共享心境已设为{EMOTIONS[name]} {value:.0%}，随后会自然恢复。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mood_refresh")
    async def mood_refresh(self, event: AstrMessageEvent):
        """清除人格情绪表达缓存，下次对话重新生成。"""
        async with self.lock:
            self.store.clear_profiles()
            self.retry_after = 0.0
        yield event.plain_result("已清除情绪表达缓存，下次对话会按主提示词重新生成。")

    async def terminate(self):
        async with self.lock:
            self.store.close()
