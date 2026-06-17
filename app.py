import os
import requests
import psycopg2
import psycopg2.extras
from flask import Flask, request, redirect, jsonify, render_template, url_for
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from urllib.parse import urlencode

app = Flask(__name__)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
ML_CLIENT_ID     = os.environ.get("ML_CLIENT_ID", "8220714576874952").strip()
ML_CLIENT_SECRET = os.environ.get("ML_CLIENT_SECRET", "3tJSeu3knCS8mGU3lpAGuYkKduLEbOEn").strip()
ML_REDIRECT_URI  = "https://ml-kpi-dashboard.onrender.com/callback"
DATABASE_URL     = os.environ.get("DATABASE_URL", "").strip()

ML_AUTH_URL  = "https://auth.mercadolivre.com.br/authorization"
ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
ML_API_BASE  = "https://api.mercadolibre.com"

# ─── DATABASE ──────────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sellers (
                    user_id       TEXT PRIMARY KEY,
                    nickname      TEXT,
                    access_token  TEXT,
                    refresh_token TEXT,
                    created_at    TIMESTAMP DEFAULT NOW(),
                    updated_at    TIMESTAMP DEFAULT NOW()
                );
            """)
        conn.commit()

# ─── TOKEN HELPERS ─────────────────────────────────────────────────────────────
def refresh_token(user_id, refresh_tk):
    r = requests.post(ML_TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "client_id":     ML_CLIENT_ID,
        "client_secret": ML_CLIENT_SECRET,
        "refresh_token": refresh_tk,
    })
    if r.status_code == 200:
        data = r.json()
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE sellers
                    SET access_token=%s, refresh_token=%s, updated_at=NOW()
                    WHERE user_id=%s
                """, (data["access_token"], data.get("refresh_token", refresh_tk), user_id))
            conn.commit()
        return data["access_token"]
    return None

