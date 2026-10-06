from tirodhan.deployment.release import runtime_secret_names


def test_runtime_secret_names_excludes_operational_secrets() -> None:
    available = frozenset(
        {
            "ghcr-pull-pat",
            "fluent-bit-sas",
            "google-maps-api-key",
        }
    )

    assert runtime_secret_names(available) == frozenset({"google-maps-api-key"})


def test_runtime_secret_names_ignores_unrelated_key_vault_secrets() -> None:
    available = frozenset({"kaleyra-api-key", "some-other-secret"})

    assert runtime_secret_names(available) == frozenset({"kaleyra-api-key"})
