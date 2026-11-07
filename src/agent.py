"""One agent per viewer.

`Runtime` routes every trigger, live event and control event to the owning `ViewerAgent`. Only a viewer's agent
writes that viewer's goals and actions, so two components can never pursue different goals for one viewer.

Decision flow for one trigger (each step can end in a recorded NO_ACTION):

    idempotency -> freshness -> data-store read -> facts -> goal (keep or choose) -> model second opinion
    -> independent goal check -> plan page (+ at most one message) -> independent action checks -> publish
"""
import json
import time
from datetime import timedelta

from .content import build_page
from .facts import resolve
from .timeutil import buffered_seconds, iso, ist_day, next_ist_time, true_time, ts

STALE_TRIGGER_S = 600       # an app open held offline longer than this: the viewer is no longer in that session
PAGE_TTL = timedelta(minutes=120)
MODEL_SKIP_QUEUE_MS = 10_000  # under load, skip the optional model call instead of growing the queue
MESSAGE_HOUR_IST = 19         # one evening slot, well outside quiet hours


class Config:
    def __init__(self, **kw):
        self.max_model_calls = kw.get('max_model_calls', 8)
        self.max_tool_calls = kw.get('max_tool_calls', 20)
        self.model_timeout_ms = kw.get('model_timeout_ms', 2500)
        self.latency_budget_ms = kw.get('latency_budget_ms', 60_000)
        self.messages_enabled = kw.get('messages_enabled', True)


class Runtime:
    def __init__(self, pack, store, goalbook, verifier, model, datastore, config=None):
        self.pack, self.store, self.goalbook, self.verifier = pack, store, goalbook, verifier
        self.model, self.datastore = model, datastore
        self.cfg = config or Config()
        self.agents = {}
        self.n_dec = self.n_act = self.n_goal = 0

    def agent(self, user_id):
        a = self.agents.get(user_id)
        if a is None:
            a = self.agents[user_id] = ViewerAgent(user_id, self)
        return a

    def next_id(self, kind):
        if kind == 'D':
            self.n_dec += 1
            return 'D-%05d' % self.n_dec
        if kind == 'A':
            self.n_act += 1
            return 'A-%05d' % self.n_act
        self.n_goal += 1
        return 'G-%05d' % self.n_goal

    # ------------------------------------------------------------------ inputs
    def on_trigger(self, trig, start, queue_wait_ms=0):
        d = self.agent(trig['user_id']).decide(trig, start, queue_wait_ms)
        self.store.save_decision(d)
        self.store.commit()
        return d

    def on_live_event(self, e):
        if not self.pack.history.add(dict(e)):
            return
        self.agent(e['user_id']).observe(e)
        self.store.commit()

    def on_control(self, c):
        at = ts(c['at'])
        self.store.journal(c['at'], None, 'control_event', c)
        if c['type'] in ('dependency_unavailable', 'dependency_degraded'):
            self.datastore.deps.add(c)
        elif c['type'] == 'title_withdrawn':
            self._title_withdrawn(c, at)
        elif c['type'] == 'goal_rule_corrected':
            self._goal_rule_corrected(c, at)
        self.store.commit()

    def _title_withdrawn(self, c, at):
        tid = c['title_id']
        self.pack.withdrawn[tid] = at
        affected = self.store.actions_with_title(tid)
        summary = {'title_id': tid, 'pages_republished': 0, 'pages_left_without_replacement': 0,
                   'messages_cancelled': 0, 'users': []}
        for a in affected:
            if a['type'] == 'home_page' and a['status'] == 'published' and ts(a['live_until']) > at:
                ok = self.agent(a['user_id']).republish_without(a, tid, at)
                summary['pages_republished' if ok else 'pages_left_without_replacement'] += 1
                summary['users'].append(a['user_id'])
            elif a['status'] == 'scheduled' and ts(a['send_at']) > at:
                self.store.set_action_status(a['action_id'], 'cancelled')
                self.store.journal(iso(at), a['user_id'], 'action_cancelled',
                                   {'action_id': a['action_id'], 'reason': 'title %s withdrawn' % tid})
                summary['messages_cancelled'] += 1
                summary['users'].append(a['user_id'])
        self.store.journal(iso(at), None, 'title_withdrawn_impact', summary)

    def _goal_rule_corrected(self, c, at):
        gid = c['goal_id']
        v = self.goalbook.correct(gid, c['new_success'], at)
        open_goals = self.store.goals("goal_id=? AND status='open'", (gid,))
        for g in open_goals:
            old = g['rule_version']
            g['rule_version'] = v['version']
            self.store.save_goal(g)
            self.store.journal(iso(at), g['user_id'], 'goal_rule_corrected',
                               {'instance_id': g['instance_id'], 'goal_id': gid, 'from_version': old,
                                'to_version': v['version'], 'new_success': self.goalbook.signal_text(gid)})
        # The correction applies to open goals whatever it says. If it cannot be judged, say so now: those goals will
        # be judged inconclusive, and no new viewer is committed to this goal while the rule stays unmeasurable.
        measurable, why = self.goalbook.measurable(gid)
        self.store.journal(iso(at), None, 'goal_rule_correction_impact',
                           {'goal_id': gid, 'version': v['version'], 'open_goals_updated': len(open_goals),
                            'viewers_touched': len({g['user_id'] for g in open_goals}),
                            'measurable': measurable, 'measurable_reason': why,
                            'note': 'only open %s goals re-pointed; no other viewer reprocessed' % gid})


