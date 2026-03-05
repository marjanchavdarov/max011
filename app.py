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
                    page_url = upload_image(img_bytes, sn.lower() + "_" + cat_name.lower().replace(" ", "_") + "_page_" + str(i+1).zfill(3) + ".jpg")
                    products, fine_print = extract(img_b64, sn, i+1, vf)
                    if fine_print:
                        cat_fp = (cat_fp + " " + fine_print) if cat_fp else fine_print
                    saved = save_products(products, sn, i+1, page_url, cat_name, vf, vu)
                    total_products += saved

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
# GEMINI EXTRACT — ALL DATA IN ENGLISH
# ─────────────────────────────────────────

def extract(img_b64, store, page_num, valid_from):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key=" + GEMINI_API_KEY
    try:
        year = str(datetime.strptime(valid_from, "%Y-%m-%d").year)
    except:
        year = str(date.today().year)

    prompt = (
        "Page " + str(page_num) + " of " + store + " catalogue. "
        "Extract ONLY real purchasable products that have a clear price in euros. "
        "IMPORTANT: Translate ALL product names, brands, and categories to ENGLISH. "
        "STRICT RULES: "
        "1. Product MUST have a visible sale price in euros (e.g. 2.99) - if no euro price skip it. "
        "2. If only percentage discount shown with no actual price skip it. "
        "3. Skip promotional items, collectibles, gifts, loyalty rewards, stuffed animals, contest prizes. "
        "4. Skip anything that is not a real grocery or household product with a real euro price. "
        "5. Convert ALL dates to YYYY-MM-DD format, year is " + year + ". od/von/de means valid_from, do/bis/a means valid_until. "
        "Example: Od 2.3. do 8.3. means valid_from " + year + "-03-02 valid_until " + year + "-03-08. "
        "6. fine_print is ONLY for legal disclaimers like limited quantity, while supplies last, not in all stores - otherwise null. "
        "Return ONLY a JSON array with ALL fields in English: "
        "[{\"product\":\"Whole Milk\",\"brand\":\"brand or null\",\"quantity\":\"1L or null\","
        "\"original_price\":\"2.99 or null\",\"sale_price\":\"1.99\",\"discount_percent\":\"33% or null\","
        "\"valid_from\":\"" + year + "-03-02 or null\",\"valid_until\":\"" + year + "-03-08 or null\","
        "\"category\":\"category\",\"subcategory\":\"subcategory\",\"fine_print\":\"legal disclaimer or null\"}] "
        "Categories in English: Meat and Fish, Dairy, Bread and Bakery, Fruit and Vegetables, Drinks, "
        "Snacks and Sweets, Canned Food, Cosmetics and Hygiene, Household and Cleaning, "
        "Tools and Construction, Home and Garden, Electronics, Clothing and Shoes, Pet Food, Health and Pharmacy, Other. "
        "If no valid products return: []"
    )
    body = {
        "contents": [{"parts": [{"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}, {"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.1}
    }
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
        if not p.get("sale_price") or p.get("sale_price") in [None, "null", ""]:
            continue
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
            "category": p.get("category", "Other"),
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
    active = requests.get(SUPABASE_URL + "/rest/v1/products?valid_from=lte." + today + "&or=(valid_until.gte." + today + ",valid_until.is.null)&is_expired=eq.false&limit=300&order=store", headers=h)
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

def filter_products(message, active, upcoming):
    # Translate common words to English for matching
    translations = {
        "mlijeko": "milk", "mlijeka": "milk", "mlijecni": "dairy",
        "meso": "meat", "mesa": "meat", "pile": "chicken", "piletina": "chicken",
        "kruh": "bread", "kruha": "bread", "pecivo": "bakery",
        "voce": "fruit", "voca": "fruit", "povrce": "vegetables",
        "jogurt": "yogurt", "jogurta": "yogurt", "sir": "cheese",
        "grickalice": "snacks", "slatkisi": "sweets", "cokolada": "chocolate",
        "pivo": "beer", "vino": "wine", "sokovi": "juice",
        "ulje": "oil", "brasno": "flour", "secer": "sugar",
        "kava": "coffee", "kafe": "coffee", "caj": "tea",
        "riba": "fish", "ribe": "fish", "svinjetina": "pork",
        "govedina": "beef", "janjetina": "lamb",
        "detergent": "detergent", "sapun": "soap", "samponi": "shampoo",
        "kucanstvo": "household", "ljubimci": "pets", "pas": "dog", "macka": "cat"
    }
    msg_lower = message.lower()
    # Replace Croatian words with English equivalents
    for cro, eng in translations.items():
        msg_lower = msg_lower.replace(cro, eng)

    keywords = [w for w in msg_lower.split() if len(w) > 2]
    if not keywords:
        return active[:50], upcoming[:20]

    def matches(p):
        name = (p.get("product") or "").lower()
        brand = (p.get("brand") or "").lower()
        cat = (p.get("category") or "").lower()
        subcat = (p.get("subcategory") or "").lower()
        for kw in keywords:
            if kw in name or kw in brand or kw in cat or kw in subcat:
                return True
        return False

    filtered_active = [p for p in active if matches(p)]
    filtered_upcoming = [p for p in upcoming if matches(p)]

    if not filtered_active and not filtered_upcoming:
        return active[:50], upcoming[:20]

    return filtered_active[:50], filtered_upcoming[:20]

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
    return result or "No matching products found."

def update_user_summary(phone, user_summary, conversation, user_message, bot_reply):
    conv = conversation or []
    conv.append({"role": "user", "content": user_message})
    conv.append({"role": "bot", "content": bot_reply})
    conv = conv[-10:]
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + GEMINI_API_KEY
    prompt = ("Current user profile: " + (user_summary or "empty") + ". Latest exchange - User said: " + user_message + ". Bot replied: " + bot_reply[:200] + ". Update the profile in max 60 words. Include: preferred stores, product interests, language preference, any personal details mentioned. Return ONLY the updated profile text.")
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = requests.post(url, json=body, timeout=15)
        new_summary = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except:
        new_summary = user_summary or ""
    update_user(phone, {
        "last_active": date.today().strftime("%Y-%m-%d"),
        "conversation": json.dumps(conv),
        "user_summary": new_summary
    })

def ask_gemini(message, products, user):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + GEMINI_API_KEY
    today = date.today().strftime("%d.%m.%Y.")
    user_ctx = ""
    if user.get("user_summary"):
        user_ctx += "User profile: " + user.get("user_summary") + "\n"
    if user.get("conversation"):
        try:
            conv = json.loads(user.get("conversation")) if isinstance(user.get("conversation"), str) else user.get("conversation")
            if conv:
                user_ctx += "Recent conversation:\n"
                for msg in conv[-6:]:
                    user_ctx += msg.get("role", "") + ": " + msg.get("content", "") + "\n"
        except:
            pass
    prompt = (
        "You are katalog.ai - a friendly personal shopping assistant. Today is " + today + ". "
        + (user_ctx if user_ctx else "")
        + "PRODUCT DATABASE (in English): " + products + " "
        "RULES: "
        "1. If user says hello or greeting - introduce yourself as katalog.ai shopping assistant and ask what they are looking for. Also tell them: type + for next page, - for previous page after seeing a catalogue page. Do NOT list products on greeting. "
        "2. For product questions - start with one short friendly line, then list max 5 products each as its own block with name and brand on first line, price and date on second line. "
        "3. Use emojis: meat=🥩 dairy=🥛 fruit=🍎 snacks=🍿 sweets=🍫 bread=🍞 drinks=🥤 pets=🐾 home=🏠 veggies=🥦. "
        "4. Upcoming deals - mention start date. "
        "5. End with one short friendly line. "
        "6. NO markdown NO asterisks. "
        "7. IMPORTANT: Respond in the same language the user writes in. Translate product names to that language. Keep ALL dates and words in that same language. "
        "8. Never say you dont have images or pictures. "
        "9. Be warm and natural like a friend who knows all the deals - not robotic. "
        "User asks: " + message
    )
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
    # Translate Croatian keywords to English for matching
    translations = {
        "mlijeko": "milk", "meso": "meat", "pile": "chicken",
        "kruh": "bread", "voce": "fruit", "povrce": "vegetables",
        "jogurt": "yogurt", "sir": "cheese", "grickalice": "snacks",
        "cokolada": "chocolate", "pivo": "beer", "riba": "fish"
    }
    for cro, eng in translations.items():
        msg_lower = msg_lower.replace(cro, eng)

    keywords = [w for w in msg_lower.split() if len(w) > 3]
    for keyword in keywords:
        for p in products:
            product_name = (p.get("product") or "").lower()
            category = (p.get("category") or "").lower()
            if keyword in product_name or keyword in category:
                if p.get("page_image_url"):
                    return p.get("page_image_url")
    return None

def get_adjacent_page(current_url, direction):
    if not current_url:
        return None
    try:
        parts = current_url.rsplit("_page_", 1)
        if len(parts) != 2:
            return None
        prefix = parts[0]
        page_part = parts[1]
        current_num = int(page_part.replace(".jpg", ""))
        new_num = current_num + direction
        if new_num < 1:
            return None
        new_url = prefix + "_page_" + str(new_num).zfill(3) + ".jpg"
        # Check if page exists by looking in products table
        filename = new_url.split("/katalog-images/")[-1]
        h = db_headers()
        r = requests.get(SUPABASE_URL + "/rest/v1/products?page_image_url=eq." + new_url + "&limit=1&select=page_image_url", headers=h, timeout=5)
        if r.status_code == 200 and r.json():
            return new_url
        return None
    except Exception as e:
        print("Adjacent page error: " + str(e))
        return None

@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.form.get("From", "")
    message = request.form.get("Body", "").strip()
    user = get_or_create_user(phone)

    # Handle page navigation
    if message in ["+", ">"]:
        next_url = get_adjacent_page(user.get("last_page_url"), 1)
        resp = MessagingResponse()
        if next_url:
            msg = resp.message("Next page ➡️  (+ for next, - for previous)")
            msg.media(next_url)
            update_user(phone, {"last_page_url": next_url})
        else:
            resp.message("No next page. Send - for previous page.")
        return str(resp)

    if message in ["-", "<"]:
        prev_url = get_adjacent_page(user.get("last_page_url"), -1)
        resp = MessagingResponse()
        if prev_url:
            msg = resp.message("Previous page ⬅️  (+ for next, - for previous)")
            msg.media(prev_url)
            update_user(phone, {"last_page_url": prev_url})
        else:
            resp.message("No previous page. Send + for next page.")
        return str(resp)

    active, upcoming, fine_prints = get_products()
    filtered_active, filtered_upcoming = filter_products(message, active, upcoming)
    products_ctx = format_products(filtered_active, filtered_upcoming, fine_prints)
    reply = ask_gemini(message, products_ctx, user)
    page_image = find_page_image(message, active + upcoming)
    conversation = user.get("conversation") or []
    if isinstance(conversation, str):
        try:
            conversation = json.loads(conversation)
        except:
            conversation = []
    update_user_summary(phone, user.get("user_summary"), conversation, message, reply)
    resp = MessagingResponse()
    msg = resp.message(reply)
    if page_image:
        msg.media(page_image)
        update_user(phone, {"last_page_url": page_image})
    return str(resp)

@app.route("/", methods=["GET"])
def home():
    return "katalog.ai is running!"

if __name__ == "__main__":
    app.run(debug=True)
