# clara-fencing

Clara Tohumcu's fencing stats and the upcoming saber tournaments she can enter, rebuilt every day and published with GitHub Pages.

- **Stats** come from [fencingtracker](https://fencingtracker.com/p/102185990/Clara-Tohumcu): the summary, bout history, strength and event results.
- **Upcoming tournaments** come from [AskFRED](https://www.askfred.net/tournaments), within `askfred_radius_miles` of `home.location`. They are filtered to the saber events she is eligible for by age (from her birth year) and rating.
- **Circuit and national calendars** come from USA Fencing's [national events](https://www.usafencing.org/events-national) page and its regional circuit calendar on Airtable.

## Public page and private stats

The public page (`site/index.html`) shows only the tournament finder. Clara's stats are on `site/stats/`, encrypted with AES-256-GCM using a key derived from the `STATS_PASSWORD` secret (PBKDF2, 600k iterations). The browser decrypts the page after the password is entered, and the password is never sent anywhere. Her stats data (`data/fencingtracker.json`, `data/events.json`) is not committed.

To set or change the password:

```bash
gh secret set STATS_PASSWORD
```

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install "cryptography<45"
STATS_PASSWORD=... .venv/bin/python scripts/update.py
```

This writes `data/*.json` and `site/index.html`, and needs no dependencies. To change the fencer, home location or search radius, edit `config.json`.

## Automation

`.github/workflows/update.yml` runs every day at about 6am ET, on every push to `main`, and on demand from the Actions tab. It refreshes the data, commits `data/`, and deploys `site/` to Pages. If a source can't be reached, the last saved data is used and the page shows a notice.
