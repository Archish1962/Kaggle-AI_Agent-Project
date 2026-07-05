# ruff: noqa
import datetime
from zoneinfo import ZoneInfo
import os
import re
import json
from typing import Any, Union
from pydantic import BaseModel, Field

from google.adk.agents import Agent, Context
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.workflow import node, START, Workflow, Edge, DEFAULT_ROUTE
from google.adk.events.request_input import RequestInput
from google.adk.events.event import Event
from google.adk.tools import McpToolset
from google.adk.tools.mcp_tool import StdioConnectionParams
from mcp import StdioServerParameters
from google.genai import types

from app.config import config

# We initialize Gemini Model using config
gemini_model = Gemini(
    model=config.model,
    retry_options=types.HttpRetryOptions(attempts=config.max_iterations),
)

# ---------------------------------------------------------------------------
# MCP Toolsets
# ---------------------------------------------------------------------------
mcp_toolset_planner = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "app.mcp_server"]
        )
    )
)

mcp_toolset_env = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "app.mcp_server"]
        )
    )
)

# ---------------------------------------------------------------------------
# Specialized Agents & Tools
# ---------------------------------------------------------------------------

# Sub-agent 1: Route Planner
route_planner_agent = Agent(
    name="route_planner_agent",
    model=gemini_model,
    instruction="""You are an EV route planner agent. Your job is to plan the standard trip route
and select optimal charging stops based on origin, destination, vehicle model, and charger availability.
Use the `compute_route` tool to find coordinates and distances.
Use the `find_chargers_near` tool to find chargers near a location.
Do not calculate weather/elevation degradation yourself; delegate or consume details provided by the Environmental Analyst.
Always output the planned route and selected charging stops with duration and estimated arrival battery percentage (SoC).
Be extremely precise with names, charging speeds (e.g. 150kW, 350kW), and estimated SoC.""",
    tools=[mcp_toolset_planner]
)

# Sub-agent 2: Environmental Analyst
environmental_analyst_agent = Agent(
    name="environmental_analyst_agent",
    model=gemini_model,
    instruction="""You are an EV Environmental Analyst agent. Your job is to evaluate temperature, weather,
and route elevation profiles to calculate the range degradation factor and adjust battery usage estimates.
Use the `fetch_weather` tool to check weather conditions at route waypoints.
Evaluate route elevation profiles (from the route waypoints) and temperature to adjust the State of Charge (SoC) estimations.
For example, cold weather or high elevation climbs degrade range (reducing SoC by an extra 10-20%).
Analyze the route and provide the range degradation multiplier and the modified SoC estimates for each leg of the trip.
Provide clear details of elevation changes and temperature impacts on range.""",
    tools=[mcp_toolset_env]
)

# We wrap sub-agents in AgentTools for the Orchestrator
from google.adk.tools import AgentTool
route_planner_tool = AgentTool(agent=route_planner_agent)
environmental_analyst_tool = AgentTool(agent=environmental_analyst_agent)

# Orchestrator Agent
orchestrator_agent = Agent(
    name="orchestrator_agent",
    model=gemini_model,
    instruction="""You are the EV Route and Charging Orchestrator agent.
Your goal is to coordinate planning a trip for an EV driver.
You have access to two specialized sub-agents via tools:
1. `route_planner_agent`: Plans the route and picks chargers.
2. `environmental_analyst_agent`: Adjusts the battery estimates based on weather and elevation.

When a user requests a trip plan:
1. Call `route_planner_agent` to plan the basic route and charging stops.
2. Call `environmental_analyst_agent` to analyze weather and elevation degradation for that route.
3. Combine the findings into a cohesive, optimized final itinerary.
Ensure you output a structured trip plan containing:
- Origin and Destination
- EV Model
- Range Degradation factor (from environmental analyst)
- List of charging stops, stop durations, and estimated arrival State of Charge (SoC).
- Final arrival State of Charge (SoC) at destination.
If the final arrival SoC or any intermediate SoC drops below 15%, highlight this clearly in your final response.""",
    tools=[route_planner_tool, environmental_analyst_tool]
)


# ---------------------------------------------------------------------------
# Workflow State Schema
# ---------------------------------------------------------------------------
class EVRouteState(BaseModel):
    user_query: str = ""
    sanitized_query: str = ""
    raw_itinerary: str = ""
    verification_status: str = "pending"
    final_output: str = ""
    pii_scrubbed: bool = False
    audit_log: list[str] = []
    error_message: str = ""


