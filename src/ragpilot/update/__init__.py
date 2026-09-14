"""Software update subsystem (CLI performance improvement plan, Phase 3).

Distinct from ``ragpilot upgrade``/``ops/upgrade.py``, which applies
database *schema* migrations to an already-installed RAGpilot -- this
package is about discovering and installing a newer RAGpilot *release*
itself. See ``cli/update.py`` for the ``ragpilot update ...`` commands and
this plan's "Phase 5 -- Upgrade Process" section for why the two are kept
separate rather than folded into one command.

- ``versioning``: the installed version, and MAJOR.MINOR.PATCH parsing/
  comparison.
- ``checker``: queries GitHub for the latest release (HTTPS, this
  repository only, semver-validated).
- ``cache``: reads/writes ``<RAGPILOT_HOME>/update.json`` so a later
  phase's startup notification never needs a synchronous network call.
- ``models``: the small data types shared across the above.
- ``installer``: installing a newer release once found -- fleshed out in
  a later phase (installation-method detection, migrations, health
  check); ``ragpilot update install`` is a clearly-labeled stub until then.
"""
