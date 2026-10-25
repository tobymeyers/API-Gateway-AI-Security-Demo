#!/usr/bin/env python3
"""Generates docs/presentation.png - a simplified, audience-facing version
of the architecture, for slides rather than engineering reference. See
generate_architecture_diagram.py for the full technical diagram.

    python3 docs/generate_presentation_diagram.py
"""
import os

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.general import User
from diagrams.aws.management import Cloudwatch
from diagrams.aws.network import APIGateway

OUT_DIR = os.path.dirname(os.path.abspath(__file__))

BLUE = "#2E5C99"
RED = "#A85226"

graph_attr = {
    "fontsize": "34",
    "fontname": "Helvetica-Bold",
    "bgcolor": "white",
    "pad": "1.2",
    "margin": "0.6",
    "splines": "curved",
    "nodesep": "1.0",
    "ranksep": "1.4",
}
node_attr = {"fontname": "Helvetica-Bold", "fontsize": "18"}
edge_attr = {"fontname": "Helvetica", "fontsize": "16", "penwidth": "2.5"}

with Diagram(
    "AI vs. AI: Defending the Gateway",
    filename=os.path.join(OUT_DIR, "presentation"),
    outformat="png",
    direction="LR",
    show=False,
    graph_attr=graph_attr,
    node_attr=node_attr,
    edge_attr=edge_attr,
):
    red_team = User("Red Team AI\nprecision attack")

    with Cluster("Your Table's Gateway"):
        gateway = APIGateway("API Gateway")
        services = Lambda("3 Backend\nServices")
        gateway >> Edge(color="#999999", style="invis") >> services

    scoreboard = Cloudwatch("Live Score")
    blue_team = User("Your Blue Team AI\nyour prompt, live")

    red_team >> Edge(label="  attacks  ", color=RED, penwidth="3") >> gateway
    blue_team >> Edge(label="  defends  ", color=BLUE, penwidth="3") >> gateway
    gateway >> Edge(label="  scores live  ", color="#5B6068") >> scoreboard

print(f"Wrote {os.path.join(OUT_DIR, 'presentation.png')}")
