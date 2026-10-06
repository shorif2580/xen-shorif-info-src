import os
import re
import time
import httpx
import json
import base64
import itertools
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
from proto import main_pb2, AccountPersonalShow_pb2
from google.protobuf import json_format
from Crypto.Cipher import AES
from flask import Response
import imagegen

MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==') # Yg&tc%DEuh6%Zc^8
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')  # 6oyZDr22E3ychjM%
RELEASEVERSION = "OB55"
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"
SUPPORTED_REGIONS = ["BD", "IND", "SG", "BR", "US", "SAC", "NA", "PK", "ID", "TH", "VN", "TW", "RU", "ME", "CIS", "EUROPE"]

app = Flask(__name__)
CORS(app)

# ==============================================================================
# 📂 ACCOUNTS LOADER (accounts.json ফাইল থেকে স্বয়ংক্রিয়ভাবে লোড হবে)
# ==============================================================================
def load_accounts_config():
    """সার্ভার অনুযায়ী আলাদা JSON ফাইল: account_bd.json / account_ind.json / account_global.json
       ফরম্যাট: [{"uid": "...", "password": "..."}, ...]
       (ঐচ্ছিক) ACCOUNTS_JSON environment variable দিয়ে পুরো কনফিগ ওভাররাইড করা যায়।"""
    env_json = os.environ.get("ACCOUNTS_JSON", "").strip()
    if env_json:
        try: return json.loads(env_json)
        except Exception: pass
    base = os.path.dirname(os.path.abspath(__file__))
    cfg = {}
    for key in ("BD", "IND", "GLOBAL"):
        path = os.path.join(base, f"account_{key.lower()}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg[key] = [{"uid": str(c["uid"]), "password": str(c["password"])} for c in data if isinstance(c, dict) and c.get("uid") and c.get("password")]
        except Exception:
            cfg[key] = []
    return cfg

ACCOUNTS_CONFIG = load_accounts_config()
cached_tokens = {}
uid_region_cache = {}
bad_accounts = {}          # guest_uid -> cooldown শেষ হওয়ার সময়
COOLDOWN_SECONDS = 120
TOKEN_API_URL = os.environ.get("TOKEN_API_URL", "https://flash-token-v2.vercel.app/token")
TOKEN_API_KEY = os.environ.get("TOKEN_API_KEY", "Flash")
_rr = itertools.count()

# Crypto helpers
def pad(text: bytes) -> bytes:
    n = AES.block_size - (len(text) % AES.block_size)
    return text + bytes([n] * n)

def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext))

def decode_protobuf(data: bytes, msg_type):
    inst = msg_type()
    inst.ParseFromString(data)
    return inst

# 🌟 প্রতি রিকোয়েস্টে আলাদা গেস্ট আইডি নির্বাচন (Rotation)
# 🌟 রিজিয়ন অনুযায়ী গেস্ট লিস্ট
def accounts_for(region: str) -> list:
    r = region.upper()
    key = "IND" if r == "IND" else ("GLOBAL" if r in {"BR", "US", "SAC", "NA"} else "BD")
    return [c for c in ACCOUNTS_CONFIG.get(key, []) if c.get("uid") and c.get("password")]

def mark_bad(guest_uid: str):
    bad_accounts[guest_uid] = time.time() + COOLDOWN_SECONDS
    cached_tokens.pop(guest_uid, None)

def ordered_accounts(region: str) -> list:
    """রাউন্ড-রবিন + খারাপ আইডি সাময়িক বাদ (সব খারাপ হলে সবগুলোই আবার চেষ্টা)"""
    lst = accounts_for(region)
    if not lst: return []
    s = next(_rr) % len(lst)
    lst = lst[s:] + lst[:s]
    now = time.time()
    return [c for c in lst if bad_accounts.get(c["uid"], 0) <= now] or lst

# ==============================================================================
# 🎟 DUAL TOKEN SOURCES — দুটো সার্ভিস একসাথে চালু; যেটা আগে বৈধ JWT দেয় সেটাই ব্যবহার হয়
#   env: TOKEN_SOURCES=flash,abhi  (শুধু একটা চাইলে TOKEN_SOURCES=flash)
# ==============================================================================
ABHI_TOKEN_URL = os.environ.get("ABHI_TOKEN_URL", "https://abhi-jwt-2iax.vercel.app/token")
ABHI_API_KEY = os.environ.get("ABHI_API_KEY", "ABHI")
DEFAULT_SERVER_URL = "https://clientbp.ppmainecoonghj.com"
JWT_RE = re.compile(r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$")
SOURCE_STATS = {}   # name -> {"ok": n, "fail": n, "last_ms": ms, "last_error": str}

def _sources():
    names = [x.strip().lower() for x in os.environ.get("TOKEN_SOURCES", "flash,abhi").split(",") if x.strip()]
    table = {
        "flash": (TOKEN_API_URL, lambda u, p: {"uid": u, "password": p, "key": TOKEN_API_KEY}),
        "abhi": (ABHI_TOKEN_URL, lambda u, p: {"uid": u, "password": p, "api_key": ABHI_API_KEY}),
    }
    return [(n, table[n][0], table[n][1]) for n in names if n in table]

def _strip_bearer(v):
    return v[7:] if isinstance(v, str) and v.startswith("Bearer ") else v

def _find_jwt(d):
    """JSON-এর যেকোনো জায়গা থেকে JWT খোঁজে (ফরম্যাট না জেনেও)। token/jwt নামের কী আগে, তারপর JWT-আকৃতির যেকোনো স্ট্রিং"""
    if isinstance(d, dict):
        for k in ("token", "jwt", "access_jwt", "jwt_token", "accessToken"):
            v = _strip_bearer(d.get(k))
            if isinstance(v, str) and JWT_RE.match(v): return v
        for v in d.values():
            r = _find_jwt(v)
            if r: return r
    elif isinstance(d, list):
        for v in d:
            r = _find_jwt(v)
            if r: return r
    elif isinstance(d, str):
        v = _strip_bearer(d)
        if JWT_RE.match(v): return v
    return None

def _find_str(d, keys):
    if isinstance(d, dict):
        low = {str(k).lower(): v for k, v in d.items()}
        for k in keys:
            v = low.get(k.lower())
            if isinstance(v, (str, int)) and str(v): return str(v)
        for v in d.values():
            r = _find_str(v, keys)
            if r: return r
    elif isinstance(d, list):
        for v in d:
            r = _find_str(v, keys)
            if r: return r
    return None

def _jwt_payload(jwt):
    try:
        p = jwt.split(".")[1]; p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p.encode()).decode("utf-8"))
    except Exception:
        return {}

