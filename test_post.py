import requests

BASE = "https://2ea873c1049c.ngrok-free.app"   # <- update when ngrok URL changes

# 1) Hit your /voice endpoint (simulates Twilio). Should print TwiML.
twiml = requests.post(f"{BASE}/voice").text
print("---- /voice TwiML ----")
print(twiml)

# 2) Check your intro audio file is reachable (should be HTTP 200).
intro = requests.get(f"{BASE}/static/after_hours_intro.mp3")
print("\nIntro audio status:", intro.status_code)

# 3) Check (optional) hold file is reachable (should be HTTP 200 if present).
hold = requests.get(f"{BASE}/static/hold.mp3")
print("Hold audio status:", hold.status_code)