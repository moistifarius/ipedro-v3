"""The Dale Gribble GIF library: tags, random pick, dedupe, file_id upgrade."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ipedro import dale_gifs as dg


class _FakeDB:
    """In-memory stand-in for the dale_gifs table."""

    _COLS = ("id", "tags", "file_id", "file_unique_id", "url", "send_count")

    def __init__(self):
        self.rows: list[dict] = []
        self._next_id = 1

    def _cols(self, r: dict) -> dict:
        return {k: r.get(k) for k in self._COLS}

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO dale_gifs"):
            tags, file_id, file_unique_id, url, added_by = args
            # Mirror the UNIQUE(file_unique_id) upsert...
            if file_unique_id:
                for r in self.rows:
                    if r.get("file_unique_id") == file_unique_id:
                        merged = list(r["tags"])
                        merged += [t for t in tags if t not in merged]
                        r["tags"] = merged
                        r["file_id"] = file_id
                        return {"id": r["id"], "inserted": False}
            # ...and UNIQUE(url) DO NOTHING.
            if url and any(r.get("url") == url for r in self.rows):
                return None
            row = {"id": self._next_id, "tags": list(tags), "file_id": file_id,
                   "file_unique_id": file_unique_id, "url": url,
                   "added_by": added_by, "send_count": 0}
            self._next_id += 1
            self.rows.append(row)
            return {"id": row["id"], "inserted": True}
        if "$1 = ANY(tags)" in q and "ORDER BY random()" in q:
            hits = [r for r in self.rows if args[0] in r["tags"]]
            return self._cols(hits[0]) if hits else None
        if "ORDER BY random()" in q:
            return self._cols(self.rows[0]) if self.rows else None
        return None

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        if "$1 = ANY(tags)" in q:
            return [self._cols(r) for r in self.rows if args[0] in r["tags"]]
        return [self._cols(r) for r in self.rows]

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("DELETE FROM dale_gifs"):
            before = len(self.rows)
            self.rows = [r for r in self.rows if r["id"] != args[0]]
            return f"DELETE {before - len(self.rows)}"
        if q.startswith("UPDATE dale_gifs"):
            for r in self.rows:
                if r["id"] == args[0]:
                    r["send_count"] += 1
                    if len(args) > 1:
                        r["file_id"] = args[1]
                        r["url"] = None
            return "UPDATE 1"
        return "OK"


# ── tag normalization ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("PocketSand", "pocketsand"),
    ("  conspiracy  ", "conspiracy"),
    ("pocket-sand", "pocketsand"),
    ("sh-sha!", "shsha"),
    ("", ""),
    ("!!!", ""),
])
def test_normalize_tag(raw, expect):
    assert dg.normalize_tag(raw) == expect


def test_parse_tags_dedupes_and_drops_junk():
    assert dg.parse_tags("Paranoia  paranoia  pocket-sand  !!!") == [
        "paranoia", "pocketsand"]
    assert dg.parse_tags("") == []


# ── add / dedupe / retag ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_returns_new_id():
    db = _FakeDB()
    gif_id, was_new = await dg.add(
        db, ["pocketsand"], file_id="AAA", file_unique_id="U1")
    assert gif_id is not None and was_new is True
    assert db.rows[0]["tags"] == ["pocketsand"]


@pytest.mark.asyncio
async def test_resending_the_same_gif_retags_it_rather_than_duplicating():
    """Identity is file_unique_id, so the same GIF sent again with new tags
    just gains those tags — the natural way to say 'this works for X too'."""
    db = _FakeDB()
    first, _ = await dg.add(db, ["pocketsand"], file_id="AAA", file_unique_id="U1")
    again, was_new = await dg.add(
        db, ["paranoia", "pocketsand"], file_id="AAA", file_unique_id="U1")

    assert again == first and was_new is False
    assert len(db.rows) == 1
    assert db.rows[0]["tags"] == ["pocketsand", "paranoia"]


@pytest.mark.asyncio
async def test_a_reissued_file_id_still_matches_on_file_unique_id():
    """file_id is per-bot and can be re-issued; file_unique_id is stable."""
    db = _FakeDB()
    await dg.add(db, ["pocketsand"], file_id="OLD", file_unique_id="U1")
    gif_id, was_new = await dg.add(
        db, ["pocketsand"], file_id="NEW", file_unique_id="U1")
    assert was_new is False and len(db.rows) == 1
    assert db.rows[0]["file_id"] == "NEW"      # refreshed, not duplicated


@pytest.mark.asyncio
async def test_seeding_the_same_url_twice_is_a_noop():
    db = _FakeDB()
    url = "https://media.tenor.com/x.gif"
    first, was_new = await dg.add(db, ["pocketsand"], url=url)
    assert was_new is True
    again, was_new_2 = await dg.add(db, ["pocketsand"], url=url)
    assert again is None and was_new_2 is False
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_add_requires_tags_and_a_source():
    db = _FakeDB()
    assert await dg.add(db, [], file_id="AAA") == (None, False)
    assert await dg.add(db, ["!!!"], file_id="AAA") == (None, False)
    assert await dg.add(db, ["pocketsand"]) == (None, False)
    assert db.rows == []


@pytest.mark.asyncio
async def test_add_normalizes_tags():
    db = _FakeDB()
    await dg.add(db, ["Pocket-Sand", "PARANOIA"], file_id="A", file_unique_id="U")
    assert db.rows[0]["tags"] == ["pocketsand", "paranoia"]


# ── random pick ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_random_for_tag_prefers_the_tag():
    db = _FakeDB()
    await dg.add(db, ["conspiracy"], file_id="CON", file_unique_id="U1")
    await dg.add(db, ["pocketsand"], file_id="SAND", file_unique_id="U2")
    got = await dg.random_for_tag(db, "pocketsand")
    assert got is not None and got.file_id == "SAND"


@pytest.mark.asyncio
async def test_random_for_tag_falls_back_to_any_gif():
    """An unfilled bucket must still produce a GIF, not silence."""
    db = _FakeDB()
    await dg.add(db, ["conspiracy"], file_id="CON", file_unique_id="U1")
    got = await dg.random_for_tag(db, "propane")
    assert got is not None and got.file_id == "CON"


@pytest.mark.asyncio
async def test_random_for_tag_on_empty_library_is_none():
    assert await dg.random_for_tag(_FakeDB(), "anything") is None


# ── listing / removal ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_and_remove():
    db = _FakeDB()
    await dg.add(db, ["pocketsand"], file_id="A", file_unique_id="U1")
    await dg.add(db, ["pocketsand", "paranoia"], file_id="B", file_unique_id="U2")
    await dg.add(db, ["conspiracy"], file_id="C", file_unique_id="U3")

    assert len(await dg.list_all(db)) == 3
    assert len(await dg.list_all(db, "pocketsand")) == 2
    assert len(await dg.list_all(db, "paranoia")) == 1

    gif_id = (await dg.list_all(db, "conspiracy"))[0].id
    assert await dg.remove(db, gif_id) is True
    assert await dg.remove(db, gif_id) is False     # already gone
    assert len(await dg.list_all(db)) == 2


# ── the file_id self-upgrade ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_note_sent_counts_without_a_file_id():
    db = _FakeDB()
    await dg.add(db, ["pocketsand"], file_id="AAA", file_unique_id="U1")
    gif = (await dg.list_all(db))[0]
    await dg.note_sent(db, gif.id)
    assert db.rows[0]["send_count"] == 1
    assert db.rows[0]["file_id"] == "AAA"


@pytest.mark.asyncio
async def test_note_sent_upgrades_a_url_row_to_a_file_id():
    """A seeded URL row becomes a permanent file_id after one send, so the
    library stops depending on the original host."""
    db = _FakeDB()
    await dg.add(db, ["pocketsand"], url="https://media.tenor.com/x.gif")
    gif = (await dg.list_all(db))[0]
    assert gif.file_id is None

    await dg.note_sent(db, gif.id, file_id="TELEGRAM123")

    upgraded = (await dg.list_all(db))[0]
    assert upgraded.file_id == "TELEGRAM123"
    assert upgraded.url is None                 # no longer needed
    assert upgraded.send_count == 1


# ── sending ──────────────────────────────────────────────────────────────────

def _msg(*, animation_file_id: str | None = "NEWID"):
    """Fake Message. reply_animation returns a Message carrying an animation —
    that's where Telegram hands us the reusable file_id."""
    animation = (SimpleNamespace(file_id=animation_file_id)
                 if animation_file_id else None)
    return SimpleNamespace(
        chat=SimpleNamespace(id=42),
        reply=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        reply_animation=AsyncMock(return_value=SimpleNamespace(
            message_id=2, animation=animation)),
    )


