"""Fact resolver: what the agent knows about one viewer, with source, time and freshness for every fact.

Precedence rules are written per fact (never "newest/biggest source wins by default"):

| fact                 | precedence                                                                          |
|----------------------|-------------------------------------------------------------------------------------|
| page dialect         | app setting on the trigger event (the app requests the page for its own dialect).   |
|                      | Profile and campaign deeplink dialects are recorded as conflicts, never used.       |
| subscription status  | billing events received after the snapshot > snapshot row > billing events before.  |
| signup time          | signup_completed event (server receive time) > snapshot signup_at.                  |
| consent              | any notification_opt_out event for the channel > snapshot opt-in (conservative).    |
| duplicate profiles   | the row with the latest updated_at; differing fields are listed as conflicts.       |
| activity times       | received_at corrected for offline buffering (see timeutil.true_time).               |
"""
from collections import Counter
from datetime import timedelta

from .timeutil import iso, ist_day, ts, buffered_seconds

PROFILE_FIELDS = ['signup_at', 'subscription_status', 'plan_started_at', 'plan_ends_at', 'preferred_dialect',
                  'preferred_language', 'notifications_opt_in', 'whatsapp_opt_in']
BILLING = ('purchase_completed', 'subscription_renewed', 'subscription_lapsed')


def F(value, source, ref, observed_at, now, conflicts=None, rule=None):
    return {'value': value, 'source': source, 'ref': ref, 'observed_at': iso(observed_at),
            'age_s': int((now - observed_at).total_seconds()) if observed_at else None,
            'conflicts': conflicts or [], 'rule': rule}


def pick_profile(rows):
    """Duplicate snapshot rows: keep the latest updated_at, report fields that disagree."""
    if not rows:
        return None, []
    rows = sorted(rows, key=lambda r: r['updated_at'])
    chosen = rows[-1]
    conflicts = []
    for r in rows[:-1]:
        for k in PROFILE_FIELDS:
            if r[k] != chosen[k]:
                conflicts.append('profile_row(updated_at=%s):%s=%s' % (r['updated_at'], k, r[k] or '<empty>'))
    return chosen, conflicts


