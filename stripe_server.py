import os
import requests
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

# 🆕 Google Apps Script URL para registro de licencias
GOOGLE_SCRIPT_URL = os.getenv(
    "GOOGLE_SCRIPT_URL", 
    "https://script.google.com/macros/s/AKfycbwbufRIrKswV4IBxJPi_dmQQijN9qcdhvx-Gp-tAK6BiZDfTaV3iBPToUyPV_FoiQPS/exec"
)
GOOGLE_SCRIPT_TOKEN = os.getenv("GOOGLE_SCRIPT_TOKEN", "nexus_secure_live_202X")

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

# Mapeo de precio a cantidad de licencias
LICENSES_BY_AMOUNT = [
    (14900, 3),  # Business $149 = 3 licencias
    (8900, 2),   # Pro $89 = 2 licencias
    (5900, 1),   # Starter $59 = 1 licencia
]

def get_licenses_quantity(amount_cents):
    """Determina la cantidad de licencias basándose en el monto pagado"""
    for threshold, qty in LICENSES_BY_AMOUNT:
        if amount_cents >= threshold:
            return qty
    return 1  # Default

# ============================================
# HEALTH CHECK
# ============================================
@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "status": "ok",
        "service": "Nexus Stripe Server",
        "version": "3.0",
        "features": ["checkout", "referrals", "webhooks", "license-registration"]
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
# WEBHOOK HANDLER (CON REGISTRO DE LICENCIAS)
# ============================================
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    # 1. Verificar firma del webhook
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError as e:
        print(f"⚠️ Error verificando firma webhook: {str(e)}")
        return "Invalid signature", 400
    except Exception as e:
        print(f"⚠️ Error procesando webhook: {str(e)}")
        return str(e), 400

    # 2. Procesar eventos
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        handle_successful_payment(session)
    else:
        print(f"ℹ️ Evento ignorado: {event['type']}")

    # 3. Siempre responder 200 OK a Stripe
    return "", 200


def handle_successful_payment(session):
    """
    Procesa un pago exitoso:
    1. Extrae información del cliente
    2. Detecta códigos de afiliado
    3. Llama al Google Apps Script para registrar la venta
    """
    session_id = session.get("id", "unknown")
    metadata = session.get("metadata") or {}
    
    plan = metadata.get("plan", "1")
    plan_name = metadata.get("plan_name", f"Plan {plan}")
    customer_details = session.get("customer_details") or {}
    email = customer_details.get("email")
    name = customer_details.get("name", "Customer")
    amount_cents = session.get("amount_total", 0)
    amount = amount_cents / 100
    currency = session.get("currency", "usd").upper()
    
    # Determinar cantidad de licencias
    licenses_qty = get_licenses_quantity(amount_cents)
    
    print(f"=" * 50)
    print(f"✅ PAGO COMPLETADO")
    print(f"   Session ID: {session_id}")
    print(f"   Email: {email}")
    print(f"   Nombre: {name}")
    print(f"   Plan: {plan_name}")
    print(f"   Monto: ${amount} {currency}")
    print(f"   Licencias: {licenses_qty}")
    
    # Detectar código de afiliado
    referral_code = metadata.get("referral_code")
    coupon_code = detect_coupon_code(session)
    affiliate_code = referral_code or coupon_code
    
    if affiliate_code:
        print(f"   🎉 Afiliado: {affiliate_code}")
    
    print(f"=" * 50)
    
    # 🆕 REGISTRAR VENTA EN GOOGLE APPS SCRIPT
    if email:
        success = register_sale_in_google_script(
            email=email,
            name=name,
            licenses_qty=licenses_qty,
            session_id=session_id,
            amount=amount,
            plan=plan_name,
            affiliate_code=affiliate_code
        )
        if success:
            print(f"✅ Venta registrada en Google Script correctamente")
        else:
            print(f"⚠️ Error registrando venta en Google Script (revisar logs)")
    else:
        print(f"⚠️ No se pudo registrar: email no disponible")


def detect_coupon_code(session):
    """Detecta si se usó un cupón de descuento"""
    coupon_code = None
    
    # Intentar desde discounts
    if session.get("discounts"):
        try:
            for discount_id in session.get("discounts", []):
                discount = stripe.Discount.retrieve(discount_id)
                if discount and discount.coupon:
                    coupon_code = discount.coupon.name or discount.coupon.id
                    break
        except Exception as e:
            print(f"⚠️ Error obteniendo info de cupón: {e}")
    
    # Intentar desde sesión expandida
    if not coupon_code:
        try:
            expanded_session = stripe.checkout.Session.retrieve(
                session["id"],
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
    
    return coupon_code


def register_sale_in_google_script(email, name, licenses_qty, session_id, amount, plan, affiliate_code=None):
    """
    Llama al Google Apps Script para registrar la venta y generar créditos de licencia.
    Usa el endpoint doGet con action=register_sale
    """
    try:
        params = {
            "action": "register_sale",
            "token": GOOGLE_SCRIPT_TOKEN,
            "email": email,
            "name": name,
            "qty": licenses_qty,
            "session_id": session_id,
            "amount": amount,
            "plan": plan
        }
        
        if affiliate_code:
            params["affiliate"] = affiliate_code
        
        print(f"📤 Enviando registro a Google Script...")
        print(f"   URL: {GOOGLE_SCRIPT_URL}")
        print(f"   Params: email={email}, qty={licenses_qty}, session={session_id[:20]}...")
        
        response = requests.get(
            GOOGLE_SCRIPT_URL, 
            params=params, 
            timeout=30,
            allow_redirects=True  # Google Script hace redirect 302
        )
        
        print(f"📥 Respuesta Google Script: {response.status_code}")
        print(f"   Body: {response.text[:200] if response.text else 'empty'}")
        
        # Google Script responde con JSON
        if response.status_code == 200:
            try:
                result = response.json()
                if result.get("success"):
                    return True
                else:
                    print(f"⚠️ Google Script error: {result.get('error', 'Unknown')}")
                    return False
            except:
                # Si no es JSON, verificar si contiene "success" en el texto
                if "success" in response.text.lower():
                    return True
                return False
        else:
            print(f"⚠️ HTTP Error: {response.status_code}")
            return False
            
    except requests.exceptions.Timeout:
        print(f"⚠️ Timeout llamando a Google Script (30s)")
        return False
    except Exception as e:
        print(f"❌ Error llamando a Google Script: {str(e)}")
        return False


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


# ============================================
# TEST ENDPOINT
# ============================================
@app.route("/test-google-script", methods=["GET"])
def test_google_script():
    """
    Endpoint para probar la conexión con Google Apps Script
    """
    try:
        response = requests.get(
            GOOGLE_SCRIPT_URL,
            params={"action": "ping", "token": GOOGLE_SCRIPT_TOKEN},
            timeout=10,
            allow_redirects=True
        )
        return jsonify({
            "status": "ok" if response.status_code == 200 else "error",
            "http_code": response.status_code,
            "response": response.text[:500]
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500


if __name__ == "__main__":
    app.run(port=4242, debug=False)
