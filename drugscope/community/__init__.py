"""Community research: accounts, quotas, reports, votes, moderation, admin.

Everything here runs server-side inside the Streamlit process. The browser only
ever receives rendered output, so the permission checks in `service.py` are the
backend enforcement - no page or button is trusted to hide anything.
"""