def parse_token_response(d, region, now):
    """যেকোনো সোর্সের JSON → স্ট্যান্ডার্ড টোকেন-ইনফো; JWT না পেলে None"""
    jwt = _find_jwt(d)
    if not jwt: return None
    payload = _jwt_payload(jwt)
    server = _find_str(d, ["serverUrl", "server_url", "base_url"]) or _find_str(payload, ["serverUrl", "server_url"])
    if not (server and server.startswith("http")): server = DEFAULT_SERVER_URL
    exp = _find_str(d, ["expiry_time", "expires_at"]) or payload.get("exp")
    try: exp = float(exp)
    except Exception: exp = now + 25200
    if exp < now + 120: exp = now + 25200
    reg = _find_str(d, ["lockRegion", "lock_region", "region"]) or payload.get("lock_region") or region
    extra = {k: v for k, v in ((k, _find_str(d, [k])) for k in ("access_token", "open_id", "account_id", "account_name", "platform")) if v}
    return {"token": "Bearer " + jwt, "region": str(reg), "server_url": server, "expires_at": exp, "extra": extra}

def _call_source(name, url, params_fn, uid_g, pwd, region, timeout=4.5):
    t0 = time.time(); err = None; info = None; status = None; keys = []
    try:
        with httpx.Client(timeout=timeout, verify=False) as client:
            resp = client.get(url, params=params_fn(uid_g, pwd), headers={"User-Agent": USERAGENT, "Accept": "application/json"})
        status = resp.status_code
        d = resp.json()
        keys = sorted(map(str, d.keys())) if isinstance(d, dict) else []
        info = parse_token_response(d, region, time.time()) if status == 200 else None
        if not info:
            msg = _find_str(d, ["error", "message", "msg", "detail"]) if isinstance(d, dict) else None
            err = f"HTTP {status}" + (f" | {str(msg)[:60]}" if msg else " | JWT নেই")
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:50]}"
    ms = int((time.time() - t0) * 1000)
    st = SOURCE_STATS.setdefault(name, {"ok": 0, "fail": 0})
    st["ok" if info else "fail"] += 1; st["last_ms"] = ms; st["last_error"] = None if info else err
    return {"source": name, "info": info, "ms": ms, "status": status, "keys": keys, "error": err}

# ⚠️ একই গেস্ট আইডি দিয়ে দুই সার্ভিসে একসাথে লগইন করলে একটা সেশন আরেকটাকে বাতিল করতে পারে।
#    তাই: (১) একটা আইডির জন্য সোর্সগুলো এক-এক করে (একসাথে নয়) চেষ্টা হয়, (২) যেটা আগে সফল হয়েছিল সেটাই আগে,
#    (৩) প্রথমটা ব্যর্থ হলে তবেই দ্বিতীয়টা, (৪) একই আইডিতে একই সময়ে দুই রিকোয়েস্ট লগইন করে না (লক)।
PREF_SOURCE = {}          # guest_uid -> শেষবার যে সোর্স সফল ছিল
_uid_locks = {}
_locks_guard = threading.Lock()

def _lock_for(uid):
    with _locks_guard:
        return _uid_locks.setdefault(uid, threading.Lock())

def _source_order(uid):
    srcs = _sources()
    if len(srcs) <= 1: return srcs
    pref = PREF_SOURCE.get(uid)
    if pref:
        return sorted(srcs, key=lambda s: 0 if s[0] == pref else 1)
    k = int(uid) % len(srcs) if str(uid).isdigit() else 0      # আইডিগুলো দুই সোর্সে ভাগ হয়ে শুরু করে
    return srcs[k:] + srcs[:k]

def request_token(cred: dict, region: str, detail: bool = False, deadline: float = None):
    """টোকেন (ক্যাশ বা নতুন); ব্যর্থ হলে None।
       detail=True (শুধু টেস্টের জন্য): দুই সোর্সই এক-এক করে চালায়, ক্যাশ করে না — {"winner":..., "results":[...]}"""
    uid_g, pwd = cred["uid"], cred["password"]
    with _lock_for(uid_g):
        now = time.time()
        c = cached_tokens.get(uid_g)
        if c and now < c.get("expires_at", 0) - 60 and not detail:
            return c
        results, winner = [], None
        for n, u, pf in _source_order(uid_g):
            if deadline and time.time() > deadline: break
            r = _call_source(n, u, pf, uid_g, pwd, region)
            results.append(r)
            if r["info"] and winner is None:
                winner = r
                if not detail: break              # সফল হলে দ্বিতীয় সোর্সে লগইন নয়
        if detail:
            cached_tokens.pop(uid_g, None)         # টেস্টে দুবার লগইন হয়েছে — পুরনো টোকেন বাতিল ধরে ক্যাশ মুছি
            PREF_SOURCE.pop(uid_g, None)
            return {"winner": winner["source"] if winner else None, "results": results}
        if winner:
            cached_tokens[uid_g] = dict(winner["info"], source=winner["source"])
            PREF_SOURCE[uid_g] = winner["source"]
            return cached_tokens[uid_g]
        PREF_SOURCE.pop(uid_g, None)
        return None

def fetch_player_data(uid: str, region: str = "BD", deadline: float = None):
    pool = ordered_accounts(region)
    if not pool:
        raise Exception(f"No guest accounts configured for {region} (account_*.json ফাইল দেখুন)")
    last_err = "no response"
    for cred in pool[:3]:
        if deadline and time.time() > deadline:
            last_err = "time budget over"; break
        info = request_token(cred, region, deadline=deadline)
        if not info:
            mark_bad(cred["uid"]); last_err = "token generation failed"; continue
        req = main_pb2.GetPlayerPersonalShow()
        json_format.ParseDict({'a': int(uid), 'b': 7}, req)
        data_enc = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, req.SerializeToString())
        headers = {
            'User-Agent': USERAGENT, 'Connection': "Keep-Alive", 'Accept-Encoding': "gzip",
            'Content-Type': "application/octet-stream", 'Authorization': info["token"],
            'X-Unity-Version': "2018.4.11f1", 'X-GA': "v1 1", 'ReleaseVersion': RELEASEVERSION
        }
        url = info["server_url"].rstrip('/') + "/GetPlayerPersonalShow"
        try:
            with httpx.Client(timeout=7.0, verify=False) as client:
                resp = client.post(url, content=data_enc, headers=headers)
        except Exception as e:
            last_err = str(e); continue
        if resp.status_code == 200:
            try:
                data = json.loads(json_format.MessageToJson(decode_protobuf(resp.content, AccountPersonalShow_pb2.AccountPersonalShowInfo)))
            except Exception:
                last_err = "decode error"; continue
            if data.get("basicInfo") or data.get("basic_info"):
                return data
            last_err = "empty response"; continue      # আইডি ব্যান/UID নেই — পরেরটা দিয়ে দেখি (কুলডাউন নয়)
        if resp.status_code in (401, 403):
            mark_bad(cred["uid"]); last_err = f"Garena {resp.status_code}"; continue
        last_err = f"Garena {resp.status_code}"
    raise Exception(f"Failed for region {region}: {last_err}")

