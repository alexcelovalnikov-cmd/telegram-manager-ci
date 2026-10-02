# Security Policy

Telegram Manager processes communication and workflow metadata, so reports must avoid exposing
real user data.

## Reporting a vulnerability

- If GitHub offers a private **Report a vulnerability** action for this repository, use it.
- Otherwise open a minimal public issue that contains no exploit payload, credential, private
  message, production hostname, token, session material or customer identifier, and ask for a
  private reporting channel.
- Do not paste secrets even if you believe they have expired.

Include the affected component, impact, reproduction conditions using synthetic data, and the
public snapshot commit or `SOURCE_MANIFEST.json` source SHA when known.

## Public mirror boundary

This repository contains sanitized source and synthetic fixtures only. Production credentials,
databases, private messages, operational logs and deployment authority are intentionally absent.
A vulnerability in the sanitizer/publication boundary should be treated as security-sensitive.
