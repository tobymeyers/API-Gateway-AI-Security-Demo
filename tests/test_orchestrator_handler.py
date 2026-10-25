import json
import os
import sys
import unittest

# functions/orchestrator/handler.py imports `tools as agent_tools` at module
# scope, which reads these env vars at ITS own module scope (functions/
# orchestrator/tools.py) - dummy values are fine since this test only
# exercises build_initial_message (pure, no I/O), never anything that
# actually talks to AWS/Bedrock.
os.environ.setdefault("STATE_TABLE_NAME", "dummy-table")
os.environ.setdefault("USER_POOL_ID", "dummy-pool")
os.environ.setdefault("GOOD_CLIENT_ID", "dummy-good-client")
os.environ.setdefault("ADVERSARIAL_CLIENT_ID", "dummy-adversarial-client")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "functions", "orchestrator"))

from handler import build_initial_message  # noqa: E402


class TestBuildInitialMessage(unittest.TestCase):
    def test_embeds_config_traffic_stats_and_spec(self):
        config = {"block_all": False, "blocked_clients": [], "rate_limit_rps": None}
        stats = {"clients": [{"client_id": "x", "request_count": 2}]}
        spec = "openapi: 3.0.3\ninfo:\n  title: test\n"

        message = build_initial_message(config, stats, spec)

        self.assertIn(json.dumps(config), message)
        self.assertIn(json.dumps(stats), message)
        self.assertIn(spec, message)

    def test_tells_the_agent_it_does_not_need_to_re_fetch(self):
        message = build_initial_message({}, {"clients": []}, "openapi: 3.0.3\n")
        self.assertIn("no need to call them again just to see this", message)

    def test_still_decides_now(self):
        message = build_initial_message({}, {"clients": []}, "openapi: 3.0.3\n")
        self.assertIn("Decide now if any defenses are needed.", message)


if __name__ == "__main__":
    unittest.main()
