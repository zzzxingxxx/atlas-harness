from atlas_harness.kernel.errors import ConfigurationError


def test_error_is_machine_readable() -> None:
    error = ConfigurationError("invalid workspace", details={"field": "workspace_root"})

    assert error.as_dict() == {
        "error": "configuration_error",
        "message": "invalid workspace",
        "details": {"field": "workspace_root"},
    }
    assert error.exit_code == 2
