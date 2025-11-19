from flask import Flask, request, jsonify
import hmac, hashlib, requests, os, json

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
# Route racine pour test serveur
# ------------------------------
@app.route("/", methods=["GET"])
def home():
    return "✅ Webhook Intercom/Freshdesk is running 🚀", 200

# ------------------------------
# Vérification HMAC Intercom
# ------------------------------
def verify_signature(raw_body, signature_header):
    # Cas où Intercom n’envoie PAS de signature (ex : Test Webhook)
    if not signature_header:
        return False

    # Support du préfixe sha1=
    if signature_header.startswith("sha1="):
        received_sig = signature_header.split("sha1=")[1]
    else:
        received_sig = signature_header

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

    if not verify_signature(raw, signature):
        if signature is None:
            print("⚠️ Test Webhook Intercom reçu (pas signé)")
            return jsonify({"warning": "Unsigned Intercom test webhook"}), 200
        print("❌ Signature Intercom invalide")
        return "Invalid signature", 401

    print("✅ Webhook Intercom authentifié")
    payload = request.json or {}
    print("📦 Payload reçu :", json.dumps(payload, indent=2, ensure_ascii=False))

    # ------------------------------
    # Nouveau format Intercom VIP tag
    # ------------------------------
    topic = payload.get("topic")
    if topic != "contact.user.tag.created":
        print(f"ℹ️ Événement ignoré : {topic}")
        return jsonify({"ignored": "not contact.user.tag.created"})

    item = payload.get("data", {}).get("item", {})
    tag_name = item.get("tag", {}).get("name", "")
    import re
    tag_clean = re.sub(r"[^a-zA-Z0-9]", "", tag_name).lower()
    if "vip" not in tag_clean:
        print(f"➡️ Tag non VIP ({tag_name}), ignoré.")
        return jsonify({"ignored": "not VIP"})
    
    contact = item.get("contact", {})
    email = contact.get("email")
    name = contact.get("name", email)

    if not email:
        return jsonify({"error": "no email"}), 400

    print(f"🔥 Tag VIP détecté pour : {email}")

    # ------------------------------
    # Récupère ou crée contact Freshdesk
    # ------------------------------
    status, data = freshdesk_request(f"/contacts?email={email}")

    if status == 200 and isinstance(data, list) and data:
        contact_fd = data[0]
        print("📇 Contact Freshdesk trouvé")
    else:
        print("📇 Contact Freshdesk introuvable → création")
        status, data = freshdesk_request("/contacts", "POST", {"email": email, "name": name})
        if status not in (200, 201):
            print("❌ Impossible de créer le contact Freshdesk")
            return jsonify({"error": "cannot create contact", "details": data})
        contact_fd = data

    contact_id = contact_fd.get("id")

    # ------------------------------
    # Ajout tag VIP sur contact
    # ------------------------------
    existing_tags = contact_fd.get("tags", [])
    if VIP_TAG not in existing_tags:
        freshdesk_request(f"/contacts/{contact_id}", "PUT", {"tags": existing_tags + [VIP_TAG]})
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

            update_data = {"priority": DEFAULT_PRIORITY, "tags": ticket_tags}
            if ASSIGN_GROUP_ID:
                update_data["group_id"] = ASSIGN_GROUP_ID

            freshdesk_request(f"/tickets/{ticket['id']}", "PUT", update_data)
            print(f"✅ Ticket #{ticket['id']} mis à jour avec priorité VIP")

    return jsonify({"success": True, "email": email})


# ------------------------------
# Serveur local / Heroku
# ------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
