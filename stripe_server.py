import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import stripe
from datetime import datetime

app = Flask(__name__)

# ✅ Configuración CORS completa y explícita
CORS(app, 
     origins=[
         "https://nexuscopier.com",
         "https://www.nexuscopier.com",
         "http://localhost:5500",  # Para desarrollo local
         "http://127.0.0.1:5500"
     ],
     methods=["GET", "POST", "OPTIONS"],
     allow_headers=["Content-Type", "Accept"],
     supports_credentials=False,
     max_age=86400
)

# 🔒 Cargar claves desde variables de entorno (Render las inyecta)
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
DOMAIN = os.getenv("DOMAIN", "https://nexuscopier.com")

PRICE_MAP = {
    "1": "price_1ST2Cg2KiTHorHsUc2nE0SEh",  # Starter 1 cuenta - 59$
    "2": "price_1ST2Da2KiTHorHsUpkfJ0BJn",  # Pro 2 cuentas - 89$
    "3": "price_1ST2E32KiTHorHsULapHHGRG",  # Business 3 cuentas - 149$
}

PLAN_NAMES = {
    "1": {"en": "Nexus Copier - Starter (1 Account)", "es": "Nexus Copier - Starter (1 Cuenta)"},
    "2": {"en": "Nexus Copier - Pro (2 Accounts)", "es": "Nexus Copier - Pro (2 Cuentas)"},
    "3": {"en": "Nexus Copier - Business (3 Accounts)", "es": "Nexus Copier - Business (3 Cuentas)"},
}

# ============================================
# HEALTH CHECK
# ============================================
@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "status": "ok",
        "service": "Nexus Stripe Server",
        "version": "2.0",
        "features": ["checkout", "referrals", "webhooks"]
    })


