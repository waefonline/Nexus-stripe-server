import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import stripe

app = Flask(__name__)
CORS(app, origins=[
    "https://nexuscopier.com",
    "https://www.nexuscopier.com",
])

# 🔑 Cargar claves desde variables de entorno (Render las inyecta)
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
DOMAIN = os.getenv("DOMAIN", "https://nexuscopier.com")

PRICE_MAP = {
    "1": "price_1SNINe2KiTHorHsUTyGDSAtS",  # Starter 1 cuenta - 59€
    "2": "price_1SNIO62KiTHorHsUdJbaUL1Q",  # Pro 2 cuentas - 89€
    "3": "price_1SNIPW2KiTHorHsULLp42FJE",  # Business 3 cuentas - 149€
}

@app.post("/create-checkout-session")
def create_checkout_session():
    data = request.get_json()
    plan = (data or {}).get("plan")
    if plan not in PRICE_MAP:
        return jsonify({"error": "Plan inválido"}), 400

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": PRICE_MAP[plan], "quantity": 1}],
            success_url=f"{DOMAIN}/success.html",
            cancel_url=f"{DOMAIN}/cancel.html",
            allow_promotion_codes=True,
            payment_method_types=["card", "paypal", "revolut_pay", "amazon_pay", "naver_pay", "link", "payco", "bancontact", "blik", "eps", "klarna"],  # 👈 Fuerza a mostrar tarjeta
            automatic_tax={"enabled": False},
            metadata={"plan": plan}
        )

        return jsonify({"sessionId": session.id})
    except Exception as e:
        # Es útil loguear el error real en el servidor para depuración
        print(f"Error creando sesión de Stripe: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.post("/webhook")
def webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception as e:
        return str(e), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        plan = (session.get("metadata") or {}).get("plan")
        email = session.get("customer_details", {}).get("email")
        print(f"✅ Pago completado: {email} compró el plan {plan}")

    return "", 200


if __name__ == "__main__":
    app.run(port=4242, debug=False)
