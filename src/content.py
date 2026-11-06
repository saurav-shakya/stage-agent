"""Builds a home page for one viewer from titles that exist, are available and are in the page dialect.

The planner is allowed to be wrong; the verifier re-checks every title independently before publishing.
"""
from datetime import timedelta

MAX_ROWS = 5          # the platform allows 6; the 6th adds nothing measurable and costs titles in small dialects
ROW_SIZE = 6
MIN_ROW = 3


def pick_variant(pack, slug, language, dialect, at):
    """One title_id per content_slug: prefer the viewer's app language, then Hindi, among available variants."""
    vs = [pack.titles[t] for t in pack.slug_titles.get(slug, [])
          if pack.titles[t]['dialect'] == dialect and pack.available(t, at)]
    if not vs:
        return None
    vs.sort(key=lambda t: (t['language'] != language, t['language'] != 'hin', t['title_id']))
    return vs[0]['title_id']


def build_page(pack, facts, goal_id, goal_params, dialect, language, at):
    """Returns (hero_title_id, rows, notes). rows = [{'row_type', 'title_ids'}]."""
    notes = []
    slugs_in_dialect = sorted({t['slug'] for t in pack.titles.values() if t['dialect'] == dialect},
                              key=lambda s: (-pack.popularity[s], s))
    pool = []
    for s in slugs_in_dialect:
        tid = pick_variant(pack, s, language, dialect, at)
        if tid:
            pool.append(tid)
    if len(pool) < MIN_ROW:
        notes.append('only %d available titles in %s' % (len(pool), dialect))
    used = set()

    def take(cands, n=ROW_SIZE):
        out = []
        for tid in cands:
            s = pack.titles[tid]['slug']
            if s in used:
                continue
            out.append(tid)
            used.add(s)
            if len(out) == n:
                break
        if len(out) < MIN_ROW:  # give the slugs back; the platform would drop this row anyway
            for tid in out:
                used.discard(pack.titles[tid]['slug'])
            return None
        return out

    # Hero: the goal's own title first, then a campaign deeplink title, then the most popular title.
    hero = None
    series = [s for s in facts['unfinished_series']['value']]
    if goal_id == 'G07' and goal_params.get('slug'):
        hero = pick_variant(pack, goal_params['slug'], language, dialect, at)
        if hero is None:
            notes.append('unfinished series %s not available in page dialect %s' % (goal_params['slug'], dialect))
    dl = facts.get('deeplink', {}).get('value')
    if hero is None and dl:
        t = pack.titles.get(dl['title_id'])
        if t and t['dialect'] == dialect and pack.available(dl['title_id'], at):
            hero = pick_variant(pack, t['slug'], language, dialect, at)
        else:
            notes.append('campaign title %s is not available in page dialect %s; not used' % (dl['title_id'], dialect))
    if hero is None and pool:
        hero = pool[0]
    if hero:
        used.add(pack.titles[hero]['slug'])

    watched = set(facts['watched_slugs']['value'])
    fresh = [t for t in pool if pack.titles[t]['slug'] not in watched]
    genres = facts['top_genres']['value']
    rows = []

    def add(row_type, cands):
        if len(rows) >= MAX_ROWS:
            return
        ids = take(cands)
        if ids:
            rows.append({'row_type': row_type, 'title_ids': ids})

    # Continue-series titles in this dialect (the platform's own Continue Watching row is separate).
    nxt = [pick_variant(pack, s['slug'], language, dialect, at) for s in series]
    add('next_episode', [t for t in nxt if t])

    cutoff = (at - timedelta(days=30)).date().isoformat()
    new = [t for t in pool if pack.titles[t]['release_date'] >= cutoff]
    short = [t for t in fresh if pack.titles[t]['type'] == 'micro_drama']
    premium = [t for t in fresh if pack.titles[t]['premium']]

    if goal_id == 'G08':
        add('new_releases', new)
    if goal_id in ('G01', 'G02', 'G03', 'G12'):
        add('popular_in_dialect', short + fresh)   # short episodes are the easiest first finish
    if goal_id in ('G04', 'G05', 'G06'):
        add('popular_in_dialect', premium + fresh)
    if genres:
        add('because_you_watched', [t for t in fresh if genres[0] in pack.titles[t]['genres']])
    add('popular_in_dialect', fresh)
    if goal_id != 'G08':
        add('new_releases', new)
    for g in genres[1:]:
        add('genre', [t for t in fresh if g in pack.titles[t]['genres']])
    add('popular_in_dialect', pool)
    return hero, rows, notes
