from flask import Flask, request, jsonify, send_from_directory
from twilio.twiml.messaging_response import MessagingResponse
import requests
import os
import json
import base64
import threading
import uuid
from datetime import datetime, date, timedelta

app = Flask(__name__, static_folder="static")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

def db_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY, "Content-Type": "application/json"}

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
        return jsonify({"error": "PyMuPDF not installed"}), 500

    f = request.files.get("file")
    sn = request.form.get("store", "").strip()
    vf = request.form.get("valid_from", "").strip()
    vu = request.form.get("valid_until", "").strip()

    if not f or not sn or not vf:
        return jsonify({"error": "Missing fields"}), 400

    if not vu:
        d = datetime.strptime(vf, "%Y-%m-%d")
        vu = (d + timedelta(days=14)).strftime("%Y-%m-%d")

    fd = f.read()
    fn = f.filename

    try:
        import fitz
        tmp = "/tmp/count.pdf"
        with open(tmp, "wb") as fp:
            fp.write(fd)
        doc = fitz.open(tmp)
        total_pages = len(doc)
        doc.close()
        os.remove(tmp)
    except Exception as e:
        return jsonify({"error": "Could not read PDF: " + str(e)}), 500

    job_id = str(uuid.uuid4())[:8]
    cat_name = fn.replace(".pdf", "")

    # Save job to Supabase
    requests.post(
        SUPABASE_URL + "/rest/v1/jobs",
        headers={**db_headers(), "Prefer": "return=minimal"},
        json={"id": job_id, "store": sn, "catalogue_name": cat_name, "valid_from": vf, "valid_until": vu, "total_pages": total_pages, "current_page": 0, "total_products": 0, "status": "processing"}
    )

    def process():
        try:
            import fitz
            tmp = "/tmp/" + job_id + ".pdf"
            with open(tmp, "wb") as fp:
                fp.write(fd)
            doc = fitz.open(tmp)
            cat_fp = None
            total_products = 0

            for i in range(total_pages):
                try:
                    page = doc[i]
                    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5))
                    img_bytes = pix.tobytes("jpeg")
                    img_b64 = base64.b64encode(img_bytes).decode()
                    page_url = upload_image(img_bytes, sn.lower() + "_page_" + str(i+1).zfill(3) + ".jpg")
                    products, fine_print = extract(img_b64, sn, i+1)
                    if fine_print:
                        cat_fp = (cat_fp + " " + fine_print) if cat_fp else fine_print
                    saved = save_products(products, sn, i+1, page_url, cat_name, vf, vu)
                    total_products += saved

                    # Update job progress in Supabase
                    requests.patch(
                        SUPABASE_URL + "/rest/v1/jobs?id=eq." + job_id,
                        headers={**db_headers(), "Prefer": "return=minimal"},
                        json={"current_page": i + 1, "total_products": total_products, "fine_print": cat_fp}
                    )

                except Exception as e:
                    print("Page " + str(i+1) + " error: " + str(e))
                    continue

            doc.close()
            os.remove(tmp)
            save_catalogue(sn, cat_name, vf, vu, cat_fp, total_pages, total_products)

            # Mark job done
            requests.patch(
                SUPABASE_URL + "/rest/v1/jobs?id=eq." + job_id,
                headers={**db_headers(), "Prefer": "return=minimal"},
                json={"status": "done", "current_page": total_pages, "total_products": total_products}
            )

        except Exception as e:
            requests.patch(
                SUPABASE_URL + "/rest/v1/jobs?id=eq." + job_id,
                headers={**db_headers(), "Prefer": "return=minimal"},
                json={"status": "error"}
            )
            print("Job error: " + str(e))

    t = threading.Thread(target=process)
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id, "total_pages": total_pages})

@app.route("/status/<job_id>")
def status(job_id):
    r = requests.get(
        SUPABASE_URL + "/rest/v1/jobs?id=eq." + job_id,
        headers=db_headers()
    )
    if r.status_code == 200 and r.json():
        return jsonify(r.json()[0])
    return jsonify({"error": "Job not found"}), 404

# ─────────────────────────────────────────
# GEMINI
# ─────────────────────────────────────────

