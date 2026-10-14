# Running in the background and in Docker

Autostart per user on Linux, macOS and Windows, and an optional hardened container.

[← Back to the README](../README.md)

---

## Run in the background

Each script installs a per-user autostart (no admin/root), bound to localhost, and has a `--remove` / `-Remove` option.

| OS | Command | Logs |
|---|---|---|
| Linux | `./deploy/linux/install-service.sh` | `journalctl --user -u trioditis -f` |
| macOS | `./deploy/macos/install-service.sh` | `logs/server.log` |
| Windows | `powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1` | `logs\server.log` |

## Docker (optional)

Running natively is simpler and keeps Ollama on localhost. If you want the container:

```bash
cp config.example.yaml config.yaml   # set the ollama base_url to http://host.docker.internal:11434/v1
cp .env.example .env                 # TRIODITIS_API_KEY is required inside a container
docker compose up -d --build
```

- **macOS / Windows (Docker Desktop):** `host.docker.internal` reaches Ollama on the host; nothing else to change.
- **Linux:** the container can only reach Ollama if Ollama also listens on the Docker bridge. Use `OLLAMA_HOST=172.17.0.1:11434`, **not** `0.0.0.0`, which would expose your models to the whole network.

The container runs as a non-root user on a read-only filesystem with all capabilities dropped, and is published on `127.0.0.1` only.
