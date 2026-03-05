from flask import Flask, request, jsonify, send_from_directory
from twilio.twiml.messaging_response import MessagingResponse
import requests
import os
import json
import base64
import threading
import uuid
from datetime import datetime, date, timedelta
import re

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
# GEMINI EXTRACT
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
        "Translate ALL product names, brands and categories to ENGLISH. "
        "STRICT RULES: "
        "1. Product MUST have a visible euro price - skip if no price. "
        "2. Skip promotional items, gifts, loyalty rewards, contest prizes, stuffed animals. "
        "3. Convert dates to YYYY-MM-DD, year is " + year + ". od/von=valid_from, do/bis=valid_until. "
        "4. fine_print ONLY for legal disclaimers like limited quantity, while supplies last - otherwise null. "
        "Return ONLY JSON array: [{\"product\":\"English name\",\"brand\":\"brand or null\","
        "\"quantity\":\"250g or null\",\"original_price\":\"2.99 or null\",\"sale_price\":\"1.99\","
        "\"discount_percent\":\"33% or null\",\"valid_from\":\"" + year + "-03-02 or null\","
        "\"valid_until\":\"" + year + "-03-08 or null\",\"category\":\"English category\","
        "\"subcategory\":\"English subcategory\",\"fine_print\":\"disclaimer or null\"}] "
        "Categories: Meat and Fish, Dairy, Bread and Bakery, Fruit and Vegetables, Drinks, "
        "Snacks and Sweets, Canned Food, Cosmetics and Hygiene, Household and Cleaning, "
        "Tools and Construction, Home and Garden, Electronics, Clothing and Shoes, Pet Food, "
        "Health and Pharmacy, Other. If no valid products return: []"
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
    r = requests.patch(
        SUPABASE_URL + "/rest/v1/users?phone=eq." + phone,
        headers={**db_headers(), "Prefer": "return=minimal"},
        json=updates,
        timeout=10
    )
    if r.status_code not in [200, 201, 204]:
        print("update_user failed: " + str(r.status_code) + " " + r.text[:200])

def get_conversation(user):
    conv = user.get("conversation") or "[]"
    if isinstance(conv, list):
        return conv
    try:
        return json.loads(conv)
    except:
        return []

