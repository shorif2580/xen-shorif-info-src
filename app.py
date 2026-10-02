import os
import time
import httpx
import json
import base64
import itertools
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

def request_token(cred: dict, region: str):
    """একটি গেস্ট আইডির টোকেন (ক্যাশ বা নতুন); ব্যর্থ হলে None"""
    uid_g, pwd = cred["uid"], cred["password"]
    now = time.time()
    c = cached_tokens.get(uid_g)
    if c and now < c.get("expires_at", 0) - 60:
        return c
    try:
        headers = {"User-Agent": USERAGENT, "Accept": "application/json"}
        with httpx.Client(timeout=8.0, verify=False) as client:
            resp = client.get(TOKEN_API_URL, params={"uid": uid_g, "password": pwd, "key": TOKEN_API_KEY}, headers=headers)
        if resp.status_code == 200:
            msg = resp.json()
            raw = msg.get("token", "")
            if raw:
                cached_tokens[uid_g] = {
                    "token": raw if raw.startswith("Bearer ") else f"Bearer {raw}",
                    "region": msg.get("lockRegion") or msg.get("region") or region,
                    "server_url": msg.get("serverUrl", "https://clientbp.ppmainecoonghj.com"),
                    "expires_at": msg.get("expiry_time", now + 25200),
                }
                return cached_tokens[uid_g]
    except Exception as e:
        app.logger.error(f"Token generation failed for {uid_g[:3]}***: {e}")
    return None

def fetch_player_data(uid: str, region: str = "BD"):
    pool = ordered_accounts(region)
    if not pool:
        raise Exception(f"No guest accounts configured for {region} (account_*.json ফাইল দেখুন)")
    last_err = "no response"
    for cred in pool[:4]:
        info = request_token(cred, region)
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
            with httpx.Client(timeout=10.0, verify=False) as client:
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
    for region in order:
        try:
            data = fetch_player_data(uid, region)
            if data and (data.get("basicInfo") or data.get("basic_info")):
                uid_region_cache[uid] = region
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
                   "token_cached": bool(cached_tokens.get(u) and now < cached_tokens[u].get("expires_at", 0))}
            if live:
                row["token_ok"] = request_token(c, group) is not None
                if not row["token_ok"]: mark_bad(u)
            rows.append(row)
        out[group] = rows
    return jsonify({"token_api": TOKEN_API_URL.split("/token")[0], "accounts": out})

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
        return jsonify({"nickname": imagegen.describe(b.get("nickname")), "clan": imagegen.describe(c.get("clanName"))})
    try:
        return png_response(imagegen.banner_image(data))
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
        data = fetch_player_data(uid, "BD")
        basic = data.get("basicInfo") or data.get("basicinfo") or {}
        nickname = basic.get("nickname") or basic.get("PlayerNickname") or "Player"
        level = basic.get("level") or 0
        is_deleted = basic.get("is_deleted", False)

        is_banned = bool(is_deleted)
        ban_status = "Banned" if is_banned else "Clean"

        return jsonify({
            "Nickname": nickname,
            "UID": uid,
            "Region": "BD",
            "level": level,
            "is_banned": is_banned,
            "ban_status": ban_status,
            "period": "Permanent" if is_deleted else "None"
        })
    except Exception as e:
        return jsonify({"error": f"Ban status unavailable: {e}"}), 502

# ==============================================================================
# 📊 STATS (সোর্স: flash-player-info-v1 — আমাদের প্রোটো ফাইলে stats মেসেজ নেই)
# ⚠️ সোর্স না পেলে নকল/হার্ডকোড ডেটা দেওয়া হয় না — 502 এরর ফেরত যায়
# ==============================================================================
STATS_UPSTREAM = os.environ.get("STATS_API_URL", "https://flash-player-info-v1.vercel.app/stats").rstrip("/")

