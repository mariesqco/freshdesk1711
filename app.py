from flask import Flask, request, jsonify
import hmac, hashlib, requests, os, json, re

# ------------------------------
# Chargement variables Heroku / env
# ------------------------------
INTERCOM_CLIENT_SECRET = os.getenv("INTERCOM_CLIENT_SECRET")
FRESHDESK_DOMAIN = os.getenv("FRESHDESK_DOMAIN")
FRESHDESK_API_KEY = os.getenv("FRESHDESK_API_KEY")
DEFAULT_PRIORITY = int(os.getenv("DEFAULT_PRIORITY", 2))
ASSIGN_GROUP_ID = os.getenv("ASSIGN_GROUP_ID")
VIP_KEYWORDS = os.getenv("VIP_KEYWORDS", "VIP,⭐⭐VIP ⭐⭐").split(",")

# Tag VIP unifié
VIP_TAG = "⭐⭐VIP ⭐⭐"

# Nom interne du champ personnalisé Freshdesk (ou configure via env CUSTOM_FIELD_VIP)
CUSTOM_FIELD_VIP = os.getenv("CUSTOM_FIELD_VIP", "cf_vip_status")  # <-- remplace si besoin

app = Flask(__name__)

# ------------------------------
# Helpers
# ------------------------------
def log_print(*args, **kwargs):
    print(*args, **kwargs, flush=True)

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
        (INTERCOM_CLIENT_SECRET or "").encode(),
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

    try:
        response = requests.request(method, url, headers=headers, json=data, auth=auth, timeout=15)
    except Exception as e:
        log_print("❌ Erreur HTTP vers Freshdesk:", e)
        return None, {"error": str(e)}

    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = response.text

    return status, body

# ------------------------------
# Routes
# ------------------------------
@app.route("/", methods=["GET"])
def home():
    return "✅ Webhook Intercom/Freshdesk is running 🚀", 200