POOLS = ["BD", "IND", "BR"]   # BD পুল, IND পুল, GLOBAL পুল (BR লেবেল)

def get_player_data(uid: str):
    """UID-র ডেটা খুঁজে আনে (ক্যাশ করা পুল আগে)"""
    cached = uid_region_cache.get(uid)
    order = ([cached] if cached else []) + [p for p in POOLS if p != cached]
    deadline = time.time() + 8.5          # Vercel Hobby 10s সীমার ভেতরে থাকতে
    for region in order:
        if time.time() > deadline: break
        try:
            data = fetch_player_data(uid, region, deadline)
            if data and (data.get("basicInfo") or data.get("basic_info")):
                uid_region_cache[uid] = region
                try: NICK_CACHE[uid] = (data.get("basicInfo") or {}).get("nickname")
                except Exception: pass
                return data
        except Exception:
            continue
    return None

def png_response(png_bytes):
    return Response(png_bytes, mimetype="image/png", headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"})

def calculate_br_mode(mode_data):
    if not mode_data:
        return {"games_played": 0, "wins": 0, "win_rate": "0.0%", "kills": 0, "deaths": 0, "kd_ratio": 0.0, "headshot_kills": 0, "headshot_rate": "0.0%", "damage": 0}
    played = mode_data.get("gamesplayed", 0) or 0
    wins = mode_data.get("wins", 0) or 0
    kills = mode_data.get("kills", 0) or 0
    det = mode_data.get("detailedstats", {}) or {}
    deaths = det.get("deaths", 0) or 0
    hs = det.get("headshotKills", det.get("headshots", 0)) or 0
    damage = det.get("damage", 0) or 0
    highest_kills = det.get("highestKills", 0) or 0
    kd = round(kills / deaths, 2) if deaths > 0 else float(kills)
    hs_rate = round((hs / kills) * 100, 2) if kills > 0 else 0.0
    win_rate = round((wins / played) * 100, 2) if played > 0 else 0.0
    return {
        "games_played": played, "wins": wins, "win_rate": f"{win_rate}%",
        "kills": kills, "deaths": deaths, "kd_ratio": kd,
        "headshot_kills": hs, "headshot_rate": f"{hs_rate}%", "damage": damage,
        "highest_kills": highest_kills
    }

# ==============================================================================
# 🌐 API ENDPOINTS (সব রুট এক সার্ভারে)
# ==============================================================================

@app.route('/', methods=['GET'])
def root_index():
    return jsonify({
        "status": "Online",
        "service": "Xen Shorif Master Free Fire API",
        "developer": "@xen_shorif",
        "accounts_config": "account_bd.json / account_ind.json / account_global.json",
        "endpoints": {
            "player_info": "/player-info?uid=YOUR_UID",
            "banner": "/banner?uid=YOUR_UID  (&debug=1 → নামের অক্ষর-কোড)",
            "outfit": "/outfit?uid=YOUR_UID",
            "br_stats": "/stats/br?uid=YOUR_UID&mode=RANKED",
            "cs_stats": "/stats/cs?uid=YOUR_UID&mode=RANKED",
            "all_stats": "/stats/all?uid=YOUR_UID",
            "access_info": "/access?token=ACCESS_TOKEN",
            "ban_check": "/bancheck?uid=YOUR_UID"
        }
    })

# 0. 🩺 GUEST ACCOUNT HEALTH  (/accounts-status  বা  ?test=1 দিয়ে লাইভ টেস্ট)
@app.route('/accounts-status', methods=['GET'])
def accounts_status():
    admin = os.environ.get("ADMIN_KEY", "")
    if admin and request.args.get("key") != admin:
        return jsonify({"error": "unauthorized"}), 401
    live = request.args.get("test") == "1"
    mask = lambda u: (u[:3] + "****" + u[-3:]) if len(u) > 6 else "***"
    now = time.time()
    out = {}
    for group, lst in ACCOUNTS_CONFIG.items():
        rows = []
        for c in lst:
            u = str(c.get("uid", ""))
            row = {"uid": mask(u), "cooldown_s": max(0, int(bad_accounts.get(u, 0) - now)),
                   "token_cached": bool(cached_tokens.get(u) and now < cached_tokens[u].get("expires_at", 0)),
                   "source": (cached_tokens.get(u) or {}).get("source")}
            if live:
                row["token_ok"] = request_token(c, group) is not None
                if not row["token_ok"]: mark_bad(u)
            rows.append(row)
        out[group] = rows
    return jsonify({"token_sources": [s[0] for s in _sources()], "source_stats": SOURCE_STATS, "accounts": out})

# 🆚 TOKEN COMPARE — একটা গেস্ট আইডি দিয়ে দুই সার্ভিসের ফলাফল পাশাপাশি (টোকেনের মান লুকানো)
@app.route('/token-compare', methods=['GET', 'POST'])
def token_compare():
    if not admin_ok(): return jsonify({"error": "unauthorized"}), 401
    uid_g = (request.values.get("uid") or "").strip(); pwd = (request.values.get("password") or "").strip()
    if not uid_g.isdigit() or not pwd: return jsonify({"error": "uid (numeric) আর password দিন"}), 400
    res = request_token({"uid": uid_g, "password": pwd}, request.values.get("region", "BD"), detail=True)
    rows = []
    for r in res["results"]:
        info = r["info"]
        rows.append({"source": r["source"], "ok": bool(info), "ms": r["ms"], "http": r["status"], "error": r["error"], "response_keys": r["keys"],
                     "server_url": info["server_url"] if info else None, "region": info["region"] if info else None,
                     "expires_in_min": int((info["expires_at"] - time.time()) / 60) if info else None, "jwt_length": len(info["token"]) - 7 if info else None})
    return jsonify({"uid": uid_g[:3] + "****" + uid_g[-3:], "winner": res["winner"], "note": "টেস্টে দুই সোর্সে এক-এক করে লগইন হয়েছে; এই আইডির ক্যাশ-টোকেন মুছে দেওয়া হয়েছে", "sources": rows})

# 1. PLAYER INFO ROUTE
@app.route('/player-info', methods=['GET'])
def get_account_info():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit():
        return jsonify({"error": "Please provide a valid numeric UID."}), 400
    data = get_player_data(uid)
    if not data:
        return jsonify({"error": "UID not found or guest accounts unavailable."}), 404
    return json.dumps(data, indent=2, ensure_ascii=False), 200, {'Content-Type': 'application/json; charset=utf-8'}

# 2. 🖼️ BANNER ROUTE (নিজস্ব জেনারেটর — Flash API লাগে না)
@app.route('/banner', methods=['GET'])
def get_banner_image():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit():
        return jsonify({"error": "Numeric UID is required"}), 400
    data = get_player_data(uid)
    if not data:
        return jsonify({"error": "UID not found in any region."}), 404
    if request.args.get("debug"):
        b, c = data.get("basicInfo") or {}, data.get("clanBasicInfo") or {}
        return jsonify({"fonts": imagegen.fonts_status(), "nickname": imagegen.describe(b.get("nickname")), "clan": imagegen.describe(c.get("clanName"))})
    try:
        fmt = "webp" if request.args.get("format", "").lower() == "webp" else "png"
        png = imagegen.banner_image(data, fmt)
        if fmt == "webp":
            return Response(png, mimetype="image/webp", headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"})
        return png_response(png)
    except Exception as e:
        app.logger.error(f"banner error: {e}")
        return jsonify({"error": f"Banner generation failed: {e}"}), 500

# 3. 🥋 OUTFIT ROUTE (নিজস্ব জেনারেটর)
@app.route('/outfit', methods=['GET'])
def get_outfit_image():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit():
        return jsonify({"error": "Numeric UID is required"}), 400
    data = get_player_data(uid)
    if not data:
        return jsonify({"error": "UID not found in any region."}), 404
    try:
        return png_response(imagegen.outfit_image(data))
    except Exception as e:
        app.logger.error(f"outfit error: {e}")
        return jsonify({"error": f"Outfit generation failed: {e}"}), 500

# 4. 🛡️ BAN CHECK ROUTE
@app.route('/bancheck', methods=['GET'])
def get_ban_status():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit():
        return jsonify({"error": "Please provide a valid numeric UID."}), 400

    try:
        data = get_player_data(uid)
        if not data:
            return jsonify({"error": "Player not found or guest accounts unavailable."}), 404
        basic = data.get("basicInfo") or data.get("basicinfo") or {}
        nickname = basic.get("nickname") or basic.get("PlayerNickname") or "Player"
        level = basic.get("level") or 0
        is_deleted = basic.get("is_deleted", False)

        is_banned = bool(is_deleted)
        ban_status = "Banned" if is_banned else "Clean"

        return jsonify({
            "Nickname": nickname,
            "UID": uid,
            "Region": basic.get("region") or "BD",
            "level": level,
            "is_banned": is_banned,
            "ban_status": ban_status,
            "period": "Permanent" if is_deleted else "None"
        })
    except Exception as e:
        return jsonify({"error": f"Ban status unavailable: {e}"}), 502

# ==============================================================================
# 📊 STATS — সরাসরি Garena সার্ভার থেকে (GetPlayerStats), Flash লাগে না
#   রিকোয়েস্ট : ফিল্ড১=accountid, ফিল্ড২=matchmode (0=CAREER, 1=NORMAL, 2=RANKED)
#   রেসপন্স   : ফিল্ড১=solo, ২=duo, ৩=squad। প্রতিটার ভেতরে: ১=uid ২=games ৩=wins ৪=kills ৫=detailed
#   detailed  : ১=deaths ৩=top_n ৪=distance(m) ৫=survival(s) ৬=revives ৭=highest_kills ৮=damage
#               ৯=roadkills ১০=headshot_hits ১১=headshot_kills ১২=knockdowns ১৩=pickups
#   ✅ UID 7703449332-এর ইন-গেম সংখ্যার সাথে মিলিয়ে যাচাই-করা (BR)। ⚠️ CS এই এন্ডপয়েন্টে পাওয়া যায়নি।
# ==============================================================================
MATCHMODE = {"CAREER": 0, "NORMAL": 1, "RANKED": 2}
NICK_CACHE = {}          # uid -> nickname (get_player_data ভরে দেয়)
REPORT_TO = "@xen_shorif"

def _as_msg(node):
    """নেস্টেড মেসেজ ঠিকমতো dict হিসেবে; ছাপার-যোগ্য বাইটের কারণে স্ট্রিং হয়ে গেলে আবার পার্স"""
    if isinstance(node, dict): return node
    if isinstance(node, str):
        try: return parse_wire(node.encode("utf-8"))
        except Exception: return {}
    return {}

def _f(d, k):
    v = d.get(k) if isinstance(d, dict) else None
    return v[0] if isinstance(v, list) and v else None

def _int(x): return x if isinstance(x, int) else 0

def _fmt_dur(sec):
    sec = int(sec); return f"{sec // 60}m {sec % 60:02d}s"

def parse_br_mode(sub):
    sub = _as_msg(sub)
    games, wins, kills = _int(_f(sub, "2")), _int(_f(sub, "3")), _int(_f(sub, "4"))
    det = _as_msg(_f(sub, "5"))
    g = lambda k: _int(_f(det, k))
    deaths, hs_kills, damage = g("1"), g("11"), g("8")
    avg = lambda x: round(x / games, 2) if games else 0
    return {
        "games_played": games, "wins": wins, "win_rate": f"{round(wins / games * 100, 2) if games else 0.0}%",
        "kills": kills, "deaths": deaths, "kd_ratio": round(kills / deaths, 2) if deaths > 0 else float(kills),
        "headshot_kills": hs_kills, "headshot_rate": f"{round(hs_kills / kills * 100, 2) if kills else 0.0}%",
        "damage": damage, "highest_kills": g("7"),
        "top_n": g("3"), "top_n_rate": f"{round(g('3') / games * 100, 2) if games else 0.0}%",
        "knockdowns": g("12"), "roadkills": g("9"), "revives": g("6"), "pickups": g("13"), "headshot_hits": g("10"),
        "avg_damage": round(damage / games) if games else 0,
        "avg_survival": _fmt_dur(g("5") / games) if games else "0m 00s",
        "avg_distance_km": round(g("4") / games / 1000, 2) if games else 0.0,
        "total_distance_m": g("4"), "total_survival_s": g("5"),
    }

def garena_stats(uid, mm, region=None):
    """→ (tree, None) বা (None, {"stage","http","detail"})"""
    region = region or uid_region_cache.get(uid) or "BD"
    deadline = time.time() + 8.5
    err = {"stage": "token", "detail": "কোনো গেস্ট আইডির টোকেন পাওয়া যায়নি"}
    pool = ordered_accounts(region)
    if not pool:
        return None, {"stage": "config", "detail": f"{region} পুলে কোনো গেস্ট আইডি নেই"}
    for cred in pool[:3]:
        if time.time() > deadline:
            err = {"stage": "timeout", "detail": "সময়সীমা (৮.৫ সেকেন্ড) শেষ"}; break
        info = request_token(cred, region, deadline=deadline)
        if not info:
            mark_bad(cred["uid"]); continue
        raw = _varint(1 << 3) + _varint(int(uid)) + _varint(2 << 3) + _varint(mm)
        headers = {'User-Agent': USERAGENT, 'Content-Type': "application/octet-stream", 'Authorization': info["token"], 'X-Unity-Version': "2018.4.11f1", 'X-GA': "v1 1", 'ReleaseVersion': RELEASEVERSION}
        try:
            with httpx.Client(timeout=5.0, verify=False) as client:
                r = client.post(info["server_url"].rstrip('/') + "/GetPlayerStats", content=aes_cbc_encrypt(MAIN_KEY, MAIN_IV, raw), headers=headers)
        except Exception as e:
            err = {"stage": "garena", "detail": f"{type(e).__name__}: {str(e)[:60]}"}; continue
        if r.status_code == 200 and r.content:
            try:
                tree = parse_wire(r.content)
            except Exception as e:
                err = {"stage": "decode", "detail": str(e)[:60]}; continue
            if any(k in tree for k in ("1", "2", "3")):
                return tree, None
            err = {"stage": "decode", "detail": "সাড়ায় solo/duo/squad নেই"}; continue
        if r.status_code in (401, 403):
            mark_bad(cred["uid"]); err = {"stage": "garena", "http": r.status_code, "detail": "টোকেন বাতিল/নিষিদ্ধ"}; continue
        err = {"stage": "garena", "http": r.status_code, "detail": "খালি সাড়া" if r.status_code == 200 else f"Garena {r.status_code}"}
    return None, err

def build_br(uid, mode_name):
    mm = MATCHMODE[mode_name]
    tree, err = garena_stats(uid, mm)
    if tree is None: return None, err
    solo, duo, squad = (parse_br_mode(tree.get(k, [None])[0] if tree.get(k) else None) for k in ("1", "2", "3"))
    return {"uid": uid, "nickname": NICK_CACHE.get(uid), "mode": mode_name, "solo": solo, "duo": duo, "squad": squad,
            "has_data": any(x["games_played"] > 0 for x in (solo, duo, squad))}, None

def stats_error(err, uid, mode_name):
    return jsonify({"error": "Stats পাওয়া যায়নি", "stage": err.get("stage"), "http": err.get("http"), "detail": err.get("detail"),
                    "uid": uid, "mode": mode_name, "report_to": REPORT_TO}), 502

def _mode_arg():
    m = request.args.get("mode", "RANKED").upper()
    return m if m in MATCHMODE else "RANKED"

# 5. 🏆 BR STATS  (mode=RANKED | CAREER | NORMAL)
@app.route('/stats/br', methods=['GET'])
def get_br_stats():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit(): return jsonify({"error": "Numeric UID is required"}), 400
    res, err = build_br(uid, _mode_arg())
    return jsonify(res) if res else stats_error(err, uid, _mode_arg())

# 6. ⚔️ CS STATS — এই সার্ভারে পাওয়া যায়নি
@app.route('/stats/cs', methods=['GET'])
def get_cs_stats():
    return jsonify({"error": "CS stats সমর্থিত নয়", "supported": False,
                    "detail": "GetPlayerStats সব ক্ষেত্রেই BR ডেটা দেয়; CS বাছাই করার উপায় এখনও পাওয়া যায়নি",
                    "uid": request.args.get("uid"), "report_to": REPORT_TO}), 501

# 7. 📊 ALL-IN-ONE (BR Ranked + Career একসাথে; CS নেই)
@app.route('/stats/all', methods=['GET'])
def get_all_stats():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit(): return jsonify({"error": "Numeric UID is required"}), 400
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1, f2 = ex.submit(build_br, uid, "RANKED"), ex.submit(build_br, uid, "CAREER")
        (r1, e1), (r2, e2) = f1.result(), f2.result()
    out = {"uid": uid, "nickname": NICK_CACHE.get(uid), "br_ranked": r1, "br_career": r2,
           "cs_ranked": None, "cs_career": None, "cs_supported": False}
    out["errors"] = [k for k, v in (("br_ranked", r1), ("br_career", r2)) if v is None]
    out["reasons"] = {k: e for k, e in (("br_ranked", e1), ("br_career", e2)) if e}
    out["report_to"] = REPORT_TO
    return jsonify(out)


# ==============================================================================
# 🔐 TOKEN / DECODE / STATS-PROBE   (ADMIN_KEY সেট থাকলে ?key=... লাগবে)
# ==============================================================================
def admin_ok():
    k = os.environ.get("ADMIN_KEY", "")
    return (not k) or request.args.get("key") == k

def _fmt_ts(v):
    try:
        dt = datetime.fromtimestamp(int(v), timezone.utc)
        bd = dt + timedelta(hours=6)
        return {"utc": dt.strftime("%Y-%m-%d %H:%M:%S"), "bd": bd.strftime("%d %B %Y, %I:%M:%S %p"), "seconds_left": int(int(v) - time.time())}
    except Exception:
        return None

def _b64url_json(part):
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part.encode()).decode("utf-8"))

