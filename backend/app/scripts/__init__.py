"""One-off commands an operator runs against a live instance.

Separate from the repository's top-level scripts/, which are development tooling
(benchmarks, generators) and never ship: the Docker image copies backend/ only, so
anything that has to run on the server lives here and is invoked as a module,
`python -m app.scripts.<name>`.
"""
