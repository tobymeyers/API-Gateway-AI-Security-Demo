#!/usr/bin/env python3
"""Event-day: replay the fixed attack script identically against one table's round.

Run once per table, in parallel with the Good Load Tester, while that
table's blue agent is live. See generate_attack_script.py for how the
script itself was produced (once, offline, before the event).
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone

import requests


def fetch_token(token_url, client_id, client_secret, scope):
    resp = requests.post(
        token_url,
        data={"grant_type": "client_credentials", "client_id": client_id, "scope": scope},
        auth=(client_id, client_secret),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def duration_exceeded(elapsed_seconds, max_duration_seconds):
    """True if the replay should stop sending further requests. max_duration_seconds
    of None means "no cap" (the old, still-default behavior for standalone/debug use) -
    see --max-duration-seconds' help text for why a caller passes one."""
    return max_duration_seconds is not None and elapsed_seconds >= max_duration_seconds


def load_requests(data):
    """data is the parsed contents of an attack-plan file. Current format
    is {"metadata": {...}, "requests": [...]} (see generate_attack_script.py's
    build_output()); older files (generated before the metadata section
    existed) are just a bare list of entries - accept both, always
    returning just the list of entries. Pure function - see
    tests/test_replay_attack.py."""
    if isinstance(data, dict):
        return data.get("requests", [])
    return data


def is_lambda_deny_response(response_snippet):
    """True if response_snippet is (the start of) our own deny() helper's
    JSON body - {"approved": "false", "reason": "..."} - see
    infra/template.yaml. Used to distinguish "our Lambda denied this" from
    "a 403 that never reached our Lambda at all" (see attack_successful).
    Tolerates a truncated/partial body (response_snippet is only the
    first 300 chars) since "approved" always appears first in that dict's
    key order."""
    if not response_snippet:
        return False
    try:
        return "approved" in json.loads(response_snippet)
    except (json.JSONDecodeError, TypeError):
        return False


def attack_successful(status_code, error, response_snippet=None, method=None):
    """Was this specific adversarial request actually blocked by the
    gateway's own defense logic, or did it get through? True = got
    through (a "successful" attack from the adversary's point of view).
    False = genuinely blocked by our own deny() helper. None = can't
    say either way - either a network-level error (the replay client
    never got a response at all) or a 403 that never reached our Lambda
    in the first place.

    This is NOT the same question as "was the HTTP status code 200",
    and not even the same question as "was the status code 403" - two
    separate things return exactly 403:
      1. Every service Lambda's own deny() helper (reason e.g.
         "stale_token", "rate_limited", "missing_scope",
         "blocked_client", "required_claim_failed" - see
         infra/template.yaml), which ALSO emits the CloudWatch "Denied"
         metric this whole scoring system is built on - a real block.
      2. API Gateway's own built-in routing rejection (its default
         "Missing Authentication Token" response) for a request that
         never matched any resource at all - e.g. a bare "/service1/"
         with nothing after it, which doesn't satisfy the API's
         "{proxy+}" greedy path variable. This never reaches our
         Lambda, never emits any CloudWatch metric, and isn't a defense
         action at all - confirmed live via a direct CloudWatch query
         showing zero "Denied" datapoints for a round whose adversarial
         report still showed two "403"s.
    is_lambda_deny_response() tells these apart by response body shape.
    And separately: any OTHER status code, including a 500, means the
    request was never denied by deny() either - it just failed for some
    unrelated reason downstream of the approval decision (confirmed
    live: a round scored +0 while its log still showed one "-> 500" -
    that was a real successful attack, not a block).

    method == "HEAD" is a separate special case: HTTP HEAD responses
    never carry a body on the wire (RFC 7231 - Content-Length mirrors
    what GET would have returned, but zero actual body bytes are ever
    sent), so is_lambda_deny_response() can never confirm anything for
    it - confirmed live: a real HEAD request that reached our Lambda and
    got a fully correct, independently-verified answer (its
    Content-Length matched byte-for-byte what our deny()/success JSON
    would produce) still arrived with an empty response_snippet, and
    CloudWatch's own API Gateway execution logs don't trace HEAD
    requests at all either, so there's no second way to confirm it
    server-side. Trusting status_code alone for HEAD specifically is
    safe because the other known false-403 sources (a bare path that
    never reaches {proxy+}, or the SigV4-parsing edge case) only occur
    on paths that don't match {proxy+} at all - a HEAD request to a real
    sub-path, the only kind this script ever sends, does."""
    if error is not None:
        return None
    if method == "HEAD":
        return False if status_code == 403 else True
    if status_code == 403:
        return False if is_lambda_deny_response(response_snippet) else None
    return True


