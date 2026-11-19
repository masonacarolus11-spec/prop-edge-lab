# Prop Edge Lab — $0 prototype

A Streamlit research app with live odds-provider integration (when supported by the user's API plan), no-vig sportsbook consensus, manual PrizePicks/Underdog CSV import, distribution-based probability estimation, and paper-pick tracking.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy free to iPhone-accessible cloud
1. Sign up for free GitHub and Streamlit Community Cloud accounts.
2. Use the complete GitHub repository, including the provider and source modules.
3. On https://share.streamlit.io/ select **Create app**, choose the repository, and set main file to `app.py`.
4. Optional: register at https://the-odds-api.com/ and supply your API key in the app sidebar.
5. Visit your Streamlit app URL on your iPhone; add it to your home screen if desired.

## Important constraints
The Odds API free-plan quota and access to event-level player props can be limited. No provider key is included. PrizePicks/Underdog odds are NOT automatically retrieved. The staged background job discovers events. Player-stat and injury adapters are pending. Uploaded CSV lines are not auto-matched. No claims of profitability. Local SQLite data on Streamlit Cloud may disappear on restart; export CSV regularly.

## Data sources and permissions
Use authorized data feeds. Do not bypass platform access controls or violate site terms. All real market observations should be timestamped. Paper-test out of sample and use actual multi-pick payouts before estimating ROI.

## Nine-sport research and daily discovery

Open **Research sources** for the nine-sport catalog and links to candidate
statistical sources. An Odds API key enables a check of active competitions.
NBA, CBB, NFL, CFB, and MLB retain the existing player-prop scanner. Tennis
and cricket coverage is discovered by tournament. Golf catalog entries are
major-winner futures. CS2 has a candidate research source and needs an odds
adapter. Candidate links require separate integration and coverage/licensing
review. All nine sports remain available for manual evaluation.

The app caches successful provider catalog, event, and event-odds responses
for 60, 10, and 5 minutes respectively, keyed by credential and inputs.
The provider client spaces requests by at least 500 ms within a process and
credential and honors HTTP 429 Retry-After with a 30-second minimum cooldown.
The provider's quota is shared across processes that use the same key.
Catalog and event discovery are quota-free under The Odds API v4. Event odds
can consume credits by market and region. Displayed credits are from the
response at fetch time.

Run `python daily_scan.py` with `ODDS_API_KEY` in the environment for an event
report. With no key it writes coverage status without contacting a provider.
Each run takes one catalog snapshot, makes at most 12 event requests, and
records up to 12 events per competition. A rate limit or access denial stops
further event requests. The output contains source and event status, with no
player stats or predictions.

The GitHub Actions schedule is 6:00 AM in `America/Los_Angeles`, including
daylight-saving changes. Scheduled runs require the workflow on the default
branch and can start late. Configure `ODDS_API_KEY` as a GitHub Actions
repository secret when enabling live scans. The workflow retains the
`sharpline-daily-snapshot` JSON artifact for seven days. Streamlit and its
SQLite tracker run independently of the workflow.