def extract(img_b64, store, page_num):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key=" + GEMINI_API_KEY
    prompt = ("Page " + str(page_num) + " of " + store + " catalogue. Extract ALL products with prices. IMPORTANT: Convert ALL dates to YYYY-MM-DD format, year is 2026. od means valid_from, do means valid_until. Example: Od ponedjeljka 2.3. do 8.3. means valid_from 2026-03-02 valid_until 2026-03-08. Od petka 6.3. with no end date means valid_from 2026-03-06 valid_until null. fine_print is ONLY for legal disclaimers like kolicina ogranicena, dok traje zaliha, nije u svim poslovnicama, vrijedi uz karticu - otherwise null. Return ONLY a JSON array: [{\"product\":\"name\",\"brand\":\"brand or null\",\"quantity\":\"250g or null\",\"original_price\":\"2.99 or null\",\"sale_price\":\"1.99\",\"discount_percent\":\"33% or null\",\"valid_from\":\"2026-03-02 or null\",\"valid_until\":\"2026-03-08 or null\",\"category\":\"category\",\"subcategory\":\"subcategory\",\"fine_print\":\"legal disclaimer or null\"}] Categories: Meso i riba, Mlijecni proizvodi, Kruh i pekarski, Voce i povrce, Pice, Grickalice i slatkisi, Konzervirana hrana, Kozmetika i higijena, Kucanstvo i ciscenje, Alati i gradnja, Dom i vrt, Elektronika, Odjeca i obuca, Kucni ljubimci, Zdravlje i ljekarna, Ostalo. If no products return: []")
    body = {"contents": [{"parts": [{"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}, {"text": prompt}]}]}
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=45)
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = text.replace("```json", "").replace("```", "").strip()
            result = json.loads(text)
            if not isinstance(result, list):
                return [], None
            fine_print = None
            for p in result:
                if p.get("fine_print") and p.get("fine_print") not in [None, "null"]:
                    fine_print = p.get("fine_print")
                    break
            return result, fine_print
        except Exception as e:
            print("Gemini error page " + str(page_num) + " attempt " + str(attempt+1) + ": " + str(e))
            if attempt == 2:
                return [], None
            continue
    return [], None


# ─────────────────────────────────────────
# SUPABASE HELPERS
# ─────────────────────────────────────────

def upload_image(img_bytes, filename):
    h = {"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY, "Content-Type": "image/jpeg"}
    r = requests.post(SUPABASE_URL + "/storage/v1/object/katalog-images/" + filename, headers=h, data=img_bytes)
    if r.status_code in [200, 201]:
        return SUPABASE_URL + "/storage/v1/object/public/katalog-images/" + filename
    return None

def parse_date(s):
    if not s or s == "null":
        return None
    for fmt in ["%Y-%m-%d", "%d.%m.%Y.", "%d.%m.%Y"]:
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
        vf = parse_date(p.get("valid_from")) or valid_from
        if not vu:
            vu = valid_until
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
            "valid_from": vf,
            "valid_until": vu,
            "is_expired": False,
            "page_image_url": page_url,
            "page_number": page_num,
            "catalogue_name": catalogue_name,
            "catalogue_week": datetime.now().strftime("%Y-W%V")
        })
    if not records:
        return 0
    r = requests.post(SUPABASE_URL + "/rest/v1/products", headers={**db_headers(), "Prefer": "return=minimal"}, json=records)
    return len(records) if r.status_code in [200, 201] else 0

def save_catalogue(store, catalogue_name, valid_from, valid_until, fine_print, pages, products_count):
    requests.post(SUPABASE_URL + "/rest/v1/catalogues", headers={**db_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}, json={"store": store, "catalogue_name": catalogue_name, "valid_from": valid_from, "valid_until": valid_until, "fine_print": fine_print, "pages": pages, "products_count": products_count})

# ─────────────────────────────────────────
# WHATSAPP BOT
# ─────────────────────────────────────────

def get_products():
    today = date.today().strftime("%Y-%m-%d")
    future = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
    h = db_headers()
    active = requests.get(SUPABASE_URL + "/rest/v1/products?valid_from=lte." + today + "&valid_until=gte." + today + "&is_expired=eq.false&limit=300&order=store", headers=h)
    upcoming = requests.get(SUPABASE_URL + "/rest/v1/products?valid_from=gt." + today + "&valid_from=lte." + future + "&is_expired=eq.false&limit=100&order=valid_from", headers=h)
    catalogues = requests.get(SUPABASE_URL + "/rest/v1/catalogues?valid_until=gte." + today + "&select=store,fine_print", headers=h)
    fine_prints = {}
    if catalogues.status_code == 200:
        for c in catalogues.json():
            if c.get("fine_print"):
                fine_prints[c["store"]] = c["fine_print"]
    return (active.json() if active.status_code == 200 else [], upcoming.json() if upcoming.status_code == 200 else [], fine_prints)

def get_or_create_user(phone):
    h = db_headers()
    r = requests.get(SUPABASE_URL + "/rest/v1/users?phone=eq." + phone, headers=h)
    if r.status_code == 200 and r.json():
        return r.json()[0]
    requests.post(SUPABASE_URL + "/rest/v1/users", headers={**h, "Prefer": "return=minimal"}, json={"phone": phone, "total_searches": 0})
    return {"phone": phone, "total_searches": 0}

def update_user(phone, updates):
    requests.patch(SUPABASE_URL + "/rest/v1/users?phone=eq." + phone, headers={**db_headers(), "Prefer": "return=minimal"}, json=updates)

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
            result += " | until: " + str(p.get("valid_until", "")) + "\n"
    if upcoming:
        result += "\n=== UPCOMING DEALS ===\n"
        for p in upcoming:
            result += p.get("store", "") + " | " + p.get("product", "")
            result += " | " + str(p.get("sale_price", ""))
            result += " | from: " + str(p.get("valid_from", "")) + " to " + str(p.get("valid_until", "")) + "\n"
    if fine_prints:
        result += "\n=== STORE NOTES ===\n"
        for s, fp in fine_prints.items():
            result += s + ": " + fp + "\n"
    return result or "Database is empty."

def ask_gemini(message, products, user):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + GEMINI_API_KEY
    today = date.today().strftime("%d.%m.%Y.")
    user_ctx = ""
    if user.get("name"): user_ctx += "Name: " + user.get("name") + "\n"
    if user.get("preferred_stores"): user_ctx += "Prefers: " + str(user.get("preferred_stores")) + "\n"
    prompt = ("You are katalog.ai - a personal shopping assistant for Croatia. Today is " + today + ". " + ("User: " + user_ctx if user_ctx else "") + "CATALOGUES: " + products + " RULES: 1. Start with one short friendly intro line. 2. List max 5 products, each as its own block - product name and brand on first line, then price and valid date on second line. 3. Use emojis for categories: meat=🥩 dairy=🥛 fruit=🍎 snacks=🍿 sweets=🍫 bread=🍞 drinks=🥤 pets=🐾 home=🏠. 4. If upcoming deal say from which date. 5. End with one short friendly closing line. 6. NO markdown NO asterisks. 7. Respond in the same language the user writes in.8. Always write dates in Croatian format like '8. ožujka' not '2026-03-08'. 9. Always use Croatian words: 'cijena' for price, 'prije' for was, 'od' for from, 'do' for until. User asks: " + message)
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = requests.post(url, json=body, timeout=30)
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except:
        return "Sorry, I could not process your request right now."

def find_page_image(message, products):
    if not products:
        return None
    msg_lower = message.lower()
    keywords = [w for w in msg_lower.split() if len(w) > 3]
    for keyword in keywords:
        for p in products:
            product_name = (p.get("product") or "").lower()
            category = (p.get("category") or "").lower()
            if keyword in product_name or keyword in category:
                if p.get("page_image_url"):
                    return p.get("page_image_url")
    return None

@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.form.get("From", "")
    message = request.form.get("Body", "").strip()
    user = get_or_create_user(phone)
    active, upcoming, fine_prints = get_products()
    products_ctx = format_products(active, upcoming, fine_prints)
    reply = ask_gemini(message, products_ctx, user)
    update_user(phone, {"total_searches": (user.get("total_searches") or 0) + 1, "last_active": date.today().strftime("%Y-%m-%d")})
    page_image = find_page_image(message, active + upcoming)
    resp = MessagingResponse()
    msg = resp.message(reply)
    if page_image:
        msg.media(page_image)
    return str(resp)

@app.route("/", methods=["GET"])
def home():
    return "katalog.ai is running!"

if __name__ == "__main__":
    app.run(debug=True)
