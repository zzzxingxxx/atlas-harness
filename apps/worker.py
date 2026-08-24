"""Out-of-process worker entry point.

There is no worker: every command in this runtime executes in the caller's
process, and a run is driven by ``atlas run`` or by the HTTP transport. The
module stays as a named entry point so a future queue consumer has one obvious
home, and it exits with a configuration error rather than pretending to start.
"""

from atlas_harness.kernel.errors import ConfigurationError


def main() -> None:
    raise ConfigurationError(
        "there is no worker process; run agent work with `atlas run` "
        "or serve it with `python -m atlas_harness.transport.http`",
        details={"entry_points": ["atlas", "atlas_harness.transport.http"]},
    )


if __name__ == "__main__":
    main()
