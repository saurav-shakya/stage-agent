"""Writes the sample_run artefacts: outcomes, viewer state, latency report, impact report, final report."""
import json
import os
import statistics
from collections import Counter, defaultdict

from .timeutil import IST, ts


def pct(x):
    return '%.2f%%' % (100 * x)


def write_outcomes(out_dir, outcomes):
    with open(os.path.join(out_dir, 'outcomes.jsonl'), 'w', encoding='utf-8') as f:
        for o in outcomes:
            f.write(json.dumps(o, ensure_ascii=False) + '\n')


def write_viewer_state(rt, out_dir):
    st = rt.store
    vdir = os.path.join(out_dir, 'viewer_state')
    os.makedirs(vdir, exist_ok=True)
    outcomes = defaultdict(list)
    for o in st.outcomes():
        outcomes[o['user_id']].append(o)
    users = st.users()
    for u in users:
        facts = {}
        for r in st.facts_for(u):  # latest value per key, with provenance
            facts[r['key']] = {'value': json.loads(r['value']), 'source': r['source'], 'ref': r['ref'],
                               'observed_at': r['observed_at'], 'age_s_at_decision': r['age_s'],
                               'conflicts': json.loads(r['conflicts']), 'as_of_decision': r['decision_id']}
        state = {'viewer_id': u, 'arm': rt.pack.arms.get(u), 'facts_latest': facts,
                 'goals': st.goals('user_id=?', (u,)),
                 'decisions': [{k: d.get(k) for k in ('decision_id', 'status', 'action', 'no_action_reason',
                                                      'goal', 'confidence', 'published_at')}
                               | {'trigger': d['trigger']['event_id']} for d in st.decisions_for(u)],
                 'actions': [{k: a[k] for k in ('action_id', 'type', 'status', 'published_at', 'send_at',
                                                'live_until', 'dialect')} for a in st.actions_for(u)],
                 'outcomes': outcomes.get(u, []), 'journal': st.journal_for(u)}
        with open(os.path.join(vdir, '%s.json' % u), 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=1, ensure_ascii=False, default=str)
    return len(users)


def _window(d, controls):
    t = ts(d['trigger']['deliver_at'])
    for c in controls:
        if c['type'].startswith('dependency') and ts(c['at']) <= t < ts(c['until']):
            return '%s_%s' % (c['dependency'], c['type'].split('_')[1])
    return 'normal'