# 8. 🔓 JWT DECODE (সিগনেচার যাচাই করে না — শুধু header/payload পড়ে)
@app.route('/decode', methods=['GET', 'POST'])
def decode_token():
    tok = (request.values.get("token") or "").strip()
    if tok.lower().startswith("bearer "): tok = tok[7:].strip()
    parts = tok.split(".")
    if len(parts) != 3:
        return jsonify({"error": "এটা JWT নয় (header.payload.signature ফরম্যাট লাগবে)"}), 400
    try:
        header, payload = _b64url_json(parts[0]), _b64url_json(parts[1])
    except Exception as e:
        return jsonify({"error": f"Decode failed: {e}"}), 400
    times = {k: _fmt_ts(v) for k, v in payload.items() if isinstance(v, (int, float)) and 1_000_000_000 < v < 4_000_000_000 and k.lower() in ("exp", "iat", "nbf", "expiry", "expire", "created")}
    return jsonify({"header": header, "payload": payload, "times": {k: v for k, v in times.items() if v}, "signature_verified": False})

# 9. 🎟 TOKEN (আপনার টোকেন সার্ভিস থেকে সরাসরি আসা JSON — ফিল্ডে কোনো পরিবর্তন করা হয় না)
@app.route('/token', methods=['GET', 'POST'])
def token_passthrough():
    if not admin_ok(): return jsonify({"error": "unauthorized"}), 401
    uid_g, pwd = request.values.get("uid", "").strip(), request.values.get("password", "").strip()
    if not uid_g.isdigit() or not pwd:
        return jsonify({"error": "uid (numeric) আর password দিন", "report_to": REPORT_TO}), 400
    res = request_token({"uid": uid_g, "password": pwd}, request.values.get("region", "BD"), detail=True)
    win = next((r for r in res["results"] if r["info"]), None)
    if not win:
        return jsonify({"error": "Token generate হয়নি (আইডি/পাসওয়ার্ড ভুল, ব্যান, বা সার্ভিস বন্ধ)",
                        "tried": [{"source": r["source"], "error": r["error"]} for r in res["results"]], "report_to": REPORT_TO}), 502
    info = win["info"]; jwt = info["token"][7:]
    out = {"source": win["source"], "token": jwt, "serverUrl": info["server_url"], "region": info["region"], "expires_at": info["expires_at"]}
    out.update(info.get("extra") or {})
    out["payload"] = _jwt_payload(jwt)
    return jsonify(out)

