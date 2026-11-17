# Prop Edge Lab — $0 prototype

A Streamlit research app with live odds-provider integration (when supported by the user's API plan), no-vig sportsbook consensus, manual PrizePicks/Underdog CSV import, distribution-based probability estimation, and paper-pick tracking.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy free to iPhone-accessible cloud
1. Sign up for free GitHub and Streamlit Community Cloud accounts.
2. Upload `app.py` and `requirements.txt` to a GitHub repository.
3. On https://share.streamlit.io/ select **Create app**, choose the repository, and set main file to `app.py`.
4. Optional: register at https://the-odds-api.com/ and supply your API key in the app sidebar.
5. Visit your Streamlit app URL on your iPhone; add it to your home screen if desired.

## Important constraints
The Odds API free-plan quota and access to event-level player props can be limited. No provider key is included. PrizePicks/Underdog odds are NOT automatically retrieved. There is no background scanner or automatic injury/model pipeline yet. Uploaded CSV lines are not auto-matched. No claims of profitability. Local SQLite data on Streamlit Cloud may disappear on restart; export CSV regularly.

## Data sources and permissions
Use authorized data feeds. Do not bypass platform access controls or violate site terms. All real market observations should be timestamped. Paper-test out of sample and use actual multi-pick payouts before estimating ROI.
