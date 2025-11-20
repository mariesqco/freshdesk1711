from flask import Flask, request, jsonify
import hmac, hashlib, requests, os, json, re
import logging
import time

# Activation du niveau DEBUG pour voir plus de détails
logging.basicConfig(level=logging.DEBUG)

# ------------------------------
# Chargement variables Heroku / environnement
# ------------------------------
INTERCOM_CLIENT_SECRET = os.getenv("INTERCOM_CLIENT_SECRET")
FRESHDESK_DOMAIN = os.getenv("FRESHDESK_DOMAIN")
FRESHDESK_API_KEY = os.getenv("FRESHDESK_API_KEY")
DEFAULT_PRIORITY = int(os.getenv("DEFAULT_PRIORITY", 2))
ASSIGN_GROUP_ID = os.getenv("ASSIGN_GROUP_ID")

VIP_TAG = "⭐⭐VIP ⭐⭐"

app = Flask(__name__)

# ------------------------------
# Gestion Rate Limit 20/minute pour contacts list
# ------------------------------
contacts_request_count = 0
contacts_request_time = time.time()

def rate_limit_contacts():
    global contacts_request_count, contacts_request_time
    current_time = time.time()
    # Si une minute s’est écoulée, on reset
    if current_time - contacts_request_time > 60:
        contacts_request_time = current_time
        contacts_request_count = 0

    # Si déjà 20 dans la minute, on attend que la minute soit écoulée
    if contacts_request_count >= 20:
        sleep_time = 60 - (current_time - contacts_request_time)
        if sleep_time > 0:
            logging.warning(f"Limite de 20 requêtes contacts/min atteinte, attente de {sleep_time:.2f} secondes")
            time.sleep(sleep_time)
        # reset après attente
        contacts_request_time = time.time()
        contacts_request_count = 0

    contacts_request_count += 1

# ------------------------------
# Vérification signature Intercom
# ------------------------------
def verify_signature(raw_body, signature_header):
    if not signature_header:
        return False
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
# Appels API Freshdesk avec gestion du rate limiting (429)
# ------------------------------
def freshdesk_request(path, method="GET", data=None, is_contact_list=False):
    if is_contact_list:
        rate_limit_contacts()
    url = f"https://{FRESHDESK_DOMAIN}/api/v2{path}"
    headers = {"Content-Type": "application/json"}
    auth = (FRESHDESK_API_KEY, "X")

    max_retries = 5
    retries = 0

    while True:
        response = requests.request(method, url, headers=headers, json=data, auth=auth)
        logging.debug(f"Request {method} {url} Status: {response.status_code}")

        if response.status_code == 429:
            retries += 1
            if retries > max_retries:
                logging.error("Trop de tentatives suite à des erreurs 429, abandon.")
                return response.status_code, None
            retry_after = int(response.headers.get("Retry-After", 5))
            logging.warning(f"429 Too Many Requests: attente {retry_after} secondes avant retry {retries}/{max_retries}")
            time.sleep(retry_after)
            continue

        try:
            response_data = response.json()
            logging.debug(f"Response JSON: {json.dumps(response_data, indent=2)}")
            return response.status_code, response_data
        except Exception as e:
            logging.error(f"Erreur JSON response : {e}")
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
            logging.warning("⚠️ Test Webhook Intercom reçu (pas signé)")
            return jsonify({"warning": "Unsigned Intercom test webhook"}), 200
        logging.error("❌ Signature Intercom invalide")
        return "Invalid signature", 401

    logging.info("✅ Webhook Intercom authentifié")
    payload = request.json or {}
    logging.info("📦 Payload reçu :\n%s", json.dumps(payload, indent=2, ensure_ascii=False))

    topic = payload.get("topic")
    if topic != "contact.user.tag.created":
        logging.info(f"ℹ️ Événement ignoré : {topic}")
        return jsonify({"ignored": "not contact.user.tag.created"})

    item = payload.get("data", {}).get("item", {})
    tag_name = item.get("tag", {}).get("name", "")
    tag_clean = re.sub(r"[^a-zA-Z0-9]", "", tag_name).lower()
    if "vip" not in tag_clean:
        logging.info(f"➡️ Tag non VIP ({tag_name}), ignoré.")
        return jsonify({"ignored": "not VIP"})

    contact = item.get("contact", {})
    email = contact.get("email")
    name = contact.get("name", email)

    if not email:
        return jsonify({"error": "no email"}), 400

    logging.info(f"🔥 Tag VIP détecté pour : {email}")

    # ------------------------------
    # Récupération / création du contact Freshdesk
    # ------------------------------
    status, data = freshdesk_request(f"/contacts?email={email}", is_contact_list=True)

    if status == 200 and isinstance(data, list) and data:
        contact_fd = data[0]
        logging.info("📇 Contact Freshdesk trouvé")
    else:
        logging.info("📇 Contact Freshdesk introuvable → création")
        creation_data = {
            "email": email,
            "name": name,
            "custom_fields": {
                "vip": VIP_TAG
            },
            "tags": [VIP_TAG]
        }
        status, data = freshdesk_request("/contacts", "POST", creation_data)
        if status not in (200, 201):
            logging.error("❌ Impossible de créer le contact Freshdesk: %s", data)
            return jsonify({"error": "cannot create contact", "details": data})
        contact_fd = data

    logging.debug("Données du contact Freshdesk :\n%s", json.dumps(contact_fd, indent=2))
    logging.debug("Champs personnalisés actuels :\n%s", json.dumps(contact_fd.get("custom_fields", {}), indent=2))

    contact_id = contact_fd.get("id")

    # ------------------------------
    # Mise à jour des tags + champ personnalisé
    # ------------------------------
    existing_tags = contact_fd.get("tags", [])
    new_tags = existing_tags.copy()

    if VIP_TAG not in new_tags:
        new_tags.append(VIP_TAG)
        logging.info("🏷 Tag VIP ajouté")

    update_data = {
        "tags": new_tags,
        "custom_fields": {
            "vip": VIP_TAG  # Champ "Infos client" (API name = vip)
        }
    }

    logging.debug("Données pour mise à jour du contact :\n%s", json.dumps(update_data, indent=2))

    update_status, update_response = freshdesk_request(
        f"/contacts/{contact_id}",
        "PUT",
        update_data
    )

    if update_status in (200, 201):
        logging.info("✨ Contact mis à jour avec tags + champ personnalisé VIP")
    else:
        logging.error("❌ Erreur lors de la mise à jour du contact : %s", update_response)

    # ------------------------------
    # Mise à jour des tickets Freshdesk (désactivée)
    # ------------------------------
    # status, tickets = freshdesk_request(f"/tickets?requester_id={contact_id}")
    #
    # if status == 200 and isinstance(tickets, list):
    #     logging.info(f"🎫 {len(tickets)} tickets à mettre à jour")
    #     for ticket in tickets:
    #         ticket_tags = ticket.get("tags", [])
    #         if VIP_TAG not in ticket_tags:
    #             ticket_tags.append(VIP_TAG)
    #
    #         update_ticket = {"priority": DEFAULT_PRIORITY, "tags": ticket_tags}
    #         if ASSIGN_GROUP_ID:
    #             update_ticket["group_id"] = ASSIGN_GROUP_ID
    #
    #         freshdesk_request(f"/tickets/{ticket['id']}", "PUT", update_ticket)
    #         logging.info(f"✅ Ticket #{ticket['id']} mis à jour")

    return jsonify({"success": True, "email": email})

# ------------------------------
# Serveur local / Heroku
# ------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
