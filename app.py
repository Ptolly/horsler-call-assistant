# app.py — Twilio Media Streams ↔ AssemblyAI v3 (Universal-Streaming) ↔ GPT reply
import os
import json
import time
import ssl
import base64
import threading
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

from flask import Flask, Response, request as flask_request
from flask_sock import Sock
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client

# websocket-client
import websocket as ws_client  # optional tracing
from websocket import create_connection, ABNF
# ws_client.enableTrace(True)

# ============================== CONFIG ==============================
NGROK_BASE         = "https://a134e871c060.ngrok-free.app"  # Your actual ngrok URL
AAI_TOKEN          = os.getenv("AAI_TOKEN", "")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "")     # optional
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")

# AssemblyAI v3 Universal-Streaming endpoint (8k μ-law for Twilio media streams)
AAI_WSS_V3 = (
    "wss://streaming.assemblyai.com/v3/ws"
    "?sample_rate=8000&encoding=pcm_mulaw&format_turns=false"
)

twilio_client = (
    Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None
)

# ============================== SSL BYPASS FOR WINDOWS ==============================
# Create a more permissive SSL context
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

# Common SSL options for all connections
SSL_OPTS = {
    "cert_reqs": ssl.CERT_NONE,
    "check_hostname": False,
    "ssl_version": ssl.PROTOCOL_TLS
}

# ============================== APP SETUP ==============================
app  = Flask(__name__, static_url_path="/static", static_folder="static")
sock = Sock(app)

# Per-call state
SESSIONS = {}  # callSid -> {"transcript": str}

# ============================== GPT HELPER ==============================
def gpt_reply(full_transcript: str) -> str:
    safe = "Thanks — I heard you. How can I help you today?"
    text = (full_transcript or "").strip()
    if not text or not OPENAI_API_KEY:
        return safe
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        sys_prompt = "You are a helpful phone receptionist for Horsler Lift Services. Reply briefly and clearly."
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user",   "content": text[-1200:]},
            ],
            temperature=0.3,
            max_tokens=120,
        )
        return (resp.choices[0].message.content or safe).strip()
    except Exception as e:
        print("[GPT] error:", e)
        return safe

# ============================== ROUTE LIST (debug) ==============================
@app.route("/_routes", methods=["GET"])
def list_routes():
    lines = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        methods = ",".join(sorted(m for m in rule.methods if m in {"GET","POST","HEAD","OPTIONS"}))
        lines.append(f"{rule.rule}: [{methods}]")
    return "<br>".join(lines), 200

# ============================== SELFTEST (connect only) ==============================
@app.route("/aai_selftest", methods=["GET"])
def aai_selftest():
    if not AAI_TOKEN:
        return "No AAI_TOKEN set", 500
    try:
        print("[SELFTEST] Attempting connection to AAI...")
        aai = create_connection(
            AAI_WSS_V3,
            header=[f"Authorization: {AAI_TOKEN}"],
            timeout=20,
            sslopt=SSL_OPTS
        )
        try:
            # Wait briefly for server Begin event
            aai.settimeout(2.0)
            msg = aai.recv()
            print("[SELFTEST] Received from AAI:", msg)
        except Exception as e:
            print("[SELFTEST] Receive timeout/error:", e)
        finally:
            aai.close()
        return "OK: connected to AAI realtime (model=universal, 8k ulaw).", 200
    except Exception as e:
        print(f"[SELFTEST] Connection failed: {e}")
        return f"Selftest error: {e}", 500

# ============================== VOICE WEBHOOK ==============================
@app.route("/voice", methods=["POST"])
def voice():
    call_sid = flask_request.form.get("CallSid", "")
    ws_host  = NGROK_BASE.replace("https://", "").replace("http://", "")
    ws_url   = f"wss://{ws_host}/aai?callSid={call_sid}"

    print(f"[VOICE] callSid={call_sid}")
    print(f"[VOICE] will stream to: {ws_url}")

    r = VoiceResponse()

    connect = r.connect()
    connect.stream(url=ws_url)  # IMPORTANT: no 'track' param

    r.say(
        "Thank you for calling Horsler Lift Services — the lift company that always gives a duck. "
        "Kindly note, calls are recorded for training and monitoring purposes. "
        "You are speaking with an automated assistant, so please speak clearly and wait for me to finish speaking — "
        "that helps me understand you better. How can I help you today?",
        language="en-GB",
    )

    r.pause(length=600)
    return Response(str(r), mimetype="text/xml")

@app.route("/voice_stream_continue", methods=["GET","POST"])
def voice_stream_continue():
    call_sid = flask_request.values.get("callSid", "")
    ws_host  = NGROK_BASE.replace("https://", "").replace("http://", "")
    ws_url   = f"wss://{ws_host}/aai?callSid={call_sid}"
    print(f"[VOICE_CONTINUE] re-opening stream → {ws_url}")

    r = VoiceResponse()
    connect = r.connect()
    connect.stream(url=ws_url)
    r.pause(length=600)
    return Response(str(r), mimetype="text/xml")

