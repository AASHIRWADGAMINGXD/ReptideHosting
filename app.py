from flask import Flask, redirect, request, session, url_for, render_template
import requests, os, pyrebase

# ================== CONFIG ==================
DISCORD_CLIENT_ID = "1409439148099764234"
DISCORD_CLIENT_SECRET = "YOUR_CLIENT_SECRET"  # Replace with your secret
DISCORD_REDIRECT_URI = "http://localhost:5000/callback"
DISCORD_API_BASE_URL = "https://discord.com/api"
# ============================================

# Firebase config
firebaseConfig = {
    "apiKey": "AIzaSyCK_lY0Xo8IWE05ttGyHxIlL7uNN-DeePU",
    "authDomain": "its-magic-helper-bf764.firebaseapp.com",
    "databaseURL": "https://its-magic-helper-bf764-default-rtdb.asia-southeast1.firebasedatabase.app",
    "storageBucket": "its-magic-helper-bf764.firebasestorage.app"
}

firebase = pyrebase.initialize_app(firebaseConfig)
db = firebase.database()

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ---------- ROUTES ----------
@app.route("/")
def index():
    if "discord_user" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")

@app.route("/login")
def login():
    auth_url = (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code&scope=identify"
    )
    return redirect(auth_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Error: No code provided by Discord"

    # Exchange code for access token
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "scope": "identify"
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(f"{DISCORD_API_BASE_URL}/oauth2/token", data=data, headers=headers)
    r.raise_for_status()
    access_token = r.json().get("access_token")

    # Get user info
    user = requests.get(
        f"{DISCORD_API_BASE_URL}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"}
    ).json()

    session["discord_user"] = user
    return redirect(url_for("dashboard"))

@app.route("/dashboard")
def dashboard():
    if "discord_user" not in session:
        return redirect(url_for("index"))
    return render_template("dashboard.html", user=session["discord_user"])

@app.route("/logout")
def logout():
    session.pop("discord_user", None)
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(debug=True)
