"""Operator commands run beside the API, not through it.

The three commands here are the ones an operator runs from a terminal during the pilot:
approving an agent, revoking one, and (later) whatever the admin console takes over. They are
deliberately *not* HTTP routes. An agent approval is a decision made once, by an operator who
has checked the person's identity out of band; giving it a route would put a privileged
endpoint on the public surface for a decision that is not made in the field. The proper admin
console replaces these with routes in FR-1301-1302.

Each command builds its own session, runs one transaction, prints one line, and exits with a
status a shell can branch on. They read the same configuration the API reads, so they run
against whatever deployment the environment points at and never against a second, silent one.
"""
