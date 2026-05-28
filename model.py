from dateutil.relativedelta import relativedelta

from parser_worker import parser

import math
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal
import cloudscraper
import unicodedata
from bs4 import BeautifulSoup
from unidecode import unidecode

import json

def head_to_head(team1, team2, cur):
    cur.execute("""
        SELECT score1, score2, team1, team2
        FROM matches
        WHERE (team1=%s AND team2=%s) OR (team1=%s AND team2=%s)
    """, (team1, team2, team2, team1))
    matches = cur.fetchall()
    if not matches:
        return 0.0
    wins_team1 = 0
    for s1, s2, t1, t2 in matches:
        if (t1 == team1 and s1 > s2) or (t2 == team1 and s2 > s1):
            wins_team1 += 1
    return round(wins_team1 / len(matches) * 100, 2)


def team_form(team_name, cur, last_n=5):
    cur.execute("""
        SELECT score1, score2, team1, team2
        FROM matches
        WHERE team1=%s OR team2=%s
        ORDER BY id DESC
        LIMIT %s
    """, (team_name, team_name, last_n))
    matches = cur.fetchall()
    if not matches:
        return []

    form_list = []
    for s1, s2, t1, t2 in matches:
        if (team_name == t1 and s1 > s2) or (team_name == t2 and s2 > s1):
            form_list.append(1)
        else:
            form_list.append(0)

    return form_list

def team_form_string(team_name, cur, last_n=5):

    form_list = team_form(team_name, cur, last_n)
    if not form_list:
        return ""

    form_str = ""
    for result in form_list:
        if result == 1:
            form_str += "W"
        else:
            form_str += "<span style='color:red;'>L</span>"
    return form_str

def head_to_head_scores(team1, team2, cur):
    cur.execute("""
        SELECT score1, score2, team1, team2, tournament
        FROM matches
        WHERE (team1=%s AND team2=%s) OR (team1=%s AND team2=%s)
        ORDER BY date DESC
    """, (team1, team2, team2, team1))
    matches = cur.fetchall()
    if not matches:
        return [f"{team1} - 0 wins, {team2} - 0 wins"]

    wins_team1 = 0
    wins_team2 = 0
    results = []

    for s1, s2, t1, t2, tournament in matches:
        if (t1 == team1 and s1 > s2) or (t2 == team1 and s2 > s1):
            wins_team1 += 1
        elif (t1 == team2 and s1 > s2) or (t2 == team2 and s2 > s1):
            wins_team2 += 1
        results.append(f"{t1} {s1}-{s2} {t2} | {tournament}")

    summary = f"{team1} - {wins_team1} wins, {team2} - {wins_team2} wins"
    return [summary, ""] + results


def clean_text(text: str) -> str:
    if not text:
        return text
    text = unicodedata.normalize('NFKC', text.strip())
    text = unidecode(text)
    text = re.sub(r'\s+', ' ', text)
    return text


