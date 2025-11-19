from flask import Flask, request, jsonify
import hmac
import hashlib
import requests
import os
import json
import re

# ------------------------------
# Chargement variables Heroku
# ------------------------------
INTERCOM_CLIENT_SECRET = os.getenv("INTERCOM_CLIENT_SECRET")
FRESHDESK_DOMAIN = os.getenv("FRESHDESK_DOMAIN")
FRESHDESK_API_KEY = os.getenv("FRESHDESK_API_KEY")
DEFAULT_PRIORITY = int(os.getenv("DEFAULT_PRIORITY", 2))
ASSIGN_GROUP_ID = os.getenv("ASSIGN_GROUP_ID")
VIP_KEYWORDS = os.getenv("VIP_KEYWORDS", "VIP,⭐⭐VIP ⭐⭐").split(",")
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
    if not signature_header:
        return False
    if signature_header.startswith("sha1="):
        received_sig = signature_header.split("sha1=")[1]
    else:
        received_sig = signature_header
    computed_sig = hmac.new(
        INTERCOM_CLIENT_SECRET.encode(), raw_body, hashlib.sha1
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
# Ajout d'un tag VIP si absent (contact ou ticket)
# ------------------------------
def add_vip_tag_if_missing(entity_type, entity_id, existing_tags):
    if VIP_TAG not in existing_tags:
        status, data = freshdesk_request(
            f"/{entity_type}/{entity_id}",
            method="PATCH",
            data={"tags": existing_tags + [VIP_TAG]}
        )
        if status in (200, 201):
            print(f"🏷 VIP tag ajouté sur {entity_type[:-1]} #{entity_id}")
        else:
            print(f"❌ Échec ajout VIP tag sur {entity_type[:-1]} #{entity_id}: {data}")

# ------------------------------
# Mettre à jour tous les tickets d'un contact avec VIP
# ------------------------------
def update_contact_tickets_with_vip(contact_id):
    status, tickets = freshdesk_request(f"/tickets?requester_id={contact_id}")
    if status != 200 or not isinstance(tickets, list):
        print(f"❌ Impossible de récupérer les tickets pour le contact #{contact_id}")
        return
    for ticket in tickets:
        ticket_tags = ticket.get("tags", [])
        if VIP_TAG not in ticket_tags:
            update_data = {"tags": ticket_tags + [VIP_TAG]}
            freshdesk_request(f"/tickets/{ticket['id']}", "PATCH", update_data)
            print(f"✅ Ticket #{ticket['id']} mis à jour avec VIP")

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

    payload = request.json or {}
    topic = payload.get("topic")
    if topic != "contact.user.tag.created":
        print(f"ℹ️ Événement ignoré : {topic}")
        return jsonify({"ignored": "not contact.user.tag.created"})

    item = payload.get("data", {}).get("item", {})
    tag_name = item.get("tag", {}).get("name", "")
    tag_clean = re.sub(r"[^a-zA-Z0-9]", "", tag_name).lower()
    if "vip" not in tag_clean:
        print(f"➡️ Tag non VIP ({tag_name}), ignoré.")
        return jsonify({"ignored": "not VIP"})

    contact = item.get("contact", {})
    email = contact.get("email")
    name = contact.get("name", email)
    if not email:
        return jsonify({"error": "no email"}), 400

    # Récupère ou crée contact Freshdesk
    status, data = freshdesk_request(f"/contacts?email={email}")
    if status == 200 and isinstance(data, list) and data:
        contact_fd = data[0]
        print("📇 Contact Freshdesk trouvé")
    else:
        status, data = freshdesk_request("/contacts", "POST", {"email": email, "name": name})
        if status not in (200, 201):
            print("❌ Impossible de créer le contact Freshdesk")
            return jsonify({"error": "cannot create contact", "details": data})
        contact_fd = data
        print("📇 Contact Freshdesk créé")

    contact_id = contact_fd.get("id")
    existing_tags = contact_fd.get("tags", [])

    # Ajouter le tag VIP au contact si absent
    add_vip_tag_if_missing("contacts", contact_id, existing_tags)

    # Mettre à jour tous les tickets existants du contact
    update_contact_tickets_with_vip(contact_id)

    return jsonify({"success": True, "email": email})

# ------------------------------
# Webhook Freshdesk pour tickets créés
# ------------------------------
@app.route("/freshdesk-webhook", methods=["POST"])
def freshdesk_webhook():
    payload = request.json or {}
    ticket = payload.get("ticket") or payload
    ticket_id = ticket.get("id")
    requester_id = ticket.get("requester_id")
    if not ticket_id or not requester_id:
        return jsonify({"error": "no ticket or requester"}), 400

    # Récupération du contact
    status, contact_fd = freshdesk_request(f"/contacts/{requester_id}")
    if status != 200:
        print(f"❌ Impossible de récupérer le contact #{requester_id}")
        return jsonify({"error": "cannot fetch contact"}), 400

    contact_tags = contact_fd.get("tags", [])
    if VIP_TAG in contact_tags:
        # Ajouter VIP au ticket si absent
        ticket_tags = ticket.get("tags", [])
        add_vip_tag_if_missing("tickets", ticket_id, ticket_tags)

    return jsonify({"success": True})

# ------------------------------
# Serveur local / Heroku
# ------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
