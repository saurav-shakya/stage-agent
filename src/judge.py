"""Closes the loop: judges every goal from the simulator's outcome events and records what was learned.

Only attributable signals count. A goal is `succeeded` only if its success event (under the goal's current rule
version) was received inside the goal window. For the agent arm we also record whether the viewer had actually
been exposed to an agent action before the success, so a success the agent could not have caused is never
credited to it. Impact itself is measured arm-vs-arm (impact.py), not from these credits.
"""
import json
import os
from collections import defaultdict
from datetime import timedelta

from .timeutil import iso, ts


def load_outcome_events(sim_dir):
    by_user = defaultdict(list)
    with open(os.path.join(sim_dir, 'outcome_events.jsonl'), encoding='utf-8') as f:
        for line in f:
            if line.strip():
                e = json.loads(line)
                by_user[e['user_id']].append(e)
    for v in by_user.values():
        v.sort(key=lambda e: e['received_at'])
    return by_user


def judge_all(rt, sim_dir):
    st, gb, pack = rt.store, rt.goalbook, rt.pack
    events = load_outcome_events(sim_dir)
    last = max((e['received_at'] for v in events.values() for e in v), default=None)
    horizon_end = ts(last) if last else None
    outcomes = []
    for g in st.goals():
        u = g['user_id']
        evs = events.get(u, [])
        status, ev, detail = gb.judge(g, evs, pack.titles, horizon_end or ts(g['deadline']) + timedelta(days=1))
        ours = {a['action_id'] for a in st.actions_for(u)}
        start, deadline = ts(g['started_at']), ts(g['deadline'])
        shown = [e for e in evs if e['event'] == 'home_page_shown' and e['properties'].get('personalised')
                 and e['properties'].get('page_action_id') in ours and start <= ts(e['received_at']) <= deadline]
        clicked = [e for e in evs if e['event'] == 'message_clicked' and e['properties'].get('action_id') in ours]
        delivered = [e for e in evs if e['event'] == 'message_delivered' and e['properties'].get('action_id') in ours]
        failed_msgs = [e for e in evs if e['event'] == 'message_failed' and e['properties'].get('action_id') in ours]
        exposures = sorted([ts(e['received_at']) for e in shown + clicked])
        first_exposure = exposures[0] if exposures else None
        success_at = None
        if status == 'succeeded':
            success_at = min(ts(x['received_at']) for x in evs if 'outcome_event:%s' % x['event_id'] in ev)
        if g['arm'] != 'agent':
            attribution = 'holdout_shadow_goal'
        elif status != 'succeeded':
            attribution = 'n/a'
        elif first_exposure and first_exposure <= success_at:
            attribution = 'after_exposure'
        else:
            attribution = 'without_prior_exposure (not credited to the agent)'
        learning = _learning(g, status, attribution, shown, delivered, failed_msgs, st.actions_for(u))
        o = {'instance_id': g['instance_id'], 'user_id': u, 'goal_id': g['goal_id'], 'arm': g['arm'],
             'decision_id': g['decision_id'], 'rule_version': g['rule_version'],
             'success_signal': gb.signal_text(g['goal_id'], g['rule_version']),
             'started_at': g['started_at'], 'deadline': g['deadline'], 'status': status, 'detail': detail,
             'evidence': ev, 'success_at': iso(success_at), 'first_exposure_at': iso(first_exposure),
             'pages_shown': len(shown), 'messages_delivered': len(delivered), 'messages_clicked': len(clicked),
             'message_failures': [e['properties'].get('reason') for e in failed_msgs],
             'attribution': attribution, 'learning': learning, 'closed_early': g.get('close_reason')}
        st.save_outcome(o)
        # Feed results back into the viewer's state so the next decision uses them.
        learned = {'last_goal_result': {'value': {'goal_id': g['goal_id'], 'status': status,
                                                  'attribution': attribution},
                                        'source': 'outcome', 'ref': 'outcome:%s' % g['instance_id'],
                                        'observed_at': g['deadline'], 'age_s': 0}}
        if any(e['properties'].get('reason') == 'token_invalid' for e in failed_msgs):
            learned['push_undeliverable'] = {'value': True, 'source': 'outcome', 'ref': 'outcome:%s' % g['instance_id'],
                                             'observed_at': g['deadline'], 'age_s': 0}
        if g['arm'] == 'agent' and status == 'failed' and not shown:
            learned['page_not_seen'] = {'value': True, 'source': 'outcome', 'ref': 'outcome:%s' % g['instance_id'],
                                        'observed_at': g['deadline'], 'age_s': 0}
        st.save_facts(u, None, {'learned:' + k: v for k, v in learned.items()}, g['deadline'])
        g.update(status='judged:' + status, closed_at=g.get('closed_at') or g['deadline'])
        st.save_goal(g)
        st.journal(g['deadline'], u, 'learning', {'instance_id': g['instance_id'], 'learning': learning})
        outcomes.append(o)
    st.commit()
    return outcomes, horizon_end


def _learning(g, status, attribution, shown, delivered, failed_msgs, actions):
    pages = [a for a in actions if a['type'] == 'home_page']
    notes = []
    if g['arm'] != 'agent':
        return 'holdout: no action taken; outcome is the baseline for this kind of viewer'
    if not pages and not delivered:
        notes.append('agent took no action for this goal')
    elif pages and not shown:
        notes.append('page published but never seen while live: viewer did not reach home in time or changed dialect')
    elif shown:
        notes.append('page seen %d time(s)' % len(shown))
    for e in failed_msgs:
        r = e['properties'].get('reason')
        notes.append('message failed (%s): %s' % (r, 'do not retry push, prefer the home page'
                                                  if r == 'token_invalid' else 'check consent/template'))
    if status == 'succeeded':
        notes.append('goal reached ' + ('after exposure' if attribution == 'after_exposure'
                                        else 'without exposure: not credited'))
    elif status == 'failed':
        notes.append('goal not reached: next time try a different lever or a lower-effort goal')
    return '; '.join(notes)
