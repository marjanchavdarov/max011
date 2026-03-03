from flask import Flask, request, jsonify, send_from_directory
from twilio.twiml.messaging_response import MessagingResponse
import requests
import os
import json
import base64
from datetime import datetime, date, timedelta

app = Flask(__name__, static_folder="static")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# ─────────────────────────────────────────
# UPLOAD TOOL
# ─────────────────────────────────────────

@app.route("/upload-tool")
def upload_tool():
    return send_from_directory("static", "upload.html")

@app.route("/upload", methods=["POST"])
def upload():
    try:
        import fitz
    except ImportError:
        return jsonify({"type": "error", "message": "PyMuPDF not installed"})

    def stream():
        try:
            f = request.files.get("file")
            store = request.form.get("store", "").strip()
            valid_from = request.form.get("valid_from", "").strip()
            valid_until = request.form.get("valid_until", "").strip()

            if not f or not store or not valid_from:
                yield json.dumps({"type": "error", "message": "Missing fields"}) + "\n"
                return

            if not valid_until:
                d = datetime.strptime(valid_from, "%Y-%m-%d")
                valid_until = (d + timedelta(days=14)).strftime("%Y-%m-%d")

            tmp = "/tmp/catalogue.pdf"
            f.save(tmp)

            doc = fitz.open(tmp)
            total_pages = len(doc)
            total_products = 0
            catalogue_name = f.filename.replace(".pdf", "")
            catalogue_fine_print = None

            yield json.dumps({"type": "start", "pages": total_pages}) + "\n"

            for i in range(total_pages):
                try:
                    page = doc[i]
                    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5))
                    img_bytes = pix.tobytes("jpeg")
                    img_b64 = base64.b64encode(img_bytes).decode()

                    page_url = upload_image(img_bytes, store.lower() + "_page_" + str(i+1).zfill(3) + ".jpg")

                    products, fine_print = extract(img_b64, store, i+1)
                    if fine_print:
                        catalogue_fine_print = (catalogue_fine_print + " " + fine_print) if catalogue_fine_print else fine_print

                    saved = save_products(products, store, i+1, page_url, catalogue_name, valid_from, valid_until)
                    total_products += saved

                    yield json.dumps({"type": "page", "page": i+1, "total_pages": total_pages, "products_found": len(products), "products_saved": saved, "total_products": total_products}) + "\n"

                except Exception as e:
                    yield json.dumps({"type": "page_error", "page": i+1, "error": str(e)}) + "\n"
                    continue

            doc.close()
            os.remove(tmp)

            save_catalogue(store, catalogue_name, valid_from, valid_until, catalogue_fine_print, total_pages, total_products)

            yield json.dumps({"type": "done", "products": total_products, "pages": total_pages}) + "\n"

        except Exception as e:
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return app.response_class(stream(), mimetype="application/x-ndjson")

# ─────────────────────────────────────────
# GEMINI
# ─────────────────────────────────────────

def extract(img_b64, store, page_num):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=" + GEMINI_API_KEY
    prompt = (
        "Page " + str(page_num) + " of " + store + " catalogue. "
        "Extract ALL products with prices. Also extract any fine print or disclaimers. "
        "Return ONLY a JSON array: "
        "[{\"product\":\"name\",\"brand\":\"brand or null\",\"quantity\":\"250g or null\","
        "\"original_price\":\"2.99 or null\",\"sale_price\":\"1.99\","
        "\"discount_percent\":\"33% or null\",\"valid_until\":\"08.03.2026. or null\","
        "\"category\":\"category\",\"subcategory\":\"subcategory\","
        "\"fine_print\":\"disclaimer or null\"}] "
        "Categories: Meso i riba, Mlijecni proizvodi, Kruh i pekarski, Voce i povrce, "
        "Pice, Grickalice i slatkisi, Konzervirana hrana, Kozmetika i higijena, "
        "Kucanstvo i ciscenje, Alati i gradnja, Dom i vrt, Elektronika, "
        "Odjeca i obuca, Kucni ljubimci, Zdravlje i ljekarna, Ostalo. "
        "If no products return: []"
    )
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                {"text": prompt}
            ]
        }]
    }
    try:
        r = requests.post(url, json=body, timeout=60)
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        if not isinstance(result, list):
            return [], None
        fine_print = None
        for p in result:
            if p.get("fine_print") and p.get("fine_print") != "null":
                fine_print = p.get("fine_print")
                break
        return result, fine_print
    except Exception as e:
        print("Gemini error page " + str(page_num) + ": " + str(e))
        return [], None

# ─────────────────────────────────────────
# SUPABASE
# ─────────────────────────────────────────

def headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "application/json"
    }

def upload_image(img_bytes, filename):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "image/jpeg"
    }
    url = SUPABASE_URL + "/storage/v1/object/katalog-images/" + filename
    r = requests.post(url, headers=h, data=img_bytes)
    if r.status_code in [200, 201]:
        return SUPABASE_URL + "/storage/v1/object/public/katalog-images/" + filename
    return None

