import json
import math
import re
from datetime import datetime, timedelta
from decimal import Decimal
import cloudscraper
import unicodedata
from bs4 import BeautifulSoup
from unidecode import unidecode

# ---------- Основные функции ----------
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
    """
    Возвращает список последних N матчей команды (1=победа, 0=поражение),
    первый элемент — самый последний сыгранный матч.
    """
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

    # matches уже выбраны с конца, просто оставляем в таком порядке
    return form_list  # первый элемент — самый последний матч

def team_form_string(team_name, cur, last_n=5):
    """
    HTML-строка формы последних N матчей: W/L,
    первый символ — самый последний матч.
    """
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
    """Возвращает количество побед обеих команд и список матчей между ними со счётом и турниром"""
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


def team_player_stats(team_name, match_link, cur):
    players_stats = []

    # --- Получаем hltv_id команды и дату последнего обновления ---
    cur.execute("SELECT hltv_id, last_update FROM teams WHERE name=%s", (team_name,))
    res = cur.fetchone()
    if not res:
        print(f"[ERROR] Team {team_name} not found in DB")
        return []
    team_id, last_update = res

    # --- Проверяем дату обновления ---
    use_db_only = False
    if last_update:
        if last_update.tzinfo:
            last_update = last_update.replace(tzinfo=None)
        delta = (datetime.now() - last_update).total_seconds()
        if delta < 86400:  # <24 часа
            use_db_only = True

    if use_db_only:
        # --- Берём всех игроков с team_id = hltv_id ---
        cur.execute("""
            SELECT nickname, rating, round_swing, dpr, kast, multi_kill, adr, kpr
            FROM players_stats
            WHERE team_id=%s
        """, (team_id,))
        for row in cur.fetchall():
            players_stats.append({
                "hltv_id": None,
                "nickname": row[0],
                "rating": float(row[1]) if row[1] is not None else 0.0,
                "round_swing": float(row[2]) if row[2] is not None else 0.0,
                "dpr": float(row[3]) if row[3] is not None else 0.0,
                "kast": float(row[4]) if row[4] is not None else 0.0,
                "multi_kill": float(row[5]) if row[5] is not None else 0.0,
                "adr": float(row[6]) if row[6] is not None else 0.0,
                "kpr": float(row[7]) if row[7] is not None else 0.0,
            })
        return players_stats

    # --- Если данные старые или нет кэша, можно оставить парсинг для локалки ---
    try:
        scraper = cloudscraper.create_scraper()
        html = scraper.get(match_link, timeout=20).text
        soup = BeautifulSoup(html, "html.parser")
        lineup_divs = soup.select(".lineup")
    except Exception as e:
        print(f"[WARN] Failed to retrieve match page {match_link}: {e}")
        return []

    current_player_ids = []

    for team_div in lineup_divs:
        team_name_el = team_div.select_one(".box-headline a.text-ellipsis")
        if not team_name_el:
            continue
        current_team_name = team_name_el.get_text(strip=True)
        if current_team_name != team_name:
            continue

        for player_div in team_div.select(".player-compare")[:5]:
            player_id = player_div.get("data-player-id")
            if not player_id:
                continue
            current_player_ids.append(int(player_id))

            # --- Никнейм ---
            img = player_div.select_one("img.player-photo")
            if img and img.has_attr("alt"):
                alt_text = img["alt"]
                nickname = alt_text.split("'")[1].strip() if "'" in alt_text else alt_text.strip()
            else:
                nickname = f"Player_{player_id}"
            nickname_clean = clean_text(nickname)

            # --- Берём данные из кэша ---
            cur.execute("""
                SELECT rating, round_swing, dpr, kast, multi_kill, adr, kpr
                FROM players_stats WHERE hltv_id=%s
            """, (int(player_id),))
            row = cur.fetchone()
            if row:
                players_stats.append({
                    "nickname": nickname_clean,
                    "rating": float(row[0]) if row[0] is not None else 0.0,
                    "round_swing": float(row[1]) if row[1] is not None else 0.0,
                    "dpr": float(row[2]) if row[2] is not None else 0.0,
                    "kast": float(row[3]) if row[3] is not None else 0.0,
                    "multi_kill": float(row[4]) if row[4] is not None else 0.0,
                    "adr": float(row[5]) if row[5] is not None else 0.0,
                    "kpr": float(row[6]) if row[6] is not None else 0.0,
                })

    return players_stats

