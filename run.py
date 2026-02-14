import os
import sys
import argparse
import imaplib
import poplib
import subprocess
import shutil
import re
import time
import datetime
import urllib.request
import urllib.error

# Configuration defaults
ACCOUNTS_FILE = "/app/accounts.list"
# Use user's home directory for getmail state to avoid permission issues
GETMAIL_DIR = os.path.expanduser("~/.getmail")
DEFAULT_FETCH_INTERVAL = 300 # 5 minutes

# Get retention policy from environment or default to 7 days
try:
    DEFAULT_DELETE_AFTER_DAYS = int(os.environ.get('DELETE_AFTER_DAYS', 7))
except ValueError:
    print("Warning: Invalid DELETE_AFTER_DAYS, defaulting to 7.")
    DEFAULT_DELETE_AFTER_DAYS = 7

# Success Hook URL from environment
SUCCESS_HOOK_URL = os.environ.get('SUCCESS_HOOK_URL')

# Sender Email for SMTP (msmtp)
# Default to a generic address to avoid 'local domain' spoofing checks in Mailcow
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'getmail@mailcow-fetcher.local')

# Print configuration on startup for debugging
print(f"--- Configuration ---")
print(f"ACCOUNTS_FILE: {ACCOUNTS_FILE}")
print(f"SUCCESS_HOOK_URL: {'Set' if SUCCESS_HOOK_URL else 'Not set'}")
print(f"SENDER_EMAIL: {SENDER_EMAIL}")
print(f"FETCH_INTERVAL: {os.environ.get('FETCH_INTERVAL', DEFAULT_FETCH_INTERVAL)}")
print(f"---------------------")

def call_webhook(url):
    """
    Calls the specified webhook URL via HTTP GET.
    """
    if not url:
        return

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Calling success webhook: {url}")
    try:
        # Using a simple GET request as per requirements
        with urllib.request.urlopen(url, timeout=10) as response:
            status = response.getcode()
            print(f"  -> Webhook response status: {status}")
    except urllib.error.URLError as e:
        print(f"  -> Webhook failed: {e}")
    except Exception as e:
        print(f"  -> An unexpected error occurred during webhook call: {e}")

def parse_accounts(filepath):
    """
    Parses the accounts.list file.
    Expected format: username:"password":mailserver:local-account
    Ignores lines starting with #.
    """
    accounts = []
    if not os.path.exists(filepath):
        print(f"Error: Accounts file not found at {filepath}")
        return []

    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            # Regex to capture the 4 components. 
            match = re.search(r'^(.*?):"(.*?)":(.*?):(.*?)$', line)
            if match:
                accounts.append({
                    'user': match.group(1).strip(),
                    'password': match.group(2), # Password inside quotes
                    'server': match.group(3).strip(),
                    'target': match.group(4).strip()
                })
            else:
                print(f"Warning: Could not parse line strictly, skipping: {line}")
    return accounts

def detect_protocol(server_address):
    """
    Simple heuristic to guess protocol.
    If 'pop' is in the server name, assume POP3_SSL.
    Otherwise assume IMAP4_SSL.
    """
    if 'pop' in server_address.lower():
        return 'POP3_SSL'
    return 'IMAP4_SSL'

def dry_run_check(account):
    """
    Connects to the server and checks for messages without downloading.
    """
    server = account['server']
    user = account['user']
    password = account['password']
    protocol = detect_protocol(server)

    print(f"[{user}@{server}] Checking via {protocol}...")

    try:
        if protocol == 'IMAP4_SSL':
            with imaplib.IMAP4_SSL(server) as mail:
                mail.login(user, password)
                mail.select('INBOX')
                typ, data = mail.search(None, 'ALL')
                if typ == 'OK':
                    num_msgs = len(data[0].split())
                    print(f"  -> Found {num_msgs} messages in INBOX.")
                else:
                    print("  -> Failed to search INBOX.")
        
        elif protocol == 'POP3_SSL':
            # POP3 doesn't have folders usually, just one inbox
            mail = None
            try:
                mail = poplib.POP3_SSL(server)
                mail.user(user)
                mail.pass_(password)
                num_msgs, size = mail.stat()
                print(f"  -> Found {num_msgs} messages (Total size: {size} bytes).")
            finally:
                if mail:
                    mail.quit()

    except Exception as e:
        print(f"  -> Connection Failed: {e}")

def configure_msmtp():
    """
    Creates a global msmtp configuration to relay through the mailcow postfix.
    Assumes the container is in the proper network and can reach 'postfix-mailcow'.
    """
    msmtp_config = f"""defaults
auth           off
tls            off
tls_trust_file /etc/ssl/certs/ca-certificates.crt
# Log to stderr so docker can capture it without file permission issues
logfile        /dev/stderr

account        default
host           postfix-mailcow
port           25
from           {SENDER_EMAIL}
"""
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Writing msmtp config with SENDER_EMAIL={SENDER_EMAIL}")
    # Write to user config ~/.msmtprc (since we are non-root now)
    config_path = os.path.expanduser("~/.msmtprc")
    with open(config_path, 'w') as f:
        f.write(msmtp_config)
    
    # Secure the file (msmtp complains if permissions are too open)
    os.chmod(config_path, 0o600)

