"""Applications that make up the platform.

Each subdirectory is deployed on its own schedule and scales on its own, so nothing is
imported across the boundary: the API reaches shared code through ``packages`` rather than
through a sibling application.
"""