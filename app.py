from flask import Flask, request, jsonify
import hmac, hashlib, json, requests
import os

# On recrée config.py à partir des variables Heroku
with open("config.py", "w") as f:
    f.write(os.getenv("CONFIG_CONTENT", ""))

import config

# Tag VIP unifié
VIP_TAG = "⭐⭐VIP ⭐⭐"

app = Flask(__name__)

# Page d'accueil Heroku
@app.route('/')
def home():
    return "Webhook Intercom/Freshdesk is running 🚀"


# Vérification de signature HMAC Intercom
def verify_signature(raw_body, signature_header):
    if not signature_header or not signature_header.startswith("sha1="):
        return False

    signature = signature_header.split("sha1=")[1]

    computed = hmac.new(
        config.INTERCOM_CLIENT_SECRET.encode(),
        raw_body,
        hashlib.sha1
    ).hexdigest()

    return computed == signature


# Wrapper Freshdesk API
def freshdesk_request(path, method="GET", data=None):
    url = f"https://{config.FRESHDESK_DOMAIN}/api/v2{path}"
    headers = {"Content-Type": "application/json"}
    auth = (config.FRESHDESK_API_KEY, "X")

    response = requests.request(method, url, headers=headers, json=data, auth=auth)

    try:
        return response.status_code, response.json()
    except:
        return response.status_code, response.text


# Route Webhook Intercom
@app.route("/intercom-webhook", methods=["POST"])
def intercom_webhook():
    raw = request.get_data()
    signature = request.headers.get("X-Hub-Signature")

    # Vérification signature HMAC
    # if not verify_signature(raw, signature):
       #  return "Invalid signature", 401

    payload = request.json or {}

    # On ne traite que les événements de tag sur un utilisateur
    if payload.get("type") != "user_tag":
        return jsonify({"ignored": "not user_tag"})

    # Vérifie si le tag correspond à du VIP
    tag_name = payload.get("tag", {}).get("name", "").lower()

    if not any(keyword.lower() in tag_name for keyword in config.VIP_KEYWORDS):
        return jsonify({"ignored": "not VIP"})

    # Récupération des infos utilisateur
    user = payload.get("user", {})
    email = user.get("email")
    name = user.get("name", email)

    if not email:
        return jsonify({"error": "no email"}), 400

    # Cherche contact Freshdesk
    status, data = freshdesk_request(f"/contacts?email={email}")

    if status == 200 and isinstance(data, list) and data:
        contact = data[0]
    else:
        # Crée contact si inexistant
        status, data = freshdesk_request("/contacts", "POST", {"email": email, "name": name})
        if status not in (200, 201):
            return jsonify({"error": "cannot create contact", "details": data})
        contact = data

    contact_id = contact.get("id")
    existing_tags = contact.get("tags", [])

    # Ajoute tag VIP au contact
    if VIP_TAG not in existing_tags:
        freshdesk_request(
            f"/contacts/{contact_id}",
            "PUT",
            {"tags": existing_tags + [VIP_TAG]}
        )

    # Récupère tous les tickets Freshdesk du contact
    status, tickets = freshdesk_request(f"/tickets?requester_id={contact_id}")

    if status == 200 and isinstance(tickets, list):
        for ticket in tickets:

            ticket_tags = ticket.get("tags", [])

            if VIP_TAG not in ticket_tags:
                ticket_tags.append(VIP_TAG)

            update_data = {
                "priority": config.DEFAULT_PRIORITY,
                "tags": ticket_tags
            }

            if config.ASSIGN_GROUP_ID:
                update_data["group_id"] = config.ASSIGN_GROUP_ID

            # Met à jour chaque ticket
            freshdesk_request(
                f"/tickets/{ticket['id']}",
                "PUT",
                update_data
            )

    return jsonify({"success": True, "email": email})


if __name__ == "__main__":
    app.run()
