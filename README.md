# PBX Number Router

A small system for managing phone numbers and routing inbound calls to SIP extensions on an Asterisk/FreePBX server.

FreePBX falls back to top-to-bottom matching when many extensions share a prefix. This project works around that by generating the routing from a database: a Flask web panel manages users and number blocks, and a Telegram bot (WuidoBot) lets an admin bulk-upload .txt number lists and allocate them to extensions.

## Architecture
- Asterisk / FreePBX on a Linux VPS: SIP and call routing
- Web panel (panel/, Flask + MariaDB): users, extensions, number allocation; writes Asterisk config and reloads it
- Telegram bot (bot/, aiogram + SQLite, Docker): bulk upload and allocation; talks to the panel API

## Features
- Bulk allocation of numbers parsed from .txt files, by country and prefix
- Numbers expire automatically after 24 hours
- Admin-only bot actions; destructive actions need confirmation
- Input validation for extensions, passwords and numbers

## Security notes
- Secrets come from environment variables (.env, never committed)
- Panel login uses a constant-time password check and limits failed attempts
- The panel only accepts requests from private networks (localhost, Docker, Tailscale)
- MariaDB listens on localhost only; SSH is key-only

## Setup
Copy .env.example to .env and panel/.env.example to panel/.env, fill in the values, create the database from panel/schema.sql, then run:

    docker-compose up -d --build

## Limitations and next steps
- The panel runs on Flask's built-in server; put a production WSGI server behind HTTPS
- SIP passwords are stored in plain text in the database
- Automated tests are not written yet
