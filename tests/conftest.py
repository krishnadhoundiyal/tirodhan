"""Bounded default generation; opt-in extended/native profiles share reproducible seeds."""

import os

from hypothesis import HealthCheck, settings
from hypothesis.database import DirectoryBasedExampleDatabase

settings.register_profile(
    "fleet-ci",
    max_examples=20,
    stateful_step_count=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    print_blob=True,
    database=DirectoryBasedExampleDatabase(
        os.getenv("TIRODHAN_HYPOTHESIS_DATABASE", ".hypothesis/examples")
    ),
)
settings.register_profile(
    "fleet-extended",
    max_examples=100,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    print_blob=True,
    database=DirectoryBasedExampleDatabase(
        os.getenv("TIRODHAN_HYPOTHESIS_DATABASE", ".hypothesis/examples")
    ),
)
settings.load_profile(os.getenv("TIRODHAN_HYPOTHESIS_PROFILE", "fleet-ci"))
