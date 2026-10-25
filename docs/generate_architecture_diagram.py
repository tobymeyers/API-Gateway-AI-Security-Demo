#!/usr/bin/env python3
"""Generates docs/architecture.png - an AWS-style architecture diagram of the
ISC2 gateway exercise, from the actual resources in infra/template.yaml and
the load-testers/adversarial-traffic/ scripts. Regenerate after any architectural change:

    python3 docs/generate_architecture_diagram.py

Requires: pip install diagrams, and the Graphviz `dot` binary on PATH
(macOS: brew install graphviz).
"""
import os

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.general import Client, Disk, SDK, User
from diagrams.aws.ml import Bedrock
from diagrams.aws.management import Cloudwatch
from diagrams.aws.network import APIGateway
from diagrams.aws.security import Cognito

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

GOOD = "#2E5C99"
ADVERSARIAL = "#A85226"
INTERNAL = "#5B6068"

graph_attr = {
    "fontsize": "22",
    "fontname": "Helvetica",
    "bgcolor": "white",
    "pad": "0.5",
    "splines": "spline",
    "nodesep": "0.7",
    "ranksep": "1.0",
}
node_attr = {"fontname": "Helvetica", "fontsize": "12"}
edge_attr = {"fontname": "Helvetica", "fontsize": "10"}
cloud_graph_attr = {"margin": "40", "pad": "0.5"}

with Diagram(
    "ISC2 Gateway Exercise — Architecture",
    filename=os.path.join(OUT_DIR, "architecture"),
    outformat="png",
    direction="LR",
    show=False,
    graph_attr=graph_attr,
    node_attr=node_attr,
    edge_attr=edge_attr,
):

    with Cluster("Offline, once — before the event"):
        red_agent = SDK("Red Agent\n(Claude Tool Runner,\nstaging target)")
        attack_script = Disk("attack_script.json\n(fixed, recorded)")
        red_agent >> Edge(label="records requests\n+ real timing", color=ADVERSARIAL) >> attack_script

    facilitator = User("Facilitator")
    good_tester = Client("Good Load Tester\n(Gatling)")
    adversarial_tester = Client("Adversarial Load Tester\n(replay_attack.py)")
    attack_script >> Edge(label="replayed identically\nper table", color=ADVERSARIAL, style="dashed") >> adversarial_tester

    with Cluster("AWS Cloud — one shared stack, per-table isolation via table_id"):

        cognito = Cognito("Cognito User Pool\n(2 M2M app clients:\ngood / adversarial)")

        apigw = APIGateway("API Gateway\n(REST, custom TOKEN authorizer)")
        jwt_authorizer_fn = Lambda("JwtAuthorizerFunction\n(verifies M2M JWTs\nvia PyJWT + JWKS)")

        trigger_agent_fn = Lambda("TriggerAgentFunction")
        orchestrator_fn = Lambda("OrchestratorFunction\n(blue agent)")
        bedrock = Bedrock("Amazon Bedrock\n(Claude - IAM auth,\nno API key)")

        with Cluster("Backend services\n(limited / moderate / extreme)"):
            svc1 = Lambda("Service 1")
            svc2 = Lambda("Service 2")
            svc3 = Lambda("Service 3")
            services = [svc1, svc2, svc3]

        state_table = Dynamodb("StateTable\n(config, rate counters,\nclient stats)")
        dashboard = Cloudwatch("CloudWatch Dashboard\n(per-table score)")

        facilitator >> Edge(label="POST /trigger-agent\n(API key)", color=INTERNAL) >> apigw
        apigw >> Edge(label="sync invoke", color=INTERNAL) >> trigger_agent_fn
        trigger_agent_fn >> Edge(label="async invoke", color=INTERNAL, style="dashed") >> orchestrator_fn

        orchestrator_fn >> Edge(label="tool calls", color=INTERNAL) >> bedrock
        orchestrator_fn >> Edge(label="get/put defense\nconfig", color=INTERNAL) >> state_table
        orchestrator_fn >> Edge(label="UpdateUserPoolClient\n(token TTL)", color=INTERNAL, style="dotted") >> cognito

        good_tester >> Edge(label="client_credentials\ntoken", color=GOOD, style="dotted") >> cognito
        adversarial_tester >> Edge(label="client_credentials\ntoken", color=ADVERSARIAL, style="dotted") >> cognito

        good_tester >> Edge(label="X-Expected,\nX-Table-Id", color=GOOD) >> apigw
        adversarial_tester >> Edge(label="X-Adversarial,\nX-Table-Id", color=ADVERSARIAL) >> apigw
        # Native COGNITO_USER_POOLS authorizer doesn't support M2M
        # client_credentials tokens (confirmed live, see DEPLOYMENT.md
        # troubleshooting) - replaced with this custom Lambda authorizer,
        # which verifies the JWT's RS256 signature against the User
        # Pool's JWKS directly via PyJWT.
        apigw >> Edge(label="custom TOKEN\nauthorizer invoke", color=INTERNAL, style="dotted") >> jwt_authorizer_fn
        jwt_authorizer_fn >> Edge(label="JWKS\n(public keys)", color=INTERNAL, style="dotted") >> cognito

        for svc in services:
            apigw >> Edge(color=INTERNAL) >> svc
            svc >> Edge(color=INTERNAL) >> state_table
            svc >> Edge(label="RequestCount", color=INTERNAL) >> dashboard

    red_agent >> Edge(label="tool calls\n(offline, once,\nlocal AWS creds)", color=ADVERSARIAL, style="dashed") >> bedrock

print(f"Wrote {os.path.join(OUT_DIR, 'architecture.png')}")
