"""Paper intake - turn an uploaded question paper into reviewable, labelled
questions, then check that batch for repeats before it joins the bank.

This is "Project 2" from the original proposal (read a paper, fill in the
labels), kept in its own package so it sits beside the matching engine
(src/dedupe/) rather than inside it. It depends on src/dedupe/ for the
duplicate check at the end of the flow, never the other way round.

The flow:

    upload  ->  extract  ->  human review / edit  ->  within-batch
                                                      duplicate check

Extraction is deliberately local-only (pdfplumber for a PDF text layer,
python-docx for Word, Tesseract for images and scans) - no text ever
leaves the machine, same as the embedding model. Local extraction is
weaker on handwriting, which is exactly why the review step is not
optional: nothing here is trusted until a person has confirmed it.
"""

from . import extract, review

__all__ = ["extract", "review"]