def latency_report(decisions, controls, budget_ms):
    real = [d for d in decisions if d['status'] in ('PUBLISHED', 'NO_ACTION')]

    def stats(ds):
        if not ds:
            return {}
        a = sorted(d['publish_delay_ms'] for d in ds)
        q = lambda p: a[min(len(a) - 1, int(p * len(a)))]
        return {'n': len(a), 'p50_ms': q(0.5), 'p95_ms': q(0.95), 'p99_ms': q(0.99), 'max_ms': a[-1],
                'over_60s': sum(x > 60_000 for x in a), 'over_5min': sum(x > 300_000 for x in a),
                'mean_queue_wait_ms': round(statistics.mean(d['queue_wait_ms'] for d in ds), 1)}
    by_window = defaultdict(list)
    for d in real:
        by_window[_window(d, controls)].append(d)
    # Busiest 10 minutes overall and in the evening (17:00-24:00 IST): bursts show up as queue wait.
    def bucket(d):
        t = ts(d['trigger']['deliver_at']).astimezone(IST)
        return t.strftime('%H:') + '%d0' % (t.minute // 10)
    buckets = Counter(bucket(d) for d in decisions)
    median_bucket = statistics.median(buckets.values())

    def peak_of(keys):
        b, n = max(((k, buckets[k]) for k in keys), key=lambda x: x[1])
        return {'bucket_ist': b, 'triggers': n, 'x_median': round(n / median_bucket, 1),
                **stats([d for d in real if bucket(d) == b])}
    return {
        'budget_ms': budget_ms, 'definition': 'publish_delay = queue wait + measured processing + simulated '
                                              'dependency latency; time from trigger delivery to publish/no-action',
        'all_decisions': stats(real),
        'by_condition': {k: stats(v) for k, v in sorted(by_window.items())},
        'median_triggers_per_10min': median_bucket,
        'busiest_10min': peak_of(buckets),
        'evening_peak': peak_of([k for k in buckets if k >= '17:']),
        'skipped_idempotent': sum(d['status'].startswith('SKIPPED') for d in decisions),
        'model_outcomes': dict(Counter(d['model'].get('outcome') for d in decisions if d['model'].get('called')
                                       or d['model'].get('outcome'))),
    }


def impact_report(imp, pil, sim_report):
    L = []
    w = L.append
    w('# Impact report\n')
    w('All numbers are produced by `python -m src.cli run` from the replay day and the simulator output '
      '(seed `%s`). Re-running with the same seed reproduces them exactly.\n' % sim_report.get('seed'))
    w('## 1. Agent vs randomized holdout (intention-to-treat)\n')
    w('Unit: every viewer who sent at least one trigger on the replay day, counted in the arm `replay/arms.csv` gave '
      'them, whatever the agent then did. Agent arm n=%d, holdout n=%d. Windows start at the viewer\'s first trigger.\n'
      % (imp['arms'].get('agent', 0), imp['arms'].get('holdout', 0)))
    w('| Metric | Agent | Holdout | Difference (pts) | 95% CI | p |')
    w('|---|---|---|---|---|---|')
    for r in imp['itt']:
        w('| %s | %s (n=%d) | %s (n=%d) | %+.2f | ±%.2f | %.3f |' % (
            r['metric'], pct(r['agent_rate']), r['agent_n'], pct(r['holdout_rate']), r['holdout_n'],
            r['diff_pts'], r['ci95_pts'], r['p_value']))
    w('\nRead the CI before the point estimate: with ~850 viewers per arm, differences under ~4-5 points cannot be '
      'told apart from noise. A difference whose interval crosses zero is reported as "no detectable effect", '
      'not as a win.\n')
    w('### Per goal (first goal per viewer, same rules in both arms)\n')
    w('| Goal | Agent reached | Holdout reached |')
    w('|---|---|---|')
    for g, v in sorted(imp['per_goal'].items()):
        a, h = v['agent'], v['holdout']
        w('| %s | %d/%d (%s) | %d/%d (%s) |' % (g, a[0], a[1], pct(a[0] / a[1]) if a[1] else '-',
                                                h[0], h[1], pct(h[0] / h[1]) if h[1] else '-'))
    w('\nPer-goal cells are small; they are for diagnosis, not for claims.\n')
    n = imp['naive']
    w('## 2. Why the simpler comparison misleads\n')
    w('The pilot-style comparison, recomputed on this replay: viewers who actually **saw** an agent page played within '
      '72h at **%s** (n=%d), everyone else at **%s** (n=%d). That gap is mostly selection, not effect: seeing a page '
      'requires the viewer to be in a live session when the page lands, which already predicts playing. Inside the '
      '**holdout**, where the agent did nothing, viewers with a fresh trigger played at **%s** (n=%d) versus **%s** '
      '(n=%d) for viewers whose only triggers were stale or unreadable: a large gap with zero treatment.\n'
      % (pct(n['exposed_rate']), n['exposed_n'], pct(n['others_rate']), n['others_n'],
         pct(n['holdout_fresh_rate']), n['holdout_fresh_n'], pct(n['holdout_stale_rate']), n['holdout_stale_n']))
    w('## 3. The earlier pilot, re-analysed\n')
    t, h, s, ns = pil['treatment'], pil['holdout'], pil['served'], pil['not_served']
    w('| Comparison | Group A | Group B | Difference |')
    w('|---|---|---|---|')
    w('| Report headline: served vs everyone else | %s (%d) | %s (%d) | %+.1f pts |' % (
        pct(s[1] / s[0]), s[0], pct(ns[1] / ns[0]), ns[0], 100 * (s[1] / s[0] - ns[1] / ns[0])))
    w('| **Randomized: treatment arm vs holdout arm (ITT)** | %s (%d) | %s (%d) | **%+.2f pts ±%.2f (p=%.2f)** |' % (
        pct(t[1] / t[0]), t[0], pct(h[1] / h[0]), h[0], pil['itt']['diff_pts'], pil['itt']['ci95_pts'],
        pil['itt']['p_value']))
    w('\nThe report\'s 2.3x "uplift" compares 217 served viewers with all other viewers, including the whole holdout. '
      'Who got served was not random (the agent served viewers who showed up and qualified), and goals differ '
      'hugely in base rate (%s). The randomized comparison shows no benefit, and the point estimate is slightly '
      'negative. The pilot does **not** support rolling the agent out to every user.\n'
      % ', '.join('%s %s' % (g, pct(r)) for g, r in sorted(pil['goal_base_rates'].items())))
    w('## 4. Safety counters from the simulator\n')
    c = sim_report.get('counts', {})
    w('- Policy problems flagged by the simulator: **%d**' % sim_report.get('policy_problem_count', 0))
    w('- Actions on holdout viewers: %d' % c.get('holdout_actions_ignored', 0))
    w('- Pages accepted / shown personalised / published but not seen: %d / %d / %d' % (
        c.get('home_page_accepted', 0), c.get('home_page_shown_personalised', 0),
        c.get('home_page_published_but_not_shown', 0)))
    w('- Rows dropped by the platform: %d; pages rejected empty: %d' % (
        c.get('home_page_row_dropped_below_minimum', 0), c.get('home_page_rejected_empty', 0)))
    w('- Push requested / delivered / clicked / bad token: %d / %d / %d / %d' % (
        c.get('push_requested', 0), c.get('push_delivered', 0), c.get('push_clicked', 0),
        c.get('push_undeliverable_token', 0)))
    w('- WhatsApp requested / delivered: %d / %d' % (c.get('whatsapp_requested', 0), c.get('whatsapp_delivered', 0)))
    return '\n'.join(L) + '\n'
