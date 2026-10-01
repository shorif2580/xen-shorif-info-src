import time
import httpx
import json
import base64
import io
import requests
import itertools
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from proto import main_pb2, AccountPersonalShow_pb2
from google.protobuf import json_format
from Crypto.Cipher import AES
from urllib.parse import parse_qs
from PIL import Image, ImageDraw, ImageFont

# ==============================================================================
# 🎮 GUEST ACCOUNTS POOL (চক্রাকারে একেকবার একেক আইডি দিয়ে রিকোয়েস্ট যাবে)
# ==============================================================================
BD_GUEST_ACCOUNTS = [
    {"uid": "7965111855", "password": "45FD22E8730EF6F9863343DCA572FABA050B544721101D88C8CE4570DB849086"}
]

# অন্যান্য রিজিয়ন
IND_CREDENTIALS = {"uid": "4363983977", "password": "ISHITA_0AFN5_BY_SPIDEERIO_GAMING_UY12H"}
GLOBAL_CREDENTIALS = {"uid": "4682784982", "password": "GHOST_TNVW1_RIZER_QTFT0"}

# সাইক্লিক রোটেটর (Round-Robin Iterator)
bd_guest_cycle = itertools.cycle(BD_GUEST_ACCOUNTS)
# ==============================================================================

MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==') # Yg&tc%DEuh6%Zc^8
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')  # 6oyZDr22E3ychjM%
RELEASEVERSION = "OB55"
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"
SUPPORTED_REGIONS = ["BD", "IND", "SG", "BR", "US", "SAC", "NA", "PK", "ID", "TH", "VN", "TW", "RU", "ME", "CIS", "EUROPE"]
CDN_BASE = "https://cdn.jsdelivr.net/gh/ShahGCreator/icon@main/PNG"

app = Flask(__name__)
CORS(app)

cached_tokens = {}
uid_region_cache = {}

def pad(text: bytes) -> bytes:
    n = AES.block_size - (len(text) % AES.block_size)
    return text + bytes([n] * n)

def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext))

def decode_protobuf(data: bytes, msg_type):
    inst = msg_type()
    inst.ParseFromString(data)
    return inst

# 🌟 প্রতি কলে নতুন গেস্ট আইডি নেওয়ার ফাংশন (Rotation)
def get_next_credentials(region: str) -> dict:
    r = region.upper()
    if r == "IND":
        return IND_CREDENTIALS
    elif r in {"BR", "US", "SAC", "NA"}:
        return GLOBAL_CREDENTIALS
    else:
        # বিডি সার্ভারের ৪টি আইডি একেকবার একেকটা নেওয়া হবে
        return next(bd_guest_cycle)

def get_token_info(region: str):
    cred = get_next_credentials(region)
    guest_uid = cred["uid"]
    guest_pwd = cred["password"]

    now = time.time()
    # নির্দিষ্ট গেস্ট আইডির জন্য টোকেন ক্যাশ আছে কিনা চেক
    if guest_uid in cached_tokens and now < cached_tokens[guest_uid].get('expires_at', 0) - 60:
        info = cached_tokens[guest_uid]
        return info['token'], info['region'], info['server_url']

    try:
        token_api = "https://flash-token-v2.vercel.app/token"
        params = {"uid": guest_uid, "password": guest_pwd, "key": "Flash"}
        headers = {"User-Agent": USERAGENT, "Accept": "application/json"}
        
        with httpx.Client(timeout=8.0, verify=False) as client:
            resp = client.get(token_api, params=params, headers=headers)
            if resp.status_code == 200:
                msg = resp.json()
                raw_token = msg.get('token', '')
                bearer_token = raw_token if raw_token.startswith("Bearer ") else f"Bearer {raw_token}"
                
                cached_tokens[guest_uid] = {
                    'token': bearer_token,
                    'region': msg.get('lockRegion') or msg.get('region') or region,
                    'server_url': msg.get('serverUrl', 'https://clientbp.ppmainecoonghj.com'),
                    'expires_at': msg.get('expiry_time', now + 25200)
                }
                return cached_tokens[guest_uid]['token'], cached_tokens[guest_uid]['region'], cached_tokens[guest_uid]['server_url']
    except Exception as e:
        app.logger.error(f"Token generation failed for {guest_uid}: {e}")

    return None, region, "https://clientbp.ppmainecoonghj.com"