def fetch_and_save_player_stats(p_id_int, nickname_clean, team_id, cur, conn):
    now = datetime.now()
    end_date = now.strftime("%Y-%m-%d")

    start_date = (now - relativedelta(months=3)).strftime("%Y-%m-%d")

    url = f"https://www.hltv.org/stats/players/{p_id_int}/{nickname_clean.lower()}?startDate={start_date}&endDate={end_date}"
    print(f"[INFO] Ссылка для парсинга игрока: {url}")

    time.sleep(2)
    html = parser.get_page(url)
    if not html: return None

    soup_stats = BeautifulSoup(html, "html.parser")
    stats_dict = {"rating": 0.0, "round_swing": 0.0, "dpr": 0.0, "kast": 0.0, "multi_kill": 0.0, "adr": 0.0, "kpr": 0.0}

    try:
        # Рейтинг
        rating_el = soup_stats.select_one(".player-summary-stat-box-rating-data-text")
        if rating_el and rating_el.string:
            stats_dict["rating"] = float(Decimal(rating_el.string.strip()))

        # Round Swing
        for box in soup_stats.select(".player-summary-stat-box-right-bottom .player-summary-stat-box-data-wrapper"):
            label_el = box.select_one(".player-summary-stat-box-data-text")
            value_el = box.select_one(".player-summary-stat-box-data")
            if label_el and value_el and "round swing" in label_el.get_text(strip=True).lower():
                value_text = "".join(c for c in value_el.get_text(strip=True) if c in "0123456789.-")
                stats_dict["round_swing"] = float(Decimal(value_text)) if value_text else 0.0
                break

        # DPR, KAST, ADR, KPR, Multi-kill
        for metric_div in soup_stats.select(".player-summary-stat-box-data.traditionalData"):
            parent_label = metric_div.find_next_sibling(class_="player-summary-stat-box-data-text")
            if not parent_label: continue
            label = parent_label.get_text(strip=True).lower()
            value_text = "".join(c for c in metric_div.get_text(strip=True) if c in "0123456789.")
            try:
                value = float(value_text)
            except:
                continue

            if "dpr" in label:
                stats_dict["dpr"] = value
            elif "kast" in label:
                stats_dict["kast"] = value
            elif "multi-kill" in label:
                stats_dict["multi_kill"] = value
            elif "adr" in label:
                stats_dict["adr"] = value
            elif "kpr" in label:
                stats_dict["kpr"] = value

        # Запись в БД с ТЕАM_ID
        cur.execute("""
            INSERT INTO players_stats 
            (hltv_id, nickname, rating, round_swing, dpr, kast, multi_kill, adr, kpr, team_id, last_update)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (hltv_id) DO UPDATE SET
                nickname = EXCLUDED.nickname,
                rating = EXCLUDED.rating,
                round_swing = EXCLUDED.round_swing,
                dpr = EXCLUDED.dpr,
                kast = EXCLUDED.kast,
                multi_kill = EXCLUDED.multi_kill,
                adr = EXCLUDED.adr,
                kpr = EXCLUDED.kpr,
                team_id = EXCLUDED.team_id,
                last_update = NOW();
        """, (
            p_id_int, nickname_clean,
            stats_dict["rating"], stats_dict["round_swing"],
            stats_dict["dpr"], stats_dict["kast"],
            stats_dict["multi_kill"], stats_dict["adr"],
            stats_dict["kpr"], team_id
        ))
        conn.commit()
        return stats_dict
    except Exception as e:
        print(f"[ERROR] Ошибка записи стат игрока: {e}")
        conn.rollback()
        return None


def team_player_stats(team_name, html, team_id, cur, conn):
    players_stats = []
    soup = BeautifulSoup(html, "html.parser")

    for team_div in soup.select(".lineup"):
        team_name_el = team_div.select_one(".box-headline a.text-ellipsis")
        if not team_name_el or team_name_el.get_text(strip=True) != team_name:
            continue

        for player_div in team_div.select(".player-compare")[:5]:
            p_id = player_div.get("data-player-id")
            if not p_id: continue
            p_id_int = int(p_id)

            img = player_div.select_one("img.player-photo")
            nick = img["alt"].split("'")[1].strip() if img and "'" in img["alt"] else (
                img["alt"].strip() if img else f"P_{p_id}")
            nick_clean = clean_text(nick)

            cur.execute(
                "SELECT rating, round_swing, dpr, kast, multi_kill, adr, kpr, last_update FROM players_stats WHERE hltv_id=%s",
                (p_id_int,))
            row = cur.fetchone()

            if row and row[7] and (datetime.now() - row[7].replace(tzinfo=None)).total_seconds() < 86400:
                stats = {
                    "hltv_id": p_id_int, "nickname": nick_clean,
                    "rating": float(row[0]), "round_swing": float(row[1]),
                    "dpr": float(row[2]), "kast": float(row[3]),
                    "multi_kill": float(row[4]), "adr": float(row[5]), "kpr": float(row[6])
                }
            else:
                stats = fetch_and_save_player_stats(p_id_int, nick_clean, team_id, cur, conn)
                if not stats:
                    stats = {"hltv_id": p_id_int, "nickname": nick_clean, "rating": 0.0, "round_swing": 0.0, "dpr": 0.0,
                             "kast": 0.0, "multi_kill": 0.0, "adr": 0.0, "kpr": 0.0}
                else:
                    stats["hltv_id"] = p_id_int
                    stats["nickname"] = nick_clean

            players_stats.append(stats)

    return players_stats