def team_match_stats(team_name, match_link, cur):
    scraper = cloudscraper.create_scraper()

    # --- Очистка названия перед SQL ---
    team_name = clean_text(team_name)

    # --- Проверка кэша ---
    cur.execute("""
        SELECT hltv_id, name, world_rank, avg_age, maps_wr, lineup_wr, last_update
        FROM teams WHERE name=%s
    """, (team_name,))
    row = cur.fetchone()

    if row and row[6]:
        last_update = row[6]
        if last_update.tzinfo:
            last_update = last_update.replace(tzinfo=None)

        maps_wr_value = row[4]
        if isinstance(maps_wr_value, str):
            try:
                maps_wr_value = json.loads(maps_wr_value)
            except:
                maps_wr_value = {}

        lineup_wr_value = row[5]

        if (datetime.now() - last_update).total_seconds() < 86400 \
                and row[2] is not None \
                and lineup_wr_value is not None:
            return {
                "hltv_id": row[0],
                "name": row[1],
                "world_rank": row[2],
                "avg_age": row[3],
                "maps_wr": maps_wr_value or {},
                "lineup_wr": lineup_wr_value,
                "last_update": row[6]
            }

    # --- Получаем страницу матча ---
    try:
        html = scraper.get(match_link, timeout=20).text
    except:
        return None

    soup = BeautifulSoup(html, "html.parser")

    lineup_divs = soup.select(".lineup")
    maps_wr_team1, maps_wr_team2 = {}, {}

    # --- Maps winrates ---
    for box in soup.select('[map-stats-infobox="wins"] .map-stats-infobox-maps'):
        map_name = box.get("data-mapname", "").lower()
        if not map_name:
            continue

        try:
            left_wr = float(box.select_one(
                ".map-stats-infobox-stats:not(.team2) .map-stats-infobox-winpercentage a"
            ).text.strip().replace("%", ""))
        except:
            left_wr = None

        try:
            right_wr = float(box.select_one(
                ".map-stats-infobox-stats.team2 .map-stats-infobox-winpercentage a"
            ).text.strip().replace("%", ""))
        except:
            right_wr = None

        if left_wr is not None: maps_wr_team1[map_name] = left_wr
        if right_wr is not None: maps_wr_team2[map_name] = right_wr

    def normalize(s):
        return "".join(c for c in s.lower() if c.isalnum())

    team_info = None

    # --- Ищем команду ---
    for idx, team_div in enumerate(lineup_divs):
        el = team_div.select_one(".box-headline a.text-ellipsis")
        if not el:
            continue

        current_name = clean_text(el.text.strip())

        if normalize(current_name).find(normalize(team_name)) == -1 and \
           normalize(team_name).find(normalize(current_name)) == -1:
            continue

        is_left = (idx == 0)

        team_link = el.get("href", "")
        hltv_id = int(team_link.split("/team/")[1].split("/")[0]) if "/team/" in team_link else None
        if not hltv_id:
            return None

        # --- Профиль ---
        try:
            profile_html = scraper.get(
                f"https://www.hltv.org/team/{hltv_id}/{normalize(team_name)}",
                timeout=20
            ).text
        except:
            return None

        soup_profile = BeautifulSoup(profile_html, "html.parser")

        name_el = soup_profile.select_one(".profile-team-name")
        name = clean_text(name_el.text.strip()) if name_el else team_name

        try:
            rank_el = soup_profile.select_one(".profile-team-stat b:contains('World ranking') ~ .right a")
            world_rank = int(rank_el.text.strip().lstrip("#")) if rank_el else None
        except:
            world_rank = None

        try:
            age_el = soup_profile.select_one(".profile-team-stat b:contains('Average player age') ~ .right")
            avg_age = float(age_el.text.strip()) if age_el else None
        except:
            avg_age = None

        maps_wr = maps_wr_team1 if is_left else maps_wr_team2

        # --- Lineup WR ---
        player_ids = [
            p.get("data-player-id")
            for p in team_div.select(".player-compare")[:5]
            if p.get("data-player-id")
        ]

        lineup_wr = None

        if player_ids:
            ids_query = "&".join(f"lineup={pid}" for pid in player_ids)
            lineup_url = f"https://www.hltv.org/stats/lineup?csVersion=CS2&{ids_query}&minLineupMatch=5"

            try:
                lineup_html = scraper.get(lineup_url, timeout=20).text
                lineup_soup = BeautifulSoup(lineup_html, "html.parser")

                stats_block = None
                for col in lineup_soup.select(".col.standard-box.big-padding"):
                    label_el = col.select_one(".small-label-below")
                    if label_el and "Wins / draws / losses" in label_el.text:
                        stats_block = col.select_one(".large-strong")
                        break

                if stats_block:
                    try:
                        wins, _, losses = [int(x.strip()) for x in stats_block.text.strip().split("/")]
                        total = wins + losses
                        lineup_wr = round(wins / total * 100, 2) if total > 0 else 0.0
                    except:
                        pass
            except:
                pass

        # --- Запись в БД (чистый текст) ---
        try:
            cur.execute("""
                INSERT INTO teams (hltv_id, name, world_rank, avg_age, maps_wr, lineup_wr, last_update)
                VALUES (%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (hltv_id) DO UPDATE
                SET name=EXCLUDED.name,
                    world_rank=EXCLUDED.world_rank,
                    avg_age=EXCLUDED.avg_age,
                    maps_wr=EXCLUDED.maps_wr,
                    lineup_wr=EXCLUDED.lineup_wr,
                    last_update=NOW()
            """, (hltv_id, name, world_rank, avg_age, json.dumps(maps_wr), lineup_wr))
            cur.connection.commit()
        except:
            cur.connection.rollback()

        team_info = {
            "hltv_id": hltv_id,
            "name": name,
            "world_rank": world_rank,
            "avg_age": avg_age,
            "maps_wr": maps_wr,
            "lineup_wr": lineup_wr,
            "last_update": datetime.now()
        }

        break

    return team_info

