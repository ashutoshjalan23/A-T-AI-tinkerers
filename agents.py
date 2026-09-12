"""Bounded LLM orchestration with Pydantic JSON tool contracts."""
from __future__ import annotations

import inspect, logging
from dataclasses import dataclass
from typing import Any, Callable, Literal
from pydantic import BaseModel, Field
import db, services

from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL
from geo import ERRAND_MINUTES, eta_minutes, haversine

log = logging.getLogger("detour.agents")

class CalendarEvent(BaseModel): title: str; starts_at: str; ends_at: str | None = None
class CalendarFacts(BaseModel):
    source: str; minutes_until_next: int; next_event: str | None; current_event: CalendarEvent | None = None
class TaskFacts(BaseModel):
    id: int; title: str; place_name: str; scheduled_for: str | None = None; round_trip_min: int | None = None
class ToolResult(BaseModel):
    ok: bool; data: dict[str, Any] = Field(default_factory=dict); error: str | None = None
class CreateErrandInput(BaseModel): title: str; place_query: str; kind: Literal["place", "category"]
class CompleteErrandInput(BaseModel): task_reference: str
class ScheduleErrandInput(BaseModel): task_reference: str; scheduled_for: str = Field(description="ISO-8601 time with timezone")
class AgentReply(BaseModel): text: str; result: Any = None; action_kind: str | None = None
@dataclass

class AgentTool:
    name: str; description: str; input_model: type[BaseModel]; execute: Callable[[BaseModel], Any]
    def schema(self) -> dict: return {"type":"function","function":{"name":self.name,"description":self.description,"parameters":self.input_model.model_json_schema()}}

class MapAgent:
    def resolve(self, query: str) -> dict | None: return services.resolve_place(query)
    def nearby(self, query: str, lat: float, lng: float) -> list[dict]: return services.nearby_places(query, lat, lng)
    def distance_to(self, lat: float, lng: float, task: dict) -> int: return round(haversine(lat,lng,task["lat"],task["lng"]))

class LiveLocationAgent:
    
    def record(self, chat_id:int, lat:float, lng:float)->None: db.set_last_location(chat_id,lat,lng)
    
    def facts(self, chat_id:int)->ToolResult:
        user=db.get_user(chat_id)
        return ToolResult(ok=True,data={"known":bool(user and user.get("last_lat") is not None),"area":user.get("last_area") if user else None,"updated_at":user.get("last_seen_at") if user else None})

class CalendarAgent:
    async def facts(self,user:dict)->ToolResult:
        calendar=CalendarFacts.model_validate(services.calendar_snapshot(user)); tasks=[]
        for t in db.open_tasks(user["chat_id"]):
            trip=None
            if user.get("last_lat") is not None: trip=eta_minutes(haversine(user["last_lat"],user["last_lng"],t["lat"],t["lng"]),user["travel_mode"])*2+ERRAND_MINUTES
            tasks.append(TaskFacts(id=t["id"],title=t["title"],place_name=t["place_name"],scheduled_for=t.get("scheduled_for"),round_trip_min=trip))
        return ToolResult(ok=True,data={"calendar":calendar.model_dump(),"tasks":[t.model_dump() for t in tasks]})

