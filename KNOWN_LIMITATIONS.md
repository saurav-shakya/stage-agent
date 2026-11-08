# Known limitations

These are deliberate, given a 10-12 hour budget, and listed so a reviewer does not have to discover them.

## Agent behaviour

- **The model adds little in replay mode.** The mock agrees with the top rule candidate, so the model slot
  exercises the failure paths (timeouts, outages, malformed and unsupported claims) more than it improves
  decisions. A real model would order eligible goals or titles; it would still be advisory and verified.
- **The model is not called for holdout viewers** (the shadow goal is chosen by rules alone, at no cost). The
  fault configured for one holdout trigger (U-01090 timeout) is therefore never hit in the sample run. The same
  fault is covered by `test_model_timeout_falls_back_within_budget`.
- **Faults on stale triggers are not reached either.** Two of the three unsupported-claim faults sit on triggers
  that were held offline for 9-15 hours. The agent does not call the model for a session that is already over.
- **The goal priority is a fixed, hand-chosen order.** It is not learned. The pilot outcomes could inform it, but
  pilot goals were assigned differently, so we did not fit to them.
- **The most frequent goal shows no lift.** G07 (resume a series) is 317 of 754 agent goals and succeeds 3.5% of
  the time in both arms: easy to qualify for, hard to reach in 72 hours. The arm-level lift (+8.5 pts plays) comes
  from the page, not from G07 being met. Next step: order goals by pilot base rate and measured holdout lift
  instead of a fixed list.
- **The "never played / never completed" conditions only see 30 days.** History before 2026-09-01 is not in the
  pack.
- **G06 "recently lapsed" = 30 days. This is an assumption.** It is applied only when a lapse date can be cited
  (a lapse event or the profile's plan end date).
- **G07 eligibility is inferred** from play and complete events and `episodes.csv`, and limited to series that
  exist in the viewer's current dialect.
- **Few G03 goals are created in the run.** G01 outranks G03 for brand-new viewers. G03 is chosen only for
  viewers who signed up in the last 2 days and have already played. The 08:30 correction therefore re-points 0
  open goals in the sample run, but the mechanism is tested in
  `test_goal_rule_correction_applies_to_open_goals_only`.
- **Only success conditions the code recognises can be judged.** A corrected rule with new wording is judged
  inconclusive (and the goal is not chosen for new viewers) until that wording is added to `goals.CONDITIONS`.
  This is deliberate: guessing would inflate or deflate the measured impact.
- **Messages use one evening slot** (19:00 IST the next day) and only two goal types. There is no send-time
  optimisation, and `in_app_message` is not used.
- **Learning feeds forward but not back into this replay.** Outcomes are judged after the simulator runs, so
  learned facts (`learned:push_undeliverable`, `learned:page_not_seen`, last result) affect the next run's
  decisions, not the replay that produced them. In production the judge would run continuously as outcome events
  arrive.
- **Invalid app dialects** (`d7`, `NA`, empty) get no page. We could not tell from the pack what the app shows in
  that state, so we chose not to guess.

## Measurement

- **Primary-goal ITT has a wide interval.** With about 860 viewers per arm, effects smaller than about 3-5 points
  are not detectable. The report says so rather than claiming small wins.
- **The goal-success metric depends on the goal assigned, and goal assignment depends on facts that the
  treatment could in principle change** (a second trigger on the same day). The arm-level metrics (play,
  completion, purchase) do not have this issue and are listed alongside it.
- **The outcome simulator is the only source of post-replay behaviour.** Numbers describe the simulated world and
  will shift with the seed; evaluators use their own.
- **Measured processing time varies by a few milliseconds between runs.** `published_at` therefore varies
  slightly. Decisions and actions are otherwise deterministic for a given pack and seed.

## Engineering

- **Single process, faster than real time, with a simulated worker pool.** There is no network service and no
  real concurrency. The per-viewer ownership model is what a partitioned deployment would need (see
  ARCHITECTURE.md), but it is not exercised under real parallelism.
- **The hosted provider is not exercised by the sample run.** `--provider claude` exists and is unit-tested against
  a fake SDK (request contents, error mapping, refusal, circuit breaker), but it has not been run against the live
  API with the pack, on purpose: that would send pack-derived data to an external service. Fault replay
  (`model_faults.jsonl`, outage windows) is a mock feature.
- **Fixed thresholds.** The page TTL handling, the 10-minute staleness cut-off, the timeouts and the message hour
  are constants in code or `Config`, not tuned.
- **`sample_run/sim/outcome_events.jsonl` is not committed** (12 MB). It is regenerated by `run`, as is
  `state.sqlite`.
- **No UI.** Inspection is through `python -m src.cli viewer` and `viewer_state/*.json`.