# 10. 🧪 STATS PROBE — শুধু ডায়াগনস্টিক। Garena-র স্ট্যাটস রেসপন্সের কাঁচা (স্কিমাহীন) ডিকোড দেখায়।
#     ⚠️ এন্ডপয়েন্ট নাম/রিকোয়েস্ট ফরম্যাট আমার যাচাই করা নয় — ফলাফল দেখে মিলিয়ে নেওয়ার জন্য।
def parse_wire(buf, depth=0):
    out, i = {}, 0
    def varint(i):
        r = sh = 0
        while True:
            b = buf[i]; i += 1; r |= (b & 0x7F) << sh; sh += 7
            if not b & 0x80: return r, i
    while i < len(buf):
        key, i = varint(i); fn, wt = key >> 3, key & 7
        if fn == 0: raise ValueError("bad field")
        if wt == 0: v, i = varint(i)
        elif wt == 1: v = int.from_bytes(buf[i:i + 8], "little"); i += 8
        elif wt == 5: v = int.from_bytes(buf[i:i + 4], "little"); i += 4
        elif wt == 2:
            ln, i = varint(i); chunk = buf[i:i + ln]; i += ln
            if len(chunk) != ln: raise ValueError("truncated")
            v = None
            printable = False
            try:
                txt = chunk.decode("utf-8"); printable = txt.isprintable()
            except Exception: txt = None
            if printable:
                v = txt                      # সম্পূর্ণ ছাপার-যোগ্য → স্ট্রিং (যেমন "7703449332")
            elif depth < 5:
                try: v = parse_wire(chunk, depth + 1)   # নিয়ন্ত্রণ-বাইট আছে → নেস্টেড মেসেজ
                except Exception: v = None
            if v is None or v == {}: v = chunk.hex()
        else: raise ValueError("bad wiretype")
        out.setdefault(str(fn), []).append(v)
    return out

