# AI collaboration log

An AI coding assistant was used throughout, as the brief allows. I set the direction and the constraints, and
nothing was kept without running it. This log records what it produced, how that output was checked, and where it
was changed or rejected.

## How the work was split

| Step | AI assistant | Checked by |
|---|---|---|
| Read the brief and profile the pack | Summarised the brief; counted duplicates, late and buffered events, duplicate profile rows, viewers without a profile, language variants, invalid app dialects, the catalogue instruction text | Re-ran the counts with a separate script before relying on them |
| Pilot re-analysis | Computed ITT from `pilot_outcomes.csv`: treatment 18.25% vs holdout 19.56%, against the report's 40.1% vs 17.7% | Recomputed independently; the same figures are produced by `impact.pilot()` and appear in `impact_report.md` |
| Read the simulator | Pointed out what the simulator rewards and checks: page dialect must equal app dialect, 10-minute staleness rule, quiet hours and cap counted per action | Read `validate_page` and the session logic directly |
| Code | Drafted all modules in `src/` and the tests | Full replay, simulator run, 17 unit tests, manual reading of decision records |
| Docs | Drafted README, ARCHITECTURE, KNOWN_LIMITATIONS | Checked every number in them against the generated reports |

## Output that was changed or rejected after checking

1. **Goal priority.** The first version put "resume series" (G07) near the top. In the first full run it took
   about half of all agent goals, and many of those series were in a different dialect from the one the viewer
   would see, so the agent could neither show nor name them. Changed: G07 now requires the series to exist in the
   app dialect, and "first full episode" (G02) comes before it.
2. **Invalid app dialects.** The first run built pages for app settings `d7`, `NA` and empty. They contained zero
   valid titles and were stopped by the verifier as empty pages. Changed: an explicit `invalid_app_dialect`
   NO_ACTION with a reason, instead of relying on the verifier to catch it.
3. **The model's role.** Rejected the idea of letting the model decide eligibility or write free-text copy. It
   only picks among rule-eligible goals and is verified. This is what turns the injected "confirmed in billing"
   claim into a logged rejection instead of a wrong goal.
4. **WhatsApp "new episode" template.** Rejected using `wa_new_episode_v2` ("Naya episode aa gaya!") for the
   next unwatched episode: the pack has no episode release dates, so "new" would be an invented claim (policy
   rule 4).
5. **Uplift metric.** A served-vs-unserved comparison is easy to produce and looks good. It is kept only as the
   example of what not to do. It is set next to the same split inside the holdout, which shows the bias.
6. **Message planning.** The first version let the verifier reject about 150 messages for the frequency cap,
   because the planner proposed a second message on later triggers. Changed: the planner now checks for an
   already-scheduled message, and the verifier remains the backstop.
7. **A failing test.** The circuit-breaker test failed because its second call landed exactly on the 60-second
   breaker boundary. The behaviour was intended; the test's timing was wrong and was fixed. The behaviour was not
   changed to make the test pass.

## How correctness was checked

- **Full replay + simulator.** 0 policy problems reported by the simulator, 0 actions on holdout viewers, 0
  decisions over 60 s.
- **Decision records.** Read records for the surprise cases: the unsupported-claim viewer (U-01991), stale
  buffered triggers, three-way dialect conflicts, the purchase that arrived after a decision, and the T0001
  withdrawal.
- **Unit tests.** 17 tests, including the four the brief requires: late/duplicate trigger, conflicting source,
  tool failure, corrected goal rule.

## Before submitting (my own pass)

- [ ] Run `python -m src.cli run` and the tests on my own machine.
- [ ] Walk one viewer end to end with `python -m src.cli viewer U-02825 --out sample_run`.
- [ ] Re-read ARCHITECTURE.md and be ready to defend each choice in the discussion.
