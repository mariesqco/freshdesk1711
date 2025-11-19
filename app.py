from flask import Flask, request, jsonify
import hmac, hashlib, requests, os

# ------------------------------
# Chargement variables Heroku
# ------------------------------

INTERCOM_CLIENT_SECRET = os.getenv("INTERCOM_CLIENT_SECRET")
FRESHDESK_DOMAIN = os.getenv("FRESHDESK_DOMAIN")
FRESHDESK_API_KEY = os.getenv("FRESHDESK_API_KEY")
DEFAULT_PRIORITY = int(os.getenv("DEFAULT_PRIORITY", 2))
ASSIGN_GROUP_ID = os.getenv("ASSIGN_GROUP_ID")
VIP_KEYWORDS = os.getenv("VIP_KEYWORDS", "VIP,⭐⭐VIP ⭐⭐").split(",")

# Tag VIP unifié
VIP_TAG = "⭐⭐VIP ⭐⭐"

app = Flask(__name__)


# ------------------------------
# Page d'accueil
# ------------------------------
@app.route("/")
def home():
    return "Webhook Intercom/Freshdesk is running 🚀"


# ------------------------------
# Vérification HMAC Intercom
# ------------------------------
def verify_signature(raw_body, signature_header):
    # Cas où Intercom n’envoie PAS de signature (ex : Test Webhook)
    if not signature_header:
        return False

    if not signature_header.startswith("sha1="):
        return False

    received_sig = signature_header.split("sha1=")[1]

    computed_sig = hmac.new(
        INTERCOM_CLIENT_SECRET.encode(),
        raw_body,
        hashlib.sha1
    ).hexdigest()

    return hmac.compare_digest(received_sig, computed_sig)


# ------------------------------
# Appels API Freshdesk
# ------------------------------
def freshdesk_request(path, method="GET", data=None):
    url = f"https://{FRESHDESK_DOMAIN}/api/v2{path}"
    headers = {"Content-Type": "application/json"}
    auth = (FRESHDESK_API_KEY, "X")

    response = requests.request(method, url, headers=headers, json=data, auth=auth)

    try:
        return response.status_code, response.json()
    except:
        return response.status_code, response.text


# ------------------------------
# Webhook Intercom
# ------------------------------
@app.route("/intercom-webhook", methods=["POST"])
def intercom_webhook():
    raw = request.get_data()
    signature = request.headers.get("X-Hub-Signature")

    # Vérification signature HMAC
    if not verify_signature(raw, signature):

        # Si la signature est absente → Test Intercom → OK
        if signature is None:
            print("⚠️  Test Webhook Intercom reçu (pas signé)")
            return jsonify({"warning": "Unsigned Intercom test webhook"}), 200

        # Signature présente mais mauvaise
        print("❌ Signature Intercom invalide")
        return "Invalid signature", 401

    print("✅ Webhook Intercom authentifié")

    payload = request.json or {}

    # On traite uniquement les événements de type "user_tag"
    if payload.get("type") != "user_tag":
        return jsonify({"ignored": "not user_tag"})

    # Vérifie si le tag correspond à un VIP
    tag_name = payload.get("tag", {}).get("name", "")
    if not any(keyword.lower() in tag_name.lower() for keyword in VIP_KEYWORDS):
        print("➡️ Tag non VIP, ignoré.")
        return jsonify({"ignored": "not VIP"})

    print(f"🔥 Tag VIP détecté : {tag_name}")

    # Récupération utilisateur
    user = payload.get("user", {})
    email = user.get("email")
    name = user.get("name", email)

    if not email:
        return jsonify({"error": "no email"}), 400

    print(f"👤 Utilisateur VIP : {email}")

    # ------------------------------
    # Récupère / crée contact Freshdesk
    # ------------------------------
    status, data = freshdesk_request(f"/contacts?email={email}")

    if status == 200 and isinstance(data, list) and data:
        contact = data[0]
        print("📇 Contact Freshdesk trouvé")
    else:
        print("📇 Contact Freshdesk introuvable → création")
        status, data = freshdesk_request("/contacts", "POST", {
            "email": email,
            "name": name
        })
        if status not in (200, 201):
            print("❌ Impossible de créer le contact Freshdesk")
            return jsonify({"error": "cannot create contact", "details": data})
        contact = data

    contact_id = contact.get("id")

    # ------------------------------
    # Ajout tag VIP sur contact
    # ------------------------------
    existing_tags = contact.get("tags", [])
    if VIP_TAG not in existing_tags:
        freshdesk_request(
            f"/contacts/{contact_id}",
            "PUT",
            {"tags": existing_tags + [VIP_TAG]}
        )
        print("🏷 Tag VIP ajouté au contact")

    # ------------------------------
    # Mise à jour des tickets Freshdesk
    # ------------------------------
    status, tickets = freshdesk_request(f"/tickets?requester_id={contact_id}")

    if status == 200 and isinstance(tickets, list):
        print(f"🎫 {len(tickets)} tickets à mettre à jour")
        for ticket in tickets:

            ticket_tags = ticket.get("tags", [])
            if VIP_TAG not in ticket_tags:
                ticket_tags.append(VIP_TAG)

            update_data = {
                "priority": DEFAULT_PRIORITY,
                "tags": ticket_tags
            }

            if ASSIGN_GROUP_ID:
                update_data["group_id"] = ASSIGN_GROUP_ID

            freshdesk_request(
                f"/tickets/{ticket['id']}",
                "PUT",
                update_data
            )
            print(f"✅ Ticket #{ticket['id']} mis à jour avec priorité VIP")

    return jsonify({"success": True, "email": email})


# ------------------------------
# Start local server
# ------------------------------
if __name__ == "__main__":
    app.run()
