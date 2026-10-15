#!/usr/bin/env python3
"""Fetch Clara's fencing stats and upcoming tournaments, then build the static site.

Sources:
  * fencingtracker.com  - profile, history (bouts), strength, event results
  * askfred.net         - upcoming tournaments near home (with distance)
  * usafencing.org      - national events calendar
  * USA Fencing Airtable - regional circuit calendar (SYC / RYC / RJCC / ROC)

Each source is cached in data/. If a fetch fails, the last good copy is reused so
the site still builds. Standard library only (runs on Python 3.9+).
"""
import base64
import csv
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SITE = ROOT / "site"
CONFIG = json.loads((ROOT / "config.json").read_text())
FENCER = CONFIG["fencer"]
HOME = CONFIG["home"]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TODAY = dt.date.today()
NOW = dt.datetime.now(dt.timezone.utc)
BUILD_ID = NOW.strftime("%Y%m%dT%H%M%SZ")

COOKIES = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIES))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(url, headers=None, tries=3):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    last = None
    for i in range(tries):
        try:
            with OPENER.open(urllib.request.Request(url, headers=h), timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def text(s):
    """HTML fragment -> clean text."""
    s = re.sub(r'<span class="visually-hidden">.*?</span>', " ", s, flags=re.S)
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def tables(fragment):
    out = []
    for t in re.findall(r"<table.*?</table>", fragment, re.S):
        head = re.search(r"<thead>(.*?)</thead>", t, re.S)
        headers = [text(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", head.group(1), re.S)] if head else []
        body = re.search(r"<tbody[^>]*>(.*?)</tbody>", t, re.S)
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1) if body else t, re.S):
            cells = [text(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
            if cells:
                rows.append(cells)
        out.append({"headers": headers, "rows": rows})
    return out


def cached(name, producer):
    path = DATA / f"{name}.json"
    try:
        value = producer()
        path.write_text(json.dumps(value, indent=1, ensure_ascii=False))
        log(f"[ok] {name}")
        return value, False
    except Exception as e:  # noqa: BLE001
        log(f"[stale] {name}: {e}")
        if path.exists():
            return json.loads(path.read_text()), True
        return None, True


# --------------------------------------------------------------------------- fencingtracker

FT = "https://fencingtracker.com"


def ft_profile():
    base = f"{FT}/p/{FENCER['fencingtracker_id']}/{FENCER['fencingtracker_slug']}"
    summary = fetch(base)
    history = fetch(base + "/history")
    strength = fetch(base + "/strength")

    name = text(re.search(r"<h1>(.*?)</h1>", summary, re.S).group(1))
    by = re.search(r'person-hero__birth-year">(\d{4})<', summary)
    club = re.search(r'person-hero__club-link"\s+href="([^"]+)">(.*?)</a>', summary, re.S)
    st = tables(summary)
    ratings = [{"weapon": r[0], "rating": r[1], "date": r[2]} for r in st[0]["rows"]] if st else []
    podium = st[1] if len(st) > 1 else None
    if podium and podium["headers"][:4] == ["Season", "", "", ""]:
        podium["headers"][1:4] = ["Gold", "Silver", "Bronze"]

    # history: season stat tables, then per-competition bout tables
    hist_main, _, hist_events = history.partition('id="person-history-results"')
    season_tables = []
    for sec in re.findall(r"<h2>(.*?)</h2>(.*?)</section>", hist_main, re.S):
        tb = tables(sec[1])
        if tb and tb[0]["headers"]:
            season_tables.append({"title": text(sec[0]), **tb[0]})

    competitions = []
    for m in re.finditer(r'<section\s+class="ranking-panel person-history__event-panel"\s+id="history-event-(\d+)"(.*?)</section>',
                         hist_events, re.S):
        eid, body = m.group(1), m.group(2)
        link = re.search(r'person-history__event-link" href="([^"]+)">(.*?)</a>', body, re.S)
        chips = [text(c) for c in re.findall(r'<span class="person-history__meta-chip">(.*?)</span>', body, re.S)]
        facts = [text(c) for c in re.findall(r'<span class="person-history__fact-chip[^"]*">(.*?)</span>', body, re.S)]
        date_s = chips[-1] if chips else ""
        try:
            date_iso = dt.datetime.strptime(date_s, "%B %d, %Y").date().isoformat()
        except ValueError:
            date_iso = ""
        place = re.search(r"Place (\d+) of (\d+)", " ".join(facts))
        seed = re.search(r"Seed (\d+)", " ".join(facts))
        bouts = []
        for tr in re.findall(r"<tr data-ranking-search[^>]*>(.*?)</tr>", body, re.S):
            cells = {}
            for cls, inner in re.findall(r'<td[^>]*?person-history__([a-z-]+)-col[^>]*>(.*?)</td>', tr, re.S):
                cells[cls] = text(inner)
            opp = re.search(r'<a href="(/p/[^"]+)">', tr)
            dec = "sk-decrease" in tr
            change = cells.get("change", "")
            bouts.append({
                "type": cells.get("bout", "").replace("Log in or create an account to report a bout correction.", "").strip(),
                "result": cells.get("result", ""),
                "score": cells.get("score", ""),
                "opponent": cells.get("opponent", ""),
                "opponent_url": FT + opp.group(1) if opp else "",
                "seed": cells.get("seed", ""),
                "rank": cells.get("rank", ""),
                "rating": cells.get("rating", ""),
                "place": cells.get("place", ""),
                "club": cells.get("club", ""),
                "opp_strength": cells.get("opp-strength", ""),
                "strength": cells.get("your-strength", ""),
                "change": ("-" if dec else "+") + change if change and change != "-" else change,
                "win_chance": cells.get("chance", ""),
            })
        competitions.append({
            "id": eid,
            "tournament": text(re.search(r"<h2>(.*?)</h2>", body, re.S).group(1)),
            "event": text(link.group(2)) if link else "",
            "event_url": FT + link.group(1) if link else "",
            "class": chips[0] if len(chips) > 1 else "",
            "date": date_iso,
            "place": int(place.group(1)) if place else None,
            "field": int(place.group(2)) if place else None,
            "seed": int(seed.group(1)) if seed else None,
            "facts": facts,
            "bouts": bouts,
        })

    # strength: current table + time series embedded in the chart script
    stb = tables(strength)
    current = []
    if stb:
        for r in stb[0]["rows"]:
            if len(r) >= 4:
                current.append({"weapon": r[0], "type": r[1], "strength": r[2], "range": r[3]})
    series = {}
    sm = re.search(r"const series = \{(.*?)\};", strength, re.S)
    if sm:
        for w, p, d in re.findall(r"([EFS]):\s*\{\s*P:\s*(\[.*?\]),\s*D:\s*(\[.*?\]),?\s*\}", sm.group(1), re.S):
            series[w] = {"pool": json.loads(p), "de": json.loads(d)}

    return {
        "name": name,
        "birth_year": int(by.group(1)) if by else FENCER["birth_year"],
        "club": text(club.group(2)) if club else "",
        "club_url": FT + club.group(1) if club else "",
        "profile_url": base,
        "ratings": ratings,
        "podium": podium,
        "season_tables": season_tables,
        "competitions": competitions,
        "strength_current": current,
        "strength_series": series,
    }


def ft_event(url):
    """Podium + field for one event results page (cached forever once fetched)."""
    page = fetch(url)
    rows = []
    for m in re.finditer(r'<tr data-event-results-row data-event-results-name="([^"]*)" data-event-results-birth-year="([^"]*)">\s*'
                         r'<td[^>]*>(\d+)</td>\s*<td>(.*?)</td>', page, re.S):
        link = re.search(r'href="([^"]+)"', m.group(4))
        rows.append({"place": int(m.group(3)), "name": html.unescape(m.group(1)),
                     "birth_year": m.group(2), "url": FT + link.group(1) if link else ""})
    return {"url": url, "field": len(rows), "podium": [r for r in rows if r["place"] <= 3][:4],
            "top8": [r for r in rows if r["place"] <= 8]}


# --------------------------------------------------------------------------- AskFRED

FRED = "https://www.askfred.net"


def fred_query(page, location, radius):
    return urllib.parse.urlencode({
        "weapon": "Saber" if FENCER["weapons"] == ["saber"] else "",
        "gender": "Women or Mixed" if FENCER["gender"] == "women" else "Men or Mixed",
        "location": location,
        "radius": radius,
        "page": page,
    })


def askfred_search(location, radius, meta):
    cards = []
    page, pages = 1, 1
    while page <= pages and page <= 40:
        q = fred_query(page, location, radius)
        listing = fetch(f"{FRED}/tournaments?{q}")
        m = re.search(r"(\d+) of (\d+) pages", listing)
        pages = int(m.group(2)) if m else 1
        cards.extend(listing.split('<div class="card p-2 p-md-3 my-2">')[1:])
        # The CSV export carries structured age/rating limits. Its page size differs
        # from the HTML list, so collect every page and join globally by name.
        rows = list(csv.reader(io.StringIO(fetch(f"{FRED}/tournaments.csv?{q}"))))
        cols = rows[0] if rows else []
        for row in rows[1:]:
            # tournament names with commas are not quoted, so fold the extras back in
            extra = len(row) - len(cols)
            if extra > 0:
                row = [",".join(row[:extra + 1])] + row[extra + 1:]
            r = dict(zip(cols, row))
            meta[(html.unescape(r.get("Tournament", "")).strip(), html.unescape(r.get("Event", "")).strip())] = r
        page += 1
        time.sleep(1.0)
    return cards


def askfred():
    """Home-radius search, plus extra areas (e.g. Florida) whose distances aren't from home."""
    meta, seen, tournaments = {}, set(), []
    searches = [(HOME["location"], HOME["askfred_radius_miles"], "")]
    searches += [(x["location"], x.get("radius_miles", 300), x["label"]) for x in HOME.get("extra_searches", [])]
    for location, radius, label in searches:
        cards = askfred_search(location, radius, meta)
        for card in cards:
            t = parse_fred_card(card, meta)
            if t and t["id"] not in seen:
                seen.add(t["id"])
                if label:
                    t["distance"], t["area"] = None, label
                tournaments.append(t)
    return tournaments


def parse_fred_card(card, meta):
    h = re.search(r'<h5>(.*?)</h5>', card, re.S)
    if not h:
        return None
    a = re.search(r'href="/tournaments/([0-9a-f-]+)">(.*?)</a>', h.group(1), re.S)
    if not a:
        return None
    tid, name = a.group(1), text(a.group(2))
    closed = "Registration Closed" in h.group(1)
    gcal = re.search(r"dates=(\d{8})%2F(\d{8})", card)
    start = end = ""
    if gcal:
        start = dt.datetime.strptime(gcal.group(1), "%Y%m%d").date()
        end = dt.datetime.strptime(gcal.group(2), "%Y%m%d").date() - dt.timedelta(days=1)
        start, end = start.isoformat(), max(start, end).isoformat()
    loc = re.search(r'<div class="col-12 col-md-6 order-md-12">(.*?)<a href="https://maps', card, re.S)
    dist = re.search(r"<small>([\d,]+) miles? from", card)
    events = []
    body = card.split('<table class="table table-sm mb-0">', 1)
    for tr in re.findall(r"<tr>(.*?)</tr>", body[1] if len(body) > 1 else "", re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if not tds:
            continue
        ea = re.search(r"<a[^>]*>(.*?)</a>", tds[-1], re.S)
        if not ea:
            continue
        ename = text(ea.group(1))
        est = re.search(r'<span class="text-muted">(.*?)<small>', tds[-1], re.S)
        when = " ".join(text(x) for x in tds[:-1]).strip()
        m = meta.get((name, ename), {})
        events.append({
            "name": ename,
            "when": when,
            "estimate": text(est.group(1)) if est else "",
            "weapon": (m.get("Weapon") or "").lower(),
            "gender": (m.get("Gender") or "").lower(),
            "age": m.get("Age Limit") or "",
            "rating_limit": m.get("Rating Limit") or "",
            "start": (m.get("Close of Reg") or "")[:16],
        })
    return {
        "source": "askfred",
        "id": tid,
        "name": name,
        "url": f"{FRED}/tournaments/{tid}",
        "register_url": f"{FRED}/tournaments/{tid}/preregister" if "/preregister\"" in card else "",
        "whos_coming_url": f"{FRED}/tournaments/{tid}/preregistrations",
        "registration_open": not closed and "/preregister\"" in card,
        "start": start,
        "end": end,
        "venue": text(loc.group(1)) if loc else "",
        "distance": int(dist.group(1).replace(",", "")) if dist else None,
        "events": events,
    }


# --------------------------------------------------------------------------- USA Fencing

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_usaf_dates(s):
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:\s*[–-]\s*(?:([A-Za-z]{3,9})\.?\s+)?(\d{1,2}))?,\s*(\d{4})", s)
    if not m:
        return "", ""
    m1 = MONTHS.get(m.group(1)[:3].lower())
    m2 = MONTHS.get((m.group(3) or m.group(1))[:3].lower())
    y = int(m.group(5))
    if not m1 or not m2:
        return "", ""
    start = dt.date(y, m1, int(m.group(2)))
    end = dt.date(y, m2, int(m.group(4) or m.group(2)))
    return start.isoformat(), end.isoformat()


def usaf_national():
    page = fetch("https://www.usafencing.org/events-national")
    page = page.split("Past National Events")[0]
    out = []
    for li in re.findall(r'<li class="chakra-list__item[^"]*">(.*?)</li>', page, re.S):
        h = re.search(r"<h3[^>]*>(.*?)</h3>", li, re.S)
        if not h:
            continue
        ps = [text(p) for p in re.findall(r"<p>(.*?)</p>", li, re.S)]
        link = re.search(r'<a href="([^"]+)" class="chakra-link[^"]*">\s*Visit', li, re.S)
        when = ps[0] if ps else ""
        start, end = parse_usaf_dates(when)
        if not start and "TBA" not in when:
            continue  # news teasers share the same list markup
        loc = when.split("—", 1)[1].strip() if "—" in when else ""
        out.append({
            "source": "usaf",
            "name": text(h.group(1)),
            "when": when.split("—")[0].strip(),
            "location": loc,
            "start": start,
            "end": end,
            "categories": ps[1] if len(ps) > 1 else "",
            "notes": [p for p in ps[2:]],
            "url": urllib.parse.urljoin("https://www.usafencing.org/", link.group(1)) if link else "",
        })
    if not out:
        raise RuntimeError("no national events parsed")
    return out


AIRTABLE_SHARES = [
    ("2026-27", "https://airtable.com/appxKNz6tPo9tirzc/shr1ISwdEeBsrAhmf"),
    ("2027-28", "https://airtable.com/appxKNz6tPo9tirzc/shrRZukRHCp6blLzn"),
]


def airtable_view(share):
    page = fetch(share)
    u = re.search(r'urlWithParams:\s*"([^"]+)"', page).group(1)
    u = u.encode().decode("unicode_escape")
    app = re.search(r"airtable\.com/(app[A-Za-z0-9]+)/", share).group(1)
    raw = fetch("https://airtable.com" + u, headers={
        "x-airtable-application-id": app, "x-requested-with": "XMLHttpRequest",
        "x-time-zone": "America/New_York", "x-user-locale": "en", "Referer": share})
    table = json.loads(raw)["data"]["table"]
    cols = {c["id"]: c for c in table["columns"]}
    by_name = {c["name"].strip(): c["id"] for c in table["columns"]}

    def val(cells, colname):
        cid = by_name.get(colname)
        if not cid or cid not in cells:
            return None
        v = cells[cid]
        choices = (cols[cid].get("typeOptions") or {}).get("choices") or {}
        if isinstance(v, list):
            return [choices[x]["name"] if x in choices else x for x in v]
        if isinstance(v, str) and v in choices:
            return choices[v]["name"]
        return v

    out = []
    for r in table["rows"]:
        c = r.get("cellValuesByColumnId")
        if not c:
            continue
        s, e = (val(c, "Start Date") or "")[:10], (val(c, "End Date") or "")[:10]
        out.append({
            "source": "usaf-regional",
            "name": (val(c, "Tournament") or "").strip(),
            "start": s, "end": e or s,
            "region": val(c, "Region") or "",
            "circuits": [x.strip() for x in re.split(r"[,/]", val(c, "Circuit(s)") or "") if x.strip()],
            "contested": val(c, "Contested Events") or [],
            "other": val(c, "Other Events") or "",
            "city": val(c, "City") or "",
            "state": val(c, "State") or "",
            "notes": text(val(c, "Notes") or "") if isinstance(val(c, "Notes"), str) else "",
        })
    return out


def usaf_regional():
    out = []
    for _season, share in AIRTABLE_SHARES:
        try:
            out.extend(airtable_view(share))
        except Exception as e:  # noqa: BLE001
            log(f"  airtable {share}: {e}")
    if not out:
        raise RuntimeError("no regional events")
    return out


# --------------------------------------------------------------------------- eligibility

RATING_ORDER = "ABCDEU"
RATING_LIMITS = {
    "open": "ABCDEU", "div1": "ABCDEU", "none": "ABCDEU", "": "ABCDEU",
    "unrated": "U", "eunder": "EU", "div3": "DEU", "div2": "CDEU", "div1a": "CDEU",
    "dabove": "ABCD", "babove": "AB", "aonly": "A",
}


def season_start(d):
    return d.year if d.month >= 8 else d.year - 1


def age_categories(birth_year, d):
    """USA Fencing age categories for the season containing date d."""
    s = season_start(d)
    cats = set()
    if s - 8 <= birth_year <= s - 7:
        cats.add("y8")
    if s - 10 <= birth_year <= s - 9:
        cats.add("y10")
    if s - 12 <= birth_year <= s - 11:
        cats.add("y12")
    if s - 14 <= birth_year <= s - 12:
        cats.add("y14")
    if s - 16 <= birth_year <= s - 13:
        cats.add("cadet")
    if s - 19 <= birth_year <= s - 13:
        cats.add("junior")
    if birth_year <= s - 13:
        cats.add("senior")
    if birth_year <= s - 39:
        cats.add("veteran")
    return cats


def fencer_rating(profile, weapon):
    for r in (profile or {}).get("ratings", []):
        if r["weapon"].lower().startswith(weapon[:4]):
            return (r["rating"] or "U")[0].upper()
    return "U"


def guess_from_name(ename):
    n = ename.lower()
    weapon = "saber" if re.search(r"sab(er|re)", n) else "epee" if "epee" in n or "épée" in n else "foil" if "foil" in n else ""
    gender = "women" if re.search(r"women|girl|female", n) else "mixed" if "mixed" in n else "men" if re.search(r"\bmen|boy", n) else "mixed"
    age = ""
    for tok, val in (("y14", "Y14"), ("y-14", "Y14"), ("y12", "Y12"), ("y10", "Y10"), ("y8", "Y8"),
                     ("cadet", "Cadet"), ("junior", "Junior"), ("veteran", "Veteran"), ("vet", "Veteran"), ("senior", "Senior")):
        if tok in n:
            age = val
            break
    rl = ""
    for tok, val in (("div 3", "Div3"), ("div3", "Div3"), ("div iii", "Div3"), ("div 2", "Div2"), ("div2", "Div2"),
                     ("div ii", "Div2"), ("div 1a", "Div1A"), ("div1a", "Div1A"), ("unrated", "Unrated"),
                     ("e & under", "EUnder"), ("e and under", "EUnder"), ("d & above", "DAbove")):
        if tok in n:
            rl = val
            break
    return weapon, gender, age, rl


def event_eligible(ev, d, profile):
    weapon, gender, age, rl = ev.get("weapon"), ev.get("gender"), ev.get("age"), ev.get("rating_limit")
    if not weapon:
        weapon, gender, age, rl = guess_from_name(ev["name"])
    if weapon not in FENCER["weapons"]:
        return False
    if FENCER["gender"] == "women" and gender not in ("women", "mixed"):
        return False
    cats = age_categories(FENCER["birth_year"], d)
    a = (age or "None").strip().lower()
    if a in ("none", "", "open"):
        pass
    elif re.fullmatch(r"u(\d+)", a):
        if d.year - FENCER["birth_year"] >= int(a[1:]):
            return False
    elif a not in cats:
        return False
    allowed = RATING_LIMITS.get((rl or "").strip().lower(), "ABCDEU")
    return fencer_rating(profile, weapon) in allowed


CIRCUIT_RE = re.compile(r"\b(NAC|SYC|RYC|RJCC|ROC|SJCC|RCC|Junior Olympics?|JO Qualifier|Qualifier|Nationals?)\b", re.I)


def circuit_tags(name):
    tags = []
    for m in CIRCUIT_RE.findall(name):
        t = m.upper() if len(m) <= 5 else m.title()
        if t not in tags:
            tags.append(t)
    return tags


def tokens(s):
    stop = {"the", "and", "of", "fencing", "tournament", "club", "cup", "open", "rjcc", "ryc", "roc", "syc", "2026", "2027"}
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in stop and len(t) > 1}


def regional_relevance(r):
    """Which contested events at a circuit tournament Clara can earn points in."""
    d = dt.date.fromisoformat(r["start"]) if r["start"] else TODAY
    cats = age_categories(FENCER["birth_year"], d)
    rating = None
    want = []
    mapping = {"Y14": "y14", "CDT": "cadet", "JNR": "junior"}
    for c in r["contested"]:
        if c in mapping and mapping[c] in cats:
            want.append(c)
        elif c in ("DV1", "D1A", "DV2", "DV3") and "senior" in cats:
            want.append(c)
    own = r["region"] == FENCER["region"]
    notes = []
    if "RYC" in r["circuits"] and "Y14" in want and not own:
        notes.append("RYC points only count in your own region")
    return want, own, notes


UPCOMING_FILTER = CONFIG.get("upcoming_filter", {})
HIGHLIGHT = CONFIG.get("highlight", {})
STATE_NAMES = {
    "FL": ["florida", "fla."], "GA": ["georgia"], "SC": ["south carolina"], "NC": ["north carolina"],
    "TN": ["tennessee", "tenn."], "VA": ["virginia", "va."], "MD": ["maryland", "md."], "DC": ["district of columbia", "washington, d.c."],
    "AL": ["alabama", "ala."], "MS": ["mississippi", "miss."], "WV": ["west virginia"], "PR": ["puerto rico"],
}


def highlighted(*places, region=""):
    """True if in a highlighted region, or any place ('City, ST', venue, 'Orlando, Fla.') is in a highlighted state."""
    if region and region in HIGHLIGHT.get("regions", []):
        return True
    for st in HIGHLIGHT.get("states", []):
        names = STATE_NAMES.get(st, [])
        for place in places:
            p = (place or "").lower().strip()
            if p == st.lower() or re.search(r",\s*" + re.escape(st.lower()) + r"\b", p):
                return True
            # 'virginia' must not match 'west virginia' unless WV itself is highlighted
            if any(re.search(r"(?<!west )" + re.escape(n), p) for n in names):
                return True
    return False


def shown_on_home(ev):
    """Narrow the upcoming list to the categories chosen in config (e.g. Y14 women's saber)."""
    weapon, gender, age = ev.get("weapon"), ev.get("gender"), ev.get("age")
    if not weapon:
        weapon, gender, age, _ = guess_from_name(ev["name"])
    ages = [a.lower() for a in UPCOMING_FILTER.get("ages", [])]
    genders = UPCOMING_FILTER.get("genders", [])
    if ages and (age or "").lower() not in ages:
        return False
    return not genders or gender in genders


US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "puerto rico": "PR", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def venue_state(venue):
    """'Club: Fort Lauderdale, FL' or '...: Orlando, Florida' -> 'FL'."""
    tail = (venue or "").rsplit(",", 1)[-1].strip().lower()
    if len(tail) == 2 and tail.upper() in US_STATES.values():
        return tail.upper()
    return US_STATES.get(tail, "")


def build_upcoming(fred, regional, national, profile):
    up = []
    for t in fred or []:
        if not t["start"] or dt.date.fromisoformat(t["end"] or t["start"]) < TODAY:
            continue
        d = dt.date.fromisoformat(t["start"])
        elig = [e for e in t["events"] if event_eligible(e, d, profile) and shown_on_home(e)]
        if not elig:
            continue
        up.append({**t, "eligible": elig, "tags": circuit_tags(t["name"]),
                   "tier": "local" if t["distance"] is not None and t["distance"] <= HOME["local_miles"] else "regional",
                   "hl": highlighted(t["venue"]) or bool(t.get("area") and t["area"] == HIGHLIGHT.get("label"))})

    # attach USA Fencing circuit info to AskFRED entries; keep the rest as circuit-only items
    circuit = []
    for r in regional or []:
        if not r["start"] or dt.date.fromisoformat(r["end"] or r["start"]) < TODAY:
            continue
        want, own, notes = regional_relevance(r)
        if not want:
            continue
        item = {**r, "eligible_codes": want, "own_region": own, "notes_extra": notes, "askfred": None}
        rt = tokens(r["name"])
        for t in up:
            # same state and same dates, plus a shared name word or the same city
            if venue_state(t["venue"]) != r["state"] or not r["state"]:
                continue
            if t["start"] == r["start"] or t["end"] == r["end"]:
                city = r["city"].lower().strip()
                if rt & tokens(t["name"]) or (city and city in t["venue"].lower()):
                    item["askfred"] = t["url"]
                    t["usaf_circuit"] = r["circuits"]
                    t["usaf_region"] = r["region"]
                    for c in r["circuits"]:
                        if c not in t["tags"]:
                            t["tags"].append(c)
                    break
        circuit.append(item)

    for t in up:
        t["hl"] = t["hl"] or highlighted(region=t.get("usaf_region", ""))

    nat = []
    for n in national or []:
        if n["start"] and dt.date.fromisoformat(n["end"] or n["start"]) < TODAY:
            continue
        d = dt.date.fromisoformat(n["start"]) if n["start"] else TODAY
        cats = age_categories(FENCER["birth_year"], d)
        c = n["categories"].lower()
        fits = []
        if "y14" in c and "y14" in cats:
            fits.append("Y14")
        if "cadet" in c and "cadet" in cats:
            fits.append("Cadet")
        if "junior" in c and "junior" in cats:
            fits.append("Junior")
        if "division ii" in c and "senior" in cats:
            fits.append("Division II")
        if "division i" in c.replace("division ii", "") and "senior" in cats:
            fits.append("Division I (qualifying required)")
        if "all age" in c or "to be announced" in c:
            fits.append("Check events")
        nat.append({**n, "fits": fits})

    up.sort(key=lambda t: (t["start"], t["distance"] or 9999))
    circuit.sort(key=lambda r: (r["start"], not r["own_region"]))
    nat.sort(key=lambda n: n["start"] or "9999")
    return up, circuit, nat


# --------------------------------------------------------------------------- render

def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def fmt_date(iso, with_year=False):
    if not iso:
        return "TBA"
    d = dt.date.fromisoformat(iso)
    return d.strftime("%b %-d, %Y" if with_year else "%a, %b %-d")


def fmt_range(s, e):
    if not s:
        return "Dates TBA"
    if not e or e == s:
        return fmt_date(s, True)
    a, b = dt.date.fromisoformat(s), dt.date.fromisoformat(e)
    if a.month == b.month:
        return f"{a.strftime('%b %-d')}–{b.day}, {b.year}"
    return f"{a.strftime('%b %-d')} – {b.strftime('%b %-d, %Y')}"


def days_until(iso):
    if not iso:
        return ""
    n = (dt.date.fromisoformat(iso) - TODAY).days
    return "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days" if n > 0 else "underway"


def stat_value(profile, table_title, row_label, col=-1):
    for t in profile.get("season_tables", []):
        if t["title"].lower().startswith(table_title.lower()):
            for r in t["rows"]:
                if r and r[0].lower() == row_label.lower():
                    return r[col]
    return "-"


def render(profile, events_cache, up, circuit, nat, stale):
    p = profile or {}
    comps = p.get("competitions", [])
    wins = stat_value(p, "Win/loss", "Victories")
    losses = stat_value(p, "Win/loss", "Losses")
    ratio = stat_value(p, "Win/loss", "Win ratio")
    pool_w, pool_l = stat_value(p, "Win/loss", "Pool victories"), stat_value(p, "Win/loss", "Pool losses")
    de_w, de_l = stat_value(p, "Win/loss", "DE victories"), stat_value(p, "Win/loss", "DE losses")
    total_bouts = sum(len(c["bouts"]) for c in comps)
    touches_for = touches_against = 0
    for c in comps:
        for b in c["bouts"]:
            m = re.match(r"(\d+):(\d+)", b["score"])
            if m:
                touches_for += int(m.group(1))
                touches_against += int(m.group(2))
    rating = fencer_rating(p, FENCER["weapons"][0])
    strength = {(s["weapon"], s["type"]): s for s in p.get("strength_current", [])}
    s_de = strength.get(("Saber", "Direct elimination"), {}).get("strength", "–")
    s_pool = strength.get(("Saber", "Pool"), {}).get("strength", "–")
    best = min((c for c in comps if c["place"]), key=lambda c: c["place"] / c["field"], default=None)
    season = f"{season_start(TODAY)}–{str(season_start(TODAY) + 1)[2:]}"
    cats = sorted(age_categories(FENCER["birth_year"], TODAY), key=["y8", "y10", "y12", "y14", "cadet", "junior", "senior", "veteran"].index)
    cat_label = ", ".join({"y14": "Y14", "y12": "Y12", "y10": "Y10", "y8": "Y8"}.get(c, c.title()) for c in cats)

    def tile(label, value, sub=""):
        return f'<div class="tile"><div class="tile-label">{esc(label)}</div><div class="tile-value">{esc(value)}</div><div class="tile-sub">{esc(sub)}</div></div>'

    tiles = "".join([
        tile("Saber rating", rating, f"Eligible: {cat_label}"),
        tile("Events", len(comps), f"{total_bouts} bouts fenced"),
        tile("Record", f"{wins if wins != '-' else 0}–{losses if losses != '-' else 0}", f"win ratio {ratio}"),
        tile("Pools", f"{pool_w if pool_w != '-' else 0}–{pool_l if pool_l != '-' else 0}", "wins–losses"),
        tile("Direct elim.", f"{de_w if de_w != '-' else 0}–{de_l if de_l != '-' else 0}", "wins–losses"),
        tile("Touches", f"{touches_for}–{touches_against}", "scored – received"),
        tile("Strength (DE)", s_de, f"pool {s_pool}"),
        tile("Best finish", f"{best['place']} / {best['field']}" if best else "–", best["tournament"] if best else ""),
    ])

    # results
    res_rows = []
    for c in comps:
        ev = events_cache.get(c["id"]) or {}
        winner = ev.get("podium", [{}])[0].get("name", "") if ev.get("podium") else ""
        bouts = "".join(
            f'<tr class="{ "win" if b["result"].startswith("V") else "loss"}"><td>{esc(b["type"])}</td>'
            f'<td><span class="pill {"pill-win" if b["result"].startswith("V") else "pill-loss"}">{esc(b["result"][:1])}</span> {esc(b["score"])}</td>'
            f'<td><a href="{esc(b["opponent_url"])}">{esc(b["opponent"])}</a><div class="muted small">{esc(b["club"])}</div></td>'
            f'<td class="num">{esc(b["rating"])}</td><td class="num">{esc(b["seed"])}</td>'
            f'<td class="num">{esc(b["opp_strength"])}</td><td class="num">{esc(b["win_chance"])}</td><td class="num">{esc(b["change"])}</td></tr>'
            for b in c["bouts"])
        pct = round(100 * (1 - (c["place"] - 1) / max(c["field"] - 1, 1))) if c["place"] and c["field"] else 0
        res_rows.append(f"""
<details class="result">
  <summary>
    <div class="r-date">{esc(fmt_date(c['date'], True))}</div>
    <div class="r-main"><div class="r-title">{esc(c['tournament'])}</div><div class="muted">{esc(c['event'])} · {esc(c['class'])}</div></div>
    <div class="r-place"><b>{esc(c['place'])}</b><span class="muted"> / {esc(c['field'])}</span><div class="bar"><span style="width:{pct}%"></span></div></div>
  </summary>
  <div class="r-body">
    <p class="muted small">{esc(' · '.join(c['facts']))}{(' · Winner: ' + esc(winner)) if winner else ''} · <a href="{esc(c['event_url'])}">Full results ↗</a></p>
    <div class="scroll"><table class="bouts"><thead><tr><th>Bout</th><th>Score</th><th>Opponent</th><th class="num">Rating</th><th class="num">Seed</th><th class="num">Opp. strength</th><th class="num">Win chance</th><th class="num">Strength Δ</th></tr></thead>
    <tbody>{bouts}</tbody></table></div>
  </div>
</details>""")

    season_html = ""
    extra = [{"title": "Podium finishes", **p["podium"]}] if p.get("podium") else []
    for t in p.get("season_tables", []) + extra:
        head = "".join(f"<th>{esc(h)}</th>" for h in t["headers"])
        body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in t["rows"])
        season_html += f'<div class="season-table"><h3>{esc(t["title"])}</h3><div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></div>'

    # upcoming AskFRED
    def tcard(t):
        evs = "".join(
            f'<li><span class="ev-when">{esc(e["when"])}</span> {esc(e["name"])}'
            f'{(" <span class=muted>· est. " + esc(e["estimate"]) + "</span>") if e["estimate"] else ""}</li>'
            for e in t["eligible"])
        tags = "".join(f'<span class="tag">{esc(x)}</span>' for x in t["tags"])
        reg = (f'<a class="btn" href="{esc(t["register_url"])}">Register</a>' if t["registration_open"] and t["register_url"]
               else '<span class="btn btn-off">Registration closed</span>')
        dist = f'{t["distance"]} mi' if t["distance"] is not None else t.get("area", "")
        return f"""
<article class="tcard{' hl' if t.get('hl') else ''}" id="t-{esc(t['id'])}" data-tier="{esc(t['tier'])}" data-circuit="{'1' if t['tags'] else '0'}">
  <div class="t-date"><div class="t-mon">{esc(dt.date.fromisoformat(t['start']).strftime('%b'))}</div><div class="t-day">{esc(dt.date.fromisoformat(t['start']).day)}</div><div class="t-in">{esc(days_until(t['start']))}</div></div>
  <div class="t-body">
    <h3><a href="{esc(t['url'])}">{esc(t['name'])}</a></h3>
    <div class="muted">{esc(fmt_range(t['start'], t['end']))} · {esc(t['venue'])}{(' · <b>' + esc(dist) + '</b>') if dist else ''}</div>
    <div class="tags">{tags}{('<span class="tag tag-region">' + esc(t['usaf_region']) + '</span>') if t.get('usaf_region') else ''}</div>
    <ul class="evs">{evs}</ul>
    <div class="actions">{reg}<a class="btn btn-ghost" href="{esc(t['whos_coming_url'])}">Who's coming</a></div>
  </div>
</article>"""

    horizon = (TODAY + dt.timedelta(days=183)).isoformat()
    def city(venue):
        return venue.split(":", 1)[1].strip() if ":" in venue else venue

    def archive(items, empty):
        """Month-grouped link list: items are (iso date, anchor id, label, tooltip, extra class)."""
        out, month = "", ""
        for iso, anchor, label, tip, cls in items:
            d = dt.date.fromisoformat(iso)
            if d.strftime("%B %Y") != month:
                out += ("</ul>" if month else "") + f'<h4>{esc(d.strftime("%B %Y"))}</h4><ul>'
                month = d.strftime("%B %Y")
            out += (f'<li><a class="mini {cls}" href="#{anchor}" data-target="{anchor}" title="{esc(tip)}">'
                    f'<b>{esc(d.strftime("%b %-d"))}</b><span>{esc(label)}</span></a></li>')
        return out + "</ul>" if out else f'<p class="muted">{empty}</p>'

    soon_html = archive([(t["start"], f't-{t["id"]}', city(t["venue"]) or t["name"], t["name"], "hl" if t.get("hl") else "")
                         for t in up if t["start"] <= horizon], "Nothing in the next 6 months.")
    hl_items = [(r["start"], f"c-{i}", f'{r["city"]}, {r["state"]}', f'{r["name"] or "Name TBA"} · {"/".join(r["circuits"])}', "hl")
                for i, r in enumerate(circuit) if highlighted(r["state"], region=r["region"]) and r["start"] > horizon]
    hl_side = ""
    if hl_items:
        hl_side = (f'<div class="pinned"><h4>{esc(HIGHLIGHT.get("label", ""))} · later this season</h4><ul>'
                   + "".join(f'<li><a class="mini hl" href="#{a}" data-target="{a}" title="{esc(tip)}">'
                             f'<b>{esc(dt.date.fromisoformat(iso).strftime("%b %-d"))}</b><span>{esc(lbl)}'
                             f'<small> {esc(dt.date.fromisoformat(iso).strftime("%Y"))}</small></span></a></li>'
                             for iso, a, lbl, tip, _ in hl_items) + "</ul></div>")
    circ_side = hl_side + archive([(r["start"], f"c-{i}",
                          f'{r["city"]}, {r["state"]}' if r["city"] and r["state"] else (r["city"] or r["state"] or r["name"]),
                          f'{r["name"] or "Name TBA"} · {"/".join(r["circuits"])} · {r["region"]}',
                          "hl" if highlighted(r["state"], region=r["region"]) else "")
                         for i, r in enumerate(circuit) if r["start"] <= horizon], "No circuit events in the next 6 months.")
    up_html = "".join(tcard(t) for t in up) or '<p class="muted">No eligible tournaments found right now.</p>'
    n_local = sum(1 for t in up if t["tier"] == "local")

    circ_rows = "".join(
        f'<tr id="c-{i}" class="{"hl" if highlighted(r["state"], region=r["region"]) else ""}"><td>{esc(fmt_range(r["start"], r["end"]))}</td>'
        f'<td>{("<a href=" + chr(34) + esc(r["askfred"]) + chr(34) + ">" + esc(r["name"]) + "</a>") if r["askfred"] else esc(r["name"] or "Name TBA")}'
        f'{"".join("<div class=" + chr(34) + "muted small" + chr(34) + ">" + esc(n) + "</div>" for n in r["notes_extra"])}</td>'
        f'<td>{esc(r["city"])}{", " if r["city"] and r["state"] else ""}{esc(r["state"])}</td>'
        f'<td>{esc(r["region"])}</td><td>{"".join("<span class=tag>" + esc(c) + "</span>" for c in r["circuits"])}</td>'
        f'<td>{esc(", ".join(r["eligible_codes"]))}</td></tr>'
        for i, r in enumerate(circuit))

    nat_cards = "".join(f"""
<article class="ncard{' hl' if highlighted(n['location']) else ''}">
  <div class="muted small">{esc(fmt_range(n['start'], n['end']) if n['start'] else n['when'])} · {esc(days_until(n['start']))}</div>
  <h3>{('<a href="' + esc(n['url']) + '">' + esc(n['name']) + '</a>') if n['url'] else esc(n['name'])}</h3>
  <div class="muted">{esc(n['location'] or 'Location TBA')}</div>
  <div class="small">{esc(n['categories'])}</div>
  <div class="tags">{''.join('<span class="tag tag-fit">' + esc(f) + '</span>' for f in n['fits']) or '<span class="tag tag-none">No eligible events</span>'}</div>
</article>""" for n in nat)

    series = p.get("strength_series", {}).get("S", {"pool": [], "de": []})
    stale_note = ""
    if stale:
        stale_note = f'<p class="stale">Some sources could not be refreshed today and show the last saved data: {esc(", ".join(stale))}.</p>'
    updated = NOW.astimezone(dt.timezone(dt.timedelta(hours=-4))).strftime("%b %-d, %Y at %-I:%M %p ET")

    page = TEMPLATE
    for k, v in {
        "NAME": esc(p.get("name", FENCER["name"])),
        "BIRTH": esc(p.get("birth_year", FENCER["birth_year"])),
        "CLUB": esc(p.get("club", "")),
        "CLUB_URL": esc(p.get("club_url", "")),
        "PROFILE_URL": esc(p.get("profile_url", "")),
        "SEASON": esc(season),
        "TILES": tiles,
        "RESULTS": "".join(res_rows) or '<p class="muted">No results yet.</p>',
        "SEASON_TABLES": season_html,
        "UPCOMING": up_html,
        "SOON": soon_html,
        "CIRC_SIDE": circ_side,
        "HL_LABEL": esc(HIGHLIGHT.get("label", "")),
        "N_UP": str(len(up)),
        "N_LOCAL": str(n_local),
        "LOCAL_MI": str(HOME["local_miles"]),
        "RADIUS": str(HOME["askfred_radius_miles"]),
        "HOME": esc(HOME["location"]),
        "CIRCUIT_ROWS": circ_rows or '<tr><td colspan="6" class="muted">No upcoming circuit events.</td></tr>',
        "REGION": esc(FENCER["region"]),
        "NATIONAL": nat_cards,
        "SERIES": json.dumps(series),
        "UPDATED": esc(updated),
        "STALE": stale_note,
        "BUILD": BUILD_ID,
    }.items():
        page = page.replace("{{" + k + "}}", v)
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(variant(page, keep="PUBLIC", drop="STATS"))
    (SITE / ".nojekyll").write_text("")
    (SITE / "build.txt").write_text(BUILD_ID + "\n")
    stats_dir = SITE / "stats"
    stats_dir.mkdir(exist_ok=True)
    (stats_dir / "index.html").write_text(locked_page(variant(page, keep="STATS", drop="PUBLIC")))


def variant(page, keep, drop):
    """Drop <!--X-->...<!--/X--> blocks for one audience and unwrap the other's."""
    page = re.sub(rf"<!--{drop}-->.*?<!--/{drop}-->", "", page, flags=re.S)
    return page.replace(f"<!--{keep}-->", "").replace(f"<!--/{keep}-->", "")


# PBKDF2 salt is fixed per site so a key remembered in the browser keeps working after daily rebuilds.
STATS_SALT = hashlib.sha256(b"clara-fencing/stats").digest()[:16]
STATS_ITERATIONS = 600_000


def locked_page(private_html):
    """Encrypt the stats page with STATS_PASSWORD (AES-256-GCM); the browser decrypts it with WebCrypto."""
    password = os.environ.get("STATS_PASSWORD", "")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        AESGCM = None
    if not password or AESGCM is None:
        log("[skip] stats page: " + ("STATS_PASSWORD not set" if not password else "cryptography not installed"))
        body = '<p>The stats page has not been set up yet.</p>'
        return LOCK_TEMPLATE.replace("{{PAYLOAD}}", "").replace("{{BODY}}", body)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), STATS_SALT, STATS_ITERATIONS, 32)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, private_html.encode("utf-8"), None)
    payload = json.dumps({
        "salt": base64.b64encode(STATS_SALT).decode(), "iv": base64.b64encode(iv).decode(),
        "iter": STATS_ITERATIONS, "ct": base64.b64encode(ct).decode()})
    return LOCK_TEMPLATE.replace("{{PAYLOAD}}", payload).replace("{{BODY}}", LOCK_FORM)


