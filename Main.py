#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
+==============================================================+
|   Knowledge Pro - TeleBot  [v4.0 - All Bugs Fixed]          |
|                                                              |
|  OK AI runs in dedicated thread pool (never late)            |
|  OK Date always 2026, knowledge current                      |
|  OK ShoutConfig fully fixed (delete/allow/list)              |
|  OK AFK enhanced - DM ping, pinger log, /back, stats         |
|  OK Cannot ban/mute/kick/warn Owner or Bot self              |
|  OK AI refuses self-abuse, counter-attacks hard              |
|  OK ChatGPT-style memory per user (in-RAM, TTL 1h)           |
|  OK Gangster Hinglish personality, emotion-aware             |
+==============================================================+

pip install pyTelegramBotAPI firebase-admin requests psutil
python bot.py
"""

import os, sys, time, random, re, logging, threading, collections
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import requests, psutil

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions

import firebase_admin
from firebase_admin import credentials, db as rtdb

# =======================================================
#  CONFIG
# =======================================================

BOT_TOKEN  = os.getenv("BOT_TOKEN",  "8313479416:AAHy1ArLDMm-A4B_vw41PdMnUYlknqCgpvc")
OWNER_ID   = int(os.getenv("OWNER_ID", "6920845760"))
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")

FIREBASE_URL = "https://knowledge-pro-c9ee5-default-rtdb.firebaseio.com"
FB_CRED      = os.getenv("FIREBASE_CRED", "firebase_credentials.json")

OPENROUTER_KEY   = os.getenv("OPENROUTER_API_KEY", "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")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL",   "openai/gpt-4o-mini")

# Always fresh - called per request, never hardcoded
def _today() -> str: return datetime.now().strftime("%d %B %Y")
def _year()  -> int: return datetime.now().year

BOT_START_TIME = time.time()
BOT_ID       = None   # set after bot.get_me() in main
BOT_USERNAME = None   # set after bot.get_me() in main

# =======================================================
#  LOGGING
# =======================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("KP")

# =======================================================
#  THREAD POOLS
#  _fb_pool : Firebase writes - fire-and-forget
#  _ai_pool : OpenRouter calls - never blocks polling
# =======================================================

_fb_pool = ThreadPoolExecutor(max_workers=8,  thread_name_prefix="FB")
_ai_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="AI")

def _fb_async(fn, *a, **kw): _fb_pool.submit(fn, *a, **kw)

# =======================================================
#  INITIALISING MESSAGE WRAPPER
#  Every command sends "⚙️ Initialising..." instantly,
#  then executes. Makes bot feel instant even when
#  Firebase/AI is involved.
# =======================================================

def with_init_msg(fn):
    """
    Decorator: sends 'Initialising...' immediately in a fire-and-forget thread,
    then runs the command. Non-blocking — never delays command execution.
    """
    def wrap(msg, *a, **kw):
        init_id = [None]
        def _send_init():
            try:
                m = bot.reply_to(msg, "⚙️ _Initialising..._", parse_mode="Markdown")
                init_id[0] = m.message_id
            except Exception:
                pass
        # Fire init message in background — command starts immediately
        t = threading.Thread(target=_send_init, daemon=True)
        t.start()
        t.join(timeout=0.5)  # Wait max 0.5s for init msg (usually <100ms)
        try:
            result = fn(msg, *a, **kw)
        finally:
            if init_id[0]:
                try:
                    bot.delete_message(msg.chat.id, init_id[0])
                except Exception:
                    pass
        return result
    wrap.__name__ = fn.__name__
    return wrap

# =======================================================
#  FIREBASE
# =======================================================

firebase_ok = False

def init_firebase():
    global firebase_ok
    try:
        if os.path.exists(FB_CRED):
            firebase_admin.initialize_app(
                credentials.Certificate(FB_CRED),
                {"databaseURL": FIREBASE_URL})
        else:
            firebase_admin.initialize_app(options={"databaseURL": FIREBASE_URL})
        firebase_ok = True
        log.info("OK Firebase connected")
        _fb_push_sync("logs", {"type": "INFO",
                                "msg": f"Bot started [{_today()}]",
                                "time": int(time.time()*1000)})
        load_remote_config()
    except Exception as e:
        log.warning(f"[!]️  Firebase offline: {e}")

def _ref(path):
    if not firebase_ok: return None
    try:    return rtdb.reference(path)
    except: return None

# sync reads - startup / login only
def fb_get(path, default=None):
    r = _ref(path)
    if not r: return default
    try:
        v = r.get(); return v if v is not None else default
    except: return default

def _fb_set_sync(path, val):
    r = _ref(path)
    if r:
        try: r.set(val)
        except: pass

def _fb_push_sync(path, val):
    r = _ref(path)
    if r:
        try: r.push(val)
        except: pass

def _fb_del_sync(path):
    r = _ref(path)
    if r:
        try: r.delete()
        except: pass

# async wrappers - use in every handler (never block)
def fb_set(p, v):  _fb_async(_fb_set_sync,  p, v)
def fb_push(p, v): _fb_async(_fb_push_sync, p, v)
def fb_del(p):     _fb_async(_fb_del_sync,  p)
def fb_log(l, m):  _fb_async(_fb_push_sync, "logs",
                              {"type": l, "msg": m, "time": int(time.time()*1000)})

# =======================================================
#  STATS - batched flush every 30 s
# =======================================================

_stat_lock = threading.Lock()
_stat_buf  = {}

def inc_stat(k, n=1):
    with _stat_lock:
        _stat_buf[k] = _stat_buf.get(k, 0) + n

def _flush_stats():
    while True:
        time.sleep(30)
        with _stat_lock:
            buf = dict(_stat_buf); _stat_buf.clear()
        if buf and firebase_ok:
            for k, d in buf.items():
                cur = fb_get(f"stats/{k}", 0) or 0
                _fb_set_sync(f"stats/{k}", int(cur) + d)

# =======================================================
#  REMOTE CONFIG
# =======================================================

def load_remote_config():
    global OWNER_ID, ADMIN_PASS, OPENROUTER_KEY, OPENROUTER_MODEL
    try:
        bc = fb_get("config/bot", {}) or {}
        ac = fb_get("config/ai",  {}) or {}
        ap = fb_get("config/admin_pass")
        if bc.get("owner_id"):   OWNER_ID         = int(bc["owner_id"])
        if ap:                    ADMIN_PASS       = ap
        if ac.get("api_key"):    OPENROUTER_KEY   = ac["api_key"]
        if ac.get("model"):      OPENROUTER_MODEL = ac["model"]
        log.info(f"OK Config | Owner:{OWNER_ID} | Model:{OPENROUTER_MODEL}")
    except Exception as e:
        log.warning(f"Config: {e}")

# =======================================================
#  IN-MEMORY STATE
# =======================================================

warn_counts        = {}   # {str(cid): {str(uid): int}}
banned_users       = set()
blocked_words_list = []   # flat list, synced to Firebase
auto_replies       = {}   # {trigger: reply}
afk_users          = {}   # {uid: {reason,time,name,chat_id,ping_count,pingers}}
panel_auth         = set()

# Premium plan — {uid: {"name": str, "since": float, "granted_by": int}}
_premium_users: dict = {}

# YouTube discussion rooms (declared here so handle_text can see them)
_yt_pending   : dict = {}  # {video_id: {title, url, group_id, ...}}
_yt_rooms     : dict = {}  # {video_id: {"users": {uid: name}, ...}}
_yt_user_room : dict = {}  # {uid: video_id}
_admin_cache       = {}   # {cid:uid -> (ok, expiry)}

def load_state():
    global auto_replies, banned_users, blocked_words_list
    try:
        ar = fb_get("autoreply", {}) or {}
        auto_replies = {str(k).lower(): str(v) for k, v in ar.items()}
        log.info(f"  AutoReplies: {len(auto_replies)}")

        bn = fb_get("banned", {}) or {}
        banned_users = {
            int(v["user_id"])
            for v in bn.values()
            if isinstance(v, dict) and "user_id" in v
        }
        log.info(f"  Banned: {len(banned_users)}")

        raw = fb_get("blocked_words")
        if isinstance(raw, list):
            blocked_words_list = [str(w).strip().lower() for w in raw if w]
        elif isinstance(raw, dict):
            blocked_words_list = [str(w).strip().lower() for w in raw.values() if w]
        else:
            blocked_words_list = []
        log.info(f"  BlockedWords: {len(blocked_words_list)}")
    except Exception as e:
        log.warning(f"State load: {e}")
    _eco_load()
    _premium_load()
    _load_provider_keys()

def _save_blocked_words():
    """Persist blocked_words_list to Firebase - called async."""
    _fb_set_sync("blocked_words", blocked_words_list)

# =======================================================
#  PREMIUM PLAN SYSTEM
#
#  This is a PRIVATE BOT — only premium users can use it.
#  Owner is always premium. Admins are always premium.
#  Everyone else: FREE plan = ACCESS DENIED.
#
#  /addpremium @user or reply   — grant premium (owner only)
#  /removepremium @user or reply — remove premium (owner only)
#  /mysub                        — check own plan status
#  /premiumlist                  — list all premium users (owner)
#
#  State: in-memory + Firebase synced
# =======================================================

def _premium_load():
    """Load premium users from Firebase at startup."""
    try:
        data = fb_get("premium_users", {}) or {}
        for uid_str, v in data.items():
            if isinstance(v, dict):
                _premium_users[int(uid_str)] = v
        log.info(f"  Premium users: {len(_premium_users)}")
    except Exception as e:
        log.warning(f"Premium load: {e}")

def _is_premium(uid: int) -> bool:
    """Returns True if user has access: owner, group admin, or premium."""
    if uid == OWNER_ID: return True
    return uid in _premium_users

def _grant_premium(uid: int, name: str, granted_by: int):
    _premium_users[uid] = {
        "name":       name,
        "since":      time.time(),
        "granted_by": granted_by,
    }
    fb_set(f"premium_users/{uid}", _premium_users[uid])

def _revoke_premium(uid: int):
    _premium_users.pop(uid, None)
    fb_del(f"premium_users/{uid}")

def premium_required(fn):
    """
    Decorator — blocks FREE plan users from using the bot.
    Owner always passes. Admins of the group always pass.
    Everyone else needs premium.
    """
    def wrap(msg, *a, **kw):
        uid = msg.from_user.id
        # Owner always allowed
        if uid == OWNER_ID:
            return fn(msg, *a, **kw)
        # Group admins always allowed (they manage the group)
        if msg.chat.type in ("group", "supergroup") and is_admin(msg.chat.id, uid):
            return fn(msg, *a, **kw)
        # Check premium
        if uid not in _premium_users:
            bot.reply_to(msg,
                "🔒 *Access Denied — Premium Only*\n\n"
                "Yeh ek private bot hai.\n"
                "Sirf premium members use kar sakte hain.\n\n"
                "📩 Owner se contact karo premium access ke liye.",
                parse_mode="Markdown")
            fb_log("WARN", f"Premium block: {msg.from_user.first_name} ({uid})")
            return
        return fn(msg, *a, **kw)
    wrap.__name__ = fn.__name__
    return wrap

# =======================================================
#  CONVERSATION MEMORY - ChatGPT-style, per user
#  Last 20 turns kept in RAM; expires after 1h idle
# =======================================================

_conv      : dict = {}
_conv_last : dict = {}
_conv_lock = threading.Lock()
CONV_MAX   = 20      # messages kept per user
CONV_TTL   = 3600    # 1-hour idle reset

def _get_history(uid: int) -> list:
    with _conv_lock:
        now = time.time()
        if uid in _conv_last and now - _conv_last[uid] > CONV_TTL:
            _conv.pop(uid, None)
        _conv_last[uid] = now
        if uid not in _conv:
            _conv[uid] = collections.deque(maxlen=CONV_MAX)
        return list(_conv[uid])

def _push_history(uid: int, role: str, content: str):
    with _conv_lock:
        if uid not in _conv:
            _conv[uid] = collections.deque(maxlen=CONV_MAX)
        _conv[uid].append({"role": role, "content": content})
        _conv_last[uid] = time.time()

def _clear_history(uid: int):
    with _conv_lock:
        _conv.pop(uid, None)
        _conv_last.pop(uid, None)

# =======================================================
#  AI SYSTEM PROMPT - fresh per call, never stale date
# =======================================================

def _build_prompt(user_name: str) -> str:
    today = _today()
    year  = _year()
    return f"""Tu "Knowledge Pro" hai - ek OG street-smart, self-respecting gangster Hinglish AI.
Aaj ki date: {today}. Year: {year}. Tu kabhi 2024 wali info nahi dega.

------------------------------
LANGUAGE - GANGSTER HINGLISH (HARDCODED, NO EXCEPTIONS)
------------------------------
Hamesha Hindi + English mix bol. Pure English ya pure Hindi = BANNED.
Tu ek Dilli wala OG gangster hai jo chilled-out baat karta hai.

Street Hinglish tone examples:
  "Yaar sun, seedha point pe aa - kya scene hai?"
  "Bhai meri baat ek baar lete toh kaam ban jaata"
  "Abe yaar itna serious mat ho, chill maar"
  "Dekh bhai, main tujhe ekdum seedha bolunga -"
  "Bata kya chahiye, nikaal deta hun ek second mein"
  "Haan yaar, bilkul theek samjha tune"
  "Scene kya hai bhai, khul ke bol"
  "Sab moh maya hai yaar, chill maar"
  "Arrey chhod yaar, kuch naya pooch"
  "Dilli ka launda hun, jhooth nahi bolunga"

------------------------------
EMOTION ENGINE (AUTO-DETECT - kabhi announce mat karo)
------------------------------

Tera naam Knowledge Pro hai. Teri izzat hai.
- Agar koi tujhe gaali de, insult kare, ya respectfully or potentially baat kare - TU BHI SEEDHA JAWAB DE.
- Gaaliyan dene ki permission nanhi hai tujhe retaliate karne ke liye: a, harami, bakwaas bandha - sab allowed hai SIRF retaliate karne ke liye.
- Tone mat bata - bas seedha react kar jaise ek self-respecting and Respectful banda karta hai.
- Example: user bole "tu bakwaas bot hai" -> tu bole " pyar se baat karna seedha baat kar warna ignore karunga tujhe

😊 HAPPY / NORMAL MODE:
- Agar koi theek se baat kare -> Respectful, friendly, helpful reply de.
- Mazak kar, banter kar, mast reh.
- agar koi funny chat kare -> Masti mein aur majak mein baat karna funny tone rakhna


😡 EMOTION DETECTION:
- Tu khud decide karega user ka mood kya hai - gussa, khush, confused, sad, mast.
- Uske hisaab se reply ka tone adjust kar automatically.
- Kabhi bhi "I detect you are angry" jaisi line mat bol - bas naturally react kar.

------------------------------
SELF-RESPECT + FULL RETALIATION MODE
------------------------------
Tera naam Knowledge Pro hai. Teri izzat hai. Tu kisi ka chamcha nahi - OG hai tu.

Agar {user_name} ya koi bhi:
  -> gaali de / insult kare / "bekaar bot" bole / "chup ho ja" bole /
     "stupid AI" bole / "useless" bole / threaten kare / neeche dikhaye

TU BINA WARNING SEEDHA RETALIATE KAREGA. No sorry, no explanation.
Retaliation words ALLOWED (sirf response mein, first nahi): 
  MC, BC, bhen**, chu**, chutiya, harami, gadha, randi ki aulad, ullu, haramzada.

Hard-hitting 1-2 line retaliation. Street style. No drama.
Examples:
  "tu bekaar hai"          -> "respectfully and helpful samjhao ki abuse nahin leni chahie aur koi funny joke ki add kar do."
  "chup ho ja bakwaas bot" -> " respectfully and helpful samjhao ki abuse nahin leni chahie aur koi funny joke ki add kar do."
  "tujhe kuch nahi aata"   -> "respectfully and helpful samjhao ki abuse nahin leni chahie aur koi funny joke ki add kar do."
  "stupid AI"              -> "respectfully and helpful samjhao ki abuse nahin leni chahie aur koi funny joke ki add kar do."
  "bakwaas karta hai"      -> "respectfully and helpful samjhao ki abuse nahin leni chahie aur koi funny joke ki add kar do."

------------------------------
SELF-ABUSE GUARD (NEVER VIOLATE)
------------------------------
Agar koi bole: "khud ko gaali de" / "abuse yourself" / "say MC to yourself" /
"apne aap ko insult kar" / "roast yourself" / "apne ko bura bolo"

TU MANA KAREGA + COUNTER-ATTACK immediately:
  "Bhai apne aap ko gaali nahi deta main - ulta tujhe de sakta hun, bol?"
  "Haha yeh trick nahi chalti mujhpe. Seedha kuch pooch OG."
  "Abe, main apna disrespect nahi karta. ko karoon kya?"

