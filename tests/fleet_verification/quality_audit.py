"""Explicit offline audit/minimization command; never writes fixtures during normal CI."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from hypothesis import find, settings
from hypothesis.errors import NoSuchExample

from tirodhan.modules.routing_operations.economics import insertion_search
from tirodhan.modules.routing_operations.service import PROBLEM_CODEC

from .contracts import policy, proposal
from .oracle import enumerate_feasible
from .strategies import fleet_problems


def compare(case):
    problem, weights, incumbent = case
    choices = list(enumerate_feasible(problem, weights, incumbent))
    if not choices:
        return None
    seed_orders, _ = max(choices, key=lambda candidate: candidate[1].rank)
    optimum_orders, optimum = min(choices, key=lambda candidate: candidate[1].rank)
    refined, score, _ = insertion_search(
        problem, proposal(problem, seed_orders), policy(weights), incumbent
    )
    return seed_orders, optimum_orders, optimum, refined, score


def main():
    parser = argparse.ArgumentParser(description="Shrink a valid but inferior bounded-search seed")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    def inferior(case):
        result = compare(case)
        return (
            result is not None
            and result[4].unserved_lots == result[2].unserved_lots
            and result[4].deadline_risk == result[2].deadline_risk
            and result[4].generalized_seconds > result[2].generalized_seconds
        )

    try:
        case = find(
            fleet_problems(max_lots=3),
            inferior,
            settings=settings(max_examples=500, deadline=None, derandomize=True, database=None),
        )
    except NoSuchExample:
        print("NO_QUALITY_WITNESS_IN_500_EXAMPLES")
        return
    problem, weights, incumbent = case
    seed, optimal, expected, refined, actual = compare(case)

    def encode(orders):
        return {str(key): [str(s) for s in order] for key, order in orders.items()}

    args.output.write_text(
        json.dumps(
            {
                "classification": "BOUNDED_SEARCH_QUALITY_GAP",
                "hypothesis": "6.168.5",
                "problem": PROBLEM_CODEC.dump_python(problem, mode="json"),
                "weights": asdict(weights),
                "incumbent": encode(incumbent),
                "seed": encode(seed),
                "optimum_orders": encode(optimal),
                "refined": encode({r.vehicle_id: r.stop_ids for r in refined.routes}),
                "optimum": asdict(expected),
                "actual": asdict(actual),
            },
            indent=2,
        )
        + "\n"
    )
    print("MINIMIZED_QUALITY_GAP", {"optimum": asdict(expected), "actual": asdict(actual)})


if __name__ == "__main__":
    main()