@app.route("/intercom-webhook", methods=["POST"])
def intercom_webhook():
    raw = request.get_data()
    signature = request.headers.get("X-Hub-Signature")

    if not verify_signature(raw, signature):
        if signature is None:
            log_print("⚠️ Test Webhook Intercom reçu (pas signé)")
            return jsonify({"warning": "Unsigned Intercom test webhook"}), 200
        log_print("❌ Signature Intercom invalide")
        return "Invalid signature", 401

    log_print("✅ Webhook Intercom authentifié")
    payload = request.json or {}
    log_print("📦 Payload reçu :", json.dumps(payload, indent=2, ensure_ascii=False))

    # Vérifie topic
    topic = payload.get("topic")
    if topic != "contact.user.tag.created":
        log_print(f"ℹ️ Événement ignoré : {topic}")
        return jsonify({"ignored": "not contact.user.tag.created"})

    item = payload.get("data", {}).get("item", {})
    tag_name = item.get("tag", {}).get("name", "")

    # Normalisation: ne garder que alphanumériques pour détecter "vip"
    tag_clean = re.sub(r"[^a-zA-Z0-9]", "", tag_name).lower()
    if "vip" not in tag_clean:
        log_print(f"➡️ Tag non VIP ({tag_name}), ignoré.")
        return jsonify({"ignored": "not VIP"})

    # Récupération du contact Intercom
    contact = item.get("contact", {})
    email = contact.get("email")
    name = contact.get("name", email)

    if not email:
        return jsonify({"error": "no email"}), 400

    log_print(f"🔥 Tag VIP détecté pour : {email}")

    # ------------------------------
    # Récupère ou crée contact Freshdesk
    # ------------------------------
    status, data = freshdesk_request(f"/contacts?email={email}")
    if status is None:
        return jsonify({"error": "freshdesk request failed", "details": data}), 500

    if status == 200 and isinstance(data, list) and data:
        contact_fd = data[0]
        log_print("📇 Contact Freshdesk trouvé (recherche par email)")
    else:
        log_print("📇 Contact Freshdesk introuvable → création")
        status_create, data_create = freshdesk_request("/contacts", "POST", {"email": email, "name": name})
        if status_create not in (200, 201):
            log_print("❌ Impossible de créer le contact Freshdesk:", status_create, data_create)
            return jsonify({"error": "cannot create contact", "details": data_create}), 500
        contact_fd = data_create
        log_print("✅ Contact Freshdesk créé:", contact_fd.get("id"))

    contact_id = contact_fd.get("id")
    if not contact_id:
        log_print("❌ Aucun id contact retourné par Freshdesk:", contact_fd)
        return jsonify({"error": "no_contact_id", "details": contact_fd}), 500

    # Récupérer la fiche la plus récente du contact (GET /contacts/{id})
    status_get, contact_latest = freshdesk_request(f"/contacts/{contact_id}")
    if status_get not in (200,):
        # si pas trouvé, on continue avec contact_fd mais on loggue l'erreur
        log_print(f"⚠️ Impossible de récupérer la fiche contact mise à jour ({status_get}):", contact_latest)
        contact_latest = contact_fd

    # ------------------------------
    # Prépare tags et custom_fields
    # ------------------------------
    # Normaliser tags : Freshdesk peut renvoyer string ou list
    existing_tags = contact_latest.get("tags", [])
    if isinstance(existing_tags, str):
        # chaîne de tags séparés par virgule -> transformer en liste propre
        existing_tags = [t.strip() for t in existing_tags.split(",") if t.strip()]

    if not isinstance(existing_tags, list):
        existing_tags = []

    if VIP_TAG not in existing_tags:
        existing_tags.append(VIP_TAG)
        log_print(f"🏷 On va ajouter le tag '{VIP_TAG}' au contact {contact_id}")
    else:
        log_print("ℹ️ Le tag VIP est déjà présent sur le contact.")

    # Custom fields : préserve les existants
    custom_fields = contact_latest.get("custom_fields", {}) or {}
    # Écrire la valeur VIP dans le champ personnalisé
    custom_fields[CUSTOM_FIELD_VIP] = VIP_TAG
    log_print(f"📝 Mise à jour du champ personnalisé '{CUSTOM_FIELD_VIP}' -> '{VIP_TAG}'")

    # ------------------------------
    # Mise à jour unique du contact (tags + custom_fields)
    # ------------------------------
    update_payload = {
        "tags": existing_tags,
        "custom_fields": custom_fields
    }

    status_update, body_update = freshdesk_request(f"/contacts/{contact_id}", "PUT", update_payload)
    if status_update not in (200, 201):
        log_print("❌ Échec mise à jour contact:", status_update, body_update)
        # on retourne quand même 500 mais continue pas
        return jsonify({"error": "failed_update_contact", "status": status_update, "details": body_update}), 500

    log_print("✅ Contact mis à jour (tags + custom_fields).")

    # ------------------------------
    # Mise à jour des tickets Freshdesk
    # ------------------------------
    status_t, tickets = freshdesk_request(f"/tickets?requester_id={contact_id}")
    if status_t == 200 and isinstance(tickets, list):
        log_print(f"🎫 {len(tickets)} tickets à mettre à jour")
        for ticket in tickets:
            ticket_tags = ticket.get("tags", [])
            if isinstance(ticket_tags, str):
                ticket_tags = [t.strip() for t in ticket_tags.split(",") if t.strip()]
            if VIP_TAG not in ticket_tags:
                ticket_tags.append(VIP_TAG)

            update_data = {"priority": DEFAULT_PRIORITY, "tags": ticket_tags}
            if ASSIGN_GROUP_ID:
                try:
                    update_data["group_id"] = int(ASSIGN_GROUP_ID)
                except:
                    update_data["group_id"] = ASSIGN_GROUP_ID

            st_up, res_up = freshdesk_request(f"/tickets/{ticket['id']}", "PUT", update_data)
            if st_up not in (200, 201):
                log_print(f"⚠️ Échec mise à jour ticket #{ticket['id']}:", st_up, res_up)
            else:
                log_print(f"✅ Ticket #{ticket['id']} mis à jour avec priorité VIP")
    else:
        log_print(f"ℹ️ Aucun ticket à mettre à jour ou erreur ({status_t}):", tickets)

    return jsonify({"success": True, "email": email, "contact_id": contact_id})

# ------------------------------
# Serveur local / Heroku
# ------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
