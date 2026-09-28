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

# গ্যারিনা অফিশিয়াল সিক্রেট কি এবং আইভি
MAIN_KEY = base64.b64decode('WWcmdGMlREV1aDYlWmNeOA==')  # Yg&tc%DEuh6%Zc^8
MAIN_IV = base64.b64decode('Nm95WkRyMjJFM3ljaGpNJQ==')   # 6oyZDr22E3ychjM%
RELEASEVERSION = "OB55"
USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 13; CPH2095 Build/RKQ1.211119.001)"
DEFAULT_SERVER = "https://clientbp.ppmainecoonghj.com"

# =====================================================================
# গেস্ট অ্যাকাউন্ট: শুধু এই তিন লাইন বদলালেই হবে (uid=...&password=...)
# =====================================================================
ACCOUNT_BD = "uid=7965111855&password=45FD22E8730EF6F9863343DCA572FABA050B544721101D88C8CE4570DB849086"                       # BD + বাকি সব রিজিয়ন
ACCOUNT_IND = "uid=4363983977&password=ISHITA_0AFN5_BY_SPIDEERIO_GAMING_UY12H"        # IND
ACCOUNT_AMERICAS = "uid=4682784982&password=GHOST_TNVW1_RIZER_QTFT0"                 # BR, US, SAC, NA (এটা এখন মৃত)

ACCOUNTS = {"BD": ACCOUNT_BD, "IND": ACCOUNT_IND, "AMERICAS": ACCOUNT_AMERICAS}

# বিডি ও ইন্ডিয়াকে সবার প্রথমে রাখা হয়েছে
SUPPORTED_REGIONS = ["BD", "IND", "SG", "BR", "US", "SAC", "NA", "PK", "ID", "TH", "VN", "TW", "RU", "ME", "CIS", "EUROPE"]

RATE_LIMIT_COOLDOWN = 90    # 429 পেলে ওই অ্যাকাউন্ট এত সেকেন্ড বিশ্রামে থাকবে
TOKEN_FAIL_COOLDOWN = 300   # টোকেন না পেলে এত সেকেন্ড আবার চেষ্টা করা হবে না

app = Flask(__name__)
CORS(app)

cached_tokens = {}        # group -> টোকেন
blocked_until = {}        # group -> কখন পর্যন্ত Garena রিকোয়েস্ট বন্ধ (429 এর জন্য)
token_failed_until = {}   # group -> কখন পর্যন্ত টোকেন চেষ্টা বন্ধ
uid_region_cache = {}


def get_group(region: str) -> str:
    r = region.upper()
    if r == "IND":
        return "IND"
    if r in {"BR", "US", "SAC", "NA"}:
        return "AMERICAS"
    return "BD"


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


# === টোকেন সংগ্রহ ফাংশন (অ্যাকাউন্ট গ্রুপ অনুযায়ী ক্যাশ) ===
def get_token_info(region: str):
    group = get_group(region)
    now = time.time()

    info = cached_tokens.get(group)
    if info and now < info.get('expires_at', 0) - 60:
        return info['token'], info['region'], info['server_url']

    # কিছুক্ষণ আগে ফেল করে থাকলে আবার হ্যামার করবে না
    if now < token_failed_until.get(group, 0):
        return None, region, DEFAULT_SERVER

    try:
        creds = parse_qs(ACCOUNTS[group])
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

                # মৃত/ব্যান অ্যাকাউন্টে টোকেন ও serverUrl খালি আসে
                if not raw_token or not msg.get('serverUrl'):
                    app.logger.error(f"Dead account / empty token for {group}")
                    token_failed_until[group] = now + TOKEN_FAIL_COOLDOWN
                    return None, region, DEFAULT_SERVER

                bearer_token = raw_token if raw_token.startswith("Bearer ") else f"Bearer {raw_token}"

                try:
                    ttl = int(msg.get('ttl') or 25200)
                except (TypeError, ValueError):
                    ttl = 25200

                cached_tokens[group] = {
                    'token': bearer_token,
                    'region': msg.get('lockRegion') or msg.get('region') or region,
                    'server_url': msg['serverUrl'],
                    'expires_at': now + ttl
                }
                return cached_tokens[group]['token'], cached_tokens[group]['region'], cached_tokens[group]['server_url']
            else:
                app.logger.error(f"Token API status {resp.status_code} for {group}")
    except Exception as e:
        app.logger.error(f"Token generation failed for {group}: {e}")

    token_failed_until[group] = now + TOKEN_FAIL_COOLDOWN
    return None, region, DEFAULT_SERVER