@app.route('/stats-probe', methods=['GET'])
def stats_probe():
    if not os.environ.get("ADMIN_KEY") or request.args.get("key") != os.environ.get("ADMIN_KEY"):
        return jsonify({"error": "unauthorized (ADMIN_KEY সেট করে ?key=... দিন)"}), 401
    uid = request.args.get("uid", "")
    if not uid.isdigit(): return jsonify({"error": "uid লাগবে"}), 400
    path = request.args.get("path", "GetPlayerStats").strip("/")
    modes = [int(x) for x in request.args.get("modes", "0,1,2,3").split(",") if x.strip().isdigit()]
    info = any_token("BD")
    if not info: return jsonify({"error": "token পাওয়া যায়নি"}), 502
    results = []
    for m in modes:
        req = main_pb2.GetPlayerPersonalShow()
        json_format.ParseDict({'a': int(uid), 'b': m}, req)
        enc = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, req.SerializeToString())
        headers = {'User-Agent': USERAGENT, 'Content-Type': "application/octet-stream", 'Authorization': info["token"], 'X-Unity-Version': "2018.4.11f1", 'X-GA': "v1 1", 'ReleaseVersion': RELEASEVERSION}
        try:
            with httpx.Client(timeout=10.0, verify=False) as client:
                r = client.post(info["server_url"].rstrip('/') + "/" + path, content=enc, headers=headers)
            row = {"b": m, "status": r.status_code, "bytes": len(r.content)}
            if r.status_code == 200 and r.content:
                try: row["decoded"] = parse_wire(r.content)
                except Exception as e: row["decode_error"] = str(e); row["hex"] = r.content[:200].hex()
        except Exception as e:
            row = {"b": m, "error": str(e)}
        results.append(row)
    return jsonify({"path": path, "uid": uid, "results": results})


