# Moonback MVP

A tiny experiment in a web surface that is almost empty for humans but structured for machine visitors.

## Run locally

```bash
python3 server.py
```

Open:
- http://127.0.0.1:8000/          human-facing black page
- http://127.0.0.1:8000/observe   request observer
- http://127.0.0.1:8000/llms.txt
- http://127.0.0.1:8000/agent.json
- http://127.0.0.1:8000/signal/001

## Notes

The "agent-like" label is only a heuristic based on User-Agent text. It does not prove a visitor is an AI.
For real public deployment, place this behind HTTPS and use platform/server logs as an additional source of truth.
