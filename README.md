# stage-agent

STAGE first-round assignment.

## Setup

- Python 3.11, standard library only (same as the simulator).
- Extract the data pack zip into `data_pack/`. It is git-ignored on purpose: it holds personal data.

## Layout

```
NOTES.md              rules from the brief + configs, one line each
AI_COLLABORATION.md   log of how AI was used and what I verified myself
src/                  agent code (ingest, state store, resolver, goal picker, actions)
tests/                unit tests and verifier checks
sample_run/           output of one end-to-end run
data_pack/            local only, not committed
```