def resolve(pack, user_id, trigger, now):
    """Assemble the facts for `user_id` as known at `now` (only events our servers had received by then)."""
    hist = [e for e in pack.history.until(user_id, now) if e['event_id'] != trigger['event_id']]
    facts = {}
    prof, prof_conflicts = pick_profile(pack.profile_rows.get(user_id, []))
    snap_at = ts(prof['snapshot_at']) if prof else None
    prof_ref = 'profile:%s@%s' % (user_id, prof['updated_at']) if prof else None
    facts['profile_present'] = F(bool(prof), 'profile_snapshot', prof_ref, snap_at, now, prof_conflicts,
                                 'latest updated_at wins among duplicate rows')

    # ---------------------------------------------------------------- dialect / language
    trig_at = ts(trigger['received_at'])
    app_d, app_l = trigger.get('app_dialect'), trigger.get('app_language')
    dl = pack.parse_deeplink((trigger.get('properties') or {}).get('deeplink'))
    raw_link = (trigger.get('properties') or {}).get('deeplink')
    conflicts = []
    if prof and prof['preferred_dialect'] and prof['preferred_dialect'] != app_d:
        conflicts.append('profile:%s' % prof['preferred_dialect'])
    if dl and dl['dialect'] != app_d:
        conflicts.append('deeplink:%s' % dl['dialect'])
    facts['page_dialect'] = F(app_d, 'app_setting', 'event:%s' % trigger['event_id'], trig_at, now, conflicts,
                              'the app shows a personalised page only for its current dialect, so the app setting '
                              'decides what the viewer will actually see')
    facts['app_language'] = F(app_l, 'app_setting', 'event:%s' % trigger['event_id'], trig_at, now)
    if prof:
        facts['profile_dialect'] = F(prof['preferred_dialect'], 'profile_snapshot', prof_ref, snap_at, now)
    if raw_link:
        facts['deeplink'] = F(dl, 'campaign_link', 'event:%s' % trigger['event_id'], trig_at, now,
                              [] if dl else ['unparseable deeplink ignored'],
                              'deeplink is content: it can suggest a title, never change policy')

    # ---------------------------------------------------------------- signup
    su_events = [e for e in hist if e['event'] == 'signup_completed']
    if su_events:
        e = su_events[0]
        c = []
        if prof and prof['signup_at'] and abs((ts(prof['signup_at']) - e['_received']).total_seconds()) > 86400:
            c.append('profile:signup_at=%s' % prof['signup_at'])
        facts['signup_at'] = F(iso(e['_received']), 'event', 'event:%s' % e['event_id'], e['_received'], now, c,
                               'server receive time of signup_completed beats the warehouse row')
    elif prof and prof['signup_at']:
        facts['signup_at'] = F(prof['signup_at'], 'profile_snapshot', prof_ref, snap_at, now)
    else:
        facts['signup_at'] = F(None, 'none', None, None, now, rule='unknown signup time')

    # ---------------------------------------------------------------- subscription
    bills = [e for e in hist if e['event'] in BILLING]
    status, src, ref, obs, c = None, None, None, None, []
    lapse_at = None
    if prof:
        status, src, ref, obs = prof['subscription_status'], 'profile_snapshot', prof_ref, snap_at
        if status == 'lapsed' and prof['plan_ends_at']:
            lapse_at = ts(prof['plan_ends_at'])
        if status == 'trial_active' and prof['plan_ends_at'] and ts(prof['plan_ends_at']) < now:
            c.append('snapshot trial ended at %s with no billing event since' % prof['plan_ends_at'])
            status = 'trial_expired_unknown'
    after = [e for e in bills if snap_at is None or e['_received'] > snap_at]
    if not prof and not bills:
        status, src = 'never', 'assumption:no_profile_no_billing_events'
    if after:
        e = after[-1]
        new = _status_from_billing(e)
        if prof and new != prof['subscription_status']:
            c.append('profile:%s (snapshot, stale by billing event)' % prof['subscription_status'])
        status, src, ref, obs = new, 'event', 'event:%s' % e['event_id'], e['_received']
    elif not prof and bills:
        e = bills[-1]
        status, src, ref, obs = _status_from_billing(e), 'event', 'event:%s' % e['event_id'], e['_received']
    lapses = [e for e in bills if e['event'] == 'subscription_lapsed']
    if lapses:
        lapse_at = lapses[-1]['_true']
    facts['subscription_status'] = F(status, src, ref, obs, now, c,
                                     'billing events received after the snapshot override the snapshot')
    facts['lapsed_at'] = F(iso(lapse_at), 'event' if lapses else 'profile_snapshot', ref, lapse_at, now)
    facts['ever_paid'] = F(any(e['event'] in ('purchase_completed', 'subscription_renewed') and
                               not (e['properties'] or {}).get('is_trial') for e in bills)
                           or (prof is not None and prof['subscription_status'] in ('active', 'lapsed')),
                           'event+profile', ref, obs, now)
    facts['trial_ends_at'] = F(prof['plan_ends_at'] if prof and prof['subscription_status'] == 'trial_active' else None,
                               'profile_snapshot', prof_ref, snap_at, now)

    # ---------------------------------------------------------------- consent
    for ch, field in (('push', 'notifications_opt_in'), ('whatsapp', 'whatsapp_opt_in')):
        outs = [e for e in hist if e['event'] == 'notification_opt_out' and (e['properties'] or {}).get('channel') == ch]
        if outs:
            e = outs[-1]
            c = ['profile:%s=%s' % (field, prof[field])] if prof and prof[field] == 'true' else []
            facts['consent_' + ch] = F(False, 'event', 'event:%s' % e['event_id'], e['_received'], now, c,
                                       'an opt-out event overrides the snapshot')
        elif prof:
            facts['consent_' + ch] = F(prof[field] == 'true', 'profile_snapshot', prof_ref, snap_at, now)
        else:
            facts['consent_' + ch] = F(False, 'assumption:no_profile', None, None, now,
                                       rule='no consent record means no consent')

    # ---------------------------------------------------------------- behaviour
    plays = [e for e in hist if e['event'] == 'play_started']
    completes = [e for e in hist if e['event'] == 'episode_completed']
    opens = [e for e in hist if e['event'] == 'app_open' and e.get('session_id') != trigger.get('session_id')]
    facts['ever_played'] = _evidence_fact(plays, now, 'first play_started')
    facts['ever_completed'] = _evidence_fact(completes, now, 'first episode_completed')
    last_open = max(opens, key=lambda e: e['_true']) if opens else None
    facts['last_app_open'] = F(iso(last_open['_true']) if last_open else None, 'event',
                               'event:%s' % last_open['event_id'] if last_open else None,
                               last_open['_true'] if last_open else None, now,
                               rule='true time = received_at minus offline buffering')
    facts['first_seen'] = F(iso(min(e['_true'] for e in hist)) if hist else None, 'event', None, None, now)
    days14 = sorted({ist_day(e['_true']) for e in plays if e['_true'] >= now - timedelta(days=14)})
    facts['play_days_14d'] = F(days14, 'event', 'events:play_started(last 14d)', now, now)
    genres = Counter()
    for e in plays:
        t = pack.titles.get((e['properties'] or {}).get('title_id'))
        if t:
            genres.update(t['genres'])
    facts['top_genres'] = F([g for g, _ in genres.most_common(3)], 'event', 'events:play_started', now, now)
    facts['watched_slugs'] = F(sorted({pack.titles[e['properties']['title_id']]['slug'] for e in plays
                                       if (e['properties'] or {}).get('title_id') in pack.titles}),
                               'event', 'events:play_started', now, now)
    facts['unfinished_series'] = F(_unfinished(pack, plays + completes, now), 'event+episodes',
                                   'events:play_started,episode_completed;episodes.csv', now, now)
    sent = Counter(ist_day(e['_true']) for e in hist if e['event'] == 'message_delivered')
    facts['messages_by_ist_day'] = F(dict(sent), 'event', 'events:message_delivered', now, now)

    # ---------------------------------------------------------------- trigger freshness
    held = buffered_seconds(trigger)
    facts['trigger_buffered_s'] = F(int(held), 'event_clock_delta', 'event:%s' % trigger['event_id'], trig_at, now,
                                    rule='sent_at - occurred_at (same phone clock) = time held offline')
    return facts


