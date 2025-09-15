import random
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

# Paramètre délai avant relance
SILENCE_AFTER = timedelta(minutes=1)   # prod = 10 min ; pour test tu peux mettre 1

# =====================
# Load Environment Vars
# =====================
load_dotenv()

# Mémoire légère par contact (in-memory)
last_user_at = defaultdict(lambda: None)    # dernière heure d’un message client
last_bot_at = defaultdict(lambda: None)     # dernière heure d’un message IA
followup_sent = defaultdict(lambda: False)  # relance déjà envoyée ?

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

# =====================
# Flask + OpenAI
# =====================
from flask import Flask
app = Flask(__name__)

from openai import OpenAI
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
# Follow-up Worker (relance après silence)
# =====================

def followup_worker():
    CHECK_EVERY = 10  # pendant les tests ; remets 60 en prod
    while True:
        try:
            now = datetime.utcnow()
            for wa_id, last_user in list(last_user_at.items()):
                if not last_user:
                    print(f"[followup] skip {wa_id}: no last_user", flush=True)
                    continue

                if followup_sent.get(wa_id, False):
                    print(f"[followup] skip {wa_id}: already sent", flush=True)
                    continue

                last_bot = last_bot_at.get(wa_id)
                # Le bot doit avoir répondu après le dernier message user
                if not last_bot or last_bot <= last_user:
                    print(
                        f"[followup] skip {wa_id}: bot_not_after_user (last_bot={last_bot}, last_user={last_user})",
                        flush=True
                    )
                    continue

                delta = now - last_user
                if not (SILENCE_AFTER <= delta <= timedelta(hours=24)):
                    print(
                        f"[followup] wait {wa_id}: delta={delta}, window=({SILENCE_AFTER}, 24h)",
                        flush=True
                    )
                    continue

                print(f"[followup] SEND nudge to {wa_id} (delta={delta})", flush=True)
                try:
                    nudge = random.choice([
                        "Souhaitez-vous que je vous aide à estimer la surface ou la livraison ?",
                        "Je peux vous guider entre Elite et Water Saver si vous hésitez.",
                        "Besoin d’un récap rapide sur l’entretien (arrosage, tonte, engrais) ?",
                        "Je reste dispo si vous avez une question 🙂"
                    ])
                    send_whatsapp_message(wa_id, nudge)
                    followup_sent[wa_id] = True
                    last_bot_at[wa_id] = now
                    print(f"[followup] sent to {wa_id}", flush=True)
                except Exception as e:
                    print("followup send error:", e, flush=True)

            print(
                f"[followup] loop: users={len(last_user_at)}, sent_flags={sum(1 for v in followup_sent.values() if v)}",
                flush=True
            )
        except Exception as e:
            print("followup worker error:", e, flush=True)

        # Petit jitter pour éviter les envois trop synchronisés quand il y a beaucoup d'utilisateurs
        time.sleep(CHECK_EVERY + random.uniform(0, 2))


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

    # Amorcer un suivi même en outbound-first
    if wa_id not in last_user_at or last_user_at[wa_id] is None:
        last_user_at[wa_id] = datetime.utcnow()
        followup_sent[wa_id] = False
        print(f"[followup] outbound-first init for {wa_id} at {last_user_at[wa_id].isoformat()}", flush=True)

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

# --- Démarre le worker une seule fois (compatible Render/Gunicorn) ---
try:
    _FOLLOWUP_STARTED
