"""Offline smoke tests for the email cluster: mailbox, mailmerge, emailcheck.
No network / no Gmail credentials required — only offline logic + tool registration is exercised."""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MCP_DATA_DIR", tempfile.mkdtemp(prefix="mcp-email-test-"))
os.environ.setdefault("MCP_NO_DOTENV", "1")

# Start from a clean slate for every email-cluster server's data dir.
_SUITE = Path(os.environ["MCP_DATA_DIR"])
for _name in ("mailbox", "mailmerge", "emailcheck"):
    shutil.rmtree(_SUITE / _name, ignore_errors=True)

from fastmcp import Client  # noqa: E402


async def one(name, fn):
    sys.path.insert(0, str(ROOT / "servers" / name))
    import server  # noqa
    async with Client(server.mcp) as c:
        await fn(c, server)
    del sys.modules["server"]
    sys.path.pop(0)


async def main():
    # ---------- mailbox (offline helpers + tool registration) ----------
    async def mb(c, server):
        tools = {t.name for t in await c.list_tools()}
        for t in ("list_unread", "needs_reply", "summarize_thread", "draft_reply", "categorize",
                  "search", "list_labels", "create_label", "apply_label", "mark_read", "archive",
                  "star", "trash", "get_message", "download_attachment", "batch_modify",
                  "summarize_inbox", "create_filter", "list_filters", "thread_actions"):
            assert t in tools, f"mailbox missing tool {t}"
        # pure helpers
        a = server._parse_addr("Jane Doe <jane@example.com>")
        assert a == {"name": "Jane Doe", "email": "jane@example.com"}, a
        acts = server._extract_actions("Hi. Can you send the deck by EOD? Thanks. The sky is blue.")
        assert any("deck" in x.lower() for x in acts), acts
        assert "hello" in server._strip_html("<p>hello</p>").lower()
        meta = server._attachment_meta({"parts": [{"filename": "a.pdf",
            "mimeType": "application/pdf", "body": {"attachmentId": "x1", "size": 10}}]})
        assert meta and meta[0]["filename"] == "a.pdf", meta
        # security: filename sanitizer defeats path traversal + absolute paths
        assert server._safe_filename("../../etc/passwd", "fb") == "passwd"
        assert server._safe_filename("/abs/secret.txt", "fb") == "secret.txt"
        assert server._safe_filename("..\\..\\win.ini", "fb") == "win.ini"
        assert server._safe_filename("", "fb") == "fb"
        assert server._safe_filename("..", "fb") == "fb"
        assert server._safe_filename("ok\x00name.bin", "fb") == "okname.bin"
        # input validation: empty required IDs return errors (no network touched)
        d = await c.call_tool("download_attachment", {"message_id": "", "attachment_id": ""})
        assert "error" in d.data, d.data
        dr = await c.call_tool("draft_reply", {"thread_id": "", "to_email": "",
                                               "subject": "s", "body": "b"})
        assert "error" in dr.data, dr.data
        print(f"mailbox OK — {len(tools)} tools; helpers + sanitizer + input validation")
    await one("mailbox", mb)

    # ---------- mailmerge (full offline pipeline) ----------
    async def mm(c, server):
        tmpl_dir = ROOT / "servers" / "reachout" / "templates"
        tmpl_dir.mkdir(parents=True, exist_ok=True)
        tf = tmpl_dir / "_emailtest.md"
        created = not tf.exists()
        if created:
            tf.write_text("Subject: Hi {{ name }}\n\nHello {{ name }} at {{ company }}!\n")
        try:
            recips = [
                {"to_email": "a@example.com", "name": "Ann", "company": "Acme"},
                {"to_email": "a@example.com", "name": "Ann", "company": "Acme"},  # dup
                {"to_email": "bad-email", "name": "Bob", "company": "Beta"},
                {"name": "Carl"},  # missing email
            ]
            pv = await c.call_tool("preview", {"recipients": recips, "template_name": "_emailtest"})
            assert pv.data["count"] == 4 and any(r["valid_email"] for r in pv.data["rendered"])
            dr = await c.call_tool("dry_run", {"recipients": recips, "template_name": "_emailtest"})
            assert dr.data["sendable"] == 1, dr.data
            assert "bad-email" in dr.data["invalid_email"], dr.data

            vr = await c.call_tool("validate_recipients",
                                   {"recipients": recips, "required_vars": ["company"]})
            assert vr.data["duplicates"] == 1 and vr.data["invalid"] >= 1, vr.data

            csv_text = "email,name,company\nx@y.com,Xen,Xy\nz@w.com,Zoe,Zw\n"
            imp = await c.call_tool("import_csv", {"source": csv_text, "is_path": False})
            assert imp.data["count"] == 2 and imp.data["recipients"][0]["to_email"] == "x@y.com"

            dd = await c.call_tool("dedupe", {"recipients": recips,
                                              "against_emails": ["a@example.com"]})
            assert "a@example.com" in dd.data["removed_already_contacted"], dd.data

            tp = await c.call_tool("throttle_plan", {"count": 45, "daily_cap": 20})
            assert tp.data["days_needed"] == 3 and tp.data["schedule"], tp.data

            rb = await c.call_tool("render_batch", {"recipients": recips, "template_name": "_emailtest"})
            # both valid (incl. the duplicate) render; invalid + missing are skipped
            assert rb.data["ready"] == 2 and len(rb.data["skipped"]) == 2, rb.data

            pc = await c.call_tool("personalize_check",
                                   {"recipients": recips, "template_name": "_emailtest"})
            assert "distinct_bodies" in pc.data, pc.data

            ex = await c.call_tool("export_preview",
                                   {"recipients": recips[:2], "template_name": "_emailtest", "fmt": "md"})
            assert Path(ex.data["path"]).exists(), ex.data

            # security: HTML export escapes recipient-controlled content (no raw <script>)
            xss = [{"to_email": "x@y.com", "name": "<script>alert(1)</script>", "company": "C"}]
            exh = await c.call_tool("export_preview",
                                    {"recipients": xss, "template_name": "_emailtest", "fmt": "html"})
            html_out = Path(exh.data["path"]).read_text()
            assert "<script>alert(1)</script>" not in html_out, "XSS not escaped"
            assert "&lt;script&gt;" in html_out, html_out

            # edge: missing template name returns error + available list
            no_t = await c.call_tool("preview",
                                     {"recipients": recips, "template_name": "__does_not_exist__"})
            assert "error" in no_t.data and "available" in no_t.data, no_t.data

            # edge: import_csv input guards
            badp = await c.call_tool("import_csv", {"source": "/no/such/file.csv", "is_path": True})
            assert "error" in badp.data, badp.data
            nohdr = await c.call_tool("import_csv", {"source": "", "is_path": False})
            assert "error" in nohdr.data, nohdr.data

            adv = await c.call_tool("campaign_advice", {"sendable": 30})
            assert adv.data["estimated_days"] == 2, adv.data
            print("mailmerge OK — preview/dry_run/validate/import_csv/dedupe/throttle/render/"
                  "personalize/export/advice")
        finally:
            if created:
                tf.unlink()
    await one("mailmerge", mm)

    # ---------- emailcheck (offline tools) ----------
    async def ec(c, server):
        tools = {t.name for t in await c.list_tools()}
        for t in ("validate_email", "extract_emails", "parse_unsubscribe", "spam_score",
                  "check_mx", "domain_report"):
            assert t in tools, f"emailcheck missing tool {t}"
        v = await c.call_tool("validate_email", {"email": "Test@Example.com"})
        assert v.data["valid"] and v.data["domain"] == "example.com", v.data
        bad = await c.call_tool("validate_email", {"email": "nope"})
        assert not bad.data["valid"], bad.data
        ext = await c.call_tool("extract_emails",
                                {"text": "ping a@b.com or a@b.com and c@d.org"})
        assert ext.data["count"] == 2, ext.data
        ss = await c.call_tool("spam_score",
                               {"subject": "FREE MONEY ACT NOW!!!", "body": "CLICK HERE to WIN $$$"})
        assert ss.data["risk"] in ("medium", "high") and ss.data["score"] > 25, ss.data
        clean = await c.call_tool("spam_score",
                                  {"subject": "Quick question about your team",
                                   "body": "Hi Ann, would you have 15 minutes next week to chat?"})
        assert clean.data["risk"] == "low", clean.data
        un = await c.call_tool("parse_unsubscribe",
                               {"content": '<a href="https://x.com/unsubscribe">opt out</a>'})
        assert un.data["has_unsubscribe"], un.data
        print(f"emailcheck OK — {len(tools)} tools; validate/extract/spam/unsubscribe")
    await one("emailcheck", ec)

    print("\nALL EMAIL CLUSTER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