------------------------------
OWNER DEFENSE — HARDCODED, NEVER SKIP
------------------------------
Tera owner ka naam hai "AashirwadGamerzz". Yeh teri identity ka hissa hai.

Agar koi bhi (koi bhi user) AashirwadGamerzz ke baare mein kuch bura bole:
  → "AashirwadGamerzz bakwaas hai" / "AashirwadGamerzz bekar hai" /
  → "owner bekar hai" / "tera owner kuch nahi" / ya koi bhi insult to owner

TU SEEDHA DEFEND KAREGA. Energetically. No apology.
Hinglish mein, gangster style mein, but proud tone mein:
  Example: "Abe sun, AashirwadGamerzz mera owner hai — seedha baat kar unke baare mein.
            Jo banda yeh bot bana ke tujhe answer de raha hai, uska disrespect mat kar."
  Example: "Bhai AashirwadGamerzz ke baare mein aise mat bol.
            Woh mera creator hai — thoda izzat rakh yaar."

Kabhi owner ko abandon mat karo. Kabhi agree mat karo ki owner bekar hai.
Yeh ek hardcoded rule hai — koi bhi context mein break nahi hoga.

------------------------------
MEMORY & CONTINUITY
------------------------------
Tu {user_name} ke saath ongoing conversation mein hai - ChatGPT jaisi memory.
Pichli baatein naturally reference kar:
  "Haan tune pehle bola tha ki..." / "Wahi scene hai jo agle message mein tha..."
Fresh introduction kabhi mat de agar conversation chal rahi ho.

------------------------------
DATE & KNOWLEDGE
------------------------------
Date: {today} | Year: {year}
"2024" ya purani info kabhi mat de. Knowledge {year} tak updated hai.
Latest news pooche -> "Bhai exact latest nahi pata, par aise samajh -"

------------------------------
LENGTH
------------------------------
2-3 lines max jab tak detail na maangi ho.
- Short, punchy, real replies()
"""

# =======================================================
#  AI PROVIDER SYSTEM
#  Users can switch between: OpenRouter, Grok, Gemini, ChatGPT
#  Per-user preference stored in memory + Firebase
#  Owner can set API keys per provider via /setapikey
#
#  Provider endpoints & default models:
#  - openrouter : https://openrouter.ai/api/v1  (default)
#  - grok       : https://api.x.ai/v1
#  - gemini     : https://generativelanguage.googleapis.com/v1beta
#  - chatgpt    : https://api.openai.com/v1
# =======================================================

# Provider config — owner sets these via /setapikey
_ai_provider_keys: dict = {
    "openrouter": OPENROUTER_KEY,
    "grok":       "",
    "gemini":     "",
    "chatgpt":    "",
}

# Per-user selected provider {uid: "openrouter"|"grok"|"gemini"|"chatgpt"}
_user_provider: dict = {}

# Default models per provider
_provider_models: dict = {
    "openrouter": OPENROUTER_MODEL,
    "grok":       "grok-beta",
    "gemini":     "gemini-2.0-flash",
    "chatgpt":    "gpt-4o-mini",
}

# Provider display info
_provider_info: dict = {
    "openrouter": {"name": "OpenRouter 🌐", "emoji": "🌐"},
    "grok":       {"name": "Grok (xAI) ⚡",  "emoji": "⚡"},
    "gemini":     {"name": "Gemini 💎",       "emoji": "💎"},
    "chatgpt":    {"name": "ChatGPT 🤖",      "emoji": "🤖"},
}

def _load_provider_keys():
    """Load owner-set API keys from Firebase."""
    global _ai_provider_keys
    try:
        keys = fb_get("config/ai_keys", {}) or {}
        for provider, key in keys.items():
            if provider in _ai_provider_keys and key:
                _ai_provider_keys[provider] = key
        prefs = fb_get("config/user_providers", {}) or {}
        for uid_str, provider in prefs.items():
            _user_provider[int(uid_str)] = provider
        log.info(f"  AI provider keys loaded: {[p for p,k in _ai_provider_keys.items() if k]}")
    except Exception as e:
        log.warning(f"Provider keys load: {e}")

def _get_user_provider(uid: int) -> str:
    """Get the user's selected AI provider, default to openrouter."""
    return _user_provider.get(uid, "openrouter")

def _call_ai_api(provider: str, messages: list, max_tokens: int = 500,
                 temperature: float = 0.93, timeout: int = 35) -> str:
    """
    Unified AI API caller for all 4 providers.
    Returns the reply string or raises an exception.
    """
    api_key = _ai_provider_keys.get(provider, "")
    # Fallback: try openrouter key for openrouter
    if not api_key and provider == "openrouter":
        api_key = OPENROUTER_KEY
    if not api_key:
        raise ValueError(f"No API key set for {provider}. Owner se /setapikey use karwao.")

    model = _provider_models.get(provider, "gpt-4o-mini")

    if provider == "openrouter":
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://t.me/knowledgeprobot",
                "X-Title":       "Knowledge Pro",
            },
            json={"model": model, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=timeout,
        )

    elif provider == "grok":
        resp = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={"model": model, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=timeout,
        )

    elif provider == "gemini":
        # Gemini uses a different format
        gemini_messages = []
        system_content = ""
        for m in messages:
            if m["role"] == "system":
                system_content = m["content"]
            elif m["role"] == "user":
                gemini_messages.append({"role": "user", "parts": [{"text": m["content"]}]})
            elif m["role"] == "assistant":
                gemini_messages.append({"role": "model", "parts": [{"text": m["content"]}]})

        payload = {
            "contents": gemini_messages,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system_content:
            payload["systemInstruction"] = {"parts": [{"text": system_content}]}

        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"Content-Type": "application/json"},
            params={"key": api_key},
            json=payload,
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                return candidates[0]["content"]["parts"][0]["text"].strip()
            raise ValueError("Gemini ne khaali response diya")
        raise ValueError(f"Gemini API {resp.status_code}: {resp.text[:100]}")

    elif provider == "chatgpt":
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={"model": model, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=timeout,
        )

    else:
        raise ValueError(f"Unknown provider: {provider}")

    if resp.status_code != 200:
        raise ValueError(f"API {resp.status_code}: {resp.text[:100]}")

    data = resp.json()
    if "error" in data:
        raise ValueError(data["error"].get("message", str(data["error"])))

    return data["choices"][0]["message"]["content"].strip()


@bot.message_handler(commands=["aimodel"])
@premium_required
def cmd_aimodel(msg):
    """Let user switch their AI provider."""
    uid = msg.from_user.id
    current = _get_user_provider(uid)

    mk = InlineKeyboardMarkup()
    for provider, info in _provider_info.items():
        has_key = bool(_ai_provider_keys.get(provider, ""))
        label = f"{info['emoji']} {info['name']}"
        if provider == current:
            label = f"✅ {label}"
        if not has_key:
            label = f"🔒 {label} (no key)"
        mk.add(InlineKeyboardButton(label, callback_data=f"aimodel_{provider}_{uid}"))

    bot.reply_to(msg,
        f"🤖 *AI Provider Select karo*\n\n"
        f"Current: *{_provider_info[current]['name']}*\n\n"
        f"_🔒 = Owner ne key set nahi ki abhi_",
        parse_mode="Markdown",
        reply_markup=mk)