def fetch_stats_raw(uid, match_mode, kind):
    last = None
    for timeout in (6.0, 4.0):
        try:
            with httpx.Client(timeout=timeout, verify=False) as client:
                r = client.get(f"{STATS_UPSTREAM}/{match_mode}/{kind}", params={"uid": uid}, headers={"User-Agent": USERAGENT, "Accept": "application/json"})
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and d: return d
        except Exception as e:
            last = e
    return None

def build_br(uid, match_mode):
    raw = fetch_stats_raw(uid, match_mode, "br")
    if raw is None: return None
    sq, du, so = (calculate_br_mode(raw.get(k) or {}) for k in ("quadstats", "duostats", "solostats"))
    return {"uid": uid, "nickname": raw.get("nickname"), "mode": match_mode, "squad": sq, "duo": du, "solo": so,
            "has_data": any(x["games_played"] > 0 for x in (sq, du, so))}

def build_cs(uid, match_mode):
    raw = fetch_stats_raw(uid, match_mode, "cs")
    if raw is None: return None
    cs = raw.get("csstats") or {}
    det = cs.get("detailedstats") or {}
    played, wins, kills = cs.get("gamesplayed", 0) or 0, cs.get("wins", 0) or 0, cs.get("kills", 0) or 0
    deaths, assists = det.get("deaths", 0) or 0, det.get("assists", 0) or 0
    hs = det.get("headShotKills", det.get("headshots", 0)) or 0
    kda = round((kills + assists) / deaths, 2) if deaths > 0 else float(kills + assists)
    kd = round(kills / deaths, 2) if deaths > 0 else float(kills)
    return {
        "uid": uid, "nickname": raw.get("nickname"), "mode": match_mode, "has_data": played > 0,
        "matches": played, "wins": wins, "win_rate": f"{round(wins / played * 100, 2) if played else 0.0}%",
        "kills": kills, "deaths": deaths, "assists": assists, "kd_ratio": kd, "official_kda": kda,
        "headshot_kills": hs, "headshot_rate": f"{round(hs / kills * 100, 2) if kills else 0.0}%",
        "damage": det.get("damage", 0) or 0, "mvp": det.get("mvpCount", 0) or 0,
        "double_kills": det.get("doubleKills", 0) or 0, "triple_kills": det.get("tripleKills", 0) or 0,
        "quadra_kills": det.get("fourKills", 0) or 0
    }

def stats_route(builder):
    uid = request.args.get('uid')
    mode = request.args.get('mode', 'RANKED').upper()
    if not uid or not uid.isdigit(): return jsonify({"error": "Numeric UID is required"}), 400
    match_mode = "RANKED" if mode == "RANKED" else "CAREER"
    res = builder(uid, match_mode)
    if res is None:
        return jsonify({"error": "Stats source unavailable. Please try again.", "uid": uid, "mode": match_mode}), 502
    return jsonify(res)

# 5. 🏆 BR STATS
@app.route('/stats/br', methods=['GET'])
def get_br_stats(): return stats_route(build_br)

# 6. ⚔️ CS STATS
@app.route('/stats/cs', methods=['GET'])
def get_cs_stats(): return stats_route(build_cs)

# 7. 📊 ALL-IN-ONE (BR+CS, RANKED+CAREER একসাথে, সমান্তরালে)
@app.route('/stats/all', methods=['GET'])
def get_all_stats():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit(): return jsonify({"error": "Numeric UID is required"}), 400
    from concurrent.futures import ThreadPoolExecutor
    jobs = {"br_ranked": (build_br, "RANKED"), "br_career": (build_br, "CAREER"), "cs_ranked": (build_cs, "RANKED"), "cs_career": (build_cs, "CAREER")}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {k: ex.submit(fn, uid, m) for k, (fn, m) in jobs.items()}
        out = {k: f.result() for k, f in futs.items()}
    out["uid"] = uid
    out["errors"] = [k for k, v in out.items() if k != "uid" and v is None]
    return jsonify(out)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)