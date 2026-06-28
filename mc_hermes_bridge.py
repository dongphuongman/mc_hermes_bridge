#!/usr/bin/env python3
"""
mc_hermes_bridge.py
-------------------
Cầu nối giữa Mission Control (task board) và Hermes Agent (API server 8642).

Vòng lặp:
  1. Gửi heartbeat để MC giữ agent ở trạng thái online.
  2. Poll task queue của MC để lấy việc được giao cho agent.
  3. Đẩy nội dung task vào Hermes /v1/chat/completions để hermes thực thi.
  4. Báo kết quả ngược về MC (comment + chuyển task sang done).

Chỉ dùng thư viện chuẩn của Python — không cần pip install.
Cấu hình qua biến môi trường (xem phần CONFIG bên dưới).
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

# ----------------------------- CONFIG -----------------------------
# Địa chỉ nội bộ trên docker network của Coolify (cùng stack với hermes/MC).
MC_URL       = os.environ.get("MC_URL", "http://mission-control:3000").rstrip("/")
HERMES_URL   = os.environ.get("HERMES_URL", "http://hermes:8642").rstrip("/")
# Khi gọi MC qua tên service nội bộ, MC chặn theo MC_ALLOWED_HOSTS.
# Đặt MC_HOST_HEADER = domain hợp lệ (vd mc.dxvn.tech) để vượt allowlist.
MC_HOST_HEADER = os.environ.get("MC_HOST_HEADER", "")

# Key MC (header Authorization khi gọi MC). ROTATE sau khi lộ!
MC_API_KEY   = os.environ.get("MC_API_KEY", "")
# Key hermes API server (header Authorization khi gọi hermes). ROTATE sau khi lộ!
HERMES_KEY   = os.environ.get("HERMES_API_KEY", "")

AGENT_NAME   = os.environ.get("AGENT_NAME", "hermes-1")
AGENT_ID     = os.environ.get("AGENT_ID", "1")
HERMES_MODEL = os.environ.get("HERMES_MODEL", "hermes-agent")

POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL", "15"))       # giây
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "60"))  # giây
HERMES_TIMEOUT     = int(os.environ.get("HERMES_TIMEOUT", "300"))     # giây cho 1 turn
# ------------------------------------------------------------------


def log(msg):
    print(f"[bridge] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def http(method, url, token, body=None, timeout=30):
    """Gọi HTTP đơn giản, trả về (status, parsed_json_or_text)."""
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Nếu gọi MC qua tên service nội bộ, cần Host header hợp lệ để qua allowlist.
    if MC_HOST_HEADER and url.startswith(MC_URL):
        headers["Host"] = MC_HOST_HEADER
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return e.code, raw
    except Exception as e:
        return 0, f"ERR {e}"


# ------------------------- MC operations --------------------------
def heartbeat():
    st, _ = http("POST", f"{MC_URL}/api/agents/{AGENT_ID}/heartbeat", MC_API_KEY)
    if st not in (200, 201, 204):
        log(f"heartbeat -> {st}")


def claim_task():
    """Lấy (claim) task ưu tiên cao nhất được giao cho agent. None nếu không có."""
    st, body = http("GET", f"{MC_URL}/api/tasks/queue?agent={AGENT_NAME}", MC_API_KEY)
    if st != 200:
        if st not in (404,):  # 404/empty là bình thường khi hết việc
            log(f"claim_task -> {st}: {body}")
        return None
    # MC có thể trả {"task": {...}} hoặc {...} hoặc {"task": null}
    if isinstance(body, dict):
        task = body.get("task", body)
        if task and isinstance(task, dict) and task.get("id"):
            return task
    return None


def report_result(task_id, text, ok=True):
    """Báo kết quả về MC: comment + chuyển trạng thái.
       MC chưa chốt 1 endpoint chuẩn cho mọi version, nên thử lần lượt."""
    # 1) comment kết quả
    http("POST", f"{MC_URL}/api/tasks/{task_id}/comments", MC_API_KEY,
         {"body": text[:4000]})
    # 2) chuyển trạng thái — thử vài biến thể, cái nào trả 2xx là xong
    status = "done" if ok else "review"
    attempts = [
        ("POST", f"{MC_URL}/api/tasks/{task_id}/complete", {}),
        ("PATCH", f"{MC_URL}/api/tasks/{task_id}", {"status": status}),
        ("POST", f"{MC_URL}/api/tasks/{task_id}/status", {"status": status}),
    ]
    for method, url, payload in attempts:
        st, _ = http(method, url, MC_API_KEY, payload)
        if st in (200, 201, 204):
            log(f"task {task_id} -> {status} (via {method} {url.split('/api/')[1]})")
            return
    log(f"task {task_id}: KHÔNG chuyển được trạng thái — kiểm tra endpoint MC")


# ----------------------- Hermes execution -------------------------
def run_on_hermes(prompt, session_key):
    """Đẩy prompt vào hermes, trả về text kết quả."""
    url = f"{HERMES_URL}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {HERMES_KEY}",
        # giữ ngữ cảnh/memory ổn định theo từng task
        "X-Hermes-Session-Key": f"mc-task-{session_key}",
    }
    body = {
        "model": HERMES_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HERMES_TIMEOUT) as r:
            resp = json.loads(r.read().decode())
        return resp["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        return f"[hermes HTTP {e.code}] {raw[:500]}"
    except Exception as e:
        return f"[hermes error] {e}"


# ----------------------------- main -------------------------------
def main():
    if not MC_API_KEY or not HERMES_KEY:
        log("THIẾU MC_API_KEY hoặc HERMES_API_KEY — set env rồi chạy lại.")
        sys.exit(1)

    log(f"start | MC={MC_URL} HERMES={HERMES_URL} agent={AGENT_NAME}(id={AGENT_ID})")
    last_hb = 0.0
    done_ids = set()  # các task đã xử lý, tránh làm lại nếu MC chưa gỡ khỏi queue

    while True:
        now = time.time()
        if now - last_hb >= HEARTBEAT_INTERVAL:
            heartbeat()
            last_hb = now

        task = claim_task()
        if task:
            tid = task.get("id")
            if tid in done_ids:
                # MC vẫn trả lại task đã xong -> đợi rồi poll lại, KHÔNG xử lý lại
                time.sleep(POLL_INTERVAL)
                continue
            title = task.get("title", "")
            desc  = task.get("description", "")
            prompt = (title + "\n\n" + desc).strip() or title or desc
            log(f"task {tid}: {title!r} -> hermes")
            result = run_on_hermes(prompt, session_key=str(tid))
            log(f"task {tid}: hermes trả {len(result)} ký tự")
            ok = not result.startswith("[hermes")
            report_result(tid, result, ok=ok)
            if ok:
                done_ids.add(tid)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()