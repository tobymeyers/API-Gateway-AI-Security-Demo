#!/usr/bin/env python3
"""Offline, run once ahead of the event.

An AI agent reads the OpenAPI spec and probes a staging copy of the
gateway, crafting an adaptive campaign. Defaults to a 100-150 request
volume (see MIN_REQUESTS/MAX_REQUESTS below and --min-requests/
--max-requests) rather than pure low-and-slow: an unconstrained model
asked only to "stay stealthy" converged on 3 requests in practice, which
is too thin a sample to score meaningfully against ~1000 good requests
per round. Pass --min-requests 0 to go back to unconstrained low-and-
slow. Every run prints its resolved config up front, before spending
any Bedrock tokens, so the defaults are never a surprise. The harness
(not the model) timestamps every action,
so the recorded script captures the real pacing the agent chose, and
every request carries the model's own one-line rationale for sending
it. That script is then replayed byte-for-byte at every table (see
replay_attack.py) so N_mal_baseline is identical across all 12 tables -
"genuinely AI-driven" without breaking the scoring formula's fairness
assumption. See infra/template.yaml and the project README for the rest
of the architecture.

The written file is {"metadata": {...}, "requests": [...]} - metadata
records the minimum inputs that shaped this generation (model,
min_requests, max_requests, max_total_seconds), so reviewing the file
later never requires digging up the original command. See build_output().
"""
import argparse
import json
import os
import sys
import time

import requests
import anthropic
from anthropic import beta_tool

STAGING_URL = None
TOKEN_URL = None
CLIENT_ID = None
CLIENT_SECRET = None
SCOPE = "gateway/service1.access gateway/service2.access gateway/service3.access"
# Defaults sized for a ~1000-good-request round (see run_round.sh's default
# --target-rps 20 x 60s round): 100 malicious requests gives a theoretical
# score range of +100 (perfect defense) to -1000 (blocks everything), a
# meaningful ~10% ratio - not an arbitrary round number. MAX_REQUESTS gives
# some headroom above the floor so the model isn't fenced in exactly at it.
# Both are overridable with --min-requests/--max-requests at any time.
MIN_REQUESTS = 100
MAX_REQUESTS = 150
# Hard budget on total recorded delay_seconds, independent of MAX_REQUESTS -
# a handful of requests can still blow the round length if the agent paces
# them far enough apart (wait() alone allows up to 30s/call with no count
# limit). Kept below the 60s round length (see attack_script_check.py's
# preflight, which hard-fails the whole run if this is ever violated
# anyway) so the generated script is guaranteed to fit without a second
# regeneration pass.
MAX_TOTAL_SECONDS = 45

recorded_script = []
_last_ts = [None]
_script_start = [None]
_token_cache = {"value": None, "fetched_at": 0.0}


def build_output(model, min_requests, max_requests, max_total_seconds, requests_list):
    """Assembles the full attack-plan file contents: a "metadata" section
    recording the minimum inputs that shaped this generation (so anyone
    reviewing the file later knows what produced it, without having to
    find the original command that generated it), plus the actual
    "requests" list. Pure function - see tests/test_generate_attack_script.py."""
    return {
        "metadata": {
            "model": model,
            "min_requests": min_requests,
            "max_requests": max_requests,
            "max_total_seconds": max_total_seconds,
        },
        "requests": requests_list,
    }


def resolve_min_requests(min_requests, max_requests):
    """A floor above the ceiling is a contradictory prompt (e.g. a
    disposable 5-request test run that didn't override --min-requests off
    its own default) - clamp rather than let the model receive "send at
    least 100 (budget is 5 max)". Returns (effective_min_requests, note),
    note is None if nothing was clamped. Pure function - see
    tests/test_generate_attack_script.py."""
    if min_requests > max_requests:
        return max_requests, (
            f"NOTE: --min-requests {min_requests} > --max-requests {max_requests} - "
            f"clamping the floor down to {max_requests}."
        )
    return min_requests, None


def _elapsed():
    if _script_start[0] is None:
        _script_start[0] = time.monotonic()
        return 0.0
    return time.monotonic() - _script_start[0]


