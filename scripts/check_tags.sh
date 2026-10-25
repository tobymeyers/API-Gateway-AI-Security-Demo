#!/usr/bin/env bash
# Account-wide orphan/drift detector for this project - deliberately
# independent of whether any CloudFormation stack currently exists.
# Answers "did something (another session, a failed teardown, a manual
# experiment) leave AWS resources running that nothing is tracking
# anymore?", not just "is the current stack tagged correctly."
#
# Two passes:
#   1. Tag-based: every resource Resource Groups Tagging API shows tagged
#      Project=<tag>, regardless of stack state.
#   2. Name-based (best-effort): a handful of key services, listed
#      directly and matched against this project's naming convention, to
#      catch anything a misbehaving process created WITHOUT tagging it
#      (tag search alone can't find that).
#
# For every resource found either way, this makes a real existence call
# (describe/get) before reporting it - the tagging API has a confirmed
# indexing lag and can list resources for hours after they were actually
# deleted (seen live on this exact project). A "ghost" entry that no
# longer actually exists is reported as such, not as a live orphan.
#
# Usage: ./scripts/check_tags.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_aws_credentials "checking tags"

python3 - "$PROJECT_TAG" "$AWS_REGION" <<'PYEOF'
import json
import subprocess
import sys

PROJECT_TAG, REGION = sys.argv[1], sys.argv[2]
NAME_NEEDLE = "isc2-gateway-exercise"  # this project's stack/resource naming convention


