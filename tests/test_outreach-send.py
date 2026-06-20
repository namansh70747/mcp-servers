"""Offline tests for the outreach-send cluster: reachout, campaign, contacts, pitchbuilder,
emailcheck.

No network / Gmail / LibreOffice — only offline logic (Gmail is monkeypatched with a fake
service so draft/send paths exercise the DB + envelope logic without leaving the process).
Uses an isolated MCP_DATA_DIR so it never touches real ~/.mcp-suite data.

Run:
    VIRTUAL_ENV= .venv/bin/python tests/test_outreach-send.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Isolate data + skip dotenv before importing any server.
_TMP = tempfile.mkdtemp(prefix="outreach-send-test-")
os.environ["MCP_DATA_DIR"] = _TMP
os.environ["MCP_NO_DOTENV"] = "1"

# Clean any prior per-server dirs under our isolated root (defensive — tempdir is fresh anyway).
for _name in ("reachout", "campaign", "contacts", "pitchbuilder", "emailcheck", "jobtrack"):
    shutil.rmtree(Path(_TMP) / _name, ignore_errors=True)

from fastmcp import Client  # noqa: E402


async def one(name, fn, *, setup=None):
    """Import a server fresh, optionally run a setup(module) hook (e.g. to monkeypatch Gmail),
    then exercise it via the in-memory client."""
    sys.path.insert(0, str(ROOT / "servers" / name))
    import server  # noqa
    if setup:
        setup(server)
    async with Client(server.mcp) as c:
        await fn(c, server)
    del sys.modules["server"]
    sys.path.pop(0)


# ---------- fake Gmail service ----------
class _FakeDrafts:
    def __init__(self, store):
        self._store = store

    def create(self, userId=None, body=None):
        self._store["n"] += 1
        n = self._store["n"]
        msg = (body or {}).get("message", {})
        self._store["last_create_body"] = body
        return _Exec({"id": f"draft{n}", "message": {"id": f"msg{n}",
                                                     "threadId": msg.get("threadId", f"thread{n}")}})

    def send(self, userId=None, body=None):
        did = (body or {}).get("id", "draftX")
        return _Exec({"id": f"sent-{did}", "threadId": f"thread-{did}"})

    def delete(self, userId=None, id=None):
        self._store["last_deleted"] = id
        return _Exec({})


class _FakeMessages:
    def send(self, userId=None, body=None):
        return _Exec({"id": "sentmsg1", "threadId": "thread1"})


class _FakeUsers:
    def __init__(self, store):
        self._store = store

    def drafts(self):
        return _FakeDrafts(self._store)

    def messages(self):
        return _FakeMessages()


class _FakeService:
    def __init__(self):
        self._store = {"n": 0}

    def users(self):
        return _FakeUsers(self._store)


class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


def _patch_gmail(server):
    server._gmail = lambda: _FakeService()


async def main():
    # ---------- contacts ----------
    async def contacts(c, srv):
        r = await c.call_tool("add_contact", {"name": "Ada Lovelace", "company": "Analytical",
                                              "email": "ada@analytical.io", "role": "CTO"})
        assert r.data["id"]
        bad = await c.call_tool("add_contact", {"name": "X", "company": "Y", "email": "not-an-email"})
        assert "error" in bad.data, bad.data
        # duplicate by email
        await c.call_tool("add_contact", {"name": "Ada L", "company": "Other",
                                          "email": "ada@analytical.io"})
        dups = await c.call_tool("find_duplicates", {})
        assert dups.data["duplicate_groups"] >= 1, dups.data
        # tag roundtrip + untag
        await c.call_tool("tag", {"contact_id": r.data["id"], "tags": "vip, founder"})
        got = await c.call_tool("get", {"contact_id": r.data["id"]})
        assert "vip" in got.data["tags"]
        assert got.data["domain"] == "analytical.io"
        ut = await c.call_tool("untag", {"contact_id": r.data["id"], "tags": "vip"})
        assert "vip" not in ut.data["tags"] and "founder" in ut.data["tags"]
        # enrich from signature
        en = await c.call_tool("enrich_from_signature",
                               {"contact_id": r.data["id"],
                                "signature": "Ada\nhttps://github.com/ada\n+1 415 555 1234"})
        assert en.data["found"]["github"].endswith("ada")
        # status validation
        bs = await c.call_tool("update_status", {"contact_id": r.data["id"], "status": "bogus"})
        assert "error" in bs.data
        ok = await c.call_tool("update_status", {"contact_id": r.data["id"], "status": "queued"})
        assert ok.data["status"] == "queued"
        # csv export/import roundtrip
        exp = await c.call_tool("export_csv", {})
        assert exp.data["count"] >= 1
        imp = await c.call_tool("import_csv", {"path": exp.data["path"]})
        assert imp.data["updated"] >= 1, imp.data
        # missing file + oversized file guard
        mi = await c.call_tool("import_csv", {"path": str(Path(_TMP) / "does_not_exist.csv")})
        assert "error" in mi.data
        big = Path(_TMP) / "big.csv"
        big.write_text("name,company\n")
        os.truncate(big, srv.MAX_IMPORT_BYTES + 1)
        mb = await c.call_tool("import_csv", {"path": str(big)})
        assert "too large" in mb.data.get("error", ""), mb.data
        # dedupe dry-run
        dd = await c.call_tool("dedupe", {"dry_run": True})
        assert dd.data["dry_run"] and "plan" in dd.data
        # jobtrack payload + stats
        jp = await c.call_tool("link_jobtrack_payload", {"contact_id": r.data["id"]})
        assert jp.data["company"] == "Analytical"
        st = await c.call_tool("stats", {})
        assert st.data["total"] >= 2
        print("contacts OK — validation, dedupe, tags, enrich, status, csv roundtrip + caps, stats")
    await one("contacts", contacts)

    # ---------- campaign ----------
    async def campaign(c, srv):
        f = await c.call_tool("filter_uncontacted",
                              {"companies": [{"company": "Acme", "domain": "acme.com"}]})
        assert f.data["fresh_count"] == 1
        await c.call_tool("record_outreach", {"company": "Acme", "domain": "acme.com",
                                              "email": "cto@acme.com", "cooldown_days": 30})
        f2 = await c.call_tool("filter_uncontacted",
                               {"companies": [{"company": "Acme", "domain": "acme.com"}]})
        assert f2.data["fresh_count"] == 0 and f2.data["skipped"][0]["reason"] == "cooldown", f2.data
        cc = await c.call_tool("contact_cooldown", {"email": "cto@acme.com"})
        assert cc.data["times_contacted"] == 1
        # is_contacted + suppression
        ic = await c.call_tool("is_contacted", {"company": "Acme"})
        assert ic.data["contacted"]
        bad_kind = await c.call_tool("add_suppression", {"value": "x.com", "kind": "bogus"})
        assert "error" in bad_kind.data
        empty = await c.call_tool("add_suppression", {"value": "", "kind": "domain"})
        assert "error" in empty.data
        # suppression bulk + remove + suppressed-gate in filter
        await c.call_tool("bulk_add_suppression", {"values": ["spam.com", "no.com"], "kind": "domain"})
        fs = await c.call_tool("filter_uncontacted",
                               {"companies": [{"company": "Spammy", "domain": "spam.com"}]})
        assert fs.data["skipped"][0]["reason"] == "suppressed", fs.data
        rm = await c.call_tool("remove_suppression", {"value": "spam.com"})
        assert rm.data["removed"]
        ls = await c.call_tool("list_suppressions", {})
        assert any(s["value"] == "no.com" for s in ls.data)
        # csv roundtrip + oversize guard
        exp = await c.call_tool("export_suppression_csv", {})
        imp = await c.call_tool("import_suppression_csv", {"path": exp.data["path"]})
        assert imp.data["submitted"] >= 1
        big = Path(_TMP) / "supp_big.csv"
        big.write_text("value\n")
        os.truncate(big, srv.MAX_IMPORT_BYTES + 1)
        mb = await c.call_tool("import_suppression_csv", {"path": str(big)})
        assert "too large" in mb.data.get("error", ""), mb.data
        # run bookkeeping + analytics
        await c.call_tool("record_run", {"note": "test run"})
        nd = await c.call_tool("next_run_due", {"interval_days": 7})
        assert "due" in nd.data
        an = await c.call_tool("analytics", {})
        assert an.data["companies_contacted"] >= 1
        # P2 linkage: record_outreach accepts + surfaces gmail_message_id/thread_id
        ro = await c.call_tool("record_outreach",
                               {"company": "Linked", "domain": "linked.io",
                                "email": "ceo@linked.io", "gmail_message_id": "msg-abc",
                                "thread_id": "thr-xyz"})
        assert ro.data["gmail_message_id"] == "msg-abc" and ro.data["thread_id"] == "thr-xyz", ro.data
        icl = await c.call_tool("is_contacted", {"company": "Linked"})
        assert icl.data["gmail_message_id"] == "msg-abc" and icl.data["thread_id"] == "thr-xyz", icl.data
        rec = await c.call_tool("recent_outreach", {"days": 7})
        assert any(r.get("gmail_message_id") == "msg-abc" for r in rec.data), rec.data
        # old behavior preserved: record_outreach without ids -> None message/thread
        ro2 = await c.call_tool("record_outreach", {"company": "NoLink", "domain": "nolink.io"})
        assert ro2.data["gmail_message_id"] is None and ro2.data["thread_id"] is None, ro2.data
        print("campaign OK — cooldown gate, suppression gate+csv+caps, runs, analytics, msg-id linkage")
    await one("campaign", campaign)

    # ---------- jobtrack (P2 contact linkage) ----------
    async def jobtrack(c, srv):
        a = await c.call_tool("add_application",
                              {"company": "Hooli", "role": "Engineer", "contact_id": 42})
        aid = a.data["id"]
        # old behavior preserved: contact_id is optional
        a2 = await c.call_tool("add_application", {"company": "Pied Piper", "role": "Founder"})
        assert a2.data["id"] and a2.data["status"] == "applied"
        # for_contact returns only the linked application
        fc = await c.call_tool("for_contact", {"contact_id": 42})
        assert any(r["id"] == aid for r in fc.data) and all(r["company"] == "Hooli" for r in fc.data), fc.data
        # archive (soft) keeps the row + records archived_at
        arch = await c.call_tool("archive_application", {"application_id": aid})
        assert arch.data["ok"] and arch.data["archived_at"]
        got = await c.call_tool("get_application", {"application_id": aid})
        assert got.data.get("archived_at"), got.data
        assert got.data.get("contact_id") == 42, got.data
        # delete (hard) removes it; second delete errors
        dele = await c.call_tool("delete_application", {"application_id": a2.data["id"]})
        assert dele.data["deleted"]
        gone = await c.call_tool("delete_application", {"application_id": a2.data["id"]})
        assert "error" in gone.data
        print("jobtrack OK — contact_id linkage, for_contact, archive (soft), delete (hard)")
    await one("jobtrack", jobtrack)

    # ---------- pitchbuilder ----------
    async def pitch(c, srv):
        await c.call_tool("save_pitch", {"company": "Globex", "pitch": "A real-time dashboard.",
                                         "angle": "recent_funding", "tags": "fintech"})
        gp = await c.call_tool("get_pitch", {"company": "Globex"})
        assert gp.data["pitch"].startswith("A real-time")
        lp = await c.call_tool("list_pitches", {"tag": "fintech"})
        assert any(p["company"] == "Globex" for p in lp.data)
        # variants
        await c.call_tool("save_variant", {"company": "Globex", "variant": "short", "pitch": "Tiny."})
        vs = await c.call_tool("list_variants", {"company": "Globex"})
        assert any(v["variant"] == "short" for v in vs.data)
        gv = await c.call_tool("get_variant", {"company": "Globex", "variant": "short"})
        assert gv.data["pitch"] == "Tiny."
        # template library + render + render_into_template
        await c.call_tool("save_template", {"name": "intro", "subject": "Hi {{ company }}",
                                            "body": "Idea for {{ company }}: {{ pitch }}"})
        rt = await c.call_tool("render_template", {"name": "intro",
                                                   "context": {"company": "Globex", "pitch": "X"}})
        assert rt.data["subject"] == "Hi Globex" and "Idea for Globex" in rt.data["body"]
        rit = await c.call_tool("render_into_template",
                                {"template": "Subject: Hey {{ name }}\n\nBody {{ company }}",
                                 "context": {"name": "Sam", "company": "Globex"}})
        assert rit.data["subject"] == "Hey Sam" and "Globex" in rit.data["body"]
        miss = await c.call_tool("render_template", {"name": "nope", "context": {}})
        assert "error" in miss.data
        # delete template
        dl = await c.call_tool("delete_template", {"name": "intro"})
        assert dl.data["deleted"]
        # angles
        ang = await c.call_tool("suggest_angles", {"company": "Globex"})
        assert len(ang.data["angles"]) >= 4
        # onepager + graceful pdf
        op = await c.call_tool("make_onepager", {"company": "Globex",
                                                 "sections": {"problem": "p", "solution": "s"}})
        assert Path(op.data["path"]).exists()
        # path-traversal guard on filename: a '../' name must collapse to a basename inside OUT
        trav = await c.call_tool("make_onepager", {"company": "Evil",
                                                   "sections": {"x": "y"},
                                                   "filename": "../escape.md"})
        tp = Path(trav.data["path"]).resolve()
        assert tp.parent == Path(srv.OUT).resolve(), tp
        assert not (Path(srv.OUT).resolve().parent / "escape.md").exists()
        lo = await c.call_tool("list_onepagers", {})
        assert any(o["name"].endswith(".md") for o in lo.data)
        pdf = await c.call_tool("onepager_to_pdf", {"md_path": op.data["path"]})
        assert "docx" in pdf.data or pdf.data.get("ok"), pdf.data  # docx always written
        nf = await c.call_tool("onepager_to_pdf", {"md_path": str(Path(_TMP) / "nope.md")})
        assert "error" in nf.data
        print("pitchbuilder OK — pitch, variants, templates, angles, onepager + traversal guard, pdf")
    await one("pitchbuilder", pitch)

    # ---------- emailcheck (fully offline) ----------
    async def emailcheck(c, srv):
        v = await c.call_tool("validate_email", {"email": "  Ada@Example.com "})
        assert v.data["valid"] and v.data["domain"] == "example.com"
        iv = await c.call_tool("validate_email", {"email": "nope@@bad"})
        assert iv.data["valid"] is False
        ex = await c.call_tool("extract_emails",
                               {"text": "ping a@b.com or A@B.COM and z@q.io"})
        assert ex.data["count"] == 2, ex.data  # case-insensitive dedupe
        un = await c.call_tool("parse_unsubscribe",
                               {"content": "List-Unsubscribe: <mailto:off@x.com>\n"
                                           "<a href='https://x.com/unsubscribe'>off</a>"})
        assert un.data["has_unsubscribe"] and un.data["list_unsubscribe_header"]
        clean = await c.call_tool("spam_score", {"subject": "Quick question about your API",
                                                 "body": "Hi, I built a small tool. Worth a look?"})
        assert clean.data["risk"] == "low", clean.data
        spammy = await c.call_tool("spam_score",
                                   {"subject": "ACT NOW!!! FREE CASH WINNER",
                                    "body": "100% guaranteed, click here, buy now, no obligation $$$"})
        assert spammy.data["risk"] in ("medium", "high") and spammy.data["score"] > clean.data["score"]
        empty = await c.call_tool("spam_score", {"subject": "", "body": "x"})
        assert any("empty subject" in r for r in empty.data["reasons"])
        print("emailcheck OK — validate, extract, unsubscribe, spam scoring")
    await one("emailcheck", emailcheck)

    # ---------- reachout (Gmail monkeypatched) ----------
    async def reachout(c, srv):
        ts = await c.call_tool("list_templates", {})
        assert any(t["name"] == "internship_cto" for t in ts.data)
        rw = await c.call_tool("render_with_profile",
                               {"template_name": "internship_cto",
                                "variables": {"name": "Sam", "company": "Hooli", "pitch": "Build X"}})
        assert "Hooli" in rw.data["body"] and "missing" in rw.data
        # template-name traversal guard
        bt = await c.call_tool("render_template", {"template_name": "../secret", "variables": {}})
        assert "error" in bt.data
        # signature + auth status (offline)
        sig = await c.call_tool("signature_block", {})
        assert "signature" in sig.data
        au = await c.call_tool("auth_status", {})
        assert "credentials_present" in au.data and "token" not in str(au.data).lower().replace("token_present", "")
        # A/B determinism
        a1 = await c.call_tool("ab_pick", {"template_a": "t1", "template_b": "t2", "key": "x@y.com"})
        a2 = await c.call_tool("ab_pick", {"template_a": "t1", "template_b": "t2", "key": "x@y.com"})
        assert a1.data == a2.data
        # dry-run send (no Gmail) + invalid email
        dr = await c.call_tool("send_email", {"to_email": "p@q.com", "subject": "Hi", "body": "Yo",
                                              "dry_run": True})
        assert dr.data["dry_run"] and dr.data["to"] == "p@q.com"
        bad = await c.call_tool("send_email", {"to_email": "bad", "subject": "s", "body": "b",
                                               "dry_run": True})
        assert "error" in bad.data
        # create_draft via fake Gmail, then send_draft
        cd = await c.call_tool("create_draft", {"to_email": "lead@hooli.com", "subject": "Hi",
                                                "body": "Yo", "company": "Hooli", "variant": "A",
                                                "contact_id": 7})
        assert cd.data["draft_id"] and cd.data["outreach_id"]
        sd = await c.call_tool("send_draft", {"draft_id": cd.data["draft_id"]})
        assert sd.data["status"] == "sent"
        # send_draft id guard: empty draft_id -> clean error (no Gmail call)
        sde = await c.call_tool("send_draft", {"draft_id": ""})
        assert "error" in sde.data, sde.data
        # P2 linkage: history_for_contact returns the linked outreach row
        hist = await c.call_tool("history_for_contact", {"contact_id": 7})
        assert any(h["id"] == cd.data["outreach_id"] for h in hist.data), hist.data
        # delete_draft: empty id guard, then a real delete marking the tracked row
        dde = await c.call_tool("delete_draft", {"draft_id": ""})
        assert "error" in dde.data
        cd2 = await c.call_tool("create_draft", {"to_email": "two@hooli.com", "subject": "Hi2",
                                                 "body": "Yo2", "company": "Hooli"})
        dd = await c.call_tool("delete_draft", {"draft_id": cd2.data["draft_id"],
                                                "outreach_id": cd2.data["outreach_id"]})
        assert dd.data["ok"] and dd.data["status"] == "deleted"
        delrow = srv.store.query_one("SELECT status FROM outreach WHERE id=?",
                                     (cd2.data["outreach_id"],))
        assert delrow["status"] == "deleted", delrow
        # attachment size cap (oversized file rejected during build) -> tool surfaces error
        big = Path(_TMP) / "huge.bin"
        big.write_bytes(b"x")
        os.truncate(big, srv.MAX_ATTACH_BYTES + 1)
        from fastmcp.exceptions import ToolError
        blocked = False
        try:
            await c.call_tool("create_draft", {"to_email": "lead@hooli.com", "subject": "s",
                                               "body": "b", "attachments": [str(big)]})
        except ToolError as e:
            blocked = "too large" in str(e)
        assert blocked, "oversized attachment should block the draft"
        # logged outreach feeds followups + analytics + snooze
        lo = await c.call_tool("log_outreach", {"recipient_name": "Pat",
                                                "recipient_email": "pat@hooli.com",
                                                "company": "Hooli", "status": "sent",
                                                "template_used": "internship_cto"})
        oid = lo.data["outreach_id"]
        srv.store.execute("UPDATE outreach SET sent_at=? WHERE id=?",
                          ("2020-01-01T00:00:00+00:00", oid))
        due = await c.call_tool("list_followups_due", {"follow_up_days": 1})
        assert any(d["id"] == oid for d in due.data) and due.data[0]["days_since"] is not None
        # record followup + mark replied
        rf = await c.call_tool("record_followup", {"outreach_id": oid})
        assert rf.data["followup_count"] == 1
        await c.call_tool("snooze", {"outreach_id": oid, "days": 30})
        due2 = await c.call_tool("list_followups_due", {"follow_up_days": 1})
        assert not any(d["id"] == oid for d in due2.data)
        await c.call_tool("mark_replied", {"outreach_id": oid})
        mr = await c.call_tool("mark_status", {"outreach_id": oid, "status": "closed", "notes": "done"})
        assert mr.data["status"] == "closed"
        # sequences: define, start (drafts via fake Gmail), advance, status
        await c.call_tool("define_sequence",
                          {"name": "warm",
                           "steps": [{"template": "internship_cto", "wait_days": 3},
                                     {"template": "followup_generic", "wait_days": 5}]})
        seqs = await c.call_tool("list_sequences", {})
        assert any(s["name"] == "warm" and s["steps"] == 2 for s in seqs.data)
        gs = await c.call_tool("get_sequence", {"name": "warm"})
        assert len(gs.data["steps"]) == 2
        ss = await c.call_tool("start_sequence", {"name": "warm", "to_email": "seq@hooli.com",
                                                  "recipient_name": "Seq", "company": "Hooli"})
        assert ss.data["ok"] and ss.data["draft"]["outreach_id"]
        seq_oid = ss.data["draft"]["outreach_id"]
        sst = await c.call_tool("sequence_status", {"outreach_id": seq_oid})
        assert sst.data["in_sequence"] and sst.data["total_steps"] == 2
        adv = await c.call_tool("advance_sequence", {"outreach_id": seq_oid})
        assert adv.data.get("advanced_to_step") == 2, adv.data
        # bad sequence (missing template) returns clean error, not a crash
        await c.call_tool("define_sequence", {"name": "broken", "steps": [{"wait_days": 1}]})
        sb = await c.call_tool("start_sequence", {"name": "broken", "to_email": "b@h.com"})
        assert "error" in sb.data
        # thread_followup (fake Gmail)
        tf = await c.call_tool("thread_followup", {"outreach_id": cd.data["outreach_id"],
                                                   "template_name": "followup_generic"})
        assert tf.data["draft_id"] and tf.data["subject"].lower().startswith("re:")
        # scheduling queue
        sch = await c.call_tool("schedule_send",
                                {"outreach_id": oid, "run_at": "2020-01-01T00:00:00+00:00"})
        ds = await c.call_tool("due_scheduled", {})
        assert any(x["id"] == sch.data["scheduled_id"] for x in ds.data)
        md = await c.call_tool("mark_scheduled_done", {"scheduled_id": sch.data["scheduled_id"]})
        assert md.data["ok"]
        # a fresh sent record so the funnel has a counted send
        await c.call_tool("log_outreach", {"recipient_name": "Sent", "recipient_email": "sent@hooli.com",
                                           "company": "Hooli", "status": "sent",
                                           "template_used": "internship_cto"})
        # A/B report + analytics + export
        abr = await c.call_tool("ab_report", {})
        assert "variants" in abr.data
        an = await c.call_tool("analytics", {})
        assert an.data["drafted"] >= 1 and an.data["sent"] >= 1, an.data
        exp = await c.call_tool("export_csv", {})
        assert exp.data["count"] >= 1
        print("reachout OK — render+guards, drafts/send (mock), followups, sequences, threads, schedule")
    await one("reachout", reachout, setup=_patch_gmail)

    print("\nALL outreach-send OFFLINE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
