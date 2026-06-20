"""Offline tests for Wave 4: deckforge (.pptx), resume-forge (.docx), mailmerge (preview).
Gmail (mailbox + reachout send) needs your credentials."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from fastmcp import Client  # noqa: E402


async def main():
    # deckforge
    sys.path.insert(0, str(ROOT / "servers" / "deckforge"))
    import server as deck  # noqa
    async with Client(deck.mcp) as c:
        r = await c.call_tool("create_presentation", {"title": "Naman Sharma", "template": "career", "theme": "dev_dark"})
        did = r.data["deck_id"]
        await c.call_tool("add_two_column_slide", {"deck_id": did, "title": "Skills",
                          "left": ["Python", "TypeScript"], "right": ["FastMCP", "SQLite"]})
        s = await c.call_tool("save_presentation", {"deck_id": did, "filename": "career_demo.pptx"})
        assert Path(s.data["path"]).exists() and s.data["slides"] >= 7
        print("deckforge OK —", s.data["slides"], "slides ->", s.data["path"])
    del sys.modules["server"]

    # resume-forge (uses profile.json)
    sys.path.insert(0, str(ROOT / "servers" / "resume-forge"))
    import server as rf  # noqa
    async with Client(rf.mcp) as c:
        r = await c.call_tool("build_resume", {"headline": "Backend developer", "filename": "resume_demo.docx"})
        assert Path(r.data["path"]).exists()
        cl = await c.call_tool("cover_letter", {"company": "Nova Robotics", "role": "Intern",
              "body": "I built X.\n\nI'd love to help with Y."})
        assert Path(cl.data["path"]).exists()
        g = await c.call_tool("keyword_gaps", {"jd": "We need Python, Kubernetes, and gRPC experience."})
        print("resume-forge OK — docx built; sample gaps:", g.data["possible_gaps"][:5])
    del sys.modules["server"]

    # mailmerge (uses reachout templates)
    sys.path.insert(0, str(ROOT / "servers" / "mailmerge"))
    import server as mm  # noqa
    async with Client(mm.mcp) as c:
        recips = [{"to_email": "a@x.com", "name": "A", "company": "X", "pitch": "p"},
                  {"to_email": "a@x.com", "name": "A2", "company": "X", "pitch": "p"},  # dup
                  {"name": "B", "company": "Y", "pitch": "p"}]  # missing email
        d = await c.call_tool("dry_run", {"recipients": recips, "template_name": "internship_cto"})
        assert d.data["sendable"] == 1 and d.data["duplicates"] and d.data["missing_email"] == [2]
        p = await c.call_tool("preview", {"recipients": recips[:1], "template_name": "internship_cto",
                                          "common": {"sender": "Naman"}})
        assert "X" in p.data["rendered"][0]["subject"]
        print("mailmerge OK — dry_run flagged dup+missing; preview rendered")

    print("\nWAVE 4 (offline) OK ✅  (mailbox + Gmail send need your credentials)")


asyncio.run(main())
