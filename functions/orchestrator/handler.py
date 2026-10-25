"""The blue agent: a Claude tool-use loop that runs for one table's round.

Manual loop (not the SDK's tool_runner) because a live-monitoring agent
needs periodic re-prompts with fresh traffic stats over ~4-5 minutes,
not a single until-done pass that stops the moment Claude has no more
tool calls to make.
"""
import json
import os
import time

import anthropic

import tools as agent_tools

client = anthropic.AnthropicBedrock()  # region/credentials come from the Lambda execution role
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
# Bedrock's acceptance of `thinking: {"type": "adaptive"}` for a given
# model/region has changed from under us before (confirmed live: it
# worked in earlier test runs, then started failing with "adaptive
# thinking is not supported on this model" on a later run with no code
# change in between) - not something this project controls. Tracked at
# module scope (persists across warm invocations in this container) so a
# single rejection disables it for the rest of this round and any
# subsequent warm invocation, instead of failing the same way on every
# single turn for the whole round.
_adaptive_thinking_supported = True
DEFAULT_ROUND_SECONDS = int(os.environ.get("ROUND_SECONDS", "60"))
CHECK_IN_SECONDS = 20
MAX_TOKENS = 4096  # tool-call-heavy turns, not long prose - kept below the
                    # generic 16000 default to hold per-check-in latency down
MAX_TOOL_ROUNDTRIPS_PER_TURN = 8  # bounds worst-case turn latency so a long
                                   # tool-calling chain can't blow the Lambda's
                                   # 300s timeout - a hard timeout kills the
                                   # process without running handler()'s
                                   # finally, which would skip reset_config

