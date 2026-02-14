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
import smtplib
import urllib.request
import urllib.error

# Configuration defaults
ACCOUNTS_FILE = "/app/accounts.list"
# Use user's home directory for getmail state to avoid permission issues
# In DRY_DELIVER mode, use a separate directory so real state is not polluted
GETMAIL_DIR = os.path.expanduser("~/.getmail")
GETMAIL_DIR_DRY = os.path.expanduser("~/.getmail-dry")
DEFAULT_FETCH_INTERVAL = 300 # 5 minutes

# Get retention policy from environment or default to 7 days
try:
    DEFAULT_DELETE_AFTER_DAYS = int(os.environ.get('DELETE_AFTER_DAYS', 7))
except ValueError:
    print("Warning: Invalid DELETE_AFTER_DAYS, defaulting to 7.")
    DEFAULT_DELETE_AFTER_DAYS = 7

# Success Hook URL from environment
SUCCESS_HOOK_URL = os.environ.get('SUCCESS_HOOK_URL')

# LMTP Configuration
LMTP_HOST = os.environ.get('LMTP_HOST', 'dovecot-mailcow')
LMTP_PORT = int(os.environ.get('LMTP_PORT', 24))

# Dry delivery mode: fetch mails but don't actually deliver them
DRY_DELIVER = os.environ.get('DRY_DELIVER', '').lower() in ('1', 'true', 'yes')


def call_webhook(url):
    """
    Calls the specified webhook URL via HTTP GET.
    """
    if not url:
        return

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Calling success webhook: {url}")
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            status = response.getcode()
            print(f"  -> Webhook response status: {status}")
    except urllib.error.URLError as e:
        print(f"  -> Webhook failed: {e}")
    except Exception as e:
        print(f"  -> An unexpected error occurred during webhook call: {e}")


def deliver_lmtp(recipient):
    """
    Reads an email from stdin and delivers it via LMTP to dovecot.
    This function is called by getmail as an MDA (Mail Delivery Agent).
    """
    msg_data = sys.stdin.buffer.read()

    if DRY_DELIVER:
        print(f"[DRY] Would deliver {len(msg_data)} bytes to {recipient} via LMTP ({LMTP_HOST}:{LMTP_PORT})", file=sys.stderr)
        return

    try:
        lmtp = smtplib.LMTP(LMTP_HOST, LMTP_PORT)
        lmtp.sendmail(
            from_addr="getmail-fetcher@localhost",
            to_addrs=[recipient],
            msg=msg_data
        )
        lmtp.quit()
    except Exception as e:
        print(f"LMTP delivery error to {recipient}: {e}", file=sys.stderr)
        sys.exit(1)


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


def generate_getmail_config(account, config_path):
    """
    Generates a getmailrc file for the account.
    Uses run.py itself as the MDA to deliver via LMTP to Dovecot.
    """
    protocol = detect_protocol(account['server'])
    
    # Map our internal protocol string to getmail retriever type
    if protocol == 'IMAP4_SSL':
        retriever_type = 'SimpleIMAPSSLRetriever'
    elif protocol == 'POP3_SSL':
        retriever_type = 'SimplePOP3SSLRetriever'
    else:
        retriever_type = 'SimpleIMAPSSLRetriever' # Default

    # Destination uses MDA_external to call this script in LMTP delivery mode
    config_content = f"""[retriever]
type = {retriever_type}
server = {account['server']}
username = {account['user']}
password = {account['password']}

[destination]
type = MDA_external
path = /usr/local/bin/python3
arguments = ("/app/run.py", "--deliver-lmtp", "{account['target']}")
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
    # Use separate state directory in dry mode to avoid marking mails as seen
    active_dir = GETMAIL_DIR_DRY if DRY_DELIVER else GETMAIL_DIR

    # Clean non-fs characters
    safe_user = re.sub(r'[^a-zA-Z0-9]', '_', account['user'])
    safe_server = re.sub(r'[^a-zA-Z0-9]', '_', account['server'])
    config_name = f"getmailrc_{safe_user}_{safe_server}"
    config_path = os.path.join(active_dir, config_name)
    
    # Ensure getmail dir exists
    os.makedirs(active_dir, exist_ok=True)

    # Note: Generating config every time allows changes in accounts.list to apply without container rebuild
    generate_getmail_config(account, config_path)

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{account['user']}] Starting fetch...")
    
    cmd = ["getmail", f"--getmaildir={active_dir}", f"--rcfile={config_name}"]
    
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running getmail for {account['user']}: exit code {e.returncode}")


def main():
    parser = argparse.ArgumentParser(description="Email Fetcher Wrapper")
    parser.add_argument('--dry-run', action='store_true', help="Check connections and counts without fetching.")
    parser.add_argument('--accounts', type=str, default=ACCOUNTS_FILE, help="Path to accounts.list")
    parser.add_argument('--interval', type=int, default=int(os.environ.get('FETCH_INTERVAL', DEFAULT_FETCH_INTERVAL)), help="Fetch interval in seconds (Daemon mode only)")
    parser.add_argument('--deliver-lmtp', type=str, metavar='RECIPIENT', help="MDA mode: Read email from stdin and deliver via LMTP to the given recipient.")
    
    args = parser.parse_args()

    # MDA mode: deliver a single email via LMTP and exit
    if args.deliver_lmtp:
        deliver_lmtp(args.deliver_lmtp)
        return

    print(f"--- Configuration ---")
    print(f"LMTP_HOST: {LMTP_HOST}")
    print(f"LMTP_PORT: {LMTP_PORT}")
    print(f"DRY_DELIVER: {DRY_DELIVER}")
    print(f"SUCCESS_HOOK_URL: {'Set' if SUCCESS_HOOK_URL else 'Not set'}")
    print(f"FETCH_INTERVAL: {os.environ.get('FETCH_INTERVAL', DEFAULT_FETCH_INTERVAL)}")
    print(f"DELETE_AFTER_DAYS: {DEFAULT_DELETE_AFTER_DAYS}")
    print(f"---------------------")

    if DRY_DELIVER:
        print("\n⚠️  DRY DELIVER MODE: Mails will be fetched but NOT delivered to mailboxes!")

    if args.dry_run:
        print("\n--- DRY RUN MODE: Checking Stats Only ---")
        for acc in parse_accounts(args.accounts):
            dry_run_check(acc)
    else:
        # Daemon Mode
        print(f"\n--- FETCH MODE STARTING ---")
        print(f"Interval: {args.interval} seconds")
        
        while True:
            # Re-parse accounts every loop to allow dynamic updates without restart
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
