import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

SMS_GATEWAY_KEY = os.getenv("SMS_GATEWAY_KEY", "change_this_secret_key").strip()
PAYMENT_GATEWAY_WEBHOOK_URL = os.getenv("PAYMENT_GATEWAY_WEBHOOK_URL", "").strip()
PAYMENT_GATEWAY_API_KEY = os.getenv("PAYMENT_GATEWAY_API_KEY", "").strip() or SMS_GATEWAY_KEY

RECEIVED_SMS = []

DEBIT_KEYWORDS = (
    "debited", "debit", "dr.", "dr ", "paid to", "sent to",
    "spent", "withdrawn", "purchase", "नामे", "निकासी", "निकाले", "खर्च"
)
CREDIT_KEYWORDS = (
    "received", "credited", "cr.", "cr ", "deposited", "प्राप्त", "जमा"
)


def is_credit_sms(message_text: str) -> bool:
    text = str(message_text or "").strip().lower()
    if not text:
        return True
    has_debit = any(word in text for word in DEBIT_KEYWORDS)
    has_credit = any(word in text for word in CREDIT_KEYWORDS)
    if has_debit and not has_credit:
        return False
    return has_credit


def extract_payment_details(message_text: str, payload: dict):
    text = str(message_text or "").strip()

    utr_match = re.search(
        r"(?:utr(?:\s*no)?|txn\s*id|transaction\s*id|upi\s*ref(?:\s*no)?|ref(?:\s*no)?|rrn)[:\s#=-]*([A-Za-z0-9_-]{8,25})",
        text,
        re.IGNORECASE,
    )
    if utr_match:
        utr = utr_match.group(1).strip().upper()
    else:
        utr_12 = re.search(r"\b(\d{12})\b", text)
        raw_payload_utr = (
            payload.get("utr")
            or payload.get("txn_id")
            or payload.get("transaction_id")
            or ""
        )
        utr = (
            utr_12.group(1).strip().upper()
            if utr_12
            else (str(raw_payload_utr).strip().upper() or None)
        )

    amount = None
    amt_patterns = [
        r"(?:credited|received|paid|sent|प्राप्त|जमा)\s*(?:with|by|of|हुए|हुआ)?\s*(?:rs\.?|inr|₹|रु\.?|रुपये|रुपए)?\s*([0-9]+(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?)",
        r"(?:rs\.?|inr|₹|रु\.?|रुपये|रुपए)\s*([0-9]+(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?)\s*(?:is\s*)?(?:credited|received|paid|sent|प्राप्त|जमा)",
        r"(?:rs\.?|inr|₹|रु\.?|रुपये|रुपए)\s*([0-9]+(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?)",
        r"([0-9]+(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?)\s*(?:rs\.?|inr|₹|रु\.?|रुपये|रुपए)",
    ]
    for pat in amt_patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            ctx = text[max(0, m.start() - 30): min(len(text), m.end() + 30)].lower()
            if any(skip in ctx for skip in ("avl bal", "avail bal", "available bal", "balance", "cashback", "refund")):
                continue
            try:
                val = float(m.group(1).replace(",", "").strip())
                if val > 0:
                    amount = round(val, 2)
                    break
            except ValueError:
                continue
        if amount is not None:
            break

    if amount is None and payload.get("amount") not in (None, ""):
        try:
            val = float(str(payload.get("amount")).replace(",", "").strip())
            if val > 0:
                amount = round(val, 2)
        except ValueError:
            amount = None

    order_id = (
        str(
            payload.get("order_id")
            or payload.get("orderId")
            or payload.get("local_order_id")
            or ""
        ).strip()
        or None
    )
    user_id = str(payload.get("user_id") or "").strip() or None

    return {
        "orderId": order_id,
        "order_id": order_id,
        "user_id": user_id,
        "utr": utr,
        "txn_id": utr,
        "amount": amount,
        "message": text,
    }


def _is_self_loop_url(target_url: str, incoming_base_url: str) -> bool:
    t = urlparse(target_url.strip())
    i = urlparse(incoming_base_url.strip())
    same_host = (t.netloc or "").lower() == (i.netloc or "").lower()
    same_path = (t.path or "").rstrip("/") == "/recharge/sms-trigger"
    return same_host and same_path


def forward_to_payment_gateway(data: dict, incoming_base_url: str = ""):
    target_url = PAYMENT_GATEWAY_WEBHOOK_URL.strip()
    if not target_url:
        return {"forwarded": False, "reason": "PAYMENT_GATEWAY_WEBHOOK_URL not set"}

    if incoming_base_url and _is_self_loop_url(target_url, incoming_base_url):
        return {
            "forwarded": False,
            "reason": "PAYMENT_GATEWAY_WEBHOOK_URL points to /recharge/sms-trigger loop",
        }

    headers = {"Content-Type": "application/json"}
    api_key = (PAYMENT_GATEWAY_API_KEY or SMS_GATEWAY_KEY).strip()
    if api_key:
        headers["X-API-KEY"] = api_key
        headers["X-SMS-Gateway-Key"] = api_key
        headers["X-SMS-KEY"] = api_key

    try:
        resp = requests.post(
            target_url,
            json=data,
            headers=headers,
            timeout=15,
        )
        return {
            "forwarded": True,
            "status_code": resp.status_code,
            "response": resp.text[:400],
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
        or request.headers.get("X-API-KEY")
        or payload.get("key")
        or ""
    ).strip()

    if not SMS_GATEWAY_KEY or provided_key != SMS_GATEWAY_KEY:
        return jsonify({"ok": False, "error": "Unauthorized SMS key"}), 401

    raw_message = str(
        payload.get("message") or payload.get("sms") or payload.get("text") or ""
    ).strip()

    if raw_message and not is_credit_sms(raw_message):
        return jsonify({
            "ok": True,
            "ignored": True,
            "reason": "Only credit/received payment SMS are processed",
        }), 200

    parsed = extract_payment_details(raw_message, payload)

    record = {
        "receivedAt": datetime.now(timezone.utc).isoformat(),
        "orderId": parsed["orderId"],
        "order_id": parsed["order_id"],
        "user_id": parsed["user_id"],
        "utr": parsed["utr"],
        "txn_id": parsed["txn_id"],
        "amount": parsed["amount"],
        "message": parsed["message"],
    }

    gateway_result = forward_to_payment_gateway(record, incoming_base_url=request.host_url)
    record["gatewayForward"] = gateway_result
    RECEIVED_SMS.append(record)

    return jsonify({
        "ok": True,
        "orderId": parsed["orderId"],
        "utr": parsed["utr"],
        "amount": parsed["amount"],
        "gatewayForward": gateway_result,
    }), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
