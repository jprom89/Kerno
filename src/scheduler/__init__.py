"""Background scheduler — batch jobs that run outside of the request cycle.

Currently contains one job: the nightly retrieval bias recalculation, which
reads each active tenant's human overrides and updates their personalised
search calibration vector. (KER-114.)
"""