# ---------- Ансамблевая модель с игроками ----------

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
        # более агрессивные веса для первых двух игроков
        player_weights = [0.5, 0.3, 0.1, 0.07, 0.03]

        def weighted_avg(key, default=0.0):
            return sum(p.get(key, default) * w for p, w in zip(sorted_players, player_weights))

        metrics = {
            "rating": weighted_avg("rating") * 60,
            "round_swing": weighted_avg("round_swing") * 50,  # масштабируем вручную
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

        gap = rank2 - rank1  # положительный, если team1 выше
        # базовый фактор для небольшой разницы
        base1 = 120 / math.log(rank1 + 1)
        base2 = 120 / math.log(rank2 + 1)

        # усиливаем влияние при гэпе
        bonus1, bonus2 = base1, base2
        if gap > 0:
            # чем больше разница, тем сильнее бонус сильной команды
            factor = 1 + (gap / 10) ** 1.5  # например, при гэпе 25 → +50% к базовому рангу
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

    # --- Улучшенный прогноз для конкретной карты ---
    # Берём уже имеющиеся метрики игроков, винрейт карты, винрейт команды и ранги
    score1 = sum(metrics1[k] * weights.get(k, 0) for k in metrics1) + \
             map_wr1 * weights["map_wr"] + team_wr1 * weights["team_wr"] + \
             f_rank1 * weights["rank"] + age_factor(team1_avg_age) * weights["avg_age"]

    score2 = sum(metrics2[k] * weights.get(k, 0) for k in metrics2) + \
             map_wr2 * weights["map_wr"] + team_wr2 * weights["team_wr"] + \
             f_rank2 * weights["rank"] + age_factor(team2_avg_age) * weights["avg_age"]

    # --- Усиление влияния ключевых факторов для конкретной карты ---
    # Можно слегка увеличить вес map_wr и rating, чтобы сильные карты и игроки давали более явный сигнал
    score1 += map_wr1 * 0.1  # добавочный бонус для карты
    score2 += map_wr2 * 0.1

    score1 += metrics1["rating"] * 0.05  # усиление лидеров
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

    # --- Функции для формы и H2H ---
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

    # --- Вычисляем формулу формы и H2H ---
    form_diff = compute_form(team1) - compute_form(team2)
    form_factor = max(-3, min(3, form_diff * 0.6))

    h2h_diff = compute_h2h(team1, team2)
    h2h_factor = max(-3, min(3, h2h_diff * 0.3))

    print(f"[DEBUG] form_factor={form_factor}, h2h_factor={h2h_factor}")

    # ---- Прогноз по картам или без них ----
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

    # --- Усреднение по картам с учётом винрейта карты ---
    total_prob1, total_prob2 = 0.0, 0.0
    total_weight = 0.0

    for map_name in maps_list:
        map_wr1 = team1_info.get("maps_wr", {}).get(map_name.lower(), 50) if team1_info else 50
        map_wr2 = team2_info.get("maps_wr", {}).get(map_name.lower(), 50) if team2_info else 50
        weight = (map_wr1 + map_wr2) / 100  # вес карты по винрейту команд

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
