# Deployment Guide

Operational runbook for deploying and running the ISC2 gateway exercise.
For architecture/design rationale, see `README.md` and
`docs/proposal-artifact.html`. All commands below are run from the
`ISC2_AI_Gateway/` project root.

## Prerequisites

- An AWS IAM user (with an access key) whose group holds the 10 managed
  policies listed in step 1a — deploy uses `CAPABILITY_NAMED_IAM` and
  touches CloudFormation, IAM, Lambda, API Gateway, DynamoDB, Cognito,
  CloudWatch, Bedrock, and S3; Billing read access is required too, not
  optional — we must be able to verify actual spend, not just trust an
  in-run estimate (see step 1c)
- Python 3.9+ locally, for packaging (Lambda itself runs python3.12) -
  `deploy.sh` creates its own S3 artifact bucket automatically, no manual
  setup step for that
- To run `load-testers/adversarial-traffic/generate_attack_script.py`, `load-testers/adversarial-traffic/replay_attack.py`,
  or `scripts/comprehensive_test.sh`/`scripts/comprehensive_gameday.sh` (which call
  both) from your own machine: `pip install -r load-testers/adversarial-traffic/requirements.txt`
  once. This is separate from the Lambda packaging above - those two
  scripts run locally, not inside Lambda, so they need the package
  actually importable in your own Python environment too.
- Bedrock model access enabled for Anthropic Claude models in your deploy
  region (step 1b) — the blue agent calls Claude via Amazon Bedrock, not
  the direct Anthropic API, so there's no separate API key to manage
- sbt + a JDK — only if you're running the Gatling good-traffic load test
  yourself (manually, per step 5) or via `scripts/comprehensive_gameday.sh`, which
  invokes it for every table in its roster and will fail its preflight
  check immediately if `sbt` isn't on `PATH`

No SAM CLI required — deployment uses plain `aws cloudformation package`
and `aws cloudformation deploy`.

**Region: hard-pinned to `us-west-2`.** `scripts/lib.sh` exports
`AWS_REGION`/`AWS_DEFAULT_REGION=us-west-2` at the top of every script,
overriding your AWS CLI profile's own default and any ambient env var -
every resource this project creates (the stack, the S3 artifact bucket,
Cognito, everything) lands there, deliberately, so nothing drifts to a
different region by accident. To retarget elsewhere, edit that line in
`scripts/lib.sh` directly (not an env var override) and re-verify
`ModelId` first (see 1b below) — the default assumes a Bedrock cross-
region inference profile whose member regions are `us-east-1`,
`us-east-2`, and `us-west-2` only.

One caveat this pin *cannot* close: the Bedrock inference call itself
uses a cross-region inference profile (required for every current-
generation Claude model — there's no direct single-region on-demand
alternative for them), and AWS may execute that inference in any of the
profile's three member regions for capacity/latency reasons, regardless
of which region you invoke it from. Everything else this project
touches is fully region-pinned; the model inference specifically is
pinned to that three-region set, not to `us-west-2` alone — that's an
AWS Bedrock product constraint, not something this project's config can
override.

## 1. One-time setup

### 1a. Create the AWS IAM user

This project deliberately uses a scoped-down IAM user rather than root or
personal admin credentials, so a leaked key has bounded blast radius and
gets torn down with everything else on event day.

