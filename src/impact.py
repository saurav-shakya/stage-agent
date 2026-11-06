"""Impact measurement against the randomized holdout (intention-to-treat), plus the pilot re-analysis.

Unit = viewer who sent at least one trigger on the replay day. Every such viewer counts in their randomized arm,
whatever the agent then did (stale trigger, no eligible goal, page not seen...). Outcome metrics are defined the
same way for both arms and do not depend on what the agent chose, so the comparison cannot be biased by the agent
selecting viewers who were already likely to convert.
"""
import csv
import math
import os
from collections import defaultdict
from datetime import timedelta

from .timeutil import ist_day, ts


def _ci(p1, n1, p0, n0):
    if not n1 or not n0:
        return 0.0, 0.0, 1.0
    diff = p1 - p0
    se = math.sqrt(p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0)
    if se == 0:
        return diff, 0.0, 1.0
    z = diff / se
    pval = math.erfc(abs(z) / math.sqrt(2))
    return diff, 1.96 * se, pval


def metric_rows(arm_users, flags):
    rows = []
    for name, f in flags.items():
        a = [f(u) for u in arm_users['agent']]
        h = [f(u) for u in arm_users['holdout']]
        pa, ph = sum(a) / max(1, len(a)), sum(h) / max(1, len(h))
        diff, half, p = _ci(pa, len(a), ph, len(h))
        rows.append({'metric': name, 'agent_n': len(a), 'agent_rate': pa, 'holdout_n': len(h), 'holdout_rate': ph,
                     'diff_pts': diff * 100, 'ci95_pts': half * 100, 'p_value': p})
    return rows


def compute(rt, events_by_user, outcomes):
    pack, st = rt.pack, rt.store
    decisions = st.all_decisions()
    first_trigger = {}
    for d in decisions:
        u = d['viewer_id']
        t = ts(d['trigger']['deliver_at'])
        if u not in first_trigger or t < first_trigger[u]:
            first_trigger[u] = t
    arm_users = defaultdict(list)
    for u in sorted(first_trigger):
        arm_users[pack.arms.get(u, 'unknown')].append(u)

    def any_event(name, hours, pred=lambda e: True):
        def f(u):
            t0 = first_trigger[u]
            return any(e['event'] == name and t0 <= ts(e['received_at']) <= t0 + timedelta(hours=hours) and pred(e)
                       for e in events_by_user.get(u, []))
        return f

    def returned_later_day(u):
        d0 = ist_day(first_trigger[u])
        return any(e['event'] == 'play_started' and ist_day(ts(e['received_at'])) > d0 and
                   ts(e['received_at']) <= first_trigger[u] + timedelta(days=7) for e in events_by_user.get(u, []))

    first_goal = {}
    for o in sorted(outcomes, key=lambda o: o['started_at']):
        first_goal.setdefault(o['user_id'], o)

    flags = {
        'committed goal reached (first goal per viewer; holdout = shadow goal)':
            lambda u: first_goal.get(u, {}).get('status') == 'succeeded',
        'play_started within 72h': any_event('play_started', 72),
        'episode_completed within 72h': any_event('episode_completed', 72),
        'plays again on a later IST day within 7d': returned_later_day,
        'paid purchase within 14d': any_event('purchase_completed', 336, lambda e: not e['properties'].get('is_trial')),
    }
    itt = metric_rows(arm_users, flags)

    # Goal-level view: among viewers whose first goal was G, success rate per arm (same rules in both arms).
    per_goal = defaultdict(lambda: {'agent': [0, 0], 'holdout': [0, 0]})
    for u, o in first_goal.items():
        arm = o['arm'] if o['arm'] in ('agent', 'holdout') else None
        if arm:
            per_goal[o['goal_id']][arm][0] += o['status'] == 'succeeded'
            per_goal[o['goal_id']][arm][1] += 1

    # The misleading comparison, reproduced on purpose: exposed viewers vs everyone else.
    exposed = {o['user_id'] for o in outcomes if o['arm'] == 'agent' and o['pages_shown'] > 0}
    everyone = arm_users['agent'] + arm_users['holdout']
    f = flags['play_started within 72h']
    ex = [f(u) for u in everyone if u in exposed]
    rest = [f(u) for u in everyone if u not in exposed]
    naive = {'exposed_n': len(ex), 'exposed_rate': sum(ex) / max(1, len(ex)),
             'others_n': len(rest), 'others_rate': sum(rest) / max(1, len(rest))}
    # Why it is biased: exposure requires the viewer to come back to the home page while the page is live,
    # which is itself a strong predictor of playing. Show the same split inside the holdout using "would have been
    # exposed" = had a fresh trigger (not stale) on the replay day.
    fresh = {d['viewer_id'] for d in decisions if d['status'] in ('PUBLISHED', 'NO_ACTION')
             and (d['no_action_reason'] or '').split(':')[0] not in ('stale_trigger', 'data_store_unavailable')}
    h_fresh = [f(u) for u in arm_users['holdout'] if u in fresh]
    h_stale = [f(u) for u in arm_users['holdout'] if u not in fresh]
    naive['holdout_fresh_rate'] = sum(h_fresh) / max(1, len(h_fresh))
    naive['holdout_fresh_n'] = len(h_fresh)
    naive['holdout_stale_rate'] = sum(h_stale) / max(1, len(h_stale))
    naive['holdout_stale_n'] = len(h_stale)
    return {'arms': {k: len(v) for k, v in arm_users.items()}, 'itt': itt, 'per_goal': dict(per_goal),
            'naive': naive}


def pilot(pack_dir):
    rows = list(csv.DictReader(open(os.path.join(pack_dir, 'pilot/pilot_outcomes.csv'), encoding='utf-8')))
    out = {}
    for arm in ('treatment', 'holdout'):
        r = [x for x in rows if x['arm'] == arm]
        out[arm] = (len(r), sum(x['succeeded'] == 'true' for x in r))
    served = [x for x in rows if x['served'] == 'true']
    others = [x for x in rows if x['served'] != 'true']
    out['served'] = (len(served), sum(x['succeeded'] == 'true' for x in served))
    out['not_served'] = (len(others), sum(x['succeeded'] == 'true' for x in others))
    t_n, t_s = out['treatment']
    h_n, h_s = out['holdout']
    diff, half, p = _ci(t_s / t_n, t_n, h_s / h_n, h_n)
    out['itt'] = {'diff_pts': diff * 100, 'ci95_pts': half * 100, 'p_value': p}
    # goal mix of served vs not served: served viewers had easier goals
    mix = defaultdict(lambda: [0, 0])
    for x in rows:
        mix[x['goal_id']][0 if x['served'] == 'true' else 1] += 1
    out['goal_mix'] = dict(mix)
    out['served_rate_by_arm'] = {arm: sum(x['served'] == 'true' for x in rows if x['arm'] == arm) for arm in ('treatment', 'holdout')}
    goal_rates = defaultdict(lambda: [0, 0])
    for x in rows:
        goal_rates[x['goal_id']][0] += x['succeeded'] == 'true'
        goal_rates[x['goal_id']][1] += 1
    out['goal_base_rates'] = {g: v[0] / v[1] for g, v in goal_rates.items()}
    return out
