"""Preflight check on a generated attack script (one of the files under
attack-plans/, see scripts/lib/attack_plans.py) before spending a
real deploy/round on it: how many requests does it have (so the stack's
ADVERSARIAL_BASELINE_COUNT can be kept in sync automatically), does its
total pacing (delay_seconds) fit inside the chosen round length, did it
reach any --min-requests floor, and (defensively) would any single
request's cumulative delay have been close enough to the target
service's own BASELINE_MAX_TOKEN_AGE to risk a baseline-only denial.

That last check (--token-age-margin) is OFF by default (0 = disabled) -
it matters only if something ever regresses replay_attack.py back to
reusing one token for the whole script. As of the fix recorded in this
repo's history, replay_attack.py fetches a fresh token immediately
before every request, which makes token age ~0s regardless of script
pacing - so a real, high-volume script's cumulative delays routinely
exceed a service's baseline_max_token_age by design (that's the whole
point of more volume) without that meaning anything is actually at
risk. Pass --token-age-margin explicitly (e.g. to audit after touching
replay_attack.py) if you want this check to run.

`summarize` is pure (data in, data out) and unit-tested directly - see
tests/test_attack_script_check.py.
"""
import argparse
import json
import sys

# Must match infra/template.yaml's BASELINE_MAX_TOKEN_AGE per service -
# None means no baseline freshness floor at all (Service 1).
SERVICE_BASELINE_MAX_TOKEN_AGE = {
    "service1": None,
    "service2": 60,
    "service3": 30,
}


def load_requests(data):
    """data is an attack-plan file's parsed contents. Current format is
    {"metadata": {...}, "requests": [...]} (see generate_attack_script.py's
    build_output()); older files (generated before the metadata section
    existed) are just a bare list - accept both, always returning just
    the list of entries."""
    if isinstance(data, dict):
        return data.get("requests", [])
    return data


def summarize(script):
    """script is the parsed attack script's list. Returns
    (request_count: int, total_delay_seconds: float)."""
    requests = [e for e in script if isinstance(e, dict) and e.get("type") == "request"]
    total_delay = sum(e.get("delay_seconds", 0) for e in script if isinstance(e, dict))
    return len(requests), total_delay


def _service_from_path(path):
    # Paths look like "/service3/probe" - first non-empty segment.
    parts = [p for p in (path or "").split("/") if p]
    return parts[0] if parts else None


def find_stale_token_risks(script, margin=5.0):
    """Walk the script accumulating delay_seconds (same clock a single
    reused token would age against), and flag any request whose
    cumulative age at send time comes within `margin` seconds of its
    target service's BASELINE_MAX_TOKEN_AGE. Returns a list of dicts
    describing each risk; empty if none (or if nothing reuses a single
    token - see module docstring)."""
    risks = []
    elapsed = 0.0
    for entry in script:
        if not isinstance(entry, dict):
            continue
        elapsed += entry.get("delay_seconds", 0) or 0
        if entry.get("type") != "request":
            continue
        service = _service_from_path(entry.get("path"))
        baseline_age = SERVICE_BASELINE_MAX_TOKEN_AGE.get(service)
        if baseline_age is None:
            continue
        if elapsed >= baseline_age - margin:
            risks.append(
                {
                    "path": entry.get("path"),
                    "cumulative_delay_seconds": round(elapsed, 3),
                    "service_baseline_max_token_age": baseline_age,
                }
            )
    return risks


def describe(script):
    """Human-readable, request-by-request rendering of what the campaign
    does and why - for understanding an attack script without opening
    the raw JSON. Requests without a recorded rationale (e.g. scripts
    generated before that field existed) just show as "(no rationale
    recorded)"."""
    lines = []
    elapsed = 0.0
    n = 0
    for entry in script:
        if not isinstance(entry, dict):
            continue
        elapsed += entry.get("delay_seconds", 0) or 0
        if entry.get("type") != "request":
            continue
        n += 1
        rationale = entry.get("rationale") or "(no rationale recorded)"
        lines.append(f"  #{n} t+{elapsed:.1f}s  {entry.get('method')} {entry.get('path')} - {rationale}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("attack_script_path")
    parser.add_argument("--round-seconds", type=int, default=None,
                         help="If given, fail (exit 1) when total_delay_seconds >= this.")
    parser.add_argument("--min-requests", type=int, default=None,
                         help="If given, fail (exit 1) when the script has fewer requests than this.")
    parser.add_argument(
        "--token-age-margin", type=float, default=0.0,
        help="Safety margin (seconds) for find_stale_token_risks - fail if any request's "
             "cumulative delay comes within this many seconds of its service's baseline "
             "max-token-age. Default 0 = disabled: replay_attack.py fetches a fresh token "
             "before every request, so this models a risk that no longer exists in normal "
             "operation - a real, high-volume script will routinely and harmlessly exceed a "
             "service's baseline_max_token_age in cumulative delay. Pass a positive value only "
             "to audit after changing replay_attack.py's token-fetch behavior.",
    )
    parser.add_argument("--describe", action="store_true",
                         help="Print a human-readable, request-by-request rationale listing.")
    args = parser.parse_args()

    with open(args.attack_script_path) as fh:
        raw = json.load(fh)
    metadata = raw.get("metadata") if isinstance(raw, dict) else None
    script = load_requests(raw)
    request_count, total_delay = summarize(script)

    print(request_count)
    print(total_delay)

    failed = False

    if args.round_seconds is not None and int(total_delay) >= args.round_seconds:
        suggested = int(total_delay) + 60
        print(
            f"ERROR: attack script's total delay_seconds ({total_delay}s) is >= "
            f"--round-seconds ({args.round_seconds}s). The replay would be killed mid-script "
            f"on every table. Pass a larger --round-seconds (e.g. --round-seconds {suggested}), "
            f"or regenerate a shorter attack script.",
            file=sys.stderr,
        )
        failed = True

    if args.min_requests is not None and request_count < args.min_requests:
        print(
            f"ERROR: attack script has {request_count} request(s), below --min-requests "
            f"{args.min_requests}. Regenerate with generate_attack_script.sh --generate-new "
            f"--min-requests {args.min_requests} (and a larger --max-requests to match).",
            file=sys.stderr,
        )
        failed = True

    if args.token_age_margin > 0:
        risks = find_stale_token_risks(script, margin=args.token_age_margin)
        if risks:
            print(
                f"ERROR: {len(risks)} request(s) would land within {args.token_age_margin}s of "
                "their service's baseline max-token-age if a single token were reused for the "
                "whole script (replay_attack.py no longer does this, but the script itself is "
                "still this close to the edge):",
                file=sys.stderr,
            )
            for r in risks:
                print(f"  {r['path']}: cumulative delay {r['cumulative_delay_seconds']}s, "
                      f"baseline max_token_age {r['service_baseline_max_token_age']}s", file=sys.stderr)
            failed = True

    if args.describe:
        print()
        if metadata:
            print("Metadata (inputs this plan was generated with):")
            print(f"  model:             {metadata.get('model')}")
            print(f"  min_requests:      {metadata.get('min_requests')}")
            print(f"  max_requests:      {metadata.get('max_requests')}")
            print(f"  max_total_seconds: {metadata.get('max_total_seconds')}")
            print()
        else:
            print("(no metadata section - this plan predates that field)")
            print()
        print(describe(script))

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
