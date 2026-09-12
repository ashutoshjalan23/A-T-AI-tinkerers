"""Small, bounded agents used by the Telegram conversation layer.

Only ``OrchestrationAgent`` and ``CalendarAgent`` call a model.  Map and live
location work deliberately stay deterministic: they are the source of truth for
coordinates, distance, and reminder delivery.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal

import db
import services
from config import OPENROUTER_API_KEY, OPENROUTER_MODEL
from geo import ERRAND_MINUTES, eta_minutes, haversine

log = logging.getLogger("detour.agents")


@dataclass
class AgentReply:
    """A conversational answer plus an optional action for the UI to render."""

    text: str
    result: Any = None


class MapAgent:
    """Deterministic map and geocoding worker. It never calls an LLM."""

    def resolve(self, query: str) -> dict | None:
        return services.resolve_place(query)

    def nearby(self, query: str, lat: float, lng: float) -> list[dict]:
        return services.nearby_places(query, lat, lng)

    def distance_to(self, lat: float, lng: float, task: dict) -> int:
        return round(haversine(lat, lng, task["lat"], task["lng"]))


class LiveLocationAgent:
    """Persistent location worker. Location messages are handled by Telegram."""

    def record(self, chat_id: int, lat: float, lng: float) -> None:
        db.set_last_location(chat_id, lat, lng)

    def status(self, chat_id: int) -> str:
        user = db.get_user(chat_id)
        if not user or user.get("last_lat") is None:
            return "I don't have a location from you yet. Share Live Location when you're ready."
        area = user.get("last_area") or "your last shared location"
        return f"Your last shared location is {area}."


class CalendarAgent:
    """LLM scheduler grounded in the deterministic calendar reader."""

    async def answer(self, user: dict, question: str) -> str:
        minutes, source, event = services.calendar_status(user)
        task_facts = []
        for task in db.open_tasks(user["chat_id"]):
            if user.get("last_lat") is None:
                task_facts.append(f"{task['title']} at {task['place_name']}: no shared location yet")
                continue
            distance = round(haversine(user["last_lat"], user["last_lng"], task["lat"], task["lng"]))
            eta = eta_minutes(distance, user["travel_mode"])
            task_facts.append(
                f"{task['title']} at {task['place_name']}: {distance}m away; "
                f"estimated round trip {eta * 2 + ERRAND_MINUTES} min"
            )
        facts = (
            f"Calendar source: {source}. Minutes until the next event: {minutes}. "
            f"Next event: {event or 'none'}. Outstanding errand estimates: "
            f"{' | '.join(task_facts) if task_facts else 'none'}.")
        prompt = f"""You are Detour's scheduling agent. Answer conversationally and briefly.
Use only these verified calendar facts; do not invent an event or availability.
You may explain whether an errand could fit, but cannot send reminders or change tasks.

{facts}

USER QUESTION: {question}"""
        return await _run_agent(prompt, "I couldn't read your calendar just now.")


class OrchestrationAgent:
    """The only agent users talk to. It delegates bounded jobs to three workers."""

    def __init__(
        self,
        create_from_parsed: Callable[[dict, dict], Any],
        complete_by_reference: Callable[[int, str], Any],
        make_context: Callable[[dict], str],
    ) -> None:
        self._create_from_parsed = create_from_parsed
        self._complete_by_reference = complete_by_reference
        self._make_context = make_context
        self.calendar = CalendarAgent()
        self.maps = MapAgent()
        self.live_location = LiveLocationAgent()
        self._result: Any = None

    async def respond(self, user: dict, text: str) -> AgentReply:
        """Run at most a few tool rounds, then return a normal-language reply."""
        if not OPENROUTER_API_KEY:
            return AgentReply(
                "I need an OpenRouter key before I can understand a new errand. "
                "You can still share your location or use /tasks."
            )
        try:
            tools = self._tools(user, text)
            prompt = f"""You are Detour, a helpful errand assistant. Hold a natural, concise
conversation. You can remember errands, discuss a user's calendar, and explain their
last shared location.

