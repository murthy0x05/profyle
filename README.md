Profyle - Candidate Data Ingestion & Merge CLI

Prerequisites:
Python 3.11+
pip

Installation:
pip install -r requirements.txt

Run with Sample Data:
python main.py --sources data/sample/recruiter_export.csv data/sample/ats_blob.json data/sample/recruiter_notes.txt --config config/default_config.json --out candidates/

Run with Custom Config:
python main.py --sources data/sample/recruiter_export.csv data/sample/ats_blob.json data/sample/recruiter_notes.txt --config config/custom_config.json --out candidates/

Run Tests:
pytest tests/ -v