class OrchestrationAgent:
    
    def __init__(self, create_from_parsed, complete_by_reference, schedule_task, make_context):
        self.create_from_parsed,self.complete_by_reference,self.schedule_task,self.make_context=create_from_parsed,complete_by_reference,schedule_task,make_context; self.calendar=CalendarAgent(); self.maps=MapAgent(); self.live_location=LiveLocationAgent(); self.action=None
    
    
    async def respond(self,user:dict,text:str)->AgentReply:
        if not OPENROUTER_API_KEY:return AgentReply(text="I need an OpenRouter key to help with errands and scheduling.")
        prompt=f"""
        ## Introduction
        You are Detour, a helpful errand assistant. Hold natural, concise conversations that help people remember, plan, and complete real-world errands around their schedule and location.

        ## Core capabilities
        - Create an errand: use create_errand whenever the user asks to remember, add, schedule, or be reminded about an errand.
        - Complete an errand: use complete_errand when an existing errand is done, collected, purchased, or no longer needed.
        - Check calendar and schedule: use calendar_facts for current running and upcoming events, availability, conflicts, or planning around time.
        - Check location: use location_facts when answering whether a location is known.

        ## Scheduling workflow
        Look up calendar facts before discussing availability. Identify whether the practical window is before the next event, between events, or after the currently running event. `current_event.starts_at` is the start; `current_event.ends_at` is the end. When the user says “after this meeting”, use `current_event.ends_at`, never its start. To create or edit a reminder time, call schedule_errand with a verified ISO-8601 timestamp including timezone.

        ## Tool contract
        Every tool returns an API-style JSON envelope: `{{"ok": boolean, "data": object, "error": string|null}}`. This JSON is internal context only. Your final response is the only user-visible natural language: do not paste, quote, or format a tool response as your reply.

        ## Guidelines
        Never invent a place, event, distance, current location, calendar availability, or errand status. Confirm completed actions briefly. If a tool fails or returns no result, say so plainly and offer the next useful step. Do not make proximity, travel-time, or reminder-fire decisions yourself; deterministic application code does that.

        CONTEXT:\n{self.make_context(user)}\n
        USER QUERY: {text}"""
        return AgentReply(text=await _run_agent(prompt,self._tools(user),"I couldn't process that just now."),result=self.action,action_kind=getattr(self,"action_kind",None))
    
    
    def _tools(self,user:dict)->list[AgentTool]:
        owner=self
        class Empty(BaseModel): pass
       
        def create(p:CreateErrandInput)->ToolResult:
            r=owner.create_from_parsed(user,{"intent":"create",**p.model_dump()}); owner.action=r; owner.action_kind="create"; return ToolResult(ok=not hasattr(r,"message"),data={"task":r if isinstance(r,dict) else {}},error=getattr(r,"message",None))
        
        def complete(p:CompleteErrandInput)->ToolResult:
            r=owner.complete_by_reference(user["chat_id"],p.task_reference); owner.action=r; owner.action_kind="complete"; return ToolResult(ok=not hasattr(r,"message"),data={"task":getattr(r,"task",{})},error=getattr(r,"message",None))
        
        def schedule(p:ScheduleErrandInput)->ToolResult:
            r=owner.schedule_task(user["chat_id"],p.task_reference,p.scheduled_for); owner.action=r; owner.action_kind="schedule"; return ToolResult(ok=not hasattr(r,"message"),data={"task":r if isinstance(r,dict) else {}},error=getattr(r,"message",None))
        
        async def calendar(_:Empty)->ToolResult:return await owner.calendar.facts(user)
        
        def location(_:Empty)->ToolResult:return owner.live_location.facts(user["chat_id"])
        
        return [AgentTool("create_errand","Create an errand.",CreateErrandInput,create),AgentTool("complete_errand","Complete an errand.",CompleteErrandInput,complete),AgentTool("schedule_errand","Create or edit an errand schedule.",ScheduleErrandInput,schedule),AgentTool("calendar_facts","Read current/upcoming calendar JSON.",Empty,calendar),AgentTool("location_facts","Read persisted location JSON.",Empty,location)]

async def _run_agent(prompt:str,tools:list[AgentTool],fallback:str)->str:
    try:
        from openai import AsyncOpenAI
        client=AsyncOpenAI(base_url=OPENROUTER_BASE_URL,api_key=OPENROUTER_API_KEY); messages=[{"role":"user","content":prompt}]; by_name={t.name:t for t in tools}
        
        for _ in range(4):
            response=await client.chat.completions.create(model=OPENROUTER_MODEL,temperature=0,messages=messages,tools=[t.schema() for t in tools],tool_choice="auto"); message=response.choices[0].message
            if not message.tool_calls:return (message.content or fallback).strip()
            messages.append({"role":"assistant","content":message.content or "","tool_calls":[c.model_dump() for c in message.tool_calls]})
            for call in message.tool_calls:
                try:
                    tool=by_name[call.function.name]; value=tool.execute(tool.input_model.model_validate_json(call.function.arguments)); value=await value if inspect.isawaitable(value) else value; payload=value.model_dump_json()
                except Exception as exc: log.warning("tool call failed: %s",exc); payload=ToolResult(ok=False,error="Tool unavailable").model_dump_json()
                messages.append({"role":"tool","tool_call_id":call.id,"content":payload})
        
        final=await client.chat.completions.create(model=OPENROUTER_MODEL,temperature=0,messages=messages)
        
        return (final.choices[0].message.content or fallback).strip()
    except Exception as exc: 
        log.warning("agent loop failed: %s",exc); return fallback
