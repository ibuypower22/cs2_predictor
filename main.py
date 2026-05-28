import hashlib
import json
import os
import pickle
import re
import statistics

import numpy as np
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from datetime import datetime
import psycopg2

from model import (
    predict_match, team_player_stats, team_match_stats, predict_map, fetch_maps
)
from parser_worker import get_html_with_cloudflare_bypass

MODELS_DIR = "."

def safe_load(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    return None

lr_model = safe_load(os.path.join(MODELS_DIR, "lr_model.pkl"))
xgb_model = safe_load(os.path.join(MODELS_DIR, "xgb_model.pkl"))
cb_model = safe_load(os.path.join(MODELS_DIR, "cb_model.pkl"))
le = safe_load(os.path.join(MODELS_DIR, "label_encoder.pkl"))
team_history = safe_load(os.path.join(MODELS_DIR, "team_history.pkl")) or {}
h2h_counts = safe_load(os.path.join(MODELS_DIR, "h2h_counts.pkl")) or {}
team_maps_diff = safe_load(os.path.join(MODELS_DIR, "team_maps_diff.pkl")) or {}

models_dict = {k:v for k,v in [
    ("Logistic Regression", lr_model),
    ("XGBoost", xgb_model),
    ("CatBoost", cb_model)
] if v is not None}

# ----------------- DB Params -----------------
DB_PARAMS = {
        "dbname": "cs2_matches",
        "user": "postgres",
        "password": "12345",
        "host": "localhost",
        "port": 5432
    }

def get_matches():
    from parser_worker import parser
    live_matches = []
    upcoming_matches = []
    seen_matches = set()

    html = parser.get_page("https://www.hltv.org/matches")
    if not html: return [], []

    soup = BeautifulSoup(html, "html.parser")

    for mw in soup.select(".match-wrapper"):
        teams = mw.select(".match-teamname")
        if len(teams) != 2: continue

        team1 = teams[0].text.strip()
        team2 = teams[1].text.strip()
        link = mw.select_one("a")['href']

        match_key = link

        if match_key in seen_matches:
            continue
        seen_matches.add(match_key)

        tournament_el = mw.select_one(".match-event")
        tournament = "Unknown"
        if tournament_el:
            tournament = tournament_el.get_text(strip=True)
            if tournament_el.has_attr("data-event-headline"):
                tournament = tournament_el["data-event-headline"]

        match_data = {
            "team1": team1,
            "team2": team2,
            "match_link": "https://www.hltv.org" + link,
            "tournament": tournament,
            "upcoming_maps": [],  # Поле для будущего парсинга
            "status": "live" if "liveMatches" in mw.parent.get("class", []) else "upcoming"
        }

        if match_data["status"] == "live":
            live_matches.append(match_data)
        else:
            upcoming_matches.append(match_data)

    return live_matches, upcoming_matches


conn = psycopg2.connect(**DB_PARAMS)
cur = conn.cursor()

def get_matches_cached(force_reload=False):
    cur = conn.cursor()

    cur.execute("""
        SELECT live, upcoming, last_update 
        FROM matches_cache 
        ORDER BY last_update DESC LIMIT 1
    """)
    row = cur.fetchone()

    db_live = row[0] if row else None
    db_upcoming = row[1] if row else None
    last_update = row[2] if row else None

    now = datetime.now()
    db_is_fresh = False

    if last_update:
        age_sec = (now - last_update).total_seconds()
        if age_sec < 15 * 60:
            db_is_fresh = True

    if db_live and db_upcoming and db_is_fresh and not force_reload:
        return db_live + db_upcoming

    try:
        live_matches, upcoming_matches = get_matches()

        cur.execute("""
            INSERT INTO matches_cache (id, live, upcoming, last_update)
            VALUES (1, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE
            SET live = EXCLUDED.live,
                upcoming = EXCLUDED.upcoming,
                last_update = EXCLUDED.last_update
        """, (json.dumps(live_matches), json.dumps(upcoming_matches), now))
        conn.commit()

        return [*live_matches, *upcoming_matches]

    except Exception as e:
        print("[WARN] Parsing failed:", e)

        if db_live or db_upcoming:
            return (db_live or []) + (db_upcoming or [])

        return []

st.title("CS2 Matches Predictor")
st.markdown(
    """
    <style>
    .main .block-container {
        max-width: 1000px;
    }

    div.stMarkdown, 
    div.stText, 
    div.stInfo, 
    div.stAlert, 
    .stTextInput label, 
    .stTextInput input, 
    .stSelectbox div, 
    .stRadio div, 
    .stButton button {
        font-size: 18px !important;
    }

    .match-row {
        white-space: nowrap;
        font-size: 18px !important;
    }
    .match-row span {
        margin-left: 15px;
        font-weight: normal;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# manual_matches = [
#     {
#         "team1": "Spirit",
#         "team2": "Vitality",
#         "tournament": "Custom Tournament",
#         "match_link": None,
#         "upcoming_maps": []
#     },
#     {
#         "team1": "FURIA",
#         "team2": "MOUZ",
#         "tournament": "Custom Tournament",
#         "match_link": None,
#         "upcoming_maps": []
#     }
# ]
#
# for m in manual_matches:
#     players_stats_team1 = team_player_stats(m['team1'], None, cur)
#     players_stats_team2 = team_player_stats(m['team2'], None, cur)
#     team1_info = team_match_stats(m['team1'], None, cur)
#     team2_info = team_match_stats(m['team2'], None, cur)
#
#     prob1, prob2, stats_info = predict_match(
#         team1=m['team1'],
#         team2=m['team2'],
#         match_id=None,
#         cur=cur,
#         conn=conn,
#         players_stats_team1=players_stats_team1,
#         players_stats_team2=players_stats_team2,
#         team1_info=team1_info,
#         team2_info=team2_info,
#         maps_list=m.get("upcoming_maps")
#     )
#     print(f"{m['team1']} {prob1}%, {m['team2']} {prob2}%")


if "status_filter" not in st.session_state:
    st.session_state.status_filter = "live"
if "refresh_flag" not in st.session_state:
    st.session_state.refresh_flag = 0

if st.button("Reload matches"):
    st.session_state.refresh_flag += 1
    matches = get_matches_cached(force_reload=True)
else:
    if "matches" not in st.session_state:
        with st.spinner("Loading matches, wait..."):
            matches = get_matches_cached()
    else:
        matches = get_matches_cached()


st.session_state.status_filter = st.selectbox(
    "Choose match status",
    ["live", "upcoming"],
    index=0 if st.session_state.status_filter == "live" else 1
)

filtered_matches = [m for m in matches if m["status"] == st.session_state.status_filter]

st.write(f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
st.write(f"Matches found: {len(filtered_matches)}")

search_team = st.text_input("Search by team name (enter the name):").strip().lower()
if search_team:
    filtered_matches = [
        m for m in filtered_matches
        if search_team in m['team1'].lower() or search_team in m['team2'].lower()
    ]
    st.write(f"Founded matches after filter: {len(filtered_matches)}")

if any(st.session_state.get(f"forecast_type_{i}", "Match prediction") == "Map prediction" for i in range(len(filtered_matches))):

    selected_model_name = "Default model"
    st.selectbox(
        "Choose the model",
        ["Default model"],
        index=0,
        disabled=True
    )
else:
    if models_dict:
        selected_model_name = st.selectbox(
            "Choose the model",
            ["Default model"] + list(models_dict.keys())
        )
    else:
        selected_model_name = "Default model"

def build_model_features(team1, team2):
    if le is None:
        raise RuntimeError("LabelEncoder not loaded.")

    if team1 not in le.classes_ or team2 not in le.classes_:
        return None

    t1_enc = int(le.transform([team1])[0])
    t2_enc = int(le.transform([team2])[0])

    last_n = 5
    hist1 = team_history.get(team1, [])
    hist2 = team_history.get(team2, [])
    form1 = float(np.mean(hist1[-last_n:])) if hist1 else 0.0
    form2 = float(np.mean(hist2[-last_n:])) if hist2 else 0.0

    key = frozenset([team1, team2])
    pair = h2h_counts.get(key, {team1: 0, team2: 0})
    h2h_val = int(pair.get(team1, 0) - pair.get(team2, 0))

    team1_maps_diff = team_maps_diff.get(team1, 0)
    team2_maps_diff = team_maps_diff.get(team2, 0)

    X_new = pd.DataFrame([{
        'team1_enc': t1_enc,
        'team2_enc': t2_enc,
        'team1_form': form1,
        'team2_form': form2,
        'h2h': h2h_val,
        'team1_maps_diff': team1_maps_diff,
        'team2_maps_diff': team2_maps_diff
     }])

    return X_new

if "prev_status_filter" not in st.session_state:
    st.session_state.prev_status_filter = st.session_state.status_filter
elif st.session_state.prev_status_filter != st.session_state.status_filter:

    keys_to_reset = [k for k in st.session_state.keys() if k.startswith("forecast_type_") or k.startswith("map_")]
    for k in keys_to_reset:
        if k.startswith("forecast_type_"):
            st.session_state[k] = "Match prediction"
        else:
            st.session_state[k] = None
    st.session_state.prev_status_filter = st.session_state.status_filter


for i, m in enumerate(filtered_matches):
    link_text = (f"[{m.get('status', 'N/A').capitalize()}] "
                 f"{m.get('tournament', 'Unknown')}: "
                 f"{m.get('team1', 'TBD')} vs {m.get('team2', 'TBD')}")
    match_link = m.get("match_link", "#")


    match_time = f"{m['match_time']}" if m.get("match_time") else ""

    html = f'<div class="match-row"><a href="{match_link}" target="_blank">{link_text}</a> <span>{match_time}</span></div>'
    st.markdown(html, unsafe_allow_html=True)

    forecast_key = f"forecast_type_{i}"
    map_key = f"map_{i}"

    if forecast_key not in st.session_state:
        st.session_state[forecast_key] = "Match prediction"
    if map_key not in st.session_state:
        st.session_state[map_key] = None

    forecast_type = st.radio(
        "Choose prediction type",
        ["Match prediction", "Map prediction"],
        key=forecast_key
    )

    match_id_hash = hashlib.md5(match_link.encode()).hexdigest()
    maps_key = f"maps_data_{match_id_hash}"

    is_live = m.get("status") == "live"

    if forecast_type == "Map prediction":
        if is_live or maps_key not in st.session_state:
            match_data = fetch_maps(match_link)
            if match_data and match_data.get("maps"):
                st.session_state[maps_key] = [item["map"] for item in match_data["maps"] if
                                              item.get("action") != "removed"]
            else:
                st.session_state[maps_key] = None

    if forecast_type == "Map prediction":
        data = st.session_state.get(maps_key)
        if data:
            selected_map = st.selectbox(
                f"Choose map: {m['team1']} vs {m['team2']}",
                data,
                key=f"select_{match_id_hash}"
            )
        else:
            st.info("Maps not yet determined.")

    if st.button(f"Make prediction for {m['team1']} vs {m['team2']}", key=f"btn_{i}"):

        # 1. Скачиваем один раз
        html, status = get_html_with_cloudflare_bypass(m['match_link'])

        if status != 200:
            st.error("Не удалось загрузить данные матча (403/Error).")
            continue

        team1_info = team_match_stats(m['team1'], html, cur, conn)
        team2_info = team_match_stats(m['team2'], html, cur, conn)

        players_stats_team1 = team_player_stats(m['team1'], html, team1_info["hltv_id"] if team1_info else None, cur,
                                                conn)
        players_stats_team2 = team_player_stats(m['team2'], html, team2_info["hltv_id"] if team2_info else None, cur,
                                                conn)

        if not players_stats_team1:
            players_stats_team1 = [
                {"hltv_id": 0, "nickname": "No Data", "rating": 0.0, "round_swing": 0.0, "dpr": 0.0, "kast": 0.0,
                 "multi_kill": 0.0, "adr": 0.0, "kpr": 0.0}]
        if not players_stats_team2:
            players_stats_team2 = [
                {"hltv_id": 0, "nickname": "No Data", "rating": 0.0, "round_swing": 0.0, "dpr": 0.0, "kast": 0.0,
                 "multi_kill": 0.0, "adr": 0.0, "kpr": 0.0}]

        match_id = None
        mobj = re.search(r"/matches/(\d+)", m.get('match_link', ""))
        if mobj:
            match_id = int(mobj.group(1))

        maps_list = m.get("upcoming_maps") or None

        if forecast_type == "Map prediction":
            match_data = fetch_maps(match_link)
            prob1, prob2 = predict_map(
                team1=m['team1'],
                team2=m['team2'],
                map_name=selected_map,
                players_stats_team1=players_stats_team1,
                players_stats_team2=players_stats_team2,
                team1_maps_wr=team1_info.get("maps_wr") if team1_info else None,
                team2_maps_wr=team2_info.get("maps_wr") if team2_info else None,
                team1_wr=team1_info.get("lineup_wr") if team1_info else None,
                team2_wr=team2_info.get("lineup_wr") if team2_info else None,
                team1_rank=team1_info.get("world_rank") if team1_info else None,
                team2_rank=team2_info.get("world_rank") if team2_info else None,
                team1_avg_age=team1_info.get("avg_age") if team1_info else None,
                team2_avg_age=team2_info.get("avg_age") if team2_info else None,
                match_id=match_id,
                conn=conn,
                cur=cur
            )

            st.info(f"Map prediction {selected_map}: {m['team1']} {prob1}%, {m['team2']} {prob2}%")
        else:
            prob1, prob2, stats_info = predict_match(
                team1=m['team1'],
                team2=m['team2'],
                match_id=match_id,
                cur=cur,
                conn=conn,
                players_stats_team1=players_stats_team1,
                players_stats_team2=players_stats_team2,
                team1_info=team1_info,
                team2_info=team2_info,
                maps_list=maps_list
            )

            st.info(f"Match prediction: {m['team1']} {prob1}%, {m['team2']} {prob2}%")

            if selected_model_name != "Default model":
                model = models_dict.get(selected_model_name)
                if model is None:
                    st.warning("Chosen model not available.")
                else:
                    X_new = build_model_features(m['team1'], m['team2'])
                    if X_new is None:
                        st.warning("There is no encoding for one of the teams - the model cannot make a prediction.")
                    else:
                        probs = model.predict_proba(X_new)[0]
                        pred = int(model.predict(X_new)[0])
                        winner = m['team1'] if pred == 1 else m['team2']
                        prob_pct = probs[pred] * 100
                        st.success(f"ML-model **{selected_model_name}** prediction: **{winner}** ({prob_pct:.2f}%)")

        def aggregate_stats(stats_list):
            keys = ["rating", "round_swing", "dpr", "kast", "multi_kill", "adr", "kpr"]
            if not stats_list:
                return {k: 0.0 for k in keys}
            agg = {}
            for k in keys:
                values = [float(p.get(k, 0)) for p in stats_list]
                if k == "round_swing":
                    agg[k] = round(statistics.median(values), 2)
                else:
                    agg[k] = round(sum(values) / len(values), 2)
            return agg

        agg1 = aggregate_stats(players_stats_team1)
        agg2 = aggregate_stats(players_stats_team2)

        team1_rank = team1_info['world_rank'] if team1_info else "?"
        team2_rank = team2_info['world_rank'] if team2_info else "?"
        team1_avg_age = team1_info['avg_age'] if team1_info else "?"
        team2_avg_age = team2_info['avg_age'] if team2_info else "?"
        team1_maps_wr = team1_info.get('maps_wr', {}) if team1_info else {}
        team2_maps_wr = team2_info.get('maps_wr', {}) if team2_info else {}
        team_wr1 = team1_info.get("lineup_wr") if team1_info else 0.0
        team_wr2 = team2_info.get("lineup_wr") if team2_info else 0.0

        if forecast_type == "Map prediction":
            map_wr1 = team1_maps_wr.get(selected_map.lower(), 50)
            map_wr2 = team2_maps_wr.get(selected_map.lower(), 50)
            stats_text = (
                f"<div style='background-color:#d4edda; color:#155724; padding:10px; border-radius:5px; border:1px solid #c3e6cb;'>"
                f"<strong>Team winrate:</strong> {m['team1']}: {team_wr1}%, {m['team2']}: {team_wr2}%<br>"
                f"<strong>Place in hltv ranking:</strong> {m['team1']}: #{team1_rank}, {m['team2']}: #{team2_rank}<br>"
                f"<strong>Average age of players:</strong> {m['team1']}: {team1_avg_age}, {m['team2']}: {team2_avg_age}<br><br>"
                f"<strong>Average players stats:</strong><br>"
                f"{m['team1']}: Rating {agg1['rating']}, Round Swing {agg1['round_swing']}, DPR {agg1['dpr']}, "
                f"KAST {agg1['kast']}, Multi-Kill {agg1['multi_kill']}, ADR {agg1['adr']}, KPR {agg1['kpr']}<br>"
                f"{m['team2']}: Rating {agg2['rating']}, Round Swing {agg2['round_swing']}, DPR {agg2['dpr']}, "
                f"KAST {agg2['kast']}, Multi-Kill {agg2['multi_kill']}, ADR {agg2['adr']}, KPR {agg2['kpr']}<br><br>"
                f"<strong>Map winrate {selected_map}:</strong> {m['team1']}: {map_wr1}%, {m['team2']}: {map_wr2}%"
                f"</div>"
            )
        else:
            stats_text = (
                f"<div style='background-color:#d4edda; color:#155724; padding:10px; border-radius:5px; border:1px solid #c3e6cb;'>"
                f"<strong>Last 5 matches form:</strong> {m['team1']}: {stats_info['form1']}, "
                f"{m['team2']}: {stats_info['form2']}<br><br>"
                f"<strong>Head-to-head:</strong><br>{'<br>'.join(stats_info['h2h'])}<br><br>"
                f"<strong>Team winrate:</strong> {m['team1']}: {stats_info['team_wr1']}%, {m['team2']}: {stats_info['team_wr2']}%<br>"
                f"<strong>Place in hltv ranking:</strong> {m['team1']}: #{team1_rank}, {m['team2']}: #{team2_rank}<br>"
                f"<strong>Average age of players:</strong> {m['team1']}: {team1_avg_age}, {m['team2']}: {team2_avg_age}<br><br>"
                f"<strong>Average players stats:</strong><br>"
                f"{m['team1']}: Rating {agg1['rating']}, Round Swing {agg1['round_swing']}, DPR {agg1['dpr']}, "
                f"KAST {agg1['kast']}, Multi-Kill {agg1['multi_kill']}, ADR {agg1['adr']}, KPR {agg1['kpr']}<br>"
                f"{m['team2']}: Rating {agg2['rating']}, Round Swing {agg2['round_swing']}, DPR {agg2['dpr']}, "
                f"KAST {agg2['kast']}, Multi-Kill {agg2['multi_kill']}, ADR {agg2['adr']}, KPR {agg2['kpr']}<br><br>"
                f"</div>"
            )
        st.markdown(stats_text, unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)


        def show_players_table(team_name, stats_list):
            if not stats_list:
                st.write(f"No data for {team_name}")
                return
            unique_players = {f"{p.get('hltv_id')}_{p['nickname']}": p for p in stats_list}.values()
            df = pd.DataFrame(unique_players)
            df = df[["nickname", "rating", "round_swing", "dpr", "kast", "multi_kill", "adr", "kpr"]]
            df.index = range(1, len(df) + 1)
            with st.expander(f"Players stats {team_name}"):
                st.dataframe(df)

        show_players_table(m['team1'], players_stats_team1)
        show_players_table(m['team2'], players_stats_team2)


        all_maps = sorted(set(list(team1_maps_wr.keys()) + list(team2_maps_wr.keys())))
        table_rows = ""
        for map_name in all_maps:
            wr1 = f"{team1_maps_wr.get(map_name, '-'):.1f}%" if map_name in team1_maps_wr else "-"
            wr2 = f"{team2_maps_wr.get(map_name, '-'):.1f}%" if map_name in team2_maps_wr else "-"
            table_rows += f"<tr><td>{map_name}</td><td>{wr1}</td><td>{wr2}</td></tr>"

        maps_table_html = f"""
        <table style='border-collapse: collapse; width: 100%;'>
        <tr><th>Map</th><th>{m['team1']}</th><th>{m['team2']}</th></tr>
        {table_rows}
        </table>
        """

        with st.expander("Map winrate(%)"):
            st.markdown(maps_table_html, unsafe_allow_html=True)

cur.close()
conn.close()

