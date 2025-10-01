# server/server.py
# -*- coding: utf-8 -*-

import os, uuid, json, smtplib, datetime
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from flask import Flask, request, send_from_directory, render_template, abort, jsonify, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

VERSION = "proofok-rescue-v1"

BASE_URL   = os.getenv("BASE_URL", "http://127.0.0.1:5000")
SMTP_HOST  = os.getenv("SMTP_HOST", "smtp.colormagic.biz")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER", "")
SMTP_PASS  = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "PROOFS@colormagic.biz")
TO_EMAIL   = os.getenv("TO_EMAIL", "orders@colormagic.biz")
SMTP_SSL   = os.getenv("SMTP_SSL", "false").lower() == "true"
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "10"))
EMAIL_MODE = os.getenv("EMAIL_MODE", "async").lower()

app = Flask(__name__)
BASE_DIR = os.path.dirname(__file__)
app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "uploads")
app.config["DATA_FOLDER"]   = os.path.join(BASE_DIR, "data")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["DATA_FOLDER"], exist_ok=True)

executor = ThreadPoolExecutor(max_workers=2)

def record_path(token: str) -> str:
    return os.path.join(app.config["DATA_FOLDER"], f"{token}.json")

def save_record(token: str, record: dict) -> None:
    with open(record_path(token), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)

def load_record(token: str) -> Optional[dict]:
    path = record_path(token)
    if not os.path.exists(path): return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def send_email(subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject; msg["From"] = FROM_EMAIL; msg["To"] = TO_EMAIL; msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text, "plain", "utf-8")); msg.attach(MIMEText(html, "html", "utf-8"))
    if SMTP_SSL:
        import ssl; ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT, context=ctx) as s:
            if SMTP_USER: s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as s:
            s.ehlo()
            try: s.starttls(); s.ehlo()
            except Exception: pass
            if SMTP_USER: s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

def render_result(ok: bool, message: str = "", warning: str = "", token: str = "", original_name: str = ""):
    return render_template("result.html", ok=ok, message=message, warning=warning, token=token,
                           original_name=original_name, base_url=BASE_URL, version=VERSION)

@app.route("/")
def index():
    return (f"ProofOK is running. Version: {VERSION} — try <a href='/healthz'>/healthz</a> or <a href='/routes'>/routes</a>", 200)

@app.get("/healthz")
def healthz():
    return {"ok": True, "version": VERSION, "time": datetime.datetime.utcnow().isoformat()+"Z"}

@app.get("/routes")
def routes():
    return {"routes": [str(r) for r in app.url_map.iter_rules()]}

@app.post("/api/upload")
def api_upload():
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a .pdf file"}), 400
    original_name = request.form.get("original_name", file.filename)
    token = uuid.uuid4().hex[:12]
    token_dir = os.path.join(app.config["UPLOAD_FOLDER"], token); os.makedirs(token_dir, exist_ok=True)
    safe_name = original_name.replace("/", "_").replace("\\", "_")
    pdf_path = os.path.join(token_dir, safe_name); file.save(pdf_path)
    now = datetime.datetime.utcnow().isoformat() + "Z"
    rec = {"token": token, "original_name": original_name, "stored_name": safe_name,
           "created_utc": now, "status": "pending", "responses": []}
    save_record(token, rec)
    url = f"{BASE_URL}/proof/{token}"
    app.logger.info("[{}] /api/upload token={} name={}".format(VERSION, token, original_name))
    return jsonify({"ok": True, "token": token, "url": url})

@app.get("/proof/<token>")
def proof_page(token):
    rec = load_record(token)
    if not rec: abort(404)
    return render_template("proof.html", token=token, original_name=rec["original_name"],
                           pdf_url=url_for("serve_pdf", token=token, filename=rec["stored_name"]),
                           base_url=BASE_URL, version=VERSION)

@app.get("/p/<token>/<path:filename>")
def serve_pdf(token, filename):
    folder = os.path.join(app.config["UPLOAD_FOLDER"], token)
    if not os.path.isdir(folder): abort(404)
    return send_from_directory(folder, filename, mimetype="application/pdf", as_attachment=False)

