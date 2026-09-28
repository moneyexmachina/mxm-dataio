# Agent operating guide

This file contains durable instructions for engineering agents working in
`mxm-dataio`. Task-specific goals and temporary implementation decisions belong
in the task or Mission plan, not here.

## MXM engineering policy
### Python environment

MXM Python repositories use Poetry with a project-local `.venv/`.

Install the project and its dependencies with:

    poetry install

Do not use or depend on Python packages installed in the host environment.
Run Python and project Python tooling through Poetry or through repository
`make` targets.

Examples:

    poetry run python ...
    poetry run pytest ...

Prefer repository `make` targets where an appropriate target exists.

### Validation

The repository Makefile defines the canonical local engineering interface.

Before considering a change complete, run:

```bash
make check
```

`make check` is the authoritative full local validation gate. During
development, use narrower checks or tests where useful, but they do not replace
the final `make check`.

Do not weaken, bypass, or remove validation in order to make a change pass.

Report any validation that could not be run or did not pass.

### Authority boundaries

Treat source code, tests, and documentation as proposal-capable state: modify
them when required by the assigned work.

Do not assume authority to change repository governance or other
authority-bearing state. In particular, do not modify:

- CI or release workflows;
- branch or repository protection;
- credentials, identities, or secrets;
- release or publication configuration; or
- this `AGENTS.md`

unless the task explicitly authorizes that class of change.

Do not publish packages, create release tags, merge changes, or otherwise
promote a proposal into authoritative state unless explicitly authorized.

Never commit credentials, tokens, secrets, private data, or development
artifacts containing them.

Do not discard or overwrite existing work merely to obtain a clean working
state.

## `mxm-dataio` contract

`mxm-dataio` provides the Money Ex Machina data-I/O capability. Its purpose is
to make interactions with external data sources reproducible and auditable,
including the identity and provenance of requests, responses, and persisted
payloads.

Preserve these properties when changing the package:

- deterministic identity and serialization where they form part of persisted or
  externally observable behaviour;
- payload integrity and provenance;
- explicit separation between external-system interaction and DataIO's own
  domain behaviour; and
- auditable persistence and reuse of external-I/O results.

Persistence technologies, internal module structure, and current implementation
patterns are not themselves architectural invariants. Determine the current
design from the repository and the task context rather than assuming that the
existing implementation must be preserved.

## Testing

Use the narrowest relevant tests while developing, then run the full validation
gate before completion.

Tests must be deterministic and isolated. Do not make uncontrolled real
external requests or depend on mutable external state when an isolated test can
exercise the behaviour.

Changes to behaviour should be accompanied by tests that demonstrate the
intended behaviour and protect relevant invariants.

Use temporary or otherwise isolated state for test persistence and filesystem
operations. Do not modify or delete operator or production data while testing.

## Completion

Before declaring engineering work complete:

1. Run focused validation appropriate to the changed behaviour.
2. Run `make check`.
3. Update durable documentation when public behaviour or architectural
   contracts have changed.
4. Report the validation performed and any checks that could not be completed.

If completing a change reveals that this operating guide is inaccurate or
incomplete, report the required update rather than silently changing the guide.
