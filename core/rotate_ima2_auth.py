import json
import os
import subprocess
import base64
import random
import urllib.request

CODEX_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
LOCAL_STORE_PATH = os.path.expanduser("/Users/huutq/Desktop/WorkingSpace/Taka/appota-hub/services/taka-router/accounts_store.json")
RAILWAY_ACCOUNTS_URL = "https://taka-router.up.railway.app/admin/accounts?include_credentials=true"

def get_chatgpt_account_id(access_token):
    try:
        payload_b64 = access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
    except Exception:
        return ""

def fetch_accounts():
    # 1. Try fetching live accounts from Railway Taka Router
    try:
        req = urllib.request.Request(RAILWAY_ACCOUNTS_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            accounts = data.get("accounts", [])
            valid_accs = [
                {
                    "name": a.get("name"),
                    "id": a.get("id"),
                    "credentials": {
                        "access_token": a.get("access_token"),
                        "refresh_token": a.get("refresh_token")
                    }
                }
                for a in accounts
                if a.get("access_token") and a.get("status") == "healthy"
            ]
            if valid_accs:
                print("🌐 Fetched active accounts from Railway Taka Router.")
                return valid_accs
    except Exception as e:
        print(f"⚠️ Railway router fetch skipped/failed: {e}")

    # 2. Fallback to local accounts_store.json
    if os.path.exists(LOCAL_STORE_PATH):
        try:
            with open(LOCAL_STORE_PATH, "r") as f:
                accounts = json.load(f)
            valid_accs = [a for a in accounts if a.get("credentials", {}).get("access_token")]
            if valid_accs:
                print("📁 Loaded active accounts from local accounts_store.json.")
                return valid_accs
        except Exception as e:
            print(f"⚠️ Local store error: {e}")

    return []

def rotate_auth(index=None):
    valid_accs = fetch_accounts()
    if not valid_accs:
        print("❌ No valid accounts found in Railway pool or local store.")
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
    with open(CODEX_AUTH_PATH, "w") as f:
        json.dump(new_auth, f, indent=2)
    
    print(f"✅ Rotated auth to account: {target.get('name')} (ID: {target.get('id')})")
    
    # Restart ima2 serve to refresh proxy
    subprocess.run(["npx", "-y", "ima2-gen", "serve", "--force"], capture_output=True)
    return True

if __name__ == "__main__":
    rotate_auth()
