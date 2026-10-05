"""
KrishiGPT web app: animated landing page + chat.
The Groq API key stays on this server. It is never sent to the browser.

Files needed in the same folder:
    krishigpt_server.py   (this file)
    index.html
    krishigpt_groq.py     (your Groq agent; if you kept the old name, krishigpt.py also works)

Setup:
    pip install flask groq requests
    Create a file named .env next to this one containing one line:
        GROQ_API_KEY=gsk_your_key_here
Run:
    python krishigpt_server.py     then open http://127.0.0.1:5000
"""

import os
import sys
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request
from groq import Groq

HERE = Path(__file__).parent

# Load GROQ_API_KEY from a local .env file (so no `export` is needed each time).
env_file = HERE / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

try:
    from krishigpt_groq import ask
except ImportError:
    from krishigpt import ask

key = os.environ.get("GROQ_API_KEY")
if not key or key.startswith("PASTE"):
    sys.exit("Open the .env file in this folder, replace PASTE_YOUR_GROQ_KEY_HERE with your real Groq key, save it (Cmd+S), then run again.")

client = Groq(api_key=key)
app = Flask(__name__)
sessions: dict = {}


@app.get("/")
def index():
    return Response((HERE / "index.html").read_text(encoding="utf-8"), mimetype="text/html")


@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()[:1000]
    if not message:
        return jsonify(error="Please type a question."), 400

    if len(sessions) > 500:
        sessions.clear()
    sid = str(data.get("session") or uuid.uuid4())[:64]
    if len(sessions.get(sid, [])) > 40:
        sessions[sid] = []  # start fresh rather than cut tool-call pairs in half
    history = sessions.setdefault(sid, [])

    before = len(history)
    reply = ask(client, history, message)
    tools = [
        tc["function"]["name"]
        for m in history[before:]
        if m.get("role") == "assistant"
        for tc in m.get("tool_calls", [])
    ]
    return jsonify(reply=reply, tools=tools)


if __name__ == "__main__":
    print("KrishiGPT running at http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000)
