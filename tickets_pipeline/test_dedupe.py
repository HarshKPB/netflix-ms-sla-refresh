"""Grouping + dedupe tests for the duplicate-row fix. Run: python -m tickets_pipeline.test_dedupe"""
from . import pipeline


def _thread(tid, bodies, subject="s"):
    return {"id": tid, "viewUrl": f"https://mail.google.com/#all/thread-f:{tid}",
            "messages": [{"plaintextBody": b, "date": "2026-01-01T00:00:00Z",
                          "sender": "x@premium-blend.com", "subject": subject} for b in bodies]}


def test_conv_merges_opener_and_numbered_reply():
    opener = _thread(111, ["Opener, no ticket number yet. %%[ConversationId: abc123]%%"])
    reply = _thread(222, ["## Please Reply above this line ## Ticket Number 4084857 "
                          "https://community.sprinklr.com/support/requests/4084857 %%[ConversationId: abc123]%%"])
    g = pipeline.group_by_ticket([opener, reply])
    assert set(g) == {"4084857"}, g  # both threads under the number, no thread:<id> stub


def test_number_recovered_from_review_link():
    t = _thread(333, ["Reopening. See https://community.sprinklr.com/support/requests/4091322 for context."])
    g = pipeline.group_by_ticket([t])
    assert set(g) == {"4091322"}, g


def test_conv_only_threads_merge_without_a_number():
    a = _thread(333, ["Scheduling a call. %%[ConversationId: deadbeef]%%"])
    b = _thread(444, ["Re: scheduling. %%[msConversationId: deadbeef]%%"])
    g = pipeline.group_by_ticket([a, b])
    assert set(g) == {"conv:deadbeef"}, g  # one group, not two stubs


def test_orphan_stays_a_thread_stub():
    t = _thread(555, ["Plain internal note, no Sprinklr markers at all."])
    g = pipeline.group_by_ticket([t])
    assert set(g) == {"thread:555"}, g


def test_unique_subject_merges_numberless_fragment():
    # a scheduling fragment: own ConversationId, no number, subject == the numbered case
    SUBJ = "Outbound API returns two posts for the same content"
    numbered = _thread(111, ["Ticket Number 4084857 %%[ConversationId: aaa]%%"], subject=SUBJ)
    fragment = _thread(222, ["Scheduling a call. %%[ConversationId: zzz]%%"], subject=SUBJ)
    g = pipeline.group_by_ticket([numbered, fragment])
    assert set(g) == {"4084857"}, g


def test_colliding_subject_does_not_merge():
    # same subject maps to two different numbers -> ambiguous -> fragment stays a stub
    SUBJ = "Access to Sprinklr please"
    a = _thread(111, ["Ticket Number 4094304 %%[ConversationId: aaa]%%"], subject=SUBJ)
    b = _thread(222, ["Ticket Number 4099999 %%[ConversationId: bbb]%%"], subject=SUBJ)
    frag = _thread(333, ["No number here. %%[ConversationId: ccc]%%"], subject=SUBJ)
    g = pipeline.group_by_ticket([a, b, frag])
    assert set(g) == {"4094304", "4099999", "conv:ccc"}, g


def test_dedupe_drops_same_thread_and_same_conv_stubs():
    hx = format(555, "x")
    rows = [
        {"case_id": "4084857", "conversation_id": "abc123", "gmail_link": "x/thread-f:555"},
        {"case_id": "thread:" + hx, "conversation_id": "", "gmail_link": "x/thread-f:555"},   # same Gmail thread
        {"case_id": "conv:abc123", "conversation_id": "abc123", "gmail_link": "x/thread-f:666"},  # same conv id
        {"case_id": "thread:deadbeef", "conversation_id": "", "gmail_link": "x/thread-f:777"},  # genuine orphan
    ]
    kept, dropped = pipeline.dedupe_rows(rows)
    assert {r["case_id"] for r in kept} == {"4084857", "thread:deadbeef"}, {r["case_id"] for r in kept}
    assert set(dropped) == {"thread:" + hx, "conv:abc123"}, dropped


def test_dedupe_drops_stub_by_unique_title():
    rows = [
        {"case_id": "4083418", "conversation_id": "", "gmail_link": "x/thread-f:1", "title": "TikTok Reach & Frequency Validation"},
        {"case_id": "thread:abc", "conversation_id": "", "gmail_link": "x/thread-f:2", "title": "TikTok Reach & Frequency Validation"},
        {"case_id": "thread:xyz", "conversation_id": "", "gmail_link": "x/thread-f:3", "title": "A different unique orphan title"},
    ]
    kept, dropped = pipeline.dedupe_rows(rows)
    assert {r["case_id"] for r in kept} == {"4083418", "thread:xyz"}, {r["case_id"] for r in kept}
    assert dropped == ["thread:abc"], dropped


def test_dedupe_keeps_stub_on_title_collision():
    rows = [
        {"case_id": "4001", "conversation_id": "", "gmail_link": "x/thread-f:1", "title": "Access to Sprinklr please"},
        {"case_id": "4002", "conversation_id": "", "gmail_link": "x/thread-f:2", "title": "Access to Sprinklr please"},
        {"case_id": "thread:q", "conversation_id": "", "gmail_link": "x/thread-f:3", "title": "Access to Sprinklr please"},
    ]
    kept, dropped = pipeline.dedupe_rows(rows)
    assert {r["case_id"] for r in kept} == {"4001", "4002", "thread:q"}, {r["case_id"] for r in kept}
    assert dropped == [], dropped


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"\n{len(fns)} passed")
