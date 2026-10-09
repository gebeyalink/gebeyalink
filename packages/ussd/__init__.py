"""The USSD channel: a session engine, a tree of menus, and the renderer for both.

A feature phone dials the short code and the gateway sends every keypress to
``POST /v1/ussd/session``. This package is the half of that path that holds no database and no
web framework: it decides which menu is owed, keeps the session in the store between the
gateway's calls, and renders the words from the same i18n registry the SMS pipeline uses.

The application half lives in ``apps/api/services/ussd_gateway.py`` and answers the engine's
questions — who owns this number, what a card says, what a choice does — in domain terms. The
seam is a Protocol, so the engine can be exercised without a database and the trees can be
read as data rather than as a call graph.
"""