def team_match_stats(team_name, html, cur, conn):
    team_name = clean_text(team_name)

    # Проверка кэша
    cur.execute("SELECT hltv_id, name, world_rank, avg_age, maps_wr, lineup_wr, last_update FROM teams WHERE name=%s", (team_name,))
    row = cur.fetchone()
    if row and row[6]:
        last_update = row[6].replace(tzinfo=None) if row[6].tzinfo else row[6]
        if (datetime.now() - last_update).total_seconds() < 86400:
            return {"hltv_id": row[0], "name": row[1], "world_rank": row[2], "avg_age": row[3], "maps_wr": row[4], "lineup_wr": row[5]}

    soup = BeautifulSoup(html, "html.parser")
    lineup_divs = soup.select(".lineup")
    maps_wr_team1, maps_wr_team2 = {}, {}

    # Парсинг винрейта карт
    for box in soup.select('[map-stats-infobox="wins"] .map-stats-infobox-maps'):
        map_name = box.get("data-mapname", "").lower()
        if not map_name: continue
        try:
            wr_left = float(box.select_one(".map-stats-infobox-stats:not(.team2) .map-stats-infobox-winpercentage a").text.strip().replace("%", ""))
            wr_right = float(box.select_one(".map-stats-infobox-stats.team2 .map-stats-infobox-winpercentage a").text.strip().replace("%", ""))
            maps_wr_team1[map_name], maps_wr_team2[map_name] = wr_left, wr_right
        except:
            continue

    def normalize(s):
        return "".join(c for c in s.lower() if c.isalnum())

    for idx, team_div in enumerate(lineup_divs):
        el = team_div.select_one(".box-headline a.text-ellipsis")
        if not el or (normalize(el.text).find(normalize(team_name)) == -1 and normalize(team_name).find(normalize(el.text)) == -1):
            continue

        hltv_id = int(el.get("href", "").split("/team/")[1].split("/")[0])

        # Получение профиля команды
        profile_html = parser.get_page(f"https://www.hltv.org/team/{hltv_id}/{normalize(team_name)}")
        soup_p = BeautifulSoup(profile_html, "html.parser")

        world_rank = int(soup_p.select_one(".profile-team-stat b:contains('World ranking') ~ .right a").text.lstrip("#")) if soup_p.select_one(".profile-team-stat") else None
        avg_age = float(soup_p.select_one(".profile-team-stat b:contains('Average player age') ~ .right").text) if soup_p.select_one(".profile-team-stat") else None

        player_ids = [p.get("data-player-id") for p in team_div.select(".player-compare")[:5]]
        lineup_wr = 0.0

        if player_ids:
            lineup_url = f"https://www.hltv.org/stats/lineup?csVersion=CS2&{'&'.join(f'lineup={pid}' for pid in player_ids)}&minLineupMatch=4"
            lineup_html = parser.get_page(lineup_url)

            stats_block = BeautifulSoup(lineup_html, "html.parser").select_one(".col.standard-box.big-padding:contains('Wins / draws / losses') .large-strong")
            if stats_block:
                try:
                    w, _, l = [int(x) for x in stats_block.text.split("/")]
                    lineup_wr = round(w / (w + l) * 100, 2) if (w + l) > 0 else 0.0
                except:
                    lineup_wr = 0.0

        current_maps_wr = maps_wr_team1 if idx == 0 else maps_wr_team2

        cur.execute("""INSERT INTO teams (hltv_id, name, world_rank, avg_age, maps_wr, lineup_wr, last_update)
                               VALUES (%s,%s,%s,%s,%s,%s,NOW())
                               ON CONFLICT (hltv_id) DO UPDATE SET 
                                   world_rank=EXCLUDED.world_rank, 
                                   maps_wr=EXCLUDED.maps_wr, 
                                   lineup_wr=EXCLUDED.lineup_wr, 
                                   last_update=NOW()""",
                    (hltv_id, team_name, world_rank, avg_age, json.dumps(current_maps_wr), lineup_wr))

        conn.commit()

        return {"hltv_id": hltv_id, "name": team_name, "world_rank": world_rank, "avg_age": avg_age,
                "maps_wr": current_maps_wr, "lineup_wr": lineup_wr}

    return None


