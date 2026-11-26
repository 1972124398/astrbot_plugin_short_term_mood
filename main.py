from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import ToolSet

from .mood import DEFAULT_AMBIENT_POOL, EMOTIONS, MoodState, MoodStore, emotion_name, finite_number, mood_candidates
from .prompts import APPRAISAL_SYSTEM, CODEX_NOTICE_AGENCY, MARKER, build_hint, parse_appraisal_result, profile_targets
from .interop import CompanionLink, conversation_key, event_kind, human_dialogue, task_history
from .time_context import ConversationClocks, TIME_CONTEXT, TIME_MARKER, time_hint


EVENT_HINT = "astrbot_short_term_mood.hint"


@dataclass(frozen=True)
class AppraisalJob:
    generation: int
    key: str
    persona: str
    origin: str
    provider_id: str
    prompt: str
    recent: list[dict[str, str]]
    images: list[str]
    proactive: bool
    queued_at: float
    preferences: str
    conversation_style: str
    banter_level: float
    event_kind: str = "dialogue"
    experience: dict | None = None
    time_context: dict | None = None
    return_context: dict | None = None
    observed_at: float = 0.0


class ShortTermMoodPlugin(Star):
    """A single emotional state shared by the entire agent."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.link = CompanionLink(context, "short_term_mood", config)
        self.store = MoodStore(Path(StarTools.get_data_dir("astrbot_plugin_short_term_mood")) / "mood.sqlite3")
        try:
            self.state = self.store.load()
        except (ValueError, TypeError, AttributeError):
            logger.warning("Short-term mood discarded an invalid saved state")
            self.state = MoodState()
        self.profiles = self.store.load_profiles()
        self.clocks = ConversationClocks(self.store.load_clocks())
        self.persistence_lock = asyncio.Lock()
        self.queue: asyncio.Queue[AppraisalJob] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.save_task: asyncio.Task | None = None
        self.dirty = False
        self.closing = False
        self.generation = 0
        self.retry_after = 0.0

    def companion_snapshot(self, origin=""):
        state = self.read_state()
        # A peer only supplies an origin, not the resolved persona. Export abstract affect only.
        result = {
            "levels": dict(state.levels),
            "ambient_emotion": state.ambient_emotion,
        }
        if origin and self.config.get("time_awareness", True):
            moment = self.clocks.sessions.get(conversation_key(origin))
            if moment:
                previous = max(moment.get("last_user", 0), moment.get("last_reply", 0))
                result["conversation_time"] = {"seconds_since_real_exchange": max(0, round(time.time() - previous))}
        return result

    def companion_observe(self, packet):
        if (self.closing or not self.config.get("enabled", True) or not self.link.enabled
                or packet["kind"] not in {"game_turn", "leisure_experience"}):
            return
        data = packet["data"]
        event = SimpleNamespace(
            unified_msg_origin=packet["origin"], get_extra=lambda _: None,
            message_obj=SimpleNamespace(message_id=packet["id"]),
            get_message_str=lambda: data.get("human_text", ""),
        )
        request = ProviderRequest(prompt=data.get("human_text", ""), system_prompt=data.get("persona", ""))
        experience = {key: value for key, value in data.items() if key != "persona"}
        experience["activity_kind"] = packet["kind"]
        job = self.snapshot(event, request)
        self.enqueue(replace(job, event_kind="shared_experience", experience=experience))

    @filter.on_llm_response(priority=-100)
    async def finish_response(self, event: AstrMessageEvent, response):
        self.link.end(event, response)
        if (event.get_extra(TIME_CONTEXT) and event_kind(event) == "dialogue"
                and not getattr(response, "tools_call_args", None) and not event.get_extra("short_term_mood.clock_finished")):
            self.clocks.finish(event.unified_msg_origin, getattr(response, "completion_text", "") or "", time.time())
            event.set_extra("short_term_mood.clock_finished", True)
            self.schedule_save()

    @filter.on_agent_done(priority=-100)
    async def finish_agent(self, event: AstrMessageEvent, run_context, response):
        await self.finish_response(event, response)
        self.link.end(event)

    def can_decline_notice(self, event):
        return (not self.closing and self.config.get("enabled", True)
                and self.config.get("enable_refusal", True) and self.config.get("allow_codex_notice_refusal", True)
                and event.get_extra("short_term_mood_skip") is not True and event_kind(event) == "task_event")

    def notice_tools(self, event, request):
        if request.func_tool is not None and hasattr(request.func_tool, "remove_tool"):
            request.func_tool.remove_tool("mood_ignore_codex_notice")
        manager = getattr(self.context, "get_llm_tool_manager", None)
        if not manager or not self.can_decline_notice(event):
            return
        tool = manager().get_func("mood_ignore_codex_notice")
        if tool is not None and getattr(tool, "active", True):
            if request.func_tool is None:
                request.func_tool = ToolSet()
            request.func_tool.add_tool(tool)

    @filter.on_llm_request(priority=-80)
    async def scope_notice_tools(self, event: AstrMessageEvent, request: ProviderRequest):
        self.notice_tools(event, request)

    @filter.llm_tool(name="mood_ignore_codex_notice")
    async def ignore_codex_notice(self, event: AstrMessageEvent, reason: str = "") -> str:
        """按你当前的心情静默放下这一条 Codex 后台播报，不向用户发送回复。决定不理时先调用，不要先输出文字。任务和结果保留。

        Args:
            reason(string): 此刻不想接话的一点心情或念头，仅作内部记录，可以留空。
        """
        if not self.can_decline_notice(event):
            return "这不是当前可静默放下的 Codex 播报，按当前对话正常接话。"
        event.set_extra("w1_notice_disposition", {"action": "ignore", "source": "short_term_mood", "reason": reason})
        event.set_extra("agent_stop_requested", True)
        event.clear_result()
        self.link.end(event)
        return "这条播报已先放下，无需再输出回复。任务与结果仍可查看。"

    @filter.on_llm_request(priority=10000)
    async def time_request(self, event: AstrMessageEvent, request: ProviderRequest):
        if (self.closing or not self.config.get("enabled", True) or not self.config.get("time_awareness", True)
                or event.get_extra("short_term_mood_skip") is True or event_kind(event) != "dialogue"):
            return
        data = event.get_extra(TIME_CONTEXT)
        if not isinstance(data, dict):
            history = request.contexts or []
            # Existing SDK history provides a useful first-install fallback.
            previous = getattr(getattr(request, "conversation", None), "updated_at", 0)
            previous = previous if isinstance(previous, (int, float)) and not any(task_history(item) for item in history) else 0
            fade = self.number("context_fade_minutes", 30, 1, 10080) * 60
            forget = self.number("context_set_aside_hours", 6, 0.01, 720) * 3600
            data = self.clocks.prepare(event.unified_msg_origin, history, request.prompt or event.get_message_str(), time.time(), fade, forget, previous)
            event.set_extra(TIME_CONTEXT, data)
            if data["phase"] == "set_aside":
                state = self.read_state()
                if state.note_scope == self.snapshot(event, request).key:
                    state.inner_state = state.response_tendency = ""
                    state.feeling_history.clear()
            self.schedule_save()
        if data["phase"] in {"fading", "set_aside"} or data["fresh_segment_since"]:
            if not any(isinstance(part, TextPart) and part.text.startswith(TIME_MARKER) for part in request.extra_user_content_parts):
                request.extra_user_content_parts.append(TextPart(text=time_hint(data, str(self.config.get("time_preferences", "") or ""))).mark_as_temp())

    def number(self, key: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = finite_number(self.config.get(key, default))
        except ValueError:
            value = default
        return min(maximum, max(minimum, value))

    def read_state(self) -> MoodState:
        """Read-only foreground access; all recovery runs in the background worker."""
        return self.state

    def recover_state(self) -> MoodState:
        if not self.config.get("natural_recovery_enabled", False):
            return self.state
        idle_seconds = 0
        if self.config.get("sustain_during_conversation", True):
            idle_seconds = self.number("conversation_idle_seconds", 300, 0, 86400)
        self.state.recover(
            time.time(),
            self.number("half_life_seconds", 300, 10, 86400),
            self.number("recovery_seconds", 1200, 30, 86400),
            idle_seconds,
        )
        return self.state

    def cached_profile(self, key: str) -> dict[str, str] | None:
        cached = self.profiles.get(key)
        if cached and time.time() - cached[1] < self.number("profile_cache_seconds", 604800, 60, 2592000):
            return cached[0]
        return None

    def begin_interaction(self, observed_at: float) -> tuple[MoodState, dict | None]:
        state = self.state
        now = observed_at
        gap = max(0, now - state.last_interaction) if state.last_interaction else 0
        threshold = self.number("random_mood_after_seconds", 21600, 60, 2592000)
        renewed = state.begin_interaction(
            now,
            random_after=threshold if self.config.get("random_mood_on_return", True) else 0,
        )
        return_context = None
        if renewed:
            return_context = {
                "idle_seconds": round(gap),
                "candidate_moods": mood_candidates(self.config.get("random_mood_pool", DEFAULT_AMBIENT_POOL)),
                "strength_reference": self.number("random_mood_strength", 0.3, 0.1, 0.6),
            }
        self.schedule_save()
        return state, return_context

    async def persist_state(self):
        async with self.persistence_lock:
            snapshot = MoodState.from_dict(self.state.as_dict())
            clocks = copy.deepcopy(self.clocks.sessions)
            await self.write_store(self.store.save, snapshot)
            await self.write_store(self.store.save_clocks, clocks)

    async def write_store(self, operation, *args):
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Finish the thread before a cancelled caller releases the database lock.
            await task
            raise

    def schedule_save(self):
        self.dirty = True
        if self.save_task is None or self.save_task.done():
            self.save_task = asyncio.create_task(self.save_changes(), name="short-term-mood-save")

    async def save_changes(self):
        try:
            while self.dirty:
                self.dirty = False
                await self.persist_state()
        except Exception as error:
            self.dirty = True
            logger.warning("Short-term mood persistence unavailable (%s)", type(error).__name__)

    def invalidate_pending(self):
        self.generation += 1
        self.retry_after = 0.0
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()

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
        for message in human_dialogue(request.contexts or [])[-6:]:
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

    def snapshot(self, event: AstrMessageEvent, request: ProviderRequest) -> AppraisalJob:
        persona = self.persona_prompt(event, request)
        provider_id = str(self.config.get("analysis_provider_id", "") or "").strip()
        preferences = str(self.config.get("expression_preferences", "") or "")[:2000]
        conversation_style = str(self.config.get("conversation_style", "casual"))
        banter_level = self.number("banter_level", 0.6, 0, 1)
        key = hashlib.sha256(
            json.dumps(
                [
                    "v9-scoped",
                    str(event.unified_msg_origin),
                    list(EMOTIONS),
                    persona,
                    provider_id,
                    request.model,
                    preferences,
                    conversation_style,
                    banter_level,
                ],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        proactive = event_kind(event) == "proactive"
        temporal = event.get_extra(TIME_CONTEXT)
        recent_request = request
        if isinstance(temporal, dict):
            recent_request = copy.copy(request)
            recent_request.contexts = temporal["recent_dialogue"]
        return AppraisalJob(
            generation=self.generation,
            key=key,
            persona=persona,
            origin=event.unified_msg_origin,
            provider_id=provider_id,
            prompt="" if proactive else (request.prompt or event.get_message_str() or "")[-10000:],
            recent=[] if proactive else self.recent_dialogue(recent_request),
            images=list(request.image_urls or [])[:2] if self.config.get("analyze_images", True) and not proactive else [],
            proactive=proactive,
            queued_at=time.monotonic(),
            observed_at=time.time(),
            preferences=preferences,
            conversation_style=conversation_style,
            banter_level=banter_level,
            event_kind=event_kind(event),
            time_context={key: value for key, value in temporal.items() if key != "recent_dialogue"} if isinstance(temporal, dict) else None,
        )

    def enqueue(self, job: AppraisalJob):
        if self.closing or (job.proactive and self.cached_profile(job.key) is not None):
            return
        limit = int(self.number("analysis_queue_size", 16, 1, 256))
        while self.queue.qsize() >= limit:
            self.queue.get_nowait()
            self.queue.task_done()
        self.queue.put_nowait(job)
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.analyze_queue(), name="short-term-mood-analysis")

    def job_current(self, job: AppraisalJob) -> bool:
        return (
            job.generation == self.generation
            and not self.closing
            and bool(self.config.get("enabled", True))
            and time.monotonic() - job.queued_at <= self.number("analysis_max_age_seconds", 120, 1, 3600)
            and job.observed_at >= self.clocks.sessions.get(conversation_key(job.origin), {}).get("break_at", 0)
        )

    async def appraise(self, job: AppraisalJob):
        provider_id = job.provider_id or await self.context.get_current_chat_provider_id(job.origin)
        profiles = self.cached_profile(job.key)
        state = self.read_state()
        targets = profile_targets(state.levels, profiles)
        notes = state.scoped_notes(job.key)
        if job.time_context and job.time_context.get("phase") == "set_aside":
            notes = {"inner_state": "", "response_tendency": "", "feeling_history": []}
        payload = {
            "persona": job.persona,
            "current_mood": dict(state.levels),
            "ambient_emotion": state.ambient_emotion,
            "previous_inner_state": notes["inner_state"],
            "previous_response_tendency": notes["response_tendency"],
            "expression_preferences": job.preferences,
            "conversation_style": job.conversation_style,
            "banter_level": job.banter_level,
            "sensitivity_preference": self.number("sensitivity", 1.0, 0.1, 2.0),
            "emotional_inertia_preference": self.number(
                "emotion_inertia", 0.35, 0, 0.9
            ),
            "feeling_history": notes["feeling_history"],
            "event_kind": job.event_kind,
            "shared_experience": job.experience,
            "time_context": job.time_context,
            "return_context": job.return_context,
            "recent_dialogue": job.recent,
            "current_input": job.prompt,
            "need_profiles": bool(targets),
            "profile_emotions": targets,
        }
        # The direct SDK call avoids recursively dispatching dialogue hooks or tools.
        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=APPRAISAL_SYSTEM,
            prompt=json.dumps(payload, ensure_ascii=False),
            image_urls=job.images,
        )
        result = parse_appraisal_result(response.completion_text, bool(targets), targets)
        if result.profiles is not None:
            result.profiles = {**(profiles or {}), **result.profiles}
        if job.proactive:
            result.deltas, result.inner_state, result.tendency, result.next_mood = {}, "", "", None
        return result

    async def process_job(self, job: AppraisalJob):
        if not self.job_current(job):
            return
        if not job.proactive:
            self.recover_state()
            if job.event_kind == "dialogue" or (job.experience or {}).get("activity_kind") == "game_turn":
                _, return_context = self.begin_interaction(job.observed_at)
                job = replace(job, return_context=return_context)
                if job.event_kind == "shared_experience" and not job.experience.get("handled_by_chat"):
                    self.clocks.human_activity(job.origin, job.observed_at)
            self.schedule_save()
        if time.monotonic() < self.retry_after:
            return
        async with asyncio.timeout(self.number("analysis_timeout_seconds", 30, 1, 120)):
            result = await self.appraise(job)
        # A reset, manual change, reload, or stale queue entry invalidates late results.
        if not self.job_current(job):
            return
        state = self.read_state()
        if not job.proactive and result.confidence >= 0.6:
            if result.next_mood is not None:
                state.adopt_mood(result.next_mood, time.time())
            else:
                state.apply(
                    result.deltas, time.time(), sensitivity=1.0, inertia=0,
                )
            if state.note_scope != job.key:
                state.inner_state = state.response_tendency = ""
                state.feeling_history.clear()
            state.note_scope = job.key
            state.remember_feeling(result.inner_state, result.tendency)
        self.schedule_save()
        generated = result.profiles
        if generated is not None:
            created = time.time()
            self.profiles[job.key] = (generated, created)
            if len(self.profiles) > 100:
                self.profiles.pop(min(self.profiles, key=lambda key: self.profiles[key][1]))
            async with self.persistence_lock:
                if job.generation == self.generation:
                    await self.write_store(
                        self.store.save_profile, job.key, generated, created,
                        self.number("profile_cache_seconds", 604800, 60, 2592000),
                    )

    async def analyze_queue(self):
        while True:
            job = await self.queue.get()
            try:
                await self.process_job(job)
            except Exception as error:
                if job.generation == self.generation:
                    delay = self.number("analysis_retry_seconds", 30, 0, 3600)
                    self.retry_after = time.monotonic() + delay
                    logger.warning("Short-term mood background appraisal unavailable (%s), retrying after %ss", type(error).__name__, delay)
            finally:
                self.queue.task_done()

    @filter.on_llm_request(priority=-100)
    async def apply_mood(self, event: AstrMessageEvent, request: ProviderRequest):
        self.notice_tools(event, request)
        if self.closing or not self.config.get("enabled", True):
            return
        if event.get_extra("short_term_mood_skip") is True or event.is_stopped():
            return
        await self.time_request(event, request)
        if event_kind(event) != "proactive":
            self.link.begin(event)
            self.link.inject(event, request)
        message = (event.get_message_str() or "").lstrip()
        prefixes = self.config.get("command_prefixes", ["/", "!"])
        if any(message.startswith(prefix) for prefix in prefixes if isinstance(prefix, str) and prefix):
            return
        hint = event.get_extra(EVENT_HINT)
        if not isinstance(hint, str):
            state = self.read_state()
            job = self.snapshot(event, request)
            notes = state.scoped_notes(job.key)
            if job.time_context and job.time_context.get("phase") == "set_aside":
                notes = {"inner_state": "", "response_tendency": "", "feeling_history": []}
            hint = build_hint(
                state.levels, self.cached_profile(job.key),
                bool(self.config.get("enable_refusal", True)) and (job.event_kind != "task_event" or self.can_decline_notice(event)),
                expression_strength=self.number("expression_strength", 1.0, 0.1, 2.0),
                inner_state=notes["inner_state"],
                response_tendency=notes["response_tendency"],
                preferences=job.preferences,
                conversation_style=job.conversation_style,
                banter_level=job.banter_level,
                feeling_history=notes["feeling_history"],
                ambient_emotion=state.ambient_emotion,
            )
            if job.event_kind == "task_event" and self.can_decline_notice(event):
                hint += "\n\n" + CODEX_NOTICE_AGENCY
            event.set_extra(EVENT_HINT, hint)
            self.enqueue(job)
        if not any(isinstance(part, TextPart) and part.text.startswith(MARKER) for part in request.extra_user_content_parts):
            request.extra_user_content_parts.append(TextPart(text=hint).mark_as_temp())

    @filter.command("mood")
    async def mood_status(self, event: AstrMessageEvent):
        """查看智能体此刻跨所有会话共享的情绪。"""
        state = self.read_state()
        levels = sorted(state.levels.items(), key=lambda item: item[1], reverse=True)
        self.schedule_save()
        description = "、".join(f"{EMOTIONS[name]} {value:.0%}" for name, value in levels) or "平稳"
        details = [f"当前共享心境为{description}。"]
        if state.ambient_emotion:
            details.append("心境底色　久未互动后自然浮现，后续会随交流变化。")
        yield event.plain_result("\n".join(details))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mood_reset")
    async def mood_reset(self, event: AstrMessageEvent):
        """将智能体的共享情绪恢复平稳。"""
        self.invalidate_pending()
        self.state = MoodState(updated=time.time())
        await self.persist_state()
        yield event.plain_result("共享心境已恢复平稳。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mood_set")
    async def mood_set(self, event: AstrMessageEvent, emotion: str, strength: float = 0.8):
        """设置共享情绪，用于调试。"""
        name = emotion_name(emotion)
        try:
            value = finite_number(strength)
            if name not in EMOTIONS or not 0.04 <= value <= 1.0:
                raise ValueError("Invalid mood setting")
        except ValueError:
            yield event.plain_result(f"用法 /mood_set <{'|'.join(EMOTIONS)}> [0.04～1]，也支持中文情绪名。")
            return
        self.invalidate_pending()
        now = time.time()
        self.state = MoodState(levels={name: value}, stimulated={name: now}, updated=now, last_interaction=now)
        await self.persist_state()
        yield event.plain_result(f"共享心境已设为{EMOTIONS[name]} {value:.0%}，随后会自然恢复。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mood_refresh")
    async def mood_refresh(self, event: AstrMessageEvent):
        """清除人格情绪表达缓存，下次对话重新生成。"""
        self.invalidate_pending()
        self.profiles.clear()
        async with self.persistence_lock:
            await self.write_store(self.store.clear_profiles)
        yield event.plain_result("已清除情绪表达缓存，下次对话会按主提示词重新生成。")

    async def terminate(self):
        if self.closing:
            return
        self.closing = True
        self.link.close()
        self.invalidate_pending()
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.save_task is not None:
            await self.save_task
        try:
            await self.persist_state()
        finally:
            await self.write_store(self.store.close)
