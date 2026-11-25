# 0028 — Backups: SQLite online backup + `age` encryption
- **Context:** OPS-6 asks for a nightly encrypted copy of SQLite and the journal, plus a restore drill.
- **Decision:**
  - `helm backup` snapshots each account journal with the SQLite online-backup API (consistent under WAL, without stopping the engine).
  - It tars the snapshots together with the JSONL mirrors and encrypts the archive to an `age` recipient public key. Only the public key lives on the server; the private key stays offline with the owner.
  - `helm restore` decrypts with an identity file and verifies the journal (append-only triggers present, `PRAGMA integrity_check` ok).
- **Consequence:** the `age` CLI is a system dependency (brew/apt). The restore drill test is skipped when it is missing.
