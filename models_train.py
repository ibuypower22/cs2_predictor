import os

import pandas as pd
import numpy as np
import psycopg2
import pickle

from catboost import CatBoostClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss, brier_score_loss
from sklearn.preprocessing import LabelEncoder
from xgboost import XGBClassifier

DB_PARAMS = {
    "dbname": "railway",  # имя базы в облаке
    "user": "postgres",   # пользователь
    "password": "nrrWMrzYFdeaBdvhqNuJDUbKHTIbfOiw",  # пароль из Railway
    "host": "interchange.proxy.rlwy.net",  # хост из public URL
    "port": 25251  # порт из public URL
}


# --- Загрузка данных ---
conn = psycopg2.connect(**DB_PARAMS)
df = pd.read_sql("""
    SELECT date, team1, team2, score1, score2, match_format
    FROM matches
""", conn)
conn.close()

df['date'] = pd.to_datetime(df['date'])
df = df.sort_values('date').reset_index(drop=True)

# --- Целевая переменная ---
def get_winner(score1, score2):
    if score1 > score2:
        return 1
    elif score2 > score1:
        return 0
    else:
        return -1  # на случай ничьей

df['winner'] = df.apply(lambda row: get_winner(row['score1'], row['score2']), axis=1)
df['team1_maps_won'] = df['score1']
df['team2_maps_won'] = df['score2']

# --- LabelEncoder для команд ---
le = LabelEncoder()
all_teams = pd.concat([df['team1'], df['team2']]).unique()
le.fit(all_teams)
df['team1_enc'] = le.transform(df['team1'])
df['team2_enc'] = le.transform(df['team2'])

# --- Итеративное вычисление формы, H2H и разницы карт ---
last_n = 5
team_history = {}          # +1/-1 по результатам последних N матчей
h2h_counts = {}            # победы команды1 против команды2
team_maps_diff = {}        # суммарная разница карт для команды
team1_form, team2_form, h2h_feat = [], [], []
team1_maps_diff, team2_maps_diff = [], []

for idx, row in df.iterrows():
    t1, t2 = row['team1'], row['team2']
    key = frozenset([t1, t2])

    # --- Форма ---
    hist1 = team_history.get(t1, [])
    hist2 = team_history.get(t2, [])
    form1 = np.mean(hist1[-last_n:]) if hist1 else 0.0
    form2 = np.mean(hist2[-last_n:]) if hist2 else 0.0

    # --- H2H ---
    pair = h2h_counts.get(key, {t1: 0, t2: 0})
    h2h_val = pair.get(t1, 0) - pair.get(t2, 0)

    # --- Суммарная разница карт ---
    maps_diff1 = team_maps_diff.get(t1, 0)
    maps_diff2 = team_maps_diff.get(t2, 0)

    team1_form.append(form1)
    team2_form.append(form2)
    h2h_feat.append(h2h_val)
    team1_maps_diff.append(maps_diff1)
    team2_maps_diff.append(maps_diff2)

    # --- обновляем историю после текущего матча ---
    if row['winner'] == 1:
        result_t1 = 1
        result_t2 = -1
    elif row['winner'] == 0:
        result_t1 = -1
        result_t2 = 1
    else:
        result_t1 = result_t2 = 0

    team_history.setdefault(t1, []).append(result_t1)
    team_history.setdefault(t2, []).append(result_t2)

    # --- обновляем разницу карт ---
    team_maps_diff[t1] = team_maps_diff.get(t1, 0) + (row['score1'] - row['score2'])
    team_maps_diff[t2] = team_maps_diff.get(t2, 0) + (row['score2'] - row['score1'])

    # --- обновляем H2H ---
    if key not in h2h_counts:
        h2h_counts[key] = {t1: 0, t2: 0}
    if row['winner'] == 1:
        h2h_counts[key][t1] += 1
    elif row['winner'] == 0:
        h2h_counts[key][t2] += 1

# --- Добавляем признаки в DataFrame ---
df['team1_form'] = team1_form
df['team2_form'] = team2_form
df['h2h'] = h2h_feat
df['team1_maps_diff'] = team1_maps_diff
df['team2_maps_diff'] = team2_maps_diff

