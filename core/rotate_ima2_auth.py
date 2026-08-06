import json
import os
import subprocess
import base64
import random

CODEX_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
LOCAL_STORE_PATH = os.path.expanduser("/Users/huutq/Desktop/WorkingSpace/Taka/appota-hub/services/taka-router/accounts_store.json")

def get_chatgpt_account_id(access_token):
    try:
        payload_b64 = access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
    except Exception:
        return ""

def rotate_auth(index=None):
    if not os.path.exists(LOCAL_STORE_PATH):
        print(f"❌ File not found: {LOCAL_STORE_PATH}")
        return False
        
    with open(LOCAL_STORE_PATH, "r") as f:
        accounts = json.load(f)
    
    valid_accs = [a for a in accounts if a.get("credentials", {}).get("access_token")]
    if not valid_accs:
        print("❌ No valid accounts found in store.")
        return False

    # Choose account index or pick random/next
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
    
    with open(CODEX_AUTH_PATH, "w") as f:
        json.dump(new_auth, f, indent=2)
    
    print(f"✅ Rotated auth to account: {target.get('name')} (ID: {target.get('id')})")
    
    # Restart ima2 serve to refresh proxy
    subprocess.run(["npx", "-y", "ima2-gen", "serve", "--force"], capture_output=True)
    return True

if __name__ == "__main__":
    rotate_auth()
