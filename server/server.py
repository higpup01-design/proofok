import os, uuid, json, smtplib, datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate
from flask import Flask, request, send_from_directory, render_template, abort, jsonify, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:5000")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.colormagic.biz")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "PROOFS@colormagic.biz")
TO_EMAIL = os.getenv("TO_EMAIL", "orders@colormagic.biz")
SMTP_SSL = os.getenv("SMTP_SSL", "false").lower() == "true"

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "uploads")
app.config["DATA_FOLDER"] = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["DATA_FOLDER"], exist_ok=True)

def record_path(token: str) -> str:
    return os.path.join(app.config["DATA_FOLDER"], f"{token}.json")

def save_record(token: str, record: dict):
    with open(record_path(token), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)

def load_record(token: str) -> dict:
    path = record_path(token)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

@app.route("/")
def index():
    return redirect("https://colormagic.biz")

@app.post("/api/upload")
def api_upload():
    file = request.files.get("file")
    if not file or not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a .pdf file"}), 400

    original_name = request.form.get("original_name", file.filename)
    token = uuid.uuid4().hex[:12]
    token_dir = os.path.join(app.config["UPLOAD_FOLDER"], token)
    os.makedirs(token_dir, exist_ok=True)

    # Store PDF
    safe_name = original_name.replace("/", "_").replace("\\", "_")
    pdf_path = os.path.join(token_dir, safe_name)
    file.save(pdf_path)

    # Create record
    now = datetime.datetime.utcnow().isoformat() + "Z"
    rec = {
        "token": token,
        "original_name": original_name,
        "stored_name": safe_name,
        "created_utc": now,
        "status": "pending",
        "responses": []
    }
    save_record(token, rec)

    url = f"{BASE_URL}/proof/{token}"
    return jsonify({"ok": True, "token": token, "url": url})

@app.get("/proof/<token>")
def proof_page(token):
    rec = load_record(token)
    if not rec:
        abort(404)
    return render_template("proof.html",
                           token=token,
                           original_name=rec["original_name"],
                           pdf_url=url_for("serve_pdf", token=token, filename=rec["stored_name"], _external=False),
                           base_url=BASE_URL)

@app.get("/p/<token>/<path:filename>")
def serve_pdf(token, filename):
    folder = os.path.join(app.config["UPLOAD_FOLDER"], token)
    if not os.path.isdir(folder):
        abort(404)
    return send_from_directory(folder, filename, mimetype="application/pdf", as_attachment=False)

def send_email(subject: str, html: str, text: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = TO_EMAIL
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    if SMTP_SSL:
        import ssl
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as s:
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo()
            try:
                s.starttls()
            except Exception:
                pass
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

@app.post("/api/respond/<token>")
def api_respond(token):
    rec = load_record(token)
    if not rec:
        return jsonify({"error": "Not found"}), 404

    data = request.json if request.is_json else request.form
    decision = (data.get("decision") or "").lower()
    comment = data.get("comment", "").strip()
    viewer_name = data.get("viewer_name", "").strip()
    viewer_email = data.get("viewer_email", "").strip()

    if decision not in ("approved", "rejected"):
        return jsonify({"error": "decision must be 'approved' or 'rejected'"}), 400

    rec["status"] = decision
    event = {
        "ts_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "decision": decision,
        "comment": comment,
        "viewer_name": viewer_name,
        "viewer_email": viewer_email,
        "ip": (request.headers.get("X-Forwarded-For") or request.remote_addr)
    }
    rec["responses"].append(event)
    save_record(token, rec)

    proof_url = f"{BASE_URL}/proof/{token}"
    subject = f"[Proof] {rec['original_name']} � {decision.upper()}"
    text = f"""Proof decision received.

File: {rec['original_name']}
Link: {proof_url}
Decision: {decision}
Name: {viewer_name}
Email: {viewer_email}
Comment:
{comment}

Time (UTC): {event['ts_utc']}
IP: {event['ip']}
"""
    html = f"""
    <h2>Proof decision received</h2>
    <p><b>File:</b> {rec['original_name']}</p>
    <p><b>Link:</b> <a href="{proof_url}">{proof_url}</a></p>
    <p><b>Decision:</b> {decision}</p>
    <p><b>Name:</b> {viewer_name} &lt;{viewer_email}&gt;</p>
    <p><b>Comment:</b><br>{(comment or '').replace(chr(10), '<br>')}</p>
    <p><small>Time (UTC): {event['ts_utc']} | IP: {event['ip']}</small></p>
    """
    try:
        send_email(subject, html, text)
    except Exception as e:
        return jsonify({"ok": True, "warning": f"Email send failed: {e}"}), 200

    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
