"""Pattern detection: the repeats an operator keeps typing, handed back as reusable work.

The invariant under test is that a suggestion is advice only. Detecting a pattern never
sends, never joins, never switches an Auto Reply rule on - accepting one saves a template
or a rule that is switched off, and the other kinds only prefill a form that already asks
for consent.
"""

import asyncio
import json
import os
import time
from datetime import datetime
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import auto_reply
import copilot
import knowledge
import patterns
import template_manager

OPERATOR = "operator-1"
REQUEST = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id=OPERATOR)))
REPEAT = "refund policy covers shipping damage report"
OTHER = "opening hours change during public holidays next"
SECRET_RUN = "AAaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """An isolated operator store, a template file in the sandbox, and quiet logs."""
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(patterns.log_manager, "add_log", lambda *a, **k: None)
    monkeypatch.setattr(template_manager, "ACCOUNTS_DIR", str(tmp_path))
    monkeypatch.setattr(template_manager, "TEMPLATE_FILE", str(tmp_path / "templates.json"))
    monkeypatch.setattr(template_manager.template_manager, "templates", {})
    yield


def run(coro):
    return asyncio.run(coro)


def accept(suggestion_id, name=""):
    return run(patterns.accept(suggestion_id, patterns.AcceptBody(name=name), REQUEST))


def dismiss(suggestion_id):
    return run(patterns.dismiss(suggestion_id, REQUEST))


def an_app(sign_in=True):
    app = FastAPI()
    app.include_router(patterns.router)

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.user = SimpleNamespace(id=OPERATOR) if sign_in else None
        return await call_next(request)

    return TestClient(app)


def note(text, *, source="manual", title=""):
    return knowledge.add_document(OPERATOR, title=title, text=text, source=source)


def three_the_same(**kwargs):
    """The same wording, three times, differing only in the case number."""
    return [note(f"{REPEAT} case {i}", **kwargs) for i in (1, 2, 3)]


def a_turn(text, *, action="template"):
    turns = copilot._turns(OPERATOR)
    turns.append({"id": f"turn-{len(turns)}", "text": text, "action": action, "title": "",
                  "time": datetime.now().isoformat(timespec="seconds")})
    copilot._write_turns(OPERATOR, turns)


_ids = count(1)


def an_item(text, *, source="manual", action=None, title="", at=None):
    return {"id": f"item-{next(_ids)}", "text": text, "source": source, "action": action,
            "title": title, "time": time.time() if at is None else at}


def store_file(name):
    return Path(os.environ["OPERATOR_STORE_DIR"]) / name


# ---------------------------------------------------------------- the wording itself


def test_two_is_a_coincidence_and_three_is_a_pattern():
    items = [an_item(f"{REPEAT} case {i}") for i in (1, 2)]

    assert patterns.detect(items) == []
    assert len(patterns.detect(items + [an_item(f"{REPEAT} case 3")])) == 1


def test_three_the_same_wording_becomes_one_suggestion():
    three_the_same()

    found = patterns.refresh(OPERATOR, force=True)["suggestions"]

    assert len(found) == 1
    assert found[0]["count"] == 3
    assert found[0]["id"] and found[0]["created"]


def test_case_numbers_and_handles_are_not_part_of_the_pattern():
    items = [an_item(f"{REPEAT} case 7 @first_one"), an_item(f"{REPEAT} case 8 @second_two"),
             an_item(f"{REPEAT} case 9 @third_three")]

    assert len(patterns.detect(items)) == 1


def test_punctuation_and_letter_case_are_ignored():
    items = [an_item(f"{REPEAT}!"), an_item(f"{REPEAT.upper()}."), an_item(f"  {REPEAT},  ")]

    assert len(patterns.detect(items)) == 1


def test_a_note_shorter_than_four_words_has_no_shape_to_repeat():
    items = [an_item("refund policy") for _ in range(4)]

    assert patterns.detect(items) == []


def test_shingles_are_three_words_long():
    assert patterns.shingles(["one", "two", "three"]) == []
    assert patterns.shingles(["one", "two", "three", "four"]) == ["one two three", "two three four"]


def test_normalise_strips_the_noise_and_caps_the_length():
    assert patterns.normalise("Order 4012 shipped today by @courier_one") == \
        ["order", "shipped", "today"]
    assert len(patterns.normalise(" ".join(["alpha"] * 200))) == 80


def test_only_the_last_two_weeks_are_read():
    stale = time.time() - (patterns.WINDOW_DAYS + 1) * 86400
    items = [an_item(f"{REPEAT} case {i}", at=stale) for i in (1, 2, 3)]

    assert patterns.detect(items) == []
    assert len(patterns.detect([an_item(f"{REPEAT} case {i}") for i in (1, 2, 3)])) == 1


def test_an_undated_item_is_not_dropped_by_the_window():
    items = [{"id": f"a{i}", "text": f"{REPEAT} case {i}", "source": "manual", "time": 0}
             for i in (1, 2, 3)]

    assert len(patterns.detect(items)) == 1


def test_each_repeat_is_claimed_by_at_most_one_suggestion():
    items = [an_item(f"{REPEAT} case {i}") for i in range(1, 6)]
    items += [an_item(f"{OTHER} case {i}") for i in range(1, 4)]

    found = patterns.detect(items)

    assert sorted(s["count"] for s in found) == [3, 5]


def test_the_suggestions_are_bounded():
    items = []
    for index in range(30):
        items += [an_item(f"{REPEAT} variant {index} report {i}") for i in (1, 2, 3)]

    assert len(patterns.detect(items)) <= patterns.MAX_SUGGESTIONS


def test_detection_makes_no_provider_call(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("pattern detection must stay inside this container")

    monkeypatch.setattr(knowledge, "search", explode)

    found = patterns.detect([an_item(f"{REPEAT} case {i}") for i in (1, 2, 3)])

    assert len(found) == 1


# ---------------------------------------------------------------- what kind it becomes


def test_repeated_own_words_become_a_template():
    three_the_same()

    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]

    assert suggestion["kind"] == "template"
    assert suggestion["payload"]["category"] == "Suggested"
    assert suggestion["payload"]["content"] == suggestion["text"]
    assert suggestion["payload"]["name"]


def test_repeated_saved_replies_become_an_auto_reply_rule_proposal():
    for index in (1, 2, 3):
        note(f"{REPEAT} case {index}", source="auto_reply")

    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]

    assert suggestion["kind"] == "reply"
    assert suggestion["payload"]["enabled"] is False
    assert suggestion["payload"]["match"] == "keyword"
    assert suggestion["payload"]["keywords"]


def test_repeated_task_wording_becomes_a_task():
    for index in (1, 2, 3):
        a_turn(f"{REPEAT} case {index}", action="task")

    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]

    assert suggestion["kind"] == "task"
    assert set(suggestion["payload"]) == {"message"}


def test_a_wording_seen_across_three_surfaces_is_a_process_worth_automating():
    three_the_same()
    note(f"{REPEAT} case 4", source="import")
    for index in (5, 6):
        a_turn(f"{REPEAT} case {index}")

    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]

    assert suggestion["kind"] == "automation"
    assert set(suggestion["payload"]) == {"instruction", "steps"}
    assert len(suggestion["payload"]["steps"]) == 3
    assert "Create a template named" in suggestion["payload"]["instruction"]


def test_evidence_shows_at_most_three_of_the_repeats():
    for index in range(6):
        note(f"{REPEAT} case {index}")

    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]

    assert len(suggestion["evidence"]) == patterns.MAX_EVIDENCE
    assert suggestion["count"] == 6


def test_the_title_prefers_a_saved_name():
    three_the_same(title="Refund wording")

    assert patterns.refresh(OPERATOR, force=True)["suggestions"][0]["title"] == "Refund wording"


def test_the_sources_are_listed_so_the_operator_can_separate_the_two():
    three_the_same()
    for index in (4, 5):
        a_turn(f"{REPEAT} case {index}")

    assert patterns.refresh(OPERATOR, force=True)["suggestions"][0]["sources"] == ["copilot", "manual"]


# ---------------------------------------------------------------- the corpus


def test_copilot_history_is_part_of_what_is_read():
    three_the_same()
    a_turn("an unrelated sentence about opening hours")

    corpus = patterns.build_corpus(OPERATOR)

    assert len(corpus) == 4
    assert {item["source"] for item in corpus} == {"manual", "copilot"}


def test_the_detector_never_reads_a_credential():
    for index in (1, 2, 3):
        note(f"{REPEAT} case {index} session string is {SECRET_RUN}")

    corpus = patterns.build_corpus(OPERATOR)

    assert all(SECRET_RUN not in item["text"] for item in corpus)
    assert SECRET_RUN not in json.dumps(patterns.detect(corpus))


def test_the_raw_text_is_bounded_per_item():
    padded = REPEAT + " " + "detail " * 400
    for index in (1, 2, 3):
        note(f"{padded} case {index}")

    assert all(len(item["text"]) <= patterns.MAX_ITEM_CHARS for item in patterns.build_corpus(OPERATOR))


def test_the_corpus_is_the_operators_own_and_nobody_elses():
    three_the_same()
    knowledge.add_document("operator-2", title="Private", text=f"{REPEAT} case 9")

    assert len(patterns.build_corpus(OPERATOR)) == 3


# ---------------------------------------------------------------- the store and the throttle


def test_a_fresh_install_says_there_is_nothing_to_detect_yet():
    record = patterns.refresh(OPERATOR)

    assert record["suggestions"] == []
    assert patterns.summary(record, 0)["empty_detail"] == patterns.NO_DATA


def test_detection_is_not_rerun_on_every_open_of_the_panel():
    three_the_same()
    patterns.refresh(OPERATOR, force=True)
    for index in (1, 2, 3):
        note(f"{OTHER} case {index}")

    assert len(patterns.refresh(OPERATOR)["suggestions"]) == 1
    assert len(patterns.refresh(OPERATOR, force=True)["suggestions"]) == 2


def test_an_accepted_suggestion_is_not_asked_twice():
    three_the_same()
    record = patterns.refresh(OPERATOR, force=True)
    record["suggestions"][0]["status"] = "accepted"
    patterns._save(OPERATOR, record)

    after = patterns.refresh(OPERATOR, force=True)

    assert [s["status"] for s in after["suggestions"]] == ["accepted"]


def test_a_dismissed_wording_stays_dismissed():
    three_the_same()
    patterns.refresh(OPERATOR, force=True)
    record = patterns._record(OPERATOR)
    record["dismissed"] = [record["suggestions"][0]["signature"]]
    record["suggestions"] = []
    patterns._save(OPERATOR, record)

    assert patterns.refresh(OPERATOR, force=True)["suggestions"] == []


def test_suggestions_are_scoped_to_the_operator_who_wrote_them():
    three_the_same()
    patterns.refresh(OPERATOR, force=True)

    assert patterns.refresh("operator-2")["suggestions"] == []


def test_the_store_holds_nothing_in_plain_text():
    three_the_same()
    patterns.refresh(OPERATOR, force=True)

    raw = json.loads(store_file("patterns.secret.json").read_text())

    assert set(raw) == {"ciphertext"}
    assert REPEAT not in raw["ciphertext"]


def test_the_dismissed_list_is_bounded():
    patterns._save(OPERATOR, {"suggestions": [], "dismissed": [f"s{i}" for i in range(300)],
                              "generated_at": 0})

    assert len(patterns._record(OPERATOR)["dismissed"]) == patterns.DISMISSED_MAX


def test_a_broken_store_is_read_as_an_empty_one():
    store_file("patterns.secret.json").write_text("not a secret file")

    assert patterns._record(OPERATOR)["suggestions"] == []


# ---------------------------------------------------------------- accepting one


def test_accepting_a_template_saves_it_and_sends_nothing():
    three_the_same()
    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]

    result = accept(suggestion["id"])

    assert result["kind"] == "template"
    saved = template_manager.template_manager.templates[result["result"]["template_id"]]
    assert saved["content"] == suggestion["text"]
    assert saved["category"] == "Suggested"
    assert template_manager.template_manager.templates[result["result"]["template_id"]]["use_count"] == 0


def test_the_operator_can_rename_the_template_as_it_is_saved():
    three_the_same()
    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]

    result = accept(suggestion["id"], "Refund line")

    assert template_manager.template_manager.templates[result["result"]["template_id"]]["name"] == "Refund line"


def test_accepting_a_reply_writes_the_rule_switched_off():
    for index in (1, 2, 3):
        note(f"{REPEAT} case {index}", source="auto_reply")
    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]

    result = accept(suggestion["id"])

    rule = auto_reply._record(OPERATOR)["rules"][0]
    assert result["result"]["enabled"] is False
    assert rule["enabled"] is False
    assert rule["keywords"] == suggestion["payload"]["keywords"]


def test_an_accepted_reply_rule_stays_inert_until_it_is_switched_on():
    for index in (1, 2, 3):
        note(f"{REPEAT} case {index}", source="auto_reply")
    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]
    accept(suggestion["id"])

    assert auto_reply.match_rule(auto_reply._record(OPERATOR)["rules"], REPEAT) is None


def test_a_full_rule_book_is_refused_rather_than_truncated():
    for index in (1, 2, 3):
        note(f"{REPEAT} case {index}", source="auto_reply")
    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]
    record = auto_reply._record(OPERATOR)
    record["rules"] = [{"id": f"r{i}", "name": "R", "match": "any", "keywords": [], "reply": "x",
                        "enabled": False} for i in range(auto_reply.MAX_RULES)]
    auto_reply._save(OPERATOR, record)

    with pytest.raises(HTTPException) as error:
        accept(suggestion["id"])

    assert error.value.status_code == 409
    assert error.value.detail == auto_reply.TOO_MANY_RULES


def test_accepting_a_task_only_hands_the_words_to_the_form():
    for index in (1, 2, 3):
        a_turn(f"{REPEAT} case {index}", action="task")
    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]

    result = accept(suggestion["id"])

    assert result["result"]["opens"] == "bulksend"
    assert result["result"]["payload"]["message"] == suggestion["text"]
    assert auto_reply._record(OPERATOR)["rules"] == []


def test_accepting_a_process_only_hands_the_words_to_the_copilot():
    three_the_same()
    note(f"{REPEAT} case 4", source="import")
    for index in (5, 6):
        a_turn(f"{REPEAT} case {index}")
    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]

    result = accept(suggestion["id"])

    assert result["result"]["opens"] == "copilot"
    assert result["result"]["payload"]["steps"]
    assert template_manager.template_manager.templates == {}
    assert auto_reply._record(OPERATOR)["rules"] == []


def test_accepting_twice_saves_the_template_once():
    three_the_same()
    suggestion = patterns.refresh(OPERATOR, force=True)["suggestions"][0]
    accept(suggestion["id"])

    with pytest.raises(HTTPException) as error:
        accept(suggestion["id"])

    assert error.value.status_code == 409
    assert len(template_manager.template_manager.templates) == 1


def test_a_suggestion_with_no_words_left_is_refused():
    patterns._save(OPERATOR, {"suggestions": [{"id": "empty", "kind": "template", "signature": "s",
                                               "payload": {"name": "N", "content": "  "}, "status": "new"}],
                              "dismissed": [], "generated_at": time.time()})

    with pytest.raises(HTTPException) as error:
        accept("empty")

    assert error.value.status_code == 400


def test_an_unknown_suggestion_is_not_invented():
    with pytest.raises(HTTPException) as error:
        accept("nope")
    assert error.value.status_code == 404

    with pytest.raises(HTTPException) as error:
        dismiss("nope")
    assert error.value.status_code == 404


# ---------------------------------------------------------------- the endpoints


def test_the_panel_gets_the_suggestions_and_how_much_was_read():
    three_the_same()

    body = an_app().get("/api/patterns/suggestions").json()

    assert body["success"] is True
    assert body["corpus"] == 3
    assert body["min_support"] == patterns.MIN_SUPPORT
    assert body["window_days"] == patterns.WINDOW_DAYS
    assert body["suggestions"][0]["kind"] == "template"


def test_the_panel_view_hides_the_internal_signature():
    three_the_same()

    shown = an_app().get("/api/patterns/suggestions").json()["suggestions"][0]

    assert "signature" not in shown
    assert set(shown) == {"id", "kind", "title", "text", "count", "sources", "evidence", "payload",
                          "status", "created"}


def test_the_refresh_button_asks_for_a_new_look():
    client = an_app()
    client.post("/api/patterns/refresh")
    three_the_same()

    assert client.get("/api/patterns/suggestions").json()["suggestions"] == []
    assert client.post("/api/patterns/refresh").json()["suggestions"]


def test_dismissing_from_the_panel_takes_it_away_for_good():
    three_the_same()
    client = an_app()
    suggestion = client.get("/api/patterns/suggestions").json()["suggestions"][0]

    assert client.post(f"/api/patterns/suggestions/{suggestion['id']}/dismiss").json()["success"] is True
    assert client.get("/api/patterns/suggestions").json()["suggestions"] == []
    assert client.post("/api/patterns/refresh").json()["suggestions"] == []


def test_accepting_from_the_panel_saves_the_template():
    three_the_same()
    client = an_app()
    suggestion = client.get("/api/patterns/suggestions").json()["suggestions"][0]

    result = client.post(f"/api/patterns/suggestions/{suggestion['id']}/accept",
                         json={"name": "Kept"}).json()

    assert result["success"] is True
    assert template_manager.template_manager.templates[result["result"]["template_id"]]["name"] == "Kept"


def test_the_limits_are_reported_to_the_panel():
    settings = an_app().get("/api/patterns/settings").json()["settings"]

    assert settings == {"min_support": patterns.MIN_SUPPORT, "window_days": patterns.WINDOW_DAYS,
                        "max_suggestions": patterns.MAX_SUGGESTIONS, "refresh_seconds": patterns.REFRESH_SECONDS}


@pytest.mark.parametrize("path,method", [("/api/patterns/suggestions", "get"),
                                         ("/api/patterns/refresh", "post"),
                                         ("/api/patterns/settings", "get"),
                                         ("/api/patterns/suggestions/x/dismiss", "post"),
                                         ("/api/patterns/suggestions/x/accept", "post")])
def test_no_anonymous_visitor_sees_anyones_repeated_words(path, method):
    response = an_app(sign_in=False).request(method.upper(), path, json={})

    assert response.status_code == 403


def test_the_router_exposes_no_send_surface():
    paths = {(route.path, tuple(sorted(route.methods - {"HEAD", "OPTIONS"})))
             for route in patterns.router.routes}

    assert paths == {("/api/patterns/suggestions", ("GET",)),
                     ("/api/patterns/refresh", ("POST",)),
                     ("/api/patterns/suggestions/{suggestion_id}/dismiss", ("POST",)),
                     ("/api/patterns/suggestions/{suggestion_id}/accept", ("POST",)),
                     ("/api/patterns/settings", ("GET",))}


def test_nothing_is_read_or_stored_without_an_encryption_key(monkeypatch):
    monkeypatch.delenv("SESSION_ENCRYPTION_KEY")

    with pytest.raises(HTTPException) as error:
        patterns.refresh(OPERATOR)

    assert error.value.status_code == 500