@pytest.mark.asyncio
async def test_send_uses_the_file_id_directly_no_download(monkeypatch):
    """A row that already knows its file_id must never hit the network."""
    monkeypatch.setattr(dg, "_download", AsyncMock(
        side_effect=AssertionError("should not download")))
    db = _FakeDB()
    await dg.add(db, ["pocketsand"], file_id="AAA", file_unique_id="U1")
    gif = (await dg.list_all(db))[0]
    msg = _msg(animation_file_id="AAA")

    assert await dg.send_one(db, msg, gif) is not None
    assert msg.reply_animation.await_args.args[0] == "AAA"   # bare str payload
    assert db.rows[0]["send_count"] == 1


@pytest.mark.asyncio
async def test_send_downloads_a_url_row_then_upgrades_it(monkeypatch):
    monkeypatch.setattr(dg, "_download", AsyncMock(return_value=b"GIFBYTES"))
    db = _FakeDB()
    await dg.add(db, ["pocketsand"], url="https://media.tenor.com/x.gif")
    gif = (await dg.list_all(db))[0]

    assert await dg.send_one(db, _msg(animation_file_id="TG-123"), gif) is not None

    row = db.rows[0]
    assert row["file_id"] == "TG-123"    # learned from the sent message
    assert row["url"] is None            # host no longer needed
    assert row["send_count"] == 1