def fetch_maps(match_link):
    from parser_worker import parser
    html = parser.get_page(match_link)
    if not html: return None

    soup = BeautifulSoup(html, "html.parser")
    map_list = ["dust2", "inferno", "nuke", "mirage", "ancient", "train", "overpass", "vertigo"]

    veto_maps = []

    # Твой селектор, который ты использовал раньше
    veto_elements = soup.select(".veto-box .padding")

    if not veto_elements:
        # Если блок не найден — это штатная ситуация для будущего матча
        print(f"[DEBUG] Не нашел veto-box на странице: {match_link}")
        return {"maps": [], "status": "not_determined"}

    for vd in veto_elements:
        for line in vd.stripped_strings:
            line_lower = line.lower()
            for map_name in map_list:
                if map_name in line_lower:

                    action = "picked" if "picked" in line_lower else (
                        "removed" if "removed" in line_lower else "left over")
                    team = line.split(" ")[0] if action != "left over" else None

                    if action != "removed":
                        veto_maps.append({"map": map_name.capitalize(), "action": action, "team": team})

    return {"maps": veto_maps, "status": "parsed"}

def predict_map(team1, team2, map_name,
                players_stats_team1=None, players_stats_team2=None,
                team1_maps_wr=None, team2_maps_wr=None,
                team1_wr=None, team2_wr=None,
                team1_rank=None, team2_rank=None,
                team1_avg_age=None, team2_avg_age=None, match_id=None, conn=None, cur=None):
    print(f"[DEBUG] predict_map: {team1} vs {team2}, map: {map_name}")
    print(f"[DEBUG] team_wr: {team1_wr} vs {team2_wr}")
    print(f"[DEBUG] team_rank: {team1_rank} vs {team2_rank}, avg_age: {team1_avg_age} vs {team2_avg_age}")

    def weighted_metrics(players_stats):
        if not players_stats:
            return {
                "rating": 1.0, "round_swing": 0.0, "dpr": 1.0,
                "kast": 0.0, "multi_kill": 0.0, "adr": 50.0, "kpr": 0.1,
                "best_rating": 1.0
            }

        sorted_players = sorted(players_stats, key=lambda p: p.get("rating", 1.0), reverse=True)

        player_weights = [0.5, 0.3, 0.1, 0.07, 0.03]

        def weighted_avg(key, default=0.0):
            return sum(p.get(key, default) * w for p, w in zip(sorted_players, player_weights))

        metrics = {
            "rating": weighted_avg("rating") * 60,
            "round_swing": weighted_avg("round_swing") * 50,
            "dpr": weighted_avg("dpr") * 50,
            "kast": weighted_avg("kast"),
            "multi_kill": weighted_avg("multi_kill") * 5,
            "adr": weighted_avg("adr"),
            "kpr": weighted_avg("kpr") * 50,
            "best_rating": sorted_players[0].get("rating", 1.0) * 60
        }
        return metrics

    metrics1 = weighted_metrics(players_stats_team1)
    metrics2 = weighted_metrics(players_stats_team2)

    map_wr1 = team1_maps_wr.get(map_name.lower(), 50) if team1_maps_wr and map_name else 50
    map_wr2 = team2_maps_wr.get(map_name.lower(), 50) if team2_maps_wr and map_name else 50

    team_wr1 = team1_wr if team1_wr is not None else 50
    team_wr2 = team2_wr if team2_wr is not None else 50

    weights = {
        "rating": 0.3,
        "round_swing": 0.2,
        "dpr": 0.03,
        "kast": 0.06,
        "multi_kill": 0.03,
        "adr": 0.03,
        "kpr": 0.03,
        "map_wr": 0.12,
        "team_wr": 0.1,
        "rank": 0.2,
        "avg_age": 0.01
    }

    def rank_factor(rank1, rank2):
        if rank1 is None or rank2 is None:
            return 0, 0

        gap = rank2 - rank1

        base1 = 120 / math.log(rank1 + 1)
        base2 = 120 / math.log(rank2 + 1)


        bonus1, bonus2 = base1, base2
        if gap > 0:

            factor = 1 + (gap / 10) ** 1.5
            if gap > 0:
                bonus1 *= factor
                bonus2 /= factor
            else:
                factor = 1 + (abs(gap) / 10) ** 1.5
                bonus2 *= factor
                bonus1 /= factor

        return bonus1, bonus2

    def age_factor(age):
        if age is None:
            return 80
        return 100 - abs(age - 24.25) * 2

    f_rank1, f_rank2 = rank_factor(team1_rank, team2_rank)

    score1 = sum(metrics1[k] * weights.get(k, 0) for k in metrics1) + \
             map_wr1 * weights["map_wr"] + team_wr1 * weights["team_wr"] + \
             f_rank1 * weights["rank"] + age_factor(team1_avg_age) * weights["avg_age"]

    score2 = sum(metrics2[k] * weights.get(k, 0) for k in metrics2) + \
             map_wr2 * weights["map_wr"] + team_wr2 * weights["team_wr"] + \
             f_rank2 * weights["rank"] + age_factor(team2_avg_age) * weights["avg_age"]

    score1 += map_wr1 * 0.1
    score2 += map_wr2 * 0.1

    score1 += metrics1["rating"] * 0.05
    score2 += metrics2["rating"] * 0.05

    print(f"[DEBUG] scores: {score1} vs {score2}")

    total = score1 + score2
    if total == 0:
        return 50.0, 50.0

    prob1 = round(score1 / total * 100, 2)
    prob2 = round(score2 / total * 100, 2)
    predicted_winner = team1 if prob1 >= prob2 else team2
    if match_id and conn and cur and map_name:
        cur.execute("""
            INSERT INTO map_predictions
            (match_id, map_name, team1, team2, predicted_prob1, predicted_prob2, predicted_winner, prediction_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (match_id, map_name)
            DO UPDATE SET
                team1 = EXCLUDED.team1,
                team2 = EXCLUDED.team2,
                predicted_prob1 = EXCLUDED.predicted_prob1,
                predicted_prob2 = EXCLUDED.predicted_prob2,
                predicted_winner = EXCLUDED.predicted_winner,
                prediction_date = NOW();
        """, (match_id, map_name, team1, team2, prob1, prob2, predicted_winner))
        conn.commit()

    print(f"[DEBUG] probs: {prob1}% vs {prob2}%")
    return prob1, prob2