# ============================================
# CREATE CHECKOUT SESSION (CON REFERIDOS)
# ============================================
@app.route("/create-checkout-session", methods=["POST", "OPTIONS"])
def create_checkout_session():
    # Manejar preflight request
    if request.method == "OPTIONS":
        return "", 200
    
    data = request.get_json() or {}
    plan = data.get("plan")
    lang = (data.get("lang") or "").lower()
    referral_code = data.get("referral_code")  # 🆕 Capturar código de referido

    # Aseguramos que el idioma sea correcto
    if lang not in ["es", "en"]:
        lang = "en"

    if plan not in PRICE_MAP:
        return jsonify({"error": "Plan inválido"}), 400

    # Definir prefijo de idioma
    lang_prefix = "/es" if lang == "es" else ""

    try:
        # 📊 Construir metadatos con código de referido
        metadata = {
            "plan": plan,
            "plan_name": PLAN_NAMES.get(plan, {}).get(lang, f"Plan {plan}"),
            "lang": lang,
            "source": "website"
        }
        
        # 🤝 Añadir código de referido si existe
        if referral_code and referral_code.strip():
            clean_code = referral_code.strip().upper()
            metadata["referral_code"] = clean_code
            metadata["is_referral"] = "true"
            print(f"🤝 Código de referido capturado: {clean_code}")
        
        print(f"📦 Petición recibida — Plan: {plan}, Idioma: {lang}, Referral: {referral_code or 'ninguno'}")
        
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": PRICE_MAP[plan], "quantity": 1}],
            
            # ✅ URLs según idioma
            success_url=f"{DOMAIN}{lang_prefix}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{DOMAIN}{lang_prefix}/cancel.html",
            
            # ✅ Mostrar Stripe en el idioma correcto
            locale="es" if lang == "es" else "en",
            
            allow_promotion_codes=True,
            payment_method_types=[
                "card", "paypal", "revolut_pay", "amazon_pay", "naver_pay",
                "link", "payco", "bancontact", "blik", "eps", "klarna"
            ],
            automatic_tax={"enabled": False},
            
            # 📊 Metadatos con código de referido
            metadata=metadata,
            
            # 🆕 Mensaje personalizado en checkout
            custom_text={
                "submit": {
                    "message": "Tu licencia será enviada por email en unos minutos." if lang == "es" else "Your license will be sent via email within minutes."
                }
            }
        )

        print(f"🌐 Sesión Stripe creada | ID: {session.id} | Referral: {metadata.get('referral_code', 'N/A')}")
        return jsonify({"sessionId": session.id})

    except Exception as e:
        print(f"❌ Error creando sesión Stripe: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ============================================
# WEBHOOK HANDLER (CON TRACKING DE REFERIDOS Y CUPONES)
# ============================================
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception as e:
        print(f"⚠️ Error verificando webhook: {str(e)}")
        return str(e), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        handle_successful_payment(session)

    return "", 200


def handle_successful_payment(session):
    """
    Procesa un pago exitoso y detecta códigos de afiliado de dos fuentes:
    1. metadata.referral_code (capturado via URL ?ref=CODE)
    2. Cupón de Stripe usado en checkout (creado para cada afiliado)
    """
    metadata = session.get("metadata") or {}
    
    plan = metadata.get("plan")
    plan_name = metadata.get("plan_name", f"Plan {plan}")
    email = session.get("customer_details", {}).get("email")
    amount = session.get("amount_total", 0) / 100
    currency = session.get("currency", "usd").upper()
    
    # Fuente 1: Código de referido via URL (metadata)
    referral_code = metadata.get("referral_code")
    
    # Fuente 2: Cupón de Stripe usado en checkout
    coupon_code = None
    discount_info = session.get("total_details", {}).get("breakdown", {}).get("discounts", [])
    
    # También intentar obtener el cupón del campo discount
    if session.get("discounts"):
        try:
            # Obtener información del descuento aplicado
            for discount_id in session.get("discounts", []):
                discount = stripe.Discount.retrieve(discount_id)
                if discount and discount.coupon:
                    coupon_code = discount.coupon.name or discount.coupon.id
                    break
        except Exception as e:
            print(f"⚠️ Error obteniendo info de cupón: {e}")
    
    # Intentar obtener de la sesión expandida si no lo tenemos
    if not coupon_code:
        try:
            # Obtener la sesión con información expandida de descuentos
            expanded_session = stripe.checkout.Session.retrieve(
                session.id,
                expand=['total_details.breakdown.discounts.discount.coupon']
            )
            discounts = expanded_session.get("total_details", {}).get("breakdown", {}).get("discounts", [])
            for d in discounts:
                if d.get("discount", {}).get("coupon", {}).get("name"):
                    coupon_code = d["discount"]["coupon"]["name"]
                    break
                elif d.get("discount", {}).get("coupon", {}).get("id"):
                    coupon_code = d["discount"]["coupon"]["id"]
                    break
        except Exception as e:
            print(f"⚠️ Error expandiendo sesión: {e}")
    
    print(f"✅ Pago completado: {email} compró {plan_name} por ${amount} {currency}")
    
    # Determinar el código de afiliado (prioridad: metadata > cupón)
    affiliate_code = referral_code or coupon_code
    affiliate_source = None
    
    if referral_code:
        affiliate_source = "url_referral"
    elif coupon_code:
        affiliate_source = "stripe_coupon"
    
    if affiliate_code:
        print(f"🎉 ¡VENTA DE AFILIADO!")
        print(f"   Código: {affiliate_code}")
        print(f"   Fuente: {affiliate_source}")
        print(f"   Email cliente: {email}")
        print(f"   Plan: {plan_name}")
        print(f"   Monto: ${amount} {currency}")
        print(f"   Fecha: {datetime.now().isoformat()}")
        
        # Aquí podrías:
        # 1. Guardar en base de datos
        # 2. Enviar notificación por email
        # 3. Llamar a Google Sheets API
        
    else:
        print(f"ℹ️ Venta sin código de afiliado")



# ============================================
# 🆕 VER VENTAS DE AFILIADOS (ADMIN)
# ============================================
@app.route("/referral-sales", methods=["GET"])
def get_referral_sales():
    """
    Endpoint para ver las ventas realizadas con códigos de referido.
    Útil para calcular comisiones de afiliados.
    
    Uso: GET /referral-sales?limit=50
    """
    try:
        limit = min(int(request.args.get("limit", 100)), 100)
        
        # Obtener sesiones de checkout recientes
        sessions = stripe.checkout.Session.list(limit=limit)
        
        referral_sales = []
        affiliates = {}
        
        for session in sessions.data:
            # Solo sesiones pagadas con código de referido
            if session.payment_status == "paid" and session.metadata:
                referral_code = session.metadata.get("referral_code")
                if referral_code:
                    sale = {
                        "session_id": session.id,
                        "date": datetime.fromtimestamp(session.created).isoformat(),
                        "referral_code": referral_code,
                        "customer_email": session.customer_details.email if session.customer_details else "N/A",
                        "amount": session.amount_total / 100,
                        "currency": session.currency.upper(),
                        "plan": session.metadata.get("plan_name", session.metadata.get("plan", "Unknown"))
                    }
                    referral_sales.append(sale)
                    
                    # Agrupar por código de afiliado
                    if referral_code not in affiliates:
                        affiliates[referral_code] = {
                            "code": referral_code,
                            "total_sales": 0,
                            "total_amount": 0,
                            "sales": []
                        }
                    affiliates[referral_code]["total_sales"] += 1
                    affiliates[referral_code]["total_amount"] += sale["amount"]
                    affiliates[referral_code]["sales"].append(sale)
        
        return jsonify({
            "total_referral_sales": len(referral_sales),
            "affiliates": list(affiliates.values())
        })
        
    except Exception as e:
        print(f"❌ Error obteniendo ventas de referidos: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ============================================
# 🆕 VER DETALLES DE UNA SESIÓN
# ============================================
@app.route("/session/<session_id>", methods=["GET"])
def get_session(session_id):
    """
    Obtener detalles de una sesión específica.
    Útil para debugging y verificación.
    """
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        return jsonify({
            "id": session.id,
            "status": session.status,
            "payment_status": session.payment_status,
            "customer_email": session.customer_details.email if session.customer_details else None,
            "amount": session.amount_total / 100 if session.amount_total else 0,
            "currency": session.currency,
            "metadata": dict(session.metadata) if session.metadata else {},
            "created": datetime.fromtimestamp(session.created).isoformat()
        })
    except stripe.error.InvalidRequestError:
        return jsonify({"error": "Session not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(port=4242, debug=False)
