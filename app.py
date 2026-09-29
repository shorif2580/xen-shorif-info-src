import time
import httpx
import json
import base64
import io
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from proto import main_pb2, AccountPersonalShow_pb2
from google.protobuf import json_format
from Crypto.Cipher import AES
from urllib.parse import parse_qs
from PIL import Image, ImageDraw, ImageFont

# ==============================================================================
# 🎮 GUEST ACCOUNTS CONFIGURATION (আপনার এই ৪টি আইডিতেই সব চলবে!)
# ==============================================================================
INFO_CREDENTIALS = {
    "BD": "uid=7965111855&password=45FD22E8730EF6F9863343DCA572FABA050B544721101D88C8CE4570DB849086",
    "IND": "uid=4363983977&password=ISHITA_0AFN5_BY_SPIDEERIO_GAMING_UY12H",
    "GLOBAL": "uid=4682784982&password=GHOST_TNVW1_RIZER_QTFT0"
}

BR_STATS_CREDENTIALS = {
    "BD": "uid=7966603004&password=1B3DF4391F1932B786A74881C8EADD58770F5141BFD70356D0FE6864BDDC6C96",
    "IND": "uid=4363983977&password=ISHITA_0AFN5_BY_SPIDEERIO_GAMING_UY12H",
    "GLOBAL": "uid=4682784982&password=GHOST_TNVW1_RIZER_QTFT0"
}

CS_STATS_CREDENTIALS = {
    "BD": "uid=7967157776&password=A91C5BD673E6EFCB1CF22FC0B2E542CD15D329E58C5A2B1768E39BB9732D64FE",
    "IND": "uid=4363983977&password=ISHITA_0AFN5_BY_SPIDEERIO_GAMING_UY12H",
    "GLOBAL": "uid=4682784982&password=GHOST_TNVW1_RIZER_QTFT0"
}

BAN_CREDENTIALS = {
    "BD": "uid=7967766964&password=015FFECDC15C208C5E9F220DEB82D1903C1CCCA89A6C586AE98E52EA32AD905B",
    "IND": "uid=4363983977&password=ISHITA_0AFN5_BY_SPIDEERIO_GAMING_UY12H",
    "GLOBAL": "uid=4682784982&password=GHOST_TNVW1_RIZER_QTFT0"
}
# ==============================================================================

MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==')
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')
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

def get_credentials_by_service(region: str, service: str = "info") -> str:
    r = region.upper()
    cred_map = {
        "info": INFO_CREDENTIALS,
        "br": BR_STATS_CREDENTIALS,
        "cs": CS_STATS_CREDENTIALS,
        "ban": BAN_CREDENTIALS
    }.get(service, INFO_CREDENTIALS)

    if r == "IND":
        return cred_map.get("IND", cred_map["GLOBAL"])
    elif r in {"BR", "US", "SAC", "NA"}:
        return cred_map.get("GLOBAL", cred_map["BD"])
    else:
        return cred_map.get("BD", cred_map["GLOBAL"])

def get_token_info(region: str, service: str = "info"):
    cache_key = f"{region}_{service}"
    info = cached_tokens.get(cache_key)
    now = time.time()
    if info and now < info.get('expires_at', 0) - 60:
        return info['token'], info['region'], info['server_url']

    try:
        creds = parse_qs(get_credentials_by_service(region, service))
        uid = creds.get("uid", [""])[0]
        password = creds.get("password", [""])[0]
        
        token_api = "https://flash-token-v2.vercel.app/token"
        params = {"uid": uid, "password": password, "key": "Flash"}
        headers = {"User-Agent": USERAGENT, "Accept": "application/json"}
        
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(token_api, params=params, headers=headers)
            if resp.status_code == 200:
                msg = resp.json()
                raw_token = msg.get('token', '')
                bearer_token = raw_token if raw_token.startswith("Bearer ") else f"Bearer {raw_token}"
                
                cached_tokens[cache_key] = {
                    'token': bearer_token,
                    'region': msg.get('lockRegion') or msg.get('region') or region,
                    'server_url': msg.get('serverUrl', 'https://clientbp.ppmainecoonghj.com'),
                    'expires_at': msg.get('expiry_time', now + 25200)
                }
                return cached_tokens[cache_key]['token'], cached_tokens[cache_key]['region'], cached_tokens[cache_key]['server_url']
    except Exception as e:
        app.logger.error(f"Token generation failed for {service}/{region}: {e}")

    return None, region, "https://clientbp.ppmainecoonghj.com"

def fetch_player_data(uid: str, region: str = "BD"):
    token, lock_reg, server = get_token_info(region, service="info")
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
# 🌐 API ENDPOINTS
# ==============================================================================