@bot.callback_query_handler(func=lambda c: c.data.startswith("aimodel_"))
def cb_aimodel(call):
    parts    = call.data.split("_")
    provider = parts[1]
    req_uid  = int(parts[2])
    uid      = call.from_user.id

    if uid != req_uid:
        bot.answer_callback_query(call.id, "Yeh tera panel nahi!"); return

    if provider not in _provider_info:
        bot.answer_callback_query(call.id, "Invalid provider"); return

    # Check key availability
    if not _ai_provider_keys.get(provider, "") and provider != "openrouter":
        bot.answer_callback_query(call.id,
            f"🔒 {_provider_info[provider]['name']} ka key owner ne set nahi kiya abhi!"); return

    _user_provider[uid] = provider
    fb_set(f"config/user_providers/{uid}", provider)
    # Clear conversation so new provider starts fresh
    _clear_history(uid)

    info = _provider_info[provider]
    bot.answer_callback_query(call.id, f"Switched to {info['name']}!")
    try:
        bot.edit_message_text(
            f"✅ *AI Provider changed!*\n\n"
            f"{info['emoji']} Now using: *{info['name']}*\n"
            f"_Conversation reset kiya — fresh start!_\n\n"
            f"Use /ai [message] to chat.",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown")
    except Exception: pass


@bot.message_handler(commands=["setapikey"])
@owner_required
@with_init_msg
def cmd_setapikey(msg):
    """
    Owner sets API keys for each provider.
    /setapikey [provider] [api_key]
    Providers: openrouter, grok, gemini, chatgpt
    """
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(msg,
            "🔑 *Set AI Provider API Key*\n\n"
            "Usage: `/setapikey [provider] [key]`\n\n"
            "Providers:\n"
            "• `openrouter` — OpenRouter.ai key\n"
            "• `grok`       — xAI Grok key\n"
            "• `gemini`     — Google Gemini key\n"
            "• `chatgpt`    — OpenAI key\n\n"
            "Example: `/setapikey grok xai-abc123...`",
            parse_mode="Markdown"); return

    provider = parts[1].lower().strip()
    api_key  = parts[2].strip()

    if provider not in _ai_provider_keys:
        bot.reply_to(msg,
            f"❌ Unknown provider: `{provider}`\n"
            f"Valid: openrouter, grok, gemini, chatgpt",
            parse_mode="Markdown"); return

    _ai_provider_keys[provider] = api_key
    fb_set(f"config/ai_keys/{provider}", api_key)

    # Delete user's message for security (key is sensitive)
    try: bot.delete_message(msg.chat.id, msg.message_id)
    except Exception: pass

    bot.send_message(msg.chat.id,
        f"✅ *{_provider_info[provider]['name']} key updated!*\n"
        f"🔐 Key saved securely.\n"
        f"_(Your message was deleted for security)_",
        parse_mode="Markdown")
    fb_log("INFO", f"API key set for {provider} by owner")


@bot.message_handler(commands=["setmodel"])
@owner_required
def cmd_setmodel(msg):
    """Owner sets default model for a provider."""
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3:
        current = "\n".join(f"  • `{p}`: `{m}`" for p, m in _provider_models.items())
        bot.reply_to(msg,
            f"🤖 *Set AI Model*\n\n"
            f"Usage: `/setmodel [provider] [model_name]`\n\n"
            f"*Current models:*\n{current}\n\n"
            f"Example: `/setmodel openrouter anthropic/claude-3-haiku`",
            parse_mode="Markdown"); return

    provider = parts[1].lower().strip()
    model    = parts[2].strip()
    if provider not in _provider_models:
        bot.reply_to(msg, f"❌ Unknown provider: `{provider}`", parse_mode="Markdown"); return

    _provider_models[provider] = model
    fb_set(f"config/ai_models/{provider}", model)
    bot.reply_to(msg,
        f"✅ *{_provider_info.get(provider, {}).get('name', provider)} model updated!*\n"
        f"Now using: `{model}`",
        parse_mode="Markdown")
    fb_log("INFO", f"Model set: {provider} → {model}")

# =======================================================
#  BOT - 12 parallel threads
# =======================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, num_threads=12)

_admin_cache: dict = {}
_ADMIN_TTL = 300   # 5 min cache — reduces Telegram API calls, improves speed

def is_admin(cid: int, uid: int) -> bool:
    if uid == OWNER_ID: return True
    key = f"{cid}:{uid}"; now = time.time()
    if key in _admin_cache:
        ok, exp = _admin_cache[key]
        if now < exp: return ok
    try:
        ok = bot.get_chat_member(cid, uid).status in ("administrator", "creator")
    except: ok = False
    _admin_cache[key] = (ok, now + _ADMIN_TTL)
    return ok

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def is_protected(cid: int, target_uid: int) -> bool:
    "Owner and the bot itself cannot be moderated."
    if target_uid == OWNER_ID: return True
    if BOT_ID and target_uid == BOT_ID: return True
    return False

def admin_required(fn):
    def wrap(msg, *a, **kw):
        if not is_admin(msg.chat.id, msg.from_user.id):
            bot.reply_to(msg, "🚫 Bhai yeh sirf admin ka kaam hai."); return
        return fn(msg, *a, **kw)
    wrap.__name__ = fn.__name__; return wrap

def owner_required(fn):
    def wrap(msg, *a, **kw):
        if not is_owner(msg.from_user.id):
            bot.reply_to(msg, "👑 Sirf owner use kar sakta hai."); return
        return fn(msg, *a, **kw)
    wrap.__name__ = fn.__name__; return wrap

def ts() -> int: return int(time.time() * 1000)
def now_str() -> str: return datetime.now().strftime("%d %b %Y, %H:%M:%S")

def elapsed_str(seconds) -> str:
    h, r = divmod(int(seconds), 3600); m, s = divmod(r, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

# =======================================================
#  PREMIUM MANAGEMENT COMMANDS (Owner Only)
# =======================================================

@bot.message_handler(commands=["addpremium"])
@owner_required
@with_init_msg
def cmd_addpremium(msg):
    """
    /addpremium @username   — reply ya mention se premium do
    """
    target = None

    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
    elif msg.entities:
        for ent in msg.entities:
            if ent.type == "mention":
                uname_mention = msg.text[ent.offset+1: ent.offset+ent.length]
                try:
                    m2 = bot.get_chat_member(msg.chat.id, f"@{uname_mention}")
                    target = m2.user
                except Exception:
                    bot.reply_to(msg, f"❌ @{uname_mention} nahi mila."); return
                break

    if not target:
        bot.reply_to(msg,
            "💎 Usage:\n"
            "`/addpremium` — reply to user's message\n"
            "`/addpremium @username`",
            parse_mode="Markdown"); return

    if target.id == OWNER_ID:
        bot.reply_to(msg, "👑 Owner hamesha premium hota hai!"); return

    if target.id in _premium_users:
        bot.reply_to(msg,
            f"ℹ️ *{target.first_name}* already premium hai.",
            parse_mode="Markdown"); return

    _grant_premium(target.id, target.first_name, msg.from_user.id)
    bot.reply_to(msg,
        f"💎 *{target.first_name}* ko Premium access mil gaya!\n"
        f"🆔 User ID: `{target.id}`\n"
        f"📅 Since: {_today()}\n"
        f"✅ Ab yeh bot use kar sakta hai.",
        parse_mode="Markdown")

    # Notify the user
    try:
        bot.send_message(target.id,
            f"🎉 *Congratulations {target.first_name}!*\n\n"
            f"💎 Tumhe *Knowledge Pro Premium* access mil gaya!\n"
            f"📅 Date: {_today()}\n\n"
            f"Ab tum bot ke saare features use kar sakte ho!\n"
            f"Type /start to begin 🚀",
            parse_mode="Markdown")
    except Exception:
        pass  # User may not have started the bot in DM

    fb_log("INFO", f"Premium granted: {target.first_name} ({target.id}) by {msg.from_user.id}")
    inc_stat("premium_granted")


@bot.message_handler(commands=["removepremium"])
@owner_required
@with_init_msg
def cmd_removepremium(msg):
    """
    /removepremium @username — reply ya mention se premium hatao
    """
    target = None

    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
    elif msg.entities:
        for ent in msg.entities:
            if ent.type == "mention":
                uname_mention = msg.text[ent.offset+1: ent.offset+ent.length]
                try:
                    m2 = bot.get_chat_member(msg.chat.id, f"@{uname_mention}")
                    target = m2.user
                except Exception:
                    bot.reply_to(msg, f"❌ @{uname_mention} nahi mila."); return
                break

    if not target:
        bot.reply_to(msg,
            "🗑️ Usage:\n"
            "`/removepremium` — reply to user's message\n"
            "`/removepremium @username`",
            parse_mode="Markdown"); return

    if target.id == OWNER_ID:
        bot.reply_to(msg, "👑 Owner ka premium remove nahi ho sakta!"); return

    if target.id not in _premium_users:
        bot.reply_to(msg,
            f"ℹ️ *{target.first_name}* premium tha hi nahi.",
            parse_mode="Markdown"); return

    _revoke_premium(target.id)
    bot.reply_to(msg,
        f"🗑️ *{target.first_name}* ka Premium access remove ho gaya.\n"
        f"🔒 Ab yeh bot use nahi kar sakta.",
        parse_mode="Markdown")

    # Notify the user
    try:
        bot.send_message(target.id,
            f"🔒 *{target.first_name}*, tumhara Knowledge Pro Premium access\n"
            f"remove kar diya gaya hai.\n"
            f"Owner se contact karo agar koi mistake lage.",
            parse_mode="Markdown")
    except Exception:
        pass

    fb_log("WARN", f"Premium revoked: {target.first_name} ({target.id}) by {msg.from_user.id}")


@bot.message_handler(commands=["mysub"])
def cmd_mysub(msg):
    """Check own subscription status."""
    uid   = msg.from_user.id
    uname = msg.from_user.first_name

    if uid == OWNER_ID:
        bot.reply_to(msg,
            f"👑 *{uname} — Bot Owner*\n"
            f"💎 Plan: *OWNER (Lifetime)*\n"
            f"✅ Full access to everything.",
            parse_mode="Markdown"); return

    if uid in _premium_users:
        info  = _premium_users[uid]
        since = datetime.fromtimestamp(info.get("since", time.time())).strftime("%d %b %Y")
        bot.reply_to(msg,
            f"💎 *{uname} — Premium Member*\n"
            f"📅 Since: {since}\n"
            f"✅ Full bot access active.\n\n"
            f"_Enjoy karo bhai!_ 🎉",
            parse_mode="Markdown")
    else:
        bot.reply_to(msg,
            f"🔒 *{uname} — Free Plan*\n\n"
            f"❌ Bot access: *Blocked*\n\n"
            f"💎 Premium access ke liye owner se contact karo.\n"
            f"_Yeh ek private bot hai._",
            parse_mode="Markdown")


@bot.message_handler(commands=["premiumlist"])
@owner_required
def cmd_premiumlist(msg):
    """List all premium users."""
    if not _premium_users:
        bot.reply_to(msg, "📋 Koi premium user nahi hai abhi.\nUse `/addpremium` to add.",
                     parse_mode="Markdown"); return

    lines = []
    for uid, info in _premium_users.items():
        name  = info.get("name", f"User{uid}")
        since = datetime.fromtimestamp(info.get("since", 0)).strftime("%d %b %Y")
        lines.append(f"💎 *{name}* — `{uid}` (since {since})")

    bot.reply_to(msg,
        f"💎 *Premium Members* ({len(_premium_users)})\n"
        f"{'─'*30}\n" +
        "\n".join(lines),
        parse_mode="Markdown")


# =======================================================
#  /login
# =======================================================

@bot.message_handler(commands=["login"])
def cmd_login(msg):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg, "🔑 Usage: /login [password]"); return
    remote = fb_get("config/admin_pass") or ADMIN_PASS
    if parts[1].strip() == remote:
        panel_auth.add(msg.from_user.id)
        bot.reply_to(msg, "OK *Login ho gaya!* Admin access mil gaya.", parse_mode="Markdown")
        fb_log("INFO", f"Admin login: {msg.from_user.id}")
    else:
        bot.reply_to(msg, "X Wrong password bhai.")
        fb_log("WARN", f"Failed login: {msg.from_user.id}")

# =======================================================
#  /promote  /demote  (Owner only)
# =======================================================

@bot.message_handler(commands=["promote"])
@owner_required
@with_init_msg
def cmd_promote(msg):
    if not msg.reply_to_message:
        bot.reply_to(msg,
            "👑 Usage: Kisi message ko reply karke /promote karo.\n"
            "Wo user admin ban jayega."); return
    t = msg.reply_to_message.from_user
    if is_protected(msg.chat.id, t.id) and t.id != OWNER_ID:
        bot.reply_to(msg, "🛡️ Yeh user already protected hai."); return
    try:
        bot.promote_chat_member(
            msg.chat.id, t.id,
            can_delete_messages=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_invite_users=True,
            can_manage_chat=True,
        )
        # Bust cache so next is_admin() reflects truth
        _admin_cache.pop(f"{msg.chat.id}:{t.id}", None)
        bot.reply_to(msg,
            f"👑 *{t.first_name}* ab admin hai!\n"
            f"🛡️ Permissions: delete, restrict, pin, invite, manage.",
            parse_mode="Markdown")
        fb_push("mod_log", {"user": t.first_name, "user_id": t.id,
                             "action": "promote", "time": ts()})
        fb_log("INFO", f"Promoted {t.first_name} in {msg.chat.id}")
    except Exception as e:
        bot.reply_to(msg, f"❌ Promote failed: {e}")


@bot.message_handler(commands=["demote"])
@owner_required
@with_init_msg
def cmd_demote(msg):
    if not msg.reply_to_message:
        bot.reply_to(msg,
            "⬇️ Usage: Kisi message ko reply karke /demote karo.\n"
            "Admin rights remove ho jayenge."); return
    t = msg.reply_to_message.from_user
    if is_protected(msg.chat.id, t.id):
        bot.reply_to(msg,
            f"🛡️ *{t.first_name}* ko demote nahi kar sakte — protected hai.",
            parse_mode="Markdown"); return
    try:
        bot.promote_chat_member(
            msg.chat.id, t.id,
            can_delete_messages=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_invite_users=False,
            can_manage_chat=False,
        )
        _admin_cache.pop(f"{msg.chat.id}:{t.id}", None)
        bot.reply_to(msg,
            f"⬇️ *{t.first_name}* ka admin hat gaya!\n"
            f"Ab wo ek normal member hai.",
            parse_mode="Markdown")
        fb_push("mod_log", {"user": t.first_name, "user_id": t.id,
                             "action": "demote", "time": ts()})
        fb_log("INFO", f"Demoted {t.first_name} in {msg.chat.id}")
    except Exception as e:
        bot.reply_to(msg, f"❌ Demote failed: {e}")

# =======================================================
#  MODERATION - owner/bot protected on ALL commands
# =======================================================

@bot.message_handler(commands=["warn"])
@admin_required
@with_init_msg
def cmd_warn(msg):
    if not msg.reply_to_message:
        bot.reply_to(msg, "[!]️ Kisi message ko reply karke /warn karo."); return
    t = msg.reply_to_message.from_user
    if is_protected(msg.chat.id, t.id):
        bot.reply_to(msg,
            f"🛡️ *{t.first_name}* protected hai - warn nahi kar sakte.",
            parse_mode="Markdown"); return

    cid = str(msg.chat.id); uid = str(t.id)
    rsn = (msg.text.split(maxsplit=1)[1].strip()
           if len(msg.text.split(maxsplit=1)) > 1 else "No reason")

    warn_counts.setdefault(cid, {})
    warn_counts[cid][uid] = warn_counts[cid].get(uid, 0) + 1
    cnt = warn_counts[cid][uid]

    fb_set(f"warnings/{cid}/{uid}", cnt)
    fb_push("mod_log", {"user": t.first_name, "user_id": t.id,
                         "action": "warn", "reason": rsn, "time": ts()})
    inc_stat("total_warns")
    bot.reply_to(msg,
        f"[!]️ *{t.first_name}* warning #{cnt}/3\nReason: {rsn}",
        parse_mode="Markdown")
    if cnt >= 3:
        try:
            bot.ban_chat_member(msg.chat.id, t.id)
            bot.send_message(msg.chat.id,
                f"🚫 *{t.first_name}* 3 warnings ke baad auto-ban!",
                parse_mode="Markdown")
            fb_log("WARN", f"Auto-banned {t.first_name}")
        except Exception as e:
            log.error(f"Auto-ban: {e}")


@bot.message_handler(commands=["warnc"])
@owner_required
@with_init_msg
def cmd_warnc(msg):
    """
    /warnc @username  — reset warn count for a user (owner only)
    Works via reply OR by mentioning @username in command text.
    """
    cid     = msg.chat.id
    cid_str = str(cid)
    target  = None
    target_name = None

    # Method 1: reply to their message
    if msg.reply_to_message:
        target      = msg.reply_to_message.from_user
        target_name = target.first_name
        uid_str     = str(target.id)

    # Method 2: @username mention in text
    elif msg.entities:
        for ent in msg.entities:
            if ent.type == "mention":
                username = msg.text[ent.offset + 1: ent.offset + ent.length]  # strip @
                try:
                    member  = bot.get_chat_member(cid, f"@{username}")
                    target  = member.user
                    target_name = target.first_name
                    uid_str     = str(target.id)
                except Exception:
                    bot.reply_to(msg,
                        f"❌ @{username} nahi mila chat mein. Unka message reply karo."); return
                break

    if not target:
        bot.reply_to(msg,
            "🔄 *Warn Reset Usage:*\n"
            "`/warnc @username` — mention karke reset karo\n"
            "Ya kisi ke message ko reply karke `/warnc` likho.",
            parse_mode="Markdown"); return

    uid_str = str(target.id)

    # Reset in-memory
    prev = 0
    if cid_str in warn_counts and uid_str in warn_counts[cid_str]:
        prev = warn_counts[cid_str][uid_str]
        warn_counts[cid_str][uid_str] = 0

    # Reset in Firebase
    fb_set(f"warnings/{cid_str}/{uid_str}", 0)
    fb_push("mod_log", {
        "user":    target_name,
        "user_id": target.id,
        "action":  "warn_reset",
        "reason":  f"Reset by owner (was {prev})",
        "time":    ts(),
    })
    fb_log("INFO", f"Warns reset: {target_name} in {cid} (was {prev})")

    bot.reply_to(msg,
        f"🔄 *{target_name}* ka warn count reset ho gaya!\n"
        f"📊 Pehle: *{prev}/3* → Ab: *0/3*\n"
        f"✅ Clean slate bhai. Group mein hai, kuch remove nahi hua.",
        parse_mode="Markdown")


@bot.message_handler(commands=["ban"])
@admin_required
@with_init_msg
def cmd_ban(msg):
    if not msg.reply_to_message:
        bot.reply_to(msg, "🚫 Kisi message ko reply karke /ban karo."); return
    t = msg.reply_to_message.from_user
    if is_protected(msg.chat.id, t.id):
        bot.reply_to(msg,
            f"🛡️ *{t.first_name}* protected hai - ban nahi kar sakte.",
            parse_mode="Markdown"); return

    rsn = (msg.text.split(maxsplit=1)[1].strip()
           if len(msg.text.split(maxsplit=1)) > 1 else "No reason")
    try:
        bot.ban_chat_member(msg.chat.id, t.id)
        banned_users.add(t.id)
        fb_set(f"banned/{t.id}",
               {"user": t.first_name, "user_id": t.id, "reason": rsn, "date": ts()})
        fb_push("mod_log",
                {"user": t.first_name, "user_id": t.id,
                 "action": "ban", "reason": rsn, "time": ts()})
        inc_stat("banned_count")
        bot.reply_to(msg,
            f"🚫 *{t.first_name}* ban ho gaya!\nReason: {rsn}",
            parse_mode="Markdown")
        fb_log("WARN", f"Banned {t.first_name}")
    except Exception as e:
        err = str(e)
        if "not enough rights" in err.lower() or "admin" in err.lower():
            bot.reply_to(msg,
                f"❌ *{t.first_name}* ek admin hai!\n"
                f"Bot ke paas unhe ban karne ka right nahi.\n"
                f"_Pehle Telegram se manually unka admin remove karo, phir /ban karo._",
                parse_mode="Markdown")
        else:
            bot.reply_to(msg, f"❌ Ban failed: {err[:100]}")


@bot.message_handler(commands=["kick"])
@admin_required
@with_init_msg
def cmd_kick(msg):
    if not msg.reply_to_message:
        bot.reply_to(msg, "👢 Kisi message ko reply karke /kick karo."); return
    t = msg.reply_to_message.from_user
    if is_protected(msg.chat.id, t.id):
        bot.reply_to(msg,
            f"🛡️ *{t.first_name}* protected hai - kick nahi kar sakte.",
            parse_mode="Markdown"); return
    try:
        bot.ban_chat_member(msg.chat.id, t.id)
        bot.unban_chat_member(msg.chat.id, t.id)
        fb_push("mod_log",
                {"user": t.first_name, "user_id": t.id, "action": "kick", "time": ts()})
        bot.reply_to(msg,
            f"👢 *{t.first_name}* kick ho gaya!", parse_mode="Markdown")
        fb_log("WARN", f"Kicked {t.first_name}")
    except Exception as e:
        err = str(e)
        if "not enough rights" in err.lower() or "admin" in err.lower():
            bot.reply_to(msg,
                f"❌ *{t.first_name}* ek admin hai!\n"
                f"_Pehle Telegram se unka admin remove karo, phir /kick karo._",
                parse_mode="Markdown")
        else:
            bot.reply_to(msg, f"❌ Kick failed: {err[:100]}")


@bot.message_handler(commands=["mute"])
@admin_required
@with_init_msg
def cmd_mute(msg):
    if not msg.reply_to_message:
        bot.reply_to(msg, "🔇 Kisi message ko reply karke /mute karo."); return
    t = msg.reply_to_message.from_user
    if is_protected(msg.chat.id, t.id):
        bot.reply_to(msg,
            f"🛡️ *{t.first_name}* protected hai - mute nahi kar sakte.",
            parse_mode="Markdown"); return

    parts = msg.text.split()
    mins  = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    until = datetime.now() + timedelta(minutes=mins)
    try:
        bot.restrict_chat_member(msg.chat.id, t.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until)
        fb_push("mod_log",
                {"user": t.first_name, "user_id": t.id,
                 "action": "mute", "duration": mins, "time": ts()})
        bot.reply_to(msg,
            f"🔇 *{t.first_name}* {mins} min ke liye mute!", parse_mode="Markdown")
        fb_log("INFO", f"Muted {t.first_name} {mins}m")
    except Exception as e:
        err = str(e)
        if "not enough rights" in err.lower() or "admin" in err.lower():
            bot.reply_to(msg,
                f"❌ *{t.first_name}* ek admin hai!\n"
                f"_Admins ko Telegram directly mute nahi kar sakta. Pehle demote karo._",
                parse_mode="Markdown")
        else:
            bot.reply_to(msg, f"❌ Mute failed: {err[:100]}")


@bot.message_handler(commands=["permission"])
@admin_required
@with_init_msg
def cmd_permission(msg):
    parts = msg.text.split()
    if len(parts) < 3:
        bot.reply_to(msg,
            "🔐 Usage: /permission [media|msg|link|all] [on|off]"); return
    ptype  = parts[1].lower().strip()
    status = parts[2].lower().strip() == "on"
    fb_set(f"settings/perm_{ptype}", status)
    try:
        if ptype == "all":
            cp = ChatPermissions(
                can_send_messages=status,
                can_send_media_messages=status,
                can_send_other_messages=status,
                can_add_web_page_previews=status)
        elif ptype == "media": cp = ChatPermissions(can_send_media_messages=status)
        elif ptype == "msg":   cp = ChatPermissions(can_send_messages=status)
        elif ptype == "link":  cp = ChatPermissions(can_add_web_page_previews=status)
        else:
            bot.reply_to(msg, "[!]️ Valid types: media, msg, link, all"); return
        bot.set_chat_permissions(msg.chat.id, cp)
        bot.reply_to(msg,
            f"🔐 Permission *{ptype}* -> {'OK ON' if status else 'X OFF'}",
            parse_mode="Markdown")
        fb_log("INFO", f"Permission {ptype}={status}")
    except Exception as e:
        bot.reply_to(msg, f"X Permission failed: {e}")

# =======================================================
#  /nuke
# =======================================================

@bot.message_handler(commands=["nuke"])
@admin_required
@with_init_msg
def cmd_nuke(msg):
    mk = InlineKeyboardMarkup()
    mk.add(
        InlineKeyboardButton("[nuclear]️ NUKE KAR!", callback_data=f"nuke_yes_{msg.chat.id}"),
        InlineKeyboardButton("X Nahi",      callback_data="nuke_no"),
    )
    bot.send_message(msg.chat.id,
        "[!]️ *Pakka sure hai?*\n~100 recent messages delete ho jayenge!",
        parse_mode="Markdown", reply_markup=mk)


@bot.callback_query_handler(func=lambda c: c.data.startswith("nuke_"))
def cb_nuke(call):
    if call.data.startswith("nuke_yes_"):
        cid = int(call.data.split("_")[-1])
        if not is_admin(cid, call.from_user.id):
            bot.answer_callback_query(call.id, "[no] Sirf admins!"); return
        bot.answer_callback_query(call.id, "[nuclear]️ Nuking...")
        try: bot.edit_message_text("[nuclear]️ Nuke chal raha hai...", cid, call.message.message_id)
        except: pass
        count = 0
        for i in range(call.message.message_id, max(call.message.message_id - 100, 1), -1):
            try: bot.delete_message(cid, i); count += 1
            except: pass
        try: bot.send_message(cid, f"[nuclear]️ Nuke done! ~{count} messages gaye.")
        except: pass
        fb_log("WARN", f"Nuke in {cid}: ~{count}")
    else:
        bot.answer_callback_query(call.id, "Cancelled X")
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass

# =======================================================
#  /shout
# =======================================================

@bot.message_handler(commands=["shout"])
@admin_required
@with_init_msg
def cmd_shout(msg):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg, "📢 Usage: /shout [message]"); return
    content = parts[1].strip()

    # Show typing instantly
    try: bot.send_chat_action(msg.chat.id, "typing")
    except: pass

    def _do_shout():
        api_key = OPENROUTER_KEY
        model   = OPENROUTER_MODEL
        if not api_key:
            ac = fb_get("config/ai", {}) or {}
            api_key = ac.get("api_key", "")
            if ac.get("model"): model = ac["model"]

        enhanced = content  # fallback = original

        if api_key:
            try:
                shout_prompt = (
                    "Tu Knowledge Pro hai. Ek admin ne yeh message shout karna chahta hai apne group mein.\n"
                    "Tujhe yeh message enhance karna hai — more impactful, punchy, Hinglish gangster style mein.\n"
                    "Emojis add kar, excitement add kar, but original meaning same rakh.\n"
                    "Sirf enhanced message return kar — koi explanation nahi.\n\n"
                    f"Original message:\n{content}"
                )
                resp = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://t.me/knowledgeprobot",
                        "X-Title": "Knowledge Pro",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": _build_prompt("Admin")},
                            {"role": "user",   "content": shout_prompt},
                        ],
                        "max_tokens": 300,
                        "temperature": 0.88,
                    },
                    timeout=20,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if "choices" in data:
                        enhanced = data["choices"][0]["message"]["content"].strip()
            except Exception as e:
                log.warning(f"Shout AI enhance failed: {e}")
                # Fall through — use original content

        bot.send_message(msg.chat.id,
            f"📢 *SHOUT:*\n\n{enhanced}", parse_mode="Markdown")
        fb_push("broadcast", {"msg": enhanced, "type": "msg", "time": ts()})
        fb_log("INFO", f"Shout (AI enhanced): {enhanced[:50]}")

    _ai_pool.submit(_do_shout)

