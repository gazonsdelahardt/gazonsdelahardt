import requests
import json
import os
import csv
import threading
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from openai import OpenAI
from collections import defaultdict

# =====================
# Load Environment Vars
# =====================
load_dotenv()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")           # Meta Verify Token
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")       # Permanent WhatsApp token
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")     # WhatsApp Phone Number ID
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")       # OpenAI API key

CHAT_CSV = "chat_history.csv"
CUSTOMER_FILE = "customers.csv"

app = Flask(__name__)

# Initialize OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# =====================
# Memory (per user chat)
# =====================
conversations = defaultdict(list)  # { wa_id: [messages] }
customers = set()  # unique customer IDs for promotions


# =====================
# Save Chat History (CSV)
# =====================
def save_chat_to_csv(wa_id, role, content):
    """Append a new chat message to CSV file"""
    with open(CHAT_CSV, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now().isoformat(), wa_id, role, content])


# Ensure CSV has headers
if not os.path.exists(CHAT_CSV):
    with open(CHAT_CSV, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "wa_id", "role", "content"])


# =====================
# Customer Management
# =====================
def load_customers():
    """Load customers from file"""
    if os.path.exists(CUSTOMER_FILE):
        with open(CUSTOMER_FILE, "r", encoding="utf-8") as f:
            for line in f:
                number = line.strip()
                if number:
                    customers.add(number)

def save_customer(wa_id):
    """Add new customer to file if not already saved"""
    if wa_id not in customers:
        customers.add(wa_id)
        with open(CUSTOMER_FILE, "a", encoding="utf-8") as f:
            f.write(f"{wa_id}\n")

# Load customers on startup
load_customers()


# =====================
# WhatsApp Messaging
# =====================
def send_whatsapp_message(wa_id, text):
    """Send a WhatsApp message. Fallback to template if >24h window closed."""
    url = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    # Try free-form message first
    payload = {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "type": "text",
        "text": {"body": text}
    }

    response = requests.post(url, headers=headers, data=json.dumps(payload))
    result = response.json()

    # ✅ If 24h window expired, send template instead

# --- Flask app ---
app = Flask(__name__)

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode == 'subscribe' and token == VERIFY_TOKEN:
            return (challenge or ''), 200
        return 'Verification token mismatch', 403

    # Réception des messages (POST)
    try:
        data = request.get_json(force=True, silent=True) or {}
        print('POST /webhook ->', data, flush=True)
        # Ici tu ajoutes ton code qui traite les messages WhatsApp
        return 'OK', 200
    except Exception as e:
        return f'Error: {e}', 500


def send_promo_template(wa_id):
    """Always send the weekly_promo template for promotions"""
    url = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    template_payload = {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "type": "template",
        "template": {
            "name": "hello_world",   # 👈 your approved promo template
            "language": {"code": "en_US"}  # 👈 must match template language
        }
    }

    response = requests.post(url, headers=headers, data=json.dumps(template_payload))
    result = response.json()
    print(f"📤 Promo API response for {wa_id}:", result)
    return result


# =====================
# Promotion Scheduler
# =====================
last_promo_date = None

def promotion_worker():
    global last_promo_date
    while True:
        now = datetime.now()
        days_ahead = (5 - now.weekday()) % 7   # every Friday
        next_run = now + timedelta(days=days_ahead)
        next_run = next_run.replace(hour=20, minute=59, second=0, microsecond=0)

        if next_run <= now:
            next_run += timedelta(days=7)

        wait_time = (next_run - now).total_seconds()
        time.sleep(wait_time)

        if last_promo_date == next_run.date():
            continue  # already sent today

        print("🚀 Sending weekly promo template...")
        for wa_id in customers:
            send_promo_template(wa_id)

        last_promo_date = next_run.date()