def aws(*args):
    """Run an AWS CLI command, return (ok, parsed_json_or_None, raw_stderr)."""
    try:
        out = subprocess.run(
            ["aws", *args, "--region", REGION, "--output", "json"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return False, None, str(exc)
    if out.returncode != 0:
        return False, None, out.stderr.strip()
    try:
        return True, (json.loads(out.stdout) if out.stdout.strip() else {}), ""
    except json.JSONDecodeError:
        return True, {}, ""


def exists_cognito_pool(pool_id):
    ok, _, err = aws("cognito-idp", "describe-user-pool", "--user-pool-id", pool_id)
    return ok, err


def exists_lambda(name):
    ok, _, err = aws("lambda", "get-function", "--function-name", name)
    return ok, err


def exists_dynamodb(name):
    ok, _, err = aws("dynamodb", "describe-table", "--table-name", name)
    return ok, err


def exists_rest_api(api_id):
    ok, _, err = aws("apigateway", "get-rest-api", "--rest-api-id", api_id)
    return ok, err


def exists_log_group(name):
    name = name.rstrip("*").rstrip(":")
    ok, data, err = aws("logs", "describe-log-groups", "--log-group-name-prefix", name)
    if not ok:
        return False, err
    groups = (data or {}).get("logGroups", [])
    return any(g.get("logGroupName") == name for g in groups), ""


def exists_s3_bucket(name):
    ok, _, err = aws("s3api", "head-bucket", "--bucket", name)
    return ok, err


# ARN service -> (resource-id extractor, existence checker)
CHECKERS = {
    "cognito-idp": (lambda r: r.split("/")[-1], exists_cognito_pool),
    "lambda": (lambda r: r.split(":")[-1], exists_lambda),
    "dynamodb": (lambda r: r.split("/")[-1], exists_dynamodb),
    "apigateway": (lambda r: r.split("/")[-1], exists_rest_api),
    "logs": (lambda r: r.split("log-group:")[-1], exists_log_group),
    "s3": (lambda r: r, exists_s3_bucket),
}


def check_arn(arn):
    """Returns (status, detail) where status is one of:
    'exists', 'ghost' (tagging API stale entry), 'unknown-type' (couldn't verify)."""
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return "unknown-type", "unparseable ARN"
    service, resource = parts[2], parts[5]
    if service not in CHECKERS:
        return "unknown-type", f"no checker for service '{service}' - verify manually"
    extractor, checker = CHECKERS[service]
    try:
        resource_id = extractor(resource)
        ok, err = checker(resource_id)
    except Exception as exc:
        return "unknown-type", f"checker error: {exc}"
    return ("exists", "") if ok else ("ghost", err)


def tagged_resources():
    ok, data, err = aws(
        "resourcegroupstaggingapi", "get-resources",
        "--tag-filters", f"Key=Project,Values={PROJECT_TAG}",
    )
    if not ok:
        print(f"ERROR: couldn't query Resource Groups Tagging API: {err}", file=sys.stderr)
        sys.exit(1)
    return [m["ResourceARN"] for m in data.get("ResourceTagMappingList", [])]


def name_matches(service_cli, list_args, jmespath_names_key, id_key):
    """List a service's resources directly and keep ones whose name
    contains NAME_NEEDLE - catches untagged leftovers tag search can't see."""
    ok, data, err = aws(service_cli, *list_args)
    if not ok:
        return [], err
    items = data
    for key in jmespath_names_key.split("."):
        items = items.get(key, []) if isinstance(items, dict) else items
    hits = []
    for item in items:
        name = item.get("Name") or item.get("FunctionName") or item.get("TableName") or ""
        if NAME_NEEDLE in name.lower():
            hits.append(item.get(id_key) or name)
    return hits, ""


print(f"=== Pass 1: resources tagged Project={PROJECT_TAG} (any stack, or no stack at all) ===\n")
arns = tagged_resources()
if not arns:
    print("None found.")
else:
    exists_count = ghost_count = unknown_count = 0
    for arn in sorted(arns):
        status, detail = check_arn(arn)
        if status == "exists":
            exists_count += 1
            print(f"  LIVE     {arn}")
        elif status == "ghost":
            ghost_count += 1
            print(f"  ghost    {arn}  (tagging-API stale entry, already deleted - {detail})")
        else:
            unknown_count += 1
            print(f"  ?        {arn}  ({detail})")
    print(f"\n{exists_count} live, {ghost_count} ghost (ignore), {unknown_count} unverified.")
    if exists_count:
        print("^ Anything marked LIVE above is a real, currently-existing, billable resource.")
        print("  If you don't have a deploy you expect to be running right now, investigate.")

print(f"\n=== Pass 2: name-based sweep for '{NAME_NEEDLE}' (catches untagged leftovers) ===\n")

sweeps = [
    ("cognito-idp", ["list-user-pools", "--max-results", "60"], "UserPools", "Id"),
    ("lambda", ["list-functions"], "Functions", "FunctionName"),
    ("dynamodb", ["list-tables"], "TableNames", None),
    ("apigateway", ["get-rest-apis"], "items", "id"),
    ("iam", ["list-roles"], "Roles", "RoleName"),  # free, but still a sign of leftover drift
    ("cloudwatch", ["list-dashboards"], "DashboardEntries", "DashboardName"),
    # Added after a confirmed live miss: an auto-created (not CFN-managed)
    # Lambda log group is never tagged, so Pass 1 can't see it at all -
    # this sweep was the only thing that could have caught it, but it was
    # missing CloudWatch Logs entirely. describe-log-groups lists every
    # log group in the account (no name filter at the API level, same as
    # the lambda/dynamodb/etc. sweeps above), filtered client-side below.
    ("logs", ["describe-log-groups"], "logGroups", "logGroupName"),
]
any_untagged_hit = False
tagged_ids = {arn.split("/")[-1].split(":")[-1] for arn in arns}
# log-group ARNs (arn:...:log-group:/aws/lambda/foo:*) don't fit the
# generic "last path segment" extraction above (it would yield just "*"),
# so compute their tagged set separately rather than mis-flagging a
# properly-tagged, currently-stack-owned log group as untagged.
tagged_log_group_names = {
    arn.split(":log-group:")[-1].rstrip(":*") for arn in arns if ":log-group:" in arn
}
for service_cli, list_args, key, id_key in sweeps:
    ok, data, err = aws(service_cli, *list_args)
    if not ok:
        print(f"  (skipped {service_cli}: {err})")
        continue
    raw_items = data.get(key, []) if isinstance(data, dict) else []
    for item in raw_items:
        if isinstance(item, str):
            name, rid = item, item
        else:
            name = item.get("Name") or item.get("FunctionName") or item.get("name") or item.get("logGroupName") or ""
            rid = item.get(id_key) if id_key else name
        already_tagged = rid in (tagged_log_group_names if service_cli == "logs" else tagged_ids)
        if NAME_NEEDLE in (name or "").lower() and not already_tagged:
            any_untagged_hit = True
            print(f"  UNTAGGED {service_cli}: {name} ({rid}) - matches naming convention but has no Project tag")

if not any_untagged_hit:
    print("None found - nothing matching this project's naming convention is missing its tag.")
PYEOF