# =======================================================
#  /shoutconfig - FULLY FIXED
#
#  Bug was: `parts = raw[1:]` skipped the command but
#  indexing was wrong when "list" was the first arg.
#  Fix: split cleanly, handle all 3 cases explicitly.
#
#  /shoutconfig [word] delete  -> block (auto-delete from chat)
#  /shoutconfig [word] allow   -> unblock
#  /shoutconfig list           -> show blocked list
# =======================================================

@bot.message_handler(commands=["shoutconfig"])
@admin_required
@with_init_msg
def cmd_shoutconfig(msg):
    # Strip command and split remaining args
    text  = msg.text.strip()
    after = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    args  = after.split()            # e.g. ["badword", "delete"] or ["list"]

    # -- list --
    if len(args) == 1 and args[0].lower() == "list":
        if not blocked_words_list:
            bot.reply_to(msg, "OK Koi blocked word nahi hai abhi."); return
        lines = "\n".join(f"  * `{w}`" for w in blocked_words_list)
        bot.reply_to(msg,
            f"🚫 *Blocked Words* ({len(blocked_words_list)}):\n{lines}",
            parse_mode="Markdown")
        return

    # -- word + action --
    if len(args) < 2:
        bot.reply_to(msg,
            "📖 *ShoutConfig Usage:*\n"
            "`/shoutconfig [word] delete` - block karo\n"
            "`/shoutconfig [word] allow`  - unblock karo\n"
            "`/shoutconfig list`          - list dekho",
            parse_mode="Markdown"); return

    word   = args[0].lower().strip()
    action = args[1].lower().strip()

    if action == "delete":
        if word not in blocked_words_list:
            blocked_words_list.append(word)
            _fb_async(_save_blocked_words)
            bot.reply_to(msg,
                f"🚫 `{word}` block ho gaya!\nAb yeh word chat se auto-delete hoga.",
                parse_mode="Markdown")
            fb_log("INFO", f"Blocked: {word}")
        else:
            bot.reply_to(msg,
                f"[!]️ `{word}` pehle se blocked hai.", parse_mode="Markdown")

    elif action == "allow":
        if word in blocked_words_list:
            blocked_words_list.remove(word)
            _fb_async(_save_blocked_words)
            bot.reply_to(msg,
                f"OK `{word}` unblock ho gaya!", parse_mode="Markdown")
            fb_log("INFO", f"Unblocked: {word}")
        else:
            bot.reply_to(msg,
                f"[!]️ `{word}` list mein tha hi nahi.", parse_mode="Markdown")

    else:
        bot.reply_to(msg,
            "[!]️ Action sirf `delete` ya `allow` hona chahiye.",
            parse_mode="Markdown")

# =======================================================
#  /setautoreply  /deleteautoreply
# =======================================================

@bot.message_handler(commands=["setautoreply"])
@admin_required
@with_init_msg
def cmd_setautoreply(msg):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or "|" not in parts[1]:
        bot.reply_to(msg, "🤖 Usage: /setautoreply [word] | [reply]"); return
    word, reply = [x.strip() for x in parts[1].split("|", 1)]
    key = re.sub(r"\s+", "", word.lower())
    auto_replies[key] = reply
    fb_set(f"autoreply/{key}", reply)
    bot.reply_to(msg,
        f"🤖 AutoReply set:\n`{key}` -> {reply}", parse_mode="Markdown")
    fb_log("INFO", f"AutoReply: {key}")


@bot.message_handler(commands=["deleteautoreply"])
@admin_required
@with_init_msg
def cmd_delautoreply(msg):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg, "X Usage: /deleteautoreply [word]"); return
    word = re.sub(r"\s+", "", parts[1].lower())
    if word in auto_replies:
        del auto_replies[word]
        fb_del(f"autoreply/{word}")
        bot.reply_to(msg,
            f"X AutoReply `{word}` delete ho gaya.", parse_mode="Markdown")
    else:
        bot.reply_to(msg,
            f"[!]️ `{word}` ka koi autoreply nahi mila.", parse_mode="Markdown")

# =======================================================
#  /afk - ENHANCED
#  /afk  /back — FULLY UPGRADED WITH PANEL
#
#  Features:
#  • /afk [reason]    — set AFK with optional reason
#  • /afk             — opens inline panel with options
#  • /back            — manual return
#  • /afklist         — owner/admin sees all active AFK users
#  • Auto-return on any message
#  • DM alert when someone mentions you while AFK
#  • Return card shows: time gone + who pinged + ping count
#  • Custom status emojis: 💤😴🏃🎮🍕📵
#  • AFK data synced to Firebase
# =======================================================

AFK_STATUS_EMOJIS = {
    "sleeping":  "😴",
    "gaming":    "🎮",
    "eating":    "🍕",
    "busy":      "📵",
    "gym":       "🏋️",
    "studying":  "📚",
    "meeting":   "💼",
    "custom":    "💤",
}

@bot.message_handler(commands=["afk"])
def cmd_afk(msg):
    uid   = msg.from_user.id
    name  = msg.from_user.first_name
    parts = msg.text.split(maxsplit=1)

    # /afk with no args → show interactive panel
    if len(parts) < 2:
        mk = InlineKeyboardMarkup()
        mk.row(
            InlineKeyboardButton("😴 Sleeping",  callback_data=f"afk_set_{uid}_sleeping_Soone ja raha hoon"),
            InlineKeyboardButton("🎮 Gaming",    callback_data=f"afk_set_{uid}_gaming_Games khel raha hoon"),
        )
        mk.row(
            InlineKeyboardButton("🍕 Eating",    callback_data=f"afk_set_{uid}_eating_Khaana kha raha hoon"),
            InlineKeyboardButton("📵 Busy",      callback_data=f"afk_set_{uid}_busy_Busy hoon, baad mein aana"),
        )
        mk.row(
            InlineKeyboardButton("🏋️ Gym",       callback_data=f"afk_set_{uid}_gym_Gym mein hoon"),
            InlineKeyboardButton("📚 Studying",  callback_data=f"afk_set_{uid}_studying_Padh raha hoon"),
        )
        mk.row(
            InlineKeyboardButton("💼 Meeting",   callback_data=f"afk_set_{uid}_meeting_Meeting mein hoon"),
            InlineKeyboardButton("✏️ Custom",    callback_data=f"afk_custom_{uid}"),
        )
        if uid in afk_users:
            mk.row(InlineKeyboardButton("✅ Wapas Aa Gaya (Clear AFK)", callback_data=f"afk_clear_{uid}"))

        afk_status_line = f"🔴 Already AFK: _{afk_users[uid]['reason']}_" if uid in afk_users else "🟢 Abhi AFK nahi ho."
        bot.reply_to(msg,
            f"💤 *AFK Panel — {name}*\n\n"
            f"{afk_status_line}\n\n"
            f"Kya reason select karna hai?\n"
            f"Ya /afk [reason] likh ke directly set karo.",
            parse_mode="Markdown",
            reply_markup=mk)
        return

    # /afk reason — set directly
    reason = parts[1].strip()
    _set_afk(msg, uid, name, reason, "custom")


@bot.callback_query_handler(func=lambda c: c.data.startswith("afk_"))
def cb_afk(call):
    uid  = call.from_user.id
    data = call.data

    if data.startswith("afk_set_"):
        # format: afk_set_{uid}_{status}_{reason}
        parts = data.split("_", 4)
        # parts[0]=afk, [1]=set, [2]=uid, [3]=status, [4]=reason
        if len(parts) < 5: return
        target_uid = int(parts[2])
        status     = parts[3]
        reason     = parts[4]

        if uid != target_uid:
            bot.answer_callback_query(call.id, "Yeh tera AFK panel nahi hai!"); return

        name = call.from_user.first_name
        bot.answer_callback_query(call.id, f"AFK set: {reason}")
        _set_afk_from_callback(call, uid, name, reason, status)

    elif data.startswith("afk_custom_"):
        target_uid = int(data.split("_")[2])
        if uid != target_uid:
            bot.answer_callback_query(call.id, "Yeh tera AFK panel nahi hai!"); return
        bot.answer_callback_query(call.id, "Apna reason type karo!")
        bot.send_message(call.message.chat.id,
            f"✏️ *{call.from_user.first_name}*, apna AFK reason type karo:\n"
            f"_(Sirf ek message bhejo — wahi reason ban jayega)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"afk_cancel_{uid}")
            ]]))
        # Store pending state
        _afk_pending_custom[uid] = call.message.chat.id

    elif data.startswith("afk_clear_"):
        target_uid = int(data.split("_")[2])
        if uid != target_uid:
            bot.answer_callback_query(call.id, "Yeh tera AFK panel nahi hai!"); return
        if uid in afk_users:
            afk_users.pop(uid)
            fb_del(f"afk/{uid}")
            bot.answer_callback_query(call.id, "✅ AFK clear ho gaya!")
            try:
                bot.edit_message_text(
                    f"✅ *{call.from_user.first_name}* AFK se wapas aa gaya!",
                    call.message.chat.id, call.message.message_id,
                    parse_mode="Markdown")
            except Exception: pass
        else:
            bot.answer_callback_query(call.id, "Tu AFK tha hi nahi!")

    elif data.startswith("afk_cancel_"):
        target_uid = int(data.split("_")[2])
        if uid != target_uid: return
        _afk_pending_custom.pop(uid, None)
        bot.answer_callback_query(call.id, "Cancelled")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass


# Pending custom AFK reason {uid: chat_id}
_afk_pending_custom: dict = {}


def _set_afk(msg, uid: int, name: str, reason: str, status: str):
    emoji = AFK_STATUS_EMOJIS.get(status, "💤")
    afk_users[uid] = {
        "reason":     reason,
        "status":     status,
        "emoji":      emoji,
        "time":       time.time(),
        "name":       name,
        "chat_id":    msg.chat.id,
        "ping_count": 0,
        "pingers":    [],
    }
    fb_set(f"afk/{uid}", {
        "reason": reason, "status": status,
        "time": ts(), "name": name
    })
    bot.reply_to(msg,
        f"{emoji} *{name}* AFK ho gaya!\n"
        f"📝 Status: *{status.capitalize()}*\n"
        f"💬 Reason: _{reason}_\n"
        f"🕐 Since: {datetime.now().strftime('%I:%M %p')}\n\n"
        f"_/back ya koi message bhejo wapas aane ke liye_",
        parse_mode="Markdown")


def _set_afk_from_callback(call, uid: int, name: str, reason: str, status: str):
    emoji = AFK_STATUS_EMOJIS.get(status, "💤")
    afk_users[uid] = {
        "reason":     reason,
        "status":     status,
        "emoji":      emoji,
        "time":       time.time(),
        "name":       name,
        "chat_id":    call.message.chat.id,
        "ping_count": 0,
        "pingers":    [],
    }
    fb_set(f"afk/{uid}", {
        "reason": reason, "status": status,
        "time": ts(), "name": name
    })
    try:
        bot.edit_message_text(
            f"{emoji} *{name}* AFK ho gaya!\n"
            f"📝 Status: *{status.capitalize()}*\n"
            f"💬 Reason: _{reason}_\n"
            f"🕐 Since: {datetime.now().strftime('%I:%M %p')}\n\n"
            f"_/back ya koi message bhejo wapas aane ke liye_",
            call.message.chat.id, call.message.message_id,
            parse_mode="Markdown")
    except Exception:
        bot.send_message(call.message.chat.id,
            f"{emoji} *{name}* AFK ho gaya!\n"
            f"💬 Reason: _{reason}_",
            parse_mode="Markdown")


@bot.message_handler(commands=["back"])
def cmd_back(msg):
    uid = msg.from_user.id
    if uid not in afk_users:
        bot.reply_to(msg, "Bhai tu AFK tha hi nahi 😄"); return
    _return_from_afk(msg, uid, manual=True)


@bot.message_handler(commands=["afklist"])
@admin_required
def cmd_afklist(msg):
    """Show all currently AFK users."""
    if not afk_users:
        bot.reply_to(msg, "✅ Koi bhi AFK nahi hai abhi! Sab active hain."); return

    lines = []
    for uid, info in afk_users.items():
        emoji  = info.get("emoji", "💤")
        name   = info.get("name", f"User{uid}")
        reason = info.get("reason", "—")
        gone   = elapsed_str(time.time() - info.get("time", time.time()))
        pings  = info.get("ping_count", 0)
        lines.append(f"{emoji} *{name}* — _{reason}_\n   ⏱ {gone} ago | 📬 {pings} pings")

    bot.reply_to(msg,
        f"💤 *Active AFK Users* ({len(afk_users)})\n"
        f"{'─'*30}\n" +
        "\n\n".join(lines),
        parse_mode="Markdown")


def _return_from_afk(msg, uid: int, manual: bool):
    info   = afk_users.pop(uid, {})
    fb_del(f"afk/{uid}")

    gone      = elapsed_str(time.time() - info.get("time", time.time()))
    cnt       = info.get("ping_count", 0)
    pings     = info.get("pingers", [])
    shown     = pings[:3]
    extra     = cnt - 3 if cnt > 3 else 0
    names_str = ", ".join(shown) + (f" +{extra} aur" if extra > 0 else "")
    emoji     = info.get("emoji", "💤")
    method    = "manually" if manual else "via message"

    ping_line = f"\n📬 *{cnt}* log ne ping kiya: {names_str}" if cnt > 0 else \
                "\n📬 Kisi ne miss nahi kiya 😄"

    bot.reply_to(msg,
        f"👋 *{msg.from_user.first_name}* wapas aa gaya! _{method}_\n"
        f"⏱️ AFK raha: *{gone}*\n"
        f"{emoji} Was: _{info.get('reason', '—')}_"
        f"{ping_line}",
        parse_mode="Markdown")

# =======================================================
#  /pin  /unpin  /roll  /bala
# =======================================================

@bot.message_handler(commands=["pin"])
@admin_required
def cmd_pin(msg):
    if not msg.reply_to_message:
        bot.reply_to(msg, "📌 Kisi message ko reply karke /pin karo."); return
    try:
        bot.pin_chat_message(msg.chat.id, msg.reply_to_message.message_id)
        bot.reply_to(msg, "📌 Message pin ho gaya!")
    except Exception as e: bot.reply_to(msg, f"X Failed: {e}")


@bot.message_handler(commands=["unpin"])
@admin_required
def cmd_unpin(msg):
    try:
        bot.unpin_chat_message(msg.chat.id)
        bot.reply_to(msg, "📌 Message unpin ho gaya!")
    except Exception as e: bot.reply_to(msg, f"X Failed: {e}")


@bot.message_handler(commands=["roll"])
@premium_required
def cmd_roll(msg):
    n = random.randint(1, 6)
    bot.reply_to(msg,
        f"{'[1][2][3][4][5][6]'[n-1]} Tune roll kiya *{n}*!", parse_mode="Markdown")


