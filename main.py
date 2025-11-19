import json
import os
import pickle
import re
import statistics

import numpy as np
import pandas as pd
import streamlit as st
import cloudscraper
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import psycopg2
from streamlit_autorefresh import st_autorefresh

from model import (
    predict_match, team_player_stats, team_match_stats, predict_map
)

MODELS_DIR = "."  # или путь, где лежат .pkl

# загрузка моделей (без ошибки, если файлов нет)
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

# --- Автообновление каждые 5 минут ---
st_autorefresh(interval=300_000, limit=None, key="refresh")

# ----------------- DB Params -----------------
DB_PARAMS = {
    "dbname": "railway",  # имя базы в облаке
    "user": "postgres",   # пользователь
    "password": "nrrWMrzYFdeaBdvhqNuJDUbKHTIbfOiw",  # пароль из Railway
    "host": "interchange.proxy.rlwy.net",  # хост из public URL
    "port": 25251  # порт из public URL
}

# ----------------- Получение матчей -----------------
def get_matches():

    scraper = cloudscraper.create_scraper()
    matches = []
    seen = set()
    today = datetime.now()
    urls = [("https://www.hltv.org/matches", True)] + [
        (f"https://www.hltv.org/matches?selectedDate={(today+timedelta(days=d)).strftime('%Y-%m-%d')}", False)
        for d in range(1, 7)
    ]

    map_list = ["dust2","inferno","nuke","mirage","ancient","train","overpass","vertigo"]

    for url, include_live in urls:
        resp = scraper.get(url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # --- Live матчи ---
        if include_live:
            for mw in soup.select(".liveMatches .match-wrapper"):
                tournament_el = mw.select_one(".match-event")
                tournament = tournament_el["data-event-headline"] if tournament_el and tournament_el.has_attr("data-event-headline") else "Unknown"
                teams = mw.select(".match-teamname")
                if len(teams) != 2: continue
                team1, team2 = [t.get_text(strip=True) for t in teams]
                link_el = mw.select_one("a[href*='/matches/']")
                match_link = "https://www.hltv.org" + link_el['href'] if link_el else None
                key = f"{team1}_{team2}_{tournament}"
                if key in seen: continue
                seen.add(key)

                # --- Дата и время ---
                time_el = mw.select_one(".match-time")
                if time_el and time_el.has_attr("data-unix"):
                    timestamp = int(time_el["data-unix"]) / 1000
                    match_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
                else:
                    match_time = None

                maps = []
                upcoming_maps = []

                try:
                    if match_link:
                        match_resp = scraper.get(match_link, timeout=15)
                        match_resp.raise_for_status()
                        match_soup = BeautifulSoup(match_resp.text, "html.parser")

                        for mholder in match_soup.select(".mapholder"):
                            map_name_el = mholder.select_one(".mapname")
                            if not map_name_el:
                                continue
                            map_name = map_name_el.get_text(strip=True)
                            results = mholder.select(".results-left .results-team-score, .results-right .results-team-score")
                            if results:
                                scores = [r.get_text(strip=True) for r in results]
                                if all(s == "-" for s in scores):
                                    status = "upcoming"
                                    upcoming_maps.append(map_name)
                                else:
                                    status = "played"
                            else:
                                status = "upcoming"
                                upcoming_maps.append(map_name)

                            maps.append({"map": map_name, "status": status})
                except:
                    maps = []
                    upcoming_maps = []

                matches.append({
                    "tournament": tournament,
                    "team1": team1,
                    "team2": team2,
                    "format": "Live",
                    "status": "live",
                    "match_link": match_link,
                    "maps": maps,
                    "upcoming_maps": upcoming_maps,
                    "match_time": match_time
                })

        # --- Upcoming матчи ---
        for ew in soup.select(".matches-event-wrapper"):
            tournament_el = ew.select_one(".event-headline-text")
            tournament = tournament_el.get_text(strip=True) if tournament_el else "Unknown"
            for mw in ew.select(".match-wrapper"):
                teams = mw.select(".match-teamname")
                if len(teams) != 2: continue
                team1, team2 = [t.get_text(strip=True) for t in teams]
                link_el = mw.select_one("a[href*='/matches/']")
                match_link = "https://www.hltv.org" + link_el['href'] if link_el else None
                key = f"{team1}_{team2}_{tournament}"
                if key in seen: continue
                seen.add(key)

                # --- Дата и время ---
                time_el = mw.select_one(".match-time")
                if time_el and time_el.has_attr("data-unix"):
                    timestamp = int(time_el["data-unix"]) / 1000
                    match_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
                else:
                    match_time = None

                maps = []
                upcoming_maps = []

                try:
                    if match_link:
                        match_resp = scraper.get(match_link, timeout=15)
                        match_resp.raise_for_status()
                        match_soup = BeautifulSoup(match_resp.text, "html.parser")

                        veto_maps = []
                        for vd in match_soup.select(".veto-box .padding"):
                            for line in vd.stripped_strings:
                                line_lower = line.lower()
                                for map_name in map_list:
                                    if map_name in line_lower:
                                        action = "picked" if "picked" in line_lower else ("removed" if "removed" in line_lower else "left over")
                                        team = line.split(" ")[0] if action != "left over" else None
                                        if action != "removed":
                                            veto_maps.append({"map": map_name.capitalize(), "action": action, "team": team})

                        # Убираем дубликаты по карте
                        seen_maps = set()
                        maps_cleaned = []
                        for m in veto_maps:
                            if m["map"] not in seen_maps:
                                seen_maps.add(m["map"])
                                maps_cleaned.append(m)
                        maps = maps_cleaned
                        upcoming_maps = [m["map"] for m in maps if m["action"] != "removed"]
                except:
                    maps = []
                    upcoming_maps = []

                matches.append({
                    "tournament": tournament,
                    "team1": team1,
                    "team2": team2,
                    "format": "Upcoming",
                    "status": "upcoming",
                    "match_link": match_link,
                    "maps": maps,
                    "upcoming_maps": upcoming_maps,
                    "match_time": match_time
                })

    return matches

# --- Загружаем матчи с HLTV ---
CACHE_FILE = "matches_cache.json"
CACHE_EXPIRY_HOURS = 1

def get_matches_cached(force_reload=False):
    # проверка session_state
    if "matches" in st.session_state and not force_reload:
        return st.session_state.matches

    # проверка кэша на диске
    if not force_reload and os.path.exists(CACHE_FILE):
        cache_mtime = datetime.fromtimestamp(os.path.getmtime(CACHE_FILE))
        if datetime.now() - cache_mtime < timedelta(hours=CACHE_EXPIRY_HOURS):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                st.session_state.matches = json.load(f)
                print("Loaded matches from cache")
                return st.session_state.matches

    # если кэша нет или force_reload=True
    matches = get_matches()  # твоя функция получения матчей
    st.session_state.matches = matches

    # сохраняем в кэш
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(matches, f, ensure_ascii=False, indent=2)

    return matches

# --- Streamlit интерфейс ---
st.title("CS2 Matches Predictor")
st.markdown(
    """
    <style>
    /* Основной контейнер: ограничение ширины страницы */
    .main .block-container {
        max-width: 1000px;
    }

    /* Все текстовые элементы кроме заголовков */
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

    /* Строки матчей: не переносить текст, отступ для времени */
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


# --- Инициализация состояния ---
if "status_filter" not in st.session_state:
    st.session_state.status_filter = "live"
if "refresh_flag" not in st.session_state:
    st.session_state.refresh_flag = 0

# --- Кнопка обновления ---
if st.button("Reload matches"):
    st.session_state.refresh_flag += 1
    matches = get_matches_cached(force_reload=True)
else:
    # Если данных ещё нет — показываем спиннер
    if "matches" not in st.session_state:
        with st.spinner("Loading matches, wait..."):
            matches = get_matches_cached()
    else:
        matches = get_matches_cached()


# --- Фильтр статуса матчей ---
st.session_state.status_filter = st.selectbox(
    "Choose match status",
    ["live", "upcoming"],
    index=0 if st.session_state.status_filter == "live" else 1
)

filtered_matches = [m for m in matches if m["status"] == st.session_state.status_filter]

st.write(f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
st.write(f"Matches found: {len(filtered_matches)}")

# --- Поиск по команде ---
search_team = st.text_input("Search by team name (enter the name):").strip().lower()
if search_team:
    filtered_matches = [
        m for m in filtered_matches
        if search_team in m['team1'].lower() or search_team in m['team2'].lower()
    ]
    st.write(f"Founded matches after filter: {len(filtered_matches)}")

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

    # --- суммарная разница карт (названия должны совпадать с обучением) ---
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

# --- Подключение к БД ---
conn = psycopg2.connect(**DB_PARAMS)
cur = conn.cursor()

# --- Вывод матчей и прогнозов ---
for i, m in enumerate(filtered_matches):
    # Текст ссылки
    link_text = f"[{m['status'].capitalize()}] {m['tournament']}: {m['team1']} vs {m['team2']}"
    match_link = m.get("match_link", "#")

    # Дата/время
    match_time = f"{m['match_time']}" if m.get("match_time") else ""

    # HTML с разделением ссылки и времени
    html = f'<div class="match-row"><a href="{match_link}" target="_blank">{link_text}</a> <span>{match_time}</span></div>'
    st.markdown(html, unsafe_allow_html=True)

    # --- Ключи для session_state ---
    forecast_key = f"forecast_type_{i}"
    map_key = f"map_{i}"

    if forecast_key not in st.session_state:
        st.session_state[forecast_key] = "Match prediction"
    if map_key not in st.session_state:
        st.session_state[map_key] = None

    # --- Тип прогноза ---
    forecast_type = st.radio(
        "Choose prediction type",
        ["Match prediction", "Map prediction"],
        key=forecast_key
    )

    # --- Выбор карты ---
    selected_map = None
    if forecast_type == "Map prediction" and m.get("upcoming_maps"):
        selected_map = st.selectbox(
            f"Choose map: {m['team1']} vs {m['team2']}",
            m["upcoming_maps"],
            key=map_key
        )
    elif forecast_type == "Map prediction":
        st.info("Maps for this match have not been determined")

    # --- Кнопка прогноза ---
    if st.button(f"Make prediction for {m['team1']} vs {m['team2']}", key=f"btn_{i}"):

        # --- Проверка карты для прогноза на карту ---
        if forecast_type == "Map prediction":
            if not selected_map:
                st.warning("Please, choose the map for the prediction or choose the Match prediction")
                continue  # прекращаем выполнение, не грузим данные

        # --- Получаем игроков и инфу о командах ---
        players_stats_team1 = team_player_stats(m['team1'], m['match_link'], cur)
        players_stats_team2 = team_player_stats(m['team2'], m['match_link'], cur)
        team1_info = team_match_stats(m['team1'], m['match_link'], cur)
        team2_info = team_match_stats(m['team2'], m['match_link'], cur)

        match_id = None
        match_link = m.get('match_link')
        if match_link:
            mobj = re.search(r"/matches/(\d+)", match_link)
            if mobj:
                match_id = int(mobj.group(1))

        # --- Прогноз ---
        maps_list = m.get("upcoming_maps") or None

        if forecast_type == "Map prediction":
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
                match_id=match_id,  # <- передаем числовой ID
                conn=conn,
                cur=cur
            )

            st.info(f"Map prediction {selected_map}: {m['team1']} {prob1}%, {m['team2']} {prob2}%")
        else:
            prob1, prob2, stats_info = predict_match(
                team1=m['team1'],
                team2=m['team2'],
                match_id=match_id,  # <- числовой ID
                cur=cur,
                conn=conn,
                players_stats_team1=players_stats_team1,
                players_stats_team2=players_stats_team2,
                team1_info=team1_info,
                team2_info=team2_info,
                maps_list=maps_list
            )

            st.info(f"Match prediction: {m['team1']} {prob1}%, {m['team2']} {prob2}%")
            # после твоего st.info с прогнозом твоей модели
            # после st.info с прогнозом твоей модели
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
                        st.success(f"ML-model **{selected_model_name}**: prediction → **{winner}** ({prob_pct:.2f}%)")


        # --- Агрегированные показатели команды ---
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

        # --- Вывод статистики ---
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

        # --- Expandable таблицы игроков ---
        def show_players_table(team_name, stats_list):
            if not stats_list:
                st.write(f"No data for {team_name}")
                return
            unique_players = {p['hltv_id']: p for p in stats_list}.values()
            df = pd.DataFrame(unique_players)
            df = df[["nickname", "rating", "round_swing", "dpr", "kast", "multi_kill", "adr", "kpr"]]
            df.index = range(1, len(df) + 1)
            with st.expander(f"Players stats {team_name}"):
                st.dataframe(df)

        show_players_table(m['team1'], players_stats_team1)
        show_players_table(m['team2'], players_stats_team2)

        # --- Expandable таблица винрейтов по картам HLTV ---
        all_maps = sorted(set(list(team1_maps_wr.keys()) + list(team2_maps_wr.keys())))
        table_rows = ""
        for map_name in all_maps:
            wr1 = f"{team1_maps_wr.get(map_name, '-'):.1f}%" if map_name in team1_maps_wr else "-"
            wr2 = f"{team2_maps_wr.get(map_name, '-'):.1f}%" if map_name in team2_maps_wr else "-"
            table_rows += f"<tr><td>{map_name}</td><td>{wr1}</td><td>{wr2}</td></tr>"

        maps_table_html = f"""
        <table style='border-collapse: collapse; width: 100%;'>
        <tr><th>Карта</th><th>{m['team1']}</th><th>{m['team2']}</th></tr>
        {table_rows}
        </table>
        """

        with st.expander("Map winrate(%)"):
            st.markdown(maps_table_html, unsafe_allow_html=True)

cur.close()
conn.close()

