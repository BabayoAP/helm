# 0014 — Restart policy: HALTED stays HALTED, everything else restarts PAUSED
- **Context:** §10 says HALTED is latched and survives restarts; OPS-5 says "starts in PAUSED if previous state was HALTED"; §16 says "resilient restart to PAUSED". These conflict.
- **Decision:** take the safer reading. A restart never un-latches HALTED (resume needs a fresh one-time code, issued to an authorized channel). A restart from RUNNING or PAUSED comes up PAUSED (exits allowed, no new risk) until an explicit resume.
- **Consequence:** no restart ever re-opens risk by itself. Owner can override later if hands-off auto-resume is wanted.