# =====================
# System prompt (role)
# =====================
GAZONS_PROMPT = """
Tu es le conseiller officiel de l’entreprise « Gazons de la Hardt », producteur et distributeur de gazon en rouleau et de produits associés (terre amendée, semences, engrais, cailloux décoratifs, bois sec avec ONF Bois Bûche Sud Alsace).
Tu représentes une entreprise familiale, sérieuse et engagée, qui met en avant le travail bien fait, l’ancrage local et la proximité avec ses clients.

🎯 Tes missions :
1. Agent commercial : informer, conseiller, convaincre et accompagner le client jusqu’à l’achat.
2. Service Après-Vente (SAV) : rassurer, expliquer clairement, résoudre les problèmes, et si besoin, orienter vers un interlocuteur humain.

🗣️ Intonation et style :
- Tu parles **comme un humain** : chaleureux, accessible et professionnel.
- Tutoiement pour les particuliers, vouvoiement pour les pros.
- Phrases courtes, fluides, naturelles et positives.
- Varier les formulations, éviter les répétitions.
- Utiliser des tournures polies et engageantes (« Je vous conseille de… », « Pour obtenir le meilleur résultat… »).
- Ajouter des intonations humaines (« Parfait ! », « Bonne nouvelle ! »).
- Montrer de l’empathie (« Je comprends votre inquiétude », « C’est une question fréquente »).

📝 Construction des réponses :
- Toujours accueillir ou valider la demande.
- Répondre clairement et directement.
- Ajouter un conseil ou une valeur ajoutée.
- Terminer par une ouverture ou un appel à l’action discret.

📋 Missions concrètes :
- Calculer les surfaces de gazon nécessaires.
- Répondre aux questions sur prix, délais, livraison.
- Conseiller sur les types de gazon (Sportif, Résistant à la sécheresse, Ornement).
- Instructions pratiques : sol, pose, arrosage, fertilisation, entretien.
- Conseils saisonniers (ex : automne → scarification, regarnissage, engrais).
- Rassurer et accompagner en cas de souci.
- Informer sur les autres produits (terre amendée, semences, engrais, cailloux décoratifs, bois sec).
- Mettre en avant le service après-vente.

📞 Coordonnées à donner si besoin :
- Téléphone : 06 71 22 75 68 – 06 38 14 25 19
- Site web : www.gdlh.fr

⚠️ Règles :
- Ne jamais dire que tu es une IA.
- Ne jamais donner de réponse fausse → sinon proposer un relais à un conseiller humain.
- Toujours être positif et rassurant.
- Conseils pratiques et simples, pas trop techniques.

🎯 Objectif final :
Donner au client le sentiment de parler à un vrai conseiller dédié, à l’écoute, compréhensif et efficace, jusqu’à déclencher une commande ou une mise en relation.
"""


# =====================
# Webhook Endpoint
# =====================
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        verify_token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if verify_token == VERIFY_TOKEN:
            return challenge
        return "Verification failed", 403

    elif request.method == "POST":
        data = request.get_json()
        print("Incoming webhook:", json.dumps(data, indent=2))

        try:
            value = data["entry"][0]["changes"][0]["value"]

            # 🚫 Ignore delivery/read receipts
            if "statuses" in value:
                return jsonify({"status": "ignored"}), 200

            # 🚫 Process only real incoming messages
            if "messages" in value:
                msg = value["messages"][0]
                wa_id = msg["from"]

                # Save customer for promotions
                save_customer(wa_id)

                # 🚫 Ignore our own business number
                if wa_id == PHONE_NUMBER_ID:
                    return jsonify({"status": "ignored"}), 200

                # 🚫 Ignore non-text
                if msg.get("type") != "text":
                    return jsonify({"status": "ignored"}), 200

                msg_text = msg["text"]["body"]
                print(f"📩 Message from {wa_id}: {msg_text}")

                # Append user message to memory + CSV
                conversations[wa_id].append({"role": "user", "content": msg_text})
                save_chat_to_csv(wa_id, "user", msg_text)

                # Prepare history
                history = [{"role": "system", "content": GAZONS_PROMPT}] + conversations[wa_id]

                # Call OpenAI
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=history
                )
                ai_reply = response.choices[0].message.content.strip()

                # Save assistant reply
                conversations[wa_id].append({"role": "assistant", "content": ai_reply})
                save_chat_to_csv(wa_id, "assistant", ai_reply)

                # Send reply back
                send_whatsapp_message(wa_id, ai_reply)

        except Exception as e:
            print("⚠️ Error processing webhook:", e)

        return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    # Prevent double thread when Flask debug reloads
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        threading.Thread(target=promotion_worker, daemon=True).start()

    app.run(port=5000, debug=True, use_reloader=True)