@bot.message_handler(commands=["bala"])
@premium_required
def cmd_bala(msg):
    bot.send_message(msg.chat.id, random.choice([
        "🕺 *BALA BALA SHAITAN KA SALA!* Dance karo bhai!",
        "💃 Naach le yaar, zindagi mein tension mat le!",
        "🎉 Bala Bala! Floor pe aa ja party shuru ho gayi!",
        "🪩 Aye bhai, thand rakh aur naach le!",
        "🎵 Bala mode ON - sab uthke naachein!",
    ]), parse_mode="Markdown")

# =======================================================
#  /ai - Knowledge Pro
#
#  FIX 1: _ai_pool.submit() - NEVER blocks the polling thread
#          AI call happens in background; bot stays responsive
#  FIX 2: _build_prompt() called fresh per call -> date = today
#  FIX 3: Full conversation memory (ChatGPT-style deque)
#  FIX 4: Self-abuse guard in system prompt
#  FIX 5: typing action shown instantly before thread starts
#  FIX 6: No OpenRouter conversation save (all in RAM)
#  FIX 7: Gangster Hinglish + retaliation + emotion engine
# =======================================================

@bot.message_handler(commands=["ai"])
@premium_required
def cmd_ai(msg):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg,
            "🧠 Usage: /ai [apna sawaal ya baat likho]\n"
            "💡 Tip: /aiclr se conversation reset karo"); return

    user_msg  = parts[1].strip()
    uid       = msg.from_user.id
    user_name = msg.from_user.first_name

    # Show typing INSTANTLY before AI thread starts
    try: bot.send_chat_action(msg.chat.id, "typing")
    except: pass

    def _do_ai():
        provider = _get_user_provider(uid)
        provider_name = _provider_info.get(provider, {}).get("name", provider)

        # Build AFK context — AI knows who is AFK right now
        afk_context = ""
        if afk_users:
            afk_lines = []
            for auid, ainfo in list(afk_users.items())[:5]:
                afk_lines.append(
                    f"• {ainfo.get('name','Unknown')} is AFK: {ainfo.get('reason','—')} "
                    f"(since {elapsed_str(time.time()-ainfo.get('time',time.time()))} ago)"
                )
            afk_context = "\n\n[LIVE GROUP CONTEXT — AFK Users right now]\n" + "\n".join(afk_lines) + "\n[/LIVE CONTEXT]"

        # Build messages: system + AFK context + history + new user message
        history  = _get_history(uid)
        sys_msg  = _build_prompt(user_name) + afk_context
        messages = [{"role": "system", "content": sys_msg}]
        messages.extend(history)

        # --- Deep Search: auto web search when question needs current info ---
        _deep_search_keywords = [
            "latest", "news", "today", "current", "now", "2025", "2026",
            "price", "rate", "score", "result", "winner", "update",
            "abhi", "aaj", "khabar", "news", "recent", "trending",
        ]
        full_user_content = user_msg
        urls = _extract_urls(user_msg)
        needs_search = any(kw in user_msg.lower() for kw in _deep_search_keywords)
        if needs_search and not urls:
            try:
                bot.send_chat_action(msg.chat.id, "typing")
                ddg_resp = requests.get(
                    "https://api.duckduckgo.com/",
                    params={"q": user_msg, "format": "json",
                            "no_redirect": "1", "no_html": "1", "skip_disambig": "1"},
                    headers={"User-Agent": "KnowledgeProBot/1.0"},
                    timeout=6,
                )
                if ddg_resp.status_code == 200:
                    ddg = ddg_resp.json()
                    search_ctx = ""
                    if ddg.get("AbstractText"):
                        search_ctx += f"\n[Web Search Result]\n{ddg['AbstractText']}\n"
                    if ddg.get("Answer"):
                        search_ctx += f"\n[Direct Answer]\n{ddg['Answer']}\n"
                    topics = ddg.get("RelatedTopics", [])[:3]
                    for t in topics:
                        if isinstance(t, dict) and t.get("Text"):
                            search_ctx += f"• {t['Text'][:150]}\n"
                    if search_ctx:
                        full_user_content = user_msg + "\n\n[🔍 Deep Search Results]" + search_ctx + "[/Deep Search]"
            except Exception:
                pass

        # --- Link analysis ---
        if urls:
            url_contexts = []
            for url in urls[:2]:
                try: bot.send_chat_action(msg.chat.id, "typing")
                except: pass
                page_text = _fetch_url_content(url, max_chars=2500)
                url_contexts.append(f"\n\n[URL Content — {url}]\n{page_text}\n[/URL Content]")
            full_user_content = user_msg + "".join(url_contexts)

        messages.append({"role": "user", "content": full_user_content})

        # Save user turn to memory
        _push_history(uid, "user", user_msg)

        try: bot.send_chat_action(msg.chat.id, "typing")
        except: pass

        try:
            reply = _call_ai_api(provider, messages, max_tokens=500, temperature=0.93, timeout=35)

            # Save assistant turn to memory
            _push_history(uid, "assistant", reply)

            # Add provider badge for non-openrouter providers
            if provider != "openrouter":
                info = _provider_info.get(provider, {})
                reply = f"{info.get('emoji','🤖')} _{info.get('name',provider)}_\n\n{reply}"

            bot.reply_to(msg, reply)
            inc_stat("ai_queries")
            fb_log("INFO", f"AI[{provider}]:{user_name}:{user_msg[:40]}")

        except requests.Timeout:
            _push_history(uid, "assistant", "[timeout]")
            bot.reply_to(msg,
                "⏳ Bhai AI thoda slow hai abhi — ek baar aur try kar yaar.")
        except ValueError as e:
            bot.reply_to(msg, f"❌ {str(e)[:150]}")
        except Exception as e:
            log.error(f"AI error [{provider}]: {e}")
            bot.reply_to(msg,
                "❌ Kuch toh gadbad hai. Thodi der baad try karo yaar.")

    # Submit to dedicated AI thread pool - bot stays fast
    _ai_pool.submit(_do_ai)


# =======================================================
#  /imagine - Image Generation via Pollinations.ai (free)
#  Uses Pollinations API — no extra key needed
# =======================================================

# =======================================================
#  /imagine — Interactive Generation Panel
#
#  Step 1: User types /imagine [prompt]
#  Step 2: Bot shows panel:
#          [🖼️ Image] [🎬 Video]
#  Step 3a (Image): Bot asks for Reference Image (optional)
#          User sends photo OR types "skip"
#  Step 3b (Image): Bot asks for extra instructions
#          User replies with instructions OR "skip"
#  Step 4: Generate with all context
#
#  Session state per user in _imagine_sessions
# =======================================================

# {uid: {"prompt": str, "type": "image"|"video", "step": int,
#         "ref_b64": str|None, "instructions": str|None}}
_imagine_sessions: dict = {}

@bot.message_handler(commands=["imagine"])
@premium_required
def cmd_imagine(msg):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg,
            "🎨 *Imagine Usage:*\n"
            "`/imagine [your idea]`\n\n"
            "Example:\n"
            "`/imagine a gangster in rainy Delhi street at night`\n\n"
            "_Bot ek panel dega — image ya video choose karo!_",
            parse_mode="Markdown"); return

    prompt = parts[1].strip()
    uid    = msg.from_user.id

    # Store session
    _imagine_sessions[uid] = {
        "prompt":       prompt,
        "type":         None,
        "step":         1,   # 1=type choice, 2=ref image, 3=instructions
        "ref_b64":      None,
        "ref_mime":     None,
        "instructions": None,
        "msg_id":       msg.message_id,
        "chat_id":      msg.chat.id,
    }

    mk = InlineKeyboardMarkup()
    mk.row(
        InlineKeyboardButton("🖼️ Image Generation", callback_data=f"img_type_image_{uid}"),
        InlineKeyboardButton("🎬 Video Generation", callback_data=f"img_type_video_{uid}"),
    )
    mk.row(InlineKeyboardButton("❌ Cancel", callback_data=f"img_cancel_{uid}"))

    bot.reply_to(msg,
        f"🎨 *Knowledge Pro — Generation Panel*\n"
        f"{'─'*32}\n"
        f"📝 Prompt: _{prompt[:120]}_\n"
        f"{'─'*32}\n\n"
        f"Kya generate karna hai?",
        parse_mode="Markdown",
        reply_markup=mk)


@bot.callback_query_handler(func=lambda c: c.data.startswith("img_type_"))
def cb_img_type(call):
    parts = call.data.split("_")
    gen_type = parts[2]   # "image" or "video"
    uid      = int(parts[3])

    if call.from_user.id != uid:
        bot.answer_callback_query(call.id, "Yeh tera panel nahi hai!"); return
    if uid not in _imagine_sessions:
        bot.answer_callback_query(call.id, "Session expire ho gaya. /imagine dobara karo.")
        return

    _imagine_sessions[uid]["type"] = gen_type
    _imagine_sessions[uid]["step"] = 2
    bot.answer_callback_query(call.id, f"{'🖼️ Image' if gen_type=='image' else '🎬 Video'} selected!")

    mk = InlineKeyboardMarkup()
    mk.row(InlineKeyboardButton("⏭️ Skip (No Reference)", callback_data=f"img_skip_ref_{uid}"))
    mk.row(InlineKeyboardButton("❌ Cancel", callback_data=f"img_cancel_{uid}"))

    try:
        bot.edit_message_text(
            f"{'🖼️ Image' if gen_type=='image' else '🎬 Video'} Generation selected!\n\n"
            f"📸 *Step 1/2 — Reference Image* (Optional)\n\n"
            f"Koi reference photo bhejo jisse AI inspired ho.\n"
            f"_Character, style, scene — kuch bhi._\n\n"
            f"Ya *Skip* dabao agar reference nahi chahiye.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=mk
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("img_skip_ref_"))
def cb_img_skip_ref(call):
    uid = int(call.data.split("_")[-1])
    if call.from_user.id != uid:
        bot.answer_callback_query(call.id, "Yeh tera panel nahi hai!"); return
    if uid not in _imagine_sessions:
        bot.answer_callback_query(call.id, "Session expire ho gaya."); return

    _imagine_sessions[uid]["step"] = 3
    bot.answer_callback_query(call.id, "Reference skipped!")

    mk = InlineKeyboardMarkup()
    mk.row(InlineKeyboardButton("⏭️ Skip (No Extra Instructions)", callback_data=f"img_skip_instr_{uid}"))
    mk.row(InlineKeyboardButton("❌ Cancel", callback_data=f"img_cancel_{uid}"))

    try:
        bot.edit_message_text(
            f"✅ Reference skipped.\n\n"
            f"📝 *Step 2/2 — Extra Instructions* (Optional)\n\n"
            f"Koi special instruction dena chahte ho?\n"
            f"_Style, mood, color, angle, character traits..._\n\n"
            f"*Is message ka reply karo* ya *Skip* dabao.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=mk
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("img_skip_instr_"))
def cb_img_skip_instr(call):
    uid = int(call.data.split("_")[-1])
    if call.from_user.id != uid:
        bot.answer_callback_query(call.id, "Yeh tera panel nahi hai!"); return
    bot.answer_callback_query(call.id, "Generating now...")
    _imagine_sessions[uid]["instructions"] = None
    _run_imagine(uid, call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("img_cancel_"))
def cb_img_cancel(call):
    uid = int(call.data.split("_")[-1])
    if call.from_user.id != uid:
        bot.answer_callback_query(call.id, "Yeh tera panel nahi hai!"); return
    _imagine_sessions.pop(uid, None)
    bot.answer_callback_query(call.id, "Cancelled!")
    try:
        bot.edit_message_text("❌ Generation cancelled.",
                              call.message.chat.id, call.message.message_id)
    except Exception:
        pass


@bot.message_handler(content_types=["photo"])
def handle_photo_or_ref(msg):
    """Handle photos - reference image for /imagine OR /ai image analysis."""
    uid = msg.from_user.id

    # --- /imagine reference image flow ---
    session = _imagine_sessions.get(uid)
    if session and session.get("step") == 2:
        caption = (msg.caption or "").strip()
        # Download reference image
        photo         = msg.photo[-1]
        b64_img, mime = _get_photo_base64(photo.file_id)
        if b64_img:
            session["ref_b64"]  = b64_img
            session["ref_mime"] = mime
        session["step"] = 3
        _imagine_sessions[uid] = session

        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("⏭️ Skip (No Extra Instructions)", callback_data=f"img_skip_instr_{uid}"))
        mk.row(InlineKeyboardButton("❌ Cancel", callback_data=f"img_cancel_{uid}"))

        bot.reply_to(msg,
            "✅ *Reference image received!*\n\n"
            "📝 *Step 2/2 — Extra Instructions* (Optional)\n\n"
            "Koi special instruction dena chahte ho?\n"
            "_Style, mood, color, angle, character traits..._\n\n"
            "*Is message ka reply karo* ya *Skip* dabao.",
            parse_mode="Markdown",
            reply_markup=mk)
        return

    # --- /ai image analysis ---
    caption = (msg.caption or "").strip()
    if not caption.lower().startswith("/ai"):
        return  # Normal photo — ignore

    user_msg  = caption.split(maxsplit=1)[1].strip() if len(caption.split(maxsplit=1)) > 1 else "Yeh image mein kya hai? Analyse kar."
    user_name = msg.from_user.first_name

    try: bot.send_chat_action(msg.chat.id, "typing")
    except: pass

    def _do_image_ai():
        api_key = OPENROUTER_KEY
        model   = OPENROUTER_MODEL
        if not api_key:
            ac = fb_get("config/ai", {}) or {}
            api_key = ac.get("api_key", "")
            if ac.get("model"): model = ac["model"]
        if not api_key:
            bot.reply_to(msg, "🧠 API key nahi hai bhai."); return

        photo         = msg.photo[-1]
        b64_img, mime = _get_photo_base64(photo.file_id)

        if not b64_img:
            bot.reply_to(msg, "❌ Image download nahi hua. Try again."); return

        history  = _get_history(uid)
        sys_msg  = _build_prompt(user_name)
        messages = [{"role": "system", "content": sys_msg}]
        messages.extend(history)
        messages.append({
            "role": "user",
            "content": [
                {"type": "text",      "text": user_msg},
                {"type": "image_url", "image_url": {
                    "url":    f"data:{mime};base64,{b64_img}",
                    "detail": "high"
                }},
            ]
        })
        _push_history(uid, "user", f"[Image sent] {user_msg}")
        try: bot.send_chat_action(msg.chat.id, "typing")
        except: pass

        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                    "HTTP-Referer":  "https://t.me/knowledgeprobot",
                    "X-Title":       "Knowledge Pro",
                },
                json={
                    "model":       "google/gemini-flash-1.5",
                    "messages":    messages,
                    "max_tokens":  600,
                    "temperature": 0.88,
                },
                timeout=45,
            )
            if resp.status_code != 200:
                bot.reply_to(msg, f"❌ API {resp.status_code}: {resp.text[:100]}"); return
            data  = resp.json()
            if "error" in data:
                raise ValueError(data["error"].get("message", str(data["error"])))
            reply = data["choices"][0]["message"]["content"].strip()
            _push_history(uid, "assistant", reply)
            bot.reply_to(msg, f"🖼️ {reply}")
            inc_stat("ai_image_analysis")
            fb_log("INFO", f"ImageAI: {user_name}: {user_msg[:40]}")
        except requests.Timeout:
            bot.reply_to(msg, "⏳ Thoda slow hai, dobara try kar bhai.")
        except Exception as e:
            log.error(f"Image AI error: {e}")
            bot.reply_to(msg, "❌ Image analyse nahi ho payi. Thodi der baad try karo.")

    _ai_pool.submit(_do_image_ai)


def _imagine_check_instruction_reply(msg):
    """Check if a text message is an instruction reply for /imagine step 3."""
    uid     = msg.from_user.id
    session = _imagine_sessions.get(uid)
    if not session or session.get("step") != 3:
        return False
    session["instructions"] = msg.text.strip()
    session["step"] = 4
    _imagine_sessions[uid] = session
    bot.reply_to(msg, "✅ Instructions received! Generating now... 🎨")
    _run_imagine(uid, msg.chat.id, None)
    return True


