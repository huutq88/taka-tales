import json
import os
import subprocess
import base64
import random
import urllib.request

CODEX_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
LOCAL_STORE_PATH = os.path.expanduser("/Users/huutq/Desktop/WorkingSpace/Taka/appota-hub/services/taka-router/accounts_store.json")
ROUTER_BASE_URL = os.getenv("TAKA_ROUTER_URL", "https://taka-router.up.railway.app").rstrip("/")

def get_chatgpt_account_id(access_token):
    try:
        payload_b64 = access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
    except Exception:
        return ""

def fetch_accounts():
    # 1. Server-to-Server Flow: Call Taka Router API over HTTP
    api_url = f"{ROUTER_BASE_URL}/admin/accounts?include_credentials=true"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "TakaTalesEngine/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            accounts = data.get("accounts", [])
            valid_accs = []
            for a in accounts:
                access_token = a.get("access_token") or a.get("credentials", {}).get("access_token")
                refresh_token = a.get("refresh_token") or a.get("credentials", {}).get("refresh_token")
                if access_token:
                    valid_accs.append({
                        "name": a.get("name"),
                        "id": a.get("id"),
                        "credentials": {
                            "access_token": access_token,
                            "refresh_token": refresh_token
                        }
                    })
            if valid_accs:
                print(f"🌐 Fetched {len(valid_accs)} active account(s) via HTTP from Taka Router ({ROUTER_BASE_URL})")
                return valid_accs
    except Exception as e:
        print(f"⚠️ Could not fetch accounts over HTTP from {api_url}: {e}")

    # 2. Local Fallback (for local offline dev)
    if os.path.exists(LOCAL_STORE_PATH):
        try:
            with open(LOCAL_STORE_PATH, "r", encoding="utf-8") as f:
                accounts = json.load(f)
            valid_accs = [a for a in accounts if a.get("credentials", {}).get("access_token")]
            if valid_accs:
                print(f"📁 Loaded {len(valid_accs)} account(s) from local file: {LOCAL_STORE_PATH}")
                return valid_accs
        except Exception as e:
            print(f"⚠️ Local store file read error: {e}")

    # 3. Existing ~/.codex/auth.json fallback
    if os.path.exists(CODEX_AUTH_PATH):
        try:
            with open(CODEX_AUTH_PATH, "r", encoding="utf-8") as f:
                c_auth = json.load(f)
            toks = c_auth.get("tokens", {})
            if toks.get("access_token"):
                print(f"🔑 Using active credentials from {CODEX_AUTH_PATH}")
                return [{
                    "name": "codex_local_active",
                    "id": "codex_local_active",
                    "credentials": {
                        "access_token": toks.get("access_token"),
                        "refresh_token": toks.get("refresh_token")
                    }
                }]
        except Exception:
            pass

    return []

def rotate_auth(index=None):
    valid_accs = fetch_accounts()
    if not valid_accs:
        print("❌ No valid accounts found in Taka Router API or local store.")
        return False

    if index is not None and index < len(valid_accs):
        target = valid_accs[index]
    else:
        target = random.choice(valid_accs)
        
    creds = target["credentials"]
    acc_id = get_chatgpt_account_id(creds["access_token"]) or creds.get("account_id", "")
    
    new_auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": creds["access_token"],
            "refresh_token": creds["refresh_token"],
            "account_id": acc_id
        }
    }
    
    os.makedirs(os.path.dirname(CODEX_AUTH_PATH), exist_ok=True)
    with open(CODEX_AUTH_PATH, "w", encoding="utf-8") as f:
        json.dump(new_auth, f, indent=2)
    
    print(f"✅ Rotated auth to account: {target.get('name')} (ID: {target.get('id')})")
    
    # Restart ima2 serve to refresh proxy
    subprocess.run(["npx", "-y", "ima2-gen", "serve", "--force"], capture_output=True)
    return True

if __name__ == "__main__":
    rotate_auth()