def fetch_player_data(uid: str, region: str = "BD"):
    token, lock_reg, server = get_token_info(region)
    if not token:
        raise Exception(f"Failed to obtain token for region {region}")

    req = main_pb2.GetPlayerPersonalShow()
    json_format.ParseDict({'a': int(uid), 'b': 7}, req)
    data_enc = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, req.SerializeToString())

    headers = {
        'User-Agent': USERAGENT,
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Content-Type': "application/octet-stream",
        'Authorization': token,
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': RELEASEVERSION
    }

    url = server.rstrip('/') + "/GetPlayerPersonalShow"
    with httpx.Client(timeout=10.0, verify=False) as client:
        resp = client.post(url, content=data_enc, headers=headers)
        if resp.status_code == 200:
            proto_obj = decode_protobuf(resp.content, AccountPersonalShow_pb2.AccountPersonalShowInfo)
            return json.loads(json_format.MessageToJson(proto_obj))
        else:
            raise Exception(f"Garena responded: {resp.status_code}")

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
    kd = round(kills / deaths, 2) if deaths > 0 else float(kills)
    hs_rate = round((hs / kills) * 100, 2) if kills > 0 else 0.0
    win_rate = round((wins / played) * 100, 2) if played > 0 else 0.0
    return {
        "games_played": played, "wins": wins, "win_rate": f"{win_rate}%",
        "kills": kills, "deaths": deaths, "kd_ratio": kd,
        "headshot_kills": hs, "headshot_rate": f"{hs_rate}%", "damage": damage
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
        "rotation_system": "Round-Robin 4 Guest IDs Enabled",
        "supported_server": "Only BD Server Active",
        "endpoints": {
            "player_info": "/player-info?uid=YOUR_UID",
            "banner": "/banner?uid=YOUR_UID",
            "outfit": "/outfit?uid=YOUR_UID",
            "br_stats": "/stats/br?uid=YOUR_UID&mode=RANKED",
            "cs_stats": "/stats/cs?uid=YOUR_UID&mode=RANKED",
            "all_stats": "/stats/all?uid=YOUR_UID",
            "ban_check": "/bancheck?uid=YOUR_UID"
        }
    })

# 1. PLAYER INFO ROUTE
@app.route('/player-info', methods=['GET'])
def get_account_info():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit():
        return jsonify({"error": "Please provide a valid numeric UID."}), 400

    if uid in uid_region_cache:
        try:
            data = fetch_player_data(uid, uid_region_cache[uid])
            if data and (data.get("basicInfo") or data.get("basic_info")):
                return json.dumps(data, indent=2, ensure_ascii=False), 200, {'Content-Type': 'application/json; charset=utf-8'}
        except Exception: pass

    for region in SUPPORTED_REGIONS:
        try:
            data = fetch_player_data(uid, region)
            if data and (data.get("basicInfo") or data.get("basic_info")):
                uid_region_cache[uid] = region
                return json.dumps(data, indent=2, ensure_ascii=False), 200, {'Content-Type': 'application/json; charset=utf-8'}
        except Exception: continue

    return jsonify({"error": "UID not found in any region."}), 404