def _status_from_billing(e):
    if e['event'] == 'subscription_lapsed':
        return 'lapsed'
    if e['event'] == 'purchase_completed' and (e['properties'] or {}).get('is_trial'):
        return 'trial_active'
    return 'active'


def _evidence_fact(evs, now, label):
    if not evs:
        return F(False, 'event', 'no %s in history' % label.split()[-1], None, now)
    e = evs[0]
    return F(True, 'event', 'event:%s' % e['event_id'], e['_received'], now)


def _unfinished(pack, evs, now):
    """Series (show / micro-drama) the viewer started, with episodes left, not played for 3+ days."""
    by_slug = {}
    for e in evs:
        p = e['properties'] or {}
        idx = pack.episode_index.get(p.get('episode_id'))
        t = pack.titles.get(p.get('title_id'))
        if not idx or not t or t['type'] not in ('show', 'micro_drama'):
            continue
        slug, i = idx
        s = by_slug.setdefault(slug, {'slug': slug, 'title_id': t['title_id'], 'max_started': -1,
                                      'max_completed': -1, 'last_play': None, 'evidence': None})
        if e['event'] == 'play_started':
            s['max_started'] = max(s['max_started'], i)
            if s['last_play'] is None or e['_true'] > s['last_play']:
                s['last_play'], s['evidence'] = e['_true'], 'event:%s' % e['event_id']
        else:
            s['max_completed'] = max(s['max_completed'], i)
    out = []
    for s in by_slug.values():
        n = len(pack.episodes.get(s['slug'], []))
        nxt = s['max_completed'] + 1 if s['max_completed'] >= s['max_started'] else s['max_started']
        if s['last_play'] is None or nxt >= n:
            continue
        idle_days = (now - s['last_play']).total_seconds() / 86400
        out.append({'slug': s['slug'], 'title_id': s['title_id'], 'next_episode_index': nxt,
                    'next_episode_number': pack.episodes[s['slug']][nxt]['episode'], 'episodes_total': n,
                    'last_play': iso(s['last_play']), 'idle_days': round(idle_days, 2), 'evidence': s['evidence']})
    out.sort(key=lambda s: s['last_play'], reverse=True)
    return out