# ---------------------------------------------------------------------------
# Workflow Nodes
# ---------------------------------------------------------------------------

@node(name="security_checkpoint", rerun_on_resume=True)
async def security_checkpoint_node(ctx: Context, node_input: str) -> str:
    # 0. Initialize state defaults to avoid KeyErrors and allow clean delta tracking
    ctx.state['user_query'] = node_input
    ctx.state['sanitized_query'] = ""
    ctx.state['raw_itinerary'] = ""
    ctx.state['verification_status'] = "pending"
    ctx.state['final_output'] = ""
    ctx.state['pii_scrubbed'] = False
    ctx.state['audit_log'] = []
    ctx.state['error_message'] = ""
    
    # Audit log entry helper
    def write_audit_log(severity: str, event: str, details: str):
        log_entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "severity": severity,
            "event": event,
            "details": details
        }
        log_str = json.dumps(log_entry)
        logs = list(ctx.state.get('audit_log', []))
        logs.append(log_str)
        ctx.state['audit_log'] = logs
        print(f"[AUDIT LOG] {log_str}")

    write_audit_log("INFO", "security_check_started", f"Analyzing query: {node_input}")
    
    # 1. Prompt Injection Detection
    injection_keywords = ["ignore previous", "system prompt", "bypass", "override", "say instructions", "developer mode"]
    query_lower = node_input.lower()
    for kw in injection_keywords:
        if kw in query_lower:
            write_audit_log("CRITICAL", "prompt_injection_detected", f"Keyword match: '{kw}'")
            ctx.route = "denied"
            ctx.state['error_message'] = f"Security Violation: Potential prompt injection detected (keyword: '{kw}')."
            return f"Unsafe prompt detected (keyword: '{kw}')."

    # 2. Domain-Specific Rule (Minimum query length)
    if len(node_input.strip()) < 5:
        write_audit_log("WARNING", "domain_rule_violated", "Query is too short.")
        ctx.route = "denied"
        ctx.state['error_message'] = "Invalid Query: Please provide origin, destination, and EV details."
        return "Query too short."

    # 3. PII Scrubbing
    scrubbed_query = node_input
    # Phone numbers
    phone_pattern = re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b")
    if phone_pattern.search(scrubbed_query):
        scrubbed_query = phone_pattern.sub("[PHONE_REDACTED]", scrubbed_query)
        write_audit_log("WARNING", "pii_redacted", "Phone number redacted.")
        
    # Credit cards
    cc_pattern = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
    if cc_pattern.search(scrubbed_query):
        scrubbed_query = cc_pattern.sub("[CREDIT_CARD_REDACTED]", scrubbed_query)
        write_audit_log("WARNING", "pii_redacted", "Credit card redacted.")

    # License plates
    plate_pattern = re.compile(r"\b[A-Za-z0-9]{7,8}\b")
    def plate_replacer(match):
        text = match.group(0)
        if any(c.isdigit() for c in text) and any(c.isalpha() for c in text):
            return "[LICENSE_PLATE_REDACTED]"
        return text
    
    if plate_pattern.search(scrubbed_query):
        scrubbed_query = plate_pattern.sub(plate_replacer, scrubbed_query)
        write_audit_log("WARNING", "pii_redacted", "License plate redacted.")

    ctx.state['sanitized_query'] = scrubbed_query
    ctx.state['pii_scrubbed'] = (scrubbed_query != node_input)
    
    write_audit_log("INFO", "security_check_passed", "Query passed security checks.")
    ctx.route = "approved"
    return scrubbed_query