# ============================== TWILIO <-> AAI v3 BRIDGE ==============================
@sock.route("/aai")
def aai_ws(twilio_ws):
    """
    Twilio -> us: read twilio_ws messages (media, start, stop).
    us -> AAI v3: send raw μ-law bytes as WebSocket binary frames.
    AAI -> us: read 'Turn' messages; on end_of_turn, speak a reply and redirect to keep streaming.
    """
    call_sid = flask_request.args.get("callSid", "")
    print(f"[/aai] WebSocket OPEN. callSid={call_sid}")

    if not AAI_TOKEN:
        print("ERROR: AAI_TOKEN is not set")
        twilio_ws.close()
        return

    SESSIONS.setdefault(call_sid, {"transcript": ""})
    call_sid_holder = {"sid": call_sid}

    # 1) Connect to AAI v3 with comprehensive SSL bypass
    try:
        print("[AAI] Attempting connection with SSL bypass...")
        aai = create_connection(
            AAI_WSS_V3,
            header=[f"Authorization: {AAI_TOKEN}"],
            timeout=30,
            sslopt=SSL_OPTS
        )
        print("[AAI] connected (v3) - SSL bypass successful")
    except Exception as e:
        print(f"[AAI] connect failed: {e}")
        print(f"[AAI] Error type: {type(e).__name__}")
        return

    # 2) Reader thread: listen for AAI 'Turn' messages
    def aai_reader():
        try:
            while True:
                msg = aai.recv()
                if not msg:
                    break
                try:
                    evt = json.loads(msg)
                except Exception as ex:
                    # v3 control frames are JSON; media is binary (we only send)
                    print("[AAI] non-JSON or bad JSON:", ex)
                    continue

                mtype = evt.get("type")
                if mtype == "Begin":
                    print("[AAI] Session started")
                    continue
                if mtype == "Turn":
                    transcript = (evt.get("transcript") or "").strip()
                    if transcript:
                        SESSIONS[call_sid]["transcript"] += ("\n" + transcript)
                        print("[AAI TURN]", transcript)

                    # At end_of_turn, send reply
                    if evt.get("end_of_turn"):
                        reply_text = gpt_reply(SESSIONS[call_sid]["transcript"])
                        sid = call_sid_holder["sid"]
                        if sid and twilio_client and reply_text:
                            twiml = (
                                f"<Response>"
                                f"<Say language='en-GB'>{reply_text}</Say>"
                                f"<Redirect method='POST'>/voice_stream_continue?callSid={sid}</Redirect>"
                                f"</Response>"
                            )
                            try:
                                twilio_client.calls(sid).update(twiml=twiml)
                                print("[Twilio] pushed reply + redirect for", sid)
                            except Exception as ex:
                                print("[Twilio] update error:", ex)

                elif mtype == "Termination":
                    print("[AAI] termination:", evt)
                    break
                else:
                    # Other event types are fine to ignore for now
                    pass

        except Exception as e:
            print("[AAI reader] exiting:", e)
        finally:
            try:
                aai.close()
            except:
                pass

    threading.Thread(target=aai_reader, daemon=True).start()

    # 3) Main loop: Twilio -> AAI (send μ-law frames as binary)
    try:
        while True:
            msg = twilio_ws.receive()
            if msg is None:
                break

            try:
                data = json.loads(msg)
            except Exception as ex:
                print("[/aai] bad JSON from Twilio:", ex)
                continue

            evt = data.get("event")

            if evt == "start":
                print("[/aai] Twilio stream started")
                cp = (data.get("start") or {}).get("customParameters") or {}
                call_sid_holder["sid"] = cp.get("callSid") or call_sid

            elif evt == "media":
                # Twilio's payload is base64 μ-law @ 8k
                try:
                    b = base64.b64decode(data["media"]["payload"])
                    # Send as binary (NO JSON WRAPPER) — this is the key change for v3
                    aai.send(b, opcode=ABNF.OPCODE_BINARY)
                except Exception as ex:
                    print("[bridge -> AAI] send failed:", ex)
                    break

            elif evt == "stop":
                print("[/aai] Twilio stream stop")
                try:
                    aai.send(json.dumps({"type": "Terminate"}))
                except Exception:
                    pass
                break

    except Exception as ex:
        print("[bridge_stream ERROR]:", ex)
    finally:
        try:
            aai.close()
        except:
            pass
        try:
            twilio_ws.close()
        except:
            pass

    print(f"[/aai] WebSocket CLOSED. callSid={call_sid}")

# ============================== HEALTH ==============================
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

# ============================== RUN (Werkzeug, no gevent) ==============================
if __name__ == "__main__":
    print("Static dir:", os.path.abspath("static"))
    try:
        print("Static files:", os.listdir("static"))
    except Exception:
        print("Static files: (none)")
    print("[VOICE] will stream to:", f"wss://{NGROK_BASE.replace('https://','').replace('http://','')}/aai?callSid=<example>")

    # Test SSL connection on startup
    print("\n=== SSL CONNECTION TEST ===")
    if AAI_TOKEN:
        try:
            test_conn = create_connection(
                AAI_WSS_V3,
                header=[f"Authorization: {AAI_TOKEN}"],
                timeout=10,
                sslopt=SSL_OPTS
            )
            print("✓ SSL connection to AssemblyAI successful!")
            test_conn.close()
        except Exception as e:
            print(f"✗ SSL connection failed: {e}")
    else:
        print("⚠ No AAI_TOKEN set - skipping SSL test")
    print("================================\n")

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)