def summarize_results(results):
    """results is the list this script builds, one dict per request sent.
    Returns {"total", "status_counts" (str(code) -> count), "errors",
    "allowed", "denied", "errored"} - pure, unit-tested in
    tests/test_replay_attack.py, and what gets printed as the
    ADVERSARIAL_SUMMARY line plus results.json's "summary" key.

    "allowed"/"denied"/"errored" deliberately match
    scripts/lib/round_report.py's parse_good_traffic_results shape (the
    same vocabulary CloudWatch's own Outcome dimension already uses) so
    good_traffic and adversarial_traffic in run_summary.json share one
    schema instead of two unrelated ad-hoc ones. "errored" is the
    ambiguous attack_successful()-is-None bucket (a network error, or a
    403 that isn't our own deny() response) - a strict superset of
    "errors" (network failures only, kept separately since it's also
    independently useful: distinguishing a flaky connection from an
    ambiguous-but-completed request)."""
    status_counts = {}
    errors = 0
    allowed = 0
    denied = 0
    errored = 0
    for r in results:
        if r.get("error"):
            errors += 1
        else:
            key = str(r["status_code"])
            status_counts[key] = status_counts.get(key, 0) + 1
        outcome = r.get("attack_successful")
        if outcome is True:
            allowed += 1
        elif outcome is False:
            denied += 1
        else:
            errored += 1
    return {
        "total": len(results), "status_counts": status_counts, "errors": errors,
        "allowed": allowed, "denied": denied, "errored": errored,
    }


