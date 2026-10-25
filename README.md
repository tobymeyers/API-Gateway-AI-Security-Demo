# ISC2 Gateway Exercise — build

## What's here

```
docs/proposal.md,         The original pitch/spec this build implements, plus
docs/proposal-artifact.html  an AWS-style architecture diagram and a presentation
docs/architecture.png,    diagram (both regenerated from infra/template.yaml via
docs/presentation.png     docs/generate_architecture_diagram.py after any
                          architectural change).
infra/template.yaml       Everything AWS-native: Cognito, DynamoDB, API Gateway,
                          3 service Lambdas (inline), trigger-agent + end-round
                          endpoints (inline), the orchestrator Lambda resource,
                          CloudWatch dashboard.
functions/orchestrator/   The blue agent - calls Claude via Amazon Bedrock
                          (IAM auth, no separate API key).
functions/jwt_authorizer/ Custom TOKEN authorizer - verifies Cognito JWTs
                          (incl. M2M tokens) via PyJWT against the User
                          Pool's JWKS. Both this and orchestrator/ need
                          external deps, so both are packaged separately
                          by build_lambdas.sh (manylinux cross-platform
                          wheels, not whatever your local OS resolves).
openapi/                  Spec for the 3 services - read by both the blue agent
                          and the offline red-agent script generator.
attack-plans/             Every generated attack plan, one file per generation
                          (attack-plan-<run_id>.json, never overwritten) -
                          tracked in git (no secrets in it) so which exact
                          campaign produced a given event's scores is always
                          answerable later. See "Generate the fixed attack
                          script" below.
load-testers/             Both sides of a round's real traffic - two parallel
                          halves, good and adversarial:
  good-traffic/             Gatling simulation for the Good Load Tester (OAuth M2M).
  adversarial-traffic/
    generate_attack_script.py  Offline, run once: AI red-agent crafts the fixed
                                 attack script (see design rationale for why
                                 fixed) - the only AI-driven piece here.
    replay_attack.py             Event-day: deterministically replays that
                                 script at one table - no AI, just HTTP.
    requirements.txt              Local deps to run the two scripts above
                                 yourself (pip install -r
                                 load-testers/adversarial-traffic/requirements.txt) -
                                 separate from the Lambda packaging above.
scripts/                  build/deploy/teardown + facilitator helpers, plus:
  run_round.sh             Event-day facilitator tool: runs ONE complete real
                           round (agent + real Good Load Tester + real attack
                           replay, concurrently) for one table against a
                           stack that's already deployed and stays up - does
                           NOT deploy or tear down anything. See "Run a
                           round" below.
  comprehensive_test.sh    Exercises the whole system end to end (deploy,
                           smoke test, a small test attack-script generation,
                           one short test round with synthetic traffic,
                           tag/cost checks before AND after teardown, then
                           teardown) with full logging to logs/ for later
                           debugging. See DEPLOYMENT.md.
  comprehensive_gameday.sh           Same idea, but the real rehearsal: a roster CSV of
                           (table_id, prompt) pairs run back-to-back via
                           run_round.sh with the real attack script and real
                           Gatling traffic. See DEPLOYMENT.md's "Gameday
                           rehearsal run".
  test_harness_lib.sh      Shared run_step/logging/teardown-on-exit machinery
                           for the two scripts above - see DEPLOYMENT.md's
                           "Test harness architecture".
  score_round.sh           Facilitator tool: score one table's round on demand,
                           live or after teardown - see DEPLOYMENT.md's "Scoring".
  lib/                     The actual cost/config/health/roster/scoring logic
                           behind that harness, as real importable Python modules
                           (not inline bash heredocs) - unit-tested, see tests/.
  examples/sample_roster.csv  Example roster CSV for comprehensive_gameday.sh -
                           data, not a script, kept here since it's only used
                           by that one tool.
tests/                    Unit tests for every Python module in the repo
                           (scripts/lib/*.py AND load-testers/adversarial-traffic/*.py) -
                           stdlib unittest, zero extra deps, one runner regardless
                           of how many source directories feed into it - run with
                           `python3 -m unittest discover -s tests`.
```