def generate_getmail_config(account, config_path):
    """
    Generates a getmailrc file for the account.
    """
    protocol = detect_protocol(account['server'])
    
    # Map our internal protocol string to getmail retriever type
    if protocol == 'IMAP4_SSL':
        retriever_type = 'SimpleIMAPSSLRetriever'
    elif protocol == 'POP3_SSL':
        retriever_type = 'SimplePOP3SSLRetriever'
    else:
        retriever_type = 'SimpleIMAPSSLRetriever' # Default

    # Destination uses MDA_external to call msmtp
    config_content = f"""[retriever]
type = {retriever_type}
server = {account['server']}
username = {account['user']}
password = {account['password']}

[destination]
type = MDA_external
path = /usr/bin/msmtp
arguments = ("-a", "default", "-f", "{SENDER_EMAIL}", "{account['target']}")
ignore_stderr = true

[options]
read_all = false
delete = false
delete_after = {DEFAULT_DELETE_AFTER_DAYS}
# getmail crashes if message_log is /dev/stdout because it tries to seek.
# Using a file in the user directory or /dev/null is safer.
message_log = /home/getmail/.getmail/getmail.log
"""
    with open(config_path, 'w') as f:
        f.write(config_content)

def run_fetch(account):
    """
    Runs getmail for the specified account.
    """
    # Clean non-fs characters
    safe_user = re.sub(r'[^a-zA-Z0-9]', '_', account['user'])
    safe_server = re.sub(r'[^a-zA-Z0-9]', '_', account['server'])
    config_name = f"getmailrc_{safe_user}_{safe_server}"
    config_path = os.path.join(GETMAIL_DIR, config_name)
    
    # Ensure getmail dir exists
    os.makedirs(GETMAIL_DIR, exist_ok=True)

    # Note: Generating config every time allows changes in accounts.list to apply without container rebuild
    generate_getmail_config(account, config_path)

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{account['user']}] Starting fetch for account {account['user']}...")
    
    # Debug: Check if msmtp exists
    msmtp_path = "/usr/bin/msmtp"
    if not os.path.exists(msmtp_path):
        print(f"CRITICAL ERROR: {msmtp_path} not found inside container!")
        # Try to find it
        alt_path = shutil.which("msmtp")
        print(f"shutil.which('msmtp') says: {alt_path}")
    
    # Debug: Print generated config content (be careful with passwords, but here we need to see it)
    print(f"--- Generated Config ({config_name}) ---")
    with open(config_path, 'r') as f:
        # Hide password in logs for safety, but show the rest
        for line in f:
            if "password =" in line:
                print("password = ********")
            else:
                print(line.strip())
    print(f"------------------------------------------")

    cmd = ["getmail", f"--getmaildir={GETMAIL_DIR}", f"--rcfile={config_name}"]
    
    try:
        # Capture output to avoid log spam unless error? Or verbose?
        # Using check=True to raise exception on error
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running getmail for {account['user']}: {e}")

def main():
    parser = argparse.ArgumentParser(description="Email Fetcher Wrapper")
    parser.add_argument('--dry-run', action='store_true', help="Check connections and counts without fetching.")
    parser.add_argument('--accounts', type=str, default=ACCOUNTS_FILE, help="Path to accounts.list")
    parser.add_argument('--interval', type=int, default=int(os.environ.get('FETCH_INTERVAL', DEFAULT_FETCH_INTERVAL)), help="Fetch interval in seconds (Daemon mode only)")
    
    args = parser.parse_args()

    print("Parsing accounts...")
    accounts = parse_accounts(args.accounts)
    print(f"Found {len(accounts)} accounts.")

    if args.dry_run:
        print("\n--- DRY RUN MODE: Checking Stats Only ---")
        for acc in accounts:
            dry_run_check(acc)
    else:
        # Daemon Mode
        configure_msmtp()
        print(f"\n--- FETCH MODE STARTING ---")
        print(f"Interval: {args.interval} seconds")
        
        while True:
            # Re-parse accounts every loop to allow dynamic updates without restart
            # (Optional improvement: check file mod time)
            current_accounts = parse_accounts(args.accounts)
            
            for acc in current_accounts:
                run_fetch(acc)
            
            # Call webhook after all accounts are processed
            if SUCCESS_HOOK_URL:
                call_webhook(SUCCESS_HOOK_URL)
            
            print(f"Sleeping for {args.interval} seconds...\n")
            time.sleep(args.interval)

if __name__ == "__main__":
    main()
