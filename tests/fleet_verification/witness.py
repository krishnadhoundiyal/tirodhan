import json
from pathlib import Path
from uuid import UUID

from tirodhan.modules.routing_operations.service import PROBLEM_CODEC

from .oracle import Weights


def single_sweep_witness(name="single_sweep_extra_vehicle.json"):
    data = json.loads((Path(__file__).parent / "data" / name).read_text())
    problem = PROBLEM_CODEC.validate_python(data["problem"])
    weights = Weights(**data["weights"])

    def decode(rows):
        return {UUID(v): tuple(UUID(s) for s in stops) for v, stops in rows.items()}

    return problem, weights, decode(data["incumbent"]), decode(data["seed"]), data