LOCK_FORM = """<form id="f">
  <label for="pw">Password</label>
  <input id="pw" type="password" autocomplete="current-password" autofocus required>
  <label class="rem"><input id="rem" type="checkbox" checked> Remember on this device</label>
  <button type="submit">Unlock</button>
  <p id="err" role="alert"></p>
</form>"""

LOCK_TEMPLATE = (Path(__file__).resolve().parent / "lock.html").read_text()


TEMPLATE = (Path(__file__).resolve().parent / "template.html").read_text()


def main():
    DATA.mkdir(exist_ok=True)
    stale = []
    profile, s = cached("fencingtracker", ft_profile)
    if s:
        stale.append("fencingtracker")

    events_path = DATA / "events.json"
    events_cache = json.loads(events_path.read_text()) if events_path.exists() else {}
    for c in (profile or {}).get("competitions", []):
        if c["id"] not in events_cache and c["event_url"]:
            try:
                events_cache[c["id"]] = ft_event(c["event_url"])
                time.sleep(1)
            except Exception as e:  # noqa: BLE001
                log(f"  event {c['id']}: {e}")
    events_path.write_text(json.dumps(events_cache, indent=1, ensure_ascii=False))

    fred, s = cached("askfred", askfred)
    if s:
        stale.append("AskFRED")
    national, s = cached("usaf_national", usaf_national)
    if s:
        stale.append("USA Fencing national")
    regional, s = cached("usaf_regional", usaf_regional)
    if s:
        stale.append("USA Fencing regional")

    up, circuit, nat = build_upcoming(fred, regional, national, profile)
    (DATA / "upcoming.json").write_text(json.dumps(
        {"generated": NOW.isoformat(), "askfred": up, "circuit": circuit, "national": nat}, indent=1, ensure_ascii=False))
    render(profile, events_cache, up, circuit, nat, stale)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"stale={', '.join(stale)}\n")
    log(f"built site: {len(up)} eligible AskFRED tournaments, {len(circuit)} circuit events, {len(nat)} national events")


if __name__ == "__main__":
    main()