# === গ্যারিনা অফিশিয়াল সার্ভার রিকোয়েস্ট ===
def fetch_player_data(uid: str, region: str):
    group = get_group(region)
    now = time.time()

    if now < blocked_until.get(group, 0):
        wait = int(blocked_until[group] - now)
        raise Exception(f"{group} account rate-limited (429) recently, cooling down {wait}s")

    token, lock_reg, server = get_token_info(region)
    if not token:
        raise Exception(f"Failed to obtain token for {group} account (dead account or token service problem)")

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
        if resp.status_code == 429:
            blocked_until[group] = time.time() + RATE_LIMIT_COOLDOWN
            raise Exception("Garena responded: 429 (rate limited)")
        raise Exception(f"Garena responded: {resp.status_code}")


def has_basic_info(data) -> bool:
    return bool(data and (data.get("basicInfo") or data.get("basic_info")))


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

    errors = {}

    # ১. পূর্বে পাওয়া রিজিয়ন থাকলে সরাসরি চেক
    if uid in uid_region_cache:
        try:
            data = fetch_player_data(uid, uid_region_cache[uid])
            if has_basic_info(data):
                return json.dumps(data, indent=2, ensure_ascii=False), 200, {'Content-Type': 'application/json; charset=utf-8'}
        except Exception as e:
            errors["cache_" + uid_region_cache[uid]] = str(e)

    # ২. স্ক্যান: একই অ্যাকাউন্ট/সার্ভারে বারবার রিকোয়েস্ট না করে প্রতি গ্রুপে একবার
    tried_groups = set()
    rate_limited = False
    for region in SUPPORTED_REGIONS:
        group = get_group(region)
        if group in tried_groups:
            continue
        tried_groups.add(group)
        try:
            data = fetch_player_data(uid, region)
            if has_basic_info(data):
                uid_region_cache[uid] = region
                return json.dumps(data, indent=2, ensure_ascii=False), 200, {'Content-Type': 'application/json; charset=utf-8'}
            errors[group] = "response ok but no basicInfo (UID not on this server?)"
        except Exception as e:
            errors[group] = str(e)
            if "429" in str(e):
                rate_limited = True

    result = {"error": "UID not found in any region.", "details": errors}
    if rate_limited:
        result["hint"] = "Garena rate-limited the guest account (429). Wait 1-2 minutes, or replace the guest accounts at the top of app.py."
    return jsonify(result), 404


# === ডিবাগ রুট ===
# ব্যবহার: /debug?uid=আপনার_UID&region=BD
@app.route('/debug', methods=['GET'])
def debug():
    uid = request.args.get('uid', '')
    region = request.args.get('region', 'BD').upper()
    group = get_group(region)
    out = {"region": region, "account_group": group, "release": RELEASEVERSION}

    if not uid.isdigit():
        out["problem"] = "uid দিন (সংখ্যা)"
        return jsonify(out), 400

    try:
        token, lock_reg, server = get_token_info(region)
        out["token_ok"] = bool(token)
        out["lock_region"] = lock_reg
        out["server"] = server
    except Exception as e:
        out["token_error"] = str(e)
        return jsonify(out)

    if not token:
        out["problem"] = "token পাওয়া যায়নি (অ্যাকাউন্ট মৃত বা টোকেন সার্ভিস সমস্যা)"
        return jsonify(out)

    try:
        req = main_pb2.GetPlayerPersonalShow()
        json_format.ParseDict({'a': int(uid), 'b': 7}, req)
        enc = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, req.SerializeToString())
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
        with httpx.Client(timeout=12.0) as c:
            r = c.post(server.rstrip('/') + "/GetPlayerPersonalShow", content=enc, headers=headers)

        out["garena_status"] = r.status_code
        out["garena_body_len"] = len(r.content)

        if r.status_code == 200:
            try:
                proto_obj = decode_protobuf(r.content, AccountPersonalShow_pb2.AccountPersonalShowInfo)
                parsed = json.loads(json_format.MessageToJson(proto_obj))
                out["decode_ok"] = True
                out["top_level_keys"] = list(parsed.keys())
                out["has_basic_info"] = has_basic_info(parsed)
            except Exception as e:
                out["decode_error"] = str(e)
        else:
            out["garena_body_preview"] = r.text[:200]
    except Exception as e:
        out["garena_error"] = str(e)

    return jsonify(out)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
