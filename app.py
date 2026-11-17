import os, sqlite3, math, json, datetime as dt
from pathlib import Path
import numpy as np
import pandas as pd
import requests
import streamlit as st
from scipy.stats import norm, poisson

st.set_page_config(page_title='Prop Edge Lab', page_icon='📈', layout='wide')
st.title('📈 Prop Edge Lab')
st.caption('Research prototype • paper testing only • no automatic bets • no guaranteed returns')
DB = Path(os.getenv('PROP_DB', 'prop_scanner.sqlite'))
SPORTS = {'NBA':'basketball_nba','CBB':'basketball_ncaab','NFL':'americanfootball_nfl','CFB':'americanfootball_ncaaf','MLB':'baseball_mlb'}
ALL_SPORTS = ['NBA','CBB','NFL','CFB','CS2','Tennis','Cricket','MLB','PGA Golf']

@st.cache_data(ttl=300,show_spinner=False)
def fetch_events(key, sport):
    url=f'https://api.the-odds-api.com/v4/sports/{sport}/events'
    r=requests.get(url,params={'apiKey':key},timeout=15); r.raise_for_status(); return r.json()

@st.cache_data(ttl=300,show_spinner=False)
def fetch_event_odds(key,sport,event_id,regions,markets):
    url=f'https://api.the-odds-api.com/v4/sports/{sport}/events/{event_id}/odds'
    r=requests.get(url,params={'apiKey':key,'regions':regions,'markets':markets,'oddsFormat':'american'},timeout=15)
    r.raise_for_status(); return r.json(),dict(r.headers)

def init_db():
    with sqlite3.connect(DB) as c:
        c.execute('CREATE TABLE IF NOT EXISTS picks (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, sport TEXT, event TEXT, player TEXT, market TEXT, line REAL, side TEXT, model_prob REAL, market_prob REAL, edge REAL, platform TEXT, payout_multiplier REAL, status TEXT DEFAULT "Pending", actual REAL, closing_line REAL, notes TEXT)')
init_db()

def american_prob(odds):
    odds=float(odds)
    return (-odds)/(-odds+100) if odds<0 else 100/(odds+100)

def prob_model(mu, sigma, line, side, distribution):
    if distribution=='Poisson (counts)':
        # integer outcomes; half-point lines are safest; integer lines may push
        if side=='Over': return float(poisson.sf(math.floor(line),mu))
        return float(poisson.cdf(math.ceil(line)-1,mu))
    z=(line-mu)/sigma
    return float(norm.sf(z) if side=='Over' else norm.cdf(z))

def normalize_upload(df):
    required={'sport','event','player','market','line','platform'}
    missing=required-set(df.columns)
    if missing: raise ValueError('Missing CSV columns: '+', '.join(sorted(missing)))
    df=df.copy(); df['line']=pd.to_numeric(df['line'],errors='coerce')
    return df.dropna(subset=['line'])

def add_pick(row):
    cols=['created_at','sport','event','player','market','line','side','model_prob','market_prob','edge','platform','payout_multiplier','notes']
    vals=[dt.datetime.now(dt.timezone.utc).isoformat()]+[row.get(x) for x in cols[1:]]
    with sqlite3.connect(DB) as c:
        c.execute('INSERT INTO picks ('+','.join(cols)+') VALUES ('+','.join(['?']*len(cols))+')',vals)

with st.sidebar:
    st.header('Configuration')
    api_key=st.text_input('The Odds API key (optional)',value=st.secrets.get('ODDS_API_KEY','') if hasattr(st,'secrets') else '',type='password')
    st.caption('Free tier has limited credits and may not include all player props. Check provider coverage and quotas before querying.')
    st.info('Streamlit Community Cloud local SQLite storage is ephemeral. Export CSV regularly. For persistent multi-device history, connect a hosted database later.')
    st.markdown('[Get an API key](https://the-odds-api.com/) · [Deploy Streamlit](https://share.streamlit.io/)')

