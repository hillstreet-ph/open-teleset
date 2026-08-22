#!/usr/bin/env bash
# Automate AWS CLI configuration for Cloudflare R2 (open-teleset)
# Usage: R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... ./scripts/configure-r2-awscli.sh
set -euo pipefail
ACCOUNT_ID="${R2_ACCOUNT_ID:-c0e6bd9a7249856cb8497e7fe340e7ce}"
ENDPOINT="${R2_ENDPOINT:-https://${ACCOUNT_ID}.r2.cloudflarestorage.com}"
PROFILE="${AWS_R2_PROFILE:-r2}"
R2_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:-${AWS_ACCESS_KEY_ID:-}}"
R2_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:-${AWS_SECRET_ACCESS_KEY:-}}"
if [[ -z "${R2_ACCESS_KEY_ID}" || -z "${R2_SECRET_ACCESS_KEY}" ]]; then
  echo "Set R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY" >&2
  exit 1
fi
mkdir -p "$HOME/.aws"
chmod 700 "$HOME/.aws" 2>/dev/null || true
export PROFILE ENDPOINT R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY
python3 - <<'PY'
from pathlib import Path
import os, re
profile = os.environ['PROFILE']
ak = os.environ['R2_ACCESS_KEY_ID']
sk = os.environ['R2_SECRET_ACCESS_KEY']
endpoint = os.environ['ENDPOINT']
cred = Path.home() / '.aws' / 'credentials'
cfg = Path.home() / '.aws' / 'config'
def upsert(path, header, body):
    text = path.read_text() if path.exists() else ''
    block = f'[{header}]\n' + body + '\n'
    pat = re.compile(rf'(?ms)^\[{re.escape(header)}\]\n(?:.*?)(?=^\[|\Z)')
    text = pat.sub(block, text) if pat.search(text) else ((text.rstrip()+'\n\n'+block) if text else block)
    path.write_text(text)
upsert(cred, profile, f'aws_access_key_id = {ak}\naws_secret_access_key = {sk}')
cred.chmod(0o600)
upsert(cfg, f'profile {profile}', f'region = auto\noutput = json\nendpoint_url = {endpoint}')
env = Path.home() / '.aws' / 'r2-env.sh'
env.write_text(f'export AWS_PROFILE={profile}\nexport AWS_ENDPOINT_URL={endpoint}\nexport AWS_DEFAULT_REGION=auto\nexport R2_ENDPOINT={endpoint}\n')
print(f'Configured AWS profile {profile!r} -> {endpoint}')
print('Run: source ~/.aws/r2-env.sh && aws s3 ls s3://open-teleset/')
PY
