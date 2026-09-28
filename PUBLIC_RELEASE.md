# Public release notes

This repository is a privacy-filtered source release of Sebas（セバス）.

Excluded from this release:

- `.env` and credentials
- runtime databases, memory, logs, backups, uploads, and generated outputs
- internal build reports and deployment manifests
- organization-specific names and workstation/network paths

The code keeps OCR safety gates: failed, review-required, unapproved, or integrity-invalid OCR results must not flow into calculation or experience RAG. OCR remains disabled by default unless explicitly enabled.

Known incomplete acceptance work:

- OCR acceptance with representative real PDFs
- end-to-end acceptance for the vehicle monthly profit/loss workflow
- remaining business confirmation questions

Sebas is licensed under the Apache License, Version 2.0. Third-party libraries and container images remain subject to their respective licenses.
