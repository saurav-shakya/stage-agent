# AI collaboration log

I used an AI coding assistant for most of this work, which the brief allows. I decided what to build and what the
rules were. I did not keep anything without running it. Below is what the AI did, how I checked it, and where I
changed or rejected its work.

## How the work was split

| Step | What the AI did | How I checked it |
|---|---|---|
| Read the brief and the data | Summarised the brief. Counted duplicate events, late and offline events, duplicate profile rows, viewers with no profile, language variants, bad app dialects, and the catalogue text that tries to give instructions | I re-ran the counts with a separate script before trusting them |
| Re-check the old pilot | Worked out the fair comparison (ITT) from `pilot_outcomes.csv`: treatment 18.25% vs holdout 19.56%. The pilot report had claimed 40.1% vs 17.7% | I recomputed it myself. `impact.pilot()` gives the same numbers, and they are in `impact_report.md` |
| Read the simulator | Told me what the simulator checks: page dialect must match the app dialect, the 10-minute staleness rule, quiet hours and the message cap | I read `validate_page` and the session code myself |
| Code | Wrote the first version of everything in `src/` and the tests | Full replay, simulator run, 46 unit tests, and reading decision records by hand |
| Docs | Wrote the first drafts of README, ARCHITECTURE and KNOWN_LIMITATIONS | I checked every number in them against the generated reports |

## Where I changed or rejected the AI's work

1. **Goal priority.** In the first version, "resume series" (G07) was near the top. In the first full run it took
   about half of all agent goals, and many of those series were in a different dialect from the one the viewer
   sees, so the agent could not show them. I changed it: G07 now needs the series to exist in the app dialect, and
   "first full episode" (G02) comes before it.
2. **Bad app dialects.** The first run built pages for app settings like `d7`, `NA` and empty. Those pages had
   zero valid titles, and the verifier stopped them as empty. I changed it to a clear `invalid_app_dialect`
   NO_ACTION with a reason, instead of hoping the verifier catches it.
3. **What the model is allowed to do.** I rejected letting the model decide eligibility or write message text. It
   only picks from goals the rules already found eligible, and its answer is checked. That is why the fake
   "confirmed in billing" claim became a logged rejection and not a wrong goal.
4. **WhatsApp "new episode" message.** I rejected using `wa_new_episode_v2` ("Naya episode aa gaya!") for the next
   unwatched episode. The data has no episode release dates, so saying "new" would be a made-up claim (policy
   rule 4).
5. **How to measure uplift.** Comparing served viewers with unserved viewers is easy and looks great, but it is
   wrong. I kept it only as the example of what not to do, next to the same split inside the holdout, which shows
   the bias.
6. **Message planning.** In the first version the verifier rejected about 150 messages for the one-per-day cap,
   because the planner kept proposing a second message. I changed the planner to check for an already scheduled
   message first. The verifier is still the backstop.
7. **A failing test.** The circuit-breaker test failed because its second call landed exactly on the 60-second
   boundary. The code was right; the test timing was wrong, so I fixed the test. I did not change the code just to
   make the test pass.

### Second pass: checking the finished code against the brief

When the first version was done, I had the AI check the code against the brief, section by section, and against
the data. I fixed the problems one by one. For each fix: a test, a full replay, and a before/after comparison of
every decision and outcome.

8. **A rewrite that broke the code.** The AI made a big uncommitted rewrite of the time helpers. It broke the
   imports and the tests failed. I rejected it: I saved it on a side branch and went back to the last working
   commit, instead of patching the rewrite until it ran.
9. **Tests that could pass without running.** When the data pack was not found, the agent tests skipped quietly,
   so "all green" could mean nothing was tested. Now there is one pack lookup (`src/pack.py`) for both ways the zip
   can unpack, plus `$OUA_PACK`. A missing pack is now an error that lists every path it tried.
10. **A surprise that was not really simulated.** The brief says the model can become slow, but the code only
    simulated the model being down. I added a slow-model window (latency and timeouts read from the replay) and a
    circuit breaker after 3 timeouts in a row. Its first test failed because the triggers were a minute apart and
    the 60-second breaker had already closed. Again the test timing was wrong, not the code.
11. **A hidden "success".** If a goal's success rule had wording the code did not know, it counted any event as
    success. So a corrected rule with new wording would have been scored as success and made the agent look
    better than it is. Now the rule text is read strictly, and a rule the code cannot read is judged
    "inconclusive". Numbers like "30 days" and "3 days" are read from the rule, not fixed in the code.
12. **A wrong test line.** One line in a new test used variables from a different test and expected "succeeded"
    where the rule says "failed". I did not accept it as written. I replaced it with what it was trying to check:
    two days of plays pass under the corrected 2-day rule and fail under the old 3-day rule.
13. **Evidence without freshness.** Section 4 of the brief asks for evidence "with their freshness", but 5,613 of
    8,523 evidence references had no freshness. Now every reference has its source and freshness, and a test
    checks it. Two dates that were typed into the report text are now read from the data.
14. **A real model, with privacy checked first.** Before adding the optional `--provider claude`, I checked what
    the model would actually receive. One rule reason ("never paid per profile:U-…") contained the viewer id. Now
    all viewer and event references are removed before anything is sent, and a test checks it. I tested the live
    API only with made-up data (`check-model`). I did not run it with the pack, because that would send data from
    the pack to an outside service. This is written in KNOWN_LIMITATIONS.
15. **The AI was wrong about my API key.** My `check-model` call failed with HTTP 401. The AI said my key format
    (`sk-ant-usr-…`) was wrong and that Claude API keys start with `sk-ant-api03-`. I did not accept that and asked
    it to check the latest official docs. The docs showed it was wrong: `sk-ant-usr-` is the newer personal key
    type, and it works. The real problem was the key itself (Anthropic said "invalid x-api-key"). I made a new key
    and it worked. Two code changes came out of this: errors now show Anthropic's own message, and keys that are
    not tied to one workspace can send `ANTHROPIC_WORKSPACE_ID`.
16. **Model speed, measured.** With the real API, Opus 5.5 took about 5 seconds per answer. The agent waits at most
    2.5 seconds for the model, because the page must land while the viewer is still on the home screen. Haiku 5.5
    took about 2.5 seconds with an equally good answer, so I made Haiku the default for `--provider claude`. This
    also showed that the timeout and the rule fallback are not just theory; a real model really is that sl  ow.

## How I checked that it works

- **Full replay + simulator.** 0 policy problems from the simulator, 0 actions on holdout viewers, 0 decisions
  over 60 seconds.
- **Decision records.** I read the records for the surprise cases: the fake model claim (U-01991), old offline
  app opens, three-way dialect conflicts, the purchase that arrived after a decision, and the T0001 withdrawal.
- **Unit tests.** 46 tests, including the four the brief asks for: late/duplicate trigger, conflicting source,
  tool failure, corrected goal rule.
- **Before/after comparison for every fix.** After each change I re-ran the full replay and compared every
  decision (status, action, goal, content, evidence) and every outcome with the previous commit. The fixes for
  rule reading and evidence freshness (11-13) changed none of them, so they did not move the reported impact.

## Before submitting (my own pass)

- [x] Run `python -m src.cli run` and the tests on my own machine.
- [x] Walk one viewer end to end with `python -m src.cli viewer U-02825 --out sample_run`.
- [ ] Re-read ARCHITECTURE.md and be ready to defend each choice in the discussion.