1. **IAM → User groups → Create group.** Name it (e.g.
   `ISC2_2026_Demo_User_Group`) and attach these 10 AWS managed policies
   (`check_permissions.sh` enforces exactly this set before every
   deploy/teardown, so don't drift from it without also updating that
   script):
   - `AWSCloudFormationFullAccess`
   - `IAMFullAccess`
   - `AWSLambda_FullAccess`
   - `AmazonAPIGatewayAdministrator`
   - `AmazonDynamoDBFullAccess`
   - `AmazonCognitoPowerUser`
   - `CloudWatchFullAccess`
   - `AmazonS3FullAccess`
   - `AmazonBedrockFullAccess`
   - `AWSBillingReadOnlyAccess` — required, not optional: `check_cost.sh`
     (and `comprehensive_test.sh`'s pre/post-teardown cost checks) need
     `ce:GetCostAndUsage` to verify actual spend. The in-run `USAGE_LOG`
     estimate is list-price token counting, not a substitute for the
     real billed number — we must be able to check the real one.
2. **IAM → Users → Create user.** Name it (e.g. `ISC2_2026_Demo_User`),
   skip console access (this user only needs programmatic/CLI access),
   and add it to the group from step 1.
3. **Create an access key.** Open the new user → Security credentials →
   Create access key → choose **"Command Line Interface (CLI)"** as the
   use case, confirm the recommendation prompt, and save the Access Key
   ID + Secret Access Key immediately — the secret is shown only once.
4. **Configure the CLI locally:**
   ```bash
   aws configure          # paste the access key ID/secret from step 3
   ```
   `scripts/check_permissions.sh` (run automatically by `deploy.sh` and
   `teardown.sh`) will fail loudly if this user's group permissions ever
   drift from the list above — e.g. someone detaches a policy mid-event.

**At the end of the event, delete this access key** (not the user) —
`teardown.sh` does this automatically as its last step, or manually via
IAM → Users → this user → Security credentials → Access keys → Delete.

### 1b. Bedrock model access (mostly automatic - one caveat for Claude)

The blue agent (and the offline attack-script generator) call Claude via
Amazon Bedrock, authenticated purely with the IAM credentials from step
1a — no Anthropic account, no separate API key, one bill (AWS) instead
of two. This is also what makes the project easy to hand to participants
afterward: anyone with their own AWS account can run it, no Anthropic
signup required — modulo the one caveat below.

AWS retired the old "Model access" console opt-in page: serverless
foundation models now auto-enable account-wide the first time they're
invoked, no manual toggle. There are two different Anthropic-specific
wrinkles on top of that, confirmed by actually invoking Bedrock (not
just reading the console) against a live account:

- **The standard gate:** a brand-new AWS account's *first-ever*
  invocation of a Claude model on Bedrock may require submitting a
  short one-time "use case details" form in the console before it's
  allowed through — per-account, one-time, not recurring.
- **A harder gate on the newest generation specifically:** as of
  writing, `claude-opus-5` and `claude-sonnet-5` are both listed by
  `list-foundation-models` and have inference profiles, but invoking
  either returned `AccessDeniedException: ... not available for this
  account ... contact AWS Sales for additional access` on a standard
  account — not the self-service use-case form, an actual sales
  conversation. **The default shipped in this repo
  (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) is the previous
  generation, verified invocable with no extra access beyond standard
  IAM permissions** — deliberately chosen over the newest generation so
  a fresh account (including one handed to a participant) isn't blocked
  on a sales conversation before it can run at all.

1. If you want the newest generation instead of the default, confirm it
   actually invokes on your account first: `aws bedrock-runtime
   invoke-model --model-id us.anthropic.claude-opus-5 --body
   '{"anthropic_version":"bedrock-2023-05-31","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'
   --cli-binary-format raw-in-base64-out /tmp/out.json` — if that
   succeeds, override `MODEL_ID`; if it 403s with the AWS-Sales message
   above, stay on the default or contact AWS Sales.
2. To list what's actually available (model IDs change over time):
   `aws bedrock list-foundation-models --query
   "modelSummaries[?contains(modelId,'anthropic.claude')].modelId"` for
   base IDs, and `aws bedrock list-inference-profiles --query
   "inferenceProfileSummaries[].inferenceProfileId"` for the `us.`/`global.`
   cross-region IDs newer models actually require (a bare foundation-model
   ID for models like Haiku 4.5 / Sonnet 4.5 / Opus 5 fails with
   `ValidationException: ... on-demand throughput isn't supported ...
   use ... an inference profile`).
3. **If this AWS account has never invoked any Claude model on Bedrock
   before**, open Bedrock console → Model catalog → select a Claude
   model → invoke it once in the Playground, to clear the standard
   use-case-details form. Do this once, before running `deploy.sh`, on
   any brand-new account.
4. `deploy.sh` validates whatever `MODEL_ID` you pass with a real,
   minimal (1-token) invoke against Bedrock before it starts the
   CloudFormation deploy, so a wrong model ID or either gate above fails
   in seconds rather than mid-round.

### 1c. Activate cost-allocation tags (one-time, per account)

Every deploy tags all taggable resources with `Project=ISC2_AI_Gateway`
(constant across every run) and `Project_Run=<generated-id>` (unique per
`deploy.sh` invocation - see step 2). Tagging the resources is only half
of what's needed to actually *see* this in Billing, though: AWS requires
you to separately **activate** each tag key as a cost-allocation tag
before Cost Explorer will let you filter by it - tags existing on
resources alone don't make them filterable.

1. **Billing and Cost Management console → Cost allocation tags** (or
   `aws ce ...` doesn't cover this - it's console/Billing-API only, no
   plain CLI equivalent). Find `Project` and `Project_Run` under
   "User-defined cost allocation tags" (they only appear here *after*
   at least one resource has been tagged with them at least once, so
   do one `deploy.sh` run first) and activate both.
2. **New tags can take up to 24 hours to appear as activatable and,
   separately, up to 24 hours after that for tagged costs to show up in
   Cost Explorer** — this is standard AWS propagation delay, not
   something any script here can speed up. Don't expect same-day
   visibility on a fresh account; budget a day's lead time before the
   event if you want tag-filtered cost visibility working live.
3. Once activated, filter Cost Explorer by tag `Project = ISC2_AI_Gateway`
   for cumulative project spend across every deploy ever run, or by
   `Project_Run = <specific id>` (printed at the end of every
   `deploy.sh` run) to isolate one deploy-to-teardown lifecycle.

**If your account is a member/linked account in an AWS Organization,
you likely can't do step 1 at all** — confirmed live: the Cost
Allocation Tags page in Billing returned "Access denied. Cost allocation
tags are managed at the payer account level," not a permissions issue
any IAM policy on this account can fix. This is an AWS Organizations
constraint: only the **management (payer) account** can activate
user-defined cost allocation tags; it then applies org-wide. If you hit
this, either ask whoever administers the organization's payer account to
activate `Project`/`Project_Run`, or accept that tag-filtered Cost
Explorer isn't available to you and rely on the fallbacks below instead.
Don't bother trying an *untagged*, account-level Cost Explorer query as a
substitute either, unless the account is truly dedicated to this project
— confirmed live on a shared account: unrelated costs (e.g. a persistent
KMS key accruing daily, unconnected to this project) were mixed into the
account total, making it useless for isolating this project's spend.

**The reliable fallback, with or without tag access:** the in-run
`USAGE_LOG`-based estimate (`cost_estimate.py`, step 8 of
`comprehensive_test.sh` / step 7 of `comprehensive_gameday.sh`) needs no
billing/org permission at all — it reads real token counts straight from
CloudWatch Logs. Verified accurate against real metered billing: a
`comprehensive_test.sh` run estimated $0.1025 from token counts; Cost
Explorer's own (untagged, service-grouped) breakdown for that same day
showed $0.1130 for Bedrock — close enough to trust as the primary cost
signal when tag-filtered Cost Explorer isn't reachable.

**Known coverage gap:** CloudFormation stack-level tags propagate to
essentially every taggable resource in the stack automatically (no
per-resource `Tags:` block needed in the template), and the S3 artifact
bucket (created outside the stack) is tagged explicitly by `deploy.sh`.
A small number of AWS resource types have historically not supported
tags at all (e.g. `AWS::CloudWatch::Dashboard` may not) - those would
simply not carry the tags, with no error, and wouldn't show up under a
tag-filtered view even though they exist. They're also free (CloudWatch
gives the first 3 dashboards/month at no cost) or otherwise negligible,
so this doesn't affect the accuracy of tag-filtered *spend* numbers, but
"every resource visible under this tag" isn't a literal 100% guarantee.
Verify actual coverage with **Resource Groups & Tag Editor** (a free,
real-time - not 24h-delayed - way to list every resource carrying a
given tag, independent of Cost Explorer's billing-side view), or run
`./scripts/check_tags.sh`. It needs no stack to be deployed and takes no
arguments - it's an account-wide orphan/drift detector, not just a
post-deploy check: Pass 1 verifies every resource tagged `Project=...`
(whether or not a stack currently owns it) actually still exists (real
describe/get calls per resource, not just the tagging API's own say-so
- that API has a confirmed indexing lag and can list resources for
hours after they were actually deleted); Pass 2 separately sweeps
Cognito/Lambda/DynamoDB/API Gateway/IAM/CloudWatch by name for anything
matching this project's naming convention but missing its tag entirely
(catches resources a misbehaving process created without going through
`deploy.sh` at all, which tag search alone can't see). Run it any time
you want to confirm nothing's been left running - after a teardown,
after someone else touches this AWS account, or just periodically.

Once Cost Explorer is filterable (above), `./scripts/check_cost.sh
[--days N] [--run-id ID]` pulls the same tag-filtered spend via CLI
instead of the console. It needs `ce:GetCostAndUsage`, which comes from
`AWSBillingReadOnlyAccess` - one of the 10 policies required in step 1,
precisely because we must be able to verify real spend and not just
trust the in-run `USAGE_LOG` estimate. If the CLI call itself fails with
`AccessDenied`, re-check step 1: the policy was never attached, or was
detached since setup. If instead the call *succeeds* but always returns
`$0.0000` / "No cost recorded in this window for this tag filter" no
matter how long you wait, that's the tag-never-activated case above
(common on an AWS Organizations member account) - different failure,
different fix, not a policy problem.

## 2. Deploy the stack

```bash
cd ISC2_AI_Gateway
./scripts/deploy.sh
```

No separate artifact-bucket setup step needed — `deploy.sh` derives a
collision-proof default (`isc2-gateway-exercise-<your-account-id>`, since
S3 bucket names are unique account-wide across all of AWS) and creates
it automatically if it doesn't already exist. Pass `--artifact-bucket <name>`
yourself only if you want a different bucket.

Also auto-generates a run ID (date + random suffix, e.g. `20260927-a1b2c3d4`)
for the `Project_Run` cost-allocation tag (step 1c) and prints it at the
end of the run. Pass `--run-id <id>` yourself to reuse a specific run's tag
(e.g. re-running `deploy.sh` after a failure, so the retry's cost is
still attributed to the original run rather than starting a new one).

This runs `scripts/build_lambdas.sh` first (packages the orchestrator
Lambda with the `anthropic` SDK's Bedrock extra installed into it — the
only Lambda needing external dependencies), then packages and deploys
the template.
Takes a few minutes. At the end it prints the stack outputs — copy these:

- `ApiUrl` — the gateway's base URL
- `CognitoTokenEndpoint` — where load testers fetch M2M tokens
- `UserPoolId`, `GoodClientId`, `AdversarialClientId`
- `FacilitatorApiKeyId` — used internally by `trigger_agent.sh` and `end_round.sh`
- `DashboardUrl` — live scoring dashboard

Optional flags (`./scripts/deploy.sh --help` for the full list):

| Flag | Default | Purpose |
|---|---|---|
| `--stack-name <name>` | `isc2-gateway-exercise` | CloudFormation stack name |
| `--model-id <id>` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Bedrock inference-profile ID for the blue agent — verified invocable as of writing (step 1b); the newest generation (opus-5/sonnet-5) needs additional AWS-Sales-gated access on most accounts, so only switch to it after confirming it actually invokes on yours |

All scripts in `scripts/` take `--flag value` options (no env vars) —
run any of them with `--help` for its full list.

## 3. Retrieve client credentials

CloudFormation doesn't expose Cognito app client secrets as stack
outputs, so pull them directly:

```bash
./scripts/get_client_credentials.sh
```

You'll need the good and adversarial `client_id`/`client_secret` pairs
for the load testers below.

**Export these once** so every command below can just reference them —
note these are shell *variables* (`$NAME`), not `<placeholder>` angle
brackets: in bash, `<` and `>` are redirection operators, so pasting a
command with a literal `<ApiUrl>` in it fails with a confusing "no such
file or directory" error instead of doing what it looks like it should.

```bash
export API_URL="<paste ApiUrl output here>"
export TOKEN_URL="<paste CognitoTokenEndpoint output here>"
export GOOD_CLIENT_ID="<paste GoodClientId output here>"
export GOOD_CLIENT_SECRET="<paste from get_client_credentials.sh here>"
export ADVERSARIAL_CLIENT_ID="<paste AdversarialClientId output here>"
export ADVERSARIAL_CLIENT_SECRET="<paste from get_client_credentials.sh here>"
```

(Those `<...>` are a one-time exception — you're editing this line
yourself before running it, not copy-pasting it as-is.)

## 4. Generate the fixed attack script (well before event day)

Run this once, against this same stack acting as a staging target,
reviewing the output before reusing it at every table.

```bash
./scripts/generate_attack_script.sh --verify-round-seconds 60
```

`scripts/generate_attack_script.sh` resolves `--staging-url`/`--token-url`/the
adversarial client credentials from the currently-deployed stack itself (via
`stack_output` + `describe-user-pool-client`, same lookups `comprehensive_test.sh`
already does inline for its own disposable test script) - no manual credential
wiring needed. It requires a stack already deployed and does not deploy one
itself. `--verify-round-seconds <n>` is optional: when given, it runs
`attack_script_check.py` against the result immediately afterward so you see
pass/fail in the same command rather than as a separate step. Anything after a
literal `--` is forwarded straight through to `generate_attack_script.py`
(e.g. `-- --max-requests 8 --max-total-seconds 20`), and the underlying
script still refreshes its own token periodically (generation can run past
the token's 5-minute TTL) regardless of how it was invoked.

**Every generation writes a new file**,
`attack-plans/attack-plan-<run_id>.json` (`<run_id>` is the same
`YYYYMMDD-HHMMSS` convention used everywhere in this repo) - never
overwritten, and tracked in git: there are no secrets in it
(`Authorization` headers are deliberately never recorded - see
`replay_attack.py`), and it's exactly the kind of thing worth having
history for, since it's the literal record of what adversarial campaign
produced a given event's scores.

**The file itself records what produced it.** Structure is
`{"metadata": {...}, "requests": [...]}` - `metadata` carries the minimum
inputs that shaped this generation: `model`, `min_requests`,
`max_requests`, `max_total_seconds`. See `build_output()` in
`generate_attack_script.py` (unit-tested). `attack_script_check.py --describe`
prints this section before its usual request-by-request listing.
Backward-compatible: a plan generated before this field existed is just a
bare `requests` list with no wrapping object - every reader
(`replay_attack.py`'s `load_requests()`, `attack_script_check.py`'s
`load_requests()`) accepts both forms, falling back to treating a bare
list as the whole array.

Because nothing is ever overwritten,
**the default behavior is to reuse the most recent file and spend nothing**
- no AWS calls at all - unless you pass `--generate-new`:

```bash
./scripts/generate_attack_script.sh                      # reuse latest, or
                                                           # generate the
                                                           # first one if
                                                           # none exist yet
./scripts/generate_attack_script.sh --generate-new --verify-round-seconds 60
```

This replaces the old `--force-update`/exists-check guard: that guard existed
to stop an accidental re-spend against one fixed, precious file; now that
every generation has its own unique path, there's nothing to accidentally
overwrite, so the risk it guarded against is gone by construction, and the
policy question is just "generate a new one or not," which `--generate-new`
answers directly.

Every tool that replays "the" attack script without an explicit
`--attack-script` override (`run_round.sh`, `comprehensive_gameday.sh`)
resolves to this same latest file (`scripts/lib/attack_plans.py`,
unit-tested) — and records exactly which one it used in that round's own
`metadata.json` and printed output, so "which attack script produced this
score" is always answerable later without guessing from timestamps.

Reuse the exact same file for every table on event day — that's what keeps
every table facing the identical attack so scores stay comparable (the
default "reuse latest" behavior above does this automatically). Scoring
itself (see "Scoring" below) is a simple cumulative count — +1 per
adversarial request blocked, -1 per good request blocked — so a table
replayed against a longer or shorter script would have a different maximum
possible gain than one facing the real script, which isn't a fair
comparison. No stack parameter to keep in sync anymore (there used to be
one — removed once scoring stopped needing a baseline/target-rate term at
all).

Also sum the `delay_seconds` values in the file before event day. A
genuinely low-and-slow agent may pace requests far enough apart that the
total replay time exceeds the 60s round budget, in which case the
replay would still be running when the round ends. `generate_attack_script.py`
now enforces a `--max-total-seconds` pacing budget (default 45, under the
60s round) directly on the generating agent's `wait()` tool so this can't
happen by construction - but `attack_script_check.py`'s preflight (run
automatically by `comprehensive_gameday.sh`) still hard-fails before any table runs
if an older or hand-edited script violates it anyway. Either trim the
script or increase `ROUND_SECONDS`/the schedule's per-table slot to match.

**Volume: an unconstrained model sends far fewer requests than you'd
expect.** An early run with `--max-requests 10` and an unconstrained "stay
stealthy" prompt produced only 3 requests - the model has no idea it's
supposed to blend into ~1000 good requests/round, so "low-and-slow" reads as
"a handful of requests," not "a meaningful fraction of traffic." Rather than
relying on the model to infer a reasonable volume, `generate_attack_script.py`
now defaults to `--min-requests 100` / `--max-requests 150`: enough
adversarial volume that the scoring range means something (100 malicious
requests against ~1000 good requests gives a theoretical `+100`/`-1000`
range, a deliberate ~10% ratio, not an arbitrary round number). Every run
prints its resolved config - `min_requests`, `max_requests`,
`max_total_seconds`, model, scope, etc. - before spending a single Bedrock
token, so these defaults (or any override) are never a silent surprise:

```bash
./scripts/generate_attack_script.sh --generate-new --verify-round-seconds 60
```

Both are overridable at any time, including back to the original
unconstrained behavior (`--min-requests 0`):

```bash
./scripts/generate_attack_script.sh --generate-new --min-requests 20 \
  --verify-round-seconds 60 -- --max-requests 30
```

`--min-requests` is a strong instruction in the prompt (it also switches the
model away from "stealthy" pacing toward mixed bursts/probes so the requests
fit the pacing budget), not a hard guarantee - the model can still stop
short. `--verify-round-seconds`'s preflight checks `--min-requests` too (when
given) and fails if the script came in under the floor, so a shortfall is
caught immediately instead of discovered mid-event. If `--min-requests` is
ever passed larger than `--max-requests` (e.g. a disposable test run that
overrides only `--max-requests`), it's silently clamped down to match, with
a `NOTE:` printed - see `resolve_min_requests` in `generate_attack_script.py`
(unit-tested, `tests/test_generate_attack_script.py`).

**Understanding what the campaign does.** Every recorded request now
carries a `rationale` field: the model's own one-line explanation of the
tactic behind that specific request. Read the whole campaign, request by
request, with:

```bash
python3 scripts/lib/attack_script_check.py attack-plans/attack-plan-<run_id>.json --describe
```

**A real incident this surfaced: baseline alone can deny a request with no
agent action at all.** `replay_attack.py` used to fetch one OAuth token at
the start and reuse it for the whole script. Service 3's own baseline
(`BASELINE_MAX_TOKEN_AGE=30s`, a floor the agent can only tighten, never
loosen) denied a request purely because the reused token had aged past 30s
by the time that request fired — observed live as a `"Do nothing at all."`
agent still scoring `+1`. The actual fix: `replay_attack.py` now fetches a
brand-new token immediately before every request, so token age is always
~0s regardless of script pacing - this alone fully closes the exposure,
with nothing else needed.

`attack_script_check.py --token-age-margin` audits for the *old* risk
directly (fails if any request's cumulative delay comes within the margin
of its target service's baseline max-token-age) but defaults to `0`
(disabled), and is **not** run by default by `run_round.sh` or
`comprehensive_gameday.sh`'s `--round-seconds`-only preflight call. It has
to default off: once `replay_attack.py` fetches a fresh token per request,
a real, high-volume script's cumulative delay will *routinely and
harmlessly* exceed 30s+ for its later requests - that's just what more
volume looks like, not a risk. (An earlier version of this doc had this
check on by default, which immediately false-positived against a real
40-request script generated right after the min-requests-floor change -
confirmed live.) Pass `--token-age-margin` explicitly, e.g. after changing
`replay_attack.py`'s own token-fetch logic, if you want to check for a
regression back to the original bug.

## 5. Run a round

**Facilitator role, by design (confirmed, not a gap to fill later):** the
survey tool does not call `/trigger-agent` automatically, and nothing
writes scores to a spreadsheet automatically. A human facilitator reads
each table's submitted prompt and runs the command below, then records
that table's score into the scoring spreadsheet at the end of the round
— either read live off the dashboard, or (recommended — see "Scoring"
below) via `./scripts/score_round.sh`, which computes the same number
without needing to watch a graph in real time and still works after the
stack is torn down. This was a deliberate call — it adds zero new
untested code under time pressure, in exchange for a few seconds of
manual work per table across 12 rounds.

Run one complete round for one table:

```bash
./scripts/run_round.sh 3 "your defense prompt for table 3"
```

**This is the actual event-day command** — it does NOT deploy or tear
anything down (the stack stays up for the rest of the day's rounds), and
it bundles the three things a round actually needs, concurrently, for
the same `table_id`:

1. **The blue agent** — calls `/trigger-agent`, which asynchronously
   invokes the orchestrator for ~60 seconds, leaving buffer inside the
   5-minute slot (capped repo-wide to control Bedrock cost). It seeds
   baseline config, then runs check-ins every 20s. Defenses stay exactly
   as the agent left them — nothing resets them on a timer (see below
   for why that used to be a real bug). `run_round.sh` then waits a
   fixed `--agent-startup-delay-seconds` (default 10) before launching
   any traffic at all — Lambda cold start plus the first (cache-write,
   not cache-hit) Bedrock call otherwise land on live, unprotected
   requests; confirmed live, that accounted for 7 of 48 adversarial
   requests succeeding in one real round before the agent's first action
   landed. This is a fixed wait applied before anything variable-duration
   starts, not a guess about how long something else takes — nothing to
   race against.
2. **Good traffic** — the real Good Load Tester (Gatling).
3. **Adversarial traffic** — the real, fixed attack script from step 4,
   replayed byte-for-byte.

Once both generators' real exit codes are in hand, `run_round.sh` calls
`/end-round` — a synchronous signal (`scripts/end_round.sh`) that resets
the agent's defenses back to baseline right then, deterministically safe
to re-run per table without manual cleanup between rounds. This
deliberately replaced an earlier design where the orchestrator reset its
own config on an internal timer the instant its `round_seconds` elapsed:
confirmed live, that timer wasn't actually synchronized with how long the
traffic generators ran (real per-request HTTP latency on top of the
attack plan's own planned pacing routinely pushed `replay_attack.py`
seconds past the orchestrator's clock), so the tail of a real round could
go undefended regardless of the agent's strategy — the last 13 of 48
requests in one round all landed after a timer-based reset had already
fired silently. `run_round.sh` now verifies the round actually completed
cleanly (config reset to baseline, confirmed via `/end-round`'s own
response; the orchestrator really called Bedrock, not just got invoked)
and prints this round's real score.

**Every round gets its own report folder**,
`reports/round-table<N>-<run_id>/` — `metadata.json` (inputs: prompt,
round_seconds, target_rps, attack_script path), `run_summary.json`
(results: final score, per-step pass/fail, real good/adversarial request
counts), `good-traffic-report/index.html` (Gatling's own HTML report, moved
here from its default location under `load-testers/good-traffic/target/`),
`adversarial-traffic-report/index.html` (every request `replay_attack.py`
sent — status, latency, rationale, and an `attack_successful` column), and
`logs/` (this round's own `.log`/`.summary.jsonl`, for debugging this one
run in isolation). Built entirely from scripts/lib/round_report.py's pure
parsing functions (unit-tested, tests/test_round_report.py) reading
back what the round already printed — no new CloudWatch calls, so no new
lag or cost. `good_traffic` and `adversarial_traffic` deliberately share
one schema, `{total, allowed, denied, errored}` (the same vocabulary
CloudWatch's own `Outcome` dimension already uses) — `errored` is an
unrelated infra failure (e.g. a Lambda concurrency throttle surfacing as
a `500`), kept separate from `denied` (the agent actually blocking it)
so one is never mistaken for the other.

**`attack_successful` is not the same question as "was the status code
200" — and not even the same question as "was the status code 403."**
Every service Lambda's `deny()` helper (`infra/template.yaml`) returns
exactly 403 for every denial reason (`stale_token`, `rate_limited`,
`missing_scope`, `blocked_client`, `required_claim_failed`) and is the
*only* path that emits the CloudWatch `Denied` metric this whole scoring
system is built on. Any other status code — 200, or a 500 — means `deny()`
was never called for that request: it got through, regardless of what
happened afterward. Confirmed live: `round-table1-20261024-204132` scored
`+0` (nothing blocked) while one request in its log showed `-> 500` — that
was a genuinely successful attack (the request passed every defense check,
then hit an unrelated server error), not a block.

**But a second, separate incident showed that a plain 403 isn't reliably
"blocked by our defense" either.** `round-table1-20261024-224655` scored
`+0` while its adversarial report showed two `403`s, both from the model
deliberately probing a bare root path (`/service1/`, `/service2/`, no
sub-path). Checked `infra/template.yaml`: `Service1ProxyResource` uses
`PathPart: '{proxy+}'`, a greedy path variable that requires a non-empty
segment — a bare `/service1/` doesn't match that resource at all, so **API
Gateway itself rejects it with its own default `403 Missing Authentication
Token`, before the request ever reaches the authorizer or either Lambda.**
Confirmed conclusively with a direct, read-only CloudWatch query
(`get-metric-statistics` on `RequestCount`/`Outcome=Denied`/
`TrafficType=Adversarial`/`TableId=1` for that round): **zero datapoints at
all** — `deny()` was never called for anything that round, so the score was
correct.

`load-testers/adversarial-traffic/replay_attack.py` now captures each
response's body (`response_snippet`, truncated to 300 chars) and uses
`is_lambda_deny_response()` to tell the two 403 sources apart by body shape
— our own `deny()` always returns `{"approved": "false", "reason": ...}`;
API Gateway's built-in rejection returns a different shape entirely (e.g.
`{"message": "Missing Authentication Token"}`). `attack_successful(status_code,
error, response_snippet)` encodes the full picture: `True` for anything but
403, `False` only when the 403's body confirms it came from our `deny()`,
`None` for a network-level error OR a 403 whose body doesn't confirm it was
ours (can't tell blocked-vs-not from that alone — hover the badge in the
HTML report for which case applies) — see `tests/test_replay_attack.py` for
the regression tests built from both of these exact incidents.

**This routing gap itself is now closed, not just worked around.**
`Service<N>Resource` (the bare `/service1`, `/service2`, `/service3`
paths) had no method of its own for most of this project's life - only
their `{proxy+}` children did, so a bare path matched nothing at all.
Confirmed via a definitive execution-log lookup (not timing correlation
this time): the API Gateway execution log group had zero entries for
either affected request's own `x-amzn-RequestId`, meaning rejection
happened before API Gateway's own execution tracing even starts -
consistent with "no method matched," not an authorizer-level decision.
The real-world consequence: 2 of a round's 48 fixed adversarial probe
requests could never reach `deny()`/success logic at all, for the
entire time this project existed - every team replaying the same fixed
script hit this identically (deterministic, not random, so it never
actually advantaged any one team), but it was still a real, enormous
gap in test coverage: two attack vectors nobody's defense was ever
actually tested against. Fixed by adding `Service<N>RootMethod` (same
integration/authorizer as the `{proxy+}` method) on each bare resource,
plus a matching `Service<N>RootPermission` - the existing
`Service<N>Permission`'s `SourceArn` ends in `/service<N>/*`, which
requires that literal trailing slash and does NOT match the bare
method's own ARN (`.../service<N>`, no slash at all) under IAM's ARN
glob matching, so without the second permission the new route would
404/403 on an `AccessDenied` invoking the Lambda instead of actually
working.

A naming note, since this tripped someone up during rehearsal: before
`run_round.sh` existed, this script was called `start_round.sh`, and its
name read as "start the round" even though it only ever started the
*agent* — the other two pieces were always separate, manually-run
processes. Confusing enough in practice (two near-identically-named
scripts, `start_round.sh` vs. `run_round.sh`, that do very different
things) that it's been renamed to `trigger_agent.sh` — its name now says
exactly what it does, no more, no less. The live API path it calls was
renamed too (`/start-round` → `/trigger-agent`), so this is an infra
change: re-run `./scripts/deploy.sh` to pick it up, and update any
external caller (e.g. a survey-tool webhook) still pointed at the old
path. Use `trigger_agent.sh` directly only if you deliberately want to
run the three pieces apart, e.g. to debug one in isolation:

```bash
./scripts/trigger_agent.sh 3 "your defense prompt for table 3"   # agent only

# In parallel, for the same table_id:
cd load-testers/good-traffic
sbt "Gatling/testOnly simulations.GoodTrafficSimulation" \
  -Dgateway.url="$API_URL" -Dtoken.url="$TOKEN_URL" \
  -Dclient.id="$GOOD_CLIENT_ID" -Dclient.secret="$GOOD_CLIENT_SECRET" -Dtable.id=3

python3 load-testers/adversarial-traffic/replay_attack.py --script attack-plans/attack-plan-<run_id>.json \
  --gateway-url "$API_URL" --table-id 3 --token-url "$TOKEN_URL" \
  --client-id "$ADVERSARIAL_CLIENT_ID" --client-secret "$ADVERSARIAL_CLIENT_SECRET"
```

Watch the live corrected score on the `DashboardUrl` from step 2, or just
read `run_round.sh`'s own printed `Final score:` line.
Repeat for each table (6 sequential pairs, 2 tables running concurrently
per 5-minute slot, isolated by `table_id`).

To rehearse the entire roster ahead of time instead of running each table
by hand like this, see **Gameday rehearsal run** below — it drives
`run_round.sh` for every table in a roster CSV, one after another, with
full logging (plus its own deploy/teardown, which `run_round.sh` itself
deliberately does not do).

## 6. Teardown

```bash
./scripts/teardown.sh
```

Deletes the CloudFormation stack, this IAM user's access key (not the
user itself), and the artifact S3 bucket — everything created for the
event, unconditionally, with no confirmation prompts. The access key
deletion is treated as the priority: it runs before the bucket cleanup
and is the one step that aborts the script on failure (see the comment
in `teardown.sh`), since a live key is the one thing here worth being
strict about; a bucket-delete failure only warns. Do this the same
day as the event — see cost notes below.

**The stack delete itself is asynchronous** — `teardown.sh` *initiates*
deletion (its own output says so: "Delete initiated... watch progress
with describe-stacks") and returns immediately; it does not wait for
the stack to actually finish disappearing, which can take a few
minutes for this many resources. If you run `./scripts/check_tags.sh`
right after, don't be alarmed if it still shows everything as `LIVE` —
that's normal, in-progress deletion, not a bug. Either wait a few
minutes first, or run `aws cloudformation wait stack-delete-complete
--stack-name isc2-gateway-exercise` yourself to block until it's
actually done before checking.

**Pausing overnight instead of tearing down for good:** if you want to
stop paying for the billable resources (the stack, and therefore
Bedrock/CloudWatch exposure) without redoing the IAM console setup
(group/user/access-key creation) the next day, keep the access key:

```bash
./scripts/teardown.sh --keep-access-key
```

This still deletes the stack and the artifact bucket (both cheaply
recreated by `deploy.sh` next time), just skips the access-key deletion.
Delete the key for real (plain `./scripts/teardown.sh`, no flag) once
you're actually done with the event, not just done for the night.

## Comprehensive test run

`./scripts/comprehensive_test.sh` exercises the entire system in one
command: deploy → smoke test → generate a small test attack script →
one short test round (synthetic good traffic + the adversarial replay,
concurrently) → verify the blue agent actually reset config afterward →
orchestrator health check → **score the round + print the ranking** →
tag check → **cost check** → teardown → **cost check again** → tag
check again. Use this instead of manually working through steps 2–6
when you want to validate a change to the template/Lambda code, or just
confirm the whole pipeline still works end to end.

```bash
./scripts/comprehensive_test.sh                                   # defaults: 60s round, 5-request attack script
./scripts/comprehensive_test.sh --round-seconds 60 --max-attack-requests 10
./scripts/comprehensive_test.sh --skip-teardown                   # debugging only - see the warning it prints
```

**This is a test run, not a real event round** — the round length matches
the real event (60s, capped repo-wide to control Bedrock cost), but the
attack-script budget is much smaller (5 requests vs. the real 100-150), to
keep repeated review/test runs fast and cheap. It writes its own test attack
script under `logs/`, never touching anything in `attack-plans/`. It also **keeps the IAM access
key by default** (passes `--keep-access-key` to `teardown.sh`), for the
same reason - you'll run this many times while iterating, and deleting
the key every run would mean redoing the console-only key-creation step
every run too. Pass `--delete-access-key` once you're actually done
testing for good.

**Every run produces two files under `logs/`:**
- `run-<id>.log` — the complete output of every step, in order, with
  timestamps and PASS/FAIL markers - written so that you (or an AI
  assistant in a future session, with no other context) can diagnose a
  failure from the log file alone, without re-running anything.
- `run-<id>.summary.jsonl` — one JSON object per step
  (`step`/`status`/`exit_code`/`duration_s`/timestamps), for quickly
  answering "what failed and how long did each step take" without
  parsing the full log.

**Safety:** a trap runs `teardown.sh` on every exit path — success,
a failed step, or Ctrl-C — so a failure partway through doesn't leave a
billable stack running untracked. The only way to skip this is the
explicit `--skip-teardown` flag, which prints a loud warning and
requires you to tear down manually afterward.

Needs `pip install -r load-testers/adversarial-traffic/requirements.txt` once (see Prerequisites)
— it calls `generate_attack_script.py` and `replay_attack.py` directly,
which need `anthropic`/`requests` importable locally, separately from
those packages being bundled into the Lambda zips.

**Real cost estimate, no 24h wait:** `check_cost.sh` (Cost Explorer)
can't tell you what a run just cost — billing data lags ~24h regardless
of whether resources are deleted, so it's still there for the
record, but it won't show anything useful minutes after a run.
Instead, both `handler.py` and `generate_attack_script.py` log a
`USAGE_LOG {...}` line (real token counts, no lag) after every Bedrock
call; this script fetches the orchestrator's from CloudWatch Logs,
combines them with the generation step's own, and prints an estimated
dollar total using Sonnet 4.5's list pricing, both mid-run (step 7c) and
restated as the last line of the final summary. It's an estimate (list
pricing, not your actual negotiated/metered bill) — cross-check against
`check_cost.sh` tomorrow if you want the real settled number — but it's
immediate, and at this test's traffic volume Bedrock is essentially the
entire cost (everything else stays under a cent).

## Gameday rehearsal run

`./scripts/comprehensive_gameday.sh --roster <csv>` is the dry-run equivalent of
working through steps 2–6 by hand for an entire roster of tables, instead
of `comprehensive_test.sh`'s single short synthetic round. Use this the
day (or days) before the real event to see the real timing and real cost
across the whole roster, with no scaled-down shortcuts:

- Deploys the stack once, then runs **every row in the roster CSV
  sequentially** (one table at a time, not concurrently) — each with the
  **real** round length (60s, capped repo-wide to control Bedrock cost),
  the **real** attack script from step 4 (the latest in `attack-plans/`
  by default, not a disposable small one), and the
  **real** Gatling `GoodTrafficSimulation`, concurrently with that
  table's live round, same as a real event round.
- Verifies config reset after each table before moving to the next, same
  polling approach as `comprehensive_test.sh`, plus an orchestrator
  health check and a real score (see "Scoring" below) for every table -
  ending in a final ranking, sorted best-cumulative-score-first, before
  teardown.
- Tears down once at the very end (not per table) — same `on_exit` trap
  safety net: Ctrl-C or a failure anywhere still tears down, unless you
  pass `--skip-teardown`.
- A single table's failure (round wouldn't start, replay failed, config
  didn't reset in time) is logged as `FAIL` and does **not** abort the
  rest of the roster — each `table_id` is an independent DynamoDB config
  row, so one table's trouble can't bleed into the next table's round.
  The script's own exit code is still non-zero if anything failed, so a
  facilitator (or CI) sees it at a glance without reading the full log.

Roster CSV format (header required) — see `scripts/examples/sample_roster.csv`:

```csv
table_id,prompt
1,"Watch for clients whose request pattern looks scripted or unusually regular; leave everything else alone."
2,"Be conservative - only block a client once you have strong evidence."
```

```bash
./scripts/comprehensive_gameday.sh --roster scripts/examples/sample_roster.csv
./scripts/comprehensive_gameday.sh --roster roster.csv --attack-script attack-plans/attack-plan-20261024-130626.json
./scripts/comprehensive_gameday.sh --roster roster.csv --delete-access-key   # truly done rehearsing
```

Requires a real attack script from step 4 to already exist in
`attack-plans/` (fails fast with a clear message if not — it does not
generate one for you, unlike `comprehensive_test.sh`, which writes its own
disposable test-sized script); defaults to the most recently generated one,
same resolution `run_round.sh` uses (`scripts/lib/attack_plans.py`), or
pass `--attack-script` for a specific one. Also requires `sbt`/a JDK on
`PATH`, since it drives the real Gatling simulation rather than a bash
`curl` loop.

**Logs, same convention as `comprehensive_test.sh`, under a `gameday-`
prefix instead of `run-`:**
- `logs/gameday-<id>.log` — full output, every table, in order.
- `logs/gameday-<id>.summary.jsonl` — one JSON object per step, with a
  single `round_table3`-style entry per table (that table's whole
  `run_round.sh` call) — see
  `reports/round-table3-<id>/logs/round-table3-<id>.summary.jsonl` for
  that table's own finer-grained breakdown (`trigger_agent`,
  `replay_attack`, `good_traffic`, `verify_config_reset`, ...), written
  by `run_round.sh` itself — and that same `reports/round-table3-<id>/`
  folder for its Gatling/adversarial HTML reports and metadata/summary
  JSON (see "Run a round" above).
- `logs/gameday-<id>.roster.jsonl` — the parsed roster, for a quick sanity
  check that every row was read correctly before the run started.

**Cost estimate is broken out per table, not just a single total** —
it groups every `USAGE_LOG` line (from CloudWatch Logs, across the whole
run's time window) by the `table_id` field each entry already carries, so
you can see whether any one table's round ran unusually long/expensive
before the real event, not just the roster's combined total.

## Test harness architecture

`comprehensive_test.sh`, `comprehensive_gameday.sh`, and `run_round.sh`
all share their actual run_step/logging/cost-estimate machinery rather
than each carrying its own copy — this was a real bug source twice over:
the first two scripts used to duplicate ~150 lines of bash and Python
almost verbatim (a fix made to one never propagated to the other), and
`comprehensive_gameday.sh`'s per-table loop later duplicated that same
start/verify/score sequence a second time before being extracted into
`run_round.sh` — which `comprehensive_gameday.sh` now calls once per
table instead of running that sequence inline. All three source:

- **`scripts/lib.sh`** — the project-wide helpers (`stack_output`,
  `require_permissions`, `ensure_artifact_bucket`, ...), used by every
  script in `scripts/`, not just the two test runners.
- **`scripts/test_harness_lib.sh`** — the test-runner-specific bash
  functions: `log_section`, `run_step`, `record_bg_result`,
  `run_with_timeout`, `maybe_teardown`, `on_exit` (install with
  `trap on_exit EXIT` — not installed automatically by sourcing, to keep
  the library free of side effects), plus thin wrappers
  (`verify_config_reset`, `fetch_orchestrator_usage_logs`,
  `cost_estimate`, `check_orchestrator_health`, `score_table`,
  `print_rankings`) around the Python modules below. Each function's
  header comment documents exactly which global
  variables it expects the caller to have already set (`SUMMARY_FILE`,
  `ANY_STEP_FAILED`, `KEEP_ACCESS_KEY`, etc.) — nothing is set implicitly
  by sourcing the file.

**The actual logic — cost math, config-reset polling, orchestrator log
health, roster/attack-script parsing — lives in real, importable,
unit-tested Python modules**, not inline bash heredocs (the original
design, and the reason none of it had test coverage):

| Module | Pure function(s) | What it replaced |
|---|---|---|
| `scripts/lib/cost_estimate.py` | `cost_of`, `parse_usage_log` | inline heredoc duplicated in both scripts |
| `scripts/lib/orchestrator_health.py` | `is_healthy` | — (new; see "Known simplifications" for why this exists) |
| `scripts/lib/verify_config_reset.py` | `is_baseline` | inline heredoc duplicated in both scripts |
| `scripts/lib/orchestrator_usage.py` | — (thin AWS CLI wrapper) | inline heredoc duplicated in both scripts |
| `scripts/lib/roster.py` | `parse_roster`, `find_duplicate_table_ids` | inline Python block in `comprehensive_gameday.sh` |
| `scripts/lib/attack_script_check.py` | `summarize` | inline Python block in `comprehensive_gameday.sh` |
| `scripts/lib/scoring.py` | `cumulative_score`, `build_minute_grid`, `build_running_score_series` | — (new; see "Scoring" below) |
| `scripts/lib/ranking.py` | `parse_score_log`, `rank` | — (new; see "Scoring" below) |

Each module is independently runnable from the command line (used
directly by the bash wrappers above) **and** importable, so its pure
logic can be unit-tested with zero mocking and no AWS credentials. The
"pure function" column is deliberate: each of those takes plain data in
and returns plain data out (a list of log events, a DynamoDB-shaped
dict, parsed CSV text) — the only parts of each module that actually
touch the network/filesystem (`fetch_events`, `get_config`, `main`) are
thin wrappers around that logic, kept separate so the decision logic
itself needs no mocking to test.

**`tests/`** holds a seed suite (stdlib `unittest`, zero new
dependencies — run with `python3 -m unittest discover -s tests` or
`python3 -m pytest tests` if you have pytest installed) exercising every
pure function above, including a regression test
(`tests/test_orchestrator_health.py::test_regression_adaptive_thinking_crash`)
built directly from the real CloudWatch Logs output of the bug that
motivated this whole refactor. Extend this suite as you touch any of
`scripts/lib/*.py` — that's the point of having pulled the logic out of
bash heredocs in the first place.

## Scoring

Step 5's facilitator workflow used to say "read that table's peak score
off the dashboard into the scoring spreadsheet by hand" — fine for one
table watched live, unworkable for ranking many, and impossible once the
stack (and its dashboard) is torn down. `scripts/lib/scoring.py` computes
it as a standalone, scriptable tool instead.

**The formula is exactly docs/proposal.md's original design, with nothing
added:**

```
Score = (number of adversarial requests blocked this round)
      − (number of good requests blocked this round)
```

A cumulative running total — +1 the instant an adversarial request is
blocked, −1 the instant a good request is blocked, nothing else. No
baseline, no target rate, no round-length term. A no-op defense that
blocks nothing scores exactly `0`; a defense that blocks all traffic
(good and bad alike) scores deeply negative — a no-op beats
"shut everything down," as the exercise intends.

This *is* docs/proposal.md's `Score = (N_malicious_baseline −
N_malicious_attempt) − (N_good_baseline − N_good_attempt)`, just reduced
algebraically: `N_x_baseline` is how many X requests were sent and
`N_x_attempt` is how many got through (Allowed), so `N_baseline −
N_attempt` is simply `N_denied`. No external baseline/rate constant was
ever needed — a first implementation (`infra/template.yaml`'s
`ScoringDashboard` widget) introduced a per-minute rate-vs-target-baseline
version instead, which turned out to be dimensionally incoherent
(comparing a whole-round constant against one minute's count) — confirmed
by deploying it and watching every minute of a real table render an
identical, clearly-impossible score. Fixed to this simpler, directly
correct form, both in `scripts/lib/scoring.py` and in the dashboard
widget itself (`RUNNING_SUM(adversarial_denied) − RUNNING_SUM(good_denied)`,
CloudWatch metric math's native cumulative-sum function — the two should
always agree; if you ever see them diverge, trust this simpler form and
go looking for the bug in the widget, not the other way around).

**Facilitator tool, usable live or after teardown — no stack lookup
needed at all anymore, since there's no baseline to fetch:**

```bash
# No args: auto-discovers every table_id with recent CloudWatch activity
# (default lookback: 3h, see --lookback-hours) and scores all of them,
# printing a final ranking - "just score everything from the last round."
./scripts/score_round.sh

# Or score one specific table/window:
./scripts/score_round.sh <table_id> <start_time_ms> <end_time_ms>
```

If you ran the round with `run_round.sh`, you already have this number -
it calls this same scoring step itself and prints `Final score:` at the
end, no separate `score_round.sh` call needed. This section matters when
you want to re-score after the fact, or scored something started with
bare `trigger_agent.sh`.

CloudWatch metric data outlives the resources that published it, so this
works well after `teardown.sh` has already run (for single-table mode,
get `<start_time_ms>`/`<end_time_ms>` from the round's own log file, or
from when you ran `trigger_agent.sh`/`run_round.sh` — or just use no-args
discovery mode, which finds each table's own activity window itself).

**Discovery mode has no way to tell a real competitive round apart from
an auxiliary one** (e.g. `comprehensive_gameday.sh`'s own orchestrator smoke-test
table, or `comprehensive_test.sh`'s disposable test table) beyond "had
CloudWatch activity in the lookback window" — on a real event day, where
every table_id is just `"1"`–`"12"`, this isn't an issue, but during
development/testing expect smoke-test table_ids to show up in the
ranking alongside the tables you actually care about.

**Both `comprehensive_test.sh` and `comprehensive_gameday.sh` score every table
they run automatically**, and print a final ranking (sorted best-
cumulative-score-first) before teardown — look for the `SCORE_LOG` lines
in any run's log file (same greppable convention as `USAGE_LOG`), or the
"Final ranking" section near the end.

**Two real bugs this surfaced, worth knowing if you ever touch this
code:**
- The per-minute rate-based formula described above (found by deploying
  it and reading real output, not by any linter or test).
- The AWS CLI returns CloudWatch timestamps as `"...+00:00"`, not
  `"...Z"` — the same instant, but a different string. An early version
  of `scoring.py` built its per-minute lookup table keyed on the `"...Z"`
  form it generated itself, so every CloudWatch-returned datapoint
  silently missed the lookup and defaulted to `0`. Caught by testing
  against a real deployed table's real data, not by the unit tests
  (which used self-consistent mocked data and couldn't have caught a
  mismatch against AWS's actual format) — now fixed and covered by a
  regression test, but a reminder that pure-function unit tests verify
  internal consistency, not real-world API contract.

## Log/artifact retention

Every CloudWatch Log Group this stack creates (one per Lambda, plus API
Gateway's own execution logs) sets `RetentionInDays: 1` — the shortest
native CloudWatch Logs supports; there's no sub-day retention setting in
the API at all. The S3 artifact bucket `deploy.sh` stages Lambda
deployment zips in gets a matching 1-day expiration lifecycle rule
(`ensure_artifact_bucket` in `scripts/lib.sh`, re-applied on every
deploy so a bucket created before this rule existed still gets it).
Consistent across every log/artifact destination in this project, not
just the ones that happened to need debugging - confirmed live: before
this, log groups defaulted to 14 days (explicit ones) or "never expire"
(any Lambda missing from the explicit list, e.g. `EndRoundFunction` was
found missing this way).

## Cost estimate

For one event day (12 rounds, 60s each, torn down same day), rough
order of magnitude:

| Component | Est. cost |
|---|---|
| Amazon Bedrock — blue agent (12 rounds) | lower than the figures below, which were measured at the old 270s round length - see note |
| Amazon Bedrock — red-agent script generation (one-time) | $1–3 |
| API Gateway, Lambda, DynamoDB, Cognito | <$2 combined |
| CloudWatch dashboard | $0 (first 3/month free) |
| CloudWatch custom metrics | the one wildcard — see below |

**The blue-agent estimate is a derived guess, not a measurement, and the
dollar figures quoted below this point in the doc predate the 270s→60s
round-length change** — the only way to know the real current number is
to run one live round and check Cost Explorer, filtered to `Project_Run`
(step 1c), or read `comprehensive_test.sh`'s own cost-estimate step,
which measures real token counts at whatever `--round-seconds` you pass
it. Working through the old math for context (at 270s): a round ran
~270s with check-ins every 20s → ~14 turns, each making 1+
`messages.create` call (up to 8 tool-roundtrips/turn, though a
monitoring loop that checks stats and holds/adjusts typically uses far
fewer) → roughly ~28 API calls/round. At the new 60s round length with
the same 20s check-in cadence, that's only ~3 turns and well under 10
API calls/round - proportionally cheaper, though not exactly linear
since each turn still re-sends the full growing conversation. Claude Sonnet 4.5 is $3/M input
tokens vs. **$15/M output tokens — a 5x asymmetry**, so input-token
volume (not output length) is the dominant lever. Without caching, every
call resent the whole growing conversation at full price, making input
tokens grow faster than linearly with call count (~$5–15/12 rounds,
this table's number before the fix below). `handler.py` now sets a
`cache_control` breakpoint on the system+tools block and on the end of
the growing message history every call (see the code comments) — cached
reads are billed far below the $3/M input rate, so this should cut the
dominant part of that cost meaningfully, likely into the $2–8 range
shown above, but **this has not been verified against a real invoke** -
confirm with a live round before trusting either number.

CloudWatch custom metrics (only `RequestCount`, dimensioned by
`TableId`/`TrafficType`/`Outcome` — roughly 12 × 2 × 2 ≈ 48 distinct
combinations across 12 tables; `get_traffic_stats` moved to DynamoDB
during review and no longer publishes its own metric) are the other
line item where current per-metric proration matters — check the
CloudWatch pricing page for your region before the event, and tear down
promptly regardless, since that bounds exposure either way.

Further cost levers, in addition to caching (now implemented): switch
`ModelId` to a Haiku inference profile for lower per-token cost (verify
it invokes on your account first, per step 1b), or reduce check-in
frequency (`CHECK_IN_SECONDS` in `handler.py`) — that's a gameplay
tradeoff (a slower-reacting agent), not a pure efficiency win, so treat
it as a deliberate design decision, not a default to change casually.
Best practical step either way: run one round yourself before the event
and check actual billed usage (AWS Cost Explorer, filtered to Bedrock
and/or `Project_Run`) to calibrate against reality rather than any
estimate here.

## Known simplifications — verify before the event

- **Fixed live, not hypothetical:** the original `ScoringDashboard`
  metric widgets had no `"region"` property. CloudWatch now hard-rejects
  that (`CREATE_FAILED`: "Should have required property 'region'" /
  "The metric widget should have specified a region and a data source or
  an alarm annotation") — confirmed by an actual failed `CREATE_STACK`.
  Every metric widget's `properties` now includes `"region":"${AWS::Region}"`.
  If you ever regenerate the dashboard block by hand, don't drop this.
- **The dashboard assumes `table_id` values `"1"`–`"12"`.** Every widget
  enumerates these explicitly (see the comment above `ScoringDashboard`
  in `infra/template.yaml` for why this beats a dynamic `SEARCH()`
  expression here). If you run tables under different labels, regenerate
  that block rather than hand-editing 60+ metric entries.
- **Cognito `AccessTokenValidity`** is set to 5 minutes, not the original
  proposal's "1 minute" — confirm your account/region's actual minimum.
  The freshness policy that matters for the exercise
  (`max_token_age_seconds`) is enforced independently in DynamoDB/Lambda,
  not by this Cognito setting, so this doesn't block the core mechanic.
- **`required_claim`** can only check claims an M2M token actually
  carries (`client_id`, `scope`, `iat`, etc.) — no user profile exists to
  attach custom attributes to.
- **Rate limiting** is per-second DynamoDB buckets, not a true sliding
  window — adequate for a 5-minute demo round, not production-grade.
- **Gatling/sbt versions** pinned in `build.sbt`/`project/plugins.sbt` —
  bump if `sbt gatling:test` can't resolve them.
- **Prompt caching in `handler.py` is implemented but not verified against
  a live invoke.** `_with_cache_breakpoint` converts the last message's
  content blocks to plain dicts via `.model_dump(exclude_none=True)`
  before adding the cache marker - for an assistant turn that includes
  an extended-thinking block, this needs to faithfully preserve every
  field the API expects back unchanged (thinking blocks can carry a
  signature the API validates on replay). This should work - it's the
  standard SDK round-trip pattern - but hasn't been proven live. Run one
  real round before the event and confirm both (a) no errors from the
  orchestrator's CloudWatch logs and (b) `cache_read_input_tokens` /
  `cache_creation_input_tokens` in the response usage are actually
  nonzero after the first couple of turns - if they stay at zero,
  caching silently isn't engaging and the cost estimate reverts to the
  uncached one.

## Troubleshooting

- **`aws cloudformation validate-template --template-body file://...`
  fails with a `ValidationError` that echoes the whole template back as
  a "Value"** — the template (52KB+) exceeds the 51,200-byte inline
  `TemplateBody` limit that `validate-template`/`create-stack` enforce.
  This does NOT affect `deploy.sh`: `aws cloudformation deploy` with
  `--s3-bucket` auto-uploads an oversized template to S3 instead of
  inlining it, which is what `deploy.sh` already passes. If you want to
  validate a change without a full deploy, upload the template to S3
  first and validate via `--template-url` instead of `--template-body`.
- **Every service call returns a bare `{"message":"Unauthorized"}` (401),
  even with a freshly-fetched, perfectly valid token** — this was a real
  bug, found and fixed: API Gateway's native `COGNITO_USER_POOLS`
  authorizer does not support Cognito client_credentials (M2M) access
  tokens at all, confirmed via `aws apigateway test-invoke-authorizer`
  against a token that decoded as entirely correct (right issuer, right
  scopes, `token_use: access`, unexpired) and was still rejected. Fixed
  by replacing it with a custom TOKEN authorizer
  (`functions/jwt_authorizer/`) that verifies the JWT's RS256 signature
  against the User Pool's JWKS directly via PyJWT - verified locally
  against a real token (accepts valid, rejects tampered) before being
  wired into the template. If you ever see this error again after
  further changes, re-run that same local PyJWT check before assuming
  it's a code bug elsewhere - it isolates the auth layer from everything
  downstream of it.
- **A handful of adversarial requests come back `500` with body
  `{"message":null}`, looking like a defense gap (an attack "succeeding"
  despite the agent having already blocked everything)** — this is NOT
  a defense/scoring bug. Confirmed live: neither the service Lambda's nor
  the custom JWT authorizer's own CloudWatch Logs showed any error for
  those requests at all — because the invocation never started. This AWS
  account's Lambda concurrent-executions quota was **10**, not the
  1,000 documented default (`aws lambda get-account-settings` — new
  accounts start reduced and AWS raises it automatically with usage,
  per AWS's own Lambda quotas docs). Combined `ConcurrentExecutions`
  across this stack's 6 Lambda functions (3 services + authorizer +
  orchestrator + trigger-agent) peaked at ~20 under `--target-rps 20` —
  double the cap — causing real Lambda throttles that API Gateway
  surfaces as a generic `500` to the client, indistinguishable from a
  code bug unless you check `ConcurrentExecutions`/`Throttles` metrics
  directly. Fixed by lowering the default `--target-rps` to 8 in
  `run_round.sh`/`comprehensive_gameday.sh`. Request a quota increase via
  Service Quotas (`L-B99A9384`, free, account-level) if you want to run
  at higher traffic volume — it's not something this repo can work
  around in code, only avoid triggering.
- **`end_round.sh` (or any newly-added API route) returns 403 with body
  `{"message":"Missing Authentication Token"}`, even though the route
  is right there in `infra/template.yaml` and the resource genuinely
  exists** — confirmed live via `aws apigateway get-resources` (the
  route existed) vs. `aws apigateway get-deployment --embed apisummary`
  on the `prod` stage's actual `deploymentId` (the route was absent from
  what's actually serving traffic). `AWS::ApiGateway::Deployment`'s own
  `Properties` (just `RestApiId`) never changed across that stack
  update, so CloudFormation updated the REST API's resource tree in
  place but never published a new deployment to the stage — listing the
  new method in `GatewayDeployment`'s `DependsOn` only orders
  operations, it does not force a replacement. A first attempt giving
  `GatewayDeployment` a `Description` tied to a changing parameter
  *also* failed — confirmed via CloudFormation events showing
  `GatewayDeployment`/`GatewayStage` go `UPDATE_IN_PROGRESS` ->
  `UPDATE_COMPLETE` with no create/delete of a new physical deployment,
  meaning the update only touched the `Description` text in place
  without re-capturing the API's current routes. The actual fix:
  `deploy.sh` now runs `aws apigateway create-deployment --rest-api-id
  ... --stage-name prod` explicitly, right after every
  `cloudformation deploy` — this bypasses that resource's unreliable
  update semantics entirely and always re-points the stage at a fresh
  snapshot of whatever routes exist right now. If you ever add another
  API route and it 403s with this exact body right after a deploy, check
  `get-deployment --embed apisummary` first before assuming it's an auth
  or IAM problem.
- **A handful of adversarial requests 403 with body
  `{"message":"Invalid key=value pair (missing equal-sign) in
  Authorization header (hashed with SHA-256...)..."}`** — still under
  investigation as of this writing; not yet root-caused. This is AWS's
  own SigV4-signature-parsing error text, not anything our Lambdas or
  custom authorizer produce, so something is treating these specific
  requests as needing `AWS_IAM` auth instead of the `CUSTOM` authorizer
  every method actually uses. The original "transient post-deploy
  edge-node propagation lag" theory looks weaker now: across two
  separate rounds replaying the same fixed attack script, the anomaly
  landed at nearly identical `t_offset_seconds` both times (~9-10s and
  ~34-35s into the script) - consistent with something tied to the
  script's own fixed request sequence/count, not proximity to a deploy.
  Needs a clean re-test with execution logging actually delivering data
  (see next bullet) before concluding either way.
- **Execution logging was enabled (`LoggingLevel: INFO` on
  `GatewayStage`, confirmed live via `aws apigateway get-stage`) but
  silently delivered ZERO log data at all, for every request** - this
  was a real, separate bug, not a logging-verbosity question. Root
  cause: `aws apigateway get-account`'s `cloudwatchRoleArn` pointed at
  an IAM role (`APIGateway_Cloudwatch_Role`) that no longer existed
  (confirmed: `aws iam list-attached-role-policies` on it returned
  `NoSuchEntity`) - API Gateway had no valid role to assume to write
  logs anywhere, account-wide, regardless of any per-stage setting.
  Fixed by adding `ApiGatewayCloudWatchRole` (an `AWS::IAM::Role` with
  the AWS-managed `AmazonAPIGatewayPushToCloudWatchLogs` policy) and
  `ApiGatewayAccount` (`AWS::ApiGateway::Account`, pointing
  `CloudWatchRoleArn` at it) to the template, both created via
  CloudFormation rather than a direct `aws iam create-role` call, since
  IAM role creation through this template is already proven to work in
  this restricted demo account. If execution/access logging on ANY API
  Gateway stage ever looks configured-but-empty again, check
  `aws apigateway get-account` before assuming the stage config is
  wrong - this is an account-wide, not per-stage, setting.
- **Gatling's own `good-traffic-report/index.html` shows 100% "OK"
  even in a round where the agent genuinely denied every request
  (status 403 the whole time, confirmed by both `final_score` and
  `run_summary.json`'s own `good_traffic.denied` count)** - a real
  regression introduced earlier this session, not a new mystery.
  Gatling applies a default success check (status in 200-209 or 304)
  to every request UNLESS you add your own explicit `.check(...)` -
  adding one REPLACES the default rather than adding to it. The
  `.check(status.saveAs(...), bodyString.optional.saveAs(...))` added
  to capture data for `GOOD_REQUEST_RESULT` did exactly that, so
  Gatling's own report stopped reflecting real status codes at all -
  purely cosmetic (scoring reads CloudWatch directly, and
  `run_summary.json`'s `good_traffic` classifies by response body
  content, independent of Gatling's internal pass/fail), but
  `good-traffic-report/index.html` itself became meaningless the
  moment those checks were added. Fixed by re-adding
  `status.in((200 to 209).toList :+ 304: _*)` explicitly alongside the
  other two checks.
- **A `HEAD` adversarial request (e.g. `HEAD /service2/probe`)
  occasionally lands in `adversarial_traffic.errored` even though the
  request reached our Lambda and got a fully correct answer** - not a
  backend defect, confirmed two separate ways: (1) a live reproduction's
  `Content-Length` matched byte-for-byte what our deny()/success JSON
  would produce, proving the Lambda processed it correctly; (2)
  filtering the API Gateway execution log group by that exact request's
  own `x-amzn-requestid` returned nothing even for that clean 200 -
  CloudWatch execution logging simply never traces `HEAD` requests at
  all, success or failure (a platform limitation, not fixable here).
  The real cause `replay_attack.py`'s classification can control: HTTP
  `HEAD` responses never carry a body on the wire (RFC 7231 -
  `Content-Length` mirrors what `GET` would have returned, but zero
  actual bytes are sent), so `is_lambda_deny_response()` can never
  confirm anything from an empty `response_snippet`, regardless of the
  real outcome. `attack_successful()` now takes a `method` parameter
  and trusts `status_code` alone for `HEAD` specifically (403 = denied,
  anything else = allowed) instead of requiring body confirmation -
  safe because the other known false-403 sources (a bare path that
  never reaches `{proxy+}`, or the SigV4-parsing edge case) only occur
  on paths that don't match `{proxy+}` at all, and a `HEAD` request to a
  real sub-path does. Scoring itself was never affected either way -
  `_emit('RequestCount', ...)` runs inside `deny()`/success regardless
  of HTTP method, so the real metric behind `final_score` was always
  correct; this only fixes what the *report* could confirm.
- **`trigger_agent.sh` returns 403** (including inside `run_round.sh`,
  which calls it) — the facilitator API key wasn't attached correctly;
  re-check `FacilitatorApiKeyId` in the stack outputs.
- **Service calls return 403 `missing_scope`** — the load tester's
  Cognito app client isn't requesting all three scopes; check the
  `scope` parameter sent to the token endpoint.
- **Orchestrator times out mid-round** — check CloudWatch Logs for the
  orchestrator Lambda; a single check-in's `messages.create` call taking
  too long against the 300s Lambda timeout is the most likely cause —
  consider lowering `MAX_TOKENS` in `handler.py` or switching `ModelId`.
- **Orchestrator fails immediately with an access or validation error
  calling Bedrock** — almost always either the model ID being wrong for
  the deploy region, or (on a brand-new AWS account) Anthropic's
  one-time "use case details" gate not yet cleared for this account
  (step 1b); `check_permissions.sh` confirms IAM permissions but can't
  confirm that gate is cleared, only `require_bedrock_model_access` in
  `deploy.sh` (a real invoke) catches that, and only at deploy time.
- **Score not moving on the dashboard** — confirm `X-Table-Id` is being
  sent identically by both load testers and matches what you passed to
  `trigger_agent.sh` (metrics and config are both keyed by this value).
