"""Export mymind cookies from Windows Credential Locker for Railway deployment."""

import keyring

KEYRING_SERVICE = "mymind-api"

jwt = keyring.get_password(KEYRING_SERVICE, "jwt")
cid = keyring.get_password(KEYRING_SERVICE, "cid")
token = keyring.get_password(KEYRING_SERVICE, "authenticity_token")

if not all([jwt, cid, token]):
    print("No cookies found. Run 'mymind login' first.")
    raise SystemExit(1)

print("Copy these into Railway environment variables:\n")
print(f"MYMIND_JWT={jwt}")
print(f"MYMIND_CID={cid}")
print(f"MYMIND_AUTHENTICITY_TOKEN={token}")
