"""Fake OpenAI-compatible LLM server (stdlib only, offline).

Usage:
    python3 examples/fake_llm_server.py [--host 127.0.0.1] [--port 8765]

    LLM_MODE=llm LLM_BASE_URL=http://127.0.0.1:8765/v1 LLM_API_KEY=test \
        python3 examples/demo.py --mode llm

POST /v1/chat/completions returns hand-written, CLEAN JSON for
examples/sample_meeting.json. The handler dispatches on the prompt
content: prompts mentioning "action_items" (the task extractor) get
the clean task list (CLEAN_TASKS); everything else gets the clean
minutes JSON (CLEAN_MINUTES) — noticeably cleaner than the rule
output: the only risk is the expiring payment certificate (segment 2);
the opening line is NOT treated as a risk. This proves the real HTTP
chain (client -> /chat/completions -> JSON extraction -> validation)
without any real API key or network access.
"""
import argparse, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLEAN_MINUTES = {
    "summary": "上线推迟至11月8日，以完成支付联调。支付渠道证书11月1日过期，李四需在下周三之前完成续期并提交联调。灰度比例尚未确定，王五明天提交灰度方案供评审。",
    "executive_summary": "上线推迟至11月8日，以完成支付联调。支付渠道证书11月1日过期，李四需在下周三之前完成续期并提交联调。灰度比例尚未确定，王五明天提交灰度方案供评审。",
    "detailed_minutes": "## 上线时间\n原定上线时间因支付联调未完成推迟至11月8日，灰度比例待产品确认后确定。\n\n## 支付联调与证书\n支付接口联调还差证书，支付渠道证书11月1日过期。李四负责在下周三之前完成证书续期并提交联调，王五跟进联调测试并在11月1日前提交测试报告。\n\n## 后续安排\n下周二下午召开跟进会，确认证书续期和联调结果。",
    "topics": [
        {"title": "上线时间", "segment_ids": [3, 4], "summary": "上线推迟至11月8日，灰度比例尚未确定。"},
        {"title": "支付联调与证书续期", "segment_ids": [2, 5, 6], "summary": "联调还差证书，李四在下周三之前完成续期并提交联调。"},
        {"title": "后续跟进会", "segment_ids": [9, 10], "summary": "下周二下午召开跟进会，确认证书和联调结果。"},
    ],
    "decisions": [
        {"text": "上线推迟至11月8日", "decided_by": "张三", "segment_ids": [3, 4]},
        {"text": "下周二下午召开跟进会，确认证书和联调结果", "decided_by": "张三", "segment_ids": [10]},
    ],
    "risks": [
        {"text": "支付渠道证书11月1日过期，支付接口联调尚缺证书", "segment_ids": [2], "severity": "high"},
    ],
    "open_questions": ["灰度比例尚未确定，待产品确认"],
}

CLEAN_TASKS = {
    "action_items": [
        {"title": "完成支付渠道证书续期并提交联调", "owner_raw": "李四",
         "due_raw": "下周三之前", "priority": "P1", "source_segment_ids": [5, 6]},
        {"title": "提交灰度方案供评审", "owner_raw": "王五",
         "due_raw": "明天", "priority": "P2", "source_segment_ids": [7]},
        {"title": "完成支付联调测试报告", "owner_raw": "王五",
         "due_raw": "11月1日前", "priority": "P2", "source_segment_ids": [8]},
    ],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stdout clean
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/health") or self.path == "/health":
            self._send_json({"status": "ok", "fake": True})
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json({"error": f"unsupported path {self.path}"}, status=404)
            return
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            req = {}
        # Dispatch on prompt content: the task extractor's prompts name
        # the "action_items" field; the minutes analyst's never do.
        prompt_text = json.dumps(req.get("messages", []), ensure_ascii=False)
        payload = CLEAN_TASKS if "action_items" in prompt_text else CLEAN_MINUTES
        content = json.dumps(payload, ensure_ascii=False)
        self._send_json({
            "id": "chatcmpl-fake-001",
            "object": "chat.completion",
            "created": 0,
            "model": req.get("model", "fake-model"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })


def main() -> None:
    parser = argparse.ArgumentParser(description="fake OpenAI-compatible LLM server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake-llm listening on http://{args.host}:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
