import time
import httpx
import json
import base64
from flask import Flask, request, jsonify
from flask_cors import CORS
from proto import main_pb2, AccountPersonalShow_pb2
from google.protobuf import json_format
from Crypto.Cipher import AES
from urllib.parse import parse_qs

# ==============================================================================
# 🎮 GUEST ACCOUNTS CONFIGURATION (সার্ভিস অনুযায়ী আলাদা আলাদা গেস্ট আইডি)
# ==============================================================================
# ১. Player Info এর গেস্ট একাউন্ট:
INFO_CREDENTIALS = {
    "BD": "uid=7965111855&password=45FD22E8730EF6F9863343DCA572FABA050B544721101D88C8CE4570DB849086",
    "IND": "uid=4363983977&password=ISHITA_0AFN5_BY_SPIDEERIO_GAMING_UY12H",
    "GLOBAL": "uid=4682784982&password=GHOST_TNVW1_RIZER_QTFT0"
}

# ২. BR Stats (Solo, Duo, Squad) এর গেস্ট একাউন্ট:
BR_STATS_CREDENTIALS = {
    "BD": "uid=7966603004&password=1B3DF4391F1932B786A74881C8EADD58770F5141BFD70356D0FE6864BDDC6C96",
    "IND": "uid=4363983977&password=ISHITA_0AFN5_BY_SPIDEERIO_GAMING_UY12H",
    "GLOBAL": "uid=4682784982&password=GHOST_TNVW1_RIZER_QTFT0"
}

# ৩. CS Stats (KDA, Quadra Kills) এর গেস্ট একাউন্ট:
CS_STATS_CREDENTIALS = {
    "BD": "uid=7967157776&password=A91C5BD673E6EFCB1CF22FC0B2E542CD15D329E58C5A2B1768E39BB9732D64FE",
    "IND": "uid=4363983977&password=ISHITA_0AFN5_BY_SPIDEERIO_GAMING_UY12H",
    "GLOBAL": "uid=4682784982&password=GHOST_TNVW1_RIZER_QTFT0"
}

# ৪. Ban Check এর গেস্ট একাউন্ট:
BAN_CREDENTIALS = {
    "BD": "uid=7967766964&password=015FFECDC15C208C5E9F220DEB82D1903C1CCCA89A6C586AE98E52EA32AD905B",
    "IND": "uid=4363983977&password=ISHITA_0AFN5_BY_SPIDEERIO_GAMING_UY12H",
    "GLOBAL": "uid=4682784982&password=GHOST_TNVW1_RIZER_QTFT0"
}
# ==============================================================================

MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==') # Yg&tc%DEuh6%Zc^8
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')  # 6oyZDr22E3ychjM%
RELEASEVERSION = "OB55"
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"
SUPPORTED_REGIONS = ["BD", "IND", "SG", "BR", "US", "SAC", "NA", "PK", "ID", "TH", "VN", "TW", "RU", "ME", "CIS", "EUROPE"]

app = Flask(__name__)
CORS(app)

cached_tokens = {}
uid_region_cache = {}

# ক্রিপ্টোগ্রাফি হেল্পার
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
        
        with httpx.Client(timeout=10.0) as client:
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

def fetch_player_data(uid: str, region: str):
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
    with httpx.Client(timeout=12.0, verify=False) as client:
        resp = client.post(url, content=data_enc, headers=headers)
        if resp.status_code == 200:
            proto_obj = decode_protobuf(resp.content, AccountPersonalShow_pb2.AccountPersonalShowInfo)
            return json.loads(json_format.MessageToJson(proto_obj))
        else:
            raise Exception(f"Garena responded: {resp.status_code}")

# ==============================================================================
# 🌐 API ENDPOINTS (সব রুট এক সার্ভারে)
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
            "br_stats": "/stats/br?uid=YOUR_UID&mode=RANKED (or CAREER)",
            "cs_stats": "/stats/cs?uid=YOUR_UID&mode=RANKED (or CAREER)",
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