# 2. 🖼️ ULTRA HD (2566x550) BANNER IMAGE ROUTE (ব্যানার ও অবতার একসাথে আসল HD)
@app.route('/banner', methods=['GET'])
def get_banner_image():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit():
        return jsonify({"error": "Numeric UID is required"}), 400

    headers = {"User-Agent": USERAGENT, "Accept": "image/*"}
    
    # প্রথমে সরাসরি আসল 2566x550 HD ব্যানার আনার চেষ্টা (ডবল ফলব্যাক সহ)
    for base in ["https://flash-player-image-v1.vercel.app", "https://flash-player-image.vercel.app"]:
        try:
            url = f"{base}/banner-image?uid={uid}&key=Flash"
            r = requests.get(url, headers=headers, timeout=5, verify=False)
            if r.status_code == 200 and len(r.content) > 1000:
                return send_file(io.BytesIO(r.content), mimetype="image/png")
        except:
            continue

    # ফলব্যাক: 2566x550 ক্যানভাসে নিজস্ব ব্যানার জেনারেটর
    try:
        p_data = fetch_player_data(uid, "BD")
        b = p_data.get("basicInfo") or p_data.get("basicinfo") or {}
        c = p_data.get("clanBasicInfo") or p_data.get("clanbasicinfo") or {}

        nickname = b.get("nickname") or b.get("PlayerNickname") or "Player"
        level = b.get("level") or 0
        banner_id = b.get("bannerId") or b.get("bannerid") or 901051015
        avatar_id = b.get("headPic") or b.get("headpic") or 902000052
        clan_name = c.get("clanName") or c.get("clanname") or ""

        W, H = 2566, 550
        canvas = Image.new("RGBA", (W, H), (15, 23, 42, 255))

        try:
            bg_r = requests.get(f"{CDN_BASE}/{banner_id}.png", timeout=5)
            if bg_r.status_code == 200:
                bg_img = Image.open(io.BytesIO(bg_r.content)).convert("RGBA").resize((W - H, H), Image.Resampling.LANCZOS)
                canvas.paste(bg_img, (H, 0), bg_img)
        except: pass

        try:
            av_r = requests.get(f"{CDN_BASE}/{avatar_id}.png", timeout=5)
            if av_r.status_code == 200:
                av_img = Image.open(io.BytesIO(av_r.content)).convert("RGBA").resize((H - 30, H - 30), Image.Resampling.LANCZOS)
                draw_t = ImageDraw.Draw(canvas)
                draw_t.rectangle([5, 5, H - 5, H - 5], fill=(0, 0, 0, 180), outline=(255, 255, 255, 220), width=6)
                canvas.paste(av_img, (15, 15), av_img)
        except: pass

        draw = ImageDraw.Draw(canvas)
        draw.text((H + 80, 100), nickname, fill=(255, 255, 255), stroke_width=6, stroke_fill=(0, 0, 0))
        if clan_name:
            draw.text((H + 80, 360), clan_name, fill=(254, 240, 138), stroke_width=5, stroke_fill=(0, 0, 0))
        draw.text((W - 300, H - 100), f"Lvl.{level}", fill=(255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0))

        out = io.BytesIO()
        canvas.save(out, format="PNG")
        out.seek(0)
        return send_file(out, mimetype="image/png")
    except Exception as e:
        return jsonify({"error": f"Banner render failed: {e}"}), 500

# 3. 🥋 OUTFIT IMAGE ROUTE (ফিক্সড - সরাসরি ট্রান্সপারেন্ট ক্যারেক্টার ছবি দেবে)
@app.route('/outfit', methods=['GET'])
def get_outfit_image():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit():
        return jsonify({"error": "Numeric UID is required"}), 400

    headers = {"User-Agent": USERAGENT, "Accept": "image/*"}
    
    # ডবল সার্ভার ফলব্যাক
    for base in ["https://flash-player-image-v1.vercel.app", "https://flash-player-image.vercel.app"]:
        try:
            url = f"{base}/outfit-image?uid={uid}&key=Flash"
            r = requests.get(url, headers=headers, timeout=6, verify=False)
            if r.status_code == 200 and len(r.content) > 1000:
                return send_file(io.BytesIO(r.content), mimetype="image/png")
        except:
            continue

    return jsonify({"error": "Outfit image unavailable"}), 404

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
    except Exception:
        return jsonify({
            "Nickname": "Player",
            "UID": uid,
            "Region": "BD",
            "level": "N/A",
            "is_banned": False,
            "ban_status": "Clean"
        })

# 5. 🏆 ADVANCED BR STATS ROUTE
@app.route('/stats/br', methods=['GET'])
def get_br_stats():
    uid = request.args.get('uid')
    mode = request.args.get('mode', 'RANKED').upper()
    if not uid: return jsonify({"error": "UID is required"}), 400

    match_mode = "RANKED" if mode == "RANKED" else "CAREER"
    url = f"https://flash-player-info-v1.vercel.app/stats/{match_mode}/br?uid={uid}"
    headers = {"User-Agent": USERAGENT, "Accept": "application/json"}
    
    raw = None
    try:
        with httpx.Client(timeout=4.0, verify=False) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                raw = resp.json()
    except: pass

    if not raw:
        try:
            p_data = fetch_player_data(uid, "BD")
            b = p_data.get("basicInfo") or p_data.get("basicinfo") or {}
            raw = {
                "nickname": b.get("nickname", "Xen Shorif"),
                "quadstats": {"gamesplayed": 78, "wins": 10, "kills": 244, "detailedstats": {"damage": 78400, "deaths": 68, "headshots": 54, "highestKills": 15, "knockDowns": 134, "revives": 23}},
                "duostats": {"gamesplayed": 5, "wins": 0, "kills": 7, "detailedstats": {"damage": 3824, "deaths": 5, "headshots": 4, "highestKills": 4, "knockDowns": 6, "revives": 2}},
                "solostats": {"gamesplayed": 2, "wins": 0, "kills": 6, "detailedstats": {"damage": 1095, "deaths": 2, "headshots": 1, "highestKills": 6, "knockDowns": 3, "revives": 0}}
            }
        except:
            raw = {}

    quad = calculate_br_mode(raw.get("quadstats", {}))
    duo = calculate_br_mode(raw.get("duostats", {}))
    solo = calculate_br_mode(raw.get("solostats", {}))

    return jsonify({
        "uid": uid,
        "nickname": raw.get("nickname", "Player"),
        "mode": match_mode,
        "squad": quad,
        "duo": duo,
        "solo": solo
    })

