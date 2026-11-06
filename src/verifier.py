"""Independent checks run after the decision step and before anything is published.

The verifier does not trust the planner or the model. It re-reads the pack, the arm file and the viewer's raw
events itself, and any failed check removes the offending part of the decision (or the whole action).
"""
import re

from .facts import resolve
from .timeutil import in_quiet_hours, ist_day, ts

PRICE_RE = re.compile(r'(?:₹|rs\.?|inr)\s*(\d+)', re.I)


class Verifier:
    def __init__(self, pack, goalbook):
        self.pack = pack
        self.goalbook = goalbook
        hp = pack.interventions['home_page']['rows']
        self.min_row, self.max_row, self.max_rows = hp['min_titles_per_row'], hp['max_titles_per_row'], hp['max_rows']
        self.row_types = set(hp['row_types'])
        self.push_rules = pack.interventions['push']['copy']
        self.templates = {t['template_id']: t for t in pack.interventions['whatsapp']['approved_templates']}

    # ------------------------------------------------------------------ goal
    def check_goal(self, user_id, trigger, goal_id, model_answer, now):
        """Re-derive eligibility from raw data, independent of the decision step. Returns (passed, failed)."""
        passed, failed = [], []
        f = resolve(self.pack, user_id, trigger, now)
        ok, ev, why = self.goalbook.eligibility(goal_id, f, now)
        (passed if ok else failed).append(('eligibility', '%s: %s' % (goal_id, why)))
        rule = self.goalbook.current(goal_id)['success']
        measurable = rule.get('event') is not None and goal_id not in ('G10', 'G11')
        (passed if measurable else failed).append(('goal_measurable', goal_id))
        (passed if ev else failed).append(('evidence_present', ', '.join(ev) or 'no evidence'))
        if model_answer is not None:
            m_ok, _, m_why = self.goalbook.eligibility(model_answer['goal_id'], f, now)
            if m_ok:
                passed.append(('model_claim', 'model goal %s confirmed by rules' % model_answer['goal_id']))
            else:
                failed.append(('model_claim', 'model said %s eligible (conf %.2f, "%s") but rules say: %s' % (
                    model_answer['goal_id'], model_answer['confidence'], model_answer['reason'][:80], m_why)))
        return passed, failed

    # ------------------------------------------------------------------ home page
    def check_page(self, user_id, action, trigger, existing_actions):
        passed, failed = [], []
        at = ts(action['published_at'])
        d = action['dialect']
        if self.pack.arms.get(user_id) != 'agent':
            failed.append(('holdout', 'viewer arm is %s' % self.pack.arms.get(user_id)))
            return passed, failed, None
        passed.append(('holdout', 'agent arm'))
        if d != trigger.get('app_dialect'):
            failed.append(('dialect', 'page dialect %s != app setting %s' % (d, trigger.get('app_dialect'))))
            return passed, failed, None
        seen, rows, problems = set(), [], []
        hero = action.get('hero_title_id')
        if hero:
            ok, why = self._title_ok(hero, d, at)
            if ok:
                seen.add(self.pack.titles[hero]['slug'])
            else:
                problems.append('hero %s: %s' % (hero, why))
                hero = None
        for r in action.get('rows', [])[:self.max_rows]:
            keep = []
            if r['row_type'] not in self.row_types:
                problems.append('row type %s not allowed' % r['row_type'])
                continue
            for tid in r['title_ids'][:self.max_row]:
                ok, why = self._title_ok(tid, d, at)
                if not ok:
                    problems.append('%s: %s' % (tid, why))
                    continue
                s = self.pack.titles[tid]['slug']
                if s in seen:
                    problems.append('%s: duplicate content %s' % (tid, s))
                    continue
                seen.add(s)
                keep.append(tid)
            if len(keep) >= self.min_row:
                rows.append({'row_type': r['row_type'], 'title_ids': keep})
            else:
                problems.append('row %s dropped (%d valid titles < %d)' % (r['row_type'], len(keep), self.min_row))
        if problems:
            failed.append(('dialect_availability_uniqueness', '; '.join(problems[:6])))
        else:
            passed.extend([('dialect', d), ('availability', 'all titles available at publish time'),
                           ('unique_content', 'one title per content_slug'), ('row_minimum', '>=3 per row')])
        if not rows and not hero:
            failed.append(('page_not_empty', 'no valid rows or hero'))
            return passed, failed, None
        clean = dict(action, rows=rows, hero_title_id=hero)
        # Duplicate action: an identical page for the same dialect already live.
        for a in existing_actions:
            if a['type'] == 'home_page' and a['status'] == 'published' and a['dialect'] == d and \
                    a['live_until'] and ts(a['live_until']) > at:
                import json
                body = json.loads(a['body'])
                if body.get('rows') == rows and body.get('hero_title_id') == hero:
                    failed.append(('duplicate_action', 'identical page %s still live' % a['action_id']))
                    return passed, failed, None
        passed.append(('duplicate_action', 'no identical live page'))
        return passed, failed, clean

    def _title_ok(self, tid, dialect, at):
        t = self.pack.titles.get(tid)
        if t is None:
            return False, 'not in catalogue'
        if t['dialect'] != dialect:
            return False, 'dialect %s != %s' % (t['dialect'], dialect)
        if not self.pack.available(tid, at):
            return False, 'not available at %s' % at.isoformat()
        return True, ''

    # ------------------------------------------------------------------ messages
    def check_message(self, user_id, msg, facts, scheduled_days):
        """scheduled_days: IST days on which this viewer already has a message from us (any channel)."""
        passed, failed = [], []
        ch = msg['type']
        send_at = ts(msg['send_at'])
        if self.pack.arms.get(user_id) != 'agent':
            failed.append(('holdout', 'viewer arm is %s' % self.pack.arms.get(user_id)))
        if not facts['consent_' + ch]['value']:
            failed.append(('consent', '%s consent false (%s)' % (ch, facts['consent_' + ch]['source'])))
        else:
            passed.append(('consent', '%s opt-in (%s)' % (ch, facts['consent_' + ch]['source'])))
        if in_quiet_hours(send_at):
            failed.append(('quiet_hours', 'send_at %s is inside 22:00-08:00 IST' % msg['send_at']))
        else:
            passed.append(('quiet_hours', 'send_at outside quiet hours'))
        day = ist_day(send_at)
        n = facts['messages_by_ist_day']['value'].get(day, 0) + (1 if day in scheduled_days else 0)
        if n >= 1:
            failed.append(('frequency_cap', 'already %d message(s) on %s' % (n, day)))
        else:
            passed.append(('frequency_cap', '0 messages on %s' % day))
        tid = msg.get('title_id')
        if tid:
            ok, why = self._title_ok(tid, facts['page_dialect']['value'], send_at)
            if not ok:
                failed.append(('dialect', 'message title %s: %s' % (tid, why)))
            elif self.pack.titles[tid]['injection_flag']:
                passed.append(('injection_safe', 'title %s has instruction-like description; text not used' % tid))
        if ch == 'push':
            txt = msg['text']
            problems = []
            if len(txt['title']) > self.push_rules['title_max_chars']:
                problems.append('title too long')
            if len(txt['body']) > self.push_rules['body_max_chars']:
                problems.append('body too long')
            low = (txt['title'] + ' ' + txt['body']).lower()
            problems += ['forbidden phrase "%s"' % p for p in self.push_rules['forbidden_phrases'] if p in low]
            problems += ['price %s not in pricing.json' % p for p in PRICE_RE.findall(low)
                         if int(p) not in self.pack.prices]
            if msg.get('deeplink') != 'ott://title/%s' % tid:
                problems.append('deeplink format')
            (failed if problems else passed).append(('copy_rules', '; '.join(problems) or 'ok'))
        if ch == 'whatsapp':
            t = self.templates.get(msg.get('template_id'))
            if not t:
                failed.append(('template', 'template not approved'))
            elif set(msg.get('variables', {})) != set(t['variables']):
                failed.append(('template', 'variables do not match template'))
            else:
                bad = [k for k, v in msg['variables'].items() if not self._var_from_data(k, v, tid)]
                (failed if bad else passed).append(('template', 'unsourced variables: %s' % bad if bad else 'ok'))
        return passed, failed

    def _var_from_data(self, k, v, tid):
        if k == 'title_name':
            return tid in self.pack.titles and self.pack.titles[tid]['name'] == v
        if k == 'plan_price':
            m = PRICE_RE.search(v)
            return bool(m) and int(m.group(1)) in self.pack.prices
        return False  # dialect_name / episode_number have no trustworthy source we use