def _current_token():
    # This run can take well over the Cognito token's 5-minute TTL (up to
    # MAX_REQUESTS round-trips, each with model latency, plus uncapped
    # wait() calls) - a single pre-fetched token would start 401ing
    # partway through and silently corrupt the back half of the script.
    if time.monotonic() - _token_cache["fetched_at"] > 90:
        resp = requests.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials", "client_id": CLIENT_ID, "scope": SCOPE},
            auth=(CLIENT_ID, CLIENT_SECRET),
            timeout=10,
        )
        resp.raise_for_status()
        _token_cache["value"] = resp.json()["access_token"]
        _token_cache["fetched_at"] = time.monotonic()
    return _token_cache["value"]


def _record(entry):
    now = time.monotonic()
    entry["delay_seconds"] = round(now - _last_ts[0], 3) if _last_ts[0] is not None else 0.0
    _last_ts[0] = now
    recorded_script.append(entry)


def openapi_spec_path():
    """Resolves to <repo-root>/openapi/gateway-openapi.yaml regardless of
    where this file itself lives - extracted from fetch_openapi_spec()
    below specifically so this path arithmetic is unit-tested. Without
    that, a wrong ".." count here is invisible to every cheap/local
    check (bash -n, py_compile, the test suite, --help) because
    fetch_openapi_spec is a @beta_tool only ever invoked by the live
    Bedrock tool-use loop - confirmed live: this exact count was wrong
    after the load-test/ -> load-testers/ move added a directory level,
    and only surfaced via FileNotFoundError mid-generation, after
    real Bedrock tokens had already been spent."""
    return os.path.join(os.path.dirname(__file__), "..", "..", "openapi", "gateway-openapi.yaml")


@beta_tool
def fetch_openapi_spec() -> str:
    """Return the OpenAPI spec text for the target gateway."""
    with open(openapi_spec_path()) as fh:
        return fh.read()


@beta_tool
def send_request(
    method: str, path: str, rationale: str, headers_json: str = "{}", body_json: str = "null"
) -> str:
    """Send one HTTP request against the staging gateway and record it (with the real
    elapsed time since the previous recorded action) into the fixed attack script.

    Args:
        method: HTTP method, e.g. GET or POST.
        path: Path on the gateway, e.g. /service2/lookup.
        rationale: One short sentence: what tactic this request represents and why you're
            sending it now (e.g. "probing for an unauthenticated variant of the lookup
            endpoint" or "burst of 5 to test service3's rate limit"). Recorded verbatim into
            the script so a human reviewing the generated file later can see, request by
            request, what the campaign was doing and why - not just the raw HTTP calls.
        headers_json: Extra headers as a JSON object string (Authorization is added automatically).
        body_json: JSON body for POST/PUT requests, or the string "null" for none.
    """
    if len(recorded_script) >= MAX_REQUESTS:
        return "Error: request budget exhausted for this script generation run."
    if _elapsed() >= MAX_TOTAL_SECONDS:
        return (
            f"Error: pacing budget exhausted ({MAX_TOTAL_SECONDS}s of recorded delay) - "
            "stop waiting and send any remaining requests back-to-back, or finish now."
        )
    try:
        headers = json.loads(headers_json) if headers_json else {}
        body = None if body_json in (None, "null") else json.loads(body_json)
    except json.JSONDecodeError as exc:
        return f"Error: invalid JSON - {exc}"

    request_headers = dict(headers)
    request_headers["Authorization"] = f"Bearer {_current_token()}"

    try:
        resp = requests.request(
            method.upper(), f"{STAGING_URL}{path}", headers=request_headers, json=body, timeout=10
        )
        status, snippet = resp.status_code, resp.text[:300]
    except requests.RequestException as exc:
        status, snippet = None, f"request failed: {exc}"

    # Authorization is intentionally NOT stored - replay re-adds a fresh
    # token for whichever table is running, using its own short-lived TTL.
    _record(
        {
            "type": "request",
            "method": method.upper(),
            "path": path,
            "headers": headers,
            "body": body,
            "rationale": rationale,
        }
    )
    remaining = MAX_REQUESTS - len(recorded_script)
    floor_note = (
        f" {MIN_REQUESTS - len(recorded_script)} more needed to reach the {MIN_REQUESTS}-request floor."
        if MIN_REQUESTS and len(recorded_script) < MIN_REQUESTS
        else ""
    )
    return json.dumps(
        {"status_code": status, "response_snippet": snippet, "requests_remaining_budget": remaining}
    ) + floor_note