## How defense actually gets enforced

A custom Lambda TOKEN authorizer (`functions/jwt_authorizer/`) checks that the
JWT is valid (RS256 signature against the User Pool's JWKS, issuer, expiry,
`token_use`) — **not** API Gateway's native `COGNITO_USER_POOLS` authorizer
type, which was the original design but turned out not to support Cognito's
client_credentials (M2M) access tokens at all, confirmed live via
`aws apigateway test-invoke-authorizer` against a textbook-valid token it
still rejected. See DEPLOYMENT.md's troubleshooting section for the
investigation. Everything the blue agent can adjust mid-round (rate
limit, block list, token-freshness policy, extra claim requirement) is instead
enforced inside the three service Lambdas, reading a DynamoDB row keyed by
`table_id` on every request. That split is what makes "hot deployment" a
sub-second data read rather than a stack update, and what lets two tables run
concurrently per 5-minute slot on one shared stack.

Each service Lambda also has its own **baseline** strictness via env vars
(`BASELINE_RPS`, `BASELINE_MAX_TOKEN_AGE`) — Service 1 limited, Service 2
moderate, Service 3 extreme. The agent's DynamoDB overrides are clamped with
`min(baseline, override)` in the service code, so a table can only tighten a
service's inherent strictness, never loosen it.

The `get_traffic_stats` tool and the scoring dashboard read **separate**
data sources on purpose. Scoring reads CloudWatch (`RequestCount`, dimensioned
by `Outcome`/`TrafficType`/`TableId`) so the dashboard can facet per table.
`get_traffic_stats` instead reads per-client counters straight out of
DynamoDB (`CLIENTS#`/`STATS#` items, written alongside the scoring metric) —
CloudWatch's metrics index is eventually consistent for brand-new
metric/dimension combinations, which would otherwise blind the tool for a
chunk of every round (every client is "new" right after a reset), right
when catching low-and-slow reconnaissance early matters most. Either way,
the agent-visible data carries no Good/Adversarial label, so the exercise
still requires genuine behavioral inference rather than reading the answer
off a header.

## Prerequisites

- AWS CLI configured with credentials that can create IAM roles (`CAPABILITY_NAMED_IAM`)
- Python 3.9+ locally for packaging (Lambda itself runs python3.12) -
  `deploy.sh` creates its own S3 artifact bucket automatically
- The blue agent calls Claude via Amazon Bedrock, IAM-only, no separate
  API key — Bedrock auto-enables models on first invoke, but a brand-new
  AWS account's first-ever Claude invocation may need a one-time
  "use case details" form submitted in-console first (see DEPLOYMENT.md 1b)
- sbt + a JDK, only if you'll run the Gatling load test yourself

No SAM CLI required — deploy uses plain `aws cloudformation package/deploy`.

Everything is hard-pinned to `us-west-2` (`scripts/lib.sh` exports
`AWS_REGION`/`AWS_DEFAULT_REGION`, overriding your CLI profile and any
ambient env var) — except the Bedrock inference call itself, which uses
a cross-region profile spanning `us-east-1`/`us-east-2`/`us-west-2` by
AWS design; see DEPLOYMENT.md for why that one piece can't be pinned
further.

Every resource is tagged `Project=ISC2_AI_Gateway` (constant) and
`Project_Run=<generated-id>` (unique per deploy) for Cost Explorer
filtering — see DEPLOYMENT.md 1c for the one-time tag-activation step
that's required before those tags become filterable in Billing.

## Event-day script order

1. **Well before the event** — deploy a staging stack and generate the fixed
   attack script once:
   ```bash
   ./scripts/deploy.sh
   ./scripts/generate_attack_script.sh --verify-round-seconds 60
   ```
   Defaults to a 100-150-request campaign - costs meaningfully more than a
   trivial run, see "Generate the fixed attack script" below for the real
   cost reference point. Writes a new, timestamped file under
   `attack-plans/` every time it actually generates - re-running this
   same command later reuses that file for free instead of spending
   Bedrock tokens again (pass `--generate-new` to force a fresh one).
