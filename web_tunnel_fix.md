# Feeder Web Tunnel: Required Fixes

Based on a technical review of the Aggregator's implementation, the following changes are required on the **Feeder side** to ensure stable and successful tunnel connections. 

> [!IMPORTANT]
> These changes are specific to the **Feeder Client**. No changes to the Aggregator service are required to resolve these issues.

## 1. ID Sanitization Mismatch
The current Feeder sanitization logic replaces underscores with hyphens. The Aggregator preserves underscores, which results in a `feeder_id` mismatch during registration.

*   **Required Change**: Update the name normalization step to **only** replace spaces with hyphens. Do not replace underscores.
*   **Separator Logic**: Use specific MLAT separators (` | v` or `___v`) to extract the site name, rather than splitting by the first piper.

### Corrected Sanitization Logic (Python)
```python
import re

def get_feeder_id(raw_name):
    # Extract only the name before version separators
    for sep in (" | v", "___v"):
        if sep in raw_name:
            raw_name = raw_name.split(sep, 1)[0]
            break
    
    # 1. Lowercase and replace spaces with hyphens
    s = raw_name.strip().lower().replace(" ", "-")
    # 2. Strict character filtering (Preserve underscores)
    s = re.sub(r"[^a-z0-9\-_]", "-", s)
    # 3. Collapse multiple hyphens and trim
    s = re.sub(r"-+", "-", s).strip("-")
    return s
```

## 2. Heartbeat & Keep-Alive Logic
The Feeder cannot wait for the server to initiate an application-level ping.

*   **Mismatch**: The server does NOT send `{"type": "ping"}` JSON messages. It relies on standard WebSocket-level ping frames.
*   **Required Change**: The Feeder Client should remove any code waiting for a JSON `ping`.
*   **Keep-Alive**: The Feeder should proactively send `{"type": "pong"}` to the server periodically to prevent idle connection closure. 
*   **Warning**: Do not send `{"type": "ping"}` from the Feeder; the Aggregator treats this as an "Unknown message type" and ignores it.

## 3. Technical Requirements Recap
| Requirement | Value |
| :--- | :--- |
| **Underscores** | Allowed and preserved in IDs (e.g. `92882_test`). |
| **Frame Type** | Must be **Text Frame** (UTF-8 string). |
| **Timeout** | Registration must complete within 30 seconds of `on_open`. |
| **Target Ports** | Use `X-Tunnel-Target` to route to Local Port 5000 (dashboard) or 8080 (tar1090). |
