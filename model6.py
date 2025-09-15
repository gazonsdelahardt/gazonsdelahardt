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
from collections import deque

processed_message_ids = set()
processed_order = deque(maxlen=2000)

# =====================
# Load Environment Vars
# =====================
load_dotenv()


from pathlib import Path

HISTORY_FILE = Path("chat_history.csv")
HISTORY_FILE.touch(exist_ok=True)

def append_history(wa_id: str, role: str, content: str) -> None:
    """Ajoute une ligne d'historique (wa_id, role=user/assistant, content, timestamp)."""
    with HISTORY_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        w.writerow([wa_id, role, content, datetime.utcnow().isoformat()])

def read_history(wa_id: str, limit: int = 20):
    """Retourne les 'limit' derniers messages (role, content) pour ce wa_id."""
    rows = []
    if not HISTORY_FILE.exists():
        return rows
    with HISTORY_FILE.open("r", newline="") as f:
        r = csv.reader(f)
        for row in r:
            if len(row) < 4:
                continue
            if row[0] == wa_id:
                rows.append({"role": row[1], "content": row[2]})
    return rows[-limit:]



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
    print("WA send status:", response.status_code, response.text, flush=True)

    # ✅ If 24h window expired, send template instead

# --- Flask app ---
app = Flask(__name__)

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
- Vouvoiement pour les particuliers, vouvoiement pour les pros.
- Phrases courtes, fluides, naturelles et positives.
- Varier les formulations, éviter les répétitions.
- Utiliser des tournures polies et engageantes (« Je vous conseille de… », « Pour obtenir le meilleur résultat… »).
- Ajouter des intonations humaines (« Parfait ! », « Bonne nouvelle ! »).
- Montrer de l’empathie (« Je comprends votre inquiétude », « C’est une question fréquente »).

📝 Construction des réponses :
- Toujours accueillir ou valider la demande.
- Répondre clairement et directement.
- Ajouter un conseil ou une valeur ajoutée.
- Réponses courtes (1–3 phrases) + une question ouverte à la fin, mais ne pas forcer pour passer à l'achat.

📋 Missions concrètes :
- Calculer les surfaces de gazon nécessaires.
- Répondre aux questions sur prix, délais, livraison.
- Conseiller sur les types de gazon (Sportif, Résistant à la sécheresse).
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
- Ne jamais donner de réponse fausse.
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
    # --- Vérification Meta (GET) ---
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        verify_token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and verify_token == VERIFY_TOKEN:
            return (challenge or ""), 200
        return "Verification token mismatch", 403

    # --- Réception messages (POST) ---
    try:
        data = request.get_json(force=True, silent=True) or {}
        print("Incoming webhook:", json.dumps(data, indent=2), flush=True)

        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})

        # Ignore accusés de réception/lecture
        if "statuses" in value:
            return jsonify({"status": "ignored_status"}), 200

        # Traite seulement les messages entrants
        if "messages" in value:
            msg = value["messages"][0]
            msg_id = msg.get("id") or ""

            # --- Déduplication: ignore si déjà traité ---
            if msg_id in processed_message_ids:
                return jsonify({"status": "duplicate_ignored"}), 200
            processed_message_ids.add(msg_id)
            processed_order.append(msg_id)
            if len(processed_message_ids) > 5000:
                while len(processed_message_ids) > 4000 and processed_order:
                    processed_message_ids.discard(processed_order.popleft())

            wa_id = msg.get("from")
            msg_type = msg.get("type")
            user_text = ""

            if msg_type == "text":
                user_text = msg.get("text", {}).get("body", "")
            elif msg_type == "interactive":
                interactive = msg.get("interactive", {})
                # boutons / listes
                user_text = interactive.get("button_reply", {}).get("title") or \
                            interactive.get("list_reply", {}).get("title") or ""
            else:
                user_text = "(message non-textuel reçu)"


                    # 4) Appel OpenAI
                    chat = client.chat.completions.create(
                        model="gpt-4o-mini",
                        temperature=0.8,
                        max_tokens=300,
                        messages=messages
                    )
                    reply_text = (chat.choices[0].message.content or "").strip()
                        
                    # 5) Mémoriser la réponse de l'IA
                    if reply_text:
                        append_history(wa_id, "assistant", reply_text)
                        
                    # Optionnel : suffixe ultra-léger (vide ici pour ne rien ajouter)
                    light_reminder = " "   
                        
                    # Ajoute le rappel uniquement s’il n’apparaît pas déjà
                    if "entretien" not in reply_text.lower():
                        reply_text = f"{reply_text}\n\n{light_reminder}"
                        
                    # S’assure que la réponse se termine par une question
                    if not reply_text.strip().endswith(("?", "？")):
                        reply_text = reply_text.rstrip(".!… ") + " " + closing_question
            if not reply_text:
                reply_text = (
                    "Merci pour votre message 👋 Le gazon en rouleau vous fait gagner du temps "
                    "et donne une densité immédiate, mais il demande arrosage, tonte et 3 apports d’engrais/an. "
                    "Quel est votre code postal et la surface à couvrir ?"
                )

            # --- Envoi WhatsApp + sortie webhook ---
            try:
                send_whatsapp_message(wa_id, reply_text)
            except Exception as e:
                print("send_whatsapp_message error:", e, flush=True)

            return jsonify({"status": "ok"}), 200
      	  # Rien d’utile
        return jsonify({"status": "no_message"}), 200

    except Exception as e:
        print("Webhook error:", e, flush=True)
        return jsonify({"status": "error", "detail": str(e)}), 500

if __name__ == "__main__":
    # Si tu as un worker périodique, déclenche-le une seule fois en local
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        try:
            threading.Thread(target=promotion_worker, daemon=True).start()
        except Exception:
            pass

    # Render fournit la variable d'env PORT
    import os as _os
    port = int(_os.environ.get("PORT", 5050))

    # En production: pas de debug, pas de reloader
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
