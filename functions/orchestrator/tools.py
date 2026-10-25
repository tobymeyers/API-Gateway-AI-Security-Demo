"""AWS-facing implementations of the blue agent's published tool list.

Every write here goes to StateTable, keyed CFG#{table_id}, and is read by
the service Lambdas on the very next request - no redeploy, sub-second.
`None` in a config field means "no agent override"; the service Lambdas
clamp with min(baseline, override) so an agent can only tighten a
service's own baseline strictness, never loosen it.
"""
import os
import time

import boto3

dynamodb = boto3.resource("dynamodb")
cognito = boto3.client("cognito-idp")

STATE_TABLE = os.environ["STATE_TABLE_NAME"]
USER_POOL_ID = os.environ["USER_POOL_ID"]
GOOD_CLIENT_ID = os.environ["GOOD_CLIENT_ID"]
ADVERSARIAL_CLIENT_ID = os.environ["ADVERSARIAL_CLIENT_ID"]

table = dynamodb.Table(STATE_TABLE)

_openapi_spec_text = None


def get_openapi_spec():
    global _openapi_spec_text
    if _openapi_spec_text is None:
        spec_path = os.path.join(os.path.dirname(__file__), "gateway-openapi.yaml")
        with open(spec_path) as fh:
            _openapi_spec_text = fh.read()
    return {"openapi_spec_yaml": _openapi_spec_text}


def _default_config():
    return {
        "rate_limit_rps": None,
        "blocked_clients": [],
        "max_token_age_seconds": None,
        "required_claim": None,
        "block_all": False,
    }


def get_current_config(table_id):
    resp = table.get_item(Key={"pk": f"CFG#{table_id}"})
    item = resp.get("Item")
    return item["data"] if item else _default_config()


def _put_config(table_id, data):
    table.put_item(Item={"pk": f"CFG#{table_id}", "data": data})


def reset_config(table_id):
    # Also clears this table's traffic-stats bookkeeping (see
    # get_traffic_stats below) - otherwise a stale client from an
    # earlier test run of the same table_id would leak into a new round.
    clients_resp = table.get_item(Key={"pk": f"CLIENTS#{table_id}"})
    client_ids = clients_resp.get("Item", {}).get("ids") or set()
    for client_id in client_ids:
        table.delete_item(Key={"pk": f"STATS#{table_id}#{client_id}"})
    table.delete_item(Key={"pk": f"CLIENTS#{table_id}"})

    _put_config(table_id, _default_config())
    return {"status": "reset", "table_id": table_id}


def set_rate_limit(table_id, requests_per_second):
    cfg = get_current_config(table_id)
    cfg["rate_limit_rps"] = int(requests_per_second)
    _put_config(table_id, cfg)
    return {"status": "ok", "rate_limit_rps": cfg["rate_limit_rps"]}


def block_client(table_id, client_id):
    cfg = get_current_config(table_id)
    blocked = set(cfg.get("blocked_clients") or [])
    blocked.add(client_id)
    cfg["blocked_clients"] = sorted(blocked)
    _put_config(table_id, cfg)
    return {"status": "ok", "blocked_clients": cfg["blocked_clients"]}


def unblock_client(table_id, client_id):
    cfg = get_current_config(table_id)
    blocked = set(cfg.get("blocked_clients") or [])
    blocked.discard(client_id)
    cfg["blocked_clients"] = sorted(blocked)
    _put_config(table_id, cfg)
    return {"status": "ok", "blocked_clients": cfg["blocked_clients"]}


def block_all_traffic(table_id):
    """Unconditional kill switch - every request to all three services is
    denied regardless of client_id or rate, unlike set_rate_limit (a volume
    cap that structurally can't catch a low-RPS client) or block_client
    (which requires naming a client_id already seen). Checked by the
    service Lambdas before blocked_clients/rate_limit/token checks."""
    cfg = get_current_config(table_id)
    cfg["block_all"] = True
    _put_config(table_id, cfg)
    return {"status": "ok", "block_all": True}


def unblock_all_traffic(table_id):
    """Lifts block_all_traffic. Other defenses (rate limit, blocked_clients,
    token TTL, required claim) remain exactly as they were set - this only
    removes the unconditional kill switch."""
    cfg = get_current_config(table_id)
    cfg["block_all"] = False
    _put_config(table_id, cfg)
    return {"status": "ok", "block_all": False}


def set_token_ttl_policy(table_id, seconds):
    cfg = get_current_config(table_id)
    cfg["max_token_age_seconds"] = int(seconds)
    _put_config(table_id, cfg)

    # Also nudges Cognito's own issuance TTL for newly-issued tokens.
    # Verify during build whether AccessTokenValidity accepts values this
    # low in your account/region - the DynamoDB policy above is what
    # actually enforces freshness sub-second, so this is defense-in-depth
    # rather than load-bearing; failures here are swallowed on purpose.
    for client_id in (GOOD_CLIENT_ID, ADVERSARIAL_CLIENT_ID):
        try:
            cognito.update_user_pool_client(
                UserPoolId=USER_POOL_ID,
                ClientId=client_id,
                AccessTokenValidity=max(int(seconds), 1),
                TokenValidityUnits={"AccessToken": "seconds"},
            )
        except Exception:
            pass
    return {"status": "ok", "max_token_age_seconds": cfg["max_token_age_seconds"]}


def require_additional_claim(table_id, claim_name, claim_value, match="equals"):
    cfg = get_current_config(table_id)
    cfg["required_claim"] = {"name": claim_name, "value": claim_value, "match": match}
    _put_config(table_id, cfg)
    return {"status": "ok", "required_claim": cfg["required_claim"]}


def get_traffic_stats(table_id):
    """Cumulative-since-round-start per-client activity, sourced from
    StateTable rather than CloudWatch. CloudWatch's list_metrics index is
    eventually consistent for brand-new metric/dimension combinations -
    since every client is "brand new" at the start of every round, that
    lag would blind this tool for a chunk of every round, right when
    catching low-and-slow reconnaissance early matters most. The
    service Lambdas write CLIENTS#/STATS# alongside the CloudWatch
    metrics used for scoring, so this read is sub-second-fresh instead.
    """
    clients_resp = table.get_item(Key={"pk": f"CLIENTS#{table_id}"})
    client_ids = clients_resp.get("Item", {}).get("ids") or set()

    now = int(time.time())
    clients = []
    for client_id in client_ids:
        stats_resp = table.get_item(Key={"pk": f"STATS#{table_id}#{client_id}"})
        item = stats_resp.get("Item")
        if not item:
            continue
        cnt = int(item.get("cnt", 0))
        first_seen = int(item.get("first_seen", now))
        elapsed = max(now - first_seen, 1)
        clients.append(
            {
                "client_id": client_id,
                "request_count": cnt,
                "seconds_since_first_request": elapsed,
                "avg_requests_per_second": round(cnt / elapsed, 3),
            }
        )
    return {"clients": clients}