def predict_match(team1, team2, match_id, cur, conn=None,
                  players_stats_team1=None, players_stats_team2=None,
                  team1_info=None, team2_info=None, maps_list=None):

    print(f"[DEBUG] predict_match: {team1} vs {team2}, maps_list: {maps_list}")

    team_wr1 = team1_info.get("lineup_wr") if team1_info else 0.0
    team_wr2 = team2_info.get("lineup_wr") if team2_info else 0.0

    def compute_form(team):
        fl = team_form(team, cur, last_n=5)
        if fl and isinstance(fl[0], str):
            return sum(1 if x == "W" else -1 for x in fl)
        return sum(fl)

    def compute_h2h(team1, team2):
        data = head_to_head_scores(team1, team2, cur)
        import re
        m = re.search(r"(\d+).+?(\d+)", data[0])
        if m:
            return int(m.group(1)) - int(m.group(2))
        return 0

    form_diff = compute_form(team1) - compute_form(team2)
    form_factor = max(-3, min(3, form_diff * 0.6))

    h2h_diff = compute_h2h(team1, team2)
    h2h_factor = max(-3, min(3, h2h_diff * 0.3))

    print(f"[DEBUG] form_factor={form_factor}, h2h_factor={h2h_factor}")

    def adjust_probs(prob1, prob2):
        prob1 = max(0, min(100, prob1 + 1.5 * form_factor + 2 * h2h_factor))
        prob2 = max(0, min(100, prob2 - 1.5 * form_factor - 2 * h2h_factor))
        return round(prob1, 2), round(prob2, 2)

    if not maps_list:
        print("[DEBUG] No maps, predicting match without maps")
        p1, p2 = predict_map(
            team1, team2, map_name=None,
            players_stats_team1=players_stats_team1,
            players_stats_team2=players_stats_team2,
            team1_wr=team_wr1,
            team2_wr=team_wr2,
            team1_rank=team1_info.get("world_rank") if team1_info else None,
            team2_rank=team2_info.get("world_rank") if team2_info else None,
            team1_avg_age=team1_info.get("avg_age") if team1_info else None,
            team2_avg_age=team2_info.get("avg_age") if team2_info else None
        )

        p1, p2 = adjust_probs(p1, p2)
        predicted_winner = team1 if p1 >= p2 else team2

        if cur and conn and match_id:
            cur.execute("""
                INSERT INTO match_predictions
                (match_id, team1, team2, predicted_prob1, predicted_prob2,
                 predicted_prob1_map_avg, predicted_prob2_map_avg, predicted_winner, prediction_date)
                VALUES (%s, %s, %s, %s, %s, NULL, NULL, %s, NOW())
                ON CONFLICT (match_id)
                DO UPDATE SET
                    team1 = EXCLUDED.team1,
                    team2 = EXCLUDED.team2,
                    predicted_prob1 = EXCLUDED.predicted_prob1,
                    predicted_prob2 = EXCLUDED.predicted_prob2,
                    predicted_winner = EXCLUDED.predicted_winner,
                    prediction_date = NOW();
            """, (match_id, team1, team2, p1, p2, predicted_winner))
            conn.commit()

        return p1, p2, {
            "form1": team_form_string(team1, cur),
            "form2": team_form_string(team2, cur),
            "h2h": head_to_head_scores(team1, team2, cur),
            "team_wr1": team_wr1,
            "team_wr2": team_wr2
        }

    total_prob1, total_prob2 = 0.0, 0.0
    total_weight = 0.0

    for map_name in maps_list:
        map_wr1 = team1_info.get("maps_wr", {}).get(map_name.lower(), 50) if team1_info else 50
        map_wr2 = team2_info.get("maps_wr", {}).get(map_name.lower(), 50) if team2_info else 50
        weight = (map_wr1 + map_wr2) / 100

        p1, p2 = predict_map(
            team1, team2, map_name,
            players_stats_team1=players_stats_team1 or [],
            players_stats_team2=players_stats_team2 or [],
            team1_maps_wr=team1_info.get("maps_wr") if team1_info else None,
            team2_maps_wr=team2_info.get("maps_wr") if team2_info else None,
            team1_wr=team_wr1,
            team2_wr=team_wr2,
            team1_rank=team1_info.get("world_rank") if team1_info else None,
            team2_rank=team2_info.get("world_rank") if team2_info else None,
            team1_avg_age=team1_info.get("avg_age") if team1_info else None,
            team2_avg_age=team2_info.get("avg_age") if team2_info else None
        )

        total_prob1 += p1 * weight
        total_prob2 += p2 * weight
        total_weight += weight

    if total_weight > 0:
        avg_prob1 = total_prob1 / total_weight
        avg_prob2 = total_prob2 / total_weight
    else:
        avg_prob1 = avg_prob2 = 50.0

    avg_prob1, avg_prob2 = adjust_probs(avg_prob1, avg_prob2)
    predicted_winner = team1 if avg_prob1 >= avg_prob2 else team2
    if cur and conn and match_id:
        cur.execute("""
                INSERT INTO match_predictions
                (match_id, team1, team2, predicted_prob1, predicted_prob2,
                 predicted_prob1_map_avg, predicted_prob2_map_avg, predicted_winner, prediction_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (match_id) 
                DO UPDATE SET
                team1 = EXCLUDED.team1,
                team2 = EXCLUDED.team2,
                predicted_prob1 = EXCLUDED.predicted_prob1,
                predicted_prob2 = EXCLUDED.predicted_prob2,
                predicted_prob1_map_avg = EXCLUDED.predicted_prob1_map_avg,
                predicted_prob2_map_avg = EXCLUDED.predicted_prob2_map_avg,
                predicted_winner = EXCLUDED.predicted_winner,
                prediction_date = NOW();
            """, (match_id, team1, team2, avg_prob1, avg_prob2,
                  avg_prob1, avg_prob2, predicted_winner))
        conn.commit()

    return avg_prob1, avg_prob2, {
        "form1": team_form_string(team1, cur),
        "form2": team_form_string(team2, cur),
        "h2h": head_to_head_scores(team1, team2, cur),
        "team_wr1": team_wr1,
        "team_wr2": team_wr2
    }