def get_token(user_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT access_token, refresh_token FROM sellers WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
    if not row:
        return None
    token = row["access_token"]
    test = requests.get(f"{ML_API_BASE}/users/me", headers={"Authorization": f"Bearer {token}"})
    if test.status_code == 401:
        token = refresh_token(user_id, row["refresh_token"])
    return token

def ml_get(path, token, params=None):
    r = requests.get(f"{ML_API_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, params=params)
    return r.json() if r.status_code == 200 else {}

# ─── GMV HELPERS ───────────────────────────────────────────────────────────────
def fetch_gmv(user_id, token, date_from, date_to):
    offset = 0
    limit  = 50
    total_bruto     = 0.0
    total_cancelado = 0.0
    total_devolucao = 0.0
    orders_count    = 0

    while True:
        data = ml_get("/orders/search", token, {
            "seller": user_id,
            "order.date_created.from": f"{date_from}T00:00:00.000-03:00",
            "order.date_created.to":   f"{date_to}T23:59:59.000-03:00",
            "sort":   "date_desc",
            "offset": offset,
            "limit":  limit,
        })
        results = data.get("results", [])
        if not results:
            break

        for order in results:
            amount = float(order.get("total_amount", 0))
            status = order.get("status", "")
            if status == "paid":
                total_bruto  += amount
                orders_count += 1
            elif status == "cancelled":
                total_cancelado += amount

        paging = data.get("paging", {})
        offset += limit
        if offset >= paging.get("total", 0):
            break

    gmv_liquido  = total_bruto - total_cancelado - total_devolucao
    ticket_medio = (total_bruto / orders_count) if orders_count > 0 else 0

    return {
        "bruto":        round(total_bruto, 2),
        "cancelado":    round(total_cancelado, 2),
        "devolucao":    round(total_devolucao, 2),
        "liquido":      round(gmv_liquido, 2),
        "orders":       orders_count,
        "ticket_medio": round(ticket_medio, 2),
    }

def calc_comparativo(atual, anterior):
    if anterior == 0:
        return {"delta": atual, "pct": 0}
    delta = atual - anterior
    pct   = round((delta / anterior) * 100, 1)
    return {"delta": round(delta, 2), "pct": pct}

# ─── ROUTES ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/ping")
def ping():
    return "pong", 200

@app.route("/debug")
def debug():
    return jsonify({
        "client_id":     ML_CLIENT_ID,
        "secret_len":    len(ML_CLIENT_SECRET),
        "redirect_uri":  ML_REDIRECT_URI,
        "db_configured": bool(DATABASE_URL),
    })

@app.route("/auth")
def auth():
    # Página intermediária que garante o servidor está acordado ANTES de ir ao ML
    return render_template("auth_loading.html", auth_url=f"{ML_AUTH_URL}?" + urlencode({
        "response_type": "code",
        "client_id":     ML_CLIENT_ID,
        "redirect_uri":  ML_REDIRECT_URI,
    }))

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return f"Erro: código não recebido. Args: {dict(request.args)}", 400

    r = requests.post(ML_TOKEN_URL, data={
        "grant_type":    "authorization_code",
        "client_id":     ML_CLIENT_ID,
        "client_secret": ML_CLIENT_SECRET,
        "code":          code,
        "redirect_uri":  ML_REDIRECT_URI,
    })

    if r.status_code != 200:
        return f"Erro ao obter token: {r.text}<br>client_id={ML_CLIENT_ID}, redirect_uri={ML_REDIRECT_URI}, code={code[:10]}...", 400

    token_data   = r.json()
    access_token = token_data["access_token"]
    refresh_tk   = token_data.get("refresh_token", "")

    user_info = requests.get(f"{ML_API_BASE}/users/me",
        headers={"Authorization": f"Bearer {access_token}"}).json()

    user_id  = str(user_info.get("id", ""))
    nickname = user_info.get("nickname", "")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sellers (user_id, nickname, access_token, refresh_token)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET nickname=%s, access_token=%s, refresh_token=%s, updated_at=NOW()
            """, (user_id, nickname, access_token, refresh_tk,
                  nickname, access_token, refresh_tk))
        conn.commit()

    return redirect("/?connected=1")

@app.route("/api/sellers")
def api_sellers():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT user_id, nickname, updated_at FROM sellers ORDER BY nickname")
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/sellers/<user_id>", methods=["DELETE"])
def delete_seller(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sellers WHERE user_id=%s", (user_id,))
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/gmv/<user_id>")
def api_gmv(user_id):
    period = request.args.get("period", "30")
    days   = int(period)

    token = get_token(user_id)
    if not token:
        return jsonify({"error": "Seller não encontrado ou token inválido"}), 404

    today     = datetime.today()
    date_to   = today.strftime("%Y-%m-%d")
    date_from = (today - timedelta(days=days)).strftime("%Y-%m-%d")

    wow_to   = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    wow_from = (today - timedelta(days=days*2)).strftime("%Y-%m-%d")

    mom_to   = (today - relativedelta(months=1)).strftime("%Y-%m-%d")
    mom_from = (today - relativedelta(months=1) - timedelta(days=days)).strftime("%Y-%m-%d")

    yoy_to   = (today - relativedelta(years=1)).strftime("%Y-%m-%d")
    yoy_from = (today - relativedelta(years=1) - timedelta(days=days)).strftime("%Y-%m-%d")

    atual    = fetch_gmv(user_id, token, date_from, date_to)
    anterior = fetch_gmv(user_id, token, wow_from, wow_to)
    mom_data = fetch_gmv(user_id, token, mom_from, mom_to)
    yoy_data = fetch_gmv(user_id, token, yoy_from, yoy_to)

    return jsonify({
        "periodo": {"from": date_from, "to": date_to, "days": days},
        "atual":   atual,
        "wow": {**anterior, "vs_atual": calc_comparativo(atual["liquido"], anterior["liquido"])},
        "mom": {**mom_data,  "vs_atual": calc_comparativo(atual["liquido"], mom_data["liquido"])},
        "yoy": {**yoy_data,  "vs_atual": calc_comparativo(atual["liquido"], yoy_data["liquido"])},
    })

# ─── INIT ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f"DB init error: {e}")

# ─── DEMO SELLER ───────────────────────────────────────────────────────────────
@app.route("/api/demo/activate")
def demo_activate():
    """Injeta um seller fictício no banco para testes."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sellers (user_id, nickname, access_token, refresh_token)
                VALUES ('DEMO_001', 'DEMO — Oficial Modas', 'demo_token', 'demo_refresh')
                ON CONFLICT (user_id) DO UPDATE
                SET nickname='DEMO — Oficial Modas', updated_at=NOW()
            """)
        conn.commit()
    return redirect("/?connected=1")

@app.route("/api/gmv/DEMO_001")
def api_gmv_demo():
    import random, math
    period = int(request.args.get("period", "30"))

    def fake_gmv(base_bruto, seed):
        random.seed(seed)
        bruto     = round(base_bruto * random.uniform(0.9, 1.1), 2)
        cancelado = round(bruto * random.uniform(0.03, 0.07), 2)
        devolucao = round(bruto * random.uniform(0.01, 0.03), 2)
        liquido   = round(bruto - cancelado - devolucao, 2)
        orders    = random.randint(80, 300)
        return {
            "bruto": bruto, "cancelado": cancelado,
            "devolucao": devolucao, "liquido": liquido,
            "orders": orders, "ticket_medio": round(bruto / orders, 2)
        }

    base = 85000 * (period / 30)
    atual    = fake_gmv(base, 42)
    anterior = fake_gmv(base * 0.88, 7)
    mom_data = fake_gmv(base * 0.92, 13)
    yoy_data = fake_gmv(base * 0.75, 99)

    def comp(a, b):
        delta = round(a - b, 2)
        pct   = round((delta / b * 100) if b else 0, 1)
        return {"delta": delta, "pct": pct}

    today = datetime.today()
    return jsonify({
        "periodo": {
            "from": (today - timedelta(days=period)).strftime("%Y-%m-%d"),
            "to":   today.strftime("%Y-%m-%d"),
            "days": period
        },
        "atual": atual,
        "wow":   {**anterior, "vs_atual": comp(atual["liquido"], anterior["liquido"])},
        "mom":   {**mom_data,  "vs_atual": comp(atual["liquido"], mom_data["liquido"])},
        "yoy":   {**yoy_data,  "vs_atual": comp(atual["liquido"], yoy_data["liquido"])},
    })
