# hubzero-trac-macros
#
# Two HUBzero Trac wiki macros: [[image ...]] and [[link ...]].  Discovered
# system-wide via the [trac.plugins] entry points declared in pyproject.toml,
# so a single install in site-packages serves every Trac env on the host (no
# per-env file required).
#
# This package is intentionally empty -- each macro lives in its own module
# (hubzero_macros.image, hubzero_macros.link) so Trac's component discovery
# can register them independently.