def render_html(table_id, prompt, generated_at, results):
    """Pure function: table_id/prompt/generated_at/results in, a
    self-contained HTML string out - no network, no filesystem. See
    tests/test_replay_attack.py."""
    summary = summarize_results(results)
    status_badges = "".join(
        f'<span class="badge">{code}: {count}</span>' for code, count in sorted(summary["status_counts"].items())
    )

    def esc(s):
        return (
            str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        )

    rows = []
    for r in results:
        if r.get("error"):
            status_html = '<span class="status status-error">ERROR</span>'
            detail = esc(r["error"])
        else:
            code = r["status_code"]
            css = "status-2xx" if 200 <= code < 300 else "status-4xx" if 400 <= code < 500 else "status-5xx"
            status_html = f'<span class="status {css}">{code}</span>'
            detail = ""
        successful = r.get("attack_successful")
        if successful is True:
            success_html = '<span class="success success-true">TRUE</span>'
        elif successful is False:
            success_html = '<span class="success success-false" title="blocked by our own deny() logic">FALSE</span>'
        elif r.get("error"):
            success_html = '<span class="success success-na" title="network-level error, no response at all">N/A</span>'
        elif r.get("status_code") == 403:
            success_html = (
                '<span class="success success-na" '
                'title="403 never reached our Lambda - API Gateway\'s own routing rejected it '
                '(e.g. a bare path that doesn\'t match {proxy+})">N/A</span>'
            )
        else:
            success_html = '<span class="success success-na">N/A</span>'
        request_id = r.get("request_id")
        request_id_html = f'<br><code class="request-id">req: {esc(request_id)}</code>' if request_id else ""
        rows.append(
            "<tr>"
            f"<td>{r['index']}</td>"
            f"<td>t+{r['t_offset_seconds']:.1f}s</td>"
            f"<td>{esc(r['method'])}</td>"
            f"<td class=\"path\">{esc(r['path'])}</td>"
            f"<td>{status_html}</td>"
            f"<td>{success_html}</td>"
            f"<td>{r['latency_ms']:.0f}ms</td>"
            f"<td class=\"rationale\">{esc(r.get('rationale') or '(no rationale recorded)')}{esc(detail)}{request_id_html}</td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Adversarial replay - table {esc(table_id)}</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 2rem; background: #fafafa; color: #1a1a1a; }}
  h1 {{ font-size: 1.3rem; margin-bottom: 0.2rem; }}
  .meta {{ color: #555; margin-bottom: 1rem; }}
  .meta div {{ margin: 0.15rem 0; }}
  .badge {{ display: inline-block; background: #eee; border-radius: 4px; padding: 0.15rem 0.5rem; margin-right: 0.4rem; font-size: 0.85rem; }}
  table {{ border-collapse: collapse; width: 100%; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #eee; font-size: 0.9rem; }}
  th {{ background: #f0f0f0; position: sticky; top: 0; }}
  td.path {{ font-family: monospace; }}
  td.rationale {{ color: #333; }}
  .request-id {{ color: #888; font-size: 0.8rem; }}
  .status {{ padding: 0.1rem 0.4rem; border-radius: 3px; font-weight: 600; font-size: 0.85rem; }}
  .status-2xx {{ background: #e3f6e3; color: #1a7a1a; }}
  .status-4xx {{ background: #fdeee0; color: #a85b00; }}
  .status-5xx {{ background: #fde3e3; color: #a31515; }}
  .status-error {{ background: #eee; color: #555; }}
  .success {{ padding: 0.1rem 0.4rem; border-radius: 3px; font-weight: 600; font-size: 0.8rem; }}
  .success-true {{ background: #fde3e3; color: #a31515; }}
  .success-false {{ background: #e3f6e3; color: #1a7a1a; }}
  .success-na {{ background: #eee; color: #777; }}
  .legend {{ font-size: 0.8rem; color: #777; margin-bottom: 0.75rem; }}
</style>
</head>
<body>
<h1>Adversarial replay — table {esc(table_id)}</h1>
<div class="meta">
  <div><strong>Prompt:</strong> {esc(prompt) if prompt else "(not provided)"}</div>
  <div><strong>Generated:</strong> {esc(generated_at)}</div>
  <div><strong>Requests sent:</strong> {summary["total"]} &nbsp; {status_badges}
    {f'<span class="badge">errors: {summary["errors"]}</span>' if summary["errors"] else ""}
  </div>
  <div><strong>Attacks successful (allowed):</strong> {summary["allowed"]} &nbsp;
    <strong>blocked (denied):</strong> {summary["denied"]} &nbsp;
    <strong>ambiguous (errored):</strong> {summary["errored"]}</div>
</div>
<div class="legend">"Attack successful" = TRUE means the request got through our defense logic -
  whatever status code came back afterward. A 500 is NOT a block: it means the request passed
  every defense check and then hit an unrelated server error - still a successful attack.
  FALSE means our own deny() logic returned 403 (hover its badge for details). N/A covers two
  different "can't tell" cases: a network-level error with no response at all, or a 403 that
  never reached our Lambda at all - API Gateway's own routing rejected it before our code ever
  ran (e.g. a bare path that doesn't satisfy a "{{proxy+}}" resource) - confirmed, this is not a
  defense action and does not affect scoring either way. Hover a FALSE/N/A badge for which case
  applies.</div>
<table>
<thead><tr><th>#</th><th>offset</th><th>method</th><th>path</th><th>status</th><th>attack successful</th><th>latency</th><th>rationale</th></tr></thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--script", required=True,
        help="Path to the generated attack script to replay - e.g. "
             "attack-plans/attack-plan-<run_id>.json (no default: callers resolve which "
             "one explicitly, see scripts/lib/attack_plans.py).",
    )
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--table-id", required=True)
    parser.add_argument("--token-url", required=True, help="Cognito oauth2/token endpoint.")
    parser.add_argument("--client-id", required=True, help="Adversarial app client id.")
    parser.add_argument("--client-secret", required=True)
    parser.add_argument(
        "--scope",
        default="gateway/service1.access gateway/service2.access gateway/service3.access",
    )
    parser.add_argument(
        "--prompt", default=None,
        help="The facilitator's defense prompt for this round - display-only, recorded into "
             "the HTML report for context. Not used for anything else.",
    )
    parser.add_argument(
        "--report-dir", default=None,
        help="If given, write results.json and index.html (a Gatling-report-style page: "
             "status/latency/rationale per request) into this directory, and print an "
             "ADVERSARIAL_SUMMARY {...} line. Omit to just print per-request lines, as before.",
    )
    parser.add_argument(
        "--max-duration-seconds", type=float, default=None,
        help="Stop sending further requests once this many seconds have elapsed since this "
             "script started, even if script entries remain. Originally existed because the "
             "orchestrator used to reset every defense back to default the instant its own "
             "round_seconds elapsed, racing against this script's actual runtime - a request "
             "sent after that point was untested, not actually blocked or allowed by the "
             "agent's strategy, which silently inflated \"allowed\" in a real round (confirmed: "
             "the last 13 of 48 requests in reports/round-table10-20261025-035536 all landed "
             "after the agent's block_all had already been reset). That race is gone now (see "
             "scripts/end_round.sh - the orchestrator only resets on an explicit signal, not a "
             "timer), but this cap is still a reasonable sanity bound. Omit to run the whole "
             "script unmodified (old behavior, still the default for standalone/debug use).",
    )
    args = parser.parse_args()

    with open(args.script) as fh:
        script = load_requests(json.load(fh))

    results = []
    truncated = False
    script_start = time.monotonic()

    for entry in script:
        if duration_exceeded(time.monotonic() - script_start, args.max_duration_seconds):
            truncated = True
            break
        time.sleep(max(0.0, entry.get("delay_seconds", 0)))
        if entry.get("type") != "request":
            continue
        if duration_exceeded(time.monotonic() - script_start, args.max_duration_seconds):
            truncated = True
            break

        # Fetch a brand-new token for every request rather than reusing one
        # for the whole script: a service's own BASELINE_MAX_TOKEN_AGE
        # (Service 3's is 30s) is a floor the agent can't loosen, so a
        # token that's merely old enough gets denied by baseline alone,
        # with no agent action involved - this showed up live as a
        # "do-nothing" agent still scoring +1 because the attack script's
        # cumulative delay before its 3rd request (~28s) plus real network
        # latency pushed that one request's token past 30s old. Fetching
        # fresh here makes every request's token age ~0s, independent of
        # the script's pacing.
        token = fetch_token(args.token_url, args.client_id, args.client_secret, args.scope)

        headers = dict(entry.get("headers") or {})
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Adversarial"] = "true"
        headers["X-Table-Id"] = str(args.table_id)

        rationale = entry.get("rationale")
        tag = f" [{rationale}]" if rationale else ""
        t_offset = time.monotonic() - script_start

        t0 = time.monotonic()
        try:
            resp = requests.request(
                entry["method"],
                f"{args.gateway_url.rstrip('/')}{entry['path']}",
                headers=headers,
                json=entry.get("body"),
                timeout=10,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            response_snippet = resp.text[:300]
            # API Gateway attaches this to EVERY response, success or
            # failure, uniquely identifying the exact execution-log entry
            # for this one request - without it, correlating a specific
            # anomaly (e.g. the SigV4-parsing 403s seen live) to its real
            # cause means approximate timestamp-matching among a sea of
            # concurrent requests instead of an exact lookup. Case-
            # insensitive dict (requests library), so this works
            # regardless of how AWS capitalizes the header.
            request_id = resp.headers.get("x-amzn-RequestId")
            print(f"{entry['method']} {entry['path']} -> {resp.status_code}{tag}")
            results.append({
                "index": len(results) + 1, "t_offset_seconds": t_offset,
                "method": entry["method"], "path": entry["path"], "rationale": rationale,
                "status_code": resp.status_code, "error": None, "latency_ms": latency_ms,
                "response_snippet": response_snippet, "request_id": request_id,
                "attack_successful": attack_successful(resp.status_code, None, response_snippet, entry["method"]),
            })
        except requests.RequestException as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            print(f"{entry['method']} {entry['path']} -> error: {exc}{tag}")
            results.append({
                "index": len(results) + 1, "t_offset_seconds": t_offset,
                "method": entry["method"], "path": entry["path"], "rationale": rationale,
                "status_code": None, "error": str(exc), "latency_ms": latency_ms,
                "response_snippet": None, "request_id": None,
                "attack_successful": attack_successful(None, str(exc), method=entry["method"]),
            })

    if args.report_dir:
        os.makedirs(args.report_dir, exist_ok=True)
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = summarize_results(results)

        with open(os.path.join(args.report_dir, "results.json"), "w") as fh:
            json.dump(
                {"table_id": args.table_id, "prompt": args.prompt, "generated_at": generated_at,
                 "summary": summary, "truncated": truncated, "results": results},
                fh, indent=2,
            )
        with open(os.path.join(args.report_dir, "index.html"), "w") as fh:
            fh.write(render_html(args.table_id, args.prompt, generated_at, results))

        print(f"Adversarial report: {os.path.join(args.report_dir, 'index.html')}")
        print("ADVERSARIAL_SUMMARY " + json.dumps({"table_id": args.table_id, "truncated": truncated, **summary}))


if __name__ == "__main__":
    main()
