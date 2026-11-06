"""Goal catalogue: eligibility (from facts, with evidence) and success judging (from events), with rule versions.

Decisions about the catalogue itself, all recorded in KNOWN_LIMITATIONS.md:
- G10 is never chosen: its success signal is undefined ("engages meaningfully", event null).
- G11 is never chosen: its success event `tv_app_open` does not exist in the event stream.
- G06 "recently lapsed" is underspecified; we assume lapsed within the last 30 days, with a lapse date we can cite.
- G07 has no structured rule; we derive it from play/complete events + episodes.csv (see facts._unfinished).
- Success rules are versioned. A `goal_rule_corrected` control event adds a version; it applies to open goals of
  that goal_id only, so a correction never forces reprocessing of other viewers.
"""
from datetime import timedelta

from .timeutil import iso, ist_day, ts

# Order = which eligible goal the agent pursues first. Earlier goals are nearer-term and more directly movable by
# what the agent controls (home page, one message). A viewer who has never finished an episode gets that (G02)
# before resuming one particular series (G07). First purchase for never-paid viewers is last because the base
# purchase rate is tiny and a page barely moves it.
PRIORITY = ['G04', 'G01', 'G03', 'G06', 'G12', 'G02', 'G07', 'G09', 'G08', 'G05']
DISABLED = {
    'G10': 'success signal undefined (event null, condition "engages meaningfully"): not measurable',
    'G11': 'success event tv_app_open does not exist anywhere in the event stream: not measurable',
}
LAPSE_RECENT_DAYS = 30