# --- Фичи и таргет для моделей ---
features = ['team1_enc', 'team2_enc', 'team1_form', 'team2_form', 'h2h', 'team1_maps_diff', 'team2_maps_diff']
X = df[features]
y = df['winner']

# --- Временной сплит ---
split_idx = int(len(df) * 0.8)
X_train, y_train = X.iloc[:split_idx], y.iloc[:split_idx]
X_test, y_test = X.iloc[split_idx:], y.iloc[split_idx:]

# --- Обучение моделей ---
lr = LogisticRegression(max_iter=2000)
lr.fit(X_train, y_train)

xgb = XGBClassifier(eval_metric='logloss')
xgb.fit(X_train, y_train)

cb = CatBoostClassifier(iterations=300, verbose=0, cat_features=['team1_enc', 'team2_enc'])
cb.fit(X_train, y_train)

# --- Оценка моделей ---
for name, model in [('Logistic Regression', lr), ('XGBoost', xgb), ('CatBoost', cb)]:
    y_pred = model.predict(X_test)
    probs = model.predict_proba(X_test)[:, 1]
    print(f"{name} Accuracy:", accuracy_score(y_test, y_pred))
    print("Logloss:", log_loss(y_test, model.predict_proba(X_test)))
    print("Brier score:", brier_score_loss(y_test, probs))
    print("-" * 40)

# --- Сохранение моделей и словарей ---
MODELS_DIR = "."
os.makedirs(MODELS_DIR, exist_ok=True)

with open(os.path.join(MODELS_DIR, "lr_model.pkl"), "wb") as f:
    pickle.dump(lr, f)
with open(os.path.join(MODELS_DIR, "xgb_model.pkl"), "wb") as f:
    pickle.dump(xgb, f)
with open(os.path.join(MODELS_DIR, "cb_model.pkl"), "wb") as f:
    pickle.dump(cb, f)
with open(os.path.join(MODELS_DIR, "label_encoder.pkl"), "wb") as f:
    pickle.dump(le, f)
with open(os.path.join(MODELS_DIR, "team_history.pkl"), "wb") as f:
    pickle.dump(team_history, f)
with open(os.path.join(MODELS_DIR, "h2h_counts.pkl"), "wb") as f:
    pickle.dump(h2h_counts, f)
with open(os.path.join(MODELS_DIR, "team_maps_diff.pkl"), "wb") as f:
    pickle.dump(team_maps_diff, f)

# --- Функция для прогноза с учётом формы и H2H ---
def predict_from_matches(team1_name, team2_name):
    if team1_name not in le.classes_ or team2_name not in le.classes_:
        print(f"One of the teams ({team1_name}, {team2_name}) not in DB!")
        return

    # --- кодируем команды ---
    t1_enc = le.transform([team1_name])[0]
    t2_enc = le.transform([team2_name])[0]

    # --- достаём форму и H2H ---
    last_n = 5
    hist1 = team_history.get(team1_name, [])
    hist2 = team_history.get(team2_name, [])
    form1 = np.mean(hist1[-last_n:]) if hist1 else 0.0
    form2 = np.mean(hist2[-last_n:]) if hist2 else 0.0

    key = frozenset([team1_name, team2_name])
    pair = h2h_counts.get(key, {team1_name: 0, team2_name: 0})
    h2h_val = pair.get(team1_name, 0) - pair.get(team2_name, 0)

    X_new = pd.DataFrame([{
        'team1_enc': t1_enc,
        'team2_enc': t2_enc,
        'team1_form': form1,
        'team2_form': form2,
        'h2h': h2h_val,
        'match_format_num': 1
    }])

    print("\nПризнаки для прогноза:")
    print(X_new)

    models = {'Logistic Regression': lr, 'XGBoost': xgb, 'CatBoost': cb}
    results = {}

    for name, model in models.items():
        probs = model.predict_proba(X_new)[0]
        pred_class = model.predict(X_new)[0]
        winner = team1_name if pred_class == 1 else team2_name
        results[name] = {
            'winner': winner,
            'prob_winner': probs[pred_class],
            'probs_all_classes': probs
        }

    return results