def save_conversation(phone, conversation, user_message, bot_reply):
    # Keep last 30 minutes of conversation
    now = datetime.now()
    conv = conversation or []
    # Add new messages
    conv.append({
        "role": "user",
        "content": user_message,
        "time": now.strftime("%H:%M")
    })
    conv.append({
        "role": "bot",
        "content": bot_reply[:500],
        "time": now.strftime("%H:%M")
    })
    # Keep only messages from last 30 minutes - use last 30 messages as proxy
    conv = conv[-30:]
    result = update_user(phone, {
        "conversation": json.dumps(conv, ensure_ascii=False),
        "total_searches": (conversation.__len__() // 2) + 1,
        "last_active": now.isoformat()
    })
    return conv

def filter_products(message, active, upcoming):
    translations = {
        "mlijeko": "milk", "mlijeka": "milk", "mlijecni": "dairy", "mlijecnih": "dairy",
        "meso": "meat", "mesa": "meat", "mesni": "meat", "mesnih": "meat",
        "pile": "chicken", "piletina": "chicken", "pileca": "chicken", "pileći": "chicken",
        "kruh": "bread", "kruha": "bread", "pecivo": "bakery",
        "voce": "fruit", "voca": "fruit", "povrce": "vegetables", "povrca": "vegetables",
        "jogurt": "yogurt", "jogurta": "yogurt",
        "sir": "cheese", "sira": "cheese",
        "grickalice": "snacks", "slatkisi": "sweets",
        "cokolada": "chocolate", "cokolade": "chocolate",
        "pivo": "beer", "vino": "wine", "sokovi": "juice", "sok": "juice",
        "ulje": "oil", "brasno": "flour", "secer": "sugar",
        "kava": "coffee", "kafe": "coffee", "caj": "tea",
        "riba": "fish", "ribe": "fish",
        "svinjetina": "pork", "svinjski": "pork", "svinjska": "pork",
        "govedina": "beef", "goveđi": "beef",
        "detergent": "detergent", "sapun": "soap", "samponi": "shampoo",
        "ljubimci": "pets", "pas": "dog", "macka": "cat",
        "jaja": "eggs", "jaje": "eggs", "jajima": "eggs"
    }
    msg_lower = message.lower()
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
        store = (p.get("store") or "").lower()
        for kw in keywords:
            if kw in name or kw in brand or kw in cat or kw in subcat or kw in store:
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
            result += " | until: " + str(p.get("valid_until", ""))
            result += " | page: " + str(p.get("page_number", ""))
            result += " | img: " + str(p.get("page_image_url", "")) + "\n"
    if upcoming:
        result += "\n=== UPCOMING DEALS ===\n"
        for p in upcoming:
            result += p.get("store", "") + " | " + p.get("product", "")
            result += " | " + str(p.get("sale_price", ""))
            result += " | from: " + str(p.get("valid_from", "")) + " to " + str(p.get("valid_until", ""))
            result += " | page: " + str(p.get("page_number", ""))
            result += " | img: " + str(p.get("page_image_url", "")) + "\n"
    if fine_prints:
        result += "\n=== STORE NOTES ===\n"
        for s, fp in fine_prints.items():
            result += s + ": " + fp + "\n"
    return result or "No matching products found."

def get_page_image_url(store, page_num, all_products):
    # First try matching store + page number
    for p in all_products:
        if (p.get("store") or "").lower() == store.lower() and p.get("page_number") == page_num:
            if p.get("page_image_url"):
                return p.get("page_image_url")
    # Fallback - just page number
    for p in all_products:
        if p.get("page_number") == page_num and p.get("page_image_url"):
            return p.get("page_image_url")
    return None

def get_adjacent_page(current_url, direction, all_products):
    if not current_url:
        print("get_adjacent_page: no current_url")
        return None
    try:
        parts = current_url.rsplit("_page_", 1)
        if len(parts) != 2:
            print("get_adjacent_page: cant parse URL: " + current_url)
            return None
        prefix = parts[0]
        current_num = int(parts[1].replace(".jpg", ""))
        new_num = current_num + direction
        if new_num < 1:
            return None
        new_url = prefix + "_page_" + str(new_num).zfill(3) + ".jpg"
        print("get_adjacent_page: looking for " + new_url)
        # Check in all products
        for p in all_products:
            if p.get("page_image_url") == new_url:
                print("get_adjacent_page: found!")
                return new_url
        print("get_adjacent_page: not found in " + str(len(all_products)) + " products")
        return None
    except Exception as e:
        print("get_adjacent_page error: " + str(e))
        return None

def extract_page_numbers(text):
    numbers = re.findall(r'\b(\d{1,3})\b', text)
    return [int(n) for n in numbers if 1 <= int(n) <= 200]

def build_conversation_context(conversation):
    if not conversation:
        return ""
    ctx = "CONVERSATION HISTORY (last 30 min):\n"
    for msg in conversation[-20:]:
        ctx += msg.get("role", "") + " [" + msg.get("time", "") + "]: " + msg.get("content", "") + "\n"
    return ctx

def ask_gemini(message, products, user, conversation):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + GEMINI_API_KEY
    today = date.today().strftime("%d.%m.%Y.")

    user_ctx = ""
    if user.get("user_summary"):
        user_ctx = "User profile: " + user.get("user_summary") + "\n"

    conv_ctx = build_conversation_context(conversation)

    prompt = (
        "You are katalog.ai - a smart friendly shopping assistant. Today is " + today + ". "
        + user_ctx
        + conv_ctx
        + "\nPRODUCT DATABASE (English, with page numbers and image URLs):\n" + products + "\n\n"
        "INSTRUCTIONS:\n"
        "- Max 4096 characters total. Be concise.\n"
        "- Respond in the same language the user writes in. Translate product names naturally.\n"
        "- When listing products always mention which PAGE they are on.\n"
        "- After listing products always end with page numbers summary like: Str. 1, 3, 7 — odgovori brojem za pregled 📖\n"
        "- You can split into 2 messages using [MSG2] tag when it improves readability.\n"
        "- Encourage visual browsing - pages are available!\n"
        "- On first greeting introduce yourself and tell user: type a page number to see it, + next page, - previous page.\n"
        "- Use conversation history to remember context - never ask what was already answered.\n"
        "- Be warm and natural. No markdown, no asterisks. Emojis welcome.\n"
        "- Never say you dont have images.\n"
        "\nUser message: " + message
    )
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = requests.post(url, json=body, timeout=30)
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print("ask_gemini error: " + str(e))
        return "Sorry, could not process your request right now."

@app.route("/webhook", methods=["POST"])
def webhook():
    phone = request.form.get("From", "")
    message = request.form.get("Body", "").strip()
    user = get_or_create_user(phone)
    active, upcoming, fine_prints = get_products()
    all_products = active + upcoming
    conversation = get_conversation(user)
    resp = MessagingResponse()

    # ── Navigation: + or - ──
    if message in ["+", ">"]:
        print("Nav next, last_page_url: " + str(user.get("last_page_url")))
        adj = get_adjacent_page(user.get("last_page_url"), 1, all_products)
        if adj:
            msg = resp.message("➡️  ( + sljedeća / - prethodna )")
            msg.media(adj)
            update_user(phone, {"last_page_url": adj})
        else:
            resp.message("Nema sljedeće stranice. Pošalji - za prethodnu.")
        return str(resp)

    if message in ["-", "<"]:
        print("Nav prev, last_page_url: " + str(user.get("last_page_url")))
        adj = get_adjacent_page(user.get("last_page_url"), -1, all_products)
        if adj:
            msg = resp.message("⬅️  ( + sljedeća / - prethodna )")
            msg.media(adj)
            update_user(phone, {"last_page_url": adj})
        else:
            resp.message("Nema prethodne stranice. Pošalji + za sljedeću.")
        return str(resp)

    # ── Page number request ──
    waiting = user.get("waiting_for_page") or False
    available = user.get("available_pages") or []
    if isinstance(available, str):
        try:
            available = json.loads(available)
        except:
            available = []

    nums = extract_page_numbers(message)
    is_only_numbers = bool(nums) and not re.search(r'[a-zA-ZčćšđžČĆŠĐŽ]{3,}', message)
    page_request_nums = []

    if waiting and nums:
        page_request_nums = [n for n in nums if n in available] or nums[:3]
    elif is_only_numbers:
        page_request_nums = nums[:3]
    else:
        explicit = re.findall(r'(?:stranica|str\.|page|pg\.?)\s*(\d+)', message.lower())
        if explicit:
            page_request_nums = [int(n) for n in explicit[:3]]

    if page_request_nums:
        store = user.get("last_catalogue_store") or ""
        sent_any = False
        for pg in page_request_nums[:2]:
            img_url = get_page_image_url(store, pg, all_products)
            if img_url:
                msg = resp.message("Str. " + str(pg) + " 📖  ( + sljedeća / - prethodna )")
                msg.media(img_url)
                update_user(phone, {
                    "last_page_url": img_url,
                    "waiting_for_page": False
                })
                sent_any = True
        if not sent_any:
            resp.message("Stranica nije pronađena. Pokušaj drugi broj.")
        return str(resp)

    # ── Normal message ──
    filtered_active, filtered_upcoming = filter_products(message, active, upcoming)

    page_nums = sorted(set([p.get("page_number") for p in filtered_active + filtered_upcoming if p.get("page_number")]))
    stores = list(set([p.get("store") for p in filtered_active + filtered_upcoming if p.get("store")]))
    main_store = stores[0] if len(stores) == 1 else ""

    products_ctx = format_products(filtered_active, filtered_upcoming, fine_prints)
    reply = ask_gemini(message, products_ctx, user, conversation)

    # Save conversation immediately and reliably
    save_conversation(phone, conversation, message, reply)

    # Save page context
    if page_nums:
        update_user(phone, {
            "waiting_for_page": True,
            "available_pages": json.dumps(page_nums),
            "last_catalogue_store": main_store
        })
    else:
        update_user(phone, {"waiting_for_page": False})

    # Split into 2 messages if [MSG2] present
    parts = reply.split("[MSG2]")
    msg1 = parts[0].strip()
    msg2 = parts[1].strip() if len(parts) > 1 else ""

    resp.message(msg1)
    if msg2:
        resp.message(msg2)

    return str(resp)

@app.route("/", methods=["GET"])
def home():
    return "katalog.ai is running!"

if __name__ == "__main__":
    app.run(debug=True)