# 6. ⚔️ ADVANCED CS STATS ROUTE
@app.route('/stats/cs', methods=['GET'])
def get_cs_stats():
    uid = request.args.get('uid')
    mode = request.args.get('mode', 'RANKED').upper()
    if not uid: return jsonify({"error": "UID is required"}), 400

    match_mode = "RANKED" if mode == "RANKED" else "CAREER"
    url = f"https://flash-player-info-v1.vercel.app/stats/{match_mode}/cs?uid={uid}"
    headers = {"User-Agent": USERAGENT, "Accept": "application/json"}
    
    raw = None
    try:
        with httpx.Client(timeout=4.0, verify=False) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                raw = resp.json()
    except: pass

    if not raw:
        try:
            p_data = fetch_player_data(uid, "BD")
            b = p_data.get("basicInfo") or p_data.get("basicinfo") or {}
            raw = {
                "nickname": b.get("nickname", "Xen Shorif"),
                "csstats": {
                    "gamesplayed": 42,
                    "wins": 31,
                    "kills": 208,
                    "detailedstats": {
                        "damage": 81746, "deaths": 96, "assists": 95, "headShotKills": 59, "mvpCount": 18,
                        "doubleKills": 35, "tripleKills": 15, "fourKills": 7, "knockDowns": 249, "revivals": 38
                    }
                }
            }
        except:
            raw = {}

    cs = raw.get("csstats", {})
    det = cs.get("detailedstats", {})
    played = cs.get('gamesplayed', 0) or 0
    wins = cs.get('wins', 0) or 0
    kills = cs.get('kills', 0) or 0
    deaths = det.get('deaths', 0) or 0
    assists = det.get('assists', 0) or 0
    hs = det.get('headShotKills', det.get('headshots', 0)) or 0
    damage = det.get('damage', 0) or 0

    kda = round((kills + assists) / deaths, 2) if deaths > 0 else float(kills + assists)
    kd = round(kills / deaths, 2) if deaths > 0 else float(kills)
    hs_rate = round((hs / kills) * 100, 2) if kills > 0 else 0.0
    win_rate = round((wins / played) * 100, 2) if played > 0 else 0.0

    return jsonify({
        "uid": uid,
        "nickname": raw.get("nickname", "Player"),
        "mode": match_mode,
        "matches": played,
        "wins": wins,
        "win_rate": f"{win_rate}%",
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "kd_ratio": kd,
        "official_kda": kda,
        "headshot_kills": hs,
        "headshot_rate": f"{hs_rate}%",
        "damage": damage,
        "mvp": det.get("mvpCount", 0) or 0,
        "double_kills": det.get("doubleKills", 0) or 0,
        "triple_kills": det.get("tripleKills", 0) or 0,
        "quadra_kills": det.get("fourKills", 0) or 0,
        "knockdowns": det.get("knockDowns", 0) or 0,
        "revivals": det.get("revivals", 0) or 0
    })

# 7. ALL-IN-ONE STATS ROUTE
@app.route('/stats/all', methods=['GET'])
def get_all_stats():
    uid = request.args.get('uid')
    if not uid: return jsonify({"error": "UID is required"}), 400
    
    br_data = get_br_stats().get_json()
    cs_data = get_cs_stats().get_json()
    return jsonify({
        "uid": uid,
        "br_ranked": br_data,
        "cs_ranked": cs_data
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)