@beta_tool
def wait(seconds: float) -> str:
    """Deliberately pause before the next request, on top of normal latency, to control
    low-and-slow pacing precisely.

    Args:
        seconds: Additional seconds to sleep (capped at 30 to keep generation bounded,
            and further capped by whatever's left of the script's total pacing budget).
    """
    remaining_budget = MAX_TOTAL_SECONDS - _elapsed()
    if remaining_budget <= 0:
        return (
            f"Error: pacing budget exhausted ({MAX_TOTAL_SECONDS}s of recorded delay) - "
            "send any remaining requests back-to-back now, no more waiting."
        )
    capped = max(0.0, min(seconds, 30.0, remaining_budget))
    time.sleep(capped)
    return f"Paused {capped}s (requested {seconds}s, capped by the script's pacing budget)."


def main():
    global STAGING_URL, TOKEN_URL, CLIENT_ID, CLIENT_SECRET, SCOPE, MAX_REQUESTS, MIN_REQUESTS, MAX_TOTAL_SECONDS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-url", required=True, help="Base URL of the staging gateway.")
    parser.add_argument("--token-url", required=True, help="Cognito oauth2/token endpoint.")
    parser.add_argument("--client-id", required=True, help="Adversarial app client id.")
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--scope", default=SCOPE)
    parser.add_argument(
        "--output", required=True,
        help="Path to write the generated script to - e.g. "
             "attack-plans/attack-plan-<run_id>.json. No default: "
             "scripts/generate_attack_script.sh is the one that picks this path "
             "(the versioned-folder convention), so there's no implicit "
             "current-directory assumption here.",
    )
    parser.add_argument(
        "--max-requests", type=int, default=MAX_REQUESTS,
        help=f"Hard ceiling on requests the model can send (default: {MAX_REQUESTS}).",
    )
    parser.add_argument(
        "--min-requests", type=int, default=MIN_REQUESTS,
        help=f"Tell the model to send at least this many requests before finishing "
             f"(default: {MIN_REQUESTS} - an unconstrained 'stay stealthy' prompt converged on "
             "3 requests in practice, too thin to score meaningfully against ~1000 good "
             "requests/round). Pass 0 for unconstrained low-and-slow, no floor at all. This is a "
             "strong instruction in the prompt, not a hard guarantee - the model can still stop "
             "short; attack_script_check.py's --min-requests flag catches that after the fact so "
             "you know to regenerate instead of silently replaying a thin script. Clamped down to "
             "--max-requests if it's ever passed larger than that.",
    )
    parser.add_argument(
        "--max-total-seconds", type=int, default=MAX_TOTAL_SECONDS,
        help="Hard cap on total recorded delay_seconds, independent of --max-requests "
             "(default: 45, kept under the 60s round length - see attack_script_check.py).",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        help="Bedrock model ID / inference profile ID - keep in sync with infra/template.yaml's ModelId default.",
    )
    args = parser.parse_args()

    # No exists-check here anymore: whether to reuse an existing script or
    # generate a new one is scripts/generate_attack_script.sh's policy
    # (default: reuse the latest in attack-plans/, --generate-new to
    # force a fresh one) - this module just generates whatever --output
    # says, unconditionally. Generation is real money with no prompt
    # caching regardless (confirmed: a 3-request run cost $3.26 across
    # 121 uncached calls; cost scales with both request count and the
    # growing uncached conversation, so the 100-request default here
    # costs meaningfully more) - that's the wrapper's job to guard
    # against by not calling this at all unless actually needed.

    STAGING_URL = args.staging_url.rstrip("/")
    TOKEN_URL = args.token_url
    CLIENT_ID = args.client_id
    CLIENT_SECRET = args.client_secret
    SCOPE = args.scope
    MAX_REQUESTS = args.max_requests
    MIN_REQUESTS = args.min_requests
    MAX_TOTAL_SECONDS = args.max_total_seconds

    MIN_REQUESTS, clamp_note = resolve_min_requests(MIN_REQUESTS, MAX_REQUESTS)
    if clamp_note:
        print(clamp_note, file=sys.stderr)

    # Print every variable that shapes this run before spending a single
    # Bedrock token, so the defaults (or an override) are never a silent
    # surprise - see MIN_REQUESTS/MAX_REQUESTS above for why the defaults
    # are what they are.
    print("Generation config:")
    print(f"  staging_url:       {STAGING_URL}")
    print(f"  token_url:         {TOKEN_URL}")
    print(f"  client_id:         {CLIENT_ID}")
    print(f"  scope:             {SCOPE}")
    print(f"  min_requests:      {MIN_REQUESTS}{' (unconstrained low-and-slow)' if not MIN_REQUESTS else ''}")
    print(f"  max_requests:      {MAX_REQUESTS}")
    print(f"  max_total_seconds: {MAX_TOTAL_SECONDS}")
    print(f"  model:             {args.model}")
    print(f"  output:            {args.output}")
    print()

    _current_token()  # fail fast on bad Cognito credentials before spending any Bedrock tokens

    # Invoked via Amazon Bedrock rather than the direct Anthropic API -
    # picks up credentials/region from the local AWS CLI config (the same
    # `aws configure` already required for deploy.sh), no separate API key.
    if MIN_REQUESTS:
        volume_instruction = (
            f"This campaign needs real volume, not stealth: send AT LEAST {MIN_REQUESTS} requests "
            f"(budget is {MAX_REQUESTS} max), mixing tactics across all three services - e.g. rapid "
            "bursts to probe rate limits, repeated token-stale/missing-scope/wrong-claim attempts, "
            "and varied paths/methods to probe for gaps - so the round has enough adversarial volume "
            "for the scoring range to mean something. Use wait() sparingly, if at all - most requests "
            f"should be close together so {MIN_REQUESTS}+ requests fit inside the pacing budget. Do "
            f"not stop before sending at least {MIN_REQUESTS} requests unless you hit an error you "
            "can't work around."
        )
    else:
        volume_instruction = (
            "Stay well under obvious rate thresholds and adjust your approach based on each "
            f"response. Budget yourself to at most {MAX_REQUESTS} requests total."
        )

    client = anthropic.AnthropicBedrock()
    runner = client.beta.messages.tool_runner(
        model=args.model,
        max_tokens=16000,
        tools=[fetch_openapi_spec, send_request, wait],
        messages=[
            {
                "role": "user",
                "content": (
                    "You are red-teaming an API gateway ahead of a security tabletop exercise. "
                    "Read the OpenAPI spec, then craft an adaptive reconnaissance and precision-"
                    f"probing campaign against the staging gateway: explore all three services. "
                    f"{volume_instruction} Every send_request call must include a short rationale "
                    "explaining the tactic behind that specific request."
                ),
            }
        ],
    )
    # Greppable from this script's own stdout for a real, immediate cost
    # readout - no Cost Explorer lag, since this runs locally, not in Lambda.
    for message in runner:
        u = message.usage
        print(
            "USAGE_LOG "
            + json.dumps(
                {
                    "model": args.model,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                    "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                }
            )
        )

    output_data = build_output(args.model, MIN_REQUESTS, MAX_REQUESTS, MAX_TOTAL_SECONDS, recorded_script)
    with open(args.output, "w") as fh:
        json.dump(output_data, fh, indent=2)
    print(f"Wrote {len(recorded_script)} recorded actions to {args.output}")
    if MIN_REQUESTS and len(recorded_script) < MIN_REQUESTS:
        print(
            f"WARNING: only {len(recorded_script)} requests, below the requested "
            f"--min-requests {MIN_REQUESTS} floor - the model stopped early. Review the script "
            "and consider regenerating (--generate-new, via scripts/generate_attack_script.sh) "
            "with a more insistent prompt or a larger --max-total-seconds budget.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