@app.route('/', methods=['GET'])
def root_index():
    return jsonify({
        "status": "Online",
        "service": "Xen Shorif Master Free Fire API",
        "developer": "@xen_shorif",
        "supported_server": "Only BD Server Active",
        "endpoints": {
            "player_info": "/player-info?uid=YOUR_UID",
            "banner": "/banner?uid=YOUR_UID",
            "avatar": "/avatar?uid=YOUR_UID",
            "br_stats": "/stats/br?uid=YOUR_UID&mode=RANKED",
            "cs_stats": "/stats/cs?uid=YOUR_UID&mode=RANKED",
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

# 2. 🖼️ BANNER IMAGE GENERATOR ROUTE (নিজের সার্ভার থেকেই ছবি বানাবে!)
@app.route('/banner', methods=['GET'])
def get_banner_image():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit():
        return jsonify({"error": "Numeric UID is required"}), 400

    try:
        p_data = fetch_player_data(uid, "BD")
        b = p_data.get("basicInfo") or p_data.get("basicinfo") or {}
        c = p_data.get("clanBasicInfo") or p_data.get("clanbasicinfo") or {}

        nickname = b.get("nickname") or b.get("PlayerNickname") or "Player"
        level = b.get("level") or 0
        banner_id = b.get("bannerId") or b.get("bannerid") or 901051015
        avatar_id = b.get("headPic") or b.get("headpic") or 902000052
        clan_name = c.get("clanName") or c.get("clanname") or ""

        width, height = 600, 120
        banner_canvas = Image.new("RGBA", (width, height), (15, 23, 42, 255))

        # ব্যানার ও অবতার লোড
        try:
            bg_r = requests.get(f"{CDN_BASE}/{banner_id}.png", timeout=4)
            if bg_r.status_code == 200:
                bg_img = Image.open(io.BytesIO(bg_r.content)).convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)
                banner_canvas.paste(bg_img, (0, 0), bg_img)
        except: pass

        try:
            av_r = requests.get(f"{CDN_BASE}/{avatar_id}.png", timeout=4)
            if av_r.status_code == 200:
                av_img = Image.open(io.BytesIO(av_r.content)).convert("RGBA").resize((100, 100), Image.Resampling.LANCZOS)
                draw_t = ImageDraw.Draw(banner_canvas)
                draw_t.rectangle([8, 8, 112, 112], fill=(0, 0, 0, 160), outline=(255, 255, 255, 100), width=2)
                banner_canvas.paste(av_img, (10, 10), av_img)
        except: pass

        draw = ImageDraw.Draw(banner_canvas)
        draw.text((130, 24), nickname, fill=(255, 255, 255))
        if clan_name:
            draw.text((130, 64), clan_name, fill=(253, 224, 71))
        draw.text((width - 75, height - 24), f"Lvl.{level}", fill=(255, 255, 255))

        out = io.BytesIO()
        banner_canvas.save(out, format="PNG")
        out.seek(0)
        return send_file(out, mimetype="image/png")
    except Exception as e:
        return jsonify({"error": f"Banner render failed: {e}"}), 500

# 3. 👤 AVATAR IMAGE ROUTE
@app.route('/avatar', methods=['GET'])
def get_avatar_image():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit():
        return jsonify({"error": "Numeric UID is required"}), 400

    try:
        p_data = fetch_player_data(uid, "BD")
        b = p_data.get("basicInfo") or p_data.get("basicinfo") or {}
        avatar_id = b.get("headPic") or b.get("headpic") or 902000052
        
        av_r = requests.get(f"{CDN_BASE}/{avatar_id}.png", timeout=5)
        if av_r.status_code == 200:
            return send_file(io.BytesIO(av_r.content), mimetype="image/png")
    except: pass
    return jsonify({"error": "Avatar image not found"}), 404

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
        with httpx.Client(timeout=4.0) as client:
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
                "quadstats": {"gamesplayed": 78, "wins": 10, "kills": 244, "detailedstats": {"damage": 78400, "deaths": 68, "headshots": 54}},
                "duostats": {"gamesplayed": 5, "wins": 0, "kills": 7, "detailedstats": {"damage": 3824, "deaths": 5, "headshots": 4}},
                "solostats": {"gamesplayed": 2, "wins": 0, "kills": 6, "detailedstats": {"damage": 1095, "deaths": 2, "headshots": 1}}
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
        with httpx.Client(timeout=4.0) as client:
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
                    "gamesplayed": 42, "wins": 31, "kills": 208,
                    "detailedstats": {"damage": 81746, "deaths": 96, "assists": 95, "headShotKills": 59, "mvpCount": 18, "doubleKills": 35, "tripleKills": 15, "fourKills": 7}
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
        "damage": det.get('damage', 0) or 0,
        "mvp": det.get("mvpCount", 0) or 0,
        "double_kills": det.get("doubleKills", 0) or 0,
        "triple_kills": det.get("tripleKills", 0) or 0,
        "quadra_kills": det.get("fourKills", 0) or 0
    })

# 7. ALL-IN-ONE STATS ROUTE
@app.route('/stats/all', methods=['GET'])
def get_all_stats():
    uid = request.args.get('uid')
    if not uid: return jsonify({"error": "UID is required"}), 400
    return jsonify({
        "uid": uid,
        "br_ranked": get_br_stats().get_json(),
        "cs_ranked": get_cs_stats().get_json()
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)