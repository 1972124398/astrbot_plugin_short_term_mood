from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .mood import EMOTIONS, MoodState, MoodStore, finite_number
from .prompts import APPRAISAL_SYSTEM, MARKER, build_hint, parse_appraisal


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


class ShortTermMoodPlugin(Star):
    """A single emotional state shared by the entire agent."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = MoodStore(Path(StarTools.get_data_dir("astrbot_plugin_short_term_mood")) / "mood.sqlite3")
        try:
            self.state = self.store.load()
        except (ValueError, TypeError, AttributeError):
            logger.warning("Short-term mood discarded an invalid saved state")
            self.state = MoodState()
        self.profiles = self.store.load_profiles()
        self.persistence_lock = asyncio.Lock()
        self.queue: asyncio.Queue[AppraisalJob] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.save_task: asyncio.Task | None = None
        self.dirty = False
        self.closing = False
        self.generation = 0
        self.retry_after = 0.0

    def number(self, key: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = finite_number(self.config.get(key, default))
        except ValueError:
            value = default
        return min(maximum, max(minimum, value))

    def read_state(self) -> MoodState:
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

    async def persist_state(self):
        async with self.persistence_lock:
            snapshot = MoodState.from_dict(self.state.as_dict())
            await self.write_store(self.store.save, snapshot)

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

    def snapshot(self, event: AstrMessageEvent, request: ProviderRequest) -> AppraisalJob:
        persona = self.persona_prompt(event, request)
        provider_id = str(self.config.get("analysis_provider_id", "") or "").strip()
        key = hashlib.sha256(json.dumps(["v2", persona, provider_id, request.model], ensure_ascii=False).encode()).hexdigest()
        message_id = getattr(getattr(event, "message_obj", None), "message_id", "")
        proactive = str(message_id).startswith("proactive:")
        return AppraisalJob(
            generation=self.generation,
            key=key,
            persona=persona,
            origin=event.unified_msg_origin,
            provider_id=provider_id,
            prompt="" if proactive else (request.prompt or event.get_message_str() or "")[-10000:],
            recent=[] if proactive else self.recent_dialogue(request),
            images=list(request.image_urls or [])[:2] if self.config.get("analyze_images", True) and not proactive else [],
            proactive=proactive,
            queued_at=time.monotonic(),
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
        )

    async def appraise(self, job: AppraisalJob):
        provider_id = job.provider_id or await self.context.get_current_chat_provider_id(job.origin)
        profiles = self.cached_profile(job.key)
        payload = {
            "persona": job.persona,
            "current_mood": dict(self.read_state().levels),
            "event_kind": "proactive" if job.proactive else "dialogue",
            "recent_dialogue": job.recent,
            "current_input": job.prompt,
            "need_profiles": profiles is None,
        }
        # The direct SDK call avoids recursively dispatching dialogue hooks or tools.
        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=APPRAISAL_SYSTEM,
            prompt=json.dumps(payload, ensure_ascii=False),
            image_urls=job.images,
        )
        deltas, generated = parse_appraisal(response.completion_text, profiles is None)
        return ({} if job.proactive else deltas), generated

    async def process_job(self, job: AppraisalJob):
        if not self.job_current(job) or time.monotonic() < self.retry_after:
            return
        async with asyncio.timeout(self.number("analysis_timeout_seconds", 15, 1, 120)):
            deltas, generated = await self.appraise(job)
        # A reset, manual change, reload, or stale queue entry invalidates late results.
        if not self.job_current(job):
            return
        state = self.read_state()
        state.apply(deltas, time.time(), self.number("sensitivity", 1.0, 0.1, 2.0))
        self.schedule_save()
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
        if self.closing or not self.config.get("enabled", True):
            return
        if event.get_extra("short_term_mood_skip") is True or event.is_stopped():
            return
        message = (event.get_message_str() or "").lstrip()
        prefixes = self.config.get("command_prefixes", ["/", "!"])
        if any(message.startswith(prefix) for prefix in prefixes if isinstance(prefix, str) and prefix):
            return
        hint = event.get_extra(EVENT_HINT)
        if not isinstance(hint, str):
            job = self.snapshot(event, request)
            state = self.read_state()
            if not job.proactive:
                state.last_interaction = time.time()
            refuse = state.choose_refusal(
                time.time(),
                allowed=not job.proactive,
                enabled=bool(self.config.get("enable_refusal", True)),
                threshold=self.number("refusal_threshold", 0.65, 0.1, 1.0),
                probability=self.number("refusal_probability", 0.3, 0, 1),
                cooldown=self.number("refusal_cooldown_seconds", 180, 0, 86400),
                draw=random.random(),
            )
            hint = build_hint(state.levels, self.cached_profile(job.key), refuse)
            event.set_extra(EVENT_HINT, hint)
            self.schedule_save()
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
        yield event.plain_result(f"当前共享心境为{description}。情绪会随时间自动恢复。")

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
        aliases = {label: name for name, label in EMOTIONS.items()}
        name = aliases.get(emotion, emotion)
        try:
            value = finite_number(strength)
            if name not in EMOTIONS or not 0.04 <= value <= 1.0:
                raise ValueError("Invalid mood setting")
        except ValueError:
            yield event.plain_result("用法 /mood_set <joy|affection|sadness|anger|anxiety|fatigue|curiosity> [0.04～1]，也支持中文情绪名。")
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