@app.post("/api/respond/<token>")
def api_respond(token):
    rec = load_record(token)
    if not rec: return jsonify({"error": "Not found"}), 404
    data = request.json if request.is_json else request.form
    decision = (data.get("decision") or "").lower()
    comment = (data.get("comment") or "").strip()
    viewer_name = (data.get("viewer_name") or "").strip()
    viewer_email = (data.get("viewer_email") or "").strip()
    if decision not in ("approved", "rejected"):
        return jsonify({"error": "decision must be 'approved' or 'rejected'"}), 400
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    app.logger.info("[{}] /api/respond token={} decision={} ip={} mode={}".format(VERSION, token, decision, ip, EMAIL_MODE))
    event = {"ts_utc": datetime.datetime.utcnow().isoformat()+"Z", "decision": decision, "comment": comment,
             "viewer_name": viewer_name, "viewer_email": viewer_email, "ip": ip}
    rec["status"] = decision; rec["responses"].append(event); save_record(token, rec)
    proof_url = f"{BASE_URL}/proof/{token}"
    subject = "[Proof] {} — {}".format(rec['original_name'], decision.upper())
    text = ("Proof decision received.\n\nFile: {}\nLink: {}\nDecision: {}\nName: {}\nEmail: {}\nComment:\n{}\n\n"
            "Time (UTC): {}\nIP: {}\n").format(rec['original_name'], proof_url, decision, viewer_name, viewer_email, comment, event['ts_utc'], event['ip'])
    html = ("""<h2>Proof decision received</h2>
    <p><b>File:</b> {}</p><p><b>Link:</b> <a href="{}">{}</a></p><p><b>Decision:</b> {}</p>
    <p><b>Name:</b> {} &lt;{}&gt;</p><p><b>Comment:</b><br>{}</p><p><small>Time (UTC): {} | IP: {}</small></p>"""
            .format(rec['original_name'], proof_url, proof_url, decision, viewer_name, viewer_email, (comment or '').replace("\n","<br>"), event['ts_utc'], event['ip']))
    resp = {"ok": True}
    if EMAIL_MODE == "off":
        app.logger.warning("[{}] EMAIL_MODE=off (skipping SMTP)".format(VERSION)); return jsonify(resp), 200
    if EMAIL_MODE == "sync":
        try: send_email(subject, html, text)
        except Exception as e: app.logger.exception("Email send failed (sync)"); resp["warning"] = "Email send failed ({}:{}): {}".format(SMTP_HOST, SMTP_PORT, e)
        return jsonify(resp), 200
    warn = None
    try:
        fut = executor.submit(send_email, subject, html, text); fut.result(timeout=SMTP_TIMEOUT)
    except FuturesTimeout:
        warn = "Email is sending in background (timeout {}s).".format(SMTP_TIMEOUT); app.logger.warning("[{}] Email send timed out; continuing.".format(VERSION))
    except Exception as e:
        warn = "Email send failed ({}:{}): {}".format(SMTP_HOST, SMTP_PORT, e); app.logger.exception("[{}] Email send failed".format(VERSION))
    if warn: resp["warning"] = warn
    return jsonify(resp), 200

@app.post("/respond/<token>")
def respond_form(token):
    rec = load_record(token)
    if not rec: return render_result(False, "This proof link was not found.")
    decision = (request.form.get("decision") or "").lower()
    comment  = (request.form.get("comment")  or "").strip()
    viewer_name  = (request.form.get("viewer_name")  or "").strip()
    viewer_email = (request.form.get("viewer_email") or "").strip()
    if decision not in ("approved","rejected"):
        return render_result(False, "Invalid decision.", token=token, original_name=rec["original_name"])
    if decision == "rejected" and not comment:
        return render_result(False, "Please include a comment when rejecting.", token=token, original_name=rec["original_name"])
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    app.logger.info("[{}] /respond (form) token={} decision={} ip={} mode={}".format(VERSION, token, decision, ip, EMAIL_MODE))
    event = {"ts_utc": datetime.datetime.utcnow().isoformat()+"Z","decision":decision,"comment":comment,
             "viewer_name":viewer_name,"viewer_email":viewer_email,"ip":ip}
    rec["status"]=decision; rec["responses"].append(event); save_record(token, rec)
    proof_url = f"{BASE_URL}/proof/{token}"
    subject = "[Proof] {} — {}".format(rec['original_name'], decision.upper())
    text = ("Proof decision received.\n\nFile: {}\nLink: {}\nDecision: {}\nName: {}\nEmail: {}\nComment:\n{}\n\n"
            "Time (UTC): {}\nIP: {}\n").format(rec['original_name'], proof_url, decision, viewer_name, viewer_email, comment, event['ts_utc'], event['ip'])
    html = ("""<h2>Proof decision received</h2>
    <p><b>File:</b> {}</p><p><b>Link:</b> <a href="{}">{}</a></p><p><b>Decision:</b> {}</p>
    <p><b>Name:</b> {} &lt;{}&gt;</p><p><b>Comment:</b><br>{}</p><p><small>Time (UTC): {} | IP: {}</small></p>"""
            .format(rec['original_name'], proof_url, proof_url, decision, viewer_name, viewer_email, (comment or '').replace("\n","<br>"), event['ts_utc'], event['ip']))
    warning = ""
    if EMAIL_MODE == "off":
        app.logger.warning("[{}] EMAIL_MODE=off (skipping SMTP)".format(VERSION))
    elif EMAIL_MODE == "sync":
        try: send_email(subject, html, text)
        except Exception as e: app.logger.exception("Email send failed (sync form)"); warning = "Email send failed ({}:{}): {}".format(SMTP_HOST, SMTP_PORT, e)
    else:
        try:
            fut = executor.submit(send_email, subject, html, text); fut.result(timeout=SMTP_TIMEOUT)
        except FuturesTimeout:
            warning = "Email is sending in background (timeout {}s).".format(SMTP_TIMEOUT); app.logger.warning("[{}] Email send timed out; continuing.".format(VERSION))
        except Exception as e:
            warning = "Email send failed ({}:{}): {}".format(SMTP_HOST, SMTP_PORT, e); app.logger.exception("[{}] Email send failed".format(VERSION))
    return render_result(True, "Thank you — your decision was recorded.", warning, token, rec["original_name"])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
