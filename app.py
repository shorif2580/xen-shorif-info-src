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

# গ্যারিনা অফিশিয়াল সিক্রেট কি এবং আইভি
MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==') # Yg&tc%DEuh6%Zc^8
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')  # 6oyZDr22E3ychjM%
RELEASEVERSION = "OB55"
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"

# বিডি ও ইন্ডিয়াকে সবার প্রথমে রাখা হয়েছে যাতে ২ সেকেন্ডে রেসপন্স আসে
SUPPORTED_REGIONS = ["BD", "IND", "SG", "BR", "US", "SAC", "NA", "PK", "ID", "TH", "VN", "TW", "RU", "ME", "CIS", "EUROPE"]

app = Flask(__name__)
CORS(app)

cached_tokens = {}
uid_region_cache = {}

# === ক্রিপ্টোগ্রাফি হেল্পার ===
def pad(text: bytes) -> bytes:
    n = AES.block_size - (len(text) % AES.block_size)
    return text + bytes([n] * n)

def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext))

def decode_protobuf(data: bytes, msg_type):
    inst = msg_type()
    inst.ParseFromString(data)
    return inst

def get_account_credentials(region: str) -> str:
    r = region.upper()
    if r == "IND":
        return "uid=4363983977&password=ISHITA_0AFN5_BY_SPIDEERIO_GAMING_UY12H"
    elif r in {"BR", "US", "SAC", "NA"}:
        return "uid=4682784982&password=GHOST_TNVW1_RIZER_QTFT0"
    else:
        return "uid=4418979127&password=RIZER_K4CY1_RIZER_WNX02"

# === টোকেন সংগ্রহ ফাংশন ===
def get_token_info(region: str):
    info = cached_tokens.get(region)
    now = time.time()
    
    # টোকেন ক্যাশে থাকলে পুনরায় ব্যবহার
    if info and now < info.get('expires_at', 0) - 60:
        return info['token'], info['region'], info['server_url']

    try:
        creds = parse_qs(get_account_credentials(region))
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
                
                cached_tokens[region] = {
                    'token': bearer_token,
                    'region': msg.get('lockRegion') or msg.get('region') or region,
                    'server_url': msg.get('serverUrl', 'https://clientbp.ppmainecoonghj.com'),
                    'expires_at': msg.get('expiry_time', now + 25200)
                }
                return cached_tokens[region]['token'], cached_tokens[region]['region'], cached_tokens[region]['server_url']
    except Exception as e:
        app.logger.error(f"Token generation failed for {region}: {e}")

    return None, region, "https://clientbp.ppmainecoonghj.com"

# === গ্যারিনা অফিশিয়াল সার্ভার রিকোয়েস্ট ===
def fetch_player_data(uid: str, region: str):
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
    with httpx.Client(timeout=12.0) as client:
        resp = client.post(url, content=data_enc, headers=headers)
        if resp.status_code == 200:
            proto_obj = decode_protobuf(resp.content, AccountPersonalShow_pb2.AccountPersonalShowInfo)
            return json.loads(json_format.MessageToJson(proto_obj))
        else:
            raise Exception(f"Garena responded: {resp.status_code}")

# === API রুট ===
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status": "Online",
        "service": "Free Fire Player Info API",
        "developer": "@xen_shorif",
        "usage": "/player-info?uid=YOUR_UID"
    })

@app.route('/player-info', methods=['GET'])
def get_account_info():
    uid = request.args.get('uid')
    if not uid or not uid.isdigit():
        return jsonify({"error": "Please provide a valid numeric UID."}), 400

    # ১. পূর্বে পাওয়া রিজিয়ন থাকলে সরাসরি চেক
    if uid in uid_region_cache:
        try:
            data = fetch_player_data(uid, uid_region_cache[uid])
            if data and (data.get("basicInfo") or data.get("basic_info")):
                return json.dumps(data, indent=2, ensure_ascii=False), 200, {'Content-Type': 'application/json; charset=utf-8'}
        except Exception:
            pass

    # ২. সব রিজিয়ন স্ক্যান (প্রথমে BD, IND, SG)
    for region in SUPPORTED_REGIONS:
        try:
            data = fetch_player_data(uid, region)
            if data and (data.get("basicInfo") or data.get("basic_info")):
                uid_region_cache[uid] = region
                return json.dumps(data, indent=2, ensure_ascii=False), 200, {'Content-Type': 'application/json; charset=utf-8'}
        except Exception:
            continue

    return jsonify({"error": "UID not found in any region."}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)