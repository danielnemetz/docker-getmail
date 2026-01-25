# Dockerized Email Fetcher for Mailcow

A lightweight Docker container that fetches emails from external POP3/IMAP accounts (using `getmail6`) and delivers them to a local Mailcow instance via SMTP (using `msmtp`).

## Features

- **Fetch & Deliver:** Retrieves emails and pushes them to your Mailcow environment.
- **Retention Policy:** Keeps emails on the source server for `N` days (configurable).
- **Infinite Loop:** Runs permanently and fetches periodically (configurable interval).
- **Safety:** Does **not** re-fetch locally deleted mails (tracks UIDs). Does **not** sync source deletions to local.
- **Dry-Run:** Mode to check connections and message counts without fetching.
- **Non-Root:** Runs safely as user `getmail`.

## Setup

### 1. Configuration

Create a `accounts.list` file rooted in the project directory (see `accounts.list.example`):

```bash
cp accounts.list.example accounts.list
nano accounts.list
```

**Format:** `SourceUser:"SourcePassword":SourceHost:TargetEmail`

> **Note:** Passwords with special characters should be enclosed in quotes `"`.

### 2. Environment Variables (.env or compose.yml)

Adjust settings in `compose.yml`:

- `FETCH_INTERVAL`: Time in seconds between checks (default: `300` = 5 mins).
- `DELETE_AFTER_DAYS`: Days to keep mail on source server (default: `7`).

### 3. Start

```bash
docker compose up -d --build
```

## Usage

### Check Logs

```bash
docker compose logs -f email-fetcher
```

### Dry Run (Test Connection)

To check if logins work without downloading anything:

```bash
docker compose run --rm email-fetcher --dry-run
```

## Architecture

- **Helper Script (`run.py`):** Handles logic, config parsing, and the daemon loop.
- **Getmail6:** The engine fetching the mails via IMAP/POP3 SSL.
- **Msmtp:** The relay sending mails to `postfix-mailcow` (internal docker network).

## Requirements

- This container must be in the same Docker network as Mailcow (`mailcowdockerized_mailcow-network`).
- External mail accounts must allow IMAP/POP3 (check your provider settings, e.g. IONOS requires explicit activation).
