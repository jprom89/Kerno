"""Background scheduler — batch jobs that run outside of the request cycle.

Three jobs live here: the nightly retrieval-bias recalculation (KER-201), the
AI-decision log prune (KER-203), and the DORA submission-deadline check. The
first two are cron entrypoints (python -m src.scheduler.<module>); they exist
as batch jobs because their work spans every tenant and must not block requests.

How:   pytest tests/unit/scheduler/ -v
"""
