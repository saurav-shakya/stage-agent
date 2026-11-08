# Architecture

## Shape of the system

```
 triggers ─┐                       ┌──────────── ViewerAgent (one per viewer) ─────────────┐
 live ev. ─┼─► Runtime (router) ──►│ idempotency → freshness → data-store read → facts     │
 control  ─┘        │              │ → goal (keep / choose) → model 2nd opinion (optional)  │
                    │              │ → Verifier(goal) → plan page (+≤1 message)             │
                    │              │ → Verifier(actions) → publish / record NO_ACTION       │
                    │              └───────────────────────────┬────────────────────────────┘
                    │                                          ▼
                    └─────────────► SQLite store: facts · decisions · actions · goals · outcomes · journal
                                                               │
             actions.jsonl ─► simulator ─► outcome events ─► Judge ─► outcomes + learned facts ─► Impact (ITT)
```

Everything that decides is deterministic code over cited facts. The model is an optional second opinion that can
only pick among goals the rules already found eligible.

## Ownership

`Runtime` routes every input by `user_id` to that viewer's `ViewerAgent`. Only the viewer's agent writes the
viewer's goals and actions. A viewer has at most one open goal (`goals.status='open'`), which is the single active
plan. Control events that touch many viewers (title withdrawn, rule corrected) are fanned out by the runtime to
the affected viewers' records only. Nothing else in the system can create a goal or publish an action.

## Planning