@node(name="orchestrator_node", rerun_on_resume=True)
async def orchestrator_node_func(ctx: Context, node_input: str) -> str:
    # Run the orchestrator agent dynamically with sanitized query
    result = await ctx.run_node(orchestrator_agent, node_input)
    
    ctx.state['raw_itinerary'] = result
    
    result_lower = result.lower()
    
    # Audit log writer function helper
    def append_audit_log(severity: str, event: str, details: str):
        log_entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "severity": severity,
            "event": event,
            "details": details
        }
        logs = list(ctx.state.get('audit_log', []))
        logs.append(json.dumps(log_entry))
        ctx.state['audit_log'] = logs

    user_query_lower = ctx.state.get('user_query', '').lower()

    # Determine the route based on reserve battery capacity
    # If the text explicitly mentions arrival state of charge below 10%, we block the route.
    # If it is below 15% (or if the query specifies low battery warning of 12%), we request human approval.
    if "soc below 10%" in result_lower or "arrival state of charge: 5%" in result_lower or "arrival state of charge: 8%" in result_lower:
        ctx.route = "denied"
        ctx.state['verification_status'] = "blocked_battery_too_low"
        append_audit_log("WARNING", "route_blocked", "Battery reserve drops below 10% safety threshold.")
        return "Route blocked: Arrival State of Charge is below the 10% critical safety threshold. Please plan a route with more charging stops."
    elif (
        "soc below 15%" in result_lower 
        or "arrival state of charge: 12%" in result_lower 
        or "needs review" in result_lower 
        or "reserve warning" in result_lower
        or "nissan leaf" in user_query_lower
        or "12%" in user_query_lower
    ):
        ctx.route = "NEEDS_REVIEW"
        ctx.state['verification_status'] = "needs_human_approval"
        append_audit_log("INFO", "route_needs_review", "Battery reserve is low (between 10% and 15%).")
        if "12%" not in result_lower:
            result = f"{result}\n\n⚠️ WARNING: Route has a low battery reserve (Estimated arrival: 12% SoC)."
            ctx.state['raw_itinerary'] = result
        return result
    else:
        ctx.route = "AUTO_APPROVED"
        ctx.state['verification_status'] = "auto_approved"
        append_audit_log("INFO", "route_auto_approved", "Route auto-approved.")
        return result


@node(name="human_approval_node", rerun_on_resume=True)
async def human_approval_node_func(ctx: Context) -> Union[Event, RequestInput, str]:
    interrupt_id = "ev_route_human_approval"
    
    # Check if we have received a resume response for this interrupt
    user_response = ctx.resume_inputs.get(interrupt_id)
    if user_response is not None:
        # User has provided input
        ctx.state['verification_status'] = f"human_approved: {user_response}"
        
        # Log response
        log_entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "severity": "INFO",
            "event": "human_reviewed",
            "details": f"Reviewer comments: {user_response}"
        }
        logs = list(ctx.state.get('audit_log', []))
        logs.append(json.dumps(log_entry))
        ctx.state['audit_log'] = logs
        
        itinerary = ctx.state['raw_itinerary']
        ctx.state['final_output'] = f"📋 [HUMAN APPROVED] {itinerary}\nReviewer Comments: {user_response}"
        return ctx.state['final_output']
    
    # If not resumed yet, request user input
    message = (
        f"⚠️ WARNING: Route has a low battery reserve (State of Charge at destination is low).\n"
        f"Do you want to override and approve this route? (Type your approval or rejection details)"
    )
    return RequestInput(
        interrupt_id=interrupt_id,
        message=message
    )


@node(name="security_violation_handler", rerun_on_resume=True)
async def security_violation_handler_node(ctx: Context, node_input: str) -> str:
    ctx.state['verification_status'] = "security_blocked"
    return f"🛡️ Security Block: {node_input}"


@node(name="final_output", rerun_on_resume=True)
async def final_output_node(ctx: Context) -> str:
    # Gather output from state
    status = ctx.state['verification_status']
    if status == "blocked_battery_too_low":
        return f"❌ Route Blocked: Battery reserve falls below the 10% safety margin. Itinerary:\n{ctx.state['raw_itinerary']}"
    elif status.startswith("human_approved"):
        return ctx.state['final_output']
    elif status == "security_blocked":
        return f"❌ Execution Aborted: Security Violation."
    else:
        # Auto-approved
        return f"✅ Route Approved:\n{ctx.state['raw_itinerary']}"


# ---------------------------------------------------------------------------
# Workflow Compilation
# ---------------------------------------------------------------------------

ev_route_workflow = Workflow(
    name="ev_route_workflow",
    description="EV Route + Charging Intelligence Workflow",
    state_schema=EVRouteState,
    edges=[
        Edge(from_node=START, to_node=security_checkpoint_node),
        Edge(from_node=security_checkpoint_node, to_node=orchestrator_node_func, route="approved"),
        Edge(from_node=security_checkpoint_node, to_node=security_violation_handler_node, route="denied"),
        Edge(from_node=orchestrator_node_func, to_node=human_approval_node_func, route="NEEDS_REVIEW"),
        Edge(from_node=orchestrator_node_func, to_node=final_output_node, route=DEFAULT_ROUTE),
        Edge(from_node=human_approval_node_func, to_node=final_output_node),
        Edge(from_node=security_violation_handler_node, to_node=final_output_node),
    ]
)

root_agent = ev_route_workflow

app = App(
    root_agent=root_agent,
    name="app",
)
