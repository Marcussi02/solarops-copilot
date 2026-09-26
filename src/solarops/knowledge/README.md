# Knowledge base

Operations guidance that the copilot retrieves and cites. Each file is one document: a `# Title` followed by `##` sections, and each section becomes one retrievable chunk (split further if long).

These notes were written for this project from general, publicly known solar O&M and NEM practice. They are general guidance, not manufacturer instructions: always follow site procedures and equipment manuals. This README is not indexed.

To add a document, drop a Markdown file here and add questions that it should answer to `evals/retrieval.json`; CI fails if retrieval quality drops below the gate.