# 11. 🧭 STATS CALIBRATE — স্ট্যাটস রিকোয়েস্টের সঠিক ফরম্যাট খুঁজে বের করার ডায়াগনস্টিক (এক-বারের কাজ)
#     কয়েকটা সম্ভাব্য রিকোয়েস্ট-লেআউট সমান্তরালে পাঠায়, রেসপন্সে আপনার ইন-গেম সংখ্যা (UID 7703449332)
#     আছে কি না মিলিয়ে দেখে। ⚠️ এন্ডপয়েন্ট নাম (GetPlayerStats) ও লেআউট যাচাই-করা নয় — ফলাফল দেখে নিশ্চিত হতে হবে।
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        if n: out.append(b | 0x80)
        else: out.append(b); return bytes(out)

def _ints_in(node, acc):
    if isinstance(node, dict):
        for v in node.values(): _ints_in(v, acc)
    elif isinstance(node, list):
        for v in node: _ints_in(v, acc)
    elif isinstance(node, int):
        acc.add(node)

KNOWN_INGAME = {   # UID 7703449332 — আপনার ইন-গেম রেকর্ড থেকে
    "cs": {2255, 1266, 8359, 3916, 9798, 663, 1456},
    "br": {301, 17, 611, 147, 228, 21, 372, 107, 422, 2920, 351, 6446, 1443, 7453, 36},
}

@app.route('/stats-calibrate', methods=['GET'])
def stats_calibrate():
    """আগের রানে পাওয়া গেছে: GetPlayerStats-এ field1=accountid, field2=matchmode, field3=gamemode কাজ করে;
       matchmode 0/1/2 সাড়া দেয়, CS gamemode=15। এখন কাঁচা ডিকোড-ট্রি দেখাচ্ছি (BR-এর gamemode মান খোঁজাও এখানে)।"""
    if not admin_ok(): return jsonify({"error": "unauthorized"}), 401
    uid = request.args.get("uid", "7703449332")
    if not uid.isdigit(): return jsonify({"error": "uid লাগবে"}), 400
    kind = request.args.get("kind", "both").lower()
    path = request.args.get("path", "GetPlayerStats").strip("/")
    ints = lambda k, d: [int(x) for x in request.args.get(k, d).split(",") if x.strip().isdigit()]
    mms = ints("mm", "0,1,2")
    jobs = []   # (kind, gamemode, matchmode)
    if kind in ("cs", "both"): jobs += [("cs", g, m) for g in ints("gmcs", "15") for m in mms]
    if kind in ("br", "both"): jobs += [("br", g, m) for g in ints("gmbr", "0,1,2,3,4,5") for m in mms]
    if len(jobs) > 40: return jsonify({"error": f"{len(jobs)}টা কম্বিনেশন — সর্বোচ্চ ৪০"}), 400
    info = any_token("BD")
    if not info: return jsonify({"error": "token পাওয়া যায়নি"}), 502
    url = info["server_url"].rstrip('/') + "/" + path

    def one(job):
        k, g, m = job
        raw = _varint(1 << 3) + _varint(int(uid)) + _varint(2 << 3) + _varint(m) + _varint(3 << 3) + _varint(g)   # f1=acc, f2=matchmode, f3=gamemode
        enc = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, raw)
        headers = {'User-Agent': USERAGENT, 'Content-Type': "application/octet-stream", 'Authorization': info["token"], 'X-Unity-Version': "2018.4.11f1", 'X-GA': "v1 1", 'ReleaseVersion': RELEASEVERSION}
        row = {"kind": k, "gamemode": g, "matchmode": m}
        try:
            with httpx.Client(timeout=8.0, verify=False) as client:
                r = client.post(url, content=enc, headers=headers)
            row["status"], row["bytes"] = r.status_code, len(r.content)
            if r.status_code == 200 and r.content:
                try:
                    row["tree"] = parse_wire(r.content)
                    seen = set(); _ints_in(row["tree"], seen); row["known_found"] = sorted(seen & KNOWN_INGAME.get(k, set()))
                except Exception as e:
                    row["decode_error"] = str(e); row["hex"] = r.content[:160].hex()
        except Exception as e:
            row["error"] = str(e)[:80]
        return row

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as ex:
        rows = list(ex.map(one, jobs))
    empty = [f"{r['kind']} gm={r['gamemode']} mm={r['matchmode']}" for r in rows if not r.get("bytes")]
    got = sorted([r for r in rows if r.get("bytes")], key=lambda r: (-len(r.get("known_found", [])), -r["bytes"]))
    return jsonify({"uid": uid, "endpoint": path, "request_layout": "field1=accountid, field2=matchmode, field3=gamemode",
                    "tried": len(jobs), "empty_responses": empty, "responses": got})


# 12. 🔬 STATS-RAW — ডিপ্লয় না বদলে URL থেকেই রিকোয়েস্ট বদলে পরীক্ষা করার রুট (ফিল্ড১=uid, ফিল্ড২=matchmode সবসময় থাকে)
#   scan=3-16:15      → ফিল্ড ৩ থেকে ১৬ একে একে =১৫
#   vscan=3:0-40      → ফিল্ড ৩-এ মান ০ থেকে ৪০ একে একে
#   mmscan=0-20&f=3:15→ matchmode ০ থেকে ২০ একে একে (f=-র অতিরিক্ত ফিল্ড সহ)
#   f=4:15,5:2        → একটা রিকোয়েস্টে একাধিক অতিরিক্ত ফিল্ড
#   আউটপুটে একই সাড়া (sha1) বারবার দেখানো হয় না — "same_as" লেখা থাকে; নতুন ধরনের সাড়ার পুরো ডিকোড আসে।
def any_token(region="BD", tries=4):
    """কাজ করা একটা গেস্ট টোকেন খোঁজে (একটা ব্যর্থ হলে পরেরটা)"""
    for cred in ordered_accounts(region)[:tries]:
        info = request_token(cred, region)
        if info: return info
    return None

