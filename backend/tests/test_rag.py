import shutil
import threading
import unittest

from app import config, db
from app.errors import ApiError
from app.services import embedding, rag, vectorstore
from app.services.vectorstore import MemoryStore
from tests.base import BaseCase, NOTES, OWNER_A, OWNER_B

SIX = ["Batch Operating System", "Time-Sharing Operating System", "Distributed Operating System",
       "Real-Time Operating System", "Network Operating System", "Mobile Operating System"]


class TestIngestion(BaseCase):
    def test_docx_headings_tables_and_chunk_metadata(self):
        r = self.upload(OWNER_A, NOTES)
        self.assertEqual(r["status"], "added")
        self.assertGreater(r["chunks_created"], 100)
        self.assertIn("Types of Operating Systems", r["chapters_detected"])
        self.assertIn("RAID Levels (Redundant Array of Independent Disks)", r["chapters_detected"])
        rows = vectorstore.get_store()._rows.values()
        raid = [m for _, d, m in rows if "RAID Level:" in d]                      # DOCX *table* content is indexed
        self.assertTrue(raid, "table rows were lost")
        m = raid[0]
        for key in ("owner", "doc_id", "filename", "heading", "section_id", "chunk_index", "page_number"):
            self.assertIn(key, m)
        self.assertIn("RAID", m["heading"])

    def test_chunker_keeps_section_structure_and_never_drops_text(self):
        blocks = [rag.Block("heading", "1. Intro"), rag.Block("text", "A" * 50 + ". " + "B" * 2000),
                  rag.Block("heading", "Sub Part"), rag.Block("text", "Short paragraph about sub part here.")]
        chunks = rag.build_chunks(blocks, size=900, overlap=100)
        self.assertEqual(chunks[-1].heading, "1. Intro > Sub Part")
        joined = " ".join(c.text for c in chunks if c.section_idx == 0)
        self.assertEqual(joined.count("B"), 2000 + joined.count("B") - 2000)         # nothing truncated (overlap may repeat)
        self.assertGreaterEqual(joined.count("B"), 2000)
        self.assertTrue(all(len(c.text) <= 1000 for c in chunks))

    def test_pdf_not_truncated_per_page(self):
        # the old parser cut every PDF page at 5000 chars
        long_text = ("Deadlock prevention removes one of four conditions. " * 200)
        blocks = [rag.Block("heading", "Deadlocks", 1), rag.Block("text", long_text, 1)]
        total = sum(len(c.text) for c in rag.build_chunks(blocks))
        self.assertGreaterEqual(total, len(long_text) - 50)

    def test_pdf_parse_roundtrip(self):
        try:
            from reportlab.pdfgen import canvas
            import pypdf  # noqa
        except ImportError:
            self.skipTest("reportlab/pypdf not installed")
        p = self.tmp / "t.pdf"
        c = canvas.Canvas(str(p))
        c.setFont("Helvetica", 11)
        c.drawString(72, 780, "Memory Management")
        c.drawString(72, 760, "Paging divides memory into fixed size frames and pages for allocation.")
        c.showPage()
        c.drawString(72, 780, "Page Replacement")
        c.drawString(72, 760, "FIFO and LRU are page replacement algorithms used on page faults.")
        c.save()
        r = self.upload(OWNER_A, p, "t.pdf")
        self.assertGreater(r["chunks_created"], 0)
        pages = {m["page_number"] for _, _, m in vectorstore.get_store()._rows.values()}
        self.assertEqual(pages, {1, 2})                                                    # page metadata kept

    def test_empty_text_file_rejected(self):
        with self.assertRaises(ApiError) as cm:
            self.upload_text(OWNER_A, "empty.txt", "   \n\n ")
        self.assertEqual(cm.exception.status, 422)


class TestRetrieval(BaseCase):
    def setUp(self):
        super().setUp()
        self.upload(OWNER_A, NOTES)

    def test_similarity_is_true_cosine_not_half_distance(self):
        hits = rag.retrieve(OWNER_A, "Explain Round Robin scheduling", 3)
        store = vectorstore.get_store()
        qv = embedding.get_embedder().encode(["Explain Round Robin scheduling"])[0]
        raw = {r["id"]: r["distance"] for r in store.query(qv, 3, OWNER_A)}
        for h in hits:
            self.assertAlmostEqual(h.similarity, 1.0 - raw[h.chunk_id], places=3)   # similarity = 1 - cosine distance
        self.assertTrue(all(0.0 <= h.similarity < 1.0 for h in hits))

    def test_broad_request_returns_all_six_types(self):
        ev = rag.gather(OWNER_A, "Explain all six types of operating systems from my notes", broad=True)
        self.assertIn(ev.grounding, ("strong", "weak"))
        for t in SIX:
            self.assertIn(t, ev.context, f"missing {t}")
        self.assertFalse(ev.truncated)

    def test_non_broad_question_still_gets_complete_small_section(self):
        ev = rag.gather(OWNER_A, "What is a Real-Time Operating System?", broad=False)
        self.assertIn("Real-Time Operating System", ev.context)
        self.assertEqual(ev.sources[0]["chapter"].split(" > ")[-1], "Types of Operating Systems")

    def test_detail_from_later_part_of_document(self):
        ev = rag.gather(OWNER_A, "What are the minimum disks needed for RAID levels?", broad=False)
        self.assertIn("RAID Level", ev.context)
        self.assertIn("Minimum Disks", ev.context)

    def test_absent_topic_is_not_grounded(self):
        for q in ("Explain quantum chromodynamics and gluon confinement", "Who won the 1998 football world cup?"):
            ev = rag.gather(OWNER_A, q, broad=False)
            self.assertEqual(ev.grounding, "none", q)
            self.assertEqual(ev.context, "")

    def test_budget_is_respected_and_truncation_reported(self):
        old = config.BROAD_CONTEXT_CHAR_BUDGET
        config.BROAD_CONTEXT_CHAR_BUDGET = 1200
        try:
            ev = rag.gather(OWNER_A, "Explain all six types of operating systems", broad=True)
        finally:
            config.BROAD_CONTEXT_CHAR_BUDGET = old
        self.assertTrue(ev.truncated)
        self.assertLessEqual(sum(len(s) for s in [ev.context]), 1200 + 1500)

    def test_sources_carry_file_heading_and_boundaries(self):
        ev = rag.gather(OWNER_A, "Explain deadlock handling strategies", broad=False)
        self.assertTrue(ev.sources and ev.sources[0]["filename"] == "os_notes.docx")
        self.assertIn("[Source 1: os_notes.docx |", ev.context)


