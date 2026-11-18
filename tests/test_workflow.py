import json
import tempfile
import unittest
from pathlib import Path

from nlp_classroom_entity_relations.repository import RecordRepository
from nlp_classroom_entity_relations.text import (
    find_occurrences,
    normalize_text,
    split_sentences,
    tokenize,
)
from nlp_classroom_entity_relations.workflow import TextWorkflow


FIXTURE = Path("fixtures/records.json")
LESSON_C = "classroom-entity-relations-lesson-c"
LESSON_D = "classroom-entity-relations-lesson-d"


def card_for(workflow, record_id, predicate):
    matches = [
        card
        for card in workflow.list_cards(record_id=record_id)
        if card["predicate"] == predicate
    ]
    assert len(matches) == 1, (record_id, predicate, matches)
    return matches[0]


class TextWorkflowTests(unittest.TestCase):
    def test_normalization_and_tokenization(self):
        self.assertEqual(normalize_text("  Ａ  B\nC  "), "A B C")
        self.assertEqual(tokenize("语文课 A2。"), ["语", "文", "课", "A", "2", "。"])

    def test_ingest_persists_analysis_and_uses_newer_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = TextWorkflow(RecordRepository(Path(directory) / "records.json"))
            first = {"record_id": "lesson-1", "source": "synthetic", "revision": 1,
                     "observed_at": "2026-11-18T09:00:00Z", "text": "原稿 甲乙", "context": "课堂"}
            second = {**first, "revision": 2, "text": "修订稿 乙丙"}
            self.assertEqual(workflow.ingest([first]), {"received": 1, "stored": 1})
            workflow.ingest([second, first])
            result = workflow.list_records()[0]
            self.assertEqual(result["revision"], 2)
            self.assertEqual(result["text"], "修订稿 乙丙")
            self.assertEqual(result["analysis"]["token_count"], 5)

    def test_invalid_records_are_rejected_without_partial_write(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = RecordRepository(Path(directory) / "records.json")
            workflow = TextWorkflow(repository)
            valid = {"record_id": "one", "source": "synthetic", "revision": 1,
                     "observed_at": "2026-11-18T09:00:00Z", "text": "有效文本"}
            workflow.ingest([valid])
            invalid = {**valid, "record_id": "two", "observed_at": "bad-time"}
            with self.assertRaises(ValueError):
                workflow.ingest([valid, invalid])
            self.assertEqual([row["record_id"] for row in repository.read()], ["one"])

    def test_source_filter_and_fixture_are_local(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            workflow = TextWorkflow(RecordRepository(Path(directory) / "records.json"))
            workflow.ingest(fixture)
            self.assertEqual(len(workflow.list_records("school-synthetic")), 4)
            self.assertEqual(workflow.list_records("missing"), [])


class MentionAndEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workflow = TextWorkflow(
            RecordRepository(Path(self.directory.name) / "records.json")
        )
        self.workflow.ingest(json.loads(FIXTURE.read_text(encoding="utf-8")))

    def test_sentence_offsets_point_into_the_stored_text(self):
        record = next(
            row for row in self.workflow.list_records() if row["record_id"] == LESSON_C
        )
        sentences = split_sentences(record["text"])
        self.assertEqual(sentences[3]["text"], "他随后表扬了乙班的李明。")
        for sentence in sentences:
            self.assertEqual(
                record["text"][sentence["start"]:sentence["end"]], sentence["text"]
            )

    def test_occurrences_are_non_overlapping_and_ordered(self):
        text = normalize_text("李明说李明来了")
        self.assertEqual(find_occurrences(text, "李明"), [(0, 2), (3, 5)])

    def test_ambiguous_pronoun_does_not_guess(self):
        card = card_for(self.workflow, LESSON_C, "表扬")
        self.assertEqual(card["subject"]["entity_id"], None)
        self.assertEqual(card["status"], "ambiguous")
        self.assertEqual(
            card["subject"]["candidates"], ["teacher-wang", "person-li-jia"]
        )

    def test_relation_card_exposes_evidence_version_and_review_state(self):
        card = card_for(self.workflow, LESSON_C, "介绍")
        self.assertEqual(card["text_version"], f"{LESSON_C}@1")
        self.assertEqual(card["record_revision"], 1)
        self.assertEqual(card["review_status"], "unreviewed")
        self.assertEqual(card["evidence"]["quote"], "体育老师王老师先介绍了甲班的李明。")
        self.assertEqual(card["evidence"]["sentence_index"], 2)
        record = next(
            row for row in self.workflow.list_records() if row["record_id"] == LESSON_C
        )
        evidence = card["evidence"]
        self.assertEqual(
            record["text"][evidence["start"]:evidence["end"]], evidence["quote"]
        )

    def test_card_detail_returns_cited_sentences(self):
        card = card_for(self.workflow, LESSON_C, "介绍")
        detail = self.workflow.card(card["relation_id"])
        self.assertEqual(detail["text_version"], f"{LESSON_C}@1")
        self.assertEqual(len(detail["sentences"]), 1)
        self.assertTrue(detail["sentences"][0]["contains_evidence"])
        self.assertIn("王老师", detail["sentences"][0]["text"])

    def test_same_name_without_context_stays_ambiguous(self):
        card = card_for(self.workflow, LESSON_D, "参加")
        self.assertEqual(card["status"], "ambiguous")
        self.assertEqual(
            card["subject"]["candidates"], ["person-li-jia", "person-li-yi"]
        )
        self.assertIsNone(card["subject"]["entity_id"])

    def test_repeated_evidence_prefers_the_occurrence_near_the_mentions(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = TextWorkflow(RecordRepository(Path(directory) / "records.json"))
            text = "甲认识乙。乙认识甲。甲认识乙。"
            workflow.ingest(
                [
                    {
                        "record_id": "repeat",
                        "source": "synthetic",
                        "revision": 1,
                        "observed_at": "2026-11-18T09:00:00Z",
                        "text": text,
                        "entities": [
                            {"entity_id": "a", "label": "甲"},
                            {"entity_id": "b", "label": "乙"},
                        ],
                        "mentions": [
                            {"surface": "甲", "start": 10, "end": 11},
                            {"surface": "乙", "start": 13, "end": 14},
                        ],
                        "relations": [
                            {
                                "predicate": "认识",
                                "subject_mention": 0,
                                "object_mention": 1,
                                "evidence": "甲认识乙。",
                            }
                        ],
                    }
                ]
            )
            card = workflow.list_cards()[0]
            self.assertEqual(card["evidence"]["start"], 10)
            record = workflow.list_records()[0]
            self.assertEqual(
                record["text"][card["evidence"]["start"]:card["evidence"]["end"]],
                card["evidence"]["quote"],
            )

    def test_correction_propagates_to_dependent_card(self):
        mention = next(
            row
            for row in self.workflow.list_mentions(record_id=LESSON_C)
            if row["surface"] == "他"
        )
        before = card_for(self.workflow, LESSON_C, "表扬")
        self.assertIn(mention["mention_id"], before["mention_ids"])

        self.workflow.assign_mention(mention["mention_id"], "teacher-wang")
        after = card_for(self.workflow, LESSON_C, "表扬")
        self.assertEqual(after["subject"]["entity_id"], "teacher-wang")
        self.assertEqual(after["status"], "resolved")
        returned = self.workflow.mention(mention["mention_id"])
        self.assertEqual(returned["mention"]["assignment"], "manual")
        self.assertIn(after["relation_id"], [row["relation_id"] for row in returned["cards"]])

    def test_correction_is_persisted_across_workflow_instances(self):
        mention = next(
            row
            for row in self.workflow.list_mentions(record_id=LESSON_C)
            if row["surface"] == "他"
        )
        self.workflow.assign_mention(mention["mention_id"], "person-li-jia")
        reopened = TextWorkflow(RecordRepository(Path(self.directory.name) / "records.json"))
        card = card_for(reopened, LESSON_C, "表扬")
        self.assertEqual(card["subject"]["entity_id"], "person-li-jia")
        self.assertEqual(len(reopened.list_decisions()), 1)

    def test_assigning_unknown_entity_is_rejected(self):
        mention = next(
            row
            for row in self.workflow.list_mentions(record_id=LESSON_C)
            if row["surface"] == "他"
        )
        with self.assertRaises(ValueError):
            self.workflow.assign_mention(mention["mention_id"], "missing")
        self.assertTrue(
            card_for(self.workflow, LESSON_C, "表扬")["subject"]["entity_id"]
            is None
        )


class MergeSplitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workflow = TextWorkflow(
            RecordRepository(Path(self.directory.name) / "records.json")
        )
        self.workflow.ingest(json.loads(FIXTURE.read_text(encoding="utf-8")))

    def test_merge_records_members_and_flags_dependent_cards(self):
        snapshot = self.workflow.merge_entities(["person-li-jia", "person-li-yi"])
        active = [row for row in snapshot["entities"] if row["status"] == "active"]
        merged = [row for row in snapshot["entities"] if row["status"] == "merged"]
        self.assertEqual(
            [row["entity_id"] for row in active if row["entity_id"].startswith("person-li")],
            ["person-li-jia"],
        )
        self.assertEqual(merged[0]["merged_into"], "person-li-jia")
        self.assertEqual(
            card_for(self.workflow, LESSON_C, "擅长")["subject"]["entity_id"],
            "person-li-jia",
        )

    def test_split_does_not_let_one_person_inherit_another_relation(self):
        self.workflow.merge_entities(["person-li-jia", "person-li-yi"])
        self.assertEqual(
            card_for(self.workflow, LESSON_C, "擅长")["subject"]["entity_id"],
            "person-li-jia",
        )
        snapshot = self.workflow.split_entities("person-li-jia", ["person-li-yi"])
        self.assertEqual(
            card_for(self.workflow, LESSON_C, "喜欢")["subject"]["entity_id"],
            "person-li-jia",
        )
        self.assertEqual(
            card_for(self.workflow, LESSON_C, "擅长")["subject"]["entity_id"],
            "person-li-yi",
        )
        self.assertEqual(
            card_for(self.workflow, LESSON_C, "介绍")["object"]["entity_id"],
            "person-li-jia",
        )
        self.assertEqual(
            card_for(self.workflow, LESSON_C, "表扬")["object"]["entity_id"],
            "person-li-yi",
        )
        self.assertEqual(
            card_for(self.workflow, LESSON_C, "同名")["object"]["entity_id"],
            "person-li-yi",
        )
        active_ids = {
            row["entity_id"]
            for row in snapshot["entities"]
            if row["status"] == "active"
        }
        self.assertIn("person-li-yi", active_ids)

    def test_split_requires_entity_to_be_part_of_that_merge(self):
        with self.assertRaises(ValueError):
            self.workflow.split_entities("person-li-jia", ["person-li-yi"])
        self.workflow.merge_entities(["person-li-jia", "person-li-yi"])
        with self.assertRaises(ValueError):
            self.workflow.split_entities("person-li-jia", ["teacher-wang"])

    def test_merged_entity_cannot_be_used_as_manual_referent(self):
        self.workflow.merge_entities(["person-li-jia", "person-li-yi"])
        mention = next(
            row
            for row in self.workflow.list_mentions(record_id=LESSON_C)
            if row["surface"] == "他"
        )
        with self.assertRaises(ValueError):
            self.workflow.assign_mention(mention["mention_id"], "person-li-yi")

    def test_manual_mention_decision_survives_merge_and_split(self):
        mention = next(
            row
            for row in self.workflow.list_mentions(record_id=LESSON_C)
            if row["surface"] == "他"
        )
        self.workflow.assign_mention(mention["mention_id"], "teacher-wang")
        self.workflow.merge_entities(["person-li-jia", "person-li-yi"])
        self.workflow.split_entities("person-li-jia", ["person-li-yi"])
        card = card_for(self.workflow, LESSON_C, "表扬")
        self.assertEqual(card["subject"]["entity_id"], "teacher-wang")
        self.assertEqual(card["status"], "resolved")

    def test_merged_target_can_be_split_out_with_a_new_target(self):
        self.workflow.merge_entities(["person-li-jia", "person-li-yi"])
        snapshot = self.workflow.split_entities(
            "person-li-jia", ["person-li-jia"], target_entity_id="person-li-yi"
        )
        active = {
            row["entity_id"]
            for row in snapshot["entities"]
            if row["status"] == "active"
        }
        self.assertIn("person-li-jia", active)
        self.assertIn("person-li-yi", active)
        self.assertEqual(
            card_for(self.workflow, LESSON_C, "喜欢")["subject"]["entity_id"],
            "person-li-jia",
        )
        self.assertEqual(
            card_for(self.workflow, LESSON_C, "擅长")["subject"]["entity_id"],
            "person-li-yi",
        )

    def test_split_without_remaining_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            self.workflow.split_entities("person-li-jia", ["person-li-jia"])


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workflow = TextWorkflow(
            RecordRepository(Path(self.directory.name) / "records.json")
        )
        self.workflow.ingest(json.loads(FIXTURE.read_text(encoding="utf-8")))

    def test_confirmation_records_actor_and_survives_reload(self):
        card = card_for(self.workflow, LESSON_C, "介绍")
        self.workflow.confirm_relation(card["relation_id"], actor="王老师")
        reopened = TextWorkflow(RecordRepository(Path(self.directory.name) / "records.json"))
        confirmed = card_for(reopened, LESSON_C, "介绍")
        self.assertTrue(confirmed["confirmed"])
        self.assertEqual(confirmed["review_status"], "confirmed")
        self.assertEqual(confirmed["confirmed_by"], "王老师")
        self.assertIsNotNone(confirmed["confirmed_at"])

    def test_ambiguous_relation_cannot_be_confirmed(self):
        card = card_for(self.workflow, LESSON_C, "表扬")
        with self.assertRaises(ValueError):
            self.workflow.confirm_relation(card["relation_id"])

    def test_correcting_endpoint_marks_confirmation_stale(self):
        card = card_for(self.workflow, LESSON_C, "介绍")
        self.workflow.confirm_relation(card["relation_id"])
        mention = next(
            row
            for row in self.workflow.list_mentions(record_id=LESSON_C)
            if row["surface"] == "王老师"
        )
        self.workflow.assign_mention(mention["mention_id"], "person-li-jia")
        changed = card_for(self.workflow, LESSON_C, "介绍")
        self.assertEqual(changed["subject"]["entity_id"], "person-li-jia")
        self.assertFalse(changed["confirmed"])
        self.assertTrue(changed["confirmation_stale"])
        self.assertEqual(changed["review_status"], "needs_reconfirmation")

    def test_new_revision_marks_confirmation_stale(self):
        card = card_for(self.workflow, LESSON_C, "介绍")
        self.workflow.confirm_relation(card["relation_id"])
        record = next(
            row for row in self.workflow.list_records() if row["record_id"] == LESSON_C
        )
        revised = dict(record)
        revised.pop("analysis")
        revised["revision"] = 2
        self.workflow.ingest([revised])
        changed = card_for(self.workflow, LESSON_C, "介绍")
        self.assertEqual(changed["record_revision"], 2)
        self.assertTrue(changed["confirmation_stale"])
        self.assertEqual(changed["review_status"], "needs_reconfirmation")

    def test_search_filters_by_review_status(self):
        card = card_for(self.workflow, LESSON_C, "介绍")
        self.workflow.confirm_relation(card["relation_id"])
        confirmed = self.workflow.search(review_status="confirmed")
        self.assertEqual([row["relation_id"] for row in confirmed], [card["relation_id"]])
        unreviewed = self.workflow.search(record_id=LESSON_D, review_status="unreviewed")
        self.assertEqual(len(unreviewed), 1)


class StateFormatTests(unittest.TestCase):
    def test_legacy_record_only_state_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.json"
            path.write_text(
                json.dumps(
                    {
                        "records": [
                            {
                                "record_id": "legacy",
                                "source": "synthetic",
                                "revision": 1,
                                "observed_at": "2026-11-18T09:00:00Z",
                                "text": "旧记录",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            workflow = TextWorkflow(RecordRepository(path))
            self.assertEqual(workflow.list_records()[0]["record_id"], "legacy")
            self.assertEqual(workflow.list_cards(), [])
            workflow.ingest([])
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(persisted), {"records", "decisions", "events"})

    def test_conflicting_duplicate_revision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = TextWorkflow(RecordRepository(Path(directory) / "records.json"))
            record = {"record_id": "one", "source": "synthetic", "revision": 1,
                      "observed_at": "2026-11-18T09:00:00Z", "text": "第一版"}
            workflow.ingest([record])
            with self.assertRaises(ValueError):
                workflow.ingest([{**record, "text": "同一修订的不同正文"}])
            self.assertEqual(workflow.list_records()[0]["text"], "第一版")

    def test_events_record_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = TextWorkflow(RecordRepository(Path(directory) / "records.json"))
            workflow.ingest(json.loads(FIXTURE.read_text(encoding="utf-8")))
            mention = next(
                row
                for row in workflow.list_mentions(record_id=LESSON_C)
                if row["surface"] == "他"
            )
            workflow.assign_mention(mention["mention_id"], "teacher-wang", actor="李老师")
            actions = [event["action"] for event in workflow.list_events()]
            self.assertEqual(actions, ["ingest", "assign_mention"])
            self.assertEqual(workflow.list_events()[1]["actor"], "李老师")


if __name__ == "__main__":
    unittest.main()