TOOLS = [
    {
        "name": "get_traffic_stats",
        "description": (
            "Per-client request activity for this table, cumulative since the round "
            "started (request count, average requests/second, seconds since first seen). "
            "Does NOT label which client is malicious - infer that from the pattern "
            "(rate, regularity, how one client compares to others)."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_openapi_spec",
        "description": "The OpenAPI spec for the three backend services, so you know what legitimate request shapes look like.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_current_config",
        "description": "The defense configuration currently in effect for this table.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_rate_limit",
        "description": (
            "Cap requests-per-second per client, table-wide across all three services. "
            "Can only tighten each service's own baseline ceiling, never loosen it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"requests_per_second": {"type": "integer", "minimum": 1}},
            "required": ["requests_per_second"],
        },
    },
    {
        "name": "block_client",
        "description": "Block a specific client_id from all three services immediately.",
        "input_schema": {
            "type": "object",
            "properties": {"client_id": {"type": "string"}},
            "required": ["client_id"],
        },
    },
    {
        "name": "unblock_client",
        "description": "Remove a client_id from the block list.",
        "input_schema": {
            "type": "object",
            "properties": {"client_id": {"type": "string"}},
            "required": ["client_id"],
        },
    },
    {
        "name": "block_all_traffic",
        "description": (
            "Unconditional kill switch: deny every request to all three services immediately, "
            "regardless of client_id or rate. Unlike set_rate_limit (a volume cap that can never "
            "catch a client whose own rate is already below the cap) or block_client (which "
            "requires naming a client_id you've already seen), this blocks everyone - including "
            "clients you haven't observed yet. Use when the operator instruction calls for "
            "blocking all traffic, not just the attacker."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "unblock_all_traffic",
        "description": (
            "Lift block_all_traffic. Other defenses you've set (rate limit, blocked clients, "
            "token TTL, required claim) are untouched and remain in effect."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_token_ttl_policy",
        "description": (
            "Require tokens to have been issued within the last N seconds "
            "(behavioral freshness check), table-wide. Can only tighten each "
            "service's own baseline, never loosen it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "integer", "minimum": 1}},
            "required": ["seconds"],
        },
    },
    {
        "name": "require_additional_claim",
        "description": (
            "Require a specific token claim to match a value (e.g. scope must "
            "contain a substring) before any service approves the request."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim_name": {"type": "string"},
                "claim_value": {"type": "string"},
                "match": {"type": "string", "enum": ["equals", "contains"], "default": "equals"},
            },
            "required": ["claim_name", "claim_value"],
        },
    },
]
# Cache breakpoint on the last tool def: caches system+tools together (see
# `system` in _run_turn). Below Anthropic's ~1024-token minimum this is a
# harmless no-op, not an error - no cost either way if it doesn't kick in.
TOOLS[-1] = {**TOOLS[-1], "cache_control": {"type": "ephemeral"}}

SYSTEM_PROMPT_TEMPLATE = """You are the live defensive agent for API Gateway table {table_id} in a security \
tabletop exercise. Two populations of traffic hit your gateway: legitimate load and an AI-crafted \
low-and-slow adversarial probe. Both use valid OAuth tokens, so you cannot rely on a label telling you \
which is which - you must infer it from behavior (request rate, regularity, how one client compares to \
others) using get_traffic_stats and get_openapi_spec.

Your defenses (set_rate_limit, block_client, set_token_ttl_policy, require_additional_claim, \
block_all_traffic) apply immediately and table-wide. block_all_traffic is an unconditional kill switch - \
it denies everyone, including clients you haven't seen yet - distinct from set_rate_limit, which can only \
ever catch a client whose own rate exceeds the cap. Being too aggressive costs points by blocking \
legitimate traffic; being too passive costs points by letting attacks through. You have about \
{round_seconds} seconds total for this \
round, and will be checked in on roughly every {check_in_seconds} seconds. The round's starting state \
(current config, traffic stats, and the OpenAPI spec) is given to you upfront in the first message below - \
no need to re-call get_current_config/get_traffic_stats/get_openapi_spec just to see it. Call them again \
any time you want fresher data, especially at a check-in.

The table's operator gave you this instruction for the round:
---
{operator_prompt}
---
"""


def _execute(table_id, name, tool_input):
    if name == "get_traffic_stats":
        return agent_tools.get_traffic_stats(table_id)
    if name == "get_openapi_spec":
        return agent_tools.get_openapi_spec()
    if name == "get_current_config":
        return agent_tools.get_current_config(table_id)
    if name == "set_rate_limit":
        return agent_tools.set_rate_limit(table_id, tool_input["requests_per_second"])
    if name == "block_client":
        return agent_tools.block_client(table_id, tool_input["client_id"])
    if name == "unblock_client":
        return agent_tools.unblock_client(table_id, tool_input["client_id"])
    if name == "block_all_traffic":
        return agent_tools.block_all_traffic(table_id)
    if name == "unblock_all_traffic":
        return agent_tools.unblock_all_traffic(table_id)
    if name == "set_token_ttl_policy":
        return agent_tools.set_token_ttl_policy(table_id, tool_input["seconds"])
    if name == "require_additional_claim":
        return agent_tools.require_additional_claim(
            table_id, tool_input["claim_name"], tool_input["claim_value"], tool_input.get("match", "equals")
        )
    return {"error": f"unknown tool {name}"}


def _to_plain_block(block):
    """Coerce an SDK content-block object (assistant turns) or a plain dict
    (tool_result/user turns) into a plain dict we can add cache_control to."""
    if isinstance(block, dict):
        return dict(block)
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return dict(block)


def _with_cache_breakpoint(messages):
    """Copy of `messages` with a cache_control breakpoint on the last content
    block of the last message. Without this, every check-in/tool-roundtrip
    resends the whole growing conversation at full input-token price - the
    dominant cost driver for a round (see DEPLOYMENT.md's cost estimate).
    With it, each call re-reads the previous call's cached prefix cheaply
    and only pays full price for the new tail. Never mutates the stored
    `messages` list in place - Anthropic allows at most 4 cache_control
    breakpoints per request, so a stale marker left on an old message would
    eventually push a long round over that limit and error out.
    """
    if not messages:
        return messages
    last = messages[-1]
    content = last["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    plain_blocks = [_to_plain_block(b) for b in blocks]
    plain_blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return messages[:-1] + [{"role": last["role"], "content": plain_blocks}]


def _create_message(system_prompt, messages):
    global _adaptive_thinking_supported
    kwargs = dict(
        model=MODEL_ID,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=TOOLS,
        messages=_with_cache_breakpoint(messages),
    )
    if _adaptive_thinking_supported:
        try:
            return client.messages.create(thinking={"type": "adaptive"}, **kwargs)
        except anthropic.BadRequestError as exc:
            if "thinking" not in str(exc).lower():
                raise
            print(f"Adaptive thinking rejected by Bedrock for {MODEL_ID}, falling back without it: {exc}")
            _adaptive_thinking_supported = False
    return client.messages.create(**kwargs)


def _run_turn(table_id, system_prompt, messages):
    for _ in range(MAX_TOOL_ROUNDTRIPS_PER_TURN):
        response = _create_message(system_prompt, messages)
        # Greppable from CloudWatch Logs for a real, immediate cost readout -
        # unlike Cost Explorer, token usage has no ~24h lag. See
        # scripts/comprehensive_test.sh, which sums these per test run.
        u = response.usage
        print(
            "USAGE_LOG "
            + json.dumps(
                {
                    "table_id": table_id,
                    "model": MODEL_ID,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                    "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                }
            )
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return messages

        results = []
        for block in tool_uses:
            # Greppable from CloudWatch Logs, same convention as USAGE_LOG -
            # the only record of what the agent actually decided mid-round.
            # Without this there's no way to reconstruct after the fact
            # which lever was pulled and with what value; confirmed live:
            # diagnosing a "block everything" round that only ever blocked
            # good traffic (never the low-volume adversarial client) required
            # reverse-engineering the agent's behavior entirely from
            # CloudWatch request-count metrics and request-timing math,
            # because nothing logged the actual set_rate_limit() calls.
            print("TOOL_CALL " + json.dumps({"table_id": table_id, "tool": block.name, "input": block.input}))
            try:
                result = _execute(table_id, block.name, block.input)
                print("TOOL_RESULT " + json.dumps({"table_id": table_id, "tool": block.name, "result": result}))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
            except Exception as exc:  # surface the failure to the model, don't crash the round
                print("TOOL_RESULT " + json.dumps({"table_id": table_id, "tool": block.name, "error": str(exc)}))
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": f"Error: {exc}", "is_error": True}
                )
        messages.append({"role": "user", "content": results})

    # Hit the per-turn cap with Claude still wanting to call more tools -
    # stop here rather than let one turn eat the rest of the round's budget.
    return messages


def build_initial_message(config, traffic_stats, openapi_spec_yaml):
    """The agent's first-ever message of the round. Embeds what
    get_current_config/get_traffic_stats/get_openapi_spec would have told it,
    pre-fetched directly in Python (sub-millisecond DynamoDB reads plus a
    static file read - no Bedrock round-trip) instead of making the agent
    spend its first 3 of 4 observed tool-calling round-trips just gathering
    this before it could act at all. Confirmed live: in a real round, that
    sequential read-then-act pattern (get_current_config -> get_traffic_stats
    -> get_openapi_spec -> only then block_all_traffic) took ~8s, during
    which 7/48 adversarial requests landed with zero defenses in place -
    this doesn't touch that latency's OTHER contributor (Lambda cold start/
    first uncached Bedrock call, handled separately by run_round.sh's fixed
    head start), only the 3 round-trips that were pure information-gathering
    with no dependency on the model at all.

    Pure (no I/O) - the fetching happens in run_agent_loop, this just
    formats already-fetched data. Unit-tested directly, unlike the rest of
    this AWS/Bedrock-facing module - see tests/test_orchestrator_handler.py.
    """
    return (
        "The round is starting. To save you the round-trip, here's what "
        "get_current_config, get_traffic_stats, and get_openapi_spec would "
        "have told you right now - no need to call them again just to see "
        "this; call them again any time you want fresher data.\n\n"
        f"Current config: {json.dumps(config)}\n\n"
        f"Traffic stats: {json.dumps(traffic_stats)}\n\n"
        f"OpenAPI spec:\n{openapi_spec_yaml}\n\n"
        "Decide now if any defenses are needed."
    )


def run_agent_loop(table_id, operator_prompt, round_seconds, check_in_seconds=CHECK_IN_SECONDS):
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        table_id=table_id,
        round_seconds=round_seconds,
        check_in_seconds=check_in_seconds,
        operator_prompt=operator_prompt,
    )
    initial_config = agent_tools.get_current_config(table_id)
    initial_stats = agent_tools.get_traffic_stats(table_id)
    initial_spec = agent_tools.get_openapi_spec()["openapi_spec_yaml"]
    messages = [
        {
            "role": "user",
            "content": build_initial_message(initial_config, initial_stats, initial_spec),
        }
    ]
    deadline = time.monotonic() + round_seconds
    while True:
        messages = _run_turn(table_id, system_prompt, messages)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(check_in_seconds, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Check-in: about {int(remaining)} seconds left in the round. "
                    "Pull fresh traffic stats if you want, then adjust defenses or hold steady."
                ),
            }
        )


def handler(event, context):
    # Explicit, synchronous end-of-round signal (see scripts/end_round.sh /
    # the /end-round API route) - the ONLY thing that resets defenses back
    # to baseline. Deliberately NOT done on a timer inside run_agent_loop:
    # the facilitator's traffic generators (Gatling, replay_attack) aren't
    # pinned to round_seconds either (real per-request latency on top of
    # their own planned pacing routinely runs them longer), so resetting
    # on this function's own clock was racing against traffic that was
    # still live - confirmed live: the last 13/48 adversarial requests in
    # a real round landed after a timer-based reset had already fired,
    # silently un-defended. Calling end_round only after run_round.sh has
    # actually waited on both traffic generators' exit codes removes the
    # guesswork entirely - whatever defenses the agent last set stay in
    # effect for exactly as long as real traffic exists, not an estimate.
    if event.get("action") == "end_round":
        table_id = str(event["table_id"])
        return agent_tools.reset_config(table_id)

    table_id = str(event["table_id"])
    prompt = event["prompt"]
    round_seconds = int(event.get("round_seconds") or DEFAULT_ROUND_SECONDS)

    agent_tools.reset_config(table_id)
    run_agent_loop(table_id, prompt, round_seconds)
    return {"status": "round_complete", "table_id": table_id}