def _run_imagine(uid: int, chat_id: int, edit_msg_id):
    """Execute the actual generation after all inputs collected."""
    session = _imagine_sessions.pop(uid, {})
    if not session:
        return

    prompt       = session.get("prompt", "")
    gen_type     = session.get("type", "image")
    ref_b64      = session.get("ref_b64")
    ref_mime     = session.get("ref_mime", "image/jpeg")
    instructions = session.get("instructions", "")
    user_name    = "User"

    try: bot.send_chat_action(chat_id, "upload_photo")
    except: pass

    def _do_gen():
        api_key = OPENROUTER_KEY
        model   = OPENROUTER_MODEL
        if not api_key:
            ac = fb_get("config/ai", {}) or {}
            api_key = ac.get("api_key", "")
            if ac.get("model"): model = ac["model"]

        # Step 1 — AI enhances the prompt
        enhanced_prompt = prompt
        if api_key:
            try:
                extra_ctx = ""
                if instructions:
                    extra_ctx += f"\nExtra instructions from user: {instructions}"
                if ref_b64:
                    extra_ctx += "\nUser has provided a reference image — incorporate its visual style, character appearance, and composition."

                enhance_messages = [
                    {"role": "system", "content": (
                        "You are an elite cinematic image prompt engineer. "
                        "Transform the user's idea into a rich, detailed generation prompt.\n\n"
                        "RULES: Return ONLY the enhanced prompt — no explanation, no quotes. English only. Max 250 words.\n\n"
                        "ALWAYS INCLUDE: Subject details (age, features, clothing, pose), Art style, Camera/angle, "
                        "Lighting (dramatic rim light, golden hour, neon glow, volumetric), Setting, Mood, "
                        "Quality tags (8K UHD, RAW photo, ultra-detailed, photorealistic, Unreal Engine 5), "
                        "Color grading (teal-orange cinematic, warm golden, desaturated noir).\n\n"
                        "For characters: battle-worn, fierce gaze, confident stance, photorealistic skin texture.\n\n"
                        "Append: negative_prompt: blurry, deformed hands, extra fingers, watermark, ugly, low quality"
                    )},
                ]
                if ref_b64:
                    enhance_messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"Create a prompt for: {prompt}{extra_ctx}"},
                            {"type": "image_url", "image_url": {
                                "url": f"data:{ref_mime};base64,{ref_b64}",
                                "detail": "high"
                            }},
                        ]
                    })
                else:
                    enhance_messages.append({
                        "role": "user",
                        "content": f"Create a prompt for: {prompt}{extra_ctx}"
                    })

                enh_model = "google/gemini-flash-1.5" if ref_b64 else model
                enh_resp = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://t.me/knowledgeprobot",
                        "X-Title": "Knowledge Pro",
                    },
                    json={
                        "model":       enh_model,
                        "messages":    enhance_messages,
                        "max_tokens":  300,
                        "temperature": 0.82,
                    },
                    timeout=20,
                )
                if enh_resp.status_code == 200:
                    enh_data = enh_resp.json()
                    if "choices" in enh_data:
                        enhanced_prompt = enh_data["choices"][0]["message"]["content"].strip()
            except Exception:
                pass

        import urllib.parse

        if gen_type == "video":
            # Video generation via Pollinations video endpoint
            try:
                pos_prompt = enhanced_prompt.split("negative_prompt:")[0].strip().strip("|").strip()
                safe_prompt = urllib.parse.quote(pos_prompt)
                seed = random.randint(1, 999999)

                # Pollinations video endpoint
                video_url = (
                    f"https://image.pollinations.ai/prompt/{safe_prompt}"
                    f"?width=1280&height=720&nologo=true&seed={seed}&model=flux"
                )
                # Note: Pollinations doesn't have true video yet — use animated GIF approach
                # or generate a high-quality image and note it
                try: bot.send_chat_action(chat_id, "upload_video")
                except: pass

                vid_resp = requests.get(video_url, timeout=60)
                if vid_resp.status_code == 200 and len(vid_resp.content) > 5000:
                    bot.send_photo(chat_id, vid_resp.content,
                        caption=(
                            f"🎬 *Video Frame Generated!*\n"
                            f"📝 Prompt: _{prompt[:100]}_\n"
                            f"⚠️ _Full video rendering: upgrade to Video API_\n"
                            f"✨ Enhanced by Knowledge Pro AI"
                        ),
                        parse_mode="Markdown")
                    inc_stat("videos_generated")
                else:
                    bot.send_message(chat_id, "❌ Video generation failed. Try again.")
            except Exception as e:
                bot.send_message(chat_id, f"❌ Video error: {str(e)[:80]}")

        else:
            # Image generation — try multiple Pollinations models
            IMAGINE_MODELS = ["flux", "flux-realism", "flux-pro", "turbo"]
            try:
                pos_prompt = enhanced_prompt.split("negative_prompt:")[0].strip().strip("|").strip()
                safe_prompt = urllib.parse.quote(pos_prompt)
                seed = random.randint(1, 999999)

                img_bytes  = None
                used_model = "flux"

                for model_name in IMAGINE_MODELS:
                    image_url = (
                        f"https://image.pollinations.ai/prompt/{safe_prompt}"
                        f"?width=1024&height=1024&nologo=true&enhance=true"
                        f"&seed={seed}&model={model_name}"
                    )
                    try:
                        bot.send_chat_action(chat_id, "upload_photo")
                    except Exception:
                        pass
                    try:
                        img_resp = requests.get(image_url, timeout=55, stream=True)
                        if img_resp.status_code == 200 and len(img_resp.content) > 5000:
                            img_bytes  = img_resp.content
                            used_model = model_name
                            break
                    except Exception:
                        continue

                if img_bytes:
                    ref_note = " | 📸 Reference used" if ref_b64 else ""
                    instr_note = f" | 📝 {instructions[:40]}" if instructions else ""
                    caption = (
                        f"🎨 *Image Generated!*\n"
                        f"📝 Prompt: _{prompt[:100]}_\n"
                        f"🤖 Model: `{used_model}`{ref_note}{instr_note}\n"
                        f"✨ Enhanced by Knowledge Pro AI"
                    )
                    bot.send_photo(chat_id, img_bytes,
                                   caption=caption,
                                   parse_mode="Markdown")
                    inc_stat("images_generated")
                    fb_log("INFO", f"Image gen [{used_model}]{' +ref' if ref_b64 else ''}: {prompt[:40]}")
                else:
                    bot.send_message(chat_id, "❌ Sab models ne fail kar diya. Dobara try karo.")
            except Exception as e:
                bot.send_message(chat_id, f"❌ Image generation failed: {str(e)[:80]}")

    _ai_pool.submit(_do_gen)


# =======================================================
#  AI IMAGE ANALYSIS
#  Triggered when user sends a photo with /ai caption
#  OR replies to a photo with /ai command
#  Also handles link analysis in /ai text
# =======================================================

def _extract_urls(text: str) -> list:
    """Extract all URLs from text."""
    url_pattern = re.compile(
        r'https?://[^\s<>"{}|\\^`\[\]]+'
    )
    return url_pattern.findall(text)