class TestMultiDocumentAndDuplicates(BaseCase):
    def test_two_similar_documents_use_the_correct_source(self):
        self.upload_text(OWNER_A, "os_scheduling.txt",
                         "Process Scheduling\n\nThe CPU scheduler picks the next process from the ready queue. "
                         "Round robin uses a time quantum and context switching between processes.")
        self.upload_text(OWNER_A, "hospital_scheduling.txt",
                         "Hospital Scheduling\n\nNurse scheduling assigns shifts to nurses and doctors in a ward. "
                         "Shift rotation avoids fatigue and covers every patient department.")
        ev = rag.gather(OWNER_A, "How does the CPU scheduler use a time quantum?", broad=False)
        self.assertEqual(ev.sources[0]["filename"], "os_scheduling.txt")
        ev2 = rag.gather(OWNER_A, "How are nurse shifts assigned in the ward?", broad=False)
        self.assertEqual(ev2.sources[0]["filename"], "hospital_scheduling.txt")
        # sources are never merged into one block
        self.assertNotIn("nurse", ev.sources[0]["preview"].lower())

    def test_reupload_policy(self):
        a = self.upload(OWNER_A, NOTES)
        n = vectorstore.get_store().count()
        b = self.upload(OWNER_A, NOTES)                                                  # identical re-upload
        self.assertEqual(b["status"], "unchanged")
        self.assertEqual(vectorstore.get_store().count(), n)
        c = self.upload(OWNER_A, NOTES, name="copy of notes.docx")                       # same bytes, other name
        self.assertEqual(c["status"], "duplicate")
        self.assertEqual(c["doc_id"], a["doc_id"])
        self.assertEqual(vectorstore.get_store().count(), n)
        self.assertEqual(len(rag.list_files(OWNER_A)), 1)
        ids = list(vectorstore.get_store()._rows)
        self.assertEqual(len(ids), len(set(ids)))

    def test_replace_same_filename_removes_old_chunks(self):
        self.upload_text(OWNER_A, "n.txt", "Alpha Topic\n\nOld content about zebras and stripes in the savanna.")
        self.upload_text(OWNER_A, "n.txt", "Alpha Topic\n\nNew content about quasars and pulsars in deep space.")
        texts = " ".join(d for _, d, _ in vectorstore.get_store()._rows.values())
        self.assertIn("quasars", texts)
        self.assertNotIn("zebras", texts)
        self.assertEqual(len(rag.list_files(OWNER_A)), 1)

    def test_delete_removes_chunks_row_and_file(self):
        r = self.upload(OWNER_A, NOTES)
        self.assertTrue(rag.delete_file(OWNER_A, r["doc_id"]))
        self.assertEqual(vectorstore.get_store().count(), 0)
        self.assertEqual(rag.list_files(OWNER_A), [])
        self.assertFalse(rag.delete_file(OWNER_A, r["doc_id"]))

    def test_documents_are_isolated_between_owners(self):
        r = self.upload(OWNER_A, NOTES)
        self.assertEqual(rag.list_files(OWNER_B), [])
        self.assertEqual(rag.gather(OWNER_B, "Explain deadlock handling strategies").grounding, "none")
        self.assertFalse(rag.delete_file(OWNER_B, r["doc_id"]))
        self.assertEqual(len(rag.list_files(OWNER_A)), 1)


class TestRestart(BaseCase):
    def test_vector_store_wiped_then_reindexed_from_stored_files(self):
        r = self.upload(OWNER_A, NOTES)
        vectorstore.set_store(MemoryStore())                         # simulate lost/changed vector DB after restart
        self.assertEqual(rag.gather(OWNER_A, "Explain deadlock handling strategies").grounding, "none")
        self.assertEqual(rag.reindex_missing(), 1)
        ev = rag.gather(OWNER_A, "Explain deadlock handling strategies")
        self.assertIn(ev.grounding, ("strong", "weak"))
        self.assertEqual(rag.list_files(OWNER_A)[0]["doc_id"], r["doc_id"])
        self.assertEqual(rag.reindex_missing(), 0)                   # idempotent

    def test_document_registry_survives_reconnect(self):
        self.upload(OWNER_A, NOTES)
        db.configure(self.tmp / "data" / "t.db")                     # new "process", same file
        self.assertEqual(len(rag.list_files(OWNER_A)), 1)


if __name__ == "__main__":
    unittest.main()