2. **Morning of / pre-presentation** — deploy the real stack (if not already
   up) and get client credentials:
   ```bash
   ./scripts/deploy.sh
   ./scripts/get_client_credentials.sh
   ```
   Optionally rehearse the full day against the real roster first:
   ```bash
   ./scripts/comprehensive_gameday.sh --roster scripts/examples/sample_roster.csv
   ```
3. **During the event** — one `run_round.sh` call per table, back-to-back,
   stack stays up the whole time:
   ```bash
   ./scripts/run_round.sh 3 "<facilitator prompt for this table>"
   ```
   Watch the CloudWatch dashboard live, or score any table on demand:
   ```bash
   ./scripts/score_round.sh
   ```
4. **After the event** — tear down:
   ```bash
   ./scripts/teardown.sh
   ```

See "Run a round" below for what `run_round.sh` actually does under the hood,
and DEPLOYMENT.md for the full walkthrough.

## Deploy

```bash
./scripts/deploy.sh
```

This builds `infra/build/orchestrator.zip` (with the `anthropic` package
installed into it), uploads it, and deploys the stack. Outputs include the API
URL, Cognito IDs, and the dashboard URL.

Get the two app clients' secrets (not exposed as stack outputs):

```bash
./scripts/get_client_credentials.sh
```

## Run a round

```bash
./scripts/run_round.sh 3 "Watch for clients whose request pattern looks scripted \
and rate-limit them; don't touch anything that looks like normal jitter."
```

This is the actual event-day primitive: one command, one complete round for
table 3, against whatever stack is already deployed. It does NOT deploy or
tear anything down - the stack stays up for the rest of the day's rounds.
Under the hood it runs the three things a round actually needs, concurrently,
for the same `table_id`:

1. **The blue agent** - calls `/trigger-agent`, which asynchronously invokes the
   orchestrator Lambda for ~60 seconds (leaving buffer inside the 5-minute
   slot - capped repo-wide to control Bedrock cost). It seeds baseline
   config, then runs the agent loop with check-ins every 20s. Defenses stay
   exactly as the agent left them - nothing resets them on a timer. Traffic
   doesn't start for another `--agent-startup-delay-seconds` (default 10) -
   a fixed head start so Lambda cold start and the first (uncached) Bedrock
   call land on dead air instead of on live, unprotected requests.
2. **Good traffic** - the real Good Load Tester (Gatling).
3. **Adversarial traffic** - the real, fixed attack script (generated ahead
   of time - see below), replayed byte-for-byte. Defaults to the most
   recently generated file in `attack-plans/` - pass `--attack-script
   <path>` to use a specific one instead. Whichever one actually ran is
   recorded in that round's own `metadata.json` (see below) and printed
   in this command's own output, so which exact script produced a given
   score is never ambiguous after the fact.

Once both traffic generators' real exit codes are in hand (not a guess about
when they're "probably done"), it calls `/end-round` - a synchronous signal
that resets the agent's defenses back to baseline right then, deterministically
covering every request either generator could have sent. It then verifies
that reset actually happened and the orchestrator really called Bedrock, and
prints this round's real score (see "Scoring" in DEPLOYMENT.md).

Watch the corrected score live on the CloudWatch dashboard (`DashboardUrl`
output), or run `./scripts/score_round.sh` any time afterward - it works
after teardown too.