@pytest.mark.asyncio
async def test_send_survives_a_response_without_an_animation(monkeypatch):
    """Defensive: sent.animation is Optional in aiogram."""
    monkeypatch.setattr(dg, "_download", AsyncMock(return_value=b"X"))
    db = _FakeDB()
    await dg.add(db, ["pocketsand"], url="https://media.tenor.com/x.gif")
    gif = (await dg.list_all(db))[0]

    assert await dg.send_one(db, _msg(animation_file_id=None), gif) is not None
    assert db.rows[0]["send_count"] == 1
    assert db.rows[0]["file_id"] is None      # nothing learned, nothing broken


@pytest.mark.asyncio
async def test_send_returns_none_when_the_download_fails(monkeypatch):
    monkeypatch.setattr(dg, "_download", AsyncMock(return_value=None))
    db = _FakeDB()
    await dg.add(db, ["pocketsand"], url="https://media.tenor.com/dead.gif")
    gif = (await dg.list_all(db))[0]
    msg = _msg()

    assert await dg.send_one(db, msg, gif) is None
    msg.reply_animation.assert_not_awaited()
    assert db.rows[0]["send_count"] == 0


@pytest.mark.asyncio
async def test_send_returns_none_when_telegram_refuses():
    db = _FakeDB()
    await dg.add(db, ["pocketsand"], file_id="AAA", file_unique_id="U1")
    gif = (await dg.list_all(db))[0]
    msg = _msg()
    msg.reply_animation = AsyncMock(side_effect=RuntimeError("nope"))

    assert await dg.send_one(db, msg, gif) is None
    assert db.rows[0]["send_count"] == 0


@pytest.mark.asyncio
async def test_send_random_falls_back_to_text_on_empty_library():
    db, msg = _FakeDB(), _msg()
    assert await dg.send_random(db, msg, "pocketsand",
                                fallback="pocket sand!") is False
    msg.reply.assert_awaited_with("pocket sand!", disable_notification=True)


@pytest.mark.asyncio
async def test_send_random_sends_and_reports_true():
    db = _FakeDB()
    await dg.add(db, ["pocketsand"], file_id="AAA", file_unique_id="U1")
    msg = _msg(animation_file_id="AAA")
    assert await dg.send_random(db, msg, "pocketsand", fallback="x") is True
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_random_falls_back_when_the_send_fails(monkeypatch):
    monkeypatch.setattr(dg, "_download", AsyncMock(return_value=None))
    db = _FakeDB()
    await dg.add(db, ["pocketsand"], url="https://media.tenor.com/dead.gif")
    msg = _msg()
    assert await dg.send_random(db, msg, "pocketsand",
                                fallback="pocket sand!") is False
    msg.reply.assert_awaited_with("pocket sand!", disable_notification=True)


# ── the table must stay global ───────────────────────────────────────────────

def test_dale_gifs_table_has_no_chat_id_column():
    """chat_migration re-keys every table that has a chat_id; this library is
    shared across chats and must stay out of that."""
    from pathlib import Path
    sql = Path("ipedro/db/schema.sql").read_text()
    block = sql[sql.index("CREATE TABLE IF NOT EXISTS dale_gifs"):]
    block = block[:block.index(");")]
    assert "chat_id" not in block