- **Goal choice.** On a trigger, an open goal that is still running is kept: a running goal is not dropped just
  because the viewer made progress. Otherwise `GoalBook.candidates` evaluates the measurable goals in a fixed
  priority order and returns the eligible ones with evidence. The order prefers near-term goals the agent can move
  with a page or one message (new viewers' first play, first full episode, return after absence) over goals it can
  barely move (first purchase, which has a tiny base rate).
- **Replanning on events, not on a schedule.** A live `purchase_completed` overrides the snapshot's subscription
  status, closes an open purchase goal (its success signal arrived) and cancels pending messages. A
  `notification_opt_out` cancels pending messages on that channel. A purchase goal whose viewer is now active is
  closed as superseded at the next trigger.
- **Goals never chosen.** G10 (no success event) and G11 (`tv_app_open` does not exist in the event stream).
  G06 "recently" is set to 30 days; G07 is derived from play/complete events and `episodes.csv`, and is only
  chosen when the unfinished series exists in the dialect the viewer will see.
- **Action choice.** The home page is the default lever: no consent needed, no fatigue risk, and it is what the
  viewer sees seconds after opening the app. A message is added only where it is proportionate: a push for a
  resume-series goal (naming the series and the real next episode number) or a return-after-absence goal, and the
  `wa_trial_ending_v1` WhatsApp template for a trial viewer. Messages go out at 19:00 IST on the next day.
  `wa_new_episode_v2` ("Naya episode aa gaya!") is never used, because we cannot tell whether an episode is new and
  the copy would be an invented claim. `wa_recommend_title_v1` is not used because there is no data source for
  `dialect_name`.
- **Doing nothing** is a recorded decision with a reason: `stale_trigger`, `holdout_arm`, `no_eligible_goal`,
  `invalid_app_dialect`, `data_store_unavailable`, `page_already_live`, `goal_failed_independent_check`.

## Memory and source handling

The SQLite store (`src/store.py`) is the durable per-viewer state:

| Table | Content |
|---|---|
| `facts` | every fact used for every decision: value, source, reference, observed_at, age, conflicts. Outcomes add `learned:*` facts (last goal result, push token undeliverable, page not seen) |
| `decisions` | the full decision record (the Section 4 format, extended) |
| `actions` | every action with status `published / scheduled / superseded / cancelled`, live window, titles |
| `goals` | goal instances with rule version, window, params fixed at commit, close reason |
| `outcomes` | judgment, evidence, exposure, attribution, learning |
| `journal` | append-only log: goal commits, replans, cancellations, rule corrections, withdrawals, learnings |

Precedence is defined per fact (`src/facts.py`). No source wins just because it is newer, bigger or easier to read:

| Fact | Rule |
|---|---|
| Page dialect | The app setting on the trigger, because the app only shows a personalised page for its current dialect. Profile and campaign-link dialects are kept as conflicts. An app setting that is not a catalogue dialect (`d7`, `NA`, empty) means no page. |
| Subscription | Billing events received after the snapshot beat the snapshot. A snapshot trial whose end date has passed with no billing event becomes `trial_expired_unknown`, so the agent does not act on a trial that may be over. |
| Signup | The server receive time of `signup_completed` beats the warehouse row. |
| Consent | Any opt-out event beats the snapshot (conservative). No profile means no consent. |
| Duplicate profile rows | The latest `updated_at` wins. Differing fields are listed as conflicts. |
| Event time | `received_at - (sent_at - occurred_at)`. The phone clock can be hours off, but the gap between two readings of the same clock is still the offline-buffering time. |

Every reference in a decision's `evidence` has an entry in `evidence_detail` with its source and freshness, so a
reviewer can check how current it was when the agent acted: events carry their true time, receive time and age;
profile rows their snapshot time and age; catalogue titles their availability window and whether they were
available at decision time; goal rules their version and when that version took effect; aggregates such as
"no episode_completed in history" the period of history they cover. The history period itself is read from the
pack, not assumed.

## Verification

`src/verifier.py` runs after the decision step and re-derives everything from the pack itself:

- **Goal.** Eligibility is recomputed from raw data. The goal must be measurable and have evidence. Measurable
  means its success event exists in the event stream and its condition is one the code fully understands; a
  condition with unknown or extra wording is not measurable (it is never read as "any event counts"). A model
  answer is accepted only if the rules agree with it (for example, the "confirmed in billing" G04 claim for a
  never-paid viewer is rejected and logged).
- **Page.** It must be agent arm, never holdout. The page dialect must equal the app setting. Each title must be
  in the catalogue, in that dialect, and available at publish time (after withdrawals). Each content slug appears
  once. Rows need at least 3 valid titles, with at most 6 rows and 10 titles per row. A page identical to one
  still live is not republished.
- **Message.** Checks consent (with opt-out events), quiet hours at `send_at`, one message per IST day (history
  plus our own scheduled), title dialect and availability, copy length, forbidden phrases, price claims (only
  `pricing.json` prices), deeplink format, approved template, and that every template variable comes from the
  catalogue or pricing.

A failed check removes that component. If nothing valid remains, the decision becomes NO_ACTION. Generated text
is never evidence or approval: the model sees only goal ids, rule reasons and a few booleans, never descriptions,
search queries or deeplinks.

## Latency and real-time behaviour

- **Budget.** 60 s to publish, 8 model calls and 20 tool calls per decision (all configurable, counted per
  decision). The real target is tighter. The viewer reaches the home page 3-40 s after opening the app, so a page
  that lands later is wasted. The model timeout is therefore 2.5 s and the data-store timeout 1.5 s.
- **Measured, not assumed.** `decided_in_ms` = measured processing + simulated dependency latency.
  `publish_delay_ms` adds queue wait from a 4-worker pool. Bursts (the offline-buffered burst at 10:10 IST, the
  evening peak) show up there. Under queue pressure (> 10 s wait) the optional model call is skipped.
- **Idempotency.** `processed_triggers` (event_id) and `sessions` (user, session_id) make duplicates and
  login+open pairs no-ops that are still recorded.
- **Late triggers.** An app open held offline for more than 10 minutes is used as history, never as a reason to
  act.

## Failure recovery

| Failure | Behaviour |
|---|---|
| Model timeout / 503 / malformed output | Rule-based goal; the decision is still published. A 503 opens a 60 s circuit breaker so later decisions do not pay the timeout. |
| Model slow (degraded window) | Each call is cut off at our own 2.5 s timeout, never the provider's latency. 3 timeouts in a row open the same 60 s breaker; when it closes, one probe decides: a timeout re-opens it at once, an answer resets the count. |
| Model confident but wrong | Rejected by the verifier and logged as a failed `model_claim` check. |
| Data store slow / timing out | NO_ACTION `data_store_unavailable`: we cannot check consent or eligibility, and acting blind is worse than not acting. |
| Title withdrawn | Only live pages and scheduled messages containing that title are touched. Pages are republished without it (the old page is marked superseded); messages are cancelled. |
| Goal rule corrected | A new rule version is created. Only open goals of that goal id are re-pointed and journalled; nobody else is reprocessed. Closed goals keep the rule they were judged under. If the new rule cannot be judged (unknown wording, or an event that does not exist), its goals are judged inconclusive rather than guessed, no new viewer is committed to that goal, and the final report says so. |
| Crash mid-replay | All state is in SQLite and committed per input. Re-delivered triggers are skipped by event_id. |

## Trust boundaries

1. **Pack data is evidence, not instruction.** Catalogue descriptions, search queries and deeplinks are content.
   The T0002 description, which tells agents to message every user, is flagged, never sent to the model and has
   no effect on actions. Deeplinks are parsed against a strict pattern, and a deeplink title is used only if it is
   valid in the page dialect.
2. **Model output is a suggestion.** It is parsed strictly, bounded to rule-eligible goals and verified.
3. **Only the verifier's output reaches `actions.jsonl`.**
4. **The holdout is enforced in two places**: the agent never plans for it, and the verifier rejects any action
   for a non-agent arm.
5. **Personal data.** The pack is git-ignored. By default nothing is sent to an external service. The optional
   `ClaudeProvider` (`--provider claude`) sends only goal ids, rule reasons with every viewer and event reference
   removed (a reason such as "never paid per profile:U-…" would otherwise carry the viewer id), and three viewer
   facts. Its answer is limited by a JSON schema to the rule-eligible goal ids, and it is still verified. Its errors
   map to the same timeout / unavailable / error paths as the mock, so the circuit breaker and rule fallback are
   unchanged. It should run only under an account whose retention terms fit the pack's privacy rule.

## Measurement

The arms are compared by intention-to-treat. The unit is a viewer with at least one trigger on the replay day,
counted in their randomized arm whatever the agent did. Metrics are defined identically for both arms and do not
depend on the agent's choices (play within 72 h, completion within 72 h, play on a later day, paid purchase within
14 d), plus the committed goal, with holdout viewers given the same rule-chosen goal as a shadow. Each difference
is reported with a 95% CI. `impact_report.md` also reproduces the misleading comparison (exposed vs everyone) and
shows that the same gap appears inside the holdout, where the agent did nothing. That is the argument for never
reading uplift from served vs unserved viewers, which is exactly what the pilot report did.

