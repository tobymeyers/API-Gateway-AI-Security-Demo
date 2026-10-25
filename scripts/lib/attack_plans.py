"""Picks the most recent attack plan out of attack-plans/, where every
generation now writes a new, timestamped file
(attack-plan-<run_id>.json) instead of overwriting one fixed path -
see load-testers/adversarial-traffic/generate_attack_script.py and scripts/generate_attack_script.sh.
(The generating tool and its CLI flag are still called "attack script" -
only the stored, versioned artifact's own folder/filename convention
uses "plan", since that's what the file actually is: a timed sequence
of actions, not source code.)

Every tool that needs "the" attack plan by default (run_round.sh,
comprehensive_gameday.sh, generate_attack_script.sh's own "is there
already something usable, or do we need to spend Bedrock tokens"
check) goes through this same function, so "pick whatever's newest"
has exactly one implementation - no risk of two call sites silently
disagreeing on which file is current.

pick_latest/run_id_from_filename are pure (strings in, strings out)
and unit-tested directly - see tests/test_attack_plans.py.
"""
import argparse
import os
import re
import sys

FILENAME_RE = re.compile(r"^attack-plan-(\d{8}-\d{6})\.json$")


def run_id_from_filename(filename):
    """The YYYYMMDD-HHMMSS run_id embedded in a conventionally-named
    file, or None if filename doesn't match that convention at all
    (e.g. a stray file someone dropped in by hand)."""
    m = FILENAME_RE.match(filename)
    return m.group(1) if m else None


def pick_latest(filenames):
    """filenames is a list of basenames, unfiltered (e.g. raw
    os.listdir() output) - non-conforming names are ignored rather
    than erroring, so a stray file in the directory can't break this.
    Returns the latest basename by its embedded run_id (plain string
    sort works: YYYYMMDD-HHMMSS sorts chronologically), or None if no
    file in the list matches the naming convention at all."""
    candidates = [(run_id_from_filename(f), f) for f in filenames]
    candidates = [(run_id, f) for run_id, f in candidates if run_id is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]


def find_latest(directory):
    """Thin IO wrapper around pick_latest. The directory not existing
    yet (nothing has ever been generated) is "nothing found", not an
    error - returns None either way."""
    if not os.path.isdir(directory):
        return None
    return pick_latest(os.listdir(directory))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    args = parser.parse_args()

    latest = find_latest(args.directory)
    if latest is None:
        print(
            f"ERROR: no attack-plan-<run_id>.json files found in {args.directory} - "
            "generate one first: ./scripts/generate_attack_script.sh",
            file=sys.stderr,
        )
        sys.exit(1)
    print(os.path.join(args.directory, latest))


if __name__ == "__main__":
    main()