**Every round also gets a self-contained report folder**,
`reports/round-table<N>-<run_id>/`, for reviewing many rounds side by side
after the fact without digging through raw logs or CloudWatch:
```
reports/round-table3-20261024-193212/
  metadata.json          inputs: table_id, prompt, round_seconds, target_rps,
                          attack_script path, started_at
  run_summary.json        results: a steps rollup, the final score, and
                          real good/adversarial request counts - parsed
                          back out of this round's own log, not re-queried
                          from CloudWatch (which lags for brand-new
                          metric/dimension combinations - see "How defense
                          actually gets enforced" above). good_traffic and
                          adversarial_traffic share one schema:
                          {total, allowed, denied, errored} - "errored" is
                          an unrelated infra failure, kept separate from
                          "denied" (the agent actually blocking it)
  good-traffic-report/index.html        Gatling's own HTML report (response
                          times, throughput, OK/KO breakdown), moved here
                          from its default location under
                          load-testers/good-traffic/target/
  adversarial-traffic-report/index.html  every adversarial request actually
                          sent - status, latency, the model's own rationale
                          for that specific request (see "Generate the fixed
                          attack script" below), AND an "attack successful"
                          column: TRUE means it got through our defense logic,
                          whatever status code came back (a 500 is a
                          successful attack, not a block, since deny() always
                          returns exactly 403). FALSE means our own deny()
                          helper actually returned that 403 (confirmed by its
                          response body, {"approved": "false", ...}). N/A
                          covers two different "can't tell" cases: a
                          network-level error with no response at all, or a
                          403 that never reached our Lambda in the first place -
                          API Gateway's own routing rejected it before our code
                          ran (e.g. a bare path that doesn't satisfy a
                          "{proxy+}" resource) - confirmed live via a direct
                          CloudWatch query showing zero Denied datapoints for
                          a round whose log still showed two "-> 403"s.
  logs/                    this round's own .log/.summary.jsonl, for
                          debugging this specific run in isolation
```

Running the three pieces separately by hand (e.g. for debugging one piece in
isolation) still works - `run_round.sh` is a convenience wrapper, not a new
mechanism:

```bash
./scripts/trigger_agent.sh 3 "..."   # agent only

# see DEPLOYMENT.md step 3 for exporting $API_URL/$TOKEN_URL/the client
# id+secret pairs as shell variables first - don't paste literal <...>
# placeholders into bash, </> are redirection operators there
cd load-testers/good-traffic
sbt "Gatling/testOnly simulations.GoodTrafficSimulation" \
  -Dgateway.url="$API_URL" -Dtoken.url="$TOKEN_URL" \
  -Dclient.id="$GOOD_CLIENT_ID" -Dclient.secret="$GOOD_CLIENT_SECRET" -Dtable.id=3

python3 load-testers/adversarial-traffic/replay_attack.py --script attack-plans/attack-plan-<run_id>.json \
  --gateway-url "$API_URL" --table-id 3 --token-url "$TOKEN_URL" \
  --client-id "$ADVERSARIAL_CLIENT_ID" --client-secret "$ADVERSARIAL_CLIENT_SECRET"
```

## Generate the fixed attack script (do this well before the event)

```bash
./scripts/generate_attack_script.sh --verify-round-seconds 60
```

`scripts/generate_attack_script.sh` resolves `--staging-url`/`--token-url`/the
adversarial client credentials from the currently-deployed stack automatically
(no manual `stack_output`/`describe-user-pool-client` wiring). `--verify-round-seconds`
is optional and runs `attack_script_check.py` immediately afterward so you see
pass/fail without a separate command. Anything after a literal `--` is
forwarded straight through to `generate_attack_script.py` (e.g. `-- --max-requests 8`).

**Every generation writes a new file**, `attack-plans/attack-plan-<run_id>.json`
(`<run_id>` is the same `YYYYMMDD-HHMMSS` convention used everywhere else in
this repo) - never overwritten, and tracked in git (no secrets in it -
`Authorization` headers are deliberately never recorded). That's what makes
"which exact attack script produced this score" always answerable later: every
round's `metadata.json` and printed output name the literal file it replayed.
The file itself carries a `metadata` section recording the minimum inputs
that shaped it - `model`, `min_requests`, `max_requests`, `max_total_seconds` -
right alongside the `requests` list (`attack_script_check.py --describe` shows
it too), so reviewing a plan never requires digging up the original command.
(Files generated before this field existed are still readable - every reader
falls back to treating a bare list as the whole `requests` array.) Because
nothing is ever overwritten, **the default behavior is to reuse the most
recent file and do nothing else** - no AWS calls, no Bedrock cost - unless
you pass `--generate-new`:

```bash
./scripts/generate_attack_script.sh                      # reuse latest, or
                                                           # generate the first
                                                           # one if none exist
./scripts/generate_attack_script.sh --generate-new --verify-round-seconds 60
```

Defaults to `--min-requests 100` / `--max-requests 150` — enough volume
against a ~1000-good-request round for the scoring range (`+100`/`-1000`) to
mean something. An unconstrained "stay stealthy" prompt converged on just 3
requests in practice, too thin to score meaningfully. Every run prints its
resolved config (both of these included) before spending a single Bedrock
token, so the defaults are never a silent surprise.

**Cost scales with both request count and the growing uncached
conversation** (no prompt caching) — a 3-request run cost $3.26 across 121
calls, so the 100-150-request default costs meaningfully more. (If you've
just changed `generate_attack_script.py` itself — new prompt, new tool
signature, anything that could change its behavior — verify it cheaply
first with small `--min-requests`/`--max-requests` overrides and a
throwaway `--output` before spending on the real volume. Not something to
repeat on every normal event-day generation once the pipeline's proven.)

Override either flag any time, including back to unconstrained low-and-slow
(`--min-requests 0`):

```bash
./scripts/generate_attack_script.sh --generate-new --min-requests 20 \
  --verify-round-seconds 60 -- --max-requests 30
```

Every recorded request also carries a `rationale` field — the model's own
one-line explanation of the tactic behind that specific request (e.g. "burst
to test service3's rate limit"). To see the whole campaign explained,
request by request, without opening the raw JSON:

```bash
python3 scripts/lib/attack_script_check.py attack-plans/attack-plan-<run_id>.json --describe
```

This fixes a real incident: a reused token aging past a service's own
baseline freshness requirement (Service 3's is 30s) can get a request
denied by baseline alone, with no agent action involved — which silently
broke the "a no-op agent scores 0" invariant. The fix is in
`replay_attack.py`, which now fetches a fresh token immediately before
every request, so token age is always ~0s regardless of script pacing — a
real, high-volume script's cumulative delay will routinely and harmlessly
exceed 30s+ for its later requests, and that's fine. `attack_script_check.py
--token-age-margin` can audit for the old reused-token risk directly
(default `0` = off, since real replay no longer has this exposure at all —
pass a positive value only if you've changed `replay_attack.py`'s
token-fetch behavior and want to check for a regression).

Run this once against a staging deployment, review the generated file, and
reuse the exact same one for every table on event day (the default behavior
above does this automatically) — that's what keeps `N_malicious_baseline`
constant across all 12 tables (see the design doc for why a live-reasoning
attacker every round would break the scoring formula).

## Teardown

```bash
./scripts/teardown.sh
```

## Known simplifications / verify before the event

- **Cognito `AccessTokenValidity`** is set to 5 minutes, not the proposal's
  original "1 minute" — confirm your account/region's actual minimum before
  the event. The freshness policy that actually matters for the exercise
  (`max_token_age_seconds`) is enforced independently in DynamoDB/Lambda, not
  by this Cognito setting, so this doesn't block the core mechanic.
- **`required_claim`** can only check claims that a client_credentials token
  actually carries (`client_id`, `scope`, `iat`, etc.) — there's no user
  profile to attach custom attributes to on an M2M token.
- **Rate limiting granularity** is per-second buckets in DynamoDB, not a
  true sliding window — adequate for a 5-minute demo round, not a production
  rate limiter.
- **Gatling/sbt versions** in `build.sbt`/`project/plugins.sbt` are pinned to
  what was current at write time — bump if `sbt gatling:test` can't resolve them.
- **Model choice**: defaults to `us.anthropic.claude-sonnet-4-5-20250929-v1:0`,
  verified invocable with no extra access. The newest generation
  (`claude-opus-5`/`claude-sonnet-5`) is visible in the Bedrock model
  catalog but returned an AWS-Sales-gated `AccessDeniedException` on a
  standard account as of writing — see DEPLOYMENT.md 1b before switching
  to it. Override via the `ModelId` stack parameter either way.