def parse_date(s):
    if not s or s == "null":
        return None
    for fmt in ["%d.%m.%Y.", "%d.%m.%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except:
            continue
    return None

def save_products(products, store, page_num, page_url, catalogue_name, valid_from, valid_until):
    if not products:
        return 0
    records = []
    for p in products:
        vu = parse_date(p.get("valid_until")) or valid_until
        if not vu:
            continue
        records.append({
            "store": store,
            "product": p.get("product", ""),
            "brand": p.get("brand") if p.get("brand") not in [None, "null"] else None,
            "quantity": p.get("quantity") if p.get("quantity") not in [None, "null"] else None,
            "original_price": p.get("original_price") if p.get("original_price") not in [None, "null"] else None,
            "sale_price": p.get("sale_price", ""),
            "discount_percent": p.get("discount_percent") if p.get("discount_percent") not in [None, "null"] else None,
            "category": p.get("category", "Ostalo"),
            "subcategory": p.get("subcategory"),
            "valid_from": valid_from,
            "valid_until": vu,
            "is_expired": False,
            "page_image_url": page_url,
            "page_number": page_num,
            "catalogue_name": catalogue_name,
            "catalogue_week": datetime.now().strftime("%Y-W%V")
        })
    if not records:
        return 0
    r = requests.post(
        SUPABASE_URL + "/rest/v1/products",
        headers={**headers(), "Prefer": "return=minimal"},
        json=records
    )
    return len(records) if r.status_code in [200, 201] else 0

def save_catalogue(store, catalogue_name, valid_from, valid_until, fine_print, pages, products_count):
    requests.post(
        SUPABASE_URL + "/rest/v1/catalogues",
        headers={**headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        json={
            "store": store,
            "catalogue_name": catalogue_name,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "fine_print": fine_print,
            "pages": pages,
            "products_count": products_count
        }
    )

# ─────────────────────────────────────────
# WHATSAPP BOT
# ─────────────────────────────────────────

def get_products():
    today = date.today().strftime("%Y-%m-%d")
    future = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
    h = headers()
    active = requests.get(
        SUPABASE_URL + "/rest/v1/products?valid_from=lte." + today + "&valid_until=gte." + today + "&is_expired=eq.false&limit=300&order=store",
        headers=h
    )
    upcoming = requests.get(
        SUPABASE_URL + "/rest/v1/products?valid_from=gt." + today + "&valid_from=lte." + future + "&is_expired=eq.false&limit=100&order=valid_from",
        headers=h
    )
    catalogues = requests.get(
        SUPABASE_URL + "/rest/v1/catalogues?valid_until=gte." + today + "&select=store,fine_print",
        headers=h
    )
    fine_prints = {}
    if catalogues.status_code == 200:
        for c in catalogues.json():
            if c.get("fine_print"):
                fine_prints[c["store"]] = c["fine_print"]
    return (
        active.json() if active.status_code == 200 else [],
        upcoming.json() if upcoming.status_code == 200 else [],
        fine_prints
    )

def get_or_create_user(phone):
    h = headers()
    r = requests.get(SUPABASE_URL + "/rest/v1/users?phone=eq." + phone, headers=h)
    if r.status_code == 200 and r.json():
        return r.json()[0]
    requests.post(
        SUPABASE_URL + "/rest/v1/users",
        headers={**h, "Prefer": "return=minimal"},
        json={"phone": phone, "total_searches": 0}
    )
    return {"phone": phone, "total_searches": 0}

def update_user(phone, updates):
    requests.patch(
        SUPABASE_URL + "/rest/v1/users?phone=eq." + phone,
        headers={**headers(), "Prefer": "return=minimal"},
        json=updates
    )

def format_products(active, upcoming, fine_prints):
    result = ""
    if active:
        result += "=== ACTIVE DEALS ===\n"
        for p in active:
            result += p.get("store", "") + " | " + p.get("product", "")
            if p.get("brand"): result += " (" + p.get("brand") + ")"
            if p.get("quantity"): result += " " + p.get("quantity")
            result += " | " + str(p.get("sale_price", ""))
            if p.get("original_price"): result += " (was " + str(p.get("original_price")) + ")"
            if p.get("fine_print"): result += " | Note: " + p.get("fine_print")
            result += " | until: " + str(p.get("valid_until", "")) + "\n"
    if upcoming:
        result += "\n=== UPCOMING DEALS ===\n"
        for p in upcoming:
            result += p.get("store", "") + " | " + p.get("product", "")
            result += " | " + str(p.get("sale_price", ""))
            result += " | from: " + str(p.get("valid_from", "")) + " to " + str(p.get("valid_until", "")) + "\n"
    if fine_prints:
        result += "\n=== STORE NOTES ===\n"
        for store, fp in fine_prints.items():
            result += store + ": " + fp + "\n"
    return result or "Database is empty."

def ask_gemini(message, products, user):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=" + GEMINI_API_KEY
    today = date.today().strftime("%d.%m.%Y.")
    user_ctx = ""
    if user.get("name"): user_ctx += "Name: " + user.get("name") + "\n"
    if user.get("preferred_stores"): user_ctx += "Prefers: " + str(user.get("preferred_stores")) + "\n"
    prompt = (
        "You are katalog.ai - a personal shopping assistant for Croatia. Today is " + today + ". "
        + ("User: " + user_ctx if user_ctx else "")
        + "CATALOGUES: " + products + " "
        "RULES: "
        "1. If active deal exists - say where and price. "
        "2. If no active deal but upcoming - say when it starts and where. "
        "3. Max 4-5 products. "
        "4. NO markdown, NO asterisks, NO bullet points - plain text only. "
        "5. Be friendly like a friend who knows all prices. "
        "6. Include store notes/disclaimers when relevant. "
        "User asks: " + message
    )
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = requests.post(url, json=body, timeout=30)
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except:
        return "Sorry, I could not process your request right now."

@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.form.get("From", "")
    message = request.form.get("Body", "").strip()
    user = get_or_create_user(phone)
    active, upcoming, fine_prints = get_products()
    products_ctx = format_products(active, upcoming, fine_prints)
    reply = ask_gemini(message, products_ctx, user)
    update_user(phone, {
        "total_searches": (user.get("total_searches") or 0) + 1,
        "last_active": date.today().strftime("%Y-%m-%d")
    })
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

@app.route("/", methods=["GET"])
def home():
    return "katalog.ai is running!"

if __name__ == "__main__":
    app.run(debug=True)
