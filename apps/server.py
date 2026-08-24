"""HTTP entry point, kept as a thin wrapper over the transport layer.

The serving logic lives in :mod:`atlas_harness.transport.http` so that
``python -m atlas_harness.transport.http`` and this wrapper cannot drift apart.
"""

from atlas_harness.transport.http import main

if __name__ == "__main__":
    main()