@app.route('/stats-raw', methods=['GET'])
def stats_raw():
    if not admin_ok(): return jsonify({"error": "unauthorized"}), 401
    import hashlib
    uid = request.args.get("uid", "7703449332")
    if not uid.isdigit(): return jsonify({"error": "uid লাগবে"}), 400
    path = request.args.get("path", "GetPlayerStats").strip("/")
    mm0 = int(request.args["mm"]) if request.args.get("mm", "").isdigit() else 0
    kind = request.args.get("kind", "cs").lower()

    def parse_f(s):
        out = []
        for part in [p for p in s.split(",") if p.strip()]:
            n, _, v = part.partition(":")
            if n.strip().isdigit() and v.strip().lstrip("-").isdigit(): out.append((int(n), int(v)))
        return out
    extras_f = parse_f(request.args.get("f", ""))
    variants = []     # (label, extras, matchmode)
    if extras_f: variants.append(("f=" + request.args["f"], extras_f, mm0))
    m = re.match(r"^(\d+)-(\d+):(-?\d+)$", request.args.get("scan", ""))
    if m:
        lo, hi, val = int(m.group(1)), int(m.group(2)), int(m.group(3))
        variants += [(f"field{n}={val}", extras_f + [(n, val)], mm0) for n in range(lo, hi + 1)]
    m = re.match(r"^(\d+):(\d+)-(\d+)$", request.args.get("vscan", ""))
    if m:
        fld, lo, hi = int(m.group(1)), int(m.group(2)), int(m.group(3))
        variants += [(f"field{fld}={v}", extras_f + [(fld, v)], mm0) for v in range(lo, hi + 1)]
    m = re.match(r"^(\d+)-(\d+)$", request.args.get("mmscan", ""))
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        variants += [(f"mm={v}" + (f"+{request.args['f']}" if extras_f else ""), extras_f, v) for v in range(lo, hi + 1)]
    if not variants: return jsonify({"error": "scan=3-16:15  বা  vscan=3:0-40  বা  mmscan=0-20  বা  f=4:15 দিন"}), 400
    variants = [("baseline", [], mm0)] + variants
    if len(variants) > 50: return jsonify({"error": f"{len(variants)}টা রিকোয়েস্ট — সর্বোচ্চ ৫০"}), 400
    info = any_token("BD")
    if not info: return jsonify({"error": "কোনো গেস্ট আইডির টোকেন পাওয়া যায়নি"}), 502
    url = info["server_url"].rstrip('/') + "/" + path
    known = KNOWN_INGAME.get(kind, set())

    def one(v):
        label, extras, mm = v
        raw = _varint(1 << 3) + _varint(int(uid)) + _varint(2 << 3) + _varint(mm)
        for n, val in extras:
            raw += _varint(n << 3) + _varint(val if val >= 0 else val + (1 << 64))
        enc = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, raw)
        headers = {'User-Agent': USERAGENT, 'Content-Type': "application/octet-stream", 'Authorization': info["token"], 'X-Unity-Version': "2018.4.11f1", 'X-GA': "v1 1", 'ReleaseVersion': RELEASEVERSION}
        row = {"variant": label}
        try:
            with httpx.Client(timeout=8.0, verify=False) as client:
                r = client.post(url, content=enc, headers=headers)
            row["status"], row["bytes"] = r.status_code, len(r.content)
            row["sha1"] = hashlib.sha1(r.content).hexdigest()[:10]
            if r.status_code == 200 and r.content:
                try:
                    row["_tree"] = parse_wire(r.content)
                    row["top_fields"] = sorted(row["_tree"].keys(), key=int)
                    seen = set(); _ints_in(row["_tree"], seen); row["known_found"] = sorted(seen & known)
                except Exception as e:
                    row["decode_error"] = str(e); row["hex"] = r.content[:160].hex()
        except Exception as e:
            row["error"] = str(e)[:80]
        return row

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=10) as ex:
        rows = list(ex.map(one, variants))
    base = rows[0]
    first_seen = {base.get("sha1"): "baseline"}
    out = [{"variant": "baseline", "status": base.get("status"), "bytes": base.get("bytes"), "sha1": base.get("sha1"), "top_fields": base.get("top_fields")}]
    for r in rows[1:]:
        item = {"variant": r["variant"], "status": r.get("status"), "bytes": r.get("bytes"), "sha1": r.get("sha1")}
        if r.get("error"): item["error"] = r["error"]
        sh = r.get("sha1")
        if sh in first_seen:
            item["same_as"] = first_seen[sh]
        else:
            first_seen[sh] = r["variant"]
            item["NEW_RESPONSE"] = True
            item["top_fields"] = r.get("top_fields"); item["known_found"] = r.get("known_found")
            item["tree"] = r.get("_tree"); item["decode_error"] = r.get("decode_error")
        out.append(item)
    new_count = sum(1 for x in out if x.get("NEW_RESPONSE"))
    return jsonify({"uid": uid, "endpoint": path, "base_matchmode": mm0, "tried": len(variants) - 1, "new_responses": new_count,
                    "note": "NEW_RESPONSE=true মানে বেসলাইন বা আগের কোনো সাড়ার চেয়ে আলাদা — এটাই নতুন কিছু", "results": out})

# 13. 🔑 ACCESS TOKEN INFO — Garena OAuth সার্ভারে টোকেনের তথ্য (⚠️ এন্ডপয়েন্ট আমার স্মৃতি থেকে, যাচাই-করা নয়)
ACCESS_INSPECT_URL = os.environ.get("ACCESS_INSPECT_URL", "https://100067.connect.garena.com/oauth/token/inspect")

@app.route('/access', methods=['GET', 'POST'])
def access_info():
    if not admin_ok(): return jsonify({"error": "unauthorized"}), 401
    tok = (request.values.get("token") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32,128}", tok):
        return jsonify({"error": "access token ফরম্যাট ঠিক নয় (৩২-১২৮টা হেক্স অক্ষর হতে হবে)", "report_to": REPORT_TO}), 400
    try:
        with httpx.Client(timeout=7.0, verify=False) as client:
            r = client.get(ACCESS_INSPECT_URL, params={"token": tok}, headers={"User-Agent": USERAGENT, "Accept": "application/json"})
    except Exception as e:
        return jsonify({"error": "Garena সার্ভারে পৌঁছানো যায়নি", "detail": f"{type(e).__name__}: {str(e)[:60]}", "report_to": REPORT_TO}), 502
    try: d = r.json()
    except Exception: d = None
    if r.status_code != 200 or not isinstance(d, dict) or d.get("error"):
        return jsonify({"error": "Access token যাচাই হয়নি", "http": r.status_code, "detail": (str(d.get("error")) if isinstance(d, dict) and d.get("error") else r.text[:100]), "report_to": REPORT_TO}), 502
    times = {}
    for k, v in d.items():
        if isinstance(v, (int, float)) and 1_000_000_000 < v < 4_000_000_000: times[k] = _fmt_ts(v)
    return jsonify({"info": d, "times": {k: v for k, v in times.items() if v}})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)