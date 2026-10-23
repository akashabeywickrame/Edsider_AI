"""Uploaded papers and pictures in file storage (src/files.py), not in the
database: the database keeps where each one is; a file still held in the
database is read from there until scripts/move_files_to_storage.py moves
it. The tests use a folder of their own (conftest.py), never the real
storage account."""

import sys
from pathlib import Path

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import files, store

PDF = b"%PDF-1.4 a real paper would be longer"
PNG = b"\x89PNG\r\n\x1a\n a picture"


def test_a_folder_keeps_files_and_never_leaves_itself(tmp_path):
    files.use_folder(tmp_path)
    files.put("papers/1/abc.pdf", PDF)
    assert files.get("papers/1/abc.pdf") == PDF
    assert files.get("papers/1/missing.pdf") is None
    files.delete("papers/1/abc.pdf")
    assert files.get("papers/1/abc.pdf") is None
    with pytest.raises(ValueError):
        files.put("../outside.txt", b"x")


def test_names_say_whose_file_it_is():
    assert files.paper_name(83, "GIT Gr12.PDF", PDF).startswith("papers/83/")
    assert files.paper_name(83, "GIT Gr12.PDF", PDF).endswith(".pdf")
    assert files.paper_name(83, "x.pdf", PDF) == files.paper_name(83, "y.pdf", PDF)
    assert files.image_name(5012, 22, "image/png") == "images/5012/22.png"
    assert files.image_name(5012, 23, "image/jpeg") == "images/5012/23.jpg"


def _row(conn, sql, *params):
    return conn.execute(sql, params).fetchone()


def test_an_upload_goes_to_file_storage_not_the_database(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "paper.pdf", file_type="pdf", file_blob=PDF)
        row = _row(conn, "SELECT file_path, file_size, file_sha256, file_blob "
                         "FROM intake.document_file WHERE document_id = ?", doc_id)
        q_id = store.add_intake_question(conn, doc_id, 1, "A question")
        image_id = store.add_question_image(conn, q_id, "question", PNG, "image/png")
        image = _row(conn, "SELECT image_path, image_size, image_blob "
                           "FROM intake.question_image WHERE id = ?", image_id)
        assert store.intake_file_blob(conn, doc_id) == PDF
        assert store.get_question_image(conn, image_id)["image_blob"] == PNG
    assert row["file_blob"] is None and row["file_size"] == len(PDF)
    assert row["file_sha256"] == files.checksum(PDF)
    assert files.get(row["file_path"]) == PDF
    assert image["image_blob"] is None and image["image_path"] == f"images/{q_id}/{image_id}.png"
    assert files.get(image["image_path"]) == PNG


def test_deleting_a_picture_or_its_question_deletes_the_file(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "paper.pdf")
        q1 = store.add_intake_question(conn, doc_id, 1, "One")
        q2 = store.add_intake_question(conn, doc_id, 2, "Two")
        a = store.add_question_image(conn, q1, "question", PNG, "image/png")
        b = store.add_question_image(conn, q2, "answer", PNG, "image/png")
        store.delete_question_image(conn, a)
        store.delete_intake_question(conn, q2)
        left = conn.execute("SELECT count(*) AS n FROM intake.question_image").fetchone()["n"]
    assert left == 0
    assert files.get(f"images/{q1}/{a}.png") is None
    assert files.get(f"images/{q2}/{b}.png") is None


def test_files_still_in_the_database_are_read_there_then_moved(local_db):
    with store.connect() as conn:
        doc_id = store.create_intake_document(conn, "old.pdf")
        q_id = store.add_intake_question(conn, doc_id, 1, "A question")
        # As a paper uploaded before file storage: the bytes in the row.
        conn.execute("UPDATE intake.document_file SET file_blob = ? WHERE document_id = ?",
                     (psycopg2.Binary(PDF), doc_id))
        image_id = conn.execute(
            "INSERT INTO intake.question_image (question_id, slot, image_blob, content_type) "
            "VALUES (?, 'question', ?, 'image/png') RETURNING id",
            (q_id, psycopg2.Binary(PNG))).fetchone()["id"]
        assert store.intake_file_blob(conn, doc_id) == PDF
        assert store.get_question_image(conn, image_id)["image_blob"] == PNG
        todo = store.files_in_database(conn)
    assert todo == [("image", image_id, len(PNG)), ("paper", doc_id, len(PDF))]

    for kind, item_id, _ in todo:
        with store.connect() as conn:
            store.move_file_to_storage(conn, kind, item_id)
    with store.connect() as conn:
        assert store.files_in_database(conn) == []
        assert store.intake_file_blob(conn, doc_id) == PDF
        assert store.get_question_image(conn, image_id)["image_blob"] == PNG
        row = _row(conn, "SELECT file_blob, file_path FROM intake.document_file "
                         "WHERE document_id = ?", doc_id)
        # Moving again changes nothing.
        store.move_file_to_storage(conn, "paper", doc_id)
    assert row["file_blob"] is None and files.get(row["file_path"]) == PDF