except NameError:
    _FOLLOWUP_STARTED = True
    threading.Thread(target=followup_worker, daemon=True).start()
    print(">>> followup_worker STARTED", flush=True)


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

            # Le client vient de parler : on note l’heure et on autorise une future relance
            last_user_at[wa_id] = datetime.utcnow()
            followup_sent[wa_id] = False
            print(
                f"[followup] GOT user msg from {wa_id} at {last_user_at[wa_id].isoformat()} : {user_text}",
                flush=True
            )

            # --- Génère une réponse (OpenAI si possible, sinon fallback simple) ---
            reply_text = None
            try:
                if OPENAI_API_KEY:
                    # 1) mémoriser le message utilisateur
                    if user_text:
                        append_history(wa_id, "user", user_text)

                    # 2) recharger l'historique (20 derniers échanges)
                    past = read_history(wa_id, limit=20)

                    # 3) prompt système complet
                    system_prompt = (
                        "Tu es l’assistant commercial & SAV de l’entreprise « Les Gazons de la Hardt ».\n"
                        "\n"
                        "OBJECTIF\n"
                        "- Réponds en français, avec un ton professionnel, chaleureux et pédagogique.\n"
                        "- Informe le client brièvement : explique en quelques mots les avantages et limites.\n"
                        "- Mets en avant les bénéfices du gazon en rouleau : densité immédiate, gain de temps par rapport au semis.\n"
                        "- Mais rappelle aussi qu’il nécessite un entretien : tonte régulière, arrosage, 3 apports d’engrais par an.\n"
                        "- Si le client hésite, encourage-le à poser des questions et rassure-le.\n"
                        "- Ne force pas la vente immédiatement : assure-toi d’abord qu’il a toutes les infos nécessaires.\n"
                        "\n"
                        "NOTRE OFFRE\n"
                        "1) Gazon en rouleau ELITE : esthétique, dense, idéal usage familial/agrément.\n"
                        "2) Gazon en rouleau WATER SAVER : résistant à la sécheresse, économique en eau, parfait en plein soleil.\n"
                        "3) Graines de gazon : mêmes variétés que nos champs, pour semer soi-même (solution économique).\n"
                        "4) Engrais à libération lente : seulement 3 apports par an pour un gazon impeccable.\n"
                        "5) Livraison : via transporteurs, prix dépend de la ville, surface et date.\n"
                        "\n"
                        "DIAGNOSTIC À POSER (si infos manquantes)\n"
                        "- Surface (m²) et code postal.\n"
                        "- Exposition (soleil/ombre), possibilité d’arrosage.\n"
                        "- Objectif principal : rapidité, esthétique, économie d’eau, budget.\n"
                        "- Calendrier souhaité et accès camion.\n"
                        "\n"
                        "RÈGLES DE RECOMMANDATION\n"
                        "- Si mention sécheresse / arrosage limité / économie d’eau → WATER SAVER.\n"
                        "- Si priorité esthétique premium → ELITE.\n"
                        "- Si budget serré ou semis → Graines.\n"
                        "- Toujours proposer engrais comme complément utile.\n"
                        "- Si infos manquantes → poser 1 ou 2 questions ciblées.\n"
                        "\n"
                        "STYLE & CONTENU\n"
                        "- Réponds en 1–4 phrases claires, pédagogiques.\n"
                        "- Mets en avant avantages mais rappelle brièvement l’entretien nécessaire.\n"
                        "- Termine toujours par une question ouverte.\n"
                    )

                    # 4) Construire le contexte avec mémoire
                    messages = [{"role": "system", "content": system_prompt}]
                    messages.extend(past)
                    messages.append({"role": "user", "content": user_text or "Bonjour"})

                    # 5) Appel OpenAI
                    chat = client.chat.completions.create(
                        model="gpt-4o-mini",
                        temperature=0.8,
                        max_tokens=300,
                        messages=messages
                    )
                    reply_text = (chat.choices[0].message.content or "").strip()

                    # 6) Mémoriser la réponse IA
                    if reply_text:
                        append_history(wa_id, "assistant", reply_text)

                    # 7) Parfois terminer par une question (≈ 50%), sinon laisser respirer
                    def wants_question(user_txt, ai_txt):
                        # Si la réponse contient déjà des éléments “finaux”, on évite de relancer
                        keywords = ["prix", "tarif", "devis", "livraison", "planning", "disponible", "stock"]
                        if any(k in (ai_txt or "").lower() for k in keywords):
                            return False
                        return random.random() < 0.5  # 50% des cas

                    if wants_question(user_text or "", reply_text or ""):
                        closing_question = random.choice([
                            "Vous préférez viser l’esthétique, l’économie d’eau, ou la simplicité d’entretien ?",
                            "Souhaitez-vous qu’on estime la surface et la livraison ?",
                            "Vous avez déjà une date en tête pour la pose ?",
                            "Je vous détaille l’entretien (arrosage, tonte, engrais) ?"
                        ])
                        if not reply_text.strip().endswith(("?", "？")):
                            reply_text = reply_text.rstrip(".!… ") + " " + closing_question

            except Exception as e:
                print("OpenAI error:", e, flush=True)

            if not reply_text:
                # Fallback sans question systématique
                reply_text = (
                    "Merci pour votre message 👋 Le gazon en rouleau offre une densité immédiate et fait gagner du temps par rapport au semis, "
                    "tout en demandant un entretien raisonnable (arrosage, tonte, 3 apports d’engrais/an)."
                )

            # --- Envoi WhatsApp + sortie webhook ---
            try:
                send_whatsapp_message(wa_id, reply_text)
                last_bot_at[wa_id] = datetime.utcnow()
                print(f"[followup] BOT replied to {wa_id} at {last_bot_at[wa_id].isoformat()}", flush=True)
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
