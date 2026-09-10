"""Telethon objects -> plain dict rows for the database."""
from datetime import datetime
from typing import Any

from telethon import utils
from telethon.tl import types as t


def jsonable(obj: Any) -> Any:
    """Make Telethon .to_dict() output JSON-serialisable: drop bytes, iso-format dates."""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items() if not isinstance(v, bytes)}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return None
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def _text(v):
    """Telethon >=1.36 wraps poll question/answers into TextWithEntities."""
    return getattr(v, "text", v)


def _peer_id(peer) -> int | None:
    if peer is None:
        return None
    try:
        return utils.get_peer_id(peer)
    except Exception:
        return None


def peer_type(entity) -> str:
    if isinstance(entity, t.User):
        return "user"
    if isinstance(entity, (t.Chat, t.ChatForbidden, t.ChatEmpty)):
        return "chat"
    if isinstance(entity, (t.Channel, t.ChannelForbidden)):
        return "supergroup" if getattr(entity, "megagroup", False) else "channel"
    return "unknown"


def chat_row(entity) -> dict:
    kind = peer_type(entity)
    row = {
        "id": utils.get_peer_id(entity),
        "type": kind,
        "title": None,
        "username": getattr(entity, "username", None),
        "first_name": getattr(entity, "first_name", None),
        "last_name": getattr(entity, "last_name", None),
        "is_forum": bool(getattr(entity, "forum", False)),
        "participants_count": getattr(entity, "participants_count", None),
        "raw": jsonable(entity.to_dict()) if hasattr(entity, "to_dict") else None,
    }
    try:
        row["title"] = utils.get_display_name(entity) or None
    except Exception:
        row["title"] = getattr(entity, "title", None)
    return row


def user_row(u: t.User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "phone": u.phone,
        "is_bot": bool(u.bot),
        "is_self": bool(u.is_self),
        "is_contact": bool(u.contact),
        "is_premium": bool(getattr(u, "premium", False)),
        "is_deleted": bool(u.deleted),
        "raw": jsonable(u.to_dict()),
    }


def _file_meta(m) -> dict:
    f = m.file
    if f is None:
        return {}

    def get(attr):
        try:
            return getattr(f, attr, None)
        except Exception:  # Telethon helpers assume well-formed media (e.g. photo sizes)
            return None

    meta = {k: get(k) for k in ("name", "mime_type", "size", "duration", "width", "height", "emoji", "title", "performer")}
    meta["mime"] = meta.pop("mime_type")
    return {k: v for k, v in meta.items() if v is not None}


def media_info(m) -> tuple[str | None, dict | None]:
    if m.media is None:
        return None, None
    base = _file_meta(m)
    if m.sticker:
        return "sticker", base
    if m.gif:
        return "gif", base
    if m.voice:
        return "voice", base
    if m.video_note:
        return "video_note", base
    if m.video:
        return "video", base
    if m.audio:
        return "audio", base
    if m.photo:
        return "photo", base
    if m.poll:
        poll = m.poll.poll
        results = getattr(m.poll, "results", None)
        return "poll", {
            "question": _text(poll.question),
            "answers": [_text(a.text) for a in poll.answers],
            "closed": bool(poll.closed),
            "multiple_choice": bool(poll.multiple_choice),
            "quiz": bool(poll.quiz),
            "total_voters": getattr(results, "total_voters", None),
        }
    if isinstance(m.media, t.MessageMediaWebPage):
        wp = m.media.webpage
        return "webpage", {
            k: v
            for k, v in {
                "url": getattr(wp, "url", None),
                "site_name": getattr(wp, "site_name", None),
                "title": getattr(wp, "title", None),
                "description": getattr(wp, "description", None),
                **base,
            }.items()
            if v is not None
        }
    if m.contact:
        c = m.contact
        return "contact", {
            "phone": c.phone_number,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "user_id": c.user_id,
        }
    if m.venue:
        v = m.venue
        return "venue", {"title": v.title, "address": v.address, "lat": v.geo.lat, "long": v.geo.long}
    if m.geo:
        return "geo", {"lat": m.geo.lat, "long": m.geo.long}
    if m.dice:
        return "dice", {"emoji": m.dice.emoticon, "value": m.dice.value}
    if m.game:
        return "game", {"title": m.game.title, "description": m.game.description}
    if m.invoice:
        return "invoice", {"title": m.invoice.title, "description": m.invoice.description}
    if m.document:
        return "document", base
    return type(m.media).__name__, base or None