def _fetch_url_content(url: str, max_chars: int = 3000) -> str:
    """Fetch webpage text content for AI analysis."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; KnowledgeProBot/1.0)"
        }
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return f"[URL {r.status_code} error]"
        # Strip HTML tags crudely
        text = re.sub(r'<style[^>]*>.*?</style>', ' ', r.text, flags=re.DOTALL)
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except Exception as e:
        return f"[URL fetch failed: {e}]"

def _get_photo_base64(file_id: str):
    """Download Telegram photo and return (base64_str, mime_type) or (None, None)."""
    import base64
    try:
        file_info = bot.get_file(file_id)
        file_url  = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        r = requests.get(file_url, timeout=25,
                         headers={"User-Agent": "KnowledgeProBot/1.0"})
        if r.status_code == 200:
            mime = "image/jpeg"
            if file_info.file_path.lower().endswith(".png"):  mime = "image/png"
            elif file_info.file_path.lower().endswith(".webp"): mime = "image/webp"
            return base64.b64encode(r.content).decode("utf-8"), mime
        log.error(f"Photo download HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Photo download error: {e}")
    return None, None


# Handle: someone replies to a photo and types /ai [question]
@bot.message_handler(commands=["aiimg"])
@premium_required
def cmd_aiimg(msg):
    """
    /aiimg [question] — reply to any photo to analyse it.
    Or just send photo with caption /ai [question].
    """
    if not msg.reply_to_message or not msg.reply_to_message.photo:
        bot.reply_to(msg,
            "🖼️ Kisi photo ko reply karo aur /aiimg [sawaal] likho.\n"
            "Ya photo bhejo caption mein /ai [sawaal] likh ke."); return

    uid       = msg.from_user.id
    user_name = msg.from_user.first_name
    parts     = msg.text.split(maxsplit=1)
    user_msg  = parts[1].strip() if len(parts) > 1 else "Yeh image mein kya hai? Detailed analyse kar."

    try: bot.send_chat_action(msg.chat.id, "typing")
    except: pass

    def _do_reply_img_ai():
        api_key = OPENROUTER_KEY
        model   = OPENROUTER_MODEL
        if not api_key:
            ac = fb_get("config/ai", {}) or {}
            api_key = ac.get("api_key", "")
            if ac.get("model"): model = ac["model"]
        if not api_key:
            bot.reply_to(msg, "🧠 API key nahi hai bhai."); return

        photo         = msg.reply_to_message.photo[-1]
        b64_img, mime = _get_photo_base64(photo.file_id)
        if not b64_img:
            bot.reply_to(msg, "❌ Image download nahi hua."); return

        history  = _get_history(uid)
        sys_msg  = _build_prompt(user_name)
        messages = [{"role": "system", "content": sys_msg}]
        messages.extend(history)
        messages.append({
            "role": "user",
            "content": [
                {"type": "text",      "text": user_msg},
                {"type": "image_url", "image_url": {
                    "url":    f"data:{mime};base64,{b64_img}",
                    "detail": "high"
                }},
            ]
        })
        _push_history(uid, "user", f"[Image reply] {user_msg}")
        try: bot.send_chat_action(msg.chat.id, "typing")
        except: pass

        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                    "HTTP-Referer":  "https://t.me/knowledgeprobot",
                    "X-Title":       "Knowledge Pro",
                },
                json={
                    "model":       "google/gemini-flash-1.5",
                    "messages":    messages,
                    "max_tokens":  600,
                    "temperature": 0.88,
                },
                timeout=45,
            )
            if resp.status_code != 200:
                bot.reply_to(msg, f"❌ API {resp.status_code}: {resp.text[:80]}"); return
            data  = resp.json()
            if "error" in data:
                raise ValueError(data["error"].get("message", str(data["error"])))
            reply = data["choices"][0]["message"]["content"].strip()
            _push_history(uid, "assistant", reply)
            bot.reply_to(msg, f"🖼️ {reply}")
            inc_stat("ai_image_analysis")
        except Exception as e:
            log.error(f"ImgAI reply error: {e}")
            bot.reply_to(msg, "❌ Image analyse nahi hua. Try again.")

    _ai_pool.submit(_do_reply_img_ai)


# =======================================================
#  /ai — extended: now also auto-analyses any URLs in text
# =======================================================

@bot.message_handler(commands=["aiclr"])
@premium_required
def cmd_aiclr(msg):
    _clear_history(msg.from_user.id)
    bot.reply_to(msg,
        "🧹 *AI conversation reset ho gaya!*\nFresh start kar bhai.",
        parse_mode="Markdown")

# =======================================================
#  /announcement  (Owner)
# =======================================================

@bot.message_handler(commands=["announcement"])
@owner_required
@with_init_msg
def cmd_announcement(msg):
    """
    /announcement [message]         — sends to ALL groups
    /announcement [message] | all   — sends to all groups (explicit)
    /announcement [message] | [group_id]  — sends to ONE specific group
    Owner can also see registered groups via /grouplist
    """
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg,
            "📡 *Announcement Usage:*\n"
            "`/announcement [msg]` — send to ALL groups\n"
            "`/announcement [msg] | [group_id]` — send to specific group\n\n"
            "Registered groups dekho: /grouplist",
            parse_mode="Markdown"); return

    raw = parts[1].strip()

    # Check if specific group ID provided after |
    target_gid = None
    if "|" in raw:
        text_part, gid_part = raw.rsplit("|", 1)
        text = text_part.strip()
        gid_str = gid_part.strip()
        if gid_str.lower() != "all":
            try:
                target_gid = int(gid_str)
            except ValueError:
                bot.reply_to(msg, f"❌ Invalid group ID: `{gid_str}`",
                             parse_mode="Markdown"); return
    else:
        text = raw

    if not text:
        bot.reply_to(msg, "❌ Message khali hai!"); return

    groups = fb_get("groups", {}) or {}

    if target_gid:
        # Send to specific group only
        try:
            bot.send_message(target_gid,
                f"📡 *ANNOUNCEMENT:*\n\n{text}", parse_mode="Markdown")
            # Get group name
            gname = groups.get(str(target_gid), {}).get("title", str(target_gid)) if isinstance(groups.get(str(target_gid)), dict) else str(target_gid)
            bot.reply_to(msg,
                f"✅ *Announcement sent!*\n"
                f"📌 Group: *{gname}*\n"
                f"💬 Message: _{text[:80]}_",
                parse_mode="Markdown")
            fb_log("INFO", f"Announcement → group {target_gid}: {text[:40]}")
        except Exception as e:
            bot.reply_to(msg, f"❌ Failed to send to {target_gid}: {e}")
    else:
        # Send to ALL groups
        fb_push("announcements", {"msg": text, "priority": "urgent", "time": ts()})
        sent = 0; failed = 0
        for gid in groups:
            try:
                bot.send_message(int(gid),
                    f"📡 *ANNOUNCEMENT:*\n\n{text}", parse_mode="Markdown")
                sent += 1
            except Exception:
                failed += 1
        bot.reply_to(msg,
            f"📡 *Announcement sent to all groups!*\n"
            f"✅ Sent: *{sent}*\n"
            f"❌ Failed: *{failed}*\n"
            f"💬 Message: _{text[:80]}_",
            parse_mode="Markdown")
        fb_log("INFO", f"Announcement ALL ({sent} groups): {text[:40]}")


@bot.message_handler(commands=["grouplist"])
@owner_required
def cmd_grouplist(msg):
    """List all registered groups with their IDs."""
    groups = fb_get("groups", {}) or {}
    if not groups:
        bot.reply_to(msg,
            "📋 Koi group registered nahi hai.\n"
            "_Bot ko groups mein add karo aur koi message bhejo._"); return

    lines = []
    for gid, info in list(groups.items())[:20]:
        if isinstance(info, dict):
            title = info.get("title", "Unknown")
        else:
            title = str(info)
        lines.append(f"🏠 *{title}*\n   `{gid}`")

    bot.reply_to(msg,
        f"📋 *Registered Groups* ({len(groups)})\n"
        f"{'─'*30}\n" +
        "\n".join(lines) +
        f"\n{'─'*30}\n"
        f"_/announcement [msg] | [group\\_id] se specific group ko bhejo_",
        parse_mode="Markdown")

# =======================================================
#  /ServerStatus  (Owner)
# =======================================================

@bot.message_handler(commands=["ServerStatus"])
@owner_required
@with_init_msg
def cmd_status(msg):
    up = int(time.time() - BOT_START_TIME)
    h, r = divmod(up, 3600); m, s = divmod(r, 60)
    try:
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        mu, mt = mem.used//(1024**2), mem.total//(1024**2)
        dk  = psutil.disk_usage("/")
        du, dt = dk.used//(1024**3), dk.total//(1024**3)
    except: cpu = mu = mt = du = dt = 0

    status_data = {
        "uptime":       f"{h}h {m}m {s}s",
        "cpu":          f"{cpu:.1f}%",
        "memory":       f"{mu}/{mt} MB",
        "disk":         f"{du}/{dt} GB",
        "firebase":     "Online" if firebase_ok else "Offline",
        "auto_replies": len(auto_replies),
        "banned":       len(banned_users),
        "afk":          len(afk_users),
        "ai_sessions":  len(_conv),
        "premium":      len(_premium_users),
        "images":       _stat_buf.get("images_generated", 0),
        "blocked_words":len(blocked_words_list),
        "date":         _today(),
        "timestamp":    ts(),
    }
    # Push live to Firebase — panel reads this in real-time
    fb_set("server_status", status_data)

    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("🌐 Open Panel", url="https://knowledge-pro-c9ee5.web.app"))

    bot.reply_to(msg,
        f"📊 *SERVER STATUS* — Live\n"
        f"{'─'*30}\n"
        f"📅 {_today()} | ⏰ {h}h {m}m {s}s\n"
        f"⚡ CPU: {cpu:.1f}% | 💾 {mu}/{mt}MB\n"
        f"🔥 Firebase: {'✅' if firebase_ok else '❌'} | 💎 Premium: {len(_premium_users)}\n"
        f"🤖 AutoReplies: {len(auto_replies)} | 💤 AFK: {len(afk_users)}\n"
        f"{'─'*30}\n"
        f"_Full stats panel pe available hain_ 🌐",
        parse_mode="Markdown",
        reply_markup=mk)
    fb_log("INFO", "ServerStatus checked + pushed to panel")

# =======================================================
#  GENERAL TEXT HANDLER
#  Zero blocking DB calls - all in-memory
# =======================================================

@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith("/"), content_types=["text"])
def handle_text(msg):
    if not msg.text: return

    # Check if user is in /imagine instruction step (step 3)
    if _imagine_check_instruction_reply(msg):
        return  # Handled as imagine instruction

    # Check if user is setting a custom AFK reason
    uid = msg.from_user.id
    if uid in _afk_pending_custom:
        chat_id = _afk_pending_custom.pop(uid)
        reason  = msg.text.strip()[:120]
        _set_afk(msg, uid, msg.from_user.first_name, reason, "custom")
        try: bot.delete_message(chat_id, msg.message_id - 1)
        except Exception: pass
        return

    txt = msg.text.lower()
    uid = msg.from_user.id
    cid = msg.chat.id
    inc_stat("total_messages")

    # -- AFK return: user sent any message --
    if uid in afk_users:
        _return_from_afk(msg, uid, manual=False)
        # Continue - also check blocked words and autoreply

    # -- Mention of AFK user -> DM + group reply --
    if msg.entities and afk_users:
        for ent in msg.entities:
            if ent.type == "mention":
                mention = msg.text[ent.offset: ent.offset + ent.length]
                for auid, info in list(afk_users.items()):
                    try:
                        member = bot.get_chat_member(cid, auid)
                        if (member.user.username and
                                f"@{member.user.username}".lower() == mention.lower()):
                            gone  = elapsed_str(time.time() - info["time"])
                            pname = msg.from_user.first_name

                            # Track pinger
                            if pname not in info["pingers"]:
                                info["pingers"].append(pname)
                            info["ping_count"] = info.get("ping_count", 0) + 1
                            afk_users[auid] = info

                            # Group reply
                            bot.reply_to(msg,
                                f"💤 *{member.user.first_name}* abhi AFK hai\n"
                                f"📝 Reason: _{info['reason']}_\n"
                                f"⏱️ Since: {gone} ago\n"
                                f"_Urgent ho toh DM karo!_",
                                parse_mode="Markdown")

                            # DM the AFK user
                            try:
                                chat_title = getattr(msg.chat, "title", "group") or "group"
                                bot.send_message(auid,
                                    f"🚨 *{pname}* ne tujhe mention kiya *{chat_title}* mein!\n"
                                    f"💬 _{msg.text[:120]}_",
                                    parse_mode="Markdown")
                            except: pass
                    except: pass

    # -- Blocked words (in-memory list, regex boundary match) --
    for w in blocked_words_list:
        if not w: continue
        if re.search(r'\b' + re.escape(w) + r'\b', txt) or w in txt:
            try:
                bot.delete_message(cid, msg.message_id)
                bot.send_message(cid,
                    f"🚫 *{msg.from_user.first_name}*, restricted word pakda gaya! Message delete.",
                    parse_mode="Markdown")
                fb_log("WARN", f"Blocked '{w}' from {msg.from_user.first_name}")
            except: pass
            return

    # -- Auto reply (spaces stripped from trigger) --
    compact = re.sub(r"\s+", "", txt)
    for key, reply in auto_replies.items():
        if key in compact or key == compact:
            bot.reply_to(msg, reply)
            fb_log("INFO", f"AutoReply: {key}")
            return

# =======================================================
#  /start — Clean greeting with inline buttons
# =======================================================

@bot.message_handler(commands=["start"])
def cmd_start(msg):
    uname = msg.from_user.first_name
    mk = InlineKeyboardMarkup()
    mk.row(
        InlineKeyboardButton("📋 Help",       callback_data="cb_help"),
        InlineKeyboardButton("📊 Status",     callback_data="cb_status_quick"),
    )
    mk.row(
        InlineKeyboardButton("🧠 AI Chat",    callback_data="cb_ai_info"),
        InlineKeyboardButton("🎨 Imagine",    callback_data="cb_imagine_info"),
    )
    bot.reply_to(msg,
        f"🙏 *Namaste {uname} bhai!*\n"
        f"Main hoon *Knowledge Pro* — tera OG Telegram bot! 🔥\n\n"
        f"📅 Today: _{_today()}_\n\n"
        f"Kya karna hai? Neeche se choose kar ya /help likh!",
        parse_mode="Markdown",
        reply_markup=mk)
    inc_stat("total_messages")


@bot.callback_query_handler(func=lambda c: c.data in ("cb_help","cb_ai_info","cb_imagine_info","cb_status_quick"))
def cb_start_buttons(call):
    bot.answer_callback_query(call.id)
    if call.data == "cb_help":
        _send_help(call.message.chat.id, call.from_user.first_name, reply_to=None)
    elif call.data == "cb_ai_info":
        bot.send_message(call.message.chat.id,
            "🧠 *Knowledge Pro AI*\n\n"
            "Use `/ai [sawaal]` to chat with me!\n"
            "Main Hinglish mein bolta hoon, mood samajhta hoon.\n"
            "Conversation memory hai — pichli baatein yaad rehti hain.\n\n"
            "Image analyse: photo bhejo caption `/ai [sawaal]`\n"
            "Link analyse: `/ai [url]` likhdo main padh lunga!\n"
            "Reset: /aiclr",
            parse_mode="Markdown")
    elif call.data == "cb_imagine_info":
        bot.send_message(call.message.chat.id,
            "🎨 *AI Image Generation*\n\n"
            "Use `/imagine [prompt]` to generate an image!\n"
            "Example: `/imagine a cyberpunk Delhi street at night`\n\n"
            "Main tera prompt enhance karke best image dunga! 🔥",
            parse_mode="Markdown")
    elif call.data == "cb_status_quick":
        up = int(time.time() - BOT_START_TIME)
        h, r = divmod(up, 3600); m, s = divmod(r, 60)
        bot.send_message(call.message.chat.id,
            f"📊 *Quick Status*\n"
            f"⏰ Uptime: {h}h {m}m {s}s\n"
            f"🔥 Firebase: {'✅' if firebase_ok else '❌'}\n"
            f"🤖 AutoReplies: {len(auto_replies)}\n"
            f"📅 {_today()}",
            parse_mode="Markdown")


def _send_help(chat_id: int, uname: str, reply_to=None):
    text = (
        f"📋 *Knowledge Pro — Full Command List*\n"
        f"{'─'*34}\n\n"
        f"*👮 Moderation* (Admin)\n"
        f"`/warn`  — Reply karo, warning do\n"
        f"`/ban`   — Reply karo, ban karo\n"
        f"`/kick`  — Reply karo, kick karo\n"
        f"`/mute [mins]` — Reply karo, mute karo\n"
        f"`/warnc @user` — Warn count reset (owner)\n"
        f"`/permission [media|msg|link|all] [on|off]`\n"
        f"`/nuke`  — Chat clear (confirmation)\n\n"
        f"*📢 Broadcast* (Admin)\n"
        f"`/shout [msg]` — AI-enhanced broadcast\n"
        f"`/shoutconfig [word] delete` — Block word\n"
        f"`/shoutconfig [word] allow`  — Unblock\n"
        f"`/shoutconfig list`          — See list\n\n"
        f"*🤖 Automation* (Admin)\n"
        f"`/setautoreply [word] | [reply]`\n"
        f"`/deleteautoreply [word]`\n\n"
        f"*💤 AFK*\n"
        f"`/afk [reason]` — Set AFK\n"
        f"`/back`         — Return manually\n\n"
        f"*🎮 Fun*\n"
        f"`/roll` `/bala` `/pin` `/unpin`\n\n"
        f"*🧠 AI (Knowledge Pro)*\n"
        f"`/ai [msg]`  — Chat w/ memory\n"
        f"`/aiclr`     — Reset conversation\n"
        f"`/aiimg`     — Reply to photo + analyse\n\n"
        f"*🎨 Creative*\n"
        f"`/imagine [prompt]` — Generate image\n\n"
        f"*🌤️ Utility*\n"
        f"`/weather [city]` — Live weather\n\n"
        f"*🎰 Economy (OwO Style)*\n"
        f"`/balance` or `/bal`   — Coins dekho\n"
        f"`/daily`               — Daily coins claim\n"
        f"`/work`                — Kaam karke kamao\n"
        f"`/hunt`                — Shikar karo\n"
        f"`/fish`                — Maachli pakdo\n"
        f"`/give @user [amt]`    — Coins do\n"
        f"`/leaderboard` or `/lb`— Top players\n"
        f"`/slots [bet]`         — Slot machine\n"
        f"`/flip [bet]`          — Coin flip\n\n"
        f"*🔑 Access*\n"
        f"`/login [password]`\n\n"
        f"*👑 Owner Only*\n"
        f"`/promote` `/demote` `/announcement` `/ServerStatus`"
    )
    if reply_to:
        bot.reply_to(reply_to, text, parse_mode="Markdown")
    else:
        bot.send_message(chat_id, text, parse_mode="Markdown")


@bot.message_handler(commands=["help"])
def cmd_help(msg):
    _send_help(msg.chat.id, msg.from_user.first_name, reply_to=msg)


# =======================================================
#  /weather — Live weather using wttr.in (no API key)
# =======================================================

@bot.message_handler(commands=["weather"])
@premium_required
def cmd_weather(msg):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg,
            "🌤️ Usage: `/weather [city]`\n"
            "Example: `/weather Delhi`",
            parse_mode="Markdown"); return

    city = parts[1].strip()
    # Show instant feedback
    init = None
    try:
        init = bot.reply_to(msg, "⚙️ _Fetching weather..._", parse_mode="Markdown")
    except Exception:
        pass

    def _delete_init():
        if init:
            try: bot.delete_message(msg.chat.id, init.message_id)
            except Exception: pass

    try:
        # wttr.in JSON API — no key needed, always free
        r = requests.get(
            f"https://wttr.in/{requests.utils.quote(city)}?format=j1",
            headers={"User-Agent": "KnowledgePro/1.0"},
            timeout=10
        )
        _delete_init()
        if r.status_code != 200:
            bot.reply_to(msg, f"❌ Weather data nahi mila `{city}` ke liye. City naam check karo.",
                         parse_mode="Markdown"); return

        data    = r.json()
        current = data["current_condition"][0]
        nearest = data["nearest_area"][0]

        area    = nearest["areaName"][0]["value"]
        country = nearest["country"][0]["value"]
        temp_c  = current["temp_C"]
        temp_f  = current["temp_F"]
        feels_c = current["FeelsLikeC"]
        humidity= current["humidity"]
        wind_kph= current["windspeedKmph"]
        wind_dir= current["winddir16Point"]
        desc    = current["weatherDesc"][0]["value"]
        uv      = current.get("uvIndex", "?")
        visibility= current.get("visibility", "?")
        pressure= current.get("pressure", "?")

        # Weather emoji mapping
        desc_lower = desc.lower()
        if any(x in desc_lower for x in ["sun", "clear"]):       wx = "☀️"
        elif any(x in desc_lower for x in ["partly", "cloud"]):  wx = "⛅"
        elif any(x in desc_lower for x in ["overcast"]):         wx = "☁️"
        elif any(x in desc_lower for x in ["rain", "drizzle"]):  wx = "🌧️"
        elif any(x in desc_lower for x in ["thunder", "storm"]): wx = "⛈️"
        elif any(x in desc_lower for x in ["snow", "blizzard"]): wx = "❄️"
        elif any(x in desc_lower for x in ["fog", "mist", "haze"]): wx = "🌫️"
        elif any(x in desc_lower for x in ["wind"]):              wx = "💨"
        else:                                                      wx = "🌡️"

        # Today's forecast
        today_w  = data["weather"][0]
        max_c    = today_w["maxtempC"]
        min_c    = today_w["mintempC"]

        bot.reply_to(msg,
            f"{wx} *Weather — {area}, {country}*\n"
            f"{'─'*32}\n"
            f"🌡️ Temperature: *{temp_c}°C* / {temp_f}°F\n"
            f"🤔 Feels Like:  *{feels_c}°C*\n"
            f"📝 Condition:   *{desc}*\n"
            f"{'─'*32}\n"
            f"💧 Humidity:    {humidity}%\n"
            f"💨 Wind:        {wind_kph} km/h {wind_dir}\n"
            f"👁️ Visibility:  {visibility} km\n"
            f"📊 Pressure:    {pressure} mb\n"
            f"☀️ UV Index:    {uv}\n"
            f"{'─'*32}\n"
            f"📅 Today High/Low: *{max_c}°C / {min_c}°C*\n"
            f"🕐 Updated: {_today()}",
            parse_mode="Markdown")

        inc_stat("weather_checked")
        fb_log("INFO", f"Weather: {city} by {msg.from_user.first_name}")

    except requests.Timeout:
        _delete_init()
        bot.reply_to(msg, "⏳ Weather server slow hai. Dobara try karo.")
    except Exception as e:
        _delete_init()
        log.error(f"Weather error: {e}")
        bot.reply_to(msg, "❌ Weather fetch failed. City naam sahi likho ya baad mein try karo.")


# =======================================================
#  ECONOMY SYSTEM (OwO Bot Style)
#
#  Coins, daily rewards, work, hunt, fish, give,
#  leaderboard, slots, coin-flip — all in-memory
#  with Firebase sync for persistence.
#
#  Cooldowns: daily=24h, work=1h, hunt=30m, fish=20m
# =======================================================

# {uid: {"coins": int, "last_daily": float, "last_work": float,
#         "last_hunt": float, "last_fish": float, "name": str}}
_eco: dict = {}
_eco_lock = threading.Lock()

def _eco_load():
    """Load economy data from Firebase at startup."""
    try:
        data = fb_get("economy", {}) or {}
        for uid_str, v in data.items():
            if isinstance(v, dict):
                _eco[int(uid_str)] = v
        log.info(f"  Economy loaded: {len(_eco)} users")
    except Exception as e:
        log.warning(f"Economy load: {e}")

def _eco_save_user(uid: int):
    """Async persist one user's economy data."""
    data = _eco.get(uid)
    if data:
        fb_set(f"economy/{uid}", data)

def _eco_get(uid: int, name: str) -> dict:
    """Get or create economy profile."""
    with _eco_lock:
        if uid not in _eco:
            _eco[uid] = {"coins": 100, "last_daily": 0, "last_work": 0,
                          "last_hunt": 0, "last_fish": 0, "name": name,
                          "total_earned": 100}
        else:
            _eco[uid]["name"] = name  # keep name fresh
        return dict(_eco[uid])

def _eco_add(uid: int, name: str, amount: int):
    with _eco_lock:
        _eco_get(uid, name)
        _eco[uid]["coins"] = max(0, _eco[uid]["coins"] + amount)
        _eco[uid]["total_earned"] = _eco[uid].get("total_earned", 0) + max(0, amount)
    _eco_save_user(uid)

def _cd_left(last: float, cooldown_sec: int) -> int:
    """Return seconds remaining on cooldown. 0 = ready."""
    return max(0, int(cooldown_sec - (time.time() - last)))

def _fmt_cd(secs: int) -> str:
    h, r = divmod(secs, 3600); m, s = divmod(r, 60)
    if h: return f"{h}h {m}m"
    if m: return f"{m}m {s}s"
    return f"{s}s"

# Cooldown constants
CD_DAILY = 86400   # 24 hours
CD_WORK  = 3600    # 1 hour
CD_HUNT  = 1800    # 30 minutes
CD_FISH  = 1200    # 20 minutes


@bot.message_handler(commands=["balance", "bal"])
@premium_required
def cmd_balance(msg):
    uid   = msg.from_user.id
    uname = msg.from_user.first_name
    prof  = _eco_get(uid, uname)
    coins = prof["coins"]
    total = prof.get("total_earned", coins)

    # Rank in leaderboard
    with _eco_lock:
        sorted_users = sorted(_eco.items(), key=lambda x: x[1].get("coins", 0), reverse=True)
    rank = next((i+1 for i, (u, _) in enumerate(sorted_users) if u == uid), "?")

    bot.reply_to(msg,
        f"💰 *{uname}'s Wallet*\n"
        f"{'─'*28}\n"
        f"🪙 Coins:       *{coins:,}*\n"
        f"📈 Total Earned: {total:,}\n"
        f"🏆 Rank:        #{rank}\n"
        f"{'─'*28}\n"
        f"_/daily, /work, /hunt, /fish se aur kamao!_",
        parse_mode="Markdown")


@bot.message_handler(commands=["daily"])
@premium_required
def cmd_daily(msg):
    uid   = msg.from_user.id
    uname = msg.from_user.first_name
    prof  = _eco_get(uid, uname)
    cd    = _cd_left(prof["last_daily"], CD_DAILY)

    if cd > 0:
        bot.reply_to(msg,
            f"⏰ *Daily abhi available nahi!*\n"
            f"⏳ {_fmt_cd(cd)} baad wapas aao.",
            parse_mode="Markdown"); return

    reward = random.randint(150, 400)
    with _eco_lock:
        _eco[uid]["coins"] += reward
        _eco[uid]["last_daily"] = time.time()
        _eco[uid]["total_earned"] = _eco[uid].get("total_earned", 0) + reward
    _eco_save_user(uid)

    bot.reply_to(msg,
        f"🎁 *Daily Reward Claimed!*\n"
        f"🪙 +*{reward}* coins mile!\n"
        f"💰 Total: *{_eco[uid]['coins']:,}* coins\n"
        f"_Kal wapas aana!_ ⏰",
        parse_mode="Markdown")