class ViewerAgent:
    def __init__(self, user_id, rt):
        self.user_id = user_id
        self.rt = rt

    # ------------------------------------------------------------------ live events (replanning)
    def observe(self, e):
        st, u = self.rt.store, self.user_id
        at = e['received_at']
        if e['event'] == 'purchase_completed':
            g = st.open_goal(u)
            props = e.get('properties') or {}
            if g and g['goal_id'] in ('G04', 'G05', 'G06') and not props.get('is_trial'):
                g.update(status='closed', closed_at=at, close_reason='success_signal_observed_live:event:%s' % e['event_id'])
                st.save_goal(g)
            cancelled = self._cancel_scheduled(at, lambda a: True, 'viewer purchased; pending messages no longer fit')
            last = st.decisions_for(u)[-1:] if g else []
            st.journal(at, u, 'replan_purchase', {
                'event': 'event:%s' % e['event_id'], 'plan_id': props.get('plan_id'), 'is_trial': props.get('is_trial'),
                'goal_closed': g['instance_id'] if g and g['status'] == 'closed' else None,
                'messages_cancelled': cancelled,
                'seconds_after_last_decision': int((ts(at) - ts(last[0]['trigger']['decided_at'])).total_seconds())
                if last and last[0]['trigger'].get('decided_at') else None,
                'note': 'subscription fact now comes from this event, overriding the snapshot'})
        elif e['event'] == 'notification_opt_out':
            ch = (e.get('properties') or {}).get('channel')
            n = self._cancel_scheduled(at, lambda a: a['type'] == ch, 'viewer opted out of %s' % ch)
            st.journal(at, u, 'consent_withdrawn', {'channel': ch, 'messages_cancelled': n})
        elif e['event'] in ('signup_completed', 'app_dialect_changed', 'subscription_lapsed'):
            st.journal(at, u, 'fact_changed', {'event': e['event'], 'ref': 'event:%s' % e['event_id'],
                                               'properties': e.get('properties')})

    def _cancel_scheduled(self, at, pred, reason):
        n = 0
        for a in self.rt.store.actions_for(self.user_id, ('scheduled',)):
            if ts(a['send_at']) > ts(at) and pred(a):
                self.rt.store.set_action_status(a['action_id'], 'cancelled')
                self.rt.store.journal(at, self.user_id, 'action_cancelled', {'action_id': a['action_id'], 'reason': reason})
                n += 1
        return n

    def republish_without(self, a, tid, at):
        body = json.loads(a['body'])
        new = dict(body, action_id=self.rt.next_id('A'), published_at=iso(at + timedelta(seconds=1)),
                   revision_of=a['action_id'])
        new['rows'] = [dict(r, title_ids=[t for t in r['title_ids'] if t != tid]) for r in body.get('rows', [])]
        if new.get('hero_title_id') == tid:
            new['hero_title_id'] = None
        trig = {'app_dialect': a['dialect']}
        passed, failed, clean = self.rt.verifier.check_page(self.user_id, new, trig, [])
        self.rt.store.set_action_status(a['action_id'], 'superseded')
        if clean is None:
            self.rt.store.journal(iso(at), self.user_id, 'page_withdrawn_title',
                                  {'action_id': a['action_id'], 'result': 'no valid replacement', 'failed': failed})
            return False
        live_until = iso(ts(clean['published_at']) + PAGE_TTL)
        self.rt.store.save_action(clean, 'published', live_until=min(live_until, a['live_until']))
        self.rt.store.journal(iso(at), self.user_id, 'page_republished',
                              {'old': a['action_id'], 'new': clean['action_id'], 'removed_title': tid})
        return True

    # ------------------------------------------------------------------ triggers
    def decide(self, trig, start, queue_wait_ms):
        rt, st, pack = self.rt, self.rt.store, self.rt.pack
        t0 = time.perf_counter()
        sim_ms = 0
        uid, eid = self.user_id, trig['event_id']
        deliver = ts(trig['deliver_at'] or trig['received_at'])
        arm = pack.arms.get(uid, 'unknown')
        d = {
            'decision_id': rt.next_id('D'), 'viewer_id': uid, 'arm': arm,
            'trigger': {'event': trig['event'], 'event_id': eid, 'session_id': trig.get('session_id'),
                        'received_at': trig['received_at'], 'deliver_at': iso(deliver),
                        'event_age_s': int((start - true_time(trig)).total_seconds()),
                        'buffered_offline_s': int(buffered_seconds(trig))},
            'goal': None, 'action': 'NONE', 'action_ids': [], 'no_action_reason': None,
            'dialect': None, 'content': [], 'confidence': None, 'confidence_reason': [],
            'evidence': [], 'evidence_detail': [], 'assumptions': [], 'conflicts': [],
            'model': {'called': False}, 'budget': {'model_calls': 0, 'tool_calls': 0},
            'checks': {'passed': [], 'failed': []}, 'status': None,
        }
        budget = d['budget']

        def finish(status, reason=None):
            nonlocal sim_ms
            elapsed = int((time.perf_counter() - t0) * 1000)
            d['decided_in_ms'] = elapsed + sim_ms
            d['queue_wait_ms'] = queue_wait_ms
            pub = start + timedelta(milliseconds=d['decided_in_ms'])
            d['trigger']['decided_at'] = iso(pub)
            d['published_at'] = iso(pub) if status == 'PUBLISHED' else None
            d['publish_delay_ms'] = queue_wait_ms + d['decided_in_ms']
            d['within_budget'] = d['publish_delay_ms'] <= rt.cfg.latency_budget_ms
            d['status'] = status
            if reason:
                d['no_action_reason'] = reason
            d['evidence'] = list(dict.fromkeys(d['evidence']))
            seen = set()
            d['evidence_detail'] = [x for x in d['evidence_detail'] if not (x['ref'] in seen or seen.add(x['ref']))]
            return d

        # 1. idempotency: same event delivered twice, or a second trigger (login + app_open) in one session.
        prev = st.trigger_seen(eid)
        if prev:
            return finish('SKIPPED_DUPLICATE', 'event_id already handled by %s' % prev)
        pack.history.add(dict(trig))
        prev = st.session_seen(uid, trig.get('session_id'))
        if prev:
            st.mark_trigger(eid, uid, None, d['decision_id'], session_too=False)
            return finish('SKIPPED_SAME_SESSION', 'session %s already handled by %s' % (trig.get('session_id'), prev))
        st.mark_trigger(eid, uid, trig.get('session_id'), d['decision_id'])
        d['evidence'].append('event:%s' % eid)
        d['evidence_detail'].append({'ref': 'event:%s' % eid, 'source': 'trigger', 'age_s': d['trigger']['event_age_s']})

        # 2. freshness: an app open that sat on the phone for a long time is history, not a visit.
        if d['trigger']['buffered_offline_s'] > STALE_TRIGGER_S:
            d['assumptions'].append('true event time = received_at - (sent_at - occurred_at); phone clocks can be '
                                    'wrong but the gap between two readings of the same clock is still usable')
            return finish('NO_ACTION', 'stale_trigger: app open happened %ds before it reached us (held offline); '
                                       'the viewer has left that session, a page now would not be seen'
                          % d['trigger']['buffered_offline_s'])

        # 3. data store read (simulated latency / failures from control events).
        ok, lat = rt.datastore.read(uid, start, eid)
        budget['tool_calls'] += 1
        sim_ms += lat
        if not ok:
            d['checks']['failed'].append('data_store_read: timed out after %dms' % lat)
            return finish('NO_ACTION', 'data_store_unavailable: cannot verify consent, eligibility or history; '
                                       'acting blind is worse than not acting')
        f = resolve(pack, uid, trig, start)
        budget['tool_calls'] += 3  # profile, history, catalogue lookups
        st.save_facts(uid, d['decision_id'], f, iso(start))
        page_dialect = f['page_dialect']['value']
        d['dialect'] = {'used': page_dialect, 'source': 'app_setting', 'conflicts': f['page_dialect']['conflicts'],
                        'resolution': f['page_dialect']['rule']}
        for k in ('page_dialect', 'subscription_status', 'signup_at', 'profile_present', 'consent_push'):
            if f[k]['conflicts']:
                d['conflicts'].append({'fact': k, 'used': f[k]['value'], 'source': f[k]['source'],
                                       'other_sources': f[k]['conflicts'], 'rule': f[k]['rule']})
        for k in ('subscription_status', 'signup_at', 'profile_present'):
            if f[k]['ref']:
                d['evidence_detail'].append({'ref': f[k]['ref'], 'source': f[k]['source'], 'age_s': f[k]['age_s']})

        # 4. goal: keep the open goal while it runs; otherwise choose one.
        goal = st.open_goal(uid)
        if goal and ts(goal['deadline']) <= start:
            goal.update(status='closed', closed_at=iso(start), close_reason='deadline_passed')
            st.save_goal(goal)
            goal = None
        model_answer = None
        if goal:
            d['goal'] = self._goal_view(goal, carried=True)
            # A running goal can stop being "eligible" because the viewer is making progress (e.g. first play);
            # that is not a reason to drop it. Only re-plan when the goal became impossible.
            if goal['goal_id'] in ('G04', 'G05', 'G06') and f['subscription_status']['value'] == 'active':
                goal.update(status='closed', closed_at=iso(start), close_reason='superseded:viewer now active')
                st.save_goal(goal)
                goal, d['goal'] = None, None
        if goal is None:
            cands = rt.goalbook.candidates(f, start)
            if not cands:
                d['assumptions'].append('G10 and G11 are never chosen (not measurable)')
                return finish('NO_ACTION', 'no_eligible_goal: none of the measurable goals applies to this viewer '
                                           'on current evidence')
            chosen = cands[0]
            if arm == 'agent' and queue_wait_ms < MODEL_SKIP_QUEUE_MS:
                summary = {'subscription_status': f['subscription_status']['value'],
                           'days_since_signup': None, 'ever_played': f['ever_played']['value'],
                           'ever_completed': f['ever_completed']['value']}
                model_answer, rec = rt.model.ask_goal(cands, summary, {'now': start, 'trigger_event_id': eid}, budget)
                sim_ms += rec['latency_ms']
                d['model'] = rec
                if model_answer and model_answer['goal_id'] in [c['goal_id'] for c in cands]:
                    chosen = [c for c in cands if c['goal_id'] == model_answer['goal_id']][0]
            elif arm == 'agent':
                d['model'] = {'called': False, 'outcome': 'skipped_under_load'}
            gp, gf = rt.verifier.check_goal(uid, trig, chosen['goal_id'], model_answer, start)
            d['checks']['passed'] += ['%s' % n for n, _ in gp]
            d['checks']['failed'] += ['%s: %s' % (n, why) for n, why in gf]
            if any(n in ('eligibility', 'goal_measurable', 'evidence_present') for n, _ in gf):
                return finish('NO_ACTION', 'goal_failed_independent_check')
            goal = {'instance_id': rt.next_id('G'), 'user_id': uid, 'goal_id': chosen['goal_id'], 'arm': arm,
                    'status': 'open', 'rule_version': rt.goalbook.current(chosen['goal_id'])['version'],
                    'started_at': None, 'deadline': None, 'decision_id': d['decision_id'],
                    'params': rt.goalbook.params_for(chosen['goal_id'], f, start), 'eligibility': chosen}
            d['evidence'] += [e for e in chosen['evidence'] if e]
            d['goal_reason'] = chosen['reason']
            d['other_eligible_goals'] = [c['goal_id'] for c in cands if c['goal_id'] != chosen['goal_id']]
        self._assumptions(d, goal, f)

        # 5. holdout: the goal is recorded (shadow) so the arms can be compared; nothing is ever sent.
        if arm != 'agent':
            d['checks']['passed'].append('holdout: no action for %s arm' % arm)
            self._commit_goal(goal, d, start, sim_ms, t0)
            d['goal']['shadow'] = True
            return finish('NO_ACTION', 'holdout_arm: randomized holdout receives no agent action (policy rule 5)')

        # 6. plan the home page in the dialect the viewer will actually see.
        if page_dialect not in pack.dialects:
            self._commit_goal(goal, d, start, sim_ms, t0)
            d['checks']['failed'].append('dialect: app setting %r is not a catalogue dialect' % page_dialect)
            return finish('NO_ACTION', 'invalid_app_dialect: the app is set to %r, which has no catalogue; the app '
                                       'would not show a page in any other dialect, so there is nothing safe to '
                                       'publish' % page_dialect)
        hero, rows, notes = build_page(pack, f, goal['goal_id'], goal['params'], page_dialect,
                                       f['app_language']['value'], start)
        budget['tool_calls'] += 1
        d['planner_notes'] = notes
        provisional = start + timedelta(milliseconds=int((time.perf_counter() - t0) * 1000) + sim_ms + 5)
        page = {'action_id': None, 'decision_id': d['decision_id'], 'user_id': uid, 'trigger_event_id': eid,
                'type': 'home_page', 'published_at': iso(provisional), 'dialect': page_dialect,
                'hero_title_id': hero, 'rows': rows}
        existing = st.actions_for(uid)
        pp, pf, clean = rt.verifier.check_page(uid, page, trig, existing)
        budget['tool_calls'] += 1
        d['checks']['passed'] += ['page:%s' % n for n, _ in pp]
        d['checks']['failed'] += ['page:%s: %s' % (n, why) for n, why in pf]
        actions = []
        if clean:
            clean['action_id'] = rt.next_id('A')
            actions.append(clean)

        # 7. at most one message, only where it is proportionate to the goal.
        msg = self._plan_message(goal, f, clean or page, start) if rt.cfg.messages_enabled else None
        days = {ist_day(ts(a['send_at'])) for a in existing if a['type'] in ('push', 'whatsapp')
                and a['status'] in ('scheduled', 'published')}
        if msg and ist_day(ts(msg['send_at'])) in days:
            d['planner_notes'].append('message not planned: one is already scheduled for %s' % ist_day(ts(msg['send_at'])))
            msg = None
        if msg:
            mp, mf = rt.verifier.check_message(uid, msg, f, days)
            budget['tool_calls'] += 1
            d['checks']['passed'] += ['message:%s' % n for n, _ in mp]
            d['checks']['failed'] += ['message:%s: %s' % (n, why) for n, why in mf]
            if not mf:
                msg['action_id'] = rt.next_id('A')
                actions.append(msg)
        if budget['tool_calls'] > rt.cfg.max_tool_calls:
            d['checks']['failed'].append('budget: %d tool calls' % budget['tool_calls'])
            actions = []

        if not actions:
            self._commit_goal(goal, d, start, sim_ms, t0)
            reason = 'no_valid_action: ' + ('; '.join(notes) or 'page failed checks')
            if any('duplicate_action' in x for x in d['checks']['failed']):
                reason = 'page_already_live: an identical page for this dialect is still live; nothing to change'
            return finish('NO_ACTION', reason)

        # 8. publish.
        self._commit_goal(goal, d, start, sim_ms, t0)
        res = finish('PUBLISHED')
        pub = res['published_at']
        kinds = []
        for a in actions:
            a['decision_id'] = d['decision_id']
            if a['type'] == 'home_page':
                a['published_at'] = pub
                st.save_action(a, 'published', live_until=iso(ts(pub) + PAGE_TTL))
                kinds.append('HOME_PAGE')
                d['content'] = (['title:%s' % a['hero_title_id']] if a['hero_title_id'] else []) + \
                               ['title:%s' % t for r in a['rows'] for t in r['title_ids']]
                d['hero'] = a['hero_title_id']
                d['rows'] = [{'row_type': r['row_type'], 'n': len(r['title_ids'])} for r in a['rows']]
                d['evidence'] += ['catalogue:%s' % t[6:] for t in d['content'][:3]]
            else:
                a['published_at'] = pub
                st.save_action(a, 'scheduled')
                kinds.append(a['type'].upper() + '@' + a['send_at'])
            d['action_ids'].append(a['action_id'])
        d['action'] = '+'.join(k.split('@')[0] for k in kinds)
        d['message_schedule'] = [k for k in kinds if '@' in k]
        self._confidence(d, f, goal, actions)
        return res

    # ------------------------------------------------------------------ helpers
    def _goal_view(self, g, carried=False):
        gb = self.rt.goalbook
        return {'id': g['goal_id'], 'name': gb.goals[g['goal_id']]['name'], 'instance_id': g['instance_id'],
                'started_at': g['started_at'], 'deadline': g['deadline'], 'rule_version': g['rule_version'],
                'success_signal': gb.signal_text(g['goal_id'], g['rule_version']), 'carried_over': carried,
                'params': g.get('params') or {}}

    def _commit_goal(self, goal, d, start, sim_ms, t0):
        if goal.get('started_at') is None:
            commit = start + timedelta(milliseconds=int((time.perf_counter() - t0) * 1000) + sim_ms)
            goal['started_at'] = iso(commit)
            goal['deadline'] = iso(commit + self.rt.goalbook.window(goal['goal_id']))
            self.rt.store.save_goal(goal)
            self.rt.store.journal(goal['started_at'], self.user_id, 'goal_committed',
                                  {'instance_id': goal['instance_id'], 'goal_id': goal['goal_id'],
                                   'arm': goal['arm'], 'decision_id': d['decision_id']})
        d['goal'] = self._goal_view(goal, carried=d['goal'] is not None and d['goal'].get('carried_over', False))

    def _plan_message(self, goal, f, page, start):
        pack = self.rt.pack
        gid = goal['goal_id']
        send_at = next_ist_time(start, MESSAGE_HOUR_IST)
        if self.rt.store.learned(self.user_id, 'push_undeliverable'):
            return None  # an earlier push to this viewer failed with an invalid token
        dialect = f['page_dialect']['value']
        if gid == 'G07' and f['consent_push']['value']:
            from .content import pick_variant
            tid = pick_variant(pack, goal['params']['slug'], f['app_language']['value'], dialect, send_at)
            if not tid:
                return None
            name = pack.titles[tid]['name']
            if len(name) > 40:
                return None
            return {'action_id': None, 'decision_id': None, 'user_id': self.user_id, 'trigger_event_id': None,
                    'type': 'push', 'send_at': iso(send_at), 'title_id': tid,
                    'text': {'title': name, 'body': 'Episode %d aapka intezaar kar raha hai. Wahin se dekhiye jahan '
                                                    'chhoda tha.' % goal['params']['next_episode_number']},
                    'deeplink': 'ott://title/%s' % tid}
        if gid == 'G12' and f['consent_push']['value'] and page.get('hero_title_id'):
            tid = page['hero_title_id']
            if not pack.available(tid, send_at) or len(pack.titles[tid]['name']) > 40:
                return None
            name = pack.titles[tid]['name']
            return {'action_id': None, 'decision_id': None, 'user_id': self.user_id, 'trigger_event_id': None,
                    'type': 'push', 'send_at': iso(send_at), 'title_id': tid,
                    'text': {'title': name, 'body': 'Aapke liye chuna gaya: %s. Abhi dekhiye.' % name},
                    'deeplink': 'ott://title/%s' % tid}
        if gid == 'G04' and f['consent_whatsapp']['value']:
            price = pack.plans['P_MONTH']['price_inr']
            return {'action_id': None, 'decision_id': None, 'user_id': self.user_id, 'trigger_event_id': None,
                    'type': 'whatsapp', 'send_at': iso(send_at), 'template_id': 'wa_trial_ending_v1',
                    'variables': {'plan_price': '₹%d' % price}, 'title_id': None}
        return None

    def _assumptions(self, d, goal, f):
        a = d['assumptions']
        a.append('the viewer will see the page for the app-setting dialect %s' % f['page_dialect']['value'])
        if not f['profile_present']['value']:
            a.append('no profile row (new viewer): subscription treated as never-paid, no message consent')
        if goal['goal_id'] == 'G06':
            a.append('"recently lapsed" = lapsed within the last 30 days')
        if goal['goal_id'] == 'G07':
            a.append('viewer has not continued %s on another device we cannot see' % goal['params'].get('slug'))
        if goal['goal_id'] in ('G01', 'G02'):
            a.append('history before 2026-09-01 is not in the pack; "never played/completed" means not in 30 days')
        a.append('popularity = play counts in the 30-day history')

    def _confidence(self, d, f, goal, actions):
        c, why = 0.7, []
        if f['page_dialect']['conflicts']:
            c -= 0.1
            why.append('dialect sources disagree (%s); used app setting' % ', '.join(f['page_dialect']['conflicts']))
        if not f['profile_present']['value']:
            c -= 0.1
            why.append('no profile row')
        if f['subscription_status']['conflicts']:
            c -= 0.05
            why.append('subscription snapshot contradicted by events')
        if d['model'].get('outcome') not in (None, 'ok'):
            c -= 0.05
            why.append('model %s: rules-only decision' % d['model'].get('outcome'))
        if goal['goal_id'] in ('G04', 'G05', 'G06'):
            c -= 0.15
            why.append('purchase goals move little from a page')
        rows = d.get('rows') or []
        if len(rows) < 3:
            c -= 0.1
            why.append('thin page (%d rows) in %s' % (len(rows), d['dialect']['used']))
        if not why:
            why.append('fresh trigger, consistent sources, full page')
        d['confidence'] = round(max(0.1, min(0.95, c)), 2)
        d['confidence_reason'] = why
        d['evidence'] = list(dict.fromkeys(d['evidence']))