def reactions_info(m) -> list | None:
    r = m.reactions
    if r is None or not r.results:
        return None
    out = []
    for rc in r.results:
        reaction = rc.reaction
        if isinstance(reaction, t.ReactionEmoji):
            key = reaction.emoticon
        elif isinstance(reaction, t.ReactionCustomEmoji):
            key = f"custom:{reaction.document_id}"
        else:
            key = str(reaction)
        out.append({"emoji": key, "count": rc.count, "mine": rc.chosen_order is not None})
    return out


def message_row(m) -> dict:
    """Works for both Message and MessageService."""
    is_service = isinstance(m, t.MessageService)
    reply = getattr(m, "reply_to", None)
    fwd = getattr(m, "fwd_from", None)

    reply_to_msg_id = getattr(reply, "reply_to_msg_id", None)
    reply_to_top_id = getattr(reply, "reply_to_top_id", None)
    topic_id = None
    if reply is not None and getattr(reply, "forum_topic", False):
        if reply_to_top_id is None:
            # posted straight into a topic: reply_to_msg_id holds the topic id, it is not a real reply
            topic_id, reply_to_msg_id = reply_to_msg_id, None
        else:
            topic_id = reply_to_top_id
            if reply_to_msg_id == topic_id:  # reply to the topic's root service message: not a real reply
                reply_to_msg_id = None

    if is_service:
        media_type, media = None, None
        action_type = type(m.action).__name__ if m.action else None
        action = jsonable(m.action.to_dict()) if m.action else None
        text = None
        entities = None
        reactions = None
    else:
        media_type, media = media_info(m)
        action_type, action = None, None
        text = m.message or None
        entities = jsonable([e.to_dict() for e in m.entities]) if m.entities else None
        reactions = reactions_info(m)

    replies = getattr(m, "replies", None)

    return {
        "chat_id": m.chat_id,
        "msg_id": m.id,
        "sender_id": m.sender_id,
        "post_author": getattr(m, "post_author", None),
        "date": m.date,
        "edit_date": None if getattr(m, "edit_hide", False) else getattr(m, "edit_date", None),
        "text": text,
        "entities": entities,
        "is_out": bool(m.out),
        "mentioned": bool(getattr(m, "mentioned", False)),
        "is_post": bool(getattr(m, "post", False)),
        "silent": bool(getattr(m, "silent", False)),
        "pinned": bool(getattr(m, "pinned", False)),
        "via_bot_id": getattr(m, "via_bot_id", None),
        "reply_to_msg_id": reply_to_msg_id,
        "reply_to_top_id": reply_to_top_id,
        "topic_id": topic_id,
        "reply_to_chat_id": _peer_id(getattr(reply, "reply_to_peer_id", None)),
        "quote_text": getattr(reply, "quote_text", None),
        "fwd_from_id": _peer_id(getattr(fwd, "from_id", None)),
        "fwd_from_name": getattr(fwd, "from_name", None),
        "fwd_date": getattr(fwd, "date", None),
        "fwd_channel_post": getattr(fwd, "channel_post", None),
        "fwd_post_author": getattr(fwd, "post_author", None),
        "fwd_saved_from_chat_id": _peer_id(getattr(fwd, "saved_from_peer", None)),
        "fwd_saved_from_msg_id": getattr(fwd, "saved_from_msg_id", None),
        "media_type": media_type,
        "media": media,
        "grouped_id": getattr(m, "grouped_id", None),
        "action_type": action_type,
        "action": action,
        "views": getattr(m, "views", None),
        "forwards": getattr(m, "forwards", None),
        "replies_count": getattr(replies, "replies", None),
        "reactions": reactions,
        "ttl_period": getattr(m, "ttl_period", None),
        "raw": jsonable(m.to_dict()),
    }