class GoalBook:
    def __init__(self, cfg, titles=None, slug_titles=None):
        self.titles = titles or {}
        self.slug_titles = slug_titles or {}
        self.goals = {g['goal_id']: g for g in cfg['goals']}
        self.versions = {gid: [{'version': 1, 'success': dict(g['success']), 'effective_at': None}]
                         for gid, g in self.goals.items()}

    # ------------------------------------------------------------------ rule versions
    def current(self, gid):
        return self.versions[gid][-1]

    def rule(self, gid, version):
        return self.versions[gid][version - 1]

    def correct(self, gid, new_success, at):
        old = self.current(gid)['success']
        merged = dict(old)
        merged.update(new_success)
        merged.setdefault('window_hours', old['window_hours'])
        v = {'version': len(self.versions[gid]) + 1, 'success': merged, 'effective_at': iso(at)}
        self.versions[gid].append(v)
        return v

    def window(self, gid):
        return timedelta(hours=self.current(gid)['success']['window_hours'])

    def signal_text(self, gid, version=None):
        s = (self.rule(gid, version) if version else self.current(gid))['success']
        return '%s (%s) within %sh' % (s['event'], s['condition'], s['window_hours'])

    # ------------------------------------------------------------------ eligibility
    def eligibility(self, gid, f, now):
        """Returns (eligible, evidence_refs, reason). Uses only resolved facts, which carry their own provenance."""
        if gid in DISABLED:
            return False, [], DISABLED[gid]
        status = f['subscription_status']['value']
        sref = f['subscription_status']['ref'] or f['subscription_status']['source']
        signup = ts(f['signup_at']['value'])
        signup_ref = f['signup_at']['ref'] or f['signup_at']['source']
        rule = 'goal_rule:%s-v%d' % (gid, self.current(gid)['version'])

        def since_signup_days():
            return None if signup is None else (now - signup).total_seconds() / 86400

        if gid == 'G01':
            d = since_signup_days()
            if d is None or d > 7:
                return False, [], 'not signed up in the last 7 days (signup=%s)' % f['signup_at']['value']
            if f['ever_played']['value']:
                return False, [], 'has already started a play (%s)' % f['ever_played']['ref']
            return True, [signup_ref, rule], 'signed up %.1f days ago, no play_started in history' % d
        if gid == 'G02':
            if f['ever_completed']['value']:
                return False, [], 'already completed an episode'
            if not f['ever_played']['value'] and signup is None:
                return False, [], 'no history at all'
            return True, [f['ever_completed']['ref'], rule], 'no episode_completed in the 30-day history'
        if gid == 'G03':
            d = since_signup_days()
            if d is None or d > 2:
                return False, [], 'not signed up in the last 2 days'
            return True, [signup_ref, rule], 'signed up %.1f days ago' % d
        if gid == 'G04':
            if status != 'trial_active':
                return False, [], 'subscription_status=%s (%s)' % (status, sref)
            return True, [sref, rule], 'trial is active per %s' % sref
        if gid == 'G05':
            if status != 'never' or f['ever_paid']['value']:
                return False, [], 'subscription_status=%s' % status
            return True, [sref, rule], 'never paid per %s' % sref
        if gid == 'G06':
            if status != 'lapsed':
                return False, [], 'subscription_status=%s' % status
            lapsed = ts(f['lapsed_at']['value'])
            if lapsed is None:
                return False, [], 'lapsed but lapse date unknown; "recently" cannot be checked'
            days = (now - lapsed).total_seconds() / 86400
            if days > LAPSE_RECENT_DAYS:
                return False, [], 'lapsed %.0f days ago (> %d)' % (days, LAPSE_RECENT_DAYS)
            return True, [sref, f['lapsed_at']['ref'] or 'profile:plan_ends_at', rule], \
                'lapsed %.1f days ago (assumption: recent = %d days)' % (days, LAPSE_RECENT_DAYS)
        if gid == 'G07':
            cands = [s for s in f['unfinished_series']['value'] if s['idle_days'] >= 3]
            if not cands:
                return False, [], 'no unfinished series idle for 3+ days'
            # Actionable only if the series exists in the dialect the viewer will see; otherwise the agent could
            # neither show it nor name it, and the goal would be judged on something it cannot influence.
            d = f['page_dialect']['value']
            cands = [s for s in cands if any(self.titles[t]['dialect'] == d
                                             for t in self.slug_titles.get(s['slug'], []))]
            if not cands:
                return False, [], 'unfinished series exist but none in the app dialect %s' % d
            s = cands[0]
            return True, [s['evidence'], 'catalogue:%s' % s['title_id'], rule], \
                'unfinished %s (next ep %d of %d), idle %.1f days' % (s['slug'], s['next_episode_number'],
                                                                      s['episodes_total'], s['idle_days'])
        if gid == 'G08':
            if status != 'active':
                return False, [], 'not an active subscriber'
            return True, [sref, rule], 'active subscriber'
        if gid == 'G09':
            if status != 'active':
                return False, [], 'not an active subscriber'
            days = f['play_days_14d']['value']
            if len(days) > 1:
                return False, [], 'played on %d days in last 14' % len(days)
            return True, [sref, 'events:play_started(last 14d)=%d days' % len(days), rule], \
                'active subscriber, plays on %d day(s) in last 14' % len(days)
        if gid == 'G12':
            last = ts(f['last_app_open']['value'])
            if last is None:
                return False, [], 'never opened the app before: not a returning viewer'
            gap = (now - last).total_seconds() / 86400
            if gap < 14:
                return False, [], 'last app_open %.1f days ago' % gap
            return True, [f['last_app_open']['ref'], rule], 'no app_open for %.1f days' % gap
        return False, [], 'unknown goal'

    def candidates(self, f, now):
        out = []
        for gid in PRIORITY:
            ok, ev, why = self.eligibility(gid, f, now)
            if ok:
                out.append({'goal_id': gid, 'evidence': ev, 'reason': why})
        return out

    def params_for(self, gid, f, now):
        """Goal-specific parameters fixed at commit time (so the success check cannot drift later)."""
        if gid == 'G07':
            d = f['page_dialect']['value']
            s = [x for x in f['unfinished_series']['value'] if x['idle_days'] >= 3 and
                 any(self.titles[t]['dialect'] == d for t in self.slug_titles.get(x['slug'], []))][0]
            return {'slug': s['slug'], 'title_id': s['title_id'], 'next_episode_number': s['next_episode_number']}
        return {}

    # ------------------------------------------------------------------ success
    def judge(self, goal, events, titles, horizon_end):
        """Judge one goal instance from events. Returns (status, evidence, detail).

        status: succeeded | failed | inconclusive. Only events received inside (started_at, deadline] count.
        """
        gid = goal['goal_id']
        rule = self.rule(gid, goal['rule_version'])['success']
        start, deadline = ts(goal['started_at']), ts(goal['deadline'])
        if rule.get('event') is None:
            return 'inconclusive', [], 'goal has no measurable success event'
        evs = [e for e in events if e['event'] == rule['event'] and start < ts(e['received_at']) <= deadline]
        cond = (rule.get('condition') or '').lower()
        params = goal.get('params') or {}
        hits = []
        if 'is_trial = false' in cond or 'paid plan' in cond:
            hits = [e for e in evs if not (e.get('properties') or {}).get('is_trial')]
        elif 'later ist calendar day' in cond:
            d0 = ist_day(start)
            hits = [e for e in evs if ist_day(ts(e['received_at'])) > d0]
        elif 'same content_slug' in cond:
            hits = [e for e in evs if titles.get((e.get('properties') or {}).get('title_id'), {}).get('slug')
                    == params.get('slug')]
        elif 'released in the 30 days' in cond:
            lo = (start - timedelta(days=30)).date().isoformat()
            hits = [e for e in evs if lo <= titles.get((e.get('properties') or {}).get('title_id'), {})
                    .get('release_date', '') <= start.date().isoformat()]
        elif 'distinct ist days' in cond:
            days = {}
            for e in evs:
                days.setdefault(ist_day(ts(e['received_at'])), e)
            hits = list(days.values()) if len(days) >= 3 else []
        else:  # "any", "any title", "any episode"
            hits = evs
        if hits:
            return 'succeeded', ['outcome_event:%s' % e['event_id'] for e in hits[:3]], \
                'success signal %s observed at %s' % (rule['event'], hits[0]['received_at'])
        if deadline > horizon_end:
            return 'inconclusive', [], 'deadline %s is after the observed horizon %s' % (goal['deadline'], iso(horizon_end))
        return 'failed', [], 'no %s (%s) by deadline' % (rule['event'], rule.get('condition'))
