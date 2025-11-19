from flask import Flask, request, jsonify
import hmac, hashlib, requests, os
import json

# ------------------------------
# Chargement variables Heroku
# ------------------------------

INTERCOM_CLIENT_SECRET = os.getenv("INTERCOM_CLIENT_SECRET")
FRESHDESK_DOMAIN = os.getenv("FRESHDESK_DOMAIN")
FRESHDESK_API_KEY = os.getenv("FRESHDESK_API_KEY")

# Tags VIP
VIP_KEYWORDS = os.getenv("VIP_KEYWORDS", "VIP,⭐⭐VIP ⭐⭐").split(",")
VIP_TAG = "⭐⭐VIP ⭐⭐"

app = Flask(__name__)


# ------------------------------
# Vérification de la signature Intercom
# ------------------------------

def verify_signature(req):
    received_sig = req.headers.get("X-Hub-Signature")
    if not received_sig:
        print("❌ Signature Intercom manquante")
        return False

    body = req.get_data()
    computed_sig = hmac.new(
        INTERCOM_CLIENT_SECRET.encode(),
        body,
        hashlib.sha1
    ).hexdigest()

    if not hmac.compare_digest(received_sig, computed_sig):
        print("❌ Signature Intercom invalide")
        return False

    return True


# ------------------------------
# Freshdesk – Récupérer l'ID du contact
# ------------------------------

def get_freshdesk_contact_id(email):
    url = f"https://{FRESHDESK_DOMAIN}.freshdesk.com/api/v2/contacts?email={email}"

    response = requests.get(
        url,
        auth=(FRESHDESK_API_KEY, "X")
    )

    if response.status_code != 200:
        print("❌ Erreur API Freshdesk (recherche contact) :", response.status_code, response.text)
        return None

    contacts = response.json()
    if not contacts:
        print("❌ Aucun contact trouvé dans Freshdesk pour :", email)
        return None

    return contacts[0]["id"]


# ------------------------------
# Freshdesk – Ajouter le tag VIP au contact
# ------------------------------

def update_freshdesk_contact_tags(contact_id):
    url = f"https://{FRESHDESK_DOMAIN}.freshdesk.com/api/v2/contacts/{contact_id}"

    data = {
        "tags": [VIP_TAG]
    }

    response = requests.put(
        url,
        auth=(FRESHDESK_API_KEY, "X"),
        headers={"Content-Type": "application/json"},
        data=json.dumps(data)
    )

    if response.status_code in (200, 201):
        print("✅ Tag VIP ajouté dans Freshdesk !")
    else:
        print("❌ Erreur lors de la mise à jour du contact Freshdesk :", response.status_code, response.text)


# ------------------------------
# Webhook Intercom
# ------------------------------

@app.route("/intercom-webhook", methods=["POST"])
def intercom_webhook():

    if not verify_signature(request):
        return jsonify({"error": "invalid_signature"}), 401

    print("✅ Webhook Intercom authentifié")

    payload = request.get_json()
    print("📦 Payload :", json.dumps(payload, indent=2, ensure_ascii=False))

    topic = payload.get("topic")
    item = payload.get("data", {}).get("item", {})

    # On filtre sur contact.user.tag.created
    if topic != "contact.user.tag.created":
        print("ℹ️ Événement ignoré :", topic)
        return jsonify({"status": "ignored"}), 200

    tag = item.get("tag", {}).get("name", "")
    contact = item.get("contact", {})

    print(f"🏷️ Tag reçu : {tag}")

    # Vérification VIP
    if tag not in VIP_KEYWORDS:
        print("⛔ Tag pas VIP → aucune action Freshdesk")
        return jsonify({"status": "ignored_non_vip"}), 200

    print("🌟 Tag VIP détecté → mise à jour Freshdesk...")

    email = contact.get("email")
    freshdesk_id = get_freshdesk_contact_id(email)

    if not freshdesk_id:
        return jsonify({"error": "contact_not_found_freshdesk"}), 404

    update_freshdesk_contact_tags(freshdesk_id)

    return jsonify({"status": "freshdesk_tag_updated"}), 200


# ------------------------------
# Serveur local
# ------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