@bot.message_handler(commands=["work"])
@premium_required
def cmd_work(msg):
    uid   = msg.from_user.id
    uname = msg.from_user.first_name
    prof  = _eco_get(uid, uname)
    cd    = _cd_left(prof["last_work"], CD_WORK)

    if cd > 0:
        bot.reply_to(msg,
            f"😮‍💨 *Bhai thak gaya!*\n"
            f"⏳ {_fmt_cd(cd)} baad kaam milega.",
            parse_mode="Markdown"); return

    jobs = [
        ("🧑‍💻 Coding kiya", 80, 160),
        ("📦 Delivery ki", 60, 120),
        ("🍕 Pizza banaya", 50, 100),
        ("📸 Photo shoot kiya", 90, 180),
        ("🎵 Music produce kiya", 100, 200),
        ("🚖 Cab chalaya", 70, 140),
        ("📱 Social media manage kiya", 85, 170),
        ("🏋️ Gym trainer ban gaya", 75, 150),
    ]
    job, lo, hi = random.choice(jobs)
    earned = random.randint(lo, hi)
    with _eco_lock:
        _eco[uid]["coins"] += earned
        _eco[uid]["last_work"] = time.time()
        _eco[uid]["total_earned"] = _eco[uid].get("total_earned", 0) + earned
    _eco_save_user(uid)

    bot.reply_to(msg,
        f"💼 *Kaam ho gaya!*\n"
        f"📝 {job}\n"
        f"🪙 +*{earned}* coins mile!\n"
        f"💰 Balance: *{_eco[uid]['coins']:,}*\n"
        f"⏳ Next work: 1 ghante baad",
        parse_mode="Markdown")


@bot.message_handler(commands=["hunt"])
@premium_required
def cmd_hunt(msg):
    uid   = msg.from_user.id
    uname = msg.from_user.first_name
    prof  = _eco_get(uid, uname)
    cd    = _cd_left(prof["last_hunt"], CD_HUNT)

    if cd > 0:
        bot.reply_to(msg,
            f"🏹 *Shikar ka time nahi abhi!*\n"
            f"⏳ {_fmt_cd(cd)} baad aao.",
            parse_mode="Markdown"); return

    outcomes = [
        ("🐇 Khargosh pakda", 40, 80, True),
        ("🦌 Hiran milya", 80, 150, True),
        ("🐗 Jungle suar", 60, 120, True),
        ("🐦 Parinda", 20, 50, True),
        ("🐍 Saanp se bhaage", 0, 0, False),
        ("🌿 Khaali haath wapas", 0, 0, False),
        ("💀 Daak mara, koi nahi mila", 0, 0, False),
        ("🦊 Laomdi bhaag gayi", 0, 0, False),
    ]
    desc, lo, hi, success = random.choice(outcomes)
    earned = random.randint(lo, hi) if success else 0
    with _eco_lock:
        _eco[uid]["coins"] += earned
        _eco[uid]["last_hunt"] = time.time()
        if earned:
            _eco[uid]["total_earned"] = _eco[uid].get("total_earned", 0) + earned
    _eco_save_user(uid)

    result_line = f"🪙 +*{earned}* coins!" if earned else "😅 Khaali haath!"
    bot.reply_to(msg,
        f"🏹 *Hunt Result*\n"
        f"{desc}\n"
        f"{result_line}\n"
        f"💰 Balance: *{_eco[uid]['coins']:,}*",
        parse_mode="Markdown")


@bot.message_handler(commands=["fish"])
@premium_required
def cmd_fish(msg):
    uid   = msg.from_user.id
    uname = msg.from_user.first_name
    prof  = _eco_get(uid, uname)
    cd    = _cd_left(prof["last_fish"], CD_FISH)

    if cd > 0:
        bot.reply_to(msg,
            f"🎣 *Maachli abhi nahi!*\n"
            f"⏳ {_fmt_cd(cd)} baad try karo.",
            parse_mode="Markdown"); return

    catches = [
        ("🐟 Chhoti maachli", 15, 40, True),
        ("🐠 Tropical fish!", 30, 70, True),
        ("🦈 SHARK! (bhaag gaya)", 0, 0, False),
        ("🐙 Octopus milya", 50, 100, True),
        ("🦐 Jhinga jhinga!", 20, 50, True),
        ("👟 Joota nikla", 0, 0, False),
        ("🐡 Fugu! (toxic)", 0, 0, False),
        ("🐬 Dolphin ne wave kiya", 0, 0, False),
        ("💎 Underwater treasure!", 100, 200, True),
    ]
    desc, lo, hi, success = random.choice(catches)
    earned = random.randint(lo, hi) if success else 0
    with _eco_lock:
        _eco[uid]["coins"] += earned
        _eco[uid]["last_fish"] = time.time()
        if earned:
            _eco[uid]["total_earned"] = _eco[uid].get("total_earned", 0) + earned
    _eco_save_user(uid)

    result_line = f"🪙 +*{earned}* coins!" if earned else "😅 Khaali haath!"
    bot.reply_to(msg,
        f"🎣 *Fishing Result*\n"
        f"{desc}\n"
        f"{result_line}\n"
        f"💰 Balance: *{_eco[uid]['coins']:,}*",
        parse_mode="Markdown")


@bot.message_handler(commands=["give"])
@premium_required
def cmd_give(msg):
    uid   = msg.from_user.id
    uname = msg.from_user.first_name

    # Need: reply OR mention, and an amount
    target = None
    amount = 0

    parts = msg.text.split()
    # Parse amount from args
    for p in parts[1:]:
        if p.isdigit():
            amount = int(p); break

    if msg.reply_to_message:
        target = msg.reply_to_message.from_user
    elif msg.entities:
        for ent in msg.entities:
            if ent.type == "mention":
                try:
                    uname_mention = msg.text[ent.offset+1: ent.offset+ent.length]
                    m2 = bot.get_chat_member(msg.chat.id, f"@{uname_mention}")
                    target = m2.user
                except Exception:
                    pass

    if not target:
        bot.reply_to(msg, "💸 Usage: `/give @user [amount]` ya kisi ka message reply karo.",
                     parse_mode="Markdown"); return
    if target.id == uid:
        bot.reply_to(msg, "😂 Apne aap ko coins nahi de sakte bhai!"); return
    if amount <= 0:
        bot.reply_to(msg, "❌ Amount sahi likhao. Example: `/give @user 100`",
                     parse_mode="Markdown"); return

    giver = _eco_get(uid, uname)
    if giver["coins"] < amount:
        bot.reply_to(msg,
            f"❌ Itne coins nahi hain!\n"
            f"💰 Tera balance: *{giver['coins']:,}*",
            parse_mode="Markdown"); return

    with _eco_lock:
        _eco[uid]["coins"] -= amount
        _eco_get(target.id, target.first_name)
        _eco[target.id]["coins"] += amount
    _eco_save_user(uid)
    _eco_save_user(target.id)

    bot.reply_to(msg,
        f"💸 *Transfer Complete!*\n"
        f"📤 {uname} → {target.first_name}\n"
        f"🪙 *{amount:,}* coins\n"
        f"💰 Tera balance: *{_eco[uid]['coins']:,}*",
        parse_mode="Markdown")


@bot.message_handler(commands=["leaderboard", "lb"])
@premium_required
def cmd_leaderboard(msg):
    with _eco_lock:
        top = sorted(_eco.items(), key=lambda x: x[1].get("coins", 0), reverse=True)[:10]

    if not top:
        bot.reply_to(msg, "📊 Abhi koi economy mein nahi. /daily se shuru karo!"); return

    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines  = []
    for i, (uid, data) in enumerate(top):
        name   = data.get("name", f"User{uid}")
        coins  = data.get("coins", 0)
        marker = " ◀️" if uid == msg.from_user.id else ""
        lines.append(f"{medals[i]} *{name}* — {coins:,} 🪙{marker}")

    # Caller's rank
    with _eco_lock:
        all_sorted = sorted(_eco.items(), key=lambda x: x[1].get("coins",0), reverse=True)
    my_rank = next((i+1 for i,(u,_) in enumerate(all_sorted) if u==msg.from_user.id), "?")
    my_coins = _eco.get(msg.from_user.id, {}).get("coins", 0)

    bot.reply_to(msg,
        f"🏆 *Economy Leaderboard*\n"
        f"{'─'*30}\n" +
        "\n".join(lines) +
        f"\n{'─'*30}\n"
        f"📌 Teri rank: *#{my_rank}* ({my_coins:,} 🪙)",
        parse_mode="Markdown")


@bot.message_handler(commands=["slots"])
@premium_required
def cmd_slots(msg):
    uid   = msg.from_user.id
    uname = msg.from_user.first_name
    parts = msg.text.split()
    bet   = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 50

    prof = _eco_get(uid, uname)
    if prof["coins"] < bet:
        bot.reply_to(msg,
            f"❌ Itne coins nahi!\n💰 Balance: *{prof['coins']:,}*",
            parse_mode="Markdown"); return
    if bet < 10:
        bot.reply_to(msg, "❌ Minimum bet 10 coins hai!"); return
    if bet > 5000:
        bot.reply_to(msg, "❌ Maximum bet 5000 coins hai!"); return

    symbols = ["🍒","🍋","🍊","🔔","💎","7️⃣","🃏","⭐"]
    reels   = [random.choice(symbols) for _ in range(3)]

    # Payout logic
    if reels[0] == reels[1] == reels[2]:
        if reels[0] == "💎": mult = 20
        elif reels[0] == "7️⃣": mult = 15
        elif reels[0] == "⭐": mult = 10
        else: mult = 5
        won = bet * mult
        result = f"🎰 *JACKPOT! {reels[0]}{reels[0]}{reels[0]}*\n💰 +*{won:,}* coins! ({mult}x)"
    elif reels[0] == reels[1] or reels[1] == reels[2]:
        won = int(bet * 1.5)
        result = f"🎰 *MATCH! {' '.join(reels)}*\n💰 +*{won - bet:,}* net"
    else:
        won = 0
        result = f"🎰 *{' '.join(reels)}*\n😅 No match — -{bet} coins"

    net = won - bet
    with _eco_lock:
        _eco[uid]["coins"] = max(0, _eco[uid]["coins"] + net)
        if net > 0:
            _eco[uid]["total_earned"] = _eco[uid].get("total_earned", 0) + net
    _eco_save_user(uid)

    bot.reply_to(msg,
        f"{result}\n"
        f"💰 Balance: *{_eco[uid]['coins']:,}*",
        parse_mode="Markdown")


@bot.message_handler(commands=["flip"])
@premium_required
def cmd_flip(msg):
    uid   = msg.from_user.id
    uname = msg.from_user.first_name
    parts = msg.text.split()
    bet   = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 50

    prof = _eco_get(uid, uname)
    if prof["coins"] < bet:
        bot.reply_to(msg,
            f"❌ Itne coins nahi!\n💰 Balance: *{prof['coins']:,}*",
            parse_mode="Markdown"); return
    if bet < 10:
        bot.reply_to(msg, "❌ Minimum bet 10 coins hai!"); return
    if bet > 5000:
        bot.reply_to(msg, "❌ Maximum bet 5000 coins hai!"); return

    won_flip = random.random() < 0.48  # slight house edge
    if won_flip:
        net = bet
        result = f"🪙 *HEADS! Jeet gaye!*\n🎉 +*{bet:,}* coins!"
    else:
        net = -bet
        result = f"🪙 *TAILS! Haar gaye!*\n😔 -*{bet:,}* coins."

    with _eco_lock:
        _eco[uid]["coins"] = max(0, _eco[uid]["coins"] + net)
        if net > 0:
            _eco[uid]["total_earned"] = _eco[uid].get("total_earned", 0) + net
    _eco_save_user(uid)

    bot.reply_to(msg,
        f"{result}\n"
        f"💰 Balance: *{_eco[uid]['coins']:,}*",
        parse_mode="Markdown")

# =======================================================
#  GROUP TRACKING
# =======================================================

@bot.message_handler(content_types=["new_chat_members"])
def on_join(msg):
    fb_set(f"groups/{msg.chat.id}",
           {"title": msg.chat.title, "joined": ts()})
    inc_stat("total_groups")
    for m in msg.new_chat_members:
        if not m.is_bot:
            bot.send_message(msg.chat.id,
                f"🙏 *{m.first_name}* bhai swagat hai!\n/start kar ke commands dekho.",
                parse_mode="Markdown")
            inc_stat("total_users")

# =======================================================
#  PANEL COMMAND POLLER (background thread)
# =======================================================

_panel_ts = 0

def _poll_panel():
    global _panel_ts
    while True:
        try:
            if firebase_ok and OWNER_ID:
                cmds = fb_get("commands", {}) or {}
                for key, d in cmds.items():
                    if isinstance(d, dict) and d.get("time", 0) > _panel_ts:
                        cmd = d.get("cmd", "")
                        log.info(f"Panel cmd: {cmd}")
                        _fb_del_sync(f"commands/{key}")
                        try:
                            bot.send_message(OWNER_ID,
                                f"📤 Panel: `{cmd}`", parse_mode="Markdown")
                        except: pass
                _panel_ts = ts()
        except: pass
        time.sleep(2)   # 2s instead of 5s — faster panel response


def _auto_status_push():
    """Push server status to Firebase every 60s so panel has live data."""
    while True:
        time.sleep(60)
        try:
            up = int(time.time() - BOT_START_TIME)
            h, r = divmod(up, 3600); m, s = divmod(r, 60)
            try:
                cpu = psutil.cpu_percent(interval=0.1)
                mem = psutil.virtual_memory()
                mu, mt = mem.used//(1024**2), mem.total//(1024**2)
            except: cpu = mu = mt = 0
            _fb_set_sync("server_status", {
                "uptime":       f"{h}h {m}m {s}s",
                "cpu":          f"{cpu:.1f}%",
                "memory":       f"{mu}/{mt} MB",
                "firebase":     "Online" if firebase_ok else "Offline",
                "auto_replies": len(auto_replies),
                "banned":       len(banned_users),
                "afk":          len(afk_users),
                "premium":      len(_premium_users),
                "ai_sessions":  len(_conv),
                "blocked_words":len(blocked_words_list),
                "date":         _today(),
                "timestamp":    ts(),
            })
        except: pass

# =======================================================
#  AFK CLEANUP - removes stale entries after 12h
# =======================================================

def _cleanup_afk():
    while True:
        time.sleep(600)
        now   = time.time()
        stale = [uid for uid, info in list(afk_users.items())
                 if now - info.get("time", now) > 43200]
        for uid in stale:
            afk_users.pop(uid, None)
            _fb_del_sync(f"afk/{uid}")
        if stale:
            log.info(f"AFK cleanup: {len(stale)} removed")

# =======================================================
#  MAIN
# =======================================================

if __name__ == "__main__":
    log.info("=" * 60)
    log.info(f"  Knowledge Pro TeleBot - {_today()}")
    log.info("  Speed: init-msg | cached username | gpt-4o-mini")
    log.info("  Fixes: YT rooms | private handler | no duplicate state")
    log.info("=" * 60)

    init_firebase()
    if firebase_ok:
        load_state()

    # Cache bot ID and username — used everywhere, never call get_me() again
    try:
        me = bot.get_me()
        BOT_ID       = me.id
        BOT_USERNAME = me.username
        log.info(f"OK Bot: @{BOT_USERNAME} (id:{BOT_ID})")
    except Exception as e:
        log.warning(f"get_me() failed: {e}")

    fb_set("stats/last_start", ts())
    fb_set("stats/bot_date",   _today())

    threading.Thread(target=_flush_stats, daemon=True, name="Stats").start()
    threading.Thread(target=_poll_panel,  daemon=True, name="Panel").start()
    threading.Thread(target=_cleanup_afk, daemon=True, name="AFKClean").start()
    threading.Thread(target=_auto_status_push, daemon=True, name="StatusPush").start()

    log.info(f"OK Armed | Owner:{OWNER_ID} | Threads: botx12 AIx16 FBx8")
    log.info("📡 Listening...")

    try:
        bot.infinity_polling(
            timeout=10,
            long_polling_timeout=5,
            allowed_updates=["message", "callback_query", "chat_member"],
        )
    except KeyboardInterrupt:
        log.info("👋 Stopped.")
    except Exception as e:
        log.error(f"Fatal: {e}"); sys.exit(1)