tab1,tab2,tab3,tab4=st.tabs(['🔎 Scan markets','🧮 Evaluate prop','🗂️ Paper tracker','📘 Setup'])
with tab1:
    st.subheader('Live sportsbook market discovery')
    sport=st.selectbox('Sport',list(SPORTS),key='live_sport')
    st.caption('CS2, tennis and cricket are supported by CSV/manual analysis; live event endpoints depend on your data provider.')
    regions=st.selectbox('Odds region',['us','us2','uk','eu','au'])
    markets=st.text_input('Market keys supported by your plan/provider','player_points,player_rebounds,player_assists')
    if st.button('Fetch live events'):
        if not api_key: st.error('Enter a provider API key first. No demo odds will be presented as live.')
        else:
            try:
                events=fetch_events(api_key,SPORTS[sport]); st.session_state['events']=events
                st.success(f'{len(events)} events retrieved')
            except Exception as e: st.error(f'Provider request failed: {e}')
    events=st.session_state.get('events',[])
    if events and api_key:
        options={f"{e.get('home_team')} vs {e.get('away_team')} • {e.get('commence_time')}":e['id'] for e in events}
        chosen=st.selectbox('Event',list(options))
        if st.button('Fetch event player props (may consume API credits)'):
            try:
                data,headers=fetch_event_odds(api_key,SPORTS[sport],options[chosen],regions,markets)
                rows=[]
                for book in data.get('bookmakers',[]):
                    for m in book.get('markets',[]):
                        for o in m.get('outcomes',[]):
                            rows.append({'book':book.get('title'),'market':m.get('key'),'player':o.get('description',''),'side':o.get('name'),'line':o.get('point'),'odds':o.get('price'),'updated':m.get('last_update')})
                st.session_state['odds_df']=pd.DataFrame(rows)
                st.caption('Remaining provider requests: '+str(headers.get('x-requests-remaining','not reported')))
            except Exception as e: st.error(f'Odds unavailable: {e}. This may be a free-plan restriction or unsupported market.')
    odds_df=st.session_state.get('odds_df',pd.DataFrame())
    if not odds_df.empty:
        st.dataframe(odds_df,use_container_width=True)
        st.download_button('Export sportsbook snapshot',odds_df.to_csv(index=False),'sportsbook_snapshot.csv')
        st.markdown('**Consensus estimates from matched over/under pairs**')
        pairs=[]
        for (player,market,line,book),g in odds_df.dropna(subset=['line']).groupby(['player','market','line','book']):
            overs=g[g.side.str.lower()=='over']; unders=g[g.side.str.lower()=='under']
            if len(overs) and len(unders):
                po=american_prob(overs.iloc[0].odds); pu=american_prob(unders.iloc[0].odds)
                pairs.append({'player':player,'market':market,'line':line,'book':book,'fair_over':po/(po+pu),'fair_under':pu/(po+pu)})
        if pairs:
            consensus=pd.DataFrame(pairs).groupby(['player','market','line'],as_index=False).agg(fair_over=('fair_over','median'),fair_under=('fair_under','median'),books=('book','nunique'))
            st.dataframe(consensus.sort_values('books',ascending=False),use_container_width=True)
            st.download_button('Export consensus',consensus.to_csv(index=False),'consensus.csv')
        else: st.info('No matched Over/Under pairs at identical lines were returned.')
    st.divider()
    st.subheader('Import PrizePicks / Underdog lines')
    template=pd.DataFrame([{'sport':'NBA','event':'Example A vs B','player':'Example Player','market':'points','line':19.5,'platform':'PrizePicks'}])
    st.download_button('Download CSV template',template.to_csv(index=False),'platform_lines_template.csv')
    upload=st.file_uploader('Upload a CSV of platform lines',type='csv')
    if upload:
        try:
            df=normalize_upload(pd.read_csv(upload)); st.dataframe(df,use_container_width=True)
            st.info('Imported lines are not automatically matched to sportsbooks yet. Use the Evaluate tab with verified market odds and projections. No fabricated comparisons.')
        except Exception as e: st.error(str(e))
