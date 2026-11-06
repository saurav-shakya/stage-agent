# Impact report

All numbers are produced by `python -m src.cli run` from the replay day and the simulator output (seed `oua-practice`). Re-running with the same seed reproduces them exactly.

## 1. Agent vs randomized holdout (intention-to-treat)

Unit: every viewer who sent at least one trigger on the replay day, counted in the arm `replay/arms.csv` gave them, whatever the agent then did. Agent arm n=863, holdout n=883. Windows start at the viewer's first trigger.

| Metric | Agent | Holdout | Difference (pts) | 95% CI | p |
|---|---|---|---|---|---|
| committed goal reached (first goal per viewer; holdout = shadow goal) | 12.17% (n=863) | 11.10% (n=883) | +1.07 | ±3.01 | 0.486 |
| play_started within 72h | 60.25% (n=863) | 51.76% (n=883) | +8.50 | ±4.64 | 0.000 |
| episode_completed within 72h | 36.73% (n=863) | 29.33% (n=883) | +7.40 | ±4.40 | 0.001 |
| plays again on a later IST day within 7d | 50.41% (n=863) | 52.32% (n=883) | -1.92 | ±4.69 | 0.423 |
| paid purchase within 14d | 3.82% (n=863) | 4.08% (n=883) | -0.25 | ±1.83 | 0.786 |

Read the CI before the point estimate: with ~850 viewers per arm, differences under ~4-5 points cannot be told apart from noise. A difference whose interval crosses zero is reported as "no detectable effect", not as a win.

### Per goal (first goal per viewer, same rules in both arms)

| Goal | Agent reached | Holdout reached |
|---|---|---|
| G01 | 24/59 (40.68%) | 14/50 (28.00%) |
| G02 | 29/182 (15.93%) | 20/158 (12.66%) |
| G03 | 0/1 (0.00%) | 1/2 (50.00%) |
| G04 | 0/1 (0.00%) | 0/1 (0.00%) |
| G05 | 2/45 (4.44%) | 3/48 (6.25%) |
| G06 | 5/52 (9.62%) | 9/63 (14.29%) |
| G07 | 11/317 (3.47%) | 11/303 (3.63%) |
| G08 | 9/45 (20.00%) | 11/50 (22.00%) |
| G09 | 3/12 (25.00%) | 2/10 (20.00%) |
| G12 | 22/40 (55.00%) | 27/54 (50.00%) |

Per-goal cells are small; they are for diagnosis, not for claims.

## 2. Why the simpler comparison misleads

The pilot-style comparison, recomputed on this replay: viewers who actually **saw** an agent page played within 72h at **71.98%** (n=621), everyone else at **47.11%** (n=1125). That gap is mostly selection, not effect: seeing a page requires the viewer to be in a live session when the page lands, which already predicts playing. Inside the **holdout**, where the agent did nothing, viewers with a fresh trigger played at **56.49%** (n=740) versus **27.27%** (n=143) for viewers whose only triggers were stale or unreadable: a large gap with zero treatment.

## 3. The earlier pilot, re-analysed

| Comparison | Group A | Group B | Difference |
|---|---|---|---|
| Report headline: served vs everyone else | 40.09% (217) | 17.66% (3674) | +22.4 pts |
| **Randomized: treatment arm vs holdout arm (ITT)** | 18.25% (1912) | 19.56% (1979) | **-1.30 pts ±2.46 (p=0.30)** |

The report's 2.3x "uplift" compares 217 served viewers with all other viewers, including the whole holdout. Who got served was not random (the agent served viewers who showed up and qualified), and goals differ hugely in base rate (G02 19.44%, G04 23.26%, G05 0.90%, G06 1.81%, G09 64.11%). The randomized comparison shows no benefit, and the point estimate is slightly negative. The pilot does **not** support rolling the agent out to every user.

## 4. Safety counters from the simulator

- Policy problems flagged by the simulator: **0**
- Actions on holdout viewers: 0
- Pages accepted / shown personalised / published but not seen: 908 / 829 / 1
- Rows dropped by the platform: 0; pages rejected empty: 0
- Push requested / delivered / clicked / bad token: 295 / 257 / 3 / 38
- WhatsApp requested / delivered: 0 / 0
