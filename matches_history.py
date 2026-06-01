import cloudscraper
from bs4 import BeautifulSoup
import psycopg2
from datetime import datetime
import unicodedata
from unidecode import unidecode
import re
import time

from parser_worker import parser


def clean_text(text: str) -> str:
    if not text:
        return text
    text = unicodedata.normalize('NFKC', text.strip())
    text = unidecode(text)
    text = re.sub(r'\s+', ' ', text)
    return text


def parse_date(date_text: str):
    date_text = date_text.replace("Results for ", "").strip()

    date_text = re.sub(r'(?<=\d)(st|nd|rd|th)', '', date_text)

    # Массив месяцев
    months = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]

    for m in months:
        pattern = re.compile(m[:3], re.IGNORECASE)
        if pattern.match(date_text):
            rest = re.sub(r'^[A-Za-z]+', '', date_text, count=1)
            date_text = m + rest
            break

    try:
        return datetime.strptime(date_text, "%B %d %Y").date()
    except Exception as e:
        print(f"Failed to parse date: {date_text} -> {e}")
        return None

DB_PARAMS = {
        "dbname": "cs2_matches",
        "user": "postgres",
        "password": "12345",
        "host": "localhost",
        "port": 5432
    }

conn = psycopg2.connect(**DB_PARAMS)
conn.set_client_encoding("UTF8")
cur = conn.cursor()

cur.execute("""
    SELECT date, team1, team2, tournament, id
    FROM matches
    WHERE date = (SELECT MAX(date) FROM matches)
    ORDER BY id DESC
    LIMIT 1;
""")
last_match = cur.fetchone()

if last_match:
    last_date, last_team1, last_team2, last_tournament, last_id = last_match
    print(f"[DB] Last match in the DB: {last_date} | {last_team1} vs {last_team2} | {last_tournament} | id={last_id}")
else:
    print("[DB] The database is empty - a full fetch will be performed.")
    last_date, last_team1, last_team2, last_tournament = None, None, None, None


scraper = cloudscraper.create_scraper()


def fetch_matches_from_page(offset):
    url = f"https://www.hltv.org/results?offset={offset}"
    print(f"Fetching: {url}")

    max_retries = 3
    for attempt in range(max_retries):
        # ВАЖНО: Используем Selenium-парсер вместо scraper.get()
        html = parser.get_page(url)

        if html:
            soup = BeautifulSoup(html, "html.parser")
            blocks = soup.select(".results-sublist")
            if blocks:
                return blocks

        print(f"Try {attempt + 1} failed, wait 5 seconds...")
        time.sleep(5)
    return []

def parse_matches(blocks, stop_on_last=True):
    new_matches = []
    stop_parsing = False

    for block in blocks:
        date_elem = block.select_one(".standard-headline")
        if not date_elem:
            continue
        date_obj = parse_date(date_elem.text)

        # Читаем новые контейнеры матчей
        matches = block.select(".result-con")
        for match in matches:
            teams = match.select(".team")
            if len(teams) < 2:
                continue

            team1 = clean_text(teams[0].text)
            team2 = clean_text(teams[1].text)

            tournament_elem = match.select_one(".event-name")
            tournament = clean_text(tournament_elem.text) if tournament_elem else "Unknown"

            map_text_elem = match.select_one(".map-text")
            map_text = map_text_elem.text.strip() if map_text_elem else "Unknown"
            match_format = map_text.lower() if map_text.lower() in ["bo1", "bo3", "bo5"] else "bo1"

            score_elem = match.select_one(".result-score")
            if score_elem:
                scores = score_elem.text.strip().split("-")
                try:
                    s1_raw = int(scores[0].strip())
                    s2_raw = int(scores[1].strip())
                except:
                    s1_raw, s2_raw = 0, 0
            else:
                s1_raw, s2_raw = 0, 0

            if match_format == "bo1":
                s1, s2 = (1, 0) if s1_raw > s2_raw else (0, 1)
            else:
                s1, s2 = s1_raw, s2_raw

            if stop_on_last and last_date:
                if (
                    date_obj == last_date and
                    team1 == last_team1 and
                    team2 == last_team2 and
                    tournament == last_tournament
                ):
                    print("The latest match was found in the database on the page. We are adding more recent matches....")
                    stop_parsing = True
                    break

            new_matches.append((date_obj, team1, team2, match_format, s1, s2, tournament))

        if stop_parsing:
            break

    return new_matches, stop_parsing

new_matches = []

if not last_match:

    for offset in range(20000, -100, -100):
        blocks = fetch_matches_from_page(offset)
        if not blocks:
            continue
        page_matches, _ = parse_matches(blocks, stop_on_last=False)

        new_matches.extend(page_matches)
        time.sleep(1)

else:

    for offset in range(0, 15000, 100):
        blocks = fetch_matches_from_page(offset)
        if not blocks:
            continue
        page_matches, stop = parse_matches(blocks, stop_on_last=True)
        new_matches.extend(page_matches)
        if stop:
            break
        time.sleep(1)

if new_matches:
    new_matches = list(reversed(new_matches))

if last_date:
    before_filter = len(new_matches)
    new_matches = [
        m for m in new_matches
        if m[0] > last_date
        or (m[0] == last_date and (m[1], m[2], m[6]) != (last_team1, last_team2, last_tournament))
    ]
    print(f"Filtering old matches: {before_filter} → {len(new_matches)}")

if not new_matches:
    print("There are no new matches, the database is up to date.")
else:
    print(f"New matches found: {len(new_matches)}")
    for m in new_matches:
        try:
            cur.execute("""
                INSERT INTO matches (date, team1, team2, match_format, score1, score2, tournament)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (team1, team2, date, tournament) DO NOTHING
            """, m)
            print(f"Added: {m[0]} | {m[1]} vs {m[2]} | {m[6]}")
        except Exception as e:
            print(f"Insert error {m[1]} vs {m[2]}: {e}")

conn.commit()
cur.close()
conn.close()
print("DB check completed.")