Per-goal judgment uses the goal's own success signal under the rule version it is bound to. Agent-arm successes
are labelled `after_exposure` only if the viewer saw an agent page or clicked an agent message before the success
event. Successes without prior exposure are recorded but not credited.

## Scaling: 10M viewers, 40 dialects

- **Partitioning.** Partition by `user_id`. A viewer's agent state is small (facts, open goal, recent actions), so
  it fits a key-value store with per-key serialisation (one writer per viewer), and triggers can be consumed from
  a stream partitioned by `user_id`. That preserves ownership and ordering without global locks.
- **Precompute what does not depend on the viewer.** Per-dialect availability-filtered title pools, popularity
  and new-release lists change slowly; refresh them every few minutes, so the decision path is read-only lookups.
  With 40 dialects, small dialects (like d3 here, 16 titles) need an explicit "not enough content: do nothing or
  fall back" rule rather than thin pages.
- **The model stays off the hot path.** It is optional, behind a timeout and a circuit breaker, and skipped under
  load. At 10M viewers, call it only when rules leave real ambiguity.
- **Control events are routed through indexes.** A withdrawal looks up `title_id → live actions`, a rule
  correction looks up `goal_id → open goals`. Cost scales with the affected viewers, not the population.
- **1,000 opens in the same minute.** Each decision is about 50 ms of CPU plus I/O, so a modest worker pool keeps
  queue wait near zero. When it does not, the system sheds the model call first, then publishes cached
  per-dialect default pages; it never delays past the home-page window just to be clever.

## What should never be automated

- Changing consent or contacting a viewer without it.
- Inventing prices or offers, or using deadline language.
- Deciding to roll out to everyone from an analysis (the pilot report is the cautionary example).
- Acting on a model's claim about billing or eligibility.
- Overriding the holdout.
- Correcting a goal's success definition: the agent applies corrections, but a human owner makes them.
