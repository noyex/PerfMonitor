# Contributing

Thanks for helping. PerfMonitor is maintained by [@noyex](https://github.com/noyex), who reviews and merges every
pull request. Nobody else merges into `main`.

## Before you start

- For anything bigger than a small fix, open an issue first and describe what you want to change and why. A PR that
  nobody agreed on may be closed.
- One pull request = one change. Don't mix a feature, a refactor and formatting in one PR.

## How to make a pull request

1. Fork the repository and create a branch from `main`: `feat/<short-name>`, `fix/<short-name>`, `docs/<short-name>`.
2. Set up the source checkout (see [Development](README.md#development)) and make your change.
3. Commit with [Conventional Commits](https://www.conventionalcommits.org/) in English, imperative mood:
   `feat: add a GPU hotspot chart`, `fix: keep the session open after a collector reload`.
   Small, logical commits; every commit should work on its own.
4. Run the tests: `python -m unittest discover -s tests -t .`. Add tests for new logic and bug fixes.
5. Open the PR against `main` and fill in the template:
   - **What** changes and **why**,
   - **How to test** it,
   - **Screenshots** or a short video for anything visible in the UI.
6. CI must pass. The maintainer may ask for changes; push them to the same branch (no force-push after review starts).
   PRs are squash-merged.

## Conventions

- Code, comments, commits and PRs in English. The UI text is Polish: keep new UI copy in Polish.
- Match the surrounding code: naming, comment density, no new dependencies without a reason in the PR.
- The collector runs elevated in the background during games: keep it light (no heavy work on the sampling loop) and
  never do anything that kernel anti-cheats could treat as tampering with a game process.
- Everything stays on the user's machine. No telemetry, no new network calls without discussing it first.
- Never commit your own `data/` folder, databases, logs or traces.

## Reporting bugs

Open an issue with the Windows version, CPU/GPU, what happened and when, and the output of `pm status`. Attach
`collector.log` (from `%LOCALAPPDATA%\PerfMonitor`) only after checking it contains nothing you don't want to share.