Use a tool before claiming that you created or completed an errand, read calendar
details, or know the user's location. Never invent a place, event, distance, or task.
For a new errand, call create_errand. For a completed errand, call complete_errand.
For calendar or scheduling questions, call ask_scheduler. For location questions, call
location_status. A real Telegram location message is recorded outside this conversation.

{self._make_context(user)}

USER MESSAGE: {text}"""
            answer = await _run_agent(prompt, "I couldn't process that just now.", tools)
            return AgentReply(answer, self._result)
        except Exception as exc:
            log.warning("orchestration failed: %s", exc)
            return AgentReply("I couldn't process that just now. Please try again.")

    def _tools(self, user: dict, question: str) -> list[Any]:
        """Create SDK tools late so the rest of the app still imports without its extras."""
        from pydantic import BaseModel, Field
        from openrouter_agent import tool

        owner = self

        class EmptyInput(BaseModel):
            pass

        class TextOutput(BaseModel):
            text: str

        class CreateErrandInput(BaseModel):
            title: str = Field(description="Short imperative errand title")
            place_query: str = Field(description="Specific shop plus Hong Kong, or an OSM shop category")
            kind: Literal["place", "category"] = Field(description="place for a named shop; category otherwise")

        class CompleteErrandInput(BaseModel):
            task_reference: str = Field(description="The title or place of the user's open errand")

        def create_errand(params: CreateErrandInput, _ctx: Any) -> TextOutput:
            owner._result = owner._create_from_parsed(
                user, {"intent": "create", "title": params.title,
                       "place_query": params.place_query, "kind": params.kind}
            )
            return TextOutput(text=_describe_result(owner._result))

        def complete_errand(params: CompleteErrandInput, _ctx: Any) -> TextOutput:
            owner._result = owner._complete_by_reference(user["chat_id"], params.task_reference)
            return TextOutput(text=_describe_result(owner._result))

        async def ask_scheduler(_params: EmptyInput, _ctx: Any) -> TextOutput:
            return TextOutput(text=await owner.calendar.answer(user, question))

        def location_status(_params: EmptyInput, _ctx: Any) -> TextOutput:
            return TextOutput(text=owner.live_location.status(user["chat_id"]))

        return [
            tool(name="create_errand", description="Create an errand at a named place or offer nearby choices.",
                 input_schema=CreateErrandInput, output_schema=TextOutput, execute=create_errand),
            tool(name="complete_errand", description="Close an existing errand named by the user.",
                 input_schema=CompleteErrandInput, output_schema=TextOutput, execute=complete_errand),
            tool(name="ask_scheduler", description="Ask the scheduling agent about verified calendar availability.",
                 input_schema=EmptyInput, output_schema=TextOutput, execute=ask_scheduler),
            tool(name="location_status", description="Read the user's last persisted live-location status.",
                 input_schema=EmptyInput, output_schema=TextOutput, execute=location_status),
        ]


def _describe_result(result: Any) -> str:
    if hasattr(result, "message"):
        return result.message
    if hasattr(result, "task"):
        task = result.task
        return f"Closed: {task['title']} at {task['place_name']}."
    if hasattr(result, "options"):
        return "I found a few nearby options. Please choose one."
    if isinstance(result, dict):
        return f"Created: {result['title']} at {result['place_name']}."
    return "The requested action is complete."


async def _run_agent(prompt: str, fallback: str, tools: list[Any] | None = None) -> str:
    """One bounded SDK run. Fail closed with a friendly message, never a traceback."""
    try:
        from openrouter_agent import OpenRouter, call_model, step_count_is

        client = OpenRouter(api_key=OPENROUTER_API_KEY)
        result = call_model(client, {
            "model": OPENROUTER_MODEL,
            "input": prompt,
            "tools": tools or [],
            "stop_when": [step_count_is(4)],
        })
        text = (await result.get_text()).strip()
        return text or fallback
    except Exception as exc:
        log.warning("agent SDK call failed: %s", exc)
        return fallback
