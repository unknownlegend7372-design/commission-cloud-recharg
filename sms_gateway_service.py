import os
import re
from datetime import datetime
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

SMS_GATEWAY_KEY = os.getenv("SMS_GATEWAY_KEY", "change_this_secret_key")
PAYMENT_GATEWAY_WEBHOOK_URL = os.getenv("PAYMENT_GATEWAY_WEBHOOK_URL", "")
PAYMENT_GATEWAY_API_KEY = os.getenv("PAYMENT_GATEWAY_API_KEY", "")

RECEIVED_SMS = []


def is_credit_sms(message_text: str) -> bool:
    text = str(message_text or "").lower()
    debit_keywords = ("debited", "dr.", "dr ", "paid to", "sent to", "spent", "withdrawn")
    credit_keywords = ("received", "credited", "cr.", "cr ", "deposited")

    if any(word in text for word in debit_keywords) and not any(word in text for word in credit_keywords):
        return False
    return any(word in text for word in credit_keywords)


def extract_payment_details(message_text: str, payload: dict):
    text = str(message_text or "")

    utr_match = re.search(r"\b(\d{12})\b", text)
    utr = utr_match.group(1) if utr_match else (
        str(payload.get("utr")).strip() if payload.get("utr") else None
    )

    amt_match = re.search(
        r"(?:Rs\.?|INR|₹)\s*(\d+(?:\.\d{1,2})?)", text, re.IGNORECASE
    )
    amount = None
    if amt_match:
        amount = float(amt_match.group(1))
    elif payload.get("amount") not in (None, ""):
        amount = float(payload.get("amount"))

    order_id = payload.get("orderId") or payload.get("order_id")
    return {
        "orderId": order_id,
        "utr": utr,
        "amount": amount,
        "message": text,
    }


def forward_to_payment_gateway(data: dict):
    if not PAYMENT_GATEWAY_WEBHOOK_URL:
        return {"forwarded": False, "reason": "PAYMENT_GATEWAY_WEBHOOK_URL not set"}

    headers = {"Content-Type": "application/json"}
    if PAYMENT_GATEWAY_API_KEY:
        headers["X-API-KEY"] = PAYMENT_GATEWAY_API_KEY
        headers["X-SMS-Gateway-Key"] = PAYMENT_GATEWAY_API_KEY
        headers["X-SMS-KEY"] = PAYMENT_GATEWAY_API_KEY

    try:
        resp = requests.post(
            PAYMENT_GATEWAY_WEBHOOK_URL,
            json=data,
            headers=headers,
            timeout=10,
        )
        return {
            "forwarded": True,
            "status_code": resp.status_code,
            "response": resp.text[:300],
        }
    except Exception as exc:
        return {"forwarded": False, "error": str(exc)}


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "sms-gateway"}), 200


@app.route("/recharge/sms-trigger", methods=["POST"])
def sms_trigger():
    json_data = request.get_json(silent=True) or {}
    form_data = request.form.to_dict() or {}
    payload = {**form_data, **json_data}

    provided_key = (
        request.headers.get("X-SMS-Gateway-Key")
        or request.headers.get("X-SMS-KEY")
        or payload.get("key")
        or ""
    ).strip()

    if provided_key != SMS_GATEWAY_KEY:
        return jsonify({"ok": False, "error": "Unauthorized SMS key"}), 401

    raw_message = payload.get("message") or payload.get("sms") or payload.get("text") or ""

    if not is_credit_sms(raw_message):
        return jsonify({
            "ok": True,
            "ignored": True,
            "reason": "Only credit/received payment SMS are processed"
        }), 200

    parsed = extract_payment_details(raw_message, payload)

    record = {
        "receivedAt": datetime.utcnow().isoformat() + "Z",
        "orderId": parsed["orderId"],
        "utr": parsed["utr"],
        "amount": parsed["amount"],
        "message": parsed["message"],
    }

    gateway_result = forward_to_payment_gateway(record)
    record["gatewayForward"] = gateway_result
    RECEIVED_SMS.append(record)

    return jsonify(
        {
            "ok": True,
            "orderId": parsed["orderId"],
            "utr": parsed["utr"],
            "amount": parsed["amount"],
            "gatewayForward": gateway_result,
        }
    ), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