with tab2:
    st.subheader('Independent projection + market comparison')
    st.caption('All numbers here are user-entered unless explicitly sourced from the live tab. Do not mistake assumptions for verified probabilities.')
    a,b,c=st.columns(3)
    with a:
        es=st.selectbox('Sport',ALL_SPORTS); player=st.text_input('Player'); event=st.text_input('Event'); market=st.text_input('Market / stat','points')
    with b:
        platform=st.selectbox('Platform',['PrizePicks','Underdog']); line=st.number_input('Platform line',value=19.5,step=.5); side=st.selectbox('Side',['Over','Under']); dist=st.selectbox('Model distribution',['Normal (continuous approximation)','Poisson (counts)'])
    with c:
        mu=st.number_input('Independent projection (mean)',value=21.0,step=.5); sigma=st.number_input('Estimated standard deviation',value=5.5,min_value=.01,step=.5,disabled=dist.startswith('Poisson'))
        market_over=st.number_input('No-vig market probability of OVER (%)',value=50.,min_value=0.,max_value=100.,step=.5)
        payout=st.number_input('Net payout per $1 winning selection (illustrative)',value=1.0,min_value=0.0,step=.1)
    p=prob_model(mu,sigma,line,side,dist); mp=market_over/100 if side=='Over' else 1-market_over/100
    edge=p-mp; ev=p*payout-(1-p)
    m1,m2,m3,m4=st.columns(4)
    m1.metric('Model hit estimate',f'{p:.1%}');m2.metric('Market estimate',f'{mp:.1%}');m3.metric('Model-market gap',f'{edge:+.1%}');m4.metric('Illustrative binary EV',f'{ev:+.1%}')
    st.warning('This EV assumes a standalone binary payout. PrizePicks/Underdog multi-pick entries require joint probabilities, correlations and the exact payout table. Integer lines may push; this basic calculator does not model pushes.')
    notes=st.text_area('Evidence / injury and lineup checks / source timestamps')
    if st.button('Save as paper prediction'):
        if not player.strip() or not event.strip(): st.error('Player and event required')
        else:
            add_pick({'sport':es,'event':event,'player':player,'market':market,'line':line,'side':side,'model_prob':p,'market_prob':mp,'edge':edge,'platform':platform,'payout_multiplier':payout,'notes':notes})
            st.success('Paper prediction saved')
with tab3:
    st.subheader('Results and validation')
    with sqlite3.connect(DB) as conn: picks=pd.read_sql_query('SELECT * FROM picks ORDER BY id DESC',conn)
    if picks.empty: st.info('No paper predictions yet. Save a projection in Evaluate prop.')
    else:
        graded=picks[picks.status.isin(['Win','Loss'])]
        k1,k2,k3=st.columns(3)
        k1.metric('Predictions',len(picks));k2.metric('Graded',len(graded));k3.metric('Hit rate',f'{(graded.status=="Win").mean():.1%}' if len(graded) else '—')
        if len(graded):
            outcome=(graded.status=='Win').astype(float)
            st.metric('Brier score (lower is better)',f'{np.mean((graded.model_prob-outcome)**2):.3f}')
            st.caption('ROI cannot be inferred reliably from per-leg payout assumptions on multi-pick entries.')
        st.dataframe(picks,use_container_width=True)
        st.download_button('Back up all predictions (CSV)',picks.to_csv(index=False),'prop_predictions_backup.csv')
        ident=st.selectbox('Grade prediction ID',picks.id.tolist(),format_func=lambda i: f'#{i} — '+str(picks.loc[picks.id==i,'player'].iloc[0]))
        status=st.selectbox('Outcome',['Pending','Win','Loss','Push','Void'])
        actual=st.number_input('Actual stat',value=0.,step=.5)
        closing=st.number_input('Closing platform line (if observed)',value=0.,step=.5)
        if st.button('Update result'):
            with sqlite3.connect(DB) as conn: conn.execute('UPDATE picks SET status=?, actual=?, closing_line=? WHERE id=?',(status,actual,closing if closing else None,int(ident)))
            st.success('Updated. Refresh page to see new metrics.')
with tab4:
    st.markdown('''### Zero-cost deployment
1. Create a free GitHub account and a repository; upload `app.py` and `requirements.txt`.
2. Create a free account at [Streamlit Community Cloud](https://share.streamlit.io/), connect GitHub and deploy `app.py`.
3. Optional: register for a free key at [The Odds API](https://the-odds-api.com/). Enter the key in the sidebar, or configure `ODDS_API_KEY` as a Streamlit secret.
4. Fetch events, then request supported player-prop markets. API availability and credits vary; a free tier may not include player props.
5. Import PrizePicks/Underdog CSV lines and manually enter independent projections in **Evaluate prop**. Save selections before games start, then grade them in **Paper tracker**.

### Honest limitations
- No automatic PrizePicks/Underdog scraping, no unauthorized APIs, and no fake live picks.
- No automatic injury feeds or independent projections yet; the evaluator uses your supplied assumptions.
- The Odds API event/player market coverage may require paid access.
- This prototype does **not** continuously scan in the background; Streamlit refreshes only when you open it and request data.
- SQLite on free Streamlit Cloud is not durable across app restarts. Export backups after each session.
- Multi-pick payout/EV modeling and automated settlement are future upgrades.
- The system is a **research prototype**, not a validated predictive edge.
''')