# 2. ADVANCED BR STATS ROUTE (Solo, Duo, Squad)
@app.route('/stats/br', methods=['GET'])
def get_br_stats():
    uid = request.args.get('uid')
    mode = request.args.get('mode', 'RANKED').upper()
    if not uid: return jsonify({"error": "UID is required"}), 400

    match_mode = "RANKED" if mode == "RANKED" else "CAREER"
    url = f"https://flash-player-info-v1.vercel.app/stats/{match_mode}/br?uid={uid}"
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                raw = resp.json()
                # সুন্দর ফরম্যাটেড আউটপুট
                quad = raw.get("quadstats", {})
                duo = raw.get("duostats", {})
                solo = raw.get("solostats", {})
                return jsonify({
                    "uid": uid,
                    "nickname": raw.get("nickname", "N/A"),
                    "mode": match_mode,
                    "squad": quad,
                    "duo": duo,
                    "solo": solo
                })
    except Exception as e:
        return jsonify({"error": f"Failed to fetch BR stats: {e}"}), 500

    return jsonify({"error": "Stats unavailable"}), 404

# 3. ADVANCED CS STATS ROUTE (KDA, Medals, Quadra Kills)
@app.route('/stats/cs', methods=['GET'])
def get_cs_stats():
    uid = request.args.get('uid')
    mode = request.args.get('mode', 'RANKED').upper()
    if not uid: return jsonify({"error": "UID is required"}), 400

    match_mode = "RANKED" if mode == "RANKED" else "CAREER"
    url = f"https://flash-player-info-v1.vercel.app/stats/{match_mode}/cs?uid={uid}"
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                raw = resp.json()
                cs = raw.get("csstats", {})
                d = cs.get("detailedstats", {})
                kills = cs.get('kills', 0)
                deaths = d.get('deaths', 0)
                assists = d.get('assists', 0)
                kda = round((kills + assists) / deaths, 2) if deaths > 0 else (kills + assists)

                return jsonify({
                    "uid": uid,
                    "nickname": raw.get("nickname", "N/A"),
                    "mode": match_mode,
                    "matches": cs.get('gamesplayed', 0),
                    "wins": cs.get('wins', 0),
                    "kills": kills,
                    "deaths": deaths,
                    "assists": assists,
                    "official_kda": kda,
                    "headshots": d.get('headShotKills', 0),
                    "mvp": d.get('mvpCount', 0),
                    "double_kills": d.get('doubleKills', 0),
                    "triple_kills": d.get('tripleKills', 0),
                    "quadra_kills": d.get('fourKills', 0),
                    "detailed": d
                })
    except Exception as e:
        return jsonify({"error": f"Failed to fetch CS stats: {e}"}), 500

    return jsonify({"error": "Stats unavailable"}), 404

# 4. ALL-IN-ONE STATS ROUTE (এক রিকোয়েস্টে সব স্ট্যাটস)
@app.route('/stats/all', methods=['GET'])
def get_all_stats():
    uid = request.args.get('uid')
    if not uid: return jsonify({"error": "UID is required"}), 400

    out = {"uid": uid}
    with httpx.Client(timeout=6.0) as client:
        try: out["br_ranked"] = client.get(f"https://flash-player-info-v1.vercel.app/stats/RANKED/br?uid={uid}").json()
        except: out["br_ranked"] = None

        try: out["cs_ranked"] = client.get(f"https://flash-player-info-v1.vercel.app/stats/RANKED/cs?uid={uid}").json()
        except: out["cs_ranked"] = None

        try: out["br_career"] = client.get(f"https://flash-player-info-v1.vercel.app/stats/CAREER/br?uid={uid}").json()
        except: out["br_career"] = None

        try: out["cs_career"] = client.get(f"https://flash-player-info-v1.vercel.app/stats/CAREER/cs?uid={uid}").json()
        except: out["cs_career"] = None

    return jsonify(out)

# 5. BAN CHECK ROUTE
@app.route('/bancheck', methods=['GET'])
def get_ban_status():
    uid = request.args.get('uid')
    if not uid: return jsonify({"error": "UID is required"}), 400
    try:
        url = f"https://flash-player-info-v1.vercel.app/bancheck?uid={uid}"
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                data["Region"] = "BD"  # Region SG রিমুভ করে BD ফিক্স করা
                return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"Bancheck error: {e}"}), 500

    return jsonify({"error": "Ban status unavailable"}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)