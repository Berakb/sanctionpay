# Security Policy

SanctionPay handles compliance-sensitive workflows (sanction screening, on-chain
records, and payments). We take security seriously and appreciate responsible
disclosure.

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately via GitHub's **[Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)**
(Security tab → *Report a vulnerability*), or email the maintainers at
**security@sanctionpay.io**.

Please include:

- A description of the issue and its impact
- Steps to reproduce (proof-of-concept if possible)
- Affected component (`backend`, `agent`, `contracts`, `frontend`) and version/commit

We aim to acknowledge reports within **72 hours** and to provide a remediation
timeline after triage.

## Scope

In scope:

- The FastAPI backend (`backend/`) — auth, x402 verification, injection, SSRF
- The Odra contract (`contracts/`) — access control, state integrity
- The agent (`agent/`) — payment handling, key management
- Handling of secrets and API keys

Out of scope:

- Vulnerabilities in third-party sanction-list sources or the CSPR.cloud
  facilitator
- Findings that require a compromised host or physical access

## Handling of secrets

- Never commit real keys. `.env` is git-ignored; `.env.example` documents the
  required variables with placeholder values.
- The contract's on-chain writes are gated by an authorized-writer allowlist;
  the writer key must be stored in a secret manager in production, never in the
  repo.

## Supported versions

This is an actively developed hackathon project; security fixes target the
`main` branch. Pin to a commit for reproducible deployments